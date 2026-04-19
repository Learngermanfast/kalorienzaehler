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

GEMINI_API_KEY = "AIzaSyCCJFaxlc7Xsb7WHjmG-Kty7dhRkz05M2Y"


def load_meals():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_meals(meals):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(meals, f, ensure_ascii=False, indent=2)


def analyze_meal(plate: Optional[dict], items: List[dict]) -> dict:
    import google.generativeai as genai
    from google.generativeai.types import HarmCategory, HarmBlockThreshold

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")

    parts = []
    sections = []

    if plate:
        if plate.get("image_base64"):
            parts.append({
                "inline_data": {
                    "mime_type": plate.get("image_mime") or "image/jpeg",
                    "data": plate["image_base64"],
                }
            })
            sections.append("TELLER-FOTO: Siehst du oben. " + (plate.get("description") or "Bitte analysiere den Inhalt."))
        elif plate.get("description"):
            sections.append("MAHLZEIT-BESCHREIBUNG: " + plate["description"])

    for i, item in enumerate(items, 1):
        if item.get("image_base64"):
            parts.append({
                "inline_data": {
                    "mime_type": item.get("image_mime") or "image/jpeg",
                    "data": item["image_base64"],
                }
            })
            sections.append(f"PRODUKT {i} (Foto weiter oben): {item['description']}")
        else:
            sections.append(f"PRODUKT {i}: {item['description']}")

    has_plate = bool(plate and (plate.get("image_base64") or plate.get("description")))
    has_products = bool(items)

    if has_plate and has_products:
        instruction = "Du hast ein Teller-Foto und zusätzliche Produktfotos erhalten. Nutze Produktetiketten für exakte Werte, ergänze Rest aus Teller-Foto."
    elif has_products:
        instruction = "Lies Nährwertetiketten direkt ab und rechne auf die angegebene Menge um."
    else:
        instruction = "Schätze alle Zutaten und Mengen anhand des Fotos und der Beschreibung."

    prompt = f"""{instruction}

{chr(10).join(sections)}

Antworte NUR mit einem JSON-Objekt (keine Erklärungen außerhalb):
{{
  "name": "Kurzname der Mahlzeit (max. 40 Zeichen)",
  "kalorien": <ganze Zahl>,
  "protein_g": <Zahl, 1 Dezimalstelle>,
  "kohlenhydrate_g": <Zahl, 1 Dezimalstelle>,
  "fett_g": <Zahl, 1 Dezimalstelle>,
  "produkte": [
    {{"name": "Zutat", "menge": "30g", "kalorien": 120}}
  ],
  "notiz": "Kurze Anmerkung zur Genauigkeit"
}}"""

    parts.append({"text": prompt})

    response = model.generate_content(
        parts,
        safety_settings={
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
    )

    raw = response.text.strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    return json.loads(raw)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/log", methods=["POST"])
def log_meal():
    plate = None
    plate_desc = request.form.get("plate_description", "").strip()
    plate_file = request.files.get("plate_image")
    if plate_desc or (plate_file and plate_file.filename):
        plate = {"description": plate_desc, "image_base64": None, "image_mime": None}
        if plate_file and plate_file.filename:
            plate["image_base64"] = base64.standard_b64encode(plate_file.read()).decode("utf-8")
            plate["image_mime"] = plate_file.content_type or "image/jpeg"

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

    plate_summary = None
    if plate:
        plate_summary = {"beschreibung": plate.get("description") or "", "hat_foto": bool(plate.get("image_base64"))}

    entry = {
        "id": len(meals[today]) + 1,
        "zeit": datetime.datetime.now().strftime("%H:%M"),
        "plate": plate_summary,
        "items": [{"beschreibung": it["description"], "hat_foto": bool(it["image_base64"])} for it in items],
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
    print("Kalorienzähler läuft — öffne den Port 5000 Link oben in Codespaces")
    app.run(host="0.0.0.0", debug=True, port=5000)
