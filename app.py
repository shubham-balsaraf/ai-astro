import io
import json
import os
import re
import smtplib
import time
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
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

    for attempt in range(3):
        try:
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
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1)


def prokerala_get(endpoint, params, accept="application/json"):
    """Make an authenticated GET request to the Prokerala API with retry."""
    for attempt in range(2):
        try:
            token = get_prokerala_token()
            resp = requests.get(
                f"{PROKERALA_BASE_URL}/{endpoint}",
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": accept},
                timeout=30,
            )
            if resp.status_code == 401:
                _token_cache["access_token"] = None
                _token_cache["expires_at"] = 0
                if attempt == 0:
                    continue
            resp.raise_for_status()
            if accept == "image/svg+xml":
                return resp.text
            return resp.json()
        except requests.RequestException:
            if attempt == 0:
                time.sleep(1)
                continue
            raise


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


# ---------------------------------------------------------------------------
# Compatibility Matching
# ---------------------------------------------------------------------------

def fetch_kundli_matching(user_dt, user_lat, user_lng, partner_dt, partner_lat, partner_lng):
    """Call Prokerala kundli-matching/advanced endpoint."""
    params = {
        "ayanamsa": 1,
        "girl_coordinates": f"{user_lat},{user_lng}",
        "girl_datetime": user_dt,
        "boy_coordinates": f"{partner_lat},{partner_lng}",
        "boy_datetime": partner_dt,
    }
    return prokerala_get("kundli-matching/advanced", params)


def structure_match_data(raw):
    """Extract meaningful data from kundli matching response."""
    data = _safe(raw, "data") or {}
    out = {}

    message = _safe(data, "message")
    if message:
        out["message"] = message

    guna_milan = _safe(data, "guna_milan") or {}
    out["total_points"] = _safe(guna_milan, "total_points")
    out["maximum_points"] = _safe(guna_milan, "maximum_points")

    gunas = _safe(guna_milan, "guna") or []
    guna_list = []
    for g in gunas:
        guna_list.append({
            "name": _safe(g, "name"),
            "girl_koot": _safe(g, "girl_koot"),
            "boy_koot": _safe(g, "boy_koot"),
            "points": _safe(g, "points"),
            "max_points": _safe(g, "maximum_points"),
            "description": _safe(g, "description"),
        })
    out["gunas"] = guna_list

    girl_info = _safe(data, "girl_info") or {}
    boy_info = _safe(data, "boy_info") or {}
    out["girl_info"] = {
        "nakshatra": _safe(girl_info, "nakshatra", "name"),
        "rasi": _safe(girl_info, "rasi", "name"),
    }
    out["boy_info"] = {
        "nakshatra": _safe(boy_info, "nakshatra", "name"),
        "rasi": _safe(boy_info, "rasi", "name"),
    }

    exceptions = _safe(data, "exceptions") or []
    if exceptions:
        out["exceptions"] = [str(e) for e in exceptions]

    return out


COMPAT_SYSTEM_PROMPT = (
    "You are Krama, an elite Vedic Astrology compatibility analyst. "
    "Your style: direct, strategic, no-BS, like a high-level relationship advisor. "
    "You are given Kundli Matching (Ashtakoot Guna Milan) data from the Prokerala API. "
    "NEVER recalculate scores — use ONLY the provided data.\n\n"
    "Structure your analysis as:\n"
    "## Compatibility Score\n"
    "State the score (X/36) and what it means.\n\n"
    "## The Breakdown\n"
    "For each of the 8 Gunas, use:\n"
    "- **Guna Name** (score/max): What this means for them specifically.\n\n"
    "## Power Dynamics\n"
    "Who leads emotionally? Who leads practically? Where friction will come.\n\n"
    "## The Red Flags\n"
    "Be blunt about areas of concern. What will cause fights.\n\n"
    "## The Green Lights\n"
    "Where this pairing genuinely works well.\n\n"
    "## The Strategy\n"
    "Actionable advice for making this work (or knowing when to walk).\n\n"
    "## The Bottom Line\n"
    "2-sentence verdict + one bold question for the user.\n\n"
    "Keep it real. No sugarcoating. Use 'Bro', 'Listen' — same tone as readings."
)


