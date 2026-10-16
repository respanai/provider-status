from datetime import datetime, timezone
import unittest

from official_history_betterstack import extract_betterstack_history, extract_friendli_history
import json


NOW = datetime(2026, 10, 16, 14, tzinfo=timezone.utc)
ROW = {"id": "example", "name": "Example", "url": "https://status.example.com/index.json",
       "section_pattern": "^Inference$", "scope": "Inference API"}


def day(state="operational", date="2026-10-15", downtime=0, maintenance=0):
    return {"day": date, "status": state, "downtime_duration": downtime,
            "maintenance_duration": maintenance}


def payload(*histories):
    return {"data": {"type": "status_page", "attributes": {"timezone": "UTC"}},
            "included": [{"id": "section", "type": "status_page_section",
                          "attributes": {"name": "Inference"}}] + [
                {"id": str(i), "type": "status_page_resource", "attributes": {
                    "public_name": f"API {i}", "status_page_section_id": "section",
                    "status_history": history}} for i, history in enumerate(histories)]}


def extract(data, row=ROW):
    return extract_betterstack_history(data, row, NOW)


class BetterStackHistoryTests(unittest.TestCase):
    def test_complete_published_singleton_history_and_provenance(self):
        from datetime import timedelta
        data = payload([day(date=str(NOW.date() - timedelta(days=i))) for i in range(90)])
        result = extract(data)
        self.assertTrue(result["coverage"]["complete_90_days"])
        self.assertEqual(result["history"][0]["date"], "2026-07-19")
        self.assertEqual(result["history"][-1]["provenance"], "official-history")
        self.assertEqual(result["source_timezone"], "UTC")
        self.assertIsNone(result["history"][0]["uptime"])

    def test_not_monitored_and_missing_peer_dates_block_green(self):
        for second in [[day("not_monitored")], [], [day("invented")]]:
            with self.subTest(second=second):
                result = extract(payload([day()], second))
                self.assertEqual(result["history"][0]["state"], "unknown")
                self.assertFalse(result["coverage"]["complete_90_days"])

    def test_no_records_cannot_invent_green_days(self):
        self.assertEqual(extract(payload([]))["history"], [])

    def test_partial_and_non_simultaneous_outages_are_degraded(self):
        for histories in [([day("downtime")], [day()]),
                          ([day("downtime")], [day("downtime")]),
                          ([day("downtime")], [day("not_monitored")])]:
            self.assertEqual(extract(payload(*histories))["history"][0]["state"], "degraded")

    def test_singleton_official_outage_can_map_directly(self):
        self.assertEqual(extract(payload([day("downtime")]))["history"][0]["state"], "downtime")

    def test_recovery_or_positive_downtime_cannot_erase_outage_day(self):
        for entry in [day("recovered", downtime=3176.9), day(downtime=10), day("recovered")]:
            self.assertEqual(extract(payload([entry]))["history"][0]["state"], "degraded")

    def test_maintenance_and_unknown_are_retained(self):
        self.assertEqual(extract(payload([day(maintenance=20)]))["history"][0]["state"], "maintenance")
        self.assertEqual(extract(payload([day("maintenance")], [day("not_monitored")]))["history"][0]["state"], "unknown")

    def test_unrelated_website_outage_is_excluded(self):
        data = payload([day()], [day("downtime")])
        data["included"][-1]["attributes"]["status_page_section_id"] = "website"
        self.assertEqual(extract(data)["history"][0]["state"], "operational")

    def test_all_explicit_scope_requirements_must_match(self):
        for row in [{**ROW, "names": ["missing"]},
                    {**ROW, "section_pattern": "^Missing$"},
                    {**ROW, "name_pattern": "^missing$"},
                    {"id": "example", "url": ROW["url"]}]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                extract(payload([day()]), row)

    def test_invalid_durations_remain_unknown(self):
        for value in [-1, float("nan"), float("inf"), True, "0", None]:
            with self.subTest(value=value):
                self.assertEqual(extract(payload([day(downtime=value)]))["history"][0]["state"], "unknown")

    def test_duplicate_or_invalid_dates_fail_closed(self):
        for history in [[day(), day()], [day(date="2026-13-20")]]:
            with self.assertRaises(ValueError):
                extract(payload(history))

    def test_missing_history_or_schema_fails_closed(self):
        data = payload([day()])
        del data["included"][-1]["attributes"]["status_history"]
        missing_id = payload([day()])
        del missing_id["included"][-1]["id"]
        for candidate in [data, missing_id, {}, {"data": {"type": "something_else"}, "included": []}]:
            with self.assertRaises(ValueError):
                extract(candidate)

    def test_only_actual_dates_in_last_90_days_are_exported(self):
        result = extract(payload([day(date="2026-07-18"), day(date="2026-10-17"), day()]))
        self.assertEqual([x["date"] for x in result["history"]], ["2026-10-15"])

    def test_naive_capture_and_credential_urls_are_rejected(self):
        with self.assertRaises(ValueError):
            extract_betterstack_history(payload([day()]), ROW, datetime(2026, 10, 16))
        with self.assertRaises(ValueError):
            extract(payload([day()]), {**ROW, "url": "https://secret@status.example.com/index.json"})


