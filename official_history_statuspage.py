"""Export provider-published daily history with explicit component coverage.

Read-only public HTTPS requests. Empty incidents never establish healthy days.
Statuspage showcase timelines require a startDate for every selected component;
Incident.io impact timelines require component_uptimes/data_available_since.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
import time as time_module
import urllib.error
import urllib.parse
import urllib.request

from providers import _selected, load_config, state_of


UTC = timezone.utc
SUPPORTED = {"statuspage", "incident_proxy"}
MAX_BYTES = 8 * 1024 * 1024


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing timezone-aware timestamp")
    # Python 3.9 accepts only 3/6 fractional digits although RFC3339 permits
    # arbitrary precision; official impact timestamps often use two digits.
    normalized = re.sub(r"\.(\d+)(?=Z|[+-]\d{2}:\d{2}$)",
                        lambda match: "." + match.group(1).ljust(6, "0")[:6], value)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Missing timestamp timezone")
    return parsed.astimezone(UTC)


def fetch_public(url, attempts=3):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("History sources must be public HTTPS URLs")
    request = urllib.request.Request(url, headers={
        "User-Agent": "RespanProviderStatus/1.0 (+https://respan.ai)",
        "Accept": "application/json,text/html", "Cache-Control": "no-cache",
    })
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                body = response.read(MAX_BYTES + 1)
            break
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt + 1 == attempts:
                raise
        time_module.sleep(0.25 * (attempt + 1))
    if len(body) > MAX_BYTES:
        raise ValueError("History response exceeds size limit")
    text = body.decode("utf-8")
    return json.loads(text) if text.lstrip().startswith(("{", "[")) else text


def window(now):
    today = now.astimezone(UTC).date()
    return [today - timedelta(days=89 - index) for index in range(90)]


def selected_components(summary, row):
    if row["adapter"] != "incident_proxy":
        return [component for component in _selected(summary.get("components"), row)
                if component.get("group") is not True]
    selected = []
    matched = set()
    for item in summary.get("structure", {}).get("items", []):
        group = item.get("group") or {}
        if (not group.get("hidden") and row.get("group_pattern")
                and re.search(row["group_pattern"], group.get("name", ""), re.I)):
            matched.add(group["name"].casefold())
            selected.extend({"id": c["component_id"], "name": c["name"]}
                            for c in group.get("components", []) if not c.get("hidden"))
    required = {name.casefold() for name in row.get("required_groups", [])}
    known = {c["id"] for c in summary.get("components", [])}
    if not selected or required - matched or any(c["id"] not in known for c in selected):
        raise ValueError("The configured API scope is incomplete")
    return selected


def daily_state(states):
    # Separate per-component daily totals do not prove simultaneous downtime
    # across an entire multi-component provider scope.
    if len(states) == 1 and states[0] == "downtime":
        return "downtime"
    if any(state in {"downtime", "degraded"} for state in states):
        return "degraded"
    if "maintenance" in states:
        return "maintenance"
    if "unknown" in states:
        return "unknown"
    return "operational"


def history_day(day, state, source_url, captured_at):
    return {"date": str(day), "state": state, "uptime": None,
            "provenance": "official-history", "source_url": source_url,
            "captured_at": captured_at}


def parse_showcase(payload, component_ids, now, source_url):
    """Retain known issues from partial scope; healthy requires every component."""
    timelines = payload.get("timelines") if isinstance(payload, dict) else None
    if not isinstance(timelines, dict) or not component_ids:
        raise ValueError("Missing official daily component timelines")
    by_component = {}
    for component_id in component_ids:
        timeline = timelines.get(component_id)
        if timeline is None:
            by_component[component_id] = {}
            continue
        if not isinstance(timeline, dict) or timeline.get("component", {}).get("code") != component_id:
            raise ValueError("Invalid selected API component uptime timeline")
        start = date.fromisoformat(timeline["component"]["startDate"])
        days = {}
        for entry in timeline.get("days", []):
            day = date.fromisoformat(entry["date"])
            if str(day) != entry["date"] or day in days:
                raise ValueError("Invalid or duplicate historical date")
            outages = entry.get("outages")
            if not isinstance(outages, dict) or set(outages) - {"p", "m"}:
                raise ValueError("Unsupported official outage totals")
            if any(type(seconds) not in {int, float} or not 0 <= seconds <= 86400
                   for seconds in outages.values()):
                raise ValueError("Invalid official outage duration")
            if day < start and not any(outages.values()):
                continue  # The publisher renders these padded days as NO DATA.
            days[day] = "downtime" if outages.get("m", 0) else "degraded" if outages.get("p", 0) else "operational"
        by_component[component_id] = days
    captured = now.astimezone(UTC).isoformat()
    return [history_day(day, daily_state([by_component[c].get(day, "unknown") for c in component_ids]), source_url, captured)
            for day in window(now) if any(day in by_component[c] for c in component_ids)]


def parse_component_impacts(payload, component_ids, now, source_url):
    """The requested bounded native history plus explicit per-component coverage."""
    if (not isinstance(payload, dict) or not isinstance(payload.get("component_impacts"), list)
            or not isinstance(payload.get("component_uptimes"), list) or not component_ids):
        raise ValueError("Missing native history and coverage contract")
    coverage = {}
    for item in payload["component_uptimes"]:
        component_id = item.get("component_id")
        if component_id not in component_ids:
            continue
        if component_id in coverage:
            raise ValueError("Duplicate component coverage")
        percentage = float(item["uptime"])
        if not 0 <= percentage <= 100:
            raise ValueError("Invalid published uptime")
        coverage[component_id] = stamp(item["data_available_since"])
    impacts = {component_id: [] for component_id in component_ids}
    for item in payload["component_impacts"]:
        component_id = item.get("component_id")
        if component_id not in impacts:
            continue
        start = stamp(item["start_at"])
        end = stamp(item["end_at"]) if item.get("end_at") else now
        if start >= now:
            continue
        state = state_of(item.get("status"))
        if state == "unknown" or end < start:
            raise ValueError("Invalid official historical component impact")
        impacts[component_id].append((start, min(end, now), state))
    result = []
    captured = now.astimezone(UTC).isoformat()
    rank = {"operational": 0, "maintenance": 1, "degraded": 2, "downtime": 3}
    for day in window(now):
        start = datetime.combine(day, time.min, UTC)
        end = min(start + timedelta(days=1), now)
        states = []
        any_record = False
        for component_id in component_ids:
            values = [state for a, b, state in impacts[component_id] if a < end and b > start]
            covered = component_id in coverage and coverage[component_id] <= start
            any_record = any_record or bool(values) or covered
            states.append(max(values, key=rank.get) if values else "operational" if covered else "unknown")
        if any_record:
            result.append(history_day(day, daily_state(states), source_url, captured))
    return result


def native_history_url(row, now):
    parsed = urllib.parse.urlparse(row["url"])
    if "/proxy/" in parsed.path:
        base = row["url"].rstrip("/")
    elif parsed.hostname == "statuspage.incident.io":
        key = parsed.path.strip("/").split("/")[0]
        base = f"https://{parsed.netloc}/proxy/{key}"
    else:
        base = f"https://{parsed.netloc}/proxy/{parsed.hostname}"
    days = window(now)
    query = urllib.parse.urlencode({"start_at": str(days[0]) + "T00:00:00Z",
                                   "end_at": str(days[-1] + timedelta(days=1)) + "T00:00:00Z"})
    return base + "/component_impacts?" + query


def parse_positive_incident_days(payload, component_ids, now, source_url):
    """Only explicitly affected component dates; never healthy incident-free days.

    Incident lifecycle duration is not assumed to be uninterrupted service impact.
    Starts and component status-change updates provide positive dated evidence.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("incidents"), list):
        raise ValueError("Missing official incident list")
    selected = set(component_ids)
    dates = set()
    for incident in payload["incidents"]:
        affected = {c.get("id") for c in incident.get("components", [])}
        if not affected & selected or incident.get("impact") == "none":
            continue
        started = stamp(incident.get("started_at") or incident.get("created_at"))
        if started <= now:
            dates.add(started.date())
        for update in incident.get("incident_updates", []):
            changes = update.get("affected_components") or []
            if any(change.get("code") in selected
                   and state_of(change.get("new_status")) in {"downtime", "degraded", "maintenance"}
                   for change in changes):
                published = stamp(update.get("display_at") or update.get("created_at"))
                if published <= now:
                    dates.add(published.date())
    captured = now.astimezone(UTC).isoformat()
    return [history_day(day, "degraded", source_url, captured) for day in window(now) if day in dates]


