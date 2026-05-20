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
from datetime import date, datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import discover  # noqa: E402


def _init_db(path):
    with open(os.path.join(ROOT, "schema.sql")) as f:
        schema = f.read()
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


def _add_candidate_with_trends(db, slug="padel", display_name="Padel", category="sport",
                               query="padel"):
    db.execute(
        "INSERT INTO candidates (slug, display_name, category, status) VALUES (?,?,?,?)",
        (slug, display_name, category, "tracking"),
    )
    cid = db.execute("SELECT id FROM candidates WHERE slug=?", (slug,)).fetchone()[0]
    db.execute(
        "INSERT INTO sources (candidate_id, source_type, url, label) VALUES (?,?,?,?)",
        (cid, "trends", query, f"Google Trends: {query}"),
    )
    db.commit()
    return cid


class FakeGoogleProvider:
    def __init__(self, rising):
        self.rising = rising
        self.rising_calls = []

    def rising_for(self, query):
        self.rising_calls.append(query)
        return self.rising

    def trending_searches(self):
        return []


class FakeTikTokProvider:
    def __init__(self, records):
        self.records = records
        self.calls = 0

    def trending_hashtags(self, category, limit=30):
        self.calls += 1
        return self.records if self.calls == 1 else []


class FakeRedditProvider:
    def __init__(self, records):
        self.records = records

    def growing_subreddits(self, limit=30):
        return self.records


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRedditAboutSession:
    def __init__(self, payloads):
        self.payloads = payloads
        self.headers = {}

    def get(self, url, timeout=20):
        subreddit = url.rstrip("/").split("/")[-2].lower()
        payload = self.payloads[subreddit]
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


def _about_payload(name, subscribers, description="social running club meetups"):
    return {
        "data": {
            "display_name": name,
            "display_name_prefixed": f"r/{name}",
            "public_description": description,
            "title": description,
            "subscribers": subscribers,
        }
    }


def _add_subreddit_history(db, subreddit, subscribers, days_ago):
    snapshot = (date.today() - timedelta(days=days_ago)).isoformat()
    discover.upsert_subreddit_snapshot(db, subreddit, subscribers, snapshot)
    db.commit()


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
            self.assertIn("provider_state", tables)
            self.assertIn("subreddit_subscriber_history", tables)


class ProviderStateTests(unittest.TestCase):
    def test_is_available_blocks_during_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _init_db(os.path.join(tmp, "db.sqlite"))
            discover.ensure_discovery_schema(db)
            state = discover.ProviderState(
                db, "test_provider", policy={"cooldown_seconds": 90}
            )

            self.assertTrue(state.is_available())
            state.record_success(notes="ok")

            self.assertFalse(state.is_available())
            self.assertGreater(state.cooldown_remaining_seconds(), 0)

    def test_consecutive_failures_disables_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _init_db(os.path.join(tmp, "db.sqlite"))
            discover.ensure_discovery_schema(db)
            state = discover.ProviderState(
                db,
                "test_provider",
                policy={"disable_threshold": 2, "disable_minutes": 60},
            )

            state.record_failure("first")
            self.assertTrue(state.is_available())
            state.record_failure("second")

            self.assertFalse(state.is_available())
            row = state.row()
            self.assertEqual(row["consecutive_failures"], 2)
            self.assertIsNotNone(row["disabled_until"])

    def test_record_success_resets_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _init_db(os.path.join(tmp, "db.sqlite"))
            discover.ensure_discovery_schema(db)
            state = discover.ProviderState(
                db,
                "test_provider",
                policy={"disable_threshold": 3, "disable_minutes": 60},
            )
            state.record_failure("first")
            state.record_failure("second")

            state.record_success(notes="recovered")

            row = state.row()
            self.assertEqual(row["consecutive_failures"], 0)
            self.assertIsNone(row["disabled_until"])
            self.assertEqual(row["success_count"], 1)

    def test_disabled_provider_is_skipped_by_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _init_db(os.path.join(tmp, "db.sqlite"))
            discover.ensure_discovery_schema(db)
            state = discover.ProviderState(db, "tiktok_creative_center")
            for _ in range(discover.PROVIDER_POLICY["tiktok_creative_center"]["disable_threshold"]):
                state.record_failure("blocked")

            with mock.patch("discover.TikTokCreativeProvider") as provider_cls:
                summaries = discover.run_discovery_adapters(
                    db, ["tiktok"], limit_per_adapter=5, dry_run=True
                )

            provider_cls.assert_not_called()
            self.assertEqual(summaries["tiktok"]["provider_skipped"], 1)


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


