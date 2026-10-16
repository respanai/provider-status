import copy
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import betterstack as bs


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def observation(state="operational", **changes):
    value = {"id": "openai", "name": "OpenAI", "state": state,
             "observed_at": "2026-10-16T12:00:00Z",
             "source_url": "https://status.openai.com/", "detail": "Official API components"}
    value.update(changes)
    return value


def resource(rid="10", name="OpenAI", managed=True, section="7"):
    return {"id": rid, "attributes": {"status_page_section_id": section,
            "resource_type": "ManuallyTrackedItem", "public_name": name,
            "explanation": bs.marker("openai") if managed else "First-party service",
            "status": "operational"}}


class FakeClient:
    def __init__(self, sections=True):
        self.sections = [{"id": "7", "attributes": {"name": "Providers"}}] if sections else []
        self.resources, self.reports, self.updates = [], [], {}
        self.writes = []
        self.fail_kind = None
        self.commit_before_failure = False
        self.next_id = 100

    def listing(self, path):
        if path.endswith("/sections"):
            return copy.deepcopy(self.sections)
        if path.endswith("/resources"):
            return copy.deepcopy(self.resources)
        if path.endswith("/status-reports"):
            return copy.deepcopy(self.reports)
        if path.endswith("/status-updates"):
            return copy.deepcopy(self.updates.get(path.split("/")[-2], []))
        raise AssertionError(path)

    def request(self, method, path, body=None):
        self.writes.append((method, path, copy.deepcopy(body)))
        kind = "explanation" if method == "PATCH" else path.split("/")[-1]
        fail = self.fail_kind == kind
        if fail and not self.commit_before_failure:
            raise bs.AmbiguousMutation("Simulated timeout")
        self.next_id += 1
        result = {"id": str(self.next_id), "attributes": copy.deepcopy(body)}
        if kind == "sections":
            self.sections.append(result)
        elif kind == "resources":
            self.resources.append(result)
        elif kind == "explanation":
            rid = path.split("/")[-1]
            result = next(item for item in self.resources if item["id"] == rid)
            result["attributes"].update(body)
        elif kind == "status-reports":
            result["attributes"]["aggregate_state"] = body["affected_resources"][0]["status"]
            self.reports.append(result)
            self.updates[result["id"]] = [{"id": str(self.next_id + 10000), "attributes": {
                key: copy.deepcopy(body[key]) for key in ("message", "published_at", "notify_subscribers", "affected_resources")}}]
        elif kind == "status-updates":
            report_id = path.split("/")[-2]
            self.updates.setdefault(report_id, []).append(result)
            report = next(item for item in self.reports if item["id"] == report_id)
            report["attributes"]["affected_resources"] = copy.deepcopy(body["affected_resources"])
            report["attributes"]["aggregate_state"] = body["affected_resources"][0]["status"]
        else:
            raise AssertionError(kind)
        if fail:
            raise bs.AmbiguousMutation("Simulated timeout after commit")
        return {"data": copy.deepcopy(result)}


def run(client, state, values, apply=True, **kwargs):
    return bs.Reconciler(client, state, apply=apply, now=NOW, **kwargs).run(values)


