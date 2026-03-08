#!/usr/bin/env python3
"""
email_report.py — Email full user reports from krama.db

Usage:
    python3 email_report.py --email=you@gmail.com                 # all users from today
    python3 email_report.py --email=you@gmail.com --date=2026-03-08
    python3 email_report.py --email=you@gmail.com --date=all
    python3 email_report.py --email=you@gmail.com --user=shubham  # specific user, all time

Requires SMTP_USER and SMTP_PASS in .env (Gmail app password).
"""

import io
import os
import re
import smtplib
import sqlite3
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

DB_PATH = "krama.db"
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")


def q(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def md_to_html(text):
    """Lightweight markdown to HTML for email rendering."""
    lines = text.split("\n")
    out = []
    for line in lines:
        s = line.strip()
        if not s:
            out.append("<br>")
            continue
        if s.startswith("### "):
            out.append(f'<h3 style="color:#b85a0a;margin:18px 0 6px;">{s[4:]}</h3>')
        elif s.startswith("## "):
            out.append(f'<h2 style="color:#b85a0a;margin:22px 0 8px;">{s[3:]}</h2>')
        elif s.startswith("# "):
            out.append(f'<h1 style="color:#b85a0a;margin:24px 0 10px;">{s[2:]}</h1>')
        else:
            s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
            s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
            if s.startswith("- ") or s.startswith("* "):
                s = "• " + s[2:]
            out.append(f"<p style='margin:3px 0;line-height:1.6;'>{s}</p>")
    return "\n".join(out)


def build_user_html(conn, user, date_clause="1=1", date_params=()):
    """Build full HTML report for one user."""
    uid = user["id"]
    parts = []

    parts.append(f"""
    <div style="background:#1a1a2e;border-radius:12px;padding:24px;margin-bottom:24px;border:1px solid #2a2a4a;">
        <h2 style="color:#ffc857;margin:0 0 12px;">{user['username']}</h2>
        <table style="color:#ccc;font-size:14px;">
            <tr><td style="padding:2px 16px 2px 0;color:#888;">Birth Date</td><td>{user.get('birth_date') or 'N/A'}</td></tr>
            <tr><td style="padding:2px 16px 2px 0;color:#888;">Birth Time</td><td>{user.get('birth_time') or 'N/A'}</td></tr>
            <tr><td style="padding:2px 16px 2px 0;color:#888;">Birth Place</td><td>{user.get('birth_place') or 'N/A'}</td></tr>
            <tr><td style="padding:2px 16px 2px 0;color:#888;">Coordinates</td><td>{user.get('latitude','N/A')}, {user.get('longitude','N/A')}</td></tr>
            <tr><td style="padding:2px 16px 2px 0;color:#888;">Joined</td><td>{(user.get('created_at') or '')[:19].replace('T',' ')}</td></tr>
        </table>
    </div>
    """)

    readings = q(conn, f"""
        SELECT reading, created_at FROM readings
        WHERE user_id = ? AND {date_clause.replace('created_at', 'readings.created_at')}
        ORDER BY created_at DESC
    """, (uid,) + date_params)

    if readings:
        for i, r in enumerate(readings, 1):
            ts = r['created_at'][:19].replace('T', ' ')
            parts.append(f"""
            <div style="background:#12122a;border-radius:12px;padding:20px;margin-bottom:16px;border:1px solid #2a2a4a;">
                <h3 style="color:#ffc857;margin:0 0 4px;">Reading {i}</h3>
                <p style="color:#666;font-size:12px;margin:0 0 14px;">{ts}</p>
                <div style="color:#ddd;font-size:14px;">
                    {md_to_html(r['reading'])}
                </div>
            </div>
            """)

    chats = q(conn, f"""
        SELECT question, answer, lang, created_at FROM chats
        WHERE user_id = ? AND {date_clause.replace('created_at', 'chats.created_at')}
        ORDER BY created_at ASC
    """, (uid,) + date_params)

    if chats:
        chat_html = []
        for i, c in enumerate(chats, 1):
            ts = c['created_at'][:19].replace('T', ' ')
            lang_tag = f' <span style="color:#888;">[{c["lang"]}]</span>' if c['lang'] != 'en' else ""
            chat_html.append(f"""
            <div style="margin-bottom:16px;">
                <div style="background:#1e1e3a;border-radius:8px;padding:12px;margin-bottom:6px;">
                    <p style="color:#ffc857;font-size:12px;margin:0 0 4px;">Q{i}{lang_tag} — {ts}</p>
                    <p style="color:#fff;margin:0;font-size:14px;">{c['question']}</p>
                </div>
                <div style="background:#141428;border-radius:8px;padding:12px;border-left:3px solid #b85a0a;">
                    <p style="color:#b85a0a;font-size:12px;margin:0 0 4px;">Krama</p>
                    <div style="color:#ccc;font-size:14px;">
                        {md_to_html(c['answer'])}
                    </div>
                </div>
            </div>
            """)

        parts.append(f"""
        <div style="background:#12122a;border-radius:12px;padding:20px;margin-bottom:16px;border:1px solid #2a2a4a;">
            <h3 style="color:#ffc857;margin:0 0 14px;">Follow-up Q&A ({len(chats)})</h3>
            {"".join(chat_html)}
        </div>
        """)

    return "\n".join(parts)


def build_full_email(conn, users, title, date_clause="1=1", date_params=()):
    """Build the complete HTML email."""
    user_sections = []
    for user in users:
        user_sections.append(build_user_html(conn, user, date_clause, date_params))

    total_readings = sum(
        q(conn, f"SELECT COUNT(*) as c FROM readings WHERE user_id = ? AND {date_clause.replace('created_at','readings.created_at')}",
          (u['id'],) + date_params)[0]['c'] for u in users
    )
    total_chats = sum(
        q(conn, f"SELECT COUNT(*) as c FROM chats WHERE user_id = ? AND {date_clause.replace('created_at','chats.created_at')}",
          (u['id'],) + date_params)[0]['c'] for u in users
    )

    html = f"""
    <html>
    <body style="background:#0a0a1a;color:#ddd;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:20px;margin:0;">
        <div style="max-width:700px;margin:0 auto;">
            <div style="text-align:center;padding:30px 0 20px;">
                <h1 style="color:#ffc857;font-size:28px;margin:0;">ॐ Krama Report</h1>
                <p style="color:#888;margin:8px 0 0;">{title}</p>
            </div>

            <div style="background:#1a1a2e;border-radius:12px;padding:16px;margin-bottom:24px;text-align:center;border:1px solid #2a2a4a;">
                <span style="color:#ffc857;font-size:20px;font-weight:bold;">{len(users)}</span>
                <span style="color:#888;"> users</span>
                <span style="color:#333;margin:0 12px;">|</span>
                <span style="color:#ffc857;font-size:20px;font-weight:bold;">{total_readings}</span>
                <span style="color:#888;"> readings</span>
                <span style="color:#333;margin:0 12px;">|</span>
                <span style="color:#ffc857;font-size:20px;font-weight:bold;">{total_chats}</span>
                <span style="color:#888;"> questions</span>
            </div>

            {"".join(user_sections)}

            <p style="text-align:center;color:#555;font-size:12px;margin-top:30px;">
                Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · Krama Vedic Astrology
            </p>
        </div>
    </body>
    </html>
    """
    return html


def send_email(to_email, subject, html_body):
    if not SMTP_USER or not SMTP_PASS:
        print("ERROR: SMTP_USER and SMTP_PASS must be set in .env")
        print("See .env.example for details.")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    print(f"Email sent to {to_email}")


def main():
    args = sys.argv[1:]
    email = None
    date_filter = None
    user_filter = None

    for arg in args:
        if arg.startswith("--email="):
            email = arg.split("=", 1)[1]
        elif arg.startswith("--date="):
            date_filter = arg.split("=", 1)[1]
        elif arg.startswith("--user="):
            user_filter = arg.split("=", 1)[1]

    if not email:
        print("Usage: python3 email_report.py --email=you@gmail.com [--date=2026-03-08|all] [--user=name]")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    if date_filter == "all" or user_filter:
        date_clause = "1=1"
        date_params = ()
        title = "All Time Report"
    elif date_filter:
        date_clause = "date(created_at) = ?"
        date_params = (date_filter,)
        title = f"Report for {date_filter}"
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_clause = "date(created_at) = ?"
        date_params = (today,)
        title = f"Daily Report — {today}"

    if user_filter:
        users = q(conn, "SELECT * FROM users WHERE username LIKE ?", (f"%{user_filter}%",))
        if users:
            title = f"Report for {users[0]['username']}"
    else:
        users = q(conn, f"""
            SELECT u.* FROM users u
            WHERE u.id IN (
                SELECT user_id FROM readings WHERE {date_clause.replace('created_at', 'readings.created_at')}
                UNION
                SELECT user_id FROM chats WHERE {date_clause.replace('created_at', 'chats.created_at')}
                UNION
                SELECT id FROM users WHERE {date_clause.replace('created_at', 'users.created_at')}
            )
            ORDER BY u.updated_at DESC
        """, date_params * 3)

    if not users:
        print("No users found for this filter.")
        conn.close()
        sys.exit(0)

    print(f"Building report for {len(users)} user(s)...")
    html = build_full_email(conn, users, title, date_clause, date_params)
    conn.close()

    subject = f"ॐ Krama — {title}"
    send_email(email, subject, html)


if __name__ == "__main__":
    main()
