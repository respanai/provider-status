"""Read official public status sources. Python standard library; no API keys.

Every successful state is a provider's published current state for the configured
API scope. RSS/history alone, missing scopes, stale feeds and request failures
produce ``unknown``. The collector never sends inference requests.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_CONFIG = Path(__file__).with_name("config") / "providers.json"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
STATES = {
    "operational": "operational", "up": "operational", "none": "operational",
    "degraded": "degraded", "degraded_performance": "degraded",
    "partial_outage": "degraded", "minor": "degraded", "minoroutage": "degraded",
    "degradedperformance": "degraded", "hasissues": "degraded",
    "down": "downtime", "downtime": "downtime", "major_outage": "downtime",
    "full_outage": "downtime",
    "major": "downtime", "critical": "downtime", "majoroutage": "downtime",
    "under_maintenance": "maintenance", "maintenance": "maintenance",
}


class SourceError(ValueError):
    """The source cannot establish a current state for the configured scope."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_of(value) -> str:
    return STATES.get(str(value).strip().lower(), "unknown")


def load_config(path=None) -> list[dict]:
    data = json.loads(Path(path or DEFAULT_CONFIG).read_text())
    providers = data["providers"] if isinstance(data, dict) else data
    ids = [row["id"] for row in providers]
    if len(ids) != len(set(ids)):
        raise ValueError("Provider IDs must be unique")
    return providers


