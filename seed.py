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
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")
YAML_PATH = os.path.join(HERE, "candidates.yaml")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")


def ensure_schema(db):
    with open(SCHEMA_PATH) as f:
        db.executescript(f.read())
    cols = {row[1] for row in db.execute("PRAGMA table_info(candidates)").fetchall()}
    if "stage" not in cols:
        db.execute("ALTER TABLE candidates ADD COLUMN stage TEXT DEFAULT 'uncalibrated'")
    db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB, help="SQLite db path")
    args = ap.parse_args()

    with open(YAML_PATH) as f:
        data = yaml.safe_load(f)

    db = sqlite3.connect(args.db)
    ensure_schema(db)
    added = 0
    updated = 0
    for c in data["candidates"]:
        exists = db.execute("SELECT 1 FROM candidates WHERE slug=?", (c["slug"],)).fetchone()
        db.execute(
            """INSERT INTO candidates (slug, display_name, category, stage, notes)
               VALUES (?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                 display_name=excluded.display_name,
                 category=excluded.category,
                 stage=excluded.stage,
                 notes=excluded.notes""",
            (
                c["slug"], c["display_name"], c["category"],
                c.get("stage", "uncalibrated"), c.get("notes", ""),
            ),
        )
        if exists:
            updated += 1
        else:
            added += 1
            print(f"  +  {c['slug']}")
    db.commit()
    print(f"\nseeded: {added} new, {updated} updated")


if __name__ == "__main__":
    main()