def extract_provider(row, fetcher=fetch_public, now=None):
    now = (now or datetime.now(UTC)).astimezone(UTC)
    result = {"id": row["id"], "history": [], "source_urls": [row["status_url"]],
              "captured_at": now.isoformat(), "complete_90_days": False,
              "history_scope": row.get("scope"),
              "coverage_policy": "Known issues retained from any selected component; healthy requires every selected component"}
    try:
        raw = fetcher(row["url"])
        summary = raw.get("summary", raw)
        components = selected_components(summary, row)
        ids = list(dict.fromkeys(c["id"] for c in components))
        result["selected_components"] = [{"id": c["id"], "name": c["name"]} for c in components]
        if row["adapter"] == "incident_proxy":
            native = True
        else:
            html = fetcher(row["status_url"])
            native = isinstance(html, str) and "self.__next_f.push" in html and "component_impacts" in html
        if native:
            url = native_history_url(row, now)
            result["source_urls"].append(url)
            result["history"] = parse_component_impacts(fetcher(url), ids, now, url)
            result["basis"] = "Official component impacts with explicit data_available_since"
        else:
            url = urllib.parse.urljoin(row["url"], "/uptime_showcase?" + urllib.parse.urlencode({"components": ",".join(ids)}))
            result["source_urls"].append(url)
            try:
                payload = fetcher(url)
                result["history"] = parse_showcase(payload, ids, now, url)
                result["missing_timeline_component_ids"] = [component_id for component_id in ids
                                                             if component_id not in payload.get("timelines", {})]
            except urllib.error.HTTPError as error:
                if row["id"] != "replicate" or error.code != 404:
                    raise
                # One bounded official incident page, filtered by the exact
                # Replicate component. Never crawl unrelated Cloudflare history.
                url = urllib.parse.urljoin(row["url"], "/api/v2/incidents.json?page=1")
                result["source_urls"].append(url)
                payload = fetcher(url)
                result["history"] = parse_positive_incident_days(payload, ids, now, url)
                result["incident_records_checked"] = len(payload.get("incidents", []))
                result["basis"] = "Confirmed component-specific incident dates only; other dates remain unknown"
            result.setdefault("basis", "Official Statuspage daily outage totals; ignores degraded performance and maintenance")
        result["complete_90_days"] = (len(result["history"]) == 90
                                      and all(day["state"] != "unknown" for day in result["history"]))
        result["covered_days"] = sum(day["state"] != "unknown" for day in result["history"])
        if not result["complete_90_days"]:
            result["reason"] = (f"{result['covered_days']} known UTC days; remaining dates lack complete "
                                "selected-scope coverage. Known issues from covered components are retained.")
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
        result["reason"] = str(error)[:300]
    return result


