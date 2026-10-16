#!/usr/bin/env python3
"""Reconcile official provider observations into Better Stack manual rows.

Dry-run is the default. Apply requires BETTERSTACK_API_TOKEN, a durable state
file, and an explicit --apply. No write is automatically retried. State files
contain public status metadata only, never the API token.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://incidents.betterstack.com/api/v2"
PAGE_ID = "176303"
SECTION_NAME = "Providers"
PROVIDER_WIDGET = "plain"  # Native history pads unobserved pre-creation days green.
STATES = {"operational", "degraded", "downtime", "maintenance", "unknown"}
RESERVED_NAMES = {"platform", "enterprise"}


class SyncError(Exception):
    """A safe error suitable for logs; must not contain credentials."""


class ApiError(SyncError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AmbiguousMutation(ApiError):
    """A mutation might have committed; reconcile, never blindly retry."""


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise SyncError("Timestamp must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncError("Invalid ISO-8601 timestamp") from exc
    if result.tzinfo is None:
        raise SyncError("Timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def marker(provider_id: str) -> str:
    return f"Respan provider status: {provider_id}."


def attributes(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("attributes", {})


def resource_states(body: dict[str, Any]) -> list[dict[str, str]]:
    return body.get("affected_resources", [])


def normalized(value: dict[str, Any], now: datetime, max_age: int) -> dict[str, Any]:
    result = copy.deepcopy(value)
    pid = result.get("id")
    if not isinstance(pid, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", pid):
        raise SyncError("Invalid provider ID")
    name = result.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise SyncError(f"{pid}: invalid provider name")
    result["name"] = name.strip()
    if result["name"].casefold() in RESERVED_NAMES:
        raise SyncError(f"{pid}: reserved first-party resource name")
    state = result.get("state", "unknown")
    if state not in STATES:
        raise SyncError(f"{pid}: invalid provider state")
    result["state"] = state
    source = result.get("source_url") or ""
    result["source_url"] = source
    parsed = urllib.parse.urlsplit(source)
    if not source and state == "unknown":
        pass  # Unsupported providers have no invented source URL.
    elif parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise SyncError(f"{pid}: source_url must be a public HTTPS URL without credentials")
    result["detail"] = str(result.get("detail", "")).replace("\x00", "")[:600]
    try:
        observed = timestamp(result.get("observed_at"))
        if observed > now + timedelta(seconds=60):
            raise SyncError("Observation is in the future")
        if (now - observed).total_seconds() > max_age:
            raise SyncError("Observation is stale")
        result["observed_at"] = iso(observed)
    except SyncError as exc:
        result["state"] = "unknown"
        result["detail"] = str(exc)
        result["observed_at"] = iso(now)
    if result.get("state") == "maintenance":
        # The collector's basic contract has no authoritative end time. Better
        # Stack requires one; never invent a maintenance interval.
        result["state"] = "unknown"
        result["detail"] = "Provider reports maintenance; no supported authoritative maintenance interval"
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward a bearer token across a redirect.


class BetterStackClient:
    def __init__(self, token: str, timeout: int = 30):
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def _url(self, path: str) -> str:
        url = path if path.startswith("https://") else BASE_URL + path
        parsed = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(BASE_URL)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise SyncError("Rejected API URL outside the configured Better Stack origin")
        if not parsed.path.startswith(base.path + "/") or parsed.username or parsed.password:
            raise SyncError("Rejected API URL outside the configured Better Stack API")
        return url

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self._url(path)
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, payload, headers, method=method)
        write = method != "GET"
        safe_path = urllib.parse.urlsplit(url).path
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ValueError("Response too large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("Expected JSON object")
                return result
        except urllib.error.HTTPError as exc:
            # Never log response bodies: they can contain account metadata.
            error_type = AmbiguousMutation if write and (exc.code >= 500 or exc.code == 408) else ApiError
            raise error_type(f"Better Stack {method} {safe_path} returned HTTP {exc.code}", status_code=exc.code) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            error_type = AmbiguousMutation if write else ApiError
            raise error_type(f"Better Stack {method} {safe_path} returned no usable response") from None

    def listing(self, path: str) -> list[dict[str, Any]]:
        result, seen = [], set()
        while path:
            url = self._url(path)
            if url in seen or len(seen) >= 1000:
                raise SyncError("Invalid/cyclic Better Stack pagination")
            seen.add(url)
            response = self.request("GET", path)
            data = response.get("data")
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise SyncError("Invalid Better Stack list response")
            result.extend(data)
            path = response.get("pagination", {}).get("next")
            if path is not None and not isinstance(path, str):
                raise SyncError("Invalid Better Stack next-page URL")
        return result


class SnapshotClient:
    """Offline dry-run input; not a substitute for live account verification."""

    def __init__(self, snapshot: dict[str, Any] | None = None):
        self.snapshot = snapshot or {}

    def listing(self, path: str) -> list[dict[str, Any]]:
        if path.endswith("/sections"):
            return copy.deepcopy(self.snapshot.get("sections", []))
        if path.endswith("/resources"):
            return copy.deepcopy(self.snapshot.get("resources", []))
        if path.endswith("/status-reports"):
            return copy.deepcopy(self.snapshot.get("reports", []))
        if path.endswith("/status-updates"):
            report_id = path.split("/")[-2]
            return copy.deepcopy(self.snapshot.get("updates", {}).get(report_id, []))
        raise SyncError("Unsupported snapshot read")


class StateStore:
    def __init__(self, path: Path | None = None, initial: dict[str, Any] | None = None, page_id: str = PAGE_ID):
        self.path = path
        if initial is not None:
            self.data = copy.deepcopy(initial)
        elif path and path.exists():
            self.data = json.loads(path.read_text())
        else:
            self.data = {"schema_version": 1, "page_id": str(page_id), "providers": {}, "pending": {}}
        if self.data.get("schema_version") != 1:
            raise SyncError("Unsupported state schema")
        self.data.setdefault("providers", {})
        self.data.setdefault("pending", {})

    def save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, self.path)


class Reconciler:
    def __init__(self, client, state: StateStore, *, apply: bool = False,
                 page_id: str = PAGE_ID, now: datetime | None = None, max_age: int = 900):
        self.client, self.store, self.apply = client, state, apply
        self.page_id = str(page_id)
        self.root = f"/status-pages/{self.page_id}"
        self.now = now or datetime.now(timezone.utc)
        self.max_age = max_age
        if str(self.store.data.get("page_id")) != self.page_id:
            raise SyncError("State belongs to a different status page")
        self.actions, self.errors, self.warnings = [], [], []
        self.resources, self.reports, self.updates = [], [], {}

    def _list(self, suffix: str):
        return self.client.listing(self.root + suffix)

    def _updates(self, report_id: str):
        if report_id not in self.updates:
            self.updates[report_id] = self._list(f"/status-reports/{report_id}/status-updates")
        return self.updates[report_id]

    def _find_operation(self, operation: dict[str, Any]) -> dict[str, Any] | None:
        kind, body = operation["kind"], operation["body"]
        matches = []
        if kind == "section":
            matches = [item for item in self._list("/sections") if attributes(item).get("name") == SECTION_NAME]
        elif kind == "resource":
            matches = [item for item in self._list("/resources")
                       if attributes(item).get("public_name") == body["public_name"]
                       and attributes(item).get("resource_type") == "ManuallyTrackedItem"
                       and str(attributes(item).get("status_page_section_id")) == str(body["status_page_section_id"])
                       and marker(operation["owner"]) in attributes(item).get("explanation", "")]
        elif kind == "explanation":
            matches = [item for item in self._list("/resources")
                       if str(item.get("id")) == operation["path"].split("/")[-1]
                       and attributes(item).get("explanation") == body["explanation"]
                       and ("widget_type" not in body or attributes(item).get("widget_type") == body["widget_type"])]
        elif kind == "report":
            for report in self._list("/status-reports"):
                attr = attributes(report)
                if attr.get("title") != body["title"] or resource_states(attr) != resource_states(body):
                    continue
                for update in self._list(f"/status-reports/{report['id']}/status-updates"):
                    if self._same_update(attributes(update), body):
                        matches.append(report)
                        break
        elif kind == "update":
            matches = [item for item in self.client.listing(operation["path"])
                       if self._same_update(attributes(item), body)]
        if len(matches) > 1:
            raise SyncError(f"Multiple remote matches for pending {kind}; operator review required")
        return {"data": matches[0]} if matches else None

    @staticmethod
    def _same_update(actual, expected):
        try:
            same_time = timestamp(actual.get("published_at")) == timestamp(expected.get("published_at"))
        except SyncError:
            same_time = False
        return (actual.get("message") == expected.get("message") and same_time
                and resource_states(actual) == resource_states(expected)
                and actual.get("notify_subscribers") is False)

    def _recover_pending(self, owner: str):
        operation = self.store.data["pending"].get(owner)
        if operation is None:
            return
        found = self._find_operation(operation)
        if found is None:
            raise SyncError(f"Pending {operation['kind']} has no confirmed remote match; no write retried")
        self._remember(owner, operation["kind"], found)
        del self.store.data["pending"][owner]
        self.store.save()
        self.actions.append({"provider_id": owner, "operation": "recover-pending", "status": "confirmed"})

    def _remember(self, owner: str, kind: str, response: dict[str, Any]):
        rid = response.get("data", {}).get("id")
        if rid is None:
            raise AmbiguousMutation("Better Stack mutation returned no resource ID")
        if kind in {"resource", "report"}:
            field = "resource_id" if kind == "resource" else "report_id"
            self.store.data["providers"].setdefault(owner, {})[field] = str(rid)

    def _write(self, owner: str, kind: str, method: str, suffix: str, body: dict[str, Any]):
        action = {"provider_id": owner, "operation": kind, "method": method,
                  "path": self.root + suffix, "body": body, "status": "planned"}
        self.actions.append(action)
        if not self.apply:
            return {"data": {"id": f"dry-{kind}-{owner}", "attributes": copy.deepcopy(body)}}
        if owner in self.store.data["pending"]:
            raise SyncError("Pending mutation must be reconciled before another write")
        operation = {"owner": owner, "kind": kind, "method": method,
                     "path": self.root + suffix, "body": body,
                     "intent_at": iso(self.now)}
        operation["fingerprint"] = hashlib.sha256(json.dumps(operation, sort_keys=True).encode()).hexdigest()
        self.store.data["pending"][owner] = operation
        self.store.save()  # Durable intent precedes the request.
        try:
            response = self.client.request(method, operation["path"], body)
            self._remember(owner, kind, response)
        except AmbiguousMutation:
            # A single readback is safe; no second POST/PATCH is issued.
            response = self._find_operation(operation)
            if response is None:
                action["status"] = "unconfirmed"
                raise SyncError(f"Ambiguous {kind}; pending intent retained and no mutation retried") from None
            self._remember(owner, kind, response)
        except ApiError:
            del self.store.data["pending"][owner]  # Definite validation/auth rejection.
            self.store.save()
            action["status"] = "rejected"
            raise
        del self.store.data["pending"][owner]
        self.store.save()
        action["status"] = "applied"
        return response

    def _section(self):
        self._recover_pending("__section__")
        matches = [item for item in self._list("/sections") if attributes(item).get("name") == SECTION_NAME]
        if len(matches) > 1:
            raise SyncError("Multiple Providers sections; operator review required")
        if matches:
            return str(matches[0]["id"])
        response = self._write("__section__", "section", "POST", "/sections", {"name": SECTION_NAME})
        return str(response["data"]["id"])

    def _resource(self, provider, section_id):
        pid, name = provider["id"], provider["name"]
        in_section = [item for item in self.resources
                      if str(attributes(item).get("status_page_section_id")) == str(section_id)]
        candidates = [item for item in in_section if marker(pid) in attributes(item).get("explanation", "")]
        if len(candidates) > 1:
            raise SyncError("Duplicate managed provider resources; no changes made")
        if candidates:
            item = candidates[0]
            if attributes(item).get("public_name") != name or attributes(item).get("resource_type") != "ManuallyTrackedItem":
                raise SyncError("Managed marker/name/type mismatch; no changes made")
            return item
        if any(attributes(item).get("public_name") == name for item in in_section):
            raise SyncError("Unmanaged resource has the same name; no changes made")
        return None

    def _active_report(self, provider, rid):
        title = provider["name"] + " provider status"
        matches = []
        for report in self.reports:
            attr = attributes(report)
            affected = resource_states(attr)
            if not any(str(item.get("status_page_resource_id")) == rid for item in affected):
                continue
            if attr.get("report_type") != "manual" or attr.get("title") != title:
                continue
            updates = self._updates(str(report["id"]))
            if not any(marker(provider["id"]) in attributes(item).get("message", "") for item in updates):
                continue
            # Never update a mixed report that could alter Platform/Enterprise.
            if any(str(item.get("status_page_resource_id")) != rid for item in affected):
                raise SyncError("Managed report also affects other resources; operator review required")
            if any(any(str(item.get("status_page_resource_id")) != rid
                       for item in resource_states(attributes(update))) for update in updates):
                raise SyncError("Managed update also affects other resources; operator review required")
            state = attr.get("aggregate_state")
            # The report's aggregate is the server's current state. Timestamps
            # may tie across updates, so do not override a resolved aggregate
            # with the first update that happens to share that timestamp.
            if state not in {"resolved", "operational", "degraded", "downtime"} and updates:
                latest = max(enumerate(updates), key=lambda pair: (timestamp(attributes(pair[1]).get("published_at")), pair[0]))[1]
                latest_states = resource_states(attributes(latest))
                if latest_states:
                    state = latest_states[0].get("status")
            if state not in {"resolved", "operational"}:
                if state not in {"degraded", "downtime"}:
                    raise SyncError("Unrecognized managed report state")
                matches.append((report, state))
        if len(matches) > 1:
            raise SyncError("Multiple open managed reports; operator review required")
        return matches[0] if matches else (None, None)

    def _explanation(self, provider, prior):
        head = marker(provider["id"]) + "\nOfficial source: " + (provider["source_url"] or "No public feed available")
        if provider["state"] == "unknown":
            known = prior.get("last_state", "unverified")
            at = prior.get("last_success_at", "not available")
            return (head + f"\nCurrent provider status unavailable. Last verified: {known} at {at}. "
                    + provider["detail"])
        return head + f"\nProvider-reported {provider['state']}; verified {provider['observed_at']}."

    def _provider(self, provider, section_id):
        pid = provider["id"]
        if self.apply and pid in self.store.data["pending"]:
            self._recover_pending(pid)
            # Readbacks may have discovered a committed resource/report.
            self.resources, self.reports = self._list("/resources"), self._list("/status-reports")
            self.updates.clear()
        item = self._resource(provider, section_id)
        prior = self.store.data["providers"].setdefault(pid, {})
        if provider["state"] != "unknown" and prior.get("last_success_at"):
            if timestamp(provider["observed_at"]) < timestamp(prior["last_success_at"]):
                self.actions.append({"provider_id": pid, "operation": "skip-out-of-order", "status": "skipped"})
                return
        if item is None and provider["state"] == "unknown":
            self.actions.append({"provider_id": pid, "operation": "skip-unknown", "status": "skipped"})
            return
        explanation = self._explanation(provider, prior)
        if item is None:
            item = self._write(pid, "resource", "POST", "/resources", {
                "status_page_section_id": int(section_id) if section_id.isdigit() else section_id,
                # The live API requires omission: explicit null returns
                # HTTP 422 {"resource": ["can't be blank"]}.
                "resource_type": "ManuallyTrackedItem",
                "public_name": provider["name"], "explanation": explanation, "widget_type": PROVIDER_WIDGET})["data"]
            self.resources.append(item)
        rid = str(item["id"])
        prior["resource_id"] = rid
        if (attributes(item).get("explanation") != explanation
                or attributes(item).get("widget_type") != PROVIDER_WIDGET):
            self._write(pid, "explanation", "PATCH", f"/resources/{rid}",
                        {"explanation": explanation, "widget_type": PROVIDER_WIDGET})
        if provider["state"] == "unknown":
            self.warnings.append(f"{pid}: source unavailable; native row retains its last known badge, explanation marked unavailable")
            if self.apply:
                self.store.save()
            return
        report, active_state = self._active_report(provider, rid)
        state = provider["state"]
        message = (marker(pid) + f"\nProvider-reported status: {state}. " + provider["detail"]
                   + "\nOfficial source: " + provider["source_url"])
        affected = [{"status_page_resource_id": rid, "status": "resolved" if state == "operational" else state}]
        body = {"message": message, "notify_subscribers": False,
                "published_at": provider["observed_at"], "affected_resources": affected}
        if report is None and state != "operational":
            body.update({"title": provider["name"] + " provider status", "report_type": "manual",
                         "starts_at": provider["observed_at"]})
            created = self._write(pid, "report", "POST", "/status-reports", body)
            prior["report_id"] = str(created["data"]["id"])
        elif report is not None and state != active_state:
            report_id = str(report["id"])
            self._write(pid, "update", "POST", f"/status-reports/{report_id}/status-updates", body)
            if state == "operational":
                prior.pop("report_id", None)
        prior.update({"last_state": state, "last_success_at": provider["observed_at"],
                      "source_url": provider["source_url"]})
        if self.apply:
            self.store.save()

    def run(self, observations: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(observations, list):
            raise SyncError("Observations must be a JSON list")
        providers = [normalized(value, self.now, self.max_age) for value in observations]
        if len({value["id"] for value in providers}) != len(providers):
            raise SyncError("Duplicate provider IDs in observations")
        # An empty/entirely unknown first run creates no misleading rows/section.
        sections = self._list("/sections")
        has_section = any(attributes(item).get("name") == SECTION_NAME for item in sections)
        section_id = None
        if has_section or any(value["state"] != "unknown" for value in providers):
            section_id = self._section()
            self.resources, self.reports = self._list("/resources"), self._list("/status-reports")
            for provider in providers:
                try:
                    self._provider(provider, section_id)
                except SyncError as exc:
                    self.errors.append({"provider_id": provider["id"], "error": str(exc)})
                    if isinstance(exc, ApiError) and exc.status_code in {401, 403, 422}:
                        # Authentication and validation errors require a fix;
                        # do not repeat the same rejected request for every row.
                        break
        else:
            for provider in providers:
                self.actions.append({"provider_id": provider["id"], "operation": "skip-unknown", "status": "skipped"})
        return {"mode": "apply" if self.apply else "dry-run", "page_id": self.page_id,
                "section_id": section_id, "actions": self.actions, "errors": self.errors, "warnings": self.warnings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Collector observations JSON")
    parser.add_argument("--state", type=Path, default=Path("state/betterstack.json"))
    parser.add_argument("--page-id", default=os.environ.get("BETTERSTACK_STATUS_PAGE_ID", PAGE_ID))
    parser.add_argument("--snapshot", type=Path, help="Offline dry-run snapshot: sections/resources/reports/updates")
    parser.add_argument("--offline", action="store_true", help="Dry-run without any account calls, even if a token is set")
    parser.add_argument("--max-age-seconds", type=int, default=900)
    parser.add_argument("--apply", action="store_true", help="Explicitly mutate Better Stack using env token")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[0-9]+", args.page_id) or args.max_age_seconds < 1:
            raise SyncError("Invalid page ID or freshness threshold")
        token = os.environ.get("BETTERSTACK_API_TOKEN", "")
        if args.apply and (not token or args.snapshot or args.offline):
            raise SyncError("Apply requires BETTERSTACK_API_TOKEN and live account reads (no snapshot)")
        observations = json.loads(args.input.read_text())
        if isinstance(observations, dict):
            observations = observations.get("providers", observations.get("observations"))
        snapshot = json.loads(args.snapshot.read_text()) if args.snapshot else None
        client = BetterStackClient(token) if token and not args.snapshot and not args.offline else SnapshotClient(snapshot)
        store = StateStore(args.state, page_id=args.page_id)
        if not args.apply:
            store.path = None  # A dry run must not change persisted state.
        sync = Reconciler(client, store, apply=args.apply, page_id=args.page_id, max_age=args.max_age_seconds)
        output = sync.run(observations)
        if isinstance(client, SnapshotClient):
            output["warnings"].append("Offline dry-run: account uniqueness and saved editor structure are not verified")
        print(json.dumps(output, indent=2, sort_keys=True))
        return 1 if output["errors"] else 0
    except (SyncError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
