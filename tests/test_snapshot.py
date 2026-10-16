from datetime import datetime, timedelta, timezone
import unittest

from snapshot import build_snapshot, effective_state, iso


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
CONFIG = [{"id": "api", "name": "Example API", "adapter": "statuspage",
           "status_url": "https://status.example.com/"},
          {"id": "unsupported", "name": "Unsupported", "adapter": "unavailable",
           "official_url": "https://example.org/", "unavailable_reason": "No current source"}]


def observation(state="operational", observed=NOW):
    return {"id": "api", "name": "Untrusted display name", "state": state,
            "observed_at": iso(observed), "source_url": "https://status.example.com/api.json",
            "detail": "Official API components"}


def build(observations=None, previous=None, now=NOW):
    return build_snapshot(CONFIG, [observation()] if observations is None else observations,
                          previous=previous, now=now)


class PublicSnapshotTests(unittest.TestCase):
    def test_fresh_snapshot_retains_official_source_and_canonical_name(self):
        snapshot = build()
        row = snapshot["providers"][0]
        self.assertEqual(row["name"], "Example API")
        self.assertEqual(row["source_url"], "https://status.example.com/")
        self.assertEqual(row["state"], "operational")
        self.assertEqual(snapshot["stale_after_seconds"], 900)
        self.assertEqual(snapshot["expires_at"], iso(NOW + timedelta(minutes=15)))
        self.assertEqual(row["last_confirmed_state"], "operational")

    def test_scheduler_stoppage_expires_known_badges_at_consumption_time(self):
        snapshot = build()
        row = snapshot["providers"][0]
        self.assertEqual(effective_state(row, snapshot, NOW + timedelta(minutes=14)), "operational")
        self.assertEqual(effective_state(row, snapshot, NOW + timedelta(minutes=15)), "stale")

    def test_already_expired_observation_is_stale_with_prior_fact_retained(self):
        snapshot = build([observation(observed=NOW - timedelta(hours=1))])
        row = snapshot["providers"][0]
        self.assertEqual(row["state"], "stale")
        self.assertEqual(row["last_confirmed_state"], "operational")

    def test_current_unknown_never_falls_back_to_last_confirmed_green(self):
        prior = build()
        later = NOW + timedelta(minutes=5)
        snapshot = build([observation("unknown", later)], prior, later)
        row = snapshot["providers"][0]
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["last_confirmed_state"], "operational")
        self.assertEqual(effective_state(row, snapshot, later + timedelta(hours=1)), "unknown")

    def test_future_or_naive_timestamps_cannot_create_green_badges_or_history(self):
        for value in [iso(NOW + timedelta(minutes=10)), "2026-10-16T12:00:00", "invalid"]:
            with self.subTest(timestamp=value):
                obs = observation()
                obs["observed_at"] = value
                row = build([obs])["providers"][0]
                self.assertEqual(row["state"], "unknown")
                self.assertEqual(row["history"], [])
                self.assertIsNone(row["last_confirmed_at"])

    def test_missing_observation_and_disabled_source_are_explicit_unknown(self):
        snapshot = build([])
        self.assertEqual(len(snapshot["providers"]), 2)
        for row in snapshot["providers"]:
            self.assertEqual(row["state"], "unknown")
            self.assertEqual(row["history"], [])
        self.assertFalse(snapshot["providers"][1]["enabled"])

    def test_history_has_only_observed_utc_days_and_no_uptime_percentages(self):
        first = build([observation(observed=NOW - timedelta(days=2))])
        snapshot = build([observation()], first)
        history = snapshot["providers"][0]["history"]
        self.assertEqual([x["date"] for x in history], ["2026-10-14", "2026-10-16"])
        self.assertTrue(all(x["uptime"] is None for x in history))

    def test_unknown_and_recovery_cannot_clear_daily_observed_degradation(self):
        prior = build([observation("degraded")])
        later = NOW + timedelta(minutes=5)
        unknown = build([observation("unknown", later)], prior, later)
        self.assertEqual(unknown["providers"][0]["history"][0]["state"], "degraded")
        recovered = build([observation("operational", later)], unknown, later)
        self.assertEqual(recovered["providers"][0]["state"], "operational")
        self.assertEqual(recovered["providers"][0]["history"][0]["state"], "degraded")

    def test_unknown_observation_prevents_a_fully_healthy_daily_classification(self):
        prior = build()
        snapshot = build([observation("unknown")], prior)
        self.assertEqual(snapshot["providers"][0]["history"][0]["state"], "unknown")

    def test_history_is_bounded_to_90_calendar_days_without_backfill(self):
        prior = build()
        prior["providers"][0]["history"] = [
            {"date": str((NOW - timedelta(days=i)).date()), "state": "operational", "uptime": 100}
            for i in range(120)]
        row = build([], prior)["providers"][0]
        self.assertEqual(len(row["history"]), 90)
        self.assertEqual(row["history"][0]["date"], str((NOW - timedelta(days=89)).date()))
        self.assertTrue(all(x["uptime"] is None for x in row["history"]))

    def test_worst_state_aggregation_uses_utc_observation_day(self):
        obs = observation("downtime")
        obs["observed_at"] = "2026-10-16T01:00:00+08:00"
        row = build([obs])["providers"][0]
        self.assertEqual(row["history"], [{"date": "2026-10-15", "state": "downtime", "uptime": None}])

    def test_duplicate_or_unconfigured_observations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "configured and unique"):
            build([observation(), observation()])
        with self.assertRaisesRegex(ValueError, "configured and unique"):
            build([{**observation(), "id": "unconfigured"}])


if __name__ == "__main__":
    unittest.main()
