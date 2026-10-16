from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers

FIXTURES = Path(__file__).with_name("fixtures")
CONFIG = {p["id"]: p for p in providers.load_config()}


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def collect(id, data):
    return providers.collect_provider(CONFIG[id], fetcher=lambda url, timeout: data)


class ProviderStatusTests(unittest.TestCase):
    def test_all_current_source_schemas_parse_without_inference_or_network(self):
        # Reduced real public responses captured 2026-10-16. Scope selectors must
        # match every enabled publisher schema, not just a common happy-path fake.
        for id, row in CONFIG.items():
            if row["adapter"] == "unavailable":
                continue
            with self.subTest(provider=id):
                name = {"openai": "openai-proxy", "fireworks": "fireworks-proxy"}.get(id, id)
                data = fixture(name)
                if id == "deepinfra":
                    data["generatedAt"] = providers.utc_now()
                state, detail = providers.parse_source(data, row)
                self.assertIn(state, {"operational", "degraded", "downtime", "maintenance", "unknown"})
                self.assertNotIn("not found", detail)

    def test_anthropic_consumer_outage_does_not_change_api(self):
        data = fixture("anthropic")
        next(c for c in data["components"] if c["name"] == "claude.ai")["status"] = "major_outage"
        self.assertEqual(collect("anthropic", data)["state"], "operational")

    def test_api_outage_is_preserved(self):
        data = fixture("anthropic")
        next(c for c in data["components"] if "Claude API" in c["name"])["status"] = "major_outage"
        result = collect("anthropic", data)
        self.assertEqual(result["state"], "downtime")
        self.assertIn("Claude API", result["detail"])

    def test_missing_api_does_not_fall_back_to_green_aggregate(self):
        data = {"components": [{"name": "claude.ai", "status": "operational"}],
                "status": {"indicator": "none"}}
        self.assertEqual(collect("anthropic", data)["state"], "unknown")

    def test_openai_compliance_and_chatgpt_are_excluded_by_official_api_group(self):
        # Real source has a Compliance API incident assigned to ChatGPT, not APIs.
        data = fixture("openai-proxy")
        self.assertTrue(data["summary"]["affected_components"])
        self.assertEqual(collect("openai", data)["state"], "operational")
        data["summary"]["affected_components"].append({
            "component_id": "01JMXBRMFE6N2NNT7DG6XZQ6PW", "status": "partial_outage"})
        self.assertEqual(collect("openai", data)["state"], "degraded")

    def test_incident_summary_requires_complete_current_contract(self):
        data = fixture("openai-proxy")
        del data["summary"]["affected_components"]
        self.assertEqual(collect("openai", data)["state"], "unknown")
        data = fixture("openai-proxy")
        data["summary"]["structure"]["items"] = []
        self.assertEqual(collect("openai", data)["state"], "unknown")
        data = fixture("openai-proxy")
        data["summary"]["affected_components"] = [{}]
        self.assertEqual(collect("openai", data)["state"], "unknown")

    def test_fireworks_dedicated_scope_cannot_leak_into_serverless(self):
        data = fixture("fireworks-proxy")
        data["summary"]["components"].append({"id": "dedicated", "name": "Dedicated"})
        data["summary"]["affected_components"].append({"component_id": "dedicated", "status": "major_outage"})
        self.assertEqual(collect("fireworks", data)["state"], "operational")

    def test_novita_sandbox_degradation_does_not_change_llm_api(self):
        data = fixture("novita")
        self.assertTrue(any(x.get("attributes", {}).get("status") == "degraded" for x in data["included"]))
        self.assertEqual(collect("novita", data)["state"], "operational")

    def test_betterstack_not_monitored_is_unknown(self):
        self.assertEqual(collect("chutes", fixture("chutes"))["state"], "unknown")

    def test_datadog_website_failure_is_excluded_and_api_child_checked(self):
        data = fixture("openrouter")
        self.assertEqual(collect("openrouter", data)["state"], "operational")
        api = next(c for c in data["components"] if c["name"].startswith("API"))
        api["components"][0]["status"] = "downtime"
        self.assertEqual(collect("openrouter", data)["state"], "degraded")

    def test_one_selected_api_component_down_is_partial_degradation(self):
        components = [{"name": "API", "status": "operational"},
                      {"name": "Model A", "status": "major_outage"}]
        state, detail = providers._rollup(components, "Inference API and models")
        self.assertEqual(state, "degraded")
        self.assertIn("Model A", detail)

    def test_all_selected_api_components_down_is_provider_downtime(self):
        components = [{"name": "API", "status": "downtime"},
                      {"name": "Model A", "status": "major_outage"}]
        self.assertEqual(providers._rollup(components, "Inference API and models")[0], "downtime")

    def test_down_model_with_unknown_api_is_not_entire_provider_downtime(self):
        components = [{"name": "API", "status": "not_monitored"},
                      {"name": "Model A", "status": "major_outage"}]
        self.assertEqual(providers._rollup(components, "Inference API and models")[0], "degraded")

    def test_deepinfra_stale_snapshot_cannot_report_green(self):
        data = fixture("deepinfra")
        data["generatedAt"] = providers.utc_now()
        data["isStale"] = True
        self.assertEqual(collect("deepinfra", data)["state"], "unknown")
        data["isStale"] = False
        data["generatedAt"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertEqual(collect("deepinfra", data)["state"], "unknown")

    def test_unknown_selected_model_blocks_green_state(self):
        data = fixture("cerebras")
        next(c for c in data["components"] if "Console" not in c["name"])["status"] = "unexpected_new_value"
        self.assertEqual(collect("cerebras", data)["state"], "unknown")

    def test_maintenance_state_is_not_operational(self):
        data = fixture("minimax")
        data["components"][0]["status"] = "under_maintenance"
        self.assertEqual(collect("minimax", data)["state"], "maintenance")

    def test_request_failure_is_unknown_with_no_cached_green(self):
        def fail(url, timeout):
            raise urllib.error.URLError("test failure")
        result = providers.collect_provider(CONFIG["anthropic"], fetcher=fail)
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("test failure", result["detail"])

    def test_history_only_never_fetches_or_infers_current_green(self):
        def unexpected_fetch(*args, **kwargs):
            self.fail("History-only provider must not be treated as current status")
        result = providers.collect_provider(CONFIG["deepseek"], fetcher=unexpected_fetch)
        self.assertEqual(result["state"], "unknown")
        self.assertIn("history", result["detail"])

    def test_normalized_output_has_timezone_aware_observation(self):
        result = collect("minimax", fixture("minimax"))
        self.assertEqual(set(result), {"id", "name", "state", "observed_at", "source_url", "detail"})
        self.assertIsNotNone(datetime.fromisoformat(result["observed_at"]).tzinfo)


if __name__ == "__main__":
    unittest.main()
