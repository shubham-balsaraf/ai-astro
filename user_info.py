#!/usr/bin/env python3
"""
user_info.py — Look up everything about a user from krama.db

Usage:
    python3 user_info.py shubham          # exact username
    python3 user_info.py shub             # partial match
    python3 user_info.py                  # list all users
"""

import json
import sqlite3
import sys

DB_PATH = "krama.db"


def q(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_users(conn):
    users = q(conn, """
        SELECT u.username, u.birth_date, u.birth_place, u.created_at,
               COUNT(DISTINCT r.id) as readings, COUNT(DISTINCT c.id) as chats
        FROM users u
        LEFT JOIN readings r ON r.user_id = u.id
        LEFT JOIN chats c ON c.user_id = u.id
        GROUP BY u.id ORDER BY u.updated_at DESC
    """)
    if not users:
        print("No users in database.")
        return
    print(f"{'Username':<20} {'Birth Date':<12} {'Place':<25} {'Readings':<10} {'Chats':<8} {'Joined'}")
    print("-" * 100)
    for u in users:
        print(f"{u['username']:<20} {u['birth_date'] or '-':<12} {(u['birth_place'] or '-')[:24]:<25} {u['readings']:<10} {u['chats']:<8} {(u['created_at'] or '')[:10]}")


def show_user(conn, search):
    users = q(conn, "SELECT * FROM users WHERE username LIKE ?", (f"%{search}%",))
    if not users:
        print(f"No user matching '{search}'")
        return
    if len(users) > 1:
        print(f"Multiple matches for '{search}':")
        for u in users:
            print(f"  - {u['username']}")
        print(f"\nBe more specific, or showing first match: {users[0]['username']}\n")

    user = users[0]

    print(f"\n{'='*60}")
    print(f"  {user['username']}")
    print(f"{'='*60}\n")

    if user.get('birth_date'):
        print(f"  Birth Date:   {user['birth_date']}")
        print(f"  Birth Time:   {user['birth_time'] or 'N/A'}")
        print(f"  Birth Place:  {user['birth_place'] or 'N/A'}")
        print(f"  Coordinates:  {user.get('latitude', 'N/A')}, {user.get('longitude', 'N/A')}")
    print(f"  Joined:       {user['created_at'][:19].replace('T', ' ')}")
    print(f"  Last updated: {user['updated_at'][:19].replace('T', ' ')}")

    # Readings
    readings = q(conn, """
        SELECT reading, created_at FROM readings
        WHERE user_id = ? ORDER BY created_at DESC
    """, (user['id'],))

    print(f"\n{'─'*60}")
    print(f"  READINGS ({len(readings)})")
    print(f"{'─'*60}")

    for i, r in enumerate(readings, 1):
        ts = r['created_at'][:19].replace('T', ' ')
        print(f"\n  ── Reading {i} ({ts}) ──\n")
        print(r['reading'])

    # Chats
    chats = q(conn, """
        SELECT question, answer, lang, created_at FROM chats
        WHERE user_id = ? ORDER BY created_at ASC
    """, (user['id'],))

    print(f"\n{'─'*60}")
    print(f"  FOLLOW-UP Q&A ({len(chats)})")
    print(f"{'─'*60}")

    if not chats:
        print("\n  No follow-up questions.\n")
    else:
        for i, c in enumerate(chats, 1):
            ts = c['created_at'][:19].replace('T', ' ')
            lang = f" [{c['lang']}]" if c['lang'] != 'en' else ""
            print(f"\n  Q{i}{lang} ({ts}):")
            print(f"  {c['question']}\n")
            print(f"  Krama:")
            for line in c['answer'].split('\n'):
                print(f"  {line}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    if len(sys.argv) < 2:
        list_users(conn)
    else:
        show_user(conn, sys.argv[1])
    conn.close()