class ReconcileTests(unittest.TestCase):
    def test_dry_run_never_writes_or_persists(self):
        client = FakeClient(sections=False)
        store = bs.StateStore()
        output = run(client, store, [observation("degraded")], apply=False)
        self.assertFalse(client.writes)
        self.assertEqual([action["operation"] for action in output["actions"]], ["section", "resource", "report"])
        self.assertFalse(output["actions"][-1]["body"]["notify_subscribers"])

    def test_healthy_degraded_repeated_degraded_recovery_lifecycle(self):
        client, store = FakeClient(), bs.StateStore()
        self.assertFalse(run(client, store, [observation()])["errors"])
        self.assertEqual(len(client.resources), 1)
        self.assertFalse(client.reports)
        run(client, store, [observation("degraded")])
        self.assertEqual(len(client.reports), 1)
        report_id = client.reports[0]["id"]
        run(client, store, [observation("degraded")])
        self.assertEqual(len(client.reports), 1)
        self.assertEqual(len(client.updates[report_id]), 1)
        run(client, store, [observation("downtime")])
        run(client, store, [observation()])
        run(client, store, [observation()])
        self.assertEqual([u["attributes"]["affected_resources"][0]["status"]
                          for u in client.updates[report_id]], ["degraded", "downtime", "resolved"])
        self.assertTrue(all(not body["notify_subscribers"] for _, path, body in client.writes
                            if "status-report" in path))

    def test_older_observation_cannot_reopen_recovered_episode(self):
        client, store = FakeClient(), bs.StateStore()
        run(client, store, [observation()])
        writes = len(client.writes)
        output = run(client, store, [observation("downtime", observed_at="2026-10-16T11:59:00Z")])
        self.assertFalse(output["errors"])
        self.assertEqual(len(client.writes), writes)
        self.assertEqual(output["actions"][-1]["operation"], "skip-out-of-order")

    def test_existing_remote_episode_recovers_without_local_state(self):
        client = FakeClient()
        run(client, bs.StateStore(), [observation("downtime")])
        empty_store = bs.StateStore()
        output = run(client, empty_store, [observation()])
        self.assertFalse(output["errors"])
        self.assertEqual(len(client.resources), 1)
        self.assertEqual(len(client.reports), 1)
        self.assertEqual(client.updates[client.reports[0]["id"]][-1]["attributes"]["affected_resources"][0]["status"], "resolved")

    def test_unknown_never_creates_or_recovers(self):
        client, store = FakeClient(sections=False), bs.StateStore()
        run(client, store, [observation("unknown")])
        self.assertFalse(client.writes)
        client.sections = [{"id": "7", "attributes": {"name": "Providers"}}]
        run(client, store, [observation("downtime")])
        writes_before = len(client.writes)
        run(client, store, [observation("unknown", detail="Fetch failed")])
        new = client.writes[writes_before:]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0][0], "PATCH")
        self.assertIn("Current provider status unavailable", new[0][2]["explanation"])
        self.assertEqual(store.data["providers"]["openai"]["last_state"], "downtime")

    def test_unknown_without_public_source_is_skipped(self):
        client = FakeClient()
        output = run(client, bs.StateStore(), [observation("unknown", source_url="")])
        self.assertFalse(output["errors"])
        self.assertFalse(client.writes)

    def test_missing_state_defaults_to_unknown_without_writes(self):
        client = FakeClient()
        value = observation()
        del value["state"]
        output = run(client, bs.StateStore(), [value])
        self.assertFalse(output["errors"])
        self.assertFalse(client.writes)

    def test_stale_future_and_untimed_maintenance_are_unknown(self):
        for changes in ({"observed_at": "2026-10-16T11:00:00Z"},
                        {"observed_at": "2026-10-16T13:00:00Z"}, {"state": "maintenance"}):
            with self.subTest(changes=changes):
                client = FakeClient()
                run(client, bs.StateStore(), [observation(**changes)])
                self.assertFalse(client.writes)

    def test_unmanaged_same_name_and_duplicate_managed_rows_fail_closed(self):
        for rows in ([resource(managed=False)], [resource(), resource("11")]):
            with self.subTest(rows=rows):
                client = FakeClient()
                client.resources = rows
                output = run(client, bs.StateStore(), [observation("degraded")])
                self.assertTrue(output["errors"])
                self.assertFalse(client.writes)

    def test_platform_enterprise_and_other_section_never_mutated(self):
        client = FakeClient()
        client.resources = [resource("1", "Platform", False, "2"), resource("2", "Enterprise", False, "2")]
        original = copy.deepcopy(client.resources)
        run(client, bs.StateStore(), [observation("degraded")])
        self.assertEqual(client.resources[:2], original)
        for _, _, body in client.writes:
            self.assertNotIn("1", [r["status_page_resource_id"] for r in body.get("affected_resources", [])])
            self.assertNotIn("2", [r["status_page_resource_id"] for r in body.get("affected_resources", [])])
        with self.assertRaises(bs.SyncError):
            run(client, bs.StateStore(), [observation(name="Platform")])

    def test_ambiguous_post_committed_is_recovered_without_retry(self):
        client, store = FakeClient(), bs.StateStore()
        client.fail_kind = "status-reports"
        client.commit_before_failure = True
        output = run(client, store, [observation("degraded")])
        self.assertFalse(output["errors"])
        self.assertEqual(len([w for w in client.writes if w[1].endswith("/status-reports")]), 1)
        self.assertEqual(len(client.reports), 1)
        self.assertFalse(store.data["pending"])

    def test_unconfirmed_post_persists_intent_and_blocks_later_post(self):
        client = FakeClient()
        client.fail_kind = "status-reports"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = bs.StateStore(path)
            first = run(client, store, [observation("degraded")])
            self.assertTrue(first["errors"])
            self.assertIn("openai", json.loads(path.read_text())["pending"])
            writes = len(client.writes)
            client.fail_kind = None
            second = run(client, bs.StateStore(path), [observation("degraded")])
            self.assertTrue(second["errors"])
            self.assertEqual(len(client.writes), writes)
            self.assertNotIn("Authorization", path.read_text())

    def test_duplicate_ids_rejected_before_writes(self):
        client = FakeClient()
        with self.assertRaises(bs.SyncError):
            run(client, bs.StateStore(), [observation(), observation()])
        self.assertFalse(client.writes)

    def test_mixed_managed_report_never_resolved(self):
        client = FakeClient()
        run(client, bs.StateStore(), [observation("degraded")])
        client.reports[0]["attributes"]["affected_resources"].append({"status_page_resource_id": "platform", "status": "downtime"})
        before = len(client.writes)
        output = run(client, bs.StateStore(), [observation()])
        self.assertTrue(output["errors"])
        self.assertTrue(all("status-update" not in w[1] for w in client.writes[before:]))