class FriendliPublishedBarsTests(unittest.TestCase):
    row = {"id": "friendliai", "name": "FriendliAI", "scope": "Model APIs",
           "status_url": "https://status.friendli.ai/", "group_pattern": "^Model APIs$"}

    def html(self, fills=None, missing=False, other=False, start_date=None):
        fills = fills or ["#00ff00"] * 91
        site = {"components": [{"id": "group", "name": {"default": "Model APIs"},
                 "children": [{"id": "api", "showUptime": True, "children": [], "startDate": start_date}]}],
                "okColor": "#00ff00", "disruptedColor": "#ffaa00", "degradedColor": "#ffff00",
                "downColor": "#ff0000", "noticeColor": "#000000", "unknownColor": "#dddddd"}
        data = {"site": site, "startDate": "Sat Jul 18 2026 00:00:00 GMT+0000 (UTC)",
                "endDate": "Fri Oct 16 2026 23:59:59 GMT+0000 (UTC)",
                "componentsUptime": {"api": {"uptime": "99.9", "outages": []}}}
        stream = "1:" + json.dumps(data) + "\n"
        script = "<script>self.__next_f.push(" + json.dumps([1, stream]) + ")</script>"
        bars = "".join(f'<rect x="{i * 11}" fill="{fill}"></rect>' for i, fill in enumerate(fills))
        group = '<div class="component-group group-uptime"><span>Model APIs</span><svg>' + bars + '</svg></div>'
        if other:
            group += '<div class="component-group group-uptime"><span>Dedicated Endpoints</span><rect x="0" fill="#ff0000"></rect></div>'
        return group + ("" if missing else script)

    def test_explicit_bars_map_to_dates_without_using_aggregate_percentage(self):
        result = extract_friendli_history(self.html(other=True), self.row, NOW)
        self.assertTrue(result["coverage"]["complete_90_days"])
        self.assertEqual(result["history"][0]["date"], "2026-07-19")
        self.assertTrue(all(x["state"] == "operational" and x["uptime"] is None for x in result["history"]))

    def test_published_unknown_and_outage_colors_are_retained(self):
        colors = ["#00ff00"] * 91
        colors[5], colors[8] = "#dddddd", "#ff0000"
        result = extract_friendli_history(self.html(colors), self.row, NOW)
        self.assertEqual(result["history"][4]["state"], "unknown")
        self.assertEqual(result["history"][7]["state"], "downtime")

    def test_missing_bounds_or_bar_count_cannot_create_green(self):
        for html in [self.html(missing=True), self.html(["#00ff00"] * 90)]:
            with self.assertRaises(ValueError):
                extract_friendli_history(html, self.row, NOW)

    def test_unrecognized_bar_color_fails_closed(self):
        with self.assertRaises(ValueError):
            extract_friendli_history(self.html(["#abcdef"] * 91), self.row, NOW)

    def test_unmatched_api_group_fails_closed(self):
        with self.assertRaises(ValueError):
            extract_friendli_history(self.html(), {**self.row, "group_pattern": "^Missing$"}, NOW)

    def test_days_before_declared_component_start_remain_unknown(self):
        result = extract_friendli_history(self.html(start_date="2026-09-01T00:00:00Z"), self.row, NOW)
        self.assertEqual(result["history"][0]["state"], "unknown")
        self.assertEqual(result["history"][-1]["state"], "operational")
        self.assertFalse(result["coverage"]["complete_90_days"])


if __name__ == "__main__":
    unittest.main()
