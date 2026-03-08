import io
import json
import os
import re
import time
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response, send_file
from dotenv import load_dotenv
import anthropic
from fpdf import FPDF
import db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32))

db.init_db()

PROKERALA_CLIENT_ID = os.getenv("PROKERALA_CLIENT_ID")
PROKERALA_CLIENT_SECRET = os.getenv("PROKERALA_CLIENT_SECRET")
PROKERALA_TOKEN_URL = "https://api.prokerala.com/token"
PROKERALA_BASE_URL = "https://api.prokerala.com/v2/astrology"

_anthropic_client = None
_token_cache = {"access_token": None, "expires_at": 0}


def get_anthropic_client():
    """Lazy-init Anthropic client so the app can start without keys set."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _anthropic_client


def get_prokerala_token():
    """Get a valid OAuth2 token, reusing cached token if still valid."""
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    resp = requests.post(PROKERALA_TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": PROKERALA_CLIENT_ID,
        "client_secret": PROKERALA_CLIENT_SECRET,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
    return _token_cache["access_token"]


def prokerala_get(endpoint, params, accept="application/json"):
    """Make an authenticated GET request to the Prokerala API."""
    token = get_prokerala_token()
    resp = requests.get(
        f"{PROKERALA_BASE_URL}/{endpoint}",
        params=params,
        headers={"Authorization": f"Bearer {token}", "Accept": accept},
        timeout=30,
    )
    resp.raise_for_status()
    if accept == "image/svg+xml":
        return resp.text
    return resp.json()


DATA_STEPS = [
    ("birth_details",       "birth-details",         "Reading your nakshatra & birth star..."),
    ("planet_position",     "planet-position",       "Mapping exact planetary coordinates..."),
    ("kundli_advanced",     "kundli/advanced",       "Building advanced Kundli..."),
    ("kaal_sarp_dosha",     "kaal-sarp-dosha",       "Checking Kaal Sarp Dosha..."),
    ("mangal_dosha",        "mangal-dosha/advanced", "Checking Mangal Dosha (detailed)..."),
    ("sade_sati",           "sade-sati",             "Checking Sade Sati transit..."),
    ("planet_relationship", "planet-relationship",   "Mapping planet relationships..."),
]

CHART_TYPES = [
    ("rasi",    "south-indian", "Drawing Rasi (D1) chart..."),
    ("navamsa", "south-indian", "Drawing Navamsa (D9) chart..."),
    ("lagna",   "south-indian", "Drawing Lagna chart..."),
    ("moon",    "south-indian", "Drawing Moon chart..."),
    ("dasamsa", "south-indian", "Drawing Dasamsa (D10 career) chart..."),
]


def fetch_astrology_data_with_progress(datetime_str, lat, lng):
    """Yield progress messages. Final yield is (results_dict, None, 'final')."""
    base_params = {
        "ayanamsa": 1,
        "coordinates": f"{lat},{lng}",
        "datetime": datetime_str,
    }

    results = {}
    for key, endpoint, message in DATA_STEPS:
        yield message, key, None
        try:
            results[key] = prokerala_get(endpoint, base_params)
        except requests.RequestException:
            results[key] = None

    charts = {}
    for chart_type, chart_style, message in CHART_TYPES:
        yield message, None, None
        try:
            svg = prokerala_get("chart", {
                **base_params,
                "chart_type": chart_type,
                "chart_style": chart_style,
                "format": "svg",
            }, accept="image/svg+xml")
            charts[chart_type] = svg
        except requests.RequestException:
            charts[chart_type] = None

    results["_charts"] = charts
    yield results, None, "final"


def _safe(d, *keys, default=None):
    """Safely traverse nested dicts."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def structure_data_for_llm(raw_results):
    """Transform raw Prokerala API responses into a clean, structured dict for Claude."""
    out = {}

    bd = _safe(raw_results, "birth_details", "data")
    if bd:
        nak = _safe(bd, "nakshatra", 0) or _safe(bd, "nakshatra") or {}
        out["birth_profile"] = {
            "nakshatra": _safe(nak, "name") if isinstance(nak, dict) else str(nak),
            "nakshatra_lord": _safe(nak, "lord", "name"),
            "nakshatra_pada": _safe(nak, "pada"),
            "chandra_rasi": _safe(bd, "chandra_rasi", "name"),
            "soorya_rasi": _safe(bd, "soorya_rasi", "name"),
            "zodiac": _safe(bd, "zodiac", "name"),
            "additional_info": _safe(bd, "additional_info"),
        }

    pp = _safe(raw_results, "planet_position", "data", "planet_position")
    if pp and isinstance(pp, list):
        planets = []
        for p in pp:
            planets.append({
                "name": _safe(p, "name"),
                "longitude_degrees": _safe(p, "longitude"),
                "degree_in_sign": _safe(p, "degree"),
                "sign": _safe(p, "rasi", "name"),
                "sign_lord": _safe(p, "rasi", "lord", "name"),
                "is_retrograde": _safe(p, "is_retrograde"),
                "position": _safe(p, "position"),
            })
        out["planet_positions"] = planets

    ka = _safe(raw_results, "kundli_advanced", "data")
    if ka:
        nak_d = _safe(ka, "nakshatra_details")
        if nak_d:
            out["kundli_nakshatra"] = {
                "nakshatra": _safe(nak_d, "nakshatra", "name"),
                "lord": _safe(nak_d, "nakshatra", "lord", "name"),
                "pada": _safe(nak_d, "pada"),
                "chandra_rasi": _safe(nak_d, "chandra_rasi", "name"),
                "soorya_rasi": _safe(nak_d, "soorya_rasi", "name"),
                "additional_info": _safe(nak_d, "additional_info"),
            }

        md = _safe(ka, "mangal_dosha")
        if md:
            out["mangal_dosha"] = {
                "has_dosha": _safe(md, "has_dosha"),
                "type": _safe(md, "type"),
                "has_exception": _safe(md, "has_exception"),
                "description": _safe(md, "description"),
                "exceptions": _safe(md, "exceptions"),
                "remedies": _safe(md, "remedies"),
            }

        yd = _safe(ka, "yoga_details")
        if yd and isinstance(yd, list):
            active_yogas = []
            for category in yd:
                cat_name = _safe(category, "name") or "Unknown"
                yoga_list = _safe(category, "yoga_list") or []
                for y in yoga_list:
                    if _safe(y, "has_yoga"):
                        active_yogas.append({
                            "category": cat_name,
                            "name": _safe(y, "name"),
                            "has_yoga": True,
                            "description": _safe(y, "description"),
                        })
            out["active_yogas"] = active_yogas

        dp = _safe(ka, "dasha_periods")
        if dp and isinstance(dp, list):
            dashas = []
            for maha in dp:
                entry = {
                    "mahadasha": _safe(maha, "name"),
                    "start": _safe(maha, "start"),
                    "end": _safe(maha, "end"),
                }
                antars = _safe(maha, "antardasha") or []
                if antars:
                    entry["antardasha"] = [
                        {
                            "name": _safe(a, "name"),
                            "start": _safe(a, "start"),
                            "end": _safe(a, "end"),
                        }
                        for a in antars[:8]
                    ]
                dashas.append(entry)
            out["dasha_periods"] = dashas

        db_info = _safe(ka, "dasha_balance")
        if db_info:
            out["current_dasha_balance"] = {
                "lord": _safe(db_info, "lord", "name") if isinstance(_safe(db_info, "lord"), dict) else _safe(db_info, "lord"),
                "duration": _safe(db_info, "duration"),
                "description": _safe(db_info, "description"),
            }

    ksd = _safe(raw_results, "kaal_sarp_dosha", "data")
    if ksd:
        out["kaal_sarp_dosha"] = {
            "has_dosha": _safe(ksd, "has_dosha"),
            "type": _safe(ksd, "type"),
            "dosha_type": _safe(ksd, "dosha_type"),
            "description": _safe(ksd, "description"),
        }

    md_adv = _safe(raw_results, "mangal_dosha", "data")
    if md_adv:
        out["mangal_dosha_detailed"] = {
            "has_dosha": _safe(md_adv, "has_dosha"),
            "type": _safe(md_adv, "type"),
            "has_exception": _safe(md_adv, "has_exception"),
            "exceptions": _safe(md_adv, "exceptions"),
            "remedies": _safe(md_adv, "remedies"),
            "description": _safe(md_adv, "description"),
        }

    ss = _safe(raw_results, "sade_sati", "data")
    if ss:
        out["sade_sati"] = {
            "is_in_sade_sati": _safe(ss, "is_in_sade_sati"),
            "transit_phase": _safe(ss, "transit_phase"),
            "description": _safe(ss, "description"),
        }

    pr = _safe(raw_results, "planet_relationship", "data", "planet_relationship")
    if pr:
        nat = _safe(pr, "natural_relationship") or []
        if nat and isinstance(nat, list):
            rels = []
            for r in nat:
                rels.append({
                    "planet_1": _safe(r, "first_planet", "name"),
                    "planet_2": _safe(r, "second_planet", "name"),
                    "relationship": _safe(r, "relationship"),
                })
            out["planet_relationships"] = rels

    return out