class ClientTests(unittest.TestCase):
    def test_transport_error_does_not_expose_token_or_raw_error(self):
        import urllib.error
        client = bs.BetterStackClient("test-secret-token")
        with patch.object(client.opener, "open", side_effect=urllib.error.URLError("test-secret-token")):
            with self.assertRaises(bs.ApiError) as error:
                client.request("GET", "/status-pages/176303/resources")
        self.assertNotIn("test-secret-token", str(error.exception))

    def test_pagination_and_cross_origin_protection(self):
        client = bs.BetterStackClient("secret-test-token")
        responses = [{"data": [{"id": "1"}], "pagination": {"next": bs.BASE_URL + "/status-pages/176303/resources?page=2"}},
                     {"data": [{"id": "2"}], "pagination": {"next": None}}]
        with patch.object(client, "request", side_effect=responses) as request:
            self.assertEqual(len(client.listing("/status-pages/176303/resources")), 2)
            self.assertEqual(request.call_count, 2)
        with patch.object(client, "request", return_value={"data": [], "pagination": {"next": "https://evil.example/api/v2/resources"}}) as request:
            with self.assertRaises(bs.SyncError):
                client.listing("/status-pages/176303/resources")
            self.assertEqual(request.call_count, 1)

    def test_cli_offline_dry_run_does_not_touch_state(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path, state_path = Path(directory) / "input.json", Path(directory) / "state.json"
            input_path.write_text(json.dumps([observation(observed_at=bs.iso(datetime.now(timezone.utc)))]))
            with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = bs.main(["--input", str(input_path), "--state", str(state_path)])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(stdout.getvalue())["mode"], "dry-run")
            self.assertFalse(state_path.exists())

    def test_apply_requires_credential_and_disallows_snapshot(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(bs.main(["--input", "does-not-exist.json", "--apply"]), 1)

    def test_offline_never_uses_account_token(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            input_path.write_text(json.dumps([observation(observed_at=bs.iso(datetime.now(timezone.utc)))]))
            with patch.dict(os.environ, {"BETTERSTACK_API_TOKEN": "test-secret"}), patch.object(bs.BetterStackClient, "request") as request, patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(bs.main(["--input", str(input_path), "--offline"]), 0)
                request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
