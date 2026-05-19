#!/usr/bin/env python3
"""
seed.py - seed candidates table from candidates.yaml.

Idempotent. Re-run whenever you edit candidates.yaml to add trends.
Does NOT delete candidates removed from the yaml (manual in DB if you want to).
"""
import os
import sys
import yaml
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")
YAML_PATH = os.path.join(HERE, "candidates.yaml")


def main():
    with open(YAML_PATH) as f:
        data = yaml.safe_load(f)

    db = sqlite3.connect(DB)
    added = 0
    existing = 0
    for c in data["candidates"]:
        try:
            db.execute(
                """INSERT INTO candidates (slug, display_name, category, notes)
                   VALUES (?,?,?,?)""",
                (c["slug"], c["display_name"], c["category"], c.get("notes", "")),
            )
            added += 1
            print(f"  +  {c['slug']}")
        except sqlite3.IntegrityError:
            existing += 1
    db.commit()
    print(f"\nseeded: {added} new, {existing} already existed")


if __name__ == "__main__":
    main()
