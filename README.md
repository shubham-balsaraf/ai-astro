# Krama — Vedic Astrology by AI

An interactive Vedic astrology web app that generates personalized birth chart readings using Prokerala APIs and Anthropic Claude.

## Features

- **Cosmic Map** — NASA Eyes-style interactive visualization with drag, zoom, and clickable planets
- **AI Readings** — Claude acts as an Executive Vedic Strategist, translating chart data into actionable insights
- **Follow-up Chat** — Ask up to 10 follow-up questions per reading
- **Multi-language** — English, Marathi (मराठी), Albanian (Shqip) with instant client-side switching
- **PDF Export** — Download readings with chat history
- **Smart Caching** — Returning users get instant readings without burning API tokens

## Quick Start (Local)

```bash
git clone https://github.com/YOUR_USERNAME/ai-astro.git
cd ai-astro
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your API keys
python app.py
```

Open `http://localhost:5001`

## Deploy to Raspberry Pi

```bash
# On the Pi:
git clone https://github.com/YOUR_USERNAME/ai-astro.git
cd ai-astro
cp .env.example .env   # edit with your API keys
bash deploy-pi.sh
```

The deploy script installs everything, sets up gunicorn + nginx, and creates a systemd service that auto-starts on boot.

After running, set up **port forwarding** on your router (port 80 → Pi's IP) for external access.

## API Keys Required

| Service | Get it at | Free tier |
|---------|-----------|-----------|
| Prokerala | [api.prokerala.com](https://api.prokerala.com) | Yes |
| Anthropic Claude | [console.anthropic.com](https://console.anthropic.com) | Pay-as-you-go |

## Architecture

```
app.py          — Flask app (routes, API integrations, LLM prompts)
db.py           — SQLite database layer (users, readings, chats)
templates/      — Jinja2 templates (base, index, birth form, reading)
static/         — CSS and JavaScript
deploy-pi.sh    — One-command Raspberry Pi deployment
```

## Tech Stack

Flask · SQLite · Anthropic Claude · Prokerala API · HTML Canvas · Server-Sent Events
