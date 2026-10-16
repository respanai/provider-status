from datetime import datetime, timedelta, timezone
import unittest

from official_history_statuspage import (
    extract_provider, native_history_url, parse_component_impacts, parse_showcase, stamp, window,
)


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
URL = "https://status.example.com/uptime_showcase?components=api"


def showcase(start="2020-01-01"):
    return {"timelines": {"api": {"component": {"code": "api", "name": "API", "startDate": start},
                                  "days": [{"date": str(day), "outages": {}} for day in window(NOW)]}}}


def native(since="2020-01-01T00:00:00Z"):
    return {"component_impacts": [], "component_uptimes": [
        {"component_id": "api", "data_available_since": since, "uptime": "100.00"}]}


class OfficialHistoryStatuspageTests(unittest.TestCase):
    def test_official_fractional_rfc3339_timestamps_parse_on_supported_python_versions(self):
        self.assertEqual(stamp("2026-09-11T09:08:15.86Z").microsecond, 860000)

    def test_showcase_requires_explicit_start_date_and_does_not_color_padding_green(self):
        history = parse_showcase(showcase("2026-10-01"), ["api"], NOW, URL)
        self.assertEqual(len(history), 16)
        self.assertEqual(history[0]["date"], "2026-10-01")
        self.assertTrue(all(day["uptime"] is None for day in history))
        self.assertTrue(all(day["provenance"] == "official-history" for day in history))
        invalid = showcase()
        del invalid["timelines"]["api"]["component"]["startDate"]
        with self.assertRaises(KeyError):
            parse_showcase(invalid, ["api"], NOW, URL)

    def test_complete_daily_showcase_preserves_partial_and_major_outages_without_uptime_estimates(self):
        payload = showcase()
        payload["timelines"]["api"]["days"][2]["outages"] = {"p": 563}
        payload["timelines"]["api"]["days"][3]["outages"] = {"m": 307}
        history = parse_showcase(payload, ["api"], NOW, URL)
        self.assertEqual(len(history), 90)
        self.assertEqual(history[2]["state"], "degraded")
        self.assertEqual(history[3]["state"], "downtime")
        self.assertEqual(history[3]["source_url"], URL)

    def test_missing_component_or_day_never_establishes_complete_scope_coverage(self):
        with self.assertRaisesRegex(ValueError, "no published uptime timeline"):
            parse_showcase(showcase(), ["api", "new-model"], NOW, URL)
        payload = showcase()
        del payload["timelines"]["api"]["days"][7]
        self.assertEqual(len(parse_showcase(payload, ["api"], NOW, URL)), 89)

    def test_empty_incidents_without_explicit_native_component_coverage_cannot_backfill_green(self):
        with self.assertRaisesRegex(ValueError, "coverage contract"):
            parse_component_impacts({"component_impacts": []}, ["api"], NOW, URL)
        with self.assertRaisesRegex(ValueError, "lacks explicit history coverage"):
            parse_component_impacts(native(), ["api", "website"], NOW, URL)
        self.assertEqual(len(parse_component_impacts(native(), ["api"], NOW, URL)), 90)

    def test_partial_native_first_day_and_future_time_are_not_treated_as_complete_days(self):
        history = parse_component_impacts(native("2026-10-01T03:28:26Z"), ["api"], NOW, URL)
        self.assertEqual(history[0]["date"], "2026-10-02")
        self.assertEqual(len(history), 15)
        self.assertEqual(parse_component_impacts(native("2026-10-17T00:00:00Z"), ["api"], NOW, URL), [])

    def test_native_impact_across_midnight_marks_both_days_and_excludes_unselected_website(self):
        payload = native()
        payload["component_impacts"] = [
            {"component_id": "api", "start_at": "2026-10-14T23:55:00Z", "end_at": "2026-10-15T00:10:00Z", "status": "major_outage"},
            {"component_id": "website", "start_at": "2026-10-13T00:00:00Z", "end_at": None, "status": "major_outage"},
        ]
        by_day = {day["date"]: day for day in parse_component_impacts(payload, ["api"], NOW, URL)}
        self.assertEqual(by_day["2026-10-13"]["state"], "operational")
        self.assertEqual(by_day["2026-10-14"]["state"], "downtime")
        self.assertEqual(by_day["2026-10-15"]["state"], "downtime")

    def test_future_scheduled_impact_does_not_change_past_history(self):
        payload = native()
        payload["component_impacts"] = [{"component_id": "api", "start_at": "2026-10-17T00:00:00Z",
                                         "end_at": None, "status": "maintenance"}]
        history = parse_component_impacts(payload, ["api"], NOW, URL)
        self.assertEqual(len(history), 90)
        self.assertTrue(all(day["state"] == "operational" for day in history))

    def test_multiple_component_daily_totals_do_not_invent_simultaneous_total_provider_outage(self):
        payload = showcase()
        payload["timelines"]["api"]["days"][-1]["outages"] = {"m": 60}
        payload["timelines"]["model"] = {"component": {"code": "model", "startDate": "2020-01-01"},
                                         "days": [{"date": str(day), "outages": {"m": 60}} for day in window(NOW)]}
        history = parse_showcase(payload, ["api", "model"], NOW, URL)
        self.assertEqual(history[-1]["state"], "degraded")

    def test_extract_requires_the_configured_api_scope_and_explicit_history_not_an_empty_rss(self):
        row = {"id": "test", "adapter": "statuspage", "url": "https://status.example.com/api/v2/summary.json",
               "status_url": "https://status.example.com/", "names": ["API"], "scope": "API"}
        responses = {row["url"]: {"components": [{"id": "api", "name": "API"}, {"id": "web", "name": "Website"}]},
                     row["status_url"]: "<html>Official uptime showcase</html>",
                     URL: showcase()}
        result = extract_provider(row, fetcher=responses.__getitem__, now=NOW)
        self.assertTrue(result["complete_90_days"])
        self.assertEqual(result["selected_components"], [{"id": "api", "name": "API"}])
        self.assertEqual(len(result["history"]), 90)
        responses[URL] = {"incidents": []}
        result = extract_provider(row, fetcher=responses.__getitem__, now=NOW)
        self.assertFalse(result["complete_90_days"])
        self.assertEqual(result["history"], [])

    def test_native_request_is_explicitly_bounded_to_the_ninety_day_utc_window(self):
        row = {"url": "https://status.openai.com/proxy/status.openai.com"}
        url = native_history_url(row, NOW)
        self.assertIn("/component_impacts?", url)
        self.assertIn("start_at=2026-07-19T00%3A00%3A00Z", url)
        self.assertIn("end_at=2026-10-17T00%3A00%3A00Z", url)


if __name__ == "__main__":
    unittest.main()
