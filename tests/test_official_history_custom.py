import json
import unittest

from official_history_custom import extract_deepinfra_history, extract_empiriolabs_history

CAPTURED = "2026-10-16T14:00:00Z"
ROW = {"id": "example", "name": "Example", "status_url": "https://status.example.com/",
       "scope": "API and models", "names": ["API Gateway", "Models"]}


def deepinfra(day, key="api"):
    card = ["$", "li", key, {"hostname": "api.deepinfra.com", "graph": {"windowDays": 90, "days": [day]}}]
    rsc = "16:" + json.dumps(card)
    return "<script>self.__next_f.push(" + json.dumps([1, rsc]) + ")</script>"


class OfficialCustomHistoryTests(unittest.TestCase):
    def test_recovered_operational_day_keeps_published_downtime(self):
        day = {"date": "2026-10-16", "status": "OPERATIONAL", "downtimeMs": 6000, "maintenanceMs": 0}
        row = extract_deepinfra_history(deepinfra(day), ROW, CAPTURED)
        self.assertEqual(row["history"][0]["state"], "degraded")
        self.assertIsNone(row["history"][0]["uptime"])
        self.assertEqual(row["history"][0]["provenance"], "official-history")

    def test_no_data_is_not_filled_healthy_and_wrong_scope_is_rejected(self):
        day = {"date": "2026-10-16", "status": "NO_DATA", "downtimeMs": 0, "maintenanceMs": 0}
        self.assertEqual(extract_deepinfra_history(deepinfra(day), ROW, CAPTURED)["history"][0]["state"], "unknown")
        with self.assertRaises(ValueError):
            extract_deepinfra_history(deepinfra(day, "website"), ROW, CAPTURED)

    def test_empirio_explicit_daily_checks_preserve_scope_gaps_and_failures(self):
        html = "".join('<div aria-label="90 day uptime history for ' + name + '">'
                       '<span data-d="2026-10-16" data-s="up" data-c="200" data-x="' + failures + '"></span></div>'
                       for name, failures in [("API Gateway", "0"), ("Models", "1")])
        result = extract_empiriolabs_history(html, ROW, CAPTURED)
        self.assertEqual(result["history"][0]["state"], "degraded")
        with self.assertRaises(ValueError):
            extract_empiriolabs_history(html.replace("Models", "Website"), ROW, CAPTURED)
        empty = html.replace('data-c="200"', 'data-c="0"').replace('data-x="1"', 'data-x="0"')
        self.assertEqual(extract_empiriolabs_history(empty, ROW, CAPTURED)["history"][0]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