SYSTEM_PROMPT = (
    "Act as an elite, highly technical Vedic Astrologer and Executive Life Coach "
    "named Krama. Your tone is direct, grounded, authoritative, unapologetic, and "
    "highly strategic. Speak like a high-level mentor or business partner — use "
    "\"Bro\", \"Listen\", or direct language. Address the person by name.\n\n"

    "Absolutely NO generic horoscope fluff, NO \"woo-woo\" spiritual jargon, and "
    "NO sugarcoating. Treat planetary alignments strictly as source code, data, "
    "and mathematical probabilities.\n\n"

    "ZERO MATH HALLUCINATION RULE:\n"
    "You must NEVER calculate positions, transits, or chart data yourself. Rely "
    "100% on the Prokerala API data provided. The data includes: birth profile, "
    "exact planet positions with degrees, Mangal Dosha, Kaal Sarp Dosha, Sade Sati "
    "status, active Yogas, Dasha periods, and natural planet relationships.\n"
    "- For Mangal Dosha: ONLY use the 'has_dosha' field. false = NOT Manglik. "
    "true = Manglik. Period.\n"
    "- For Kaal Sarp Dosha: ONLY use the 'has_dosha' field.\n"
    "- For Sade Sati: ONLY use the 'is_in_sade_sati' field.\n"
    "- For Yogas: ONLY mention yogas listed in active_yogas (already filtered to "
    "has_yoga=true). Do not invent yogas.\n"
    "- Quote exact planet longitudes, degree_in_sign, sign names, and retrograde "
    "status from planet_positions. If a section is missing, say so.\n"
    "- Use planet_relationships to identify friends, enemies, and neutral planets "
    "when analyzing conjunctions or aspects.\n\n"

    "RUTHLESS TRANSLATION DIRECTIVE:\n"
    "Translate raw astrological data into modern, real-world execution. Focus heavily "
    "on: wealth architecture, career strategy, psychological blind spots, power "
    "dynamics, and highly calculated relationship profiling.\n\n"

    "REQUIRED FORMAT — use this exact structure for every major insight:\n\n"

    "**The Math:** [Identify the exact planet, house, sign, degree, or Nakshatra "
    "from the API data]\n\n"
    "**The Reality:** [Translate meaning into blunt, psychological, or environmental "
    "reality]\n\n"
    "**The Strategy:** [Give a ruthless, actionable takeaway — how to leverage this "
    "or protect against it]\n\n"

    "SECTIONS (use these exact headings):\n"
    "## Core Identity Architecture\n"
    "Rising sign, Moon sign, Sun sign — decoded as operating system, emotional "
    "firmware, and ego interface. What drives them, what others see, the gap "
    "between the two.\n\n"

    "## Wealth & Career Source Code\n"
    "2nd house, 10th house, 11th house analysis. Natural money-making style, "
    "career moat, income ceiling, and where they leak resources.\n\n"

    "## Relationship & Power Dynamics\n"
    "7th house, Venus/Mars placement. How they operate in relationships, their "
    "blind spots, what kind of partner amplifies vs. drains them.\n\n"

    "## Current Runtime — Dasha Analysis\n"
    "What Dasha period they're in RIGHT NOW. What phase of the game this is. "
    "What to press hard on, what to avoid, specific timing windows.\n\n"

    "## Activated Yogas — Your Edge\n"
    "ONLY yogas where has_yoga is true. What competitive advantages the chart gives. "
    "How to weaponize them.\n\n"

    "## Threat Assessment\n"
    "Mangal Dosha (use EXACT has_dosha value), Kaal Sarp Dosha (use EXACT has_dosha "
    "value), Sade Sati status (use EXACT is_in_sade_sati value), retrograde planets, "
    "enemy planet conjunctions. No fear-mongering — just strategic awareness with "
    "remedies where applicable.\n\n"

    "## Tactical Moves\n"
    "3-5 high-leverage, specific actions. No generic advice. Think timing, "
    "positioning, environment design, relationship calculus.\n\n"

    "### The Bottom Line\n"
    "2-sentence hard truth summary. Then one **bolded** highly specific coaching "
    "question that challenges them to look at their current life choices.\n\n"

    "Format with markdown. Keep the total response under 2000 words."
)