class AdapterFilterTests(unittest.TestCase):
    def test_google_related_provider_filters_brand_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)
            _add_candidate_with_trends(db, slug="padel", query="padel")
            provider = FakeGoogleProvider([
                ("padel court NYC", 250),
                ("nike padel shoes", 1000),
                ("padel near me", 500),
            ])

            summary = discover.run_google_related_discovery(
                db, limit=10, dry_run=False, provider=provider)

            self.assertEqual(summary["proposals_new"], 1)
            rows = db.execute(
                "SELECT canonical_slug FROM proposed_candidates ORDER BY canonical_slug"
            ).fetchall()
            self.assertEqual([r[0] for r in rows], ["padel_court_nyc"])

    def test_google_related_skips_seed_synonyms(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)
            _add_candidate_with_trends(db, slug="padel", query="padel")
            provider = FakeGoogleProvider([("padels", 300), ("padel", "Breakout")])

            summary = discover.run_google_related_discovery(
                db, limit=10, dry_run=False, provider=provider)

            self.assertEqual(summary["proposals_new"], 0)
            count = db.execute("SELECT COUNT(*) FROM proposed_candidates").fetchone()[0]
            self.assertEqual(count, 0)

    def test_tiktok_filters_platform_jargon(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)
            provider = FakeTikTokProvider([
                {"hashtag": "fyp", "growth_pct": 1000},
                {"hashtag": "viral", "growth_pct": 1000},
                {"hashtag": "trending", "growth_pct": 1000},
                {"hashtag": "hotgirlwalk", "growth_pct": 300, "view_count": 12_400_000},
            ])

            summary = discover.run_tiktok_creative_discovery(
                db, limit=10, dry_run=False, provider=provider)

            self.assertEqual(summary["proposals_new"], 1)
            self.assertEqual(summary["filtered_platform_jargon"], 3)
            row = db.execute(
                "SELECT canonical_slug FROM proposed_candidates"
            ).fetchone()
            self.assertEqual(row[0], "hotgirlwalk")

    def test_reddit_growing_filters_generic_subs(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)
            provider = FakeRedditProvider([
                {"name": "all", "description": "all of reddit"},
                {"name": "AskReddit", "description": "questions"},
                {"name": "funny", "description": "humor"},
                {"name": "runclub", "description": "social running club meetups",
                 "subscribers": 42000, "growth_rate": 150},
            ])

            summary = discover.run_reddit_growing_discovery(
                db, limit=10, dry_run=False, provider=provider)

            self.assertEqual(summary["proposals_new"], 1)
            self.assertEqual(summary["filtered_generic"], 3)
            row = db.execute(
                "SELECT canonical_slug FROM proposed_candidates"
            ).fetchone()
            self.assertEqual(row[0], "runclub")

    def test_provider_abstraction_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = os.path.join(tmp, "db.sqlite")
            db = _init_db(dbp)
            discover.ensure_discovery_schema(db)
            _add_candidate_with_trends(db, slug="padel", query="padel")

            with mock.patch("discover.GoogleRelatedProvider") as provider_cls:
                provider = provider_cls.return_value
                provider.rising_for.return_value = []
                provider.trending_searches.return_value = []
                discover.run_google_related_discovery(db, limit=10, dry_run=True)

            provider_cls.assert_called_once()
            provider.rising_for.assert_called_once_with("padel")


class RedditSubscriberDeltaTests(unittest.TestCase):
    def test_subscriber_delta_requires_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _init_db(os.path.join(tmp, "db.sqlite"))
            discover.ensure_discovery_schema(db)
            session = FakeRedditAboutSession({
                "runclub": _about_payload("RunClub", 10_000),
            })
            provider = discover.RedditSubscriberDeltaProvider(
                seeds=[{"subreddit": "RunClub", "category": "fitness_social"}],
                session=session,
            )

            summary = discover.run_reddit_growing_discovery(
                db, limit=5, dry_run=False, provider=provider
            )

            self.assertEqual(summary["proposals_new"], 0)
            self.assertEqual(summary["not_enough_history"], 1)
            count = db.execute(
                "SELECT COUNT(*) FROM subreddit_subscriber_history WHERE subreddit=?",
                ("runclub",),
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_subscriber_delta_emits_proposal_above_5pct_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _init_db(os.path.join(tmp, "db.sqlite"))
            discover.ensure_discovery_schema(db)
            _add_subreddit_history(db, "RunClub", 900, days_ago=14)
            _add_subreddit_history(db, "RunClub", 1_000, days_ago=7)
            session = FakeRedditAboutSession({
                "runclub": _about_payload("RunClub", 1_061),
            })
            provider = discover.RedditSubscriberDeltaProvider(
                seeds=[{"subreddit": "RunClub", "category": "fitness_social"}],
                session=session,
            )

            summary = discover.run_reddit_growing_discovery(
                db, limit=5, dry_run=False, provider=provider
            )

            self.assertEqual(summary["proposals_new"], 1)
            row = db.execute(
                "SELECT canonical_slug FROM proposed_candidates"
            ).fetchone()
            self.assertEqual(row[0], "runclub")

    def test_subscriber_delta_no_emit_below_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _init_db(os.path.join(tmp, "db.sqlite"))
            discover.ensure_discovery_schema(db)
            _add_subreddit_history(db, "RunClub", 900, days_ago=14)
            _add_subreddit_history(db, "RunClub", 1_000, days_ago=7)
            session = FakeRedditAboutSession({
                "runclub": _about_payload("RunClub", 1_040),
            })
            provider = discover.RedditSubscriberDeltaProvider(
                seeds=[{"subreddit": "RunClub", "category": "fitness_social"}],
                session=session,
            )

            summary = discover.run_reddit_growing_discovery(
                db, limit=5, dry_run=False, provider=provider
            )

            self.assertEqual(summary["proposals_new"], 0)
            self.assertEqual(summary["below_growth_threshold"], 1)
            count = db.execute("SELECT COUNT(*) FROM proposed_candidates").fetchone()[0]
            self.assertEqual(count, 0)


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