def save_archive(archive, target):
    """Retain reachable historical records when a follow-up request fails empty."""
    target = Path(target)
    if target.exists():
        previous = json.loads(target.read_text())
        for provider_id, row in previous.get("providers", {}).items():
            current = archive["providers"].get(provider_id)
            if current is None:
                archive["providers"][provider_id] = row
            elif not current.get("history") and current.get("reason") and row.get("history"):
                retained = dict(row)
                retained["last_attempt"] = {"captured_at": current["captured_at"], "error": current["reason"]}
                archive["providers"][provider_id] = retained
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(archive, indent=2) + "\n")
    temporary.replace(target)


def export_archive(config=None, fetcher=fetch_public, now=None):
    now = now or datetime.now(UTC)
    rows = [row for row in (load_config() if config is None else config) if row.get("adapter") in SUPPORTED
            and row.get("verified_on") and row.get("status_url")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda row: extract_provider(row, fetcher, now), rows))
    return {"schema_version": 1, "captured_at": now.astimezone(UTC).isoformat(),
            "providers": {result["id"]: result for result in results}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--providers", nargs="+", help="Limit read-only research to specified configured IDs")
    args = parser.parse_args()
    config = load_config()
    if args.providers:
        unknown = set(args.providers) - {row["id"] for row in config}
        if unknown:
            raise ValueError("Unknown provider IDs: " + ", ".join(sorted(unknown)))
        config = [row for row in config if row["id"] in args.providers]
    archive = export_archive(config)
    target = Path(args.output)
    save_archive(archive, target)
    complete = [key for key, row in archive["providers"].items() if row["complete_90_days"]]
    print(f"Official history exported for {len(archive['providers'])} providers; full 90 days: {', '.join(complete)}")


if __name__ == "__main__":
    main()