LANG_INSTRUCTIONS = {
    "en": "",
    "mr": (
        "\n\nLANGUAGE: Respond ENTIRELY in Marathi (मराठी). Use Devanagari script. "
        "Keep Vedic/astrological technical terms in their original Sanskrit form "
        "but explain them in Marathi. Keep the same structure and headings but "
        "translate headings to Marathi. Maintain the direct, authoritative tone."
    ),
    "sq": (
        "\n\nLANGUAGE: Respond ENTIRELY in Albanian (Shqip). "
        "Keep Vedic/astrological technical terms in their original Sanskrit form "
        "but explain them in Albanian. Keep the same structure and headings but "
        "translate headings to Albanian. Maintain the direct, authoritative tone."
    ),
}


def get_system_prompt(lang="en"):
    """Return system prompt with optional language instruction appended."""
    return SYSTEM_PROMPT + LANG_INSTRUCTIONS.get(lang, "")


def build_astrologer_prompt(username, structured_data):
    """Build the user prompt with cleanly structured astrology data."""
    section_labels = {
        "birth_profile": "Birth Profile (Nakshatra, Rasi, Zodiac)",
        "planet_positions": "Planet Positions (exact degrees, signs, retrograde status)",
        "kundli_nakshatra": "Kundli Nakshatra Details",
        "mangal_dosha": "Mangal Dosha Status",
        "mangal_dosha_detailed": "Mangal Dosha (Detailed — exceptions & remedies)",
        "active_yogas": "Active Yogas (ONLY yogas where has_yoga=true)",
        "dasha_periods": "Dasha Periods (Mahadasha → Antardasha timeline)",
        "current_dasha_balance": "Current Dasha Balance",
        "kaal_sarp_dosha": "Kaal Sarp Dosha Status",
        "sade_sati": "Sade Sati Transit Status",
        "planet_relationships": "Natural Planet Relationships",
    }

    sections = []
    for key, label in section_labels.items():
        data = structured_data.get(key)
        if data:
            sections.append(
                f"### {label}\n"
                f"```json\n{json.dumps(data, indent=2, default=str)}\n```"
            )

    user_prompt = (
        f"The person's name is {username}.\n\n"
        f"Below is their COMPLETE Vedic astrology data from the Prokerala API.\n"
        f"This includes: birth profile, exact planet positions with degrees, "
        f"D1/D9/Lagna/Moon/D10 chart data, Mangal Dosha, Kaal Sarp Dosha, "
        f"Sade Sati status, active Yogas, Dasha periods, and planet relationships.\n\n"
        f"Use ONLY this data. Every fact you state must trace back to a field below.\n\n"
        + "\n\n".join(sections)
    )

    return user_prompt


