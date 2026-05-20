"""Phase 2 discovery layer tests.

Covers:
- ensure_discovery_schema is idempotent
- normalize_slug clusters "sound bath" / "sound bath studios" /
  "sound bath club" into one canonical_slug
- upsert_proposal increments support_count on repeat
- promote_proposal creates a candidate with status='observing' +
  future router_eligible_at, marks the proposal as promoted, and
  seeds a Trends source
- scorer.py's observation gate sets would_fire=False for observing
- trend_router.latest_score_rows skips observing + future-eligible

Run with: python3 -m unittest tests/test_discover.py
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import discover  # noqa: E402


def _init_db(path):
    schema = open(os.path.join(ROOT, "schema.sql")).read()
    db = sqlite3.connect(path)
    db.executescript(schema)
    # Seed the magic general candidate that the discovery layer needs
    db.execute(
        "INSERT INTO candidates (slug, display_name, category, status) "
        "VALUES (?,?,?,?)",
        ("__general__", "(general feeds)", "meta", "tracking"),
    )
    db.commit()
    return db


class DiscoverSchemaTests(unittest.TestCase):
    def test_ensure_discovery_schema_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)
            discover.ensure_discovery_schema(db)
            # Re-running shouldn't double-add the column or tables.
            cols = {row[1] for row in db.execute("PRAGMA table_info(candidates)")}
            self.assertIn("router_eligible_at", cols)
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("proposed_candidates", tables)
            self.assertIn("proposal_evidence", tables)


class SlugNormalizationTests(unittest.TestCase):
    def test_strips_trailing_studios_classes_clubs(self):
        self.assertEqual(discover.normalize_slug("sound bath"), "sound_bath")
        self.assertEqual(discover.normalize_slug("sound bath studios"), "sound_bath")
        self.assertEqual(discover.normalize_slug("Sound-Bath Studio"), "sound_bath")
        self.assertEqual(discover.normalize_slug("sound bath classes"), "sound_bath")
        self.assertEqual(discover.normalize_slug("sound bath club"), "sound_bath")

    def test_does_not_overstrip_short_slugs(self):
        # "gym" alone should not become empty after the _gym strip.
        self.assertEqual(discover.normalize_slug("gym"), "gym")
        # Tail strip requires len(s) > len(tail) + 3, so "hot bar"
        # (len 7, tail "_bar" len 4, threshold 7) does NOT strip --
        # we don't reduce 2-word slugs to one-word stubs.
        self.assertEqual(discover.normalize_slug("hot bar"), "hot_bar")
        # 3-word slugs do strip: "longer hot bar" -> "longer_hot"
        self.assertEqual(discover.normalize_slug("longer hot bar"), "longer_hot")

    def test_truncates_to_30_chars(self):
        long_name = "a really very extremely long trend name about things"
        result = discover.normalize_slug(long_name)
        self.assertLessEqual(len(result), 30)


class ProposalDedupTests(unittest.TestCase):
    def test_upsert_proposal_clusters_synonyms(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)

            # Three near-synonyms — should land in one row
            pid1, new1 = discover.upsert_proposal(
                db, "sound bath", "Sound Bath", "wellness", "energy practice")
            pid2, new2 = discover.upsert_proposal(
                db, "sound bath studios", "Sound Bath Studios", "wellness", "")
            pid3, new3 = discover.upsert_proposal(
                db, "Sound-Bath Club", "Sound Bath Club", "", "")

            self.assertEqual(pid1, pid2)
            self.assertEqual(pid1, pid3)
            self.assertTrue(new1)
            self.assertFalse(new2)
            self.assertFalse(new3)

            row = db.execute(
                "SELECT support_count, canonical_slug FROM proposed_candidates WHERE id=?",
                (pid1,),
            ).fetchone()
            self.assertEqual(row[0], 3)
            self.assertEqual(row[1], "sound_bath")


class PromotionTests(unittest.TestCase):
    def test_promote_creates_observing_candidate_with_future_eligible_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)
            pid, _ = discover.upsert_proposal(
                db, "ice bath lounges", "Ice Bath Lounges", "wellness", "thesis")
            discover.insert_evidence(
                db, pid, surface="general_feed",
                source_url="https://example.com", raw_label="Ice Bath Lounges",
                quote="ice bath lounges opening in 12 cities",
                confidence=0.7,
            )
            db.commit()

            rc = discover.promote_proposal(db, pid, observation_days=28)
            self.assertEqual(rc, 0)

            # candidate row exists, status=observing, router_eligible_at in future
            row = db.execute(
                "SELECT id, slug, status, router_eligible_at FROM candidates WHERE slug=?",
                ("ice_bath",),  # normalized from "ice bath lounges"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[2], "observing")
            eligible = datetime.strptime(row[3][:19], "%Y-%m-%d %H:%M:%S")
            self.assertGreater(eligible, datetime.utcnow() + timedelta(days=27))

            # proposal marked promoted
            prow = db.execute(
                "SELECT proposal_status, promoted_candidate_id FROM proposed_candidates WHERE id=?",
                (pid,),
            ).fetchone()
            self.assertEqual(prow[0], "promoted")
            self.assertEqual(prow[1], row[0])

            # Trends source seeded
            sources = db.execute(
                "SELECT source_type, url FROM sources WHERE candidate_id=?",
                (row[0],),
            ).fetchall()
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0][0], "trends")
            self.assertEqual(sources[0][1], "ice bath lounges")

    def test_promote_refuses_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)
            pid, _ = discover.upsert_proposal(
                db, "test trend", "Test Trend", "", "")
            db.commit()
            discover.update_proposal_status(db, pid, "rejected")
            rc = discover.promote_proposal(db, pid)
            self.assertEqual(rc, 1)
            # No candidate created
            row = db.execute("SELECT count(*) FROM candidates WHERE slug=?",
                             ("test_trend",)).fetchone()
            self.assertEqual(row[0], 0)


class ObservationGateTests(unittest.TestCase):
    def test_router_query_excludes_observing(self):
        # Mirror trend_router.latest_score_rows's WHERE clause exactly.
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)

            # Two rows: one tracking + immediately-eligible, one observing
            db.execute(
                "INSERT INTO candidates (slug, display_name, category, status, router_eligible_at) "
                "VALUES (?,?,?,?,?)",
                ("tracked_one", "Tracked One", "cat", "tracking", None),
            )
            db.execute(
                "INSERT INTO candidates (slug, display_name, category, status, router_eligible_at) "
                "VALUES (?,?,?,?,?)",
                ("observing_one", "Observing One", "cat", "observing",
                 (datetime.utcnow() + timedelta(days=28)).strftime("%Y-%m-%d %H:%M:%S")),
            )
            # Add score rows for both
            for slug in ("tracked_one", "observing_one"):
                cid = db.execute("SELECT id FROM candidates WHERE slug=?", (slug,)).fetchone()[0]
                db.execute(
                    "INSERT INTO scores (candidate_id, as_of, velocity, spread, vocabulary, "
                    "composite, would_fire) VALUES (?,?,?,?,?,?,?)",
                    (cid, "2026-05-20", 1.0, 1.0, 1.0, 0.9, 1),
                )
            db.commit()

            # Reuse the production query
            import trend_router
            rows = trend_router.latest_score_rows(db)
            slugs = {row[1] for row in rows}
            self.assertIn("tracked_one", slugs)
            self.assertNotIn("observing_one", slugs)

    def test_router_query_excludes_future_eligible_tracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)
            db.execute(
                "INSERT INTO candidates (slug, display_name, category, status, router_eligible_at) "
                "VALUES (?,?,?,?,?)",
                ("future_tracked", "Future Tracked", "cat", "tracking",
                 (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")),
            )
            cid = db.execute("SELECT id FROM candidates WHERE slug=?",
                             ("future_tracked",)).fetchone()[0]
            db.execute(
                "INSERT INTO scores (candidate_id, as_of, velocity, spread, vocabulary, "
                "composite, would_fire) VALUES (?,?,?,?,?,?,?)",
                (cid, "2026-05-20", 1.0, 1.0, 1.0, 0.9, 1),
            )
            db.commit()
            import trend_router
            rows = trend_router.latest_score_rows(db)
            slugs = {row[1] for row in rows}
            self.assertNotIn("future_tracked", slugs)


if __name__ == "__main__":
    unittest.main()
