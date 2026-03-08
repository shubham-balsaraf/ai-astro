#!/usr/bin/env python3
"""
daily_report.py — Generate a daily activity report from krama.db

Usage:
    python3 daily_report.py                # today's activity (truncated)
    python3 daily_report.py --full         # today's activity (full text)
    python3 daily_report.py 2026-03-07     # specific date
    python3 daily_report.py all            # all time
    python3 daily_report.py all --full     # all time, full text

Output: prints to terminal and saves to daily_report_<date>.md
"""

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = "krama.db"


def run_query(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def strip_markdown(text):
    text = re.sub(r'#{1,4}\s+', '', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    return text.strip()


def truncate(text, length=300):
    text = strip_markdown(text)
    if len(text) <= length:
        return text
    return text[:length].rsplit(' ', 1)[0] + '...'


def generate_report(date_filter=None, full=False):
    conn = sqlite3.connect(DB_PATH)

    if date_filter == "all":
        date_clause = "1=1"
        date_params = ()
        title_date = "All Time"
    elif date_filter:
        date_clause = "date(created_at) = ?"
        date_params = (date_filter,)
        title_date = date_filter
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        date_clause = "date(created_at) = ?"
        date_params = (today,)
        title_date = today

    users = run_query(conn, f"""
        SELECT u.*, COUNT(DISTINCT r.id) as reading_count, COUNT(DISTINCT c.id) as chat_count
        FROM users u
        LEFT JOIN readings r ON r.user_id = u.id AND {date_clause.replace('created_at', 'r.created_at')}
        LEFT JOIN chats c ON c.user_id = u.id AND {date_clause.replace('created_at', 'c.created_at')}
        WHERE u.id IN (
            SELECT user_id FROM readings WHERE {date_clause.replace('created_at', 'readings.created_at')}
            UNION
            SELECT user_id FROM chats WHERE {date_clause.replace('created_at', 'chats.created_at')}
            UNION
            SELECT id FROM users WHERE {date_clause.replace('created_at', 'users.created_at')}
        )
        GROUP BY u.id
        ORDER BY u.updated_at DESC
    """, date_params * 5)

    lines = []
    lines.append(f"# Krama Daily Report — {title_date}")
    lines.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"\n---\n")

    if not users:
        lines.append("*No activity for this period.*\n")
    else:
        lines.append(f"## Summary\n")
        lines.append(f"- **Users active**: {len(users)}")
        total_readings = sum(u['reading_count'] for u in users)
        total_chats = sum(u['chat_count'] for u in users)
        lines.append(f"- **Readings generated**: {total_readings}")
        lines.append(f"- **Follow-up questions**: {total_chats}")
        lines.append("")

        for i, user in enumerate(users, 1):
            uname = user['username']
            lines.append(f"\n---\n")
            lines.append(f"## {i}. {uname}\n")

            if user.get('birth_date'):
                lines.append(f"**Birth details:**")
                lines.append(f"- Date: {user['birth_date']}")
                lines.append(f"- Time: {user['birth_time'] or 'N/A'}")
                lines.append(f"- Place: {user['birth_place'] or 'N/A'}")
                lines.append(f"- Coordinates: {user.get('latitude', 'N/A')}, {user.get('longitude', 'N/A')}")
                lines.append("")

            readings = run_query(conn, f"""
                SELECT reading, created_at FROM readings
                WHERE user_id = ? AND {date_clause.replace('created_at', 'readings.created_at')}
                ORDER BY created_at DESC
            """, (user['id'],) + date_params)

            if readings:
                lines.append(f"### Readings ({len(readings)})\n")
                for j, r in enumerate(readings, 1):
                    ts = r['created_at'][:19].replace('T', ' ')
                    lines.append(f"**Reading {j}** — {ts}\n")
                    if full:
                        lines.append(r['reading'])
                    else:
                        lines.append(f"```")
                        lines.append(truncate(r['reading'], 500))
                        lines.append(f"```")
                    lines.append("")

            chats = run_query(conn, f"""
                SELECT question, answer, lang, created_at FROM chats
                WHERE user_id = ? AND {date_clause.replace('created_at', 'chats.created_at')}
                ORDER BY created_at ASC
            """, (user['id'],) + date_params)

            if chats:
                lines.append(f"### Follow-up Questions ({len(chats)})\n")
                for c in chats:
                    ts = c['created_at'][:19].replace('T', ' ')
                    lang_tag = f" [{c['lang']}]" if c['lang'] != 'en' else ""
                    lines.append(f"**Q{lang_tag}** ({ts}): {c['question']}\n")
                    if full:
                        lines.append(f"> **Krama:** {c['answer']}\n")
                    else:
                        lines.append(f"> **Krama:** {truncate(c['answer'], 400)}\n")

    conn.close()

    report = "\n".join(lines)

    print(report)

    filename = f"daily_report_{title_date.replace(' ', '_')}.md"
    with open(filename, "w") as f:
        f.write(report)
    print(f"\n{'='*50}")
    print(f"Saved to: {filename}")

    return report


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--full"]
    full = "--full" in sys.argv
    date_arg = args[0] if args else None
    generate_report(date_arg, full=full)