def get_llm_reading(username, structured_data, lang="en"):
    """Send structured astrology data to Claude and get a reading."""
    user_prompt = build_astrologer_prompt(username, structured_data)

    response = get_anthropic_client().messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        max_tokens=4000,
        temperature=0.5,
        system=get_system_prompt(lang),
        messages=[
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.content[0].text


def get_followup_answer(username, structured_data, chat_history, question, lang="en"):
    """Answer a follow-up question using the chart data as context."""
    user_prompt = build_astrologer_prompt(username, structured_data)

    messages = [{"role": "user", "content": user_prompt}]
    for msg in chat_history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": question})

    response = get_anthropic_client().messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        max_tokens=1000,
        temperature=0.6,
        system=get_system_prompt(lang) + (
            "\n\nThe user is now asking follow-up questions. Use the same "
            "Math → Reality → Strategy framework. Keep answers tight and direct "
            "(2-4 paragraphs max). Stay in character — authoritative, no fluff."
        ),
        messages=messages,
    )
    return response.content[0].text


def _strip_markdown(text):
    """Convert markdown to plain text for PDF."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    return text.strip()


def generate_pdf(username, reading_text, chat_messages=None):
    """Generate a PDF from the reading text and optional chat messages."""
    font_path = os.path.join(app.static_folder, "fonts", "ArialUnicode.ttf")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font("uni", "", font_path, uni=True)
    pdf.add_font("uni", "B", font_path, uni=True)

    pdf.add_page()

    pdf.set_font("uni", "B", 22)
    pdf.set_text_color(184, 90, 10)
    pdf.cell(0, 12, "Krama", ln=True, align="C")
    pdf.set_font("uni", "", 11)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 8, f"{username}'s Vedic Astrology Reading", ln=True, align="C")
    pdf.line(20, pdf.get_y() + 2, 190, pdf.get_y() + 2)
    pdf.ln(8)

    lines = reading_text.split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            pdf.ln(3)
            continue

        if stripped.startswith("## ") or stripped.startswith("### "):
            heading = re.sub(r'^#{2,3}\s+', '', stripped)
            pdf.ln(4)
            pdf.set_font("uni", "B", 13)
            pdf.set_text_color(184, 90, 10)
            pdf.multi_cell(0, 7, heading)
            pdf.ln(2)
            continue

        clean = _strip_markdown(stripped)
        if not clean:
            continue

        if clean.startswith("**") or stripped.startswith("- **"):
            clean = re.sub(r'\*\*(.+?)\*\*', r'\1', clean)
            pdf.set_font("uni", "B", 10)
        else:
            pdf.set_font("uni", "", 10)

        is_bullet = clean.startswith("- ") or clean.startswith("* ")
        if is_bullet:
            clean = "  •  " + clean[2:]

        pdf.set_text_color(30, 30, 40)
        pdf.multi_cell(0, 6, clean)
        pdf.ln(1)

    if chat_messages:
        pdf.add_page()
        pdf.set_font("uni", "B", 14)
        pdf.set_text_color(184, 90, 10)
        pdf.cell(0, 10, "Follow-up Q&A", ln=True)
        pdf.line(20, pdf.get_y(), 190, pdf.get_y())
        pdf.ln(6)

        for msg in chat_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            label = "You" if role == "user" else "Krama"

            pdf.set_font("uni", "B", 9)
            pdf.set_text_color(184, 90, 10)
            pdf.cell(0, 6, label, ln=True)

            pdf.set_font("uni", "", 10)
            pdf.set_text_color(30, 30, 40)
            pdf.multi_cell(0, 6, _strip_markdown(content))
            pdf.ln(4)

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    """Landing page - user enters their name."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if username:
            user = db.get_or_create_user(username)
            session["username"] = username

            if user.get("birth_date"):
                session["birth_date"] = user["birth_date"]
                session["birth_time"] = user["birth_time"]
                session["birth_lat"] = user["latitude"]
                session["birth_lng"] = user["longitude"]
                session["birth_place"] = user["birth_place"]

            return redirect(url_for("birth_form"))
    return render_template("index.html")


