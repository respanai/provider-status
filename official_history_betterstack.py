"""Extract provider-published daily history from reviewed public Better Stack feeds.

This module performs no requests or writes. A daily monitor outage is evidence of
some downtime that day, not evidence that all models failed simultaneously.
"""

from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import math
import re
from urllib.parse import urlparse


def _matches(value, pattern):
    return bool(pattern and re.search(pattern, value or "", re.IGNORECASE))


def _timestamp(value):
    value = datetime.now(timezone.utc) if value is None else value
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("captured_at must be a timezone-aware timestamp")
    return value.astimezone(timezone.utc)


def _source(row):
    source = row.get("url")
    parsed = urlparse(source or "")
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError("History requires a reviewed public HTTPS source")
    return source


def _resources(data, row):
    if (not isinstance(data, dict) or not isinstance(data.get("data"), dict)
            or data["data"].get("type") != "status_page"
            or not isinstance(data.get("included"), list)):
        raise ValueError("Invalid Better Stack public status response")
    sections, resources = {}, []
    ids = set()
    for item in data["included"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid included resource")
        if item.get("type") not in {"status_page_section", "status_page_resource"}:
            continue
        raw_id = item.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            raise ValueError("Missing status resource identity")
        key = (item["type"], str(raw_id))
        if not key[1] or key in ids or not isinstance(item.get("attributes"), dict):
            raise ValueError("Missing or duplicate status resource identity")
        ids.add(key)
        if item["type"] == "status_page_section":
            sections[key[1]] = item["attributes"].get("name", "")
        else:
            resources.append(item)
    names = {str(x).strip().casefold() for x in row.get("names", [])}
    section_pattern, name_pattern = row.get("section_pattern"), row.get("name_pattern")
    if not (names or section_pattern or name_pattern):
        raise ValueError("An explicit API history scope is required")
    selected, matched_names, section_found, name_found = [], set(), False, False
    for item in resources:
        attrs = item["attributes"]
        name = str(attrs.get("public_name", "")).strip()
        section = sections.get(str(attrs.get("status_page_section_id")), "")
        by_section = _matches(section, section_pattern)
        by_pattern = _matches(name, name_pattern)
        by_name = name.casefold() in names
        if by_section or by_pattern or by_name:
            selected.append(item)
            section_found |= by_section
            name_found |= by_pattern
            if by_name:
                matched_names.add(name.casefold())
    if (not selected or names - matched_names
            or section_pattern and not section_found
            or name_pattern and not name_found):
        raise ValueError("Configured API history scope is missing or incomplete")
    return selected


def _day_state(entry):
    status = entry.get("status")
    durations = [entry.get("downtime_duration"), entry.get("maintenance_duration")]
    if any(isinstance(x, bool) or not isinstance(x, (int, float))
           or not math.isfinite(x) or x < 0 for x in durations):
        return "unknown"
    downtime, maintenance = durations
    if status == "not_monitored":
        return "unknown"
    if status == "downtime":
        return "downtime"
    if status in {"degraded", "recovered"}:
        return "degraded"
    if status not in {"operational", "maintenance"}:
        return "unknown"
    if downtime > 0:
        return "degraded"
    if status == "maintenance" or maintenance > 0:
        return "maintenance"
    return "operational"


def extract_betterstack_history(data, row, captured_at=None):
    """Return at most 90 dated rows; every scoped resource must cover a green day.

    Missing dates and not_monitored remain unknown. Positive outage evidence can
    still establish degradation when another selected resource is unknown.
    Source calendar dates are retained with source_timezone, without UTC relabeling.
    """
    captured = _timestamp(captured_at)
    captured_iso = captured.isoformat(timespec="seconds")
    source = _source(row)
    selected = _resources(data, row)
    cutoff, today = captured.date() - timedelta(days=89), captured.date()
    timelines, dates = [], set()
    for item in selected:
        history = item["attributes"].get("status_history")
        if not isinstance(history, list):
            raise ValueError("Selected API resource has no explicit daily history")
        timeline = {}
        for entry in history:
            if not isinstance(entry, dict):
                raise ValueError("Invalid daily history record")
            try:
                day = date.fromisoformat(entry.get("day", ""))
            except (ValueError, TypeError):
                raise ValueError("Invalid history calendar date") from None
            if str(day) != entry["day"] or day in timeline:
                raise ValueError("Invalid or duplicate history date")
            timeline[day] = _day_state(entry)
            if cutoff <= day <= today:
                dates.add(day)
        timelines.append(timeline)
    page_attributes = data["data"].get("attributes", {})
    if not isinstance(page_attributes, dict):
        raise ValueError("Invalid status page attributes")
    source_timezone = page_attributes.get("timezone")
    metadata = {"provenance": "official-history", "source_url": source,
                "captured_at": captured_iso}
    if isinstance(source_timezone, str) and source_timezone:
        metadata["source_timezone"] = source_timezone
    history = []
    for day in sorted(dates):
        states = [timeline.get(day, "unknown") for timeline in timelines]
        if len(states) == 1 and states[0] == "downtime":
            state = "downtime"
        elif any(state in {"downtime", "degraded"} for state in states):
            state = "degraded"
        elif "unknown" in states:
            state = "unknown"
        elif "maintenance" in states:
            state = "maintenance"
        else:
            state = "operational"
        history.append({"date": str(day), "state": state, "uptime": None, **metadata})
    known = sum(day["state"] != "unknown" for day in history)
    return {"id": row["id"], "name": row.get("name", row["id"]),
            "scope": row.get("scope"), **metadata, "history": history,
            "coverage": {"selected_resources": len(selected), "published_days": len(history),
                         "known_days": known, "unknown_days": len(history) - known,
                         "complete_90_days": len(history) == known == 90}}


class _PublishedGroups(HTMLParser):
    """Collect explicit SVG bars inside each rendered Instatus uptime group."""
    def __init__(self):
        super().__init__()
        self.depth, self.current, self.groups = 0, None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.depth += 1
            classes = attrs.get("class", "").split()
            if "component-group" in classes and "group-uptime" in classes:
                if self.current is not None:
                    raise ValueError("Ambiguous nested uptime groups")
                self.current = {"depth": self.depth, "text": [], "bars": []}
        if tag == "rect" and self.current is not None:
            self.current["bars"].append(attrs)

    def handle_endtag(self, tag):
        if tag == "div":
            if self.current is not None and self.current["depth"] == self.depth:
                self.groups.append(self.current)
                self.current = None
            self.depth -= 1

    def handle_data(self, value):
        if self.current is not None and value.strip():
            self.current["text"].append(value.strip())


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def extract_friendli_history(html, row, captured_at=None):
    """Export explicit published bars, never expand an aggregate uptime percentage.

    Friendli's reviewed renderer maps bar i to startDate+i calendar days. Require
    both that bounded RSC payload and exactly one scoped leaf/complete bar series.
    A frontend/schema change fails closed instead of manufacturing history.
    """
    if not isinstance(html, str):
        raise ValueError("Expected official public page HTML")
    chunks = []
    for raw in re.findall(r"self\.__next_f\.push\((.*?)\)</script>", html, re.DOTALL):
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(record, list) and len(record) == 2 and record[0] == 1 and isinstance(record[1], str):
            chunks.append(record[1])
    candidates = []
    for line in "".join(chunks).splitlines():
        if "componentsUptime" not in line or ":" not in line:
            continue
        try:
            record = json.loads(line.split(":", 1)[1])
        except ValueError:
            continue
        candidates.extend(x for x in _walk(record)
                          if {"componentsUptime", "startDate", "endDate", "site"} <= x.keys())
    if len(candidates) != 1:
        raise ValueError("Missing or ambiguous published uptime window")
    payload = candidates[0]
    try:
        start = datetime.strptime(payload["startDate"].split(" (")[0], "%a %b %d %Y %H:%M:%S GMT%z")
        end = datetime.strptime(payload["endDate"].split(" (")[0], "%a %b %d %Y %H:%M:%S GMT%z")
        site = payload["site"]
        components = site["components"]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("Invalid published uptime bounds or component catalog") from None
    if (not isinstance(site, dict) or not isinstance(components, list)
            or not isinstance(payload["componentsUptime"], dict)):
        raise ValueError("Invalid published uptime schema")
    if (start.utcoffset() != timedelta(0) or end.utcoffset() != timedelta(0)
            or start.time().isoformat() != "00:00:00" or end.time().isoformat() != "23:59:59"):
        raise ValueError("Unverified uptime date mapping")
    label = lambda value: value.get("default", value.get("en", "")) if isinstance(value, dict) else value
    groups = [x for x in components if _matches(label(x.get("name")), row.get("group_pattern"))]
    if len(groups) != 1 or len(groups[0].get("children", [])) != 1:
        raise ValueError("Expected exactly one reviewed API group and leaf")
    component = groups[0]["children"][0]
    if (component.get("children") or not component.get("showUptime")
            or not isinstance(payload["componentsUptime"].get(component.get("id")), dict)):
        raise ValueError("Missing explicit API uptime publication")
    starts = []
    for value in [site.get("launchDate"), groups[0].get("startDate"), component.get("startDate")]:
        if value is not None:
            try:
                starts.append(_timestamp(value).date())
            except (ValueError, TypeError):
                raise ValueError("Invalid declared API coverage start") from None
    coverage_start = max(starts) if starts else None
    parser = _PublishedGroups()
    parser.feed(html)
    rendered = [x for x in parser.groups if label(groups[0]["name"]) in x["text"]]
    count = (end.date() - start.date()).days + 1
    if len(rendered) != 1 or not 1 <= count <= 366 or len(rendered[0]["bars"]) != count:
        raise ValueError("Rendered daily bar count does not match declared dates")
    colors = {}
    for key, state in [("okColor", "operational"), ("disruptedColor", "degraded"),
                       ("degradedColor", "degraded"), ("downColor", "downtime"),
                       ("noticeColor", "maintenance"), ("unknownColor", "unknown")]:
        color = site.get(key)
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Missing published bar color mapping")
        if color.casefold() in colors:
            raise ValueError("Ambiguous published bar color mapping")
        colors[color.casefold()] = state
    captured = _timestamp(captured_at)
    metadata = {"provenance": "official-history", "source_url": _source({"url": row.get("status_url")}),
                "captured_at": captured.isoformat(timespec="seconds"), "source_timezone": "UTC"}
    history = []
    for index, bar in enumerate(rendered[0]["bars"]):
        try:
            state = colors[bar["fill"].casefold()]
            position = float(bar["x"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Unknown published daily bar state") from None
        if not math.isfinite(position) or position != index * 11:
            raise ValueError("Unexpected published bar ordering")
        day = start.date() + timedelta(days=index)
        if coverage_start is not None and day < coverage_start:
            state = "unknown"
        if captured.date() - timedelta(days=89) <= day <= captured.date():
            history.append({"date": str(day), "state": state, "uptime": None, **metadata})
    known = sum(x["state"] != "unknown" for x in history)
    return {"id": row["id"], "name": row.get("name", row["id"]), "scope": row.get("scope"),
            **metadata, "history": history, "coverage": {"selected_resources": 1,
                "published_days": len(history), "known_days": known,
                "unknown_days": len(history) - known, "complete_90_days": len(history) == known == 90}}