def fetch_json(url: str, timeout: float = 12):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise SourceError("Status sources must be public HTTPS URLs")
    req = urllib.request.Request(url, headers={
        "User-Agent": "RespanProviderStatus/1.0 (+https://respan.ai)",
        "Accept": "application/json", "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceError("Status response exceeds size limit")
    return json.loads(body)


def _matches(value, pattern) -> bool:
    return bool(pattern and re.search(pattern, str(value), re.IGNORECASE))


def _selected(items: list[dict], row: dict) -> list[dict]:
    """Select exact API names/IDs or descendants of configured groups."""
    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
        raise SourceError("Missing or invalid components")
    names = {str(x).strip().casefold() for x in row.get("names", [])}
    ids = {str(x) for x in row.get("component_ids", [])}
    group_ids = {str(x.get("id")) for x in items
                 if _matches(x.get("name", ""), row.get("group_pattern"))}
    selected = []
    for item in items:
        name = str(item.get("name", "")).strip()
        group = item.get("group")
        parent_id = group.get("id") if isinstance(group, dict) else item.get("group_id")
        direct = (row.get("all_components") or str(item.get("id")) in ids
                  or name.casefold() in names
                  or _matches(name, row.get("name_pattern")))
        descendant = str(parent_id) in group_ids
        if (direct or descendant) and not _matches(name, row.get("exclude_pattern")):
            selected.append(item)
    if not selected:
        raise SourceError("Configured API components were not found")
    missing_names = names - {str(x.get("name", "")).strip().casefold() for x in selected}
    missing_ids = ids - {str(x.get("id")) for x in selected}
    if missing_names or missing_ids:
        raise SourceError("One or more configured API components are missing")
    if row.get("group_pattern") and not group_ids and not names and not ids:
        raise SourceError("Configured API group was not found")
    return selected


def _rollup(components: list[dict], scope: str) -> tuple[str, str]:
    if not components:
        raise SourceError("No current API component states")
    states = [state_of(x.get("status")) for x in components]
    # Provider downtime means the entire selected API scope is down. A failed
    # model alongside healthy or unknown services is a partial degradation.
    if all(s == "downtime" for s in states):
        state = "downtime"
    elif any(s in {"downtime", "degraded"} for s in states):
        state = "degraded"
    else:
        state = next((s for s in ("maintenance", "unknown") if s in states), "operational")
    affected = [str(c.get("name", "Unnamed component")) + " (" +
                str(c.get("status", "unknown")).lower().replace("_", " ") + ")"
                for c, s in zip(components, states) if s != "operational"]
    if affected:
        detail = scope + ": " + ", ".join(affected[:4])
        if len(affected) > 4:
            detail += " and " + str(len(affected) - 4) + " more"
    else:
        detail = f"{scope}: {len(components)} component(s) operational"
    return state, detail


def _statuspage(data, row):
    if not isinstance(data, dict):
        raise SourceError("Invalid status summary")
    return _rollup(_selected(data.get("components"), row), row["scope"])


def _incident_proxy(data, row):
    """Incident.io's published current affected list plus exact group membership.

    Unlike RSS, this response explicitly enumerates currently affected components.
    Absence from that list means operational only after validating the complete
    current summary and required group structure.
    """
    summary = data.get("summary") if isinstance(data, dict) else None
    if not isinstance(summary, dict) or not isinstance(summary.get("affected_components"), list):
        raise SourceError("Missing current affected-components summary")
    structure = summary.get("structure", {}).get("items")
    if not isinstance(structure, list) or not isinstance(summary.get("components"), list):
        raise SourceError("Missing status-page structure")
    selected = []
    for item in structure:
        group = item.get("group", {})
        if _matches(group.get("name", ""), row.get("group_pattern")) and not group.get("hidden"):
            if not isinstance(group.get("components"), list):
                raise SourceError("Invalid API group components")
            selected.extend(x for x in group["components"] if not x.get("hidden"))
    known_ids = {x.get("id") for x in summary["components"]}
    if any(not isinstance(x, dict) or not x.get("component_id") or not x.get("status")
           for x in summary["affected_components"]):
        raise SourceError("Invalid current affected-components summary")
    affected = {x["component_id"]: x["status"] for x in summary["affected_components"]}
    if not selected or any(x.get("component_id") not in known_ids for x in selected):
        raise SourceError("Configured API group was not found completely")
    current = [{"name": x.get("name"), "status": affected.get(x["component_id"], "operational")}
               for x in selected]
    return _rollup(current, row["scope"])


def _betterstack(data, row):
    if not isinstance(data, dict) or data.get("data", {}).get("type") != "status_page":
        raise SourceError("Invalid Better Stack public status response")
    included = data.get("included")
    if not isinstance(included, list):
        raise SourceError("Missing current resources")
    sections = {str(x["id"]): x.get("attributes", {}).get("name", "")
                for x in included if x.get("type") == "status_page_section"}
    components = []
    for x in included:
        if x.get("type") != "status_page_resource":
            continue
        a = x.get("attributes", {})
        section = sections.get(str(a.get("status_page_section_id")), "")
        components.append({"id": x.get("id"), "name": a.get("public_name", ""),
                           "status": a.get("status"), "section": section})
    if row.get("section_pattern"):
        components = [x for x in components if _matches(x["section"], row["section_pattern"])
                      or str(x["name"]).strip().casefold() in
                      {n.casefold() for n in row.get("names", [])}]
        if not components:
            raise SourceError("Configured inference section was not found")
        row = {**row, "all_components": True}
    return _rollup(_selected(components, row), row["scope"])


def _datadog(data, row):
    if not isinstance(data, dict) or not isinstance(data.get("components"), list):
        raise SourceError("Invalid Datadog status response")
    selected = []
    for group in data["components"]:
        if _matches(group.get("name", ""), row.get("group_pattern")):
            children = group.get("components")
            if not isinstance(children, list) or not children:
                raise SourceError("API group has no current components")
            selected.extend(children)
    return _rollup(selected, row["scope"])


def _deepinfra(data, row):
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise SourceError("Unsupported DeepInfra status schema")
    if data.get("isStale") is not False:
        raise SourceError("Provider marks current status stale")
    generated = datetime.fromisoformat(data["generatedAt"].replace("Z", "+00:00"))
    if generated.tzinfo is None or (datetime.now(timezone.utc) - generated).total_seconds() > 600:
        raise SourceError("Provider status snapshot is stale")
    components = [{"name": x.get("displayName"), "status": x.get("status")}
                  for x in data.get("services", []) if x.get("publicId") == "api"]
    if len(components) != 1:
        raise SourceError("Missing API service state")
    return _rollup(components, row["scope"])


def _aggregate(data, row):
    if not isinstance(data, dict) or not isinstance(data.get("status"), dict):
        raise SourceError("Missing provider current aggregate state")
    state = state_of(data["status"].get("indicator"))
    return state, row["scope"] + ": " + str(data["status"].get("description", state))


def _empiriolabs(data, row):
    if not isinstance(data, dict) or not isinstance(data.get("sites"), list):
        raise SourceError("Missing EmpirioLabs current service states")
    now = datetime.now(timezone.utc)
    timestamps = [data.get("generatedAt")]
    selected = _selected(data["sites"], row)
    timestamps.extend(x.get("lastUpdated") for x in selected)
    for value in timestamps:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None or not -120 <= (now - stamp).total_seconds() <= 900:
            raise SourceError("EmpirioLabs status snapshot is stale or untimed")
    return _rollup(selected, row["scope"])


def _flashduty(data, row):
    """Flashduty's public current active-summary API, used by DeepSeek's page.

    The official frontend derives each component's current state from this
    explicit active list. History feeds and cached SSR fallback data are unused.
    """
    summary = data.get("data") if isinstance(data, dict) else None
    if not isinstance(summary, dict) or not isinstance(summary.get("active_changes"), list):
        raise SourceError("Missing Flashduty current active-summary contract")
    page = summary.get("page")
    if not isinstance(page, dict) or str(page.get("page_id")) != str(row.get("page_id")):
        raise SourceError("Unexpected Flashduty status page identity")
    if page.get("custom_domain") != urllib.parse.urlparse(row["status_url"]).hostname:
        raise SourceError("Unexpected Flashduty status page domain")
    components = page.get("components")
    if not isinstance(components, list):
        raise SourceError("Missing Flashduty current components")
    selected = _selected([{"id": x.get("component_id"), "name": x.get("name"),
                           "status": "operational"} for x in components], row)
    by_id = {x["id"]: x for x in selected}
    for change in summary["active_changes"]:
        if not isinstance(change, dict) or not isinstance(change.get("status"), str):
            raise SourceError("Invalid Flashduty active change")
        if change["status"] in {"resolved", "completed"}:
            continue
        if change.get("type") == "maintenance" and change["status"] == "scheduled":
            continue
        affected = change.get("affected_components")
        if not isinstance(affected, list) or not affected:
            raise SourceError("Active status change has no explicit affected components")
        for item in affected:
            if not isinstance(item, dict) or not item.get("component_id") or not item.get("status"):
                raise SourceError("Invalid Flashduty current component impact")
            if item["component_id"] in by_id:
                # Keep one current component entry; a second active incident
                # affecting the same component may increase its severity.
                existing = by_id[item["component_id"]]
                rank = {"operational": 0, "unknown": 1, "maintenance": 2,
                        "degraded": 3, "downtime": 4}
                if rank[state_of(item["status"])] > rank[state_of(existing["status"])]:
                    existing["status"] = item["status"]
    return _rollup(selected, row["scope"])


ADAPTERS = {"statuspage": _statuspage, "instatus": _statuspage,
            "incident_proxy": _incident_proxy, "betterstack": _betterstack,
            "datadog": _datadog, "deepinfra": _deepinfra, "aggregate": _aggregate,
            "empiriolabs": _empiriolabs, "flashduty": _flashduty}


def parse_source(data, row) -> tuple[str, str]:
    adapter = ADAPTERS.get(row.get("adapter"))
    if adapter is None:
        raise SourceError("No verified current-state adapter")
    return adapter(data, row)


def collect_provider(row: dict, timeout: float = 12, fetcher=None) -> dict:
    result = {"id": row["id"], "name": row["name"], "state": "unknown",
              "observed_at": utc_now(),
              "source_url": row.get("status_url") or row.get("url") or row.get("official_url"),
              "detail": row.get("unavailable_reason", "No verified current-state source")}
    if row.get("adapter") not in ADAPTERS:
        return result
    try:
        data = (fetcher or fetch_json)(row["url"], timeout=timeout)
        state, detail = parse_source(data, row)
        result.update(state=state, detail=detail[:400], observed_at=utc_now())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
            ValueError, KeyError, TypeError, AttributeError) as error:
        # Never use stale cached green states or a page aggregate as a fallback.
        if isinstance(error, urllib.error.HTTPError):
            detail = f"Official status source returned HTTP {error.code}"
        elif isinstance(error, SourceError):
            detail = str(error)
        elif isinstance(error, (urllib.error.URLError, TimeoutError, OSError)):
            detail = "Official status source unavailable or timed out"
        else:
            detail = "Official status source schema could not be parsed"
        result.update(detail=detail, observed_at=utc_now())
    return result


def collect_providers(config=None, timeout: float = 12, workers: int = 8) -> list[dict]:
    rows = load_config() if config is None else config
    if isinstance(rows, dict):
        rows = rows["providers"]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        return list(pool.map(lambda row: collect_provider(row, timeout), rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", help="Save normalized JSON (otherwise stdout)")
    parser.add_argument("--timeout", type=float, default=12)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    results = collect_providers(load_config(args.config), args.timeout, args.workers)
    output = json.dumps(results, indent=2) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(output)
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
