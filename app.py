import os
import json
import base64
import re
from typing import Optional, List
from datetime import date
from pathlib import Path
from flask import Flask, request, jsonify, render_template

app = Flask(__name__, template_folder="templates", static_folder="static")

DATA_FILE = Path(__file__).parent / "data" / "meals.json"
DATA_FILE.parent.mkdir(exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def load_meals():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_meals(meals):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(meals, f, ensure_ascii=False, indent=2)


def analyze_meal(
    plate: Optional[dict],   # { image_base64, image_mime, description }
    items: List[dict],        # [{ description, image_base64, image_mime }]
) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    content = []
    sections = []

    # ── Plate photo ──
    if plate:
        if plate.get("image_base64"):
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": plate.get("image_mime") or "image/jpeg",
                    "data": plate["image_base64"],
                },
            })
            sections.append("TELLER-FOTO: Siehst du oben. " + (plate.get("description") or "Bitte analysiere den Inhalt des Tellers."))
        elif plate.get("description"):
            sections.append("MAHLZEIT-BESCHREIBUNG: " + plate["description"])

    # ── Product photos ──
    if items:
        for i, item in enumerate(items, 1):
            if item.get("image_base64"):
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": item.get("image_mime") or "image/jpeg",
                        "data": item["image_base64"],
                    },
                })
                sections.append(f"PRODUKT {i} (Foto weiter oben): {item['description']}")
            else:
                sections.append(f"PRODUKT {i}: {item['description']}")

    combined = "\n".join(sections)

    has_plate = bool(plate and (plate.get("image_base64") or plate.get("description")))
    has_products = bool(items)

    if has_plate and has_products:
        instruction = (
            "Du hast ein Teller-Foto und zusätzliche Produktfotos/-angaben erhalten.\n"
            "Nutze die Produktfotos/Etiketten für exakte Label-Werte (auf die angegebene Menge umrechnen). "
            "Ergänze fehlende Zutaten aus dem Teller-Foto durch Schätzung. "
            "Berechne die Gesamtnährwerte für die komplette Mahlzeit."
        )
    elif has_products:
        instruction = (
            "Du hast Produktfotos/-angaben erhalten. "
            "Lies Nährwertetiketten direkt ab, falls sichtbar, und rechne auf die angegebene Menge um. "
            "Berechne die Gesamtnährwerte."
        )
    else:
        instruction = (
            "Du hast ein Foto eines Tellers / einer Mahlzeit erhalten. "
            "Schätze alle Zutaten und Mengen anhand des Fotos und der Beschreibung. "
            "Berechne die Gesamtnährwerte."
        )

    prompt = f"""{instruction}

{combined}

Antworte NUR mit einem JSON-Objekt in genau diesem Format (keine Erklärungen außerhalb des JSON):
{{
  "name": "Kurzname der Mahlzeit (max. 40 Zeichen)",
  "kalorien": <ganze Zahl>,
  "protein_g": <Zahl, 1 Dezimalstelle>,
  "kohlenhydrate_g": <Zahl, 1 Dezimalstelle>,
  "fett_g": <Zahl, 1 Dezimalstelle>,
  "produkte": [
    {{"name": "Zutat oder Produkt", "menge": "z.B. 30g", "kalorien": 120}},
    ...
  ],
  "notiz": "Kurze Anmerkung zur Genauigkeit (1-2 Sätze)"
}}"""

    content.append({"type": "text", "text": prompt})

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=700,
        messages=[{"role": "user", "content": content}],
    )

    raw = message.content[0].text.strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    return json.loads(raw)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/log", methods=["POST"])
def log_meal():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY nicht gesetzt"}), 400

    # ── Plate ──
    plate = None
    plate_desc = request.form.get("plate_description", "").strip()
    plate_file = request.files.get("plate_image")
    if plate_desc or (plate_file and plate_file.filename):
        plate = {"description": plate_desc, "image_base64": None, "image_mime": None}
        if plate_file and plate_file.filename:
            plate["image_base64"] = base64.standard_b64encode(plate_file.read()).decode("utf-8")
            plate["image_mime"] = plate_file.content_type or "image/jpeg"

    # ── Products ──
    items = []
    i = 1
    while True:
        desc = request.form.get(f"description_{i}", "").strip()
        if not desc:
            break
        item = {"description": desc, "image_base64": None, "image_mime": None}
        f = request.files.get(f"image_{i}")
        if f and f.filename:
            item["image_base64"] = base64.standard_b64encode(f.read()).decode("utf-8")
            item["image_mime"] = f.content_type or "image/jpeg"
        items.append(item)
        i += 1

    if not plate and not items:
        return jsonify({"error": "Bitte Teller fotografieren oder Produkte hinzufügen."}), 400

    try:
        result = analyze_meal(plate, items)
    except Exception as e:
        return jsonify({"error": f"Analyse fehlgeschlagen: {str(e)}"}), 500

    import datetime
    today = date.today().isoformat()
    meals = load_meals()
    if today not in meals:
        meals[today] = []

    # Build summary for display
    plate_summary = None
    if plate:
        plate_summary = {
            "beschreibung": plate.get("description") or "",
            "hat_foto": bool(plate.get("image_base64")),
        }

    entry = {
        "id": len(meals[today]) + 1,
        "zeit": datetime.datetime.now().strftime("%H:%M"),
        "plate": plate_summary,
        "items": [
            {"beschreibung": it["description"], "hat_foto": bool(it["image_base64"])}
            for it in items
        ],
        **result,
    }
    meals[today].append(entry)
    save_meals(meals)

    return jsonify({"success": True, "entry": entry, "tages_total": tages_total(meals[today])})


@app.route("/api/today")
def get_today():
    today = date.today().isoformat()
    meals = load_meals()
    day_meals = meals.get(today, [])
    return jsonify({"datum": today, "mahlzeiten": day_meals, "total": tages_total(day_meals)})


@app.route("/api/history")
def get_history():
    meals = load_meals()
    result = []
    for day, entries in sorted(meals.items(), reverse=True):
        result.append({"datum": day, "anzahl": len(entries), "total": tages_total(entries)})
    return jsonify(result)


@app.route("/api/day/<day_str>")
def get_day(day_str):
    meals = load_meals()
    day_meals = meals.get(day_str, [])
    return jsonify({"datum": day_str, "mahlzeiten": day_meals, "total": tages_total(day_meals)})


@app.route("/api/delete/<day_str>/<int:meal_id>", methods=["DELETE"])
def delete_meal(day_str, meal_id):
    meals = load_meals()
    if day_str in meals:
        meals[day_str] = [m for m in meals[day_str] if m.get("id") != meal_id]
        save_meals(meals)
    return jsonify({"success": True})


def tages_total(mahlzeiten: list) -> dict:
    return {
        "kalorien": sum(m.get("kalorien", 0) for m in mahlzeiten),
        "protein_g": round(sum(m.get("protein_g", 0) for m in mahlzeiten), 1),
        "kohlenhydrate_g": round(sum(m.get("kohlenhydrate_g", 0) for m in mahlzeiten), 1),
        "fett_g": round(sum(m.get("fett_g", 0) for m in mahlzeiten), 1),
    }


if __name__ == "__main__":
    print("Kalorienzähler läuft auf http://localhost:5000")
    app.run(debug=True, port=5000)