def get_compatibility_reading(username, partner_name, match_data, lang="en"):
    prompt = (
        f"User: {username}\nPartner: {partner_name}\n\n"
        f"Kundli Matching Data:\n{json.dumps(match_data, indent=2)}"
    )
    lang_suffix = LANG_INSTRUCTIONS.get(lang, "")
    response = get_anthropic_client().messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        max_tokens=3000,
        temperature=0.5,
        system=COMPAT_SYSTEM_PROMPT + lang_suffix,
        messages=[{"role": "user", "content": prompt}],
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


ADMIN_EMAIL = "balsaraf.shubham@gmail.com"
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")


def email_reading_to_admin(username, reading_text, birth_info=None):
    """Send reading PDF to admin in a background thread."""
    if not SMTP_USER or not SMTP_PASS:
        app.logger.warning("SMTP not configured — skipping email")
        return

    def _send():
        try:
            pdf_buf = generate_pdf(username, reading_text)
            pdf_bytes = pdf_buf.read()

            msg = MIMEMultipart()
            msg["From"] = SMTP_USER
            msg["To"] = ADMIN_EMAIL
            msg["Subject"] = f"Krama — New reading for {username}"

            body_lines = [f"New reading generated for **{username}**"]
            if birth_info:
                body_lines.append(
                    f"Birth: {birth_info.get('date','')} at {birth_info.get('time','')} "
                    f"in {birth_info.get('place','')}"
                )
            body_lines.append(f"\nSee attached PDF for the full report.")
            msg.attach(MIMEText("\n".join(body_lines), "plain"))

            attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
            attachment.add_header(
                "Content-Disposition", "attachment",
                filename=f"krama_{username}.pdf",
            )
            msg.attach(attachment)

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(SMTP_USER, SMTP_PASS)
                smtp.send_message(msg)
            app.logger.info(f"Reading emailed to {ADMIN_EMAIL} for {username}")
        except Exception as e:
            app.logger.error(f"Email failed for {username}: {e}")

    threading.Thread(target=_send, daemon=True).start()


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

        tz_offset = request.form.get("tz_offset", "+05:30")
        if not re.match(r'^[+-]\d{2}:\d{2}$', tz_offset):
            tz_offset = "+05:30"

        session["birth_date"] = birth_date
        session["birth_time"] = birth_time
        session["birth_lat"] = latitude
        session["birth_lng"] = longitude
        session["birth_place"] = birth_place
        session["tz_offset"] = tz_offset

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
    tz_offset = session.get("tz_offset", "+05:30")
    datetime_str = f"{date}T{time_str}:00{tz_offset}"

    birth_place = session.get("birth_place", "")

    def generate():
        try:
            astro_data = None
            for message, key, flag in fetch_astrology_data_with_progress(datetime_str, lat, lng):
                if flag == "final" and isinstance(message, dict):
                    astro_data = message
                elif isinstance(message, str):
                    yield f"data: {json.dumps({'step': message})}\n\n"

            if not astro_data:
                yield f"data: {json.dumps({'error': 'Failed to fetch astrology data. Please retry.'})}\n\n"
                return

            charts = astro_data.pop("_charts", {})

            yield f"data: {json.dumps({'step': 'Structuring chart data...'})}\n\n"
            structured = structure_data_for_llm(astro_data)

            if not structured.get("planet_positions"):
                yield f"data: {json.dumps({'error': 'Could not parse planet data. Please retry.'})}\n\n"
                return

            yield f"data: {json.dumps({'step': 'Krama is analyzing your chart...'})}\n\n"

            reading_text = get_llm_reading(username, structured, lang=lang)
            planets = structured.get("planet_positions", [])
            doshas = {
                "mangal": structured.get("mangal_dosha", {}),
                "kaal_sarp": structured.get("kaal_sarp_dosha", {}),
                "sade_sati": structured.get("sade_sati", {}),
            }
            save_payload = dict(structured, _charts=charts)
            db.save_reading(username, reading_text, save_payload)
            email_reading_to_admin(username, reading_text, {
                "date": date, "time": time_str, "place": birth_place,
            })
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