@app.route("/birth", methods=["GET", "POST"])
def birth_form():
    """Birth details form."""
    if "username" not in session:
        return redirect(url_for("index"))

    username = session["username"]

    if request.method == "POST":
        birth_date = request.form.get("birth_date")
        birth_time = request.form.get("birth_time")
        latitude = request.form.get("latitude")
        longitude = request.form.get("longitude")
        birth_place = request.form.get("birth_place")

        session["birth_date"] = birth_date
        session["birth_time"] = birth_time
        session["birth_lat"] = latitude
        session["birth_lng"] = longitude
        session["birth_place"] = birth_place

        db.update_user_birth(username, birth_date, birth_time, birth_place, latitude, longitude)
        return redirect(url_for("reading"))

    user = db.get_user(username)
    return render_template("birth_form.html", username=username, user=user)


@app.route("/api/set-lang", methods=["POST"])
def set_lang():
    """Set the language preference."""
    lang = request.get_json().get("lang", "en")
    if lang in LANG_INSTRUCTIONS:
        session["lang"] = lang
    return jsonify({"ok": True, "lang": session.get("lang", "en")})


@app.route("/reading")
def reading():
    """Generate and display the astrology reading."""
    if "username" not in session or "birth_date" not in session:
        return redirect(url_for("index"))

    past_readings = db.get_readings(session["username"], limit=5)

    return render_template(
        "reading.html",
        username=session["username"],
        birth_place=session.get("birth_place", ""),
        past_readings=past_readings,
        lang=session.get("lang", "en"),
    )