# ---------------------------------------------------------------------------
# Compatibility Routes
# ---------------------------------------------------------------------------

@app.route("/compatibility", methods=["GET", "POST"])
def compat_form():
    """Partner details form for compatibility matching."""
    if "username" not in session or "birth_date" not in session:
        return redirect(url_for("index"))

    username = session["username"]

    if request.method == "POST":
        partner_name = request.form.get("partner_name", "").strip()
        partner_date = request.form.get("partner_date")
        partner_time = request.form.get("partner_time")
        partner_place = request.form.get("partner_place", "")
        partner_lat = request.form.get("partner_lat")
        partner_lng = request.form.get("partner_lng")
        tz_offset = request.form.get("tz_offset", session.get("tz_offset", "+05:30"))

        if not all([partner_name, partner_date, partner_time, partner_lat, partner_lng]):
            return redirect(url_for("compat_form"))

        session["partner_name"] = partner_name
        session["partner_date"] = partner_date
        session["partner_time"] = partner_time
        session["partner_place"] = partner_place
        session["partner_lat"] = partner_lat
        session["partner_lng"] = partner_lng
        session["partner_tz"] = tz_offset

        return redirect(url_for("compat_result"))

    past_matches = db.get_compatibility(username, limit=10)

    return render_template("compatibility.html",
                           username=username,
                           birth_date=session.get("birth_date", ""),
                           birth_time=session.get("birth_time", ""),
                           birth_place=session.get("birth_place", ""),
                           past_matches=past_matches)


@app.route("/compatibility/result")
def compat_result():
    if "partner_name" not in session:
        return redirect(url_for("compat_form"))
    return render_template("compat_result.html",
                           username=session["username"],
                           partner_name=session["partner_name"])


@app.route("/compatibility/result/<int:match_id>")
def compat_result_saved(match_id):
    """Show a previously saved compatibility result."""
    if "username" not in session:
        return redirect(url_for("index"))
    matches = db.get_compatibility(session["username"], limit=50)
    match = next((m for m in matches if m["id"] == match_id), None)
    if not match:
        return redirect(url_for("compat_form"))
    return render_template("compat_result_saved.html",
                           username=session["username"],
                           match=match)


@app.route("/api/compatibility/stream")
def api_compat_stream():
    """SSE endpoint for compatibility analysis."""
    if "username" not in session or "partner_name" not in session:
        return jsonify({"error": "Session expired"}), 401

    username = session["username"]
    user_tz = session.get("tz_offset", "+05:30")
    partner_tz = session.get("partner_tz", user_tz)

    user_dt = f"{session['birth_date']}T{session['birth_time']}:00{user_tz}"
    partner_dt = f"{session['partner_date']}T{session['partner_time']}:00{partner_tz}"

    user_lat = session["birth_lat"]
    user_lng = session["birth_lng"]
    partner_lat = session["partner_lat"]
    partner_lng = session["partner_lng"]
    partner_name = session["partner_name"]
    lang = session.get("lang", "en")

    def generate():
        try:
            yield f"data: {json.dumps({'step': 'Fetching Kundli matching data...'})}\n\n"

            raw_match = fetch_kundli_matching(
                user_dt, user_lat, user_lng,
                partner_dt, partner_lat, partner_lng,
            )

            yield f"data: {json.dumps({'step': 'Analyzing Ashtakoot Guna Milan...'})}\n\n"
            match_data = structure_match_data(raw_match)

            yield f"data: {json.dumps({'step': 'Krama is evaluating your compatibility...'})}\n\n"
            reading = get_compatibility_reading(username, partner_name, match_data, lang=lang)

            db.save_compatibility(
                username, partner_name,
                session["partner_date"], session["partner_time"],
                session.get("partner_place", ""),
                partner_lat, partner_lng,
                match_data, reading,
            )

            yield f"data: {json.dumps({'done': True, 'reading': reading, 'total_points': match_data.get('total_points', 0), 'max_points': match_data.get('maximum_points', 36), 'gunas': match_data.get('gunas', [])})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001, use_reloader=False)