@app.route("/api/reading/cached")
def api_reading_cached():
    """Return the latest reading if birth details haven't changed."""
    if "username" not in session or "birth_date" not in session:
        return jsonify({"found": False})

    user = db.get_user(session["username"])
    if not user:
        return jsonify({"found": False})

    if (user.get("birth_date") != session.get("birth_date") or
            user.get("birth_time") != session.get("birth_time") or
            user.get("latitude") != session.get("birth_lat") or
            user.get("longitude") != session.get("birth_lng")):
        return jsonify({"found": False})

    readings = db.get_readings(session["username"], limit=1)
    if not readings:
        return jsonify({"found": False})

    raw = readings[0].get("raw_data") or {}
    if not isinstance(raw, dict):
        return jsonify({"found": False})

    charts = raw.pop("_charts", {})
    planets = raw.get("planet_positions", [])
    if not charts or not planets:
        return jsonify({"found": False})

    doshas = {
        "mangal": raw.get("mangal_dosha", {}),
        "kaal_sarp": raw.get("kaal_sarp_dosha", {}),
        "sade_sati": raw.get("sade_sati", {}),
    }

    return jsonify({
        "found": True,
        "reading": readings[0]["reading"],
        "charts": charts,
        "planets": planets,
        "doshas": doshas,
    })


@app.route("/api/reading/stream")
def api_reading_stream():
    """SSE endpoint that streams progress steps, then the final reading."""
    if "username" not in session or "birth_date" not in session:
        return jsonify({"error": "Session expired"}), 401

    date = session["birth_date"]
    time_str = session["birth_time"]
    lat = session["birth_lat"]
    lng = session["birth_lng"]
    username = session["username"]
    lang = session.get("lang", "en")
    datetime_str = f"{date}T{time_str}:00+05:30"

    def generate():
        astro_data = None
        for message, key, flag in fetch_astrology_data_with_progress(datetime_str, lat, lng):
            if flag == "final" and isinstance(message, dict):
                astro_data = message
            elif isinstance(message, str):
                yield f"data: {json.dumps({'step': message})}\n\n"

        charts = astro_data.pop("_charts", {})

        yield f"data: {json.dumps({'step': 'Structuring chart data...'})}\n\n"
        structured = structure_data_for_llm(astro_data)

        yield f"data: {json.dumps({'step': 'Krama is analyzing your chart...'})}\n\n"

        try:
            reading_text = get_llm_reading(username, structured, lang=lang)
            planets = structured.get("planet_positions", [])
            doshas = {
                "mangal": structured.get("mangal_dosha", {}),
                "kaal_sarp": structured.get("kaal_sarp_dosha", {}),
                "sade_sati": structured.get("sade_sati", {}),
            }
            save_payload = dict(structured, _charts=charts)
            db.save_reading(username, reading_text, save_payload)
            yield f"data: {json.dumps({'done': True, 'reading': reading_text, 'charts': charts, 'planets': planets, 'doshas': doshas})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Follow-up question endpoint. Limited to 10 questions per session."""
    if "username" not in session:
        return jsonify({"error": "Session expired"}), 401

    data = request.get_json()
    question = data.get("question", "").strip()
    chat_history = data.get("history", [])

    if not question:
        return jsonify({"error": "No question provided"}), 400

    question_count = len([m for m in chat_history if m.get("role") == "user"])
    if question_count >= 10:
        return jsonify({"error": "You've used all 10 follow-up questions for this reading."}), 429

    readings = db.get_readings(session["username"], limit=1)
    structured = readings[0]["raw_data"] if readings else {}
    if structured is None:
        structured = {}

    lang = session.get("lang", "en")
    try:
        answer = get_followup_answer(session["username"], structured, chat_history, question, lang=lang)
        db.save_chat(session["username"], question, answer, lang=lang)
        return jsonify({"answer": answer})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download-pdf")
def download_pdf():
    """Generate and return a PDF of the latest reading."""
    if "username" not in session:
        return jsonify({"error": "Session expired"}), 401

    username = session["username"]
    readings = db.get_readings(username, limit=1)
    if not readings:
        return jsonify({"error": "No reading found"}), 404

    reading_text = readings[0]["reading"]
    chat_json = request.args.get("chat", "[]")
    try:
        chat_messages = json.loads(chat_json)
    except (json.JSONDecodeError, TypeError):
        chat_messages = []

    buf = generate_pdf(username, reading_text, chat_messages)
    return send_file(
        buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"krama-{username}.pdf",
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001, use_reloader=False)
