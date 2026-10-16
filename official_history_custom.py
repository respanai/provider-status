"""Export explicitly published daily graphs from official provider HTML.

Read-only archive tooling; it never infers healthy days from incident absence.
The archived graph does not supply a current status or estimated uptime.
"""
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import json


class PublishedHtml(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.script = None
        self.graph = None
        self.depth = 0
        self.graphs = {}

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        if tag == "script":
            self.script = ""
        if tag == "div":
            if self.graph is not None:
                self.depth += 1
            elif attrs.get("aria-label", "").startswith("90 day uptime history for "):
                self.graph = attrs["aria-label"].removeprefix("90 day uptime history for ")
                if self.graph in self.graphs:
                    raise ValueError("Duplicate published component graph")
                self.graphs[self.graph] = []
                self.depth = 1
        if tag == "span" and self.graph is not None and "data-d" in attrs:
            self.graphs[self.graph].append(attrs)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append(self.script)
            self.script = None
        if tag == "div" and self.graph is not None:
            self.depth -= 1
            if not self.depth:
                self.graph = None

    def handle_data(self, value):
        if self.script is not None:
            self.script += value


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def graph_days(entries, captured_at, state):
    captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    if captured.tzinfo is None:
        raise ValueError("Capture time must have a timezone")
    today = captured.astimezone(timezone.utc).date()
    days = []
    seen = set()
    for entry in entries:
        key = entry.get("date", entry.get("data-d", ""))
        parsed = date.fromisoformat(key)
        if str(parsed) != key or parsed > today or key in seen:
            raise ValueError("Invalid or duplicate published date")
        seen.add(key)
        days.append({"date": key, "state": state(entry), "uptime": None,
                     "provenance": "official-history", "captured_at": captured_at})
    if not 1 <= len(days) <= 90:
        raise ValueError("Unexpected published graph window")
    return sorted(days, key=lambda entry: entry["date"])


def archive(row, history, captured_at):
    for day in history:
        day["source_url"] = row["status_url"]
    return {"id": row["id"], "name": row["name"], "source_url": row["status_url"],
            "captured_at": captured_at, "scope": row["scope"], "source_timezone": "UTC",
            "history": history}


def extract_deepinfra_history(html, row, captured_at):
    parser = PublishedHtml()
    parser.feed(html)
    payload = ""
    prefix = "self.__next_f.push("
    for script in parser.scripts:
        script = script.strip().removesuffix(";")
        if script.startswith(prefix) and script.endswith(")"):
            # Parse literal JSON only; never evaluate provider JavaScript.
            value = json.loads(script[len(prefix):-1])
            if isinstance(value, list):
                payload += "".join(item for item in value if isinstance(item, str))
    cards = []
    for line in payload.splitlines():
        try:
            value = json.loads(line.split(":", 1)[1])
        except (ValueError, IndexError):
            continue
        for item in walk(value):
            if isinstance(item, list) and len(item) == 4 and item[:3] == ["$", "li", "api"]:
                cards.append(item)
    if len(cards) != 1 or "api.deepinfra.com" not in list(walk(cards[0])):
        raise ValueError("Official API graph identity is missing or ambiguous")
    graphs = [item for item in walk(cards[0]) if isinstance(item, dict) and "windowDays" in item]
    if len(graphs) != 1 or graphs[0].get("windowDays") != 90 or not isinstance(graphs[0].get("days"), list):
        raise ValueError("Official API graph window is missing or ambiguous")

    def state(day):
        downtime, maintenance = day.get("downtimeMs"), day.get("maintenanceMs")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
               for value in (downtime, maintenance)):
            raise ValueError("Invalid published daily durations")
        supplied = day.get("status")
        if supplied == "NO_DATA":
            return "unknown"
        if downtime > 0:
            return "degraded"
        if maintenance > 0:
            return "maintenance"
        return {"OPERATIONAL": "operational", "DEGRADED": "degraded",
                "OUTAGE": "downtime", "MAINTENANCE": "maintenance"}.get(supplied, "unknown")

    return archive(row, graph_days(graphs[0]["days"], captured_at, state), captured_at)


def extract_empiriolabs_history(html, row, captured_at):
    parser = PublishedHtml()
    parser.feed(html)
    names = row.get("names")
    if not names or any(name not in parser.graphs for name in names):
        raise ValueError("Official API/Models graphs are missing")

    def state(day):
        checks, failures = int(day.get("data-c", "-1")), int(day.get("data-x", "-1"))
        if checks < 0 or not 0 <= failures <= checks:
            raise ValueError("Invalid published daily check counts")
        if checks == 0 or day.get("data-s") == "none":
            return "unknown"
        if failures > 0 or day.get("data-s") == "down":
            return "degraded"
        return "operational" if day.get("data-s") == "up" else "unknown"

    components = [graph_days(parser.graphs[name], captured_at, state) for name in names]
    dates = [{day["date"] for day in component} for component in components]
    if any(dates[0] != days for days in dates[1:]):
        raise ValueError("Official component graph dates do not match")
    maps = [{day["date"]: day for day in component} for component in components]
    history = []
    for key in sorted(dates[0]):
        states = {component[key]["state"] for component in maps}
        rolled = "degraded" if "degraded" in states else "unknown" if "unknown" in states else "operational"
        history.append({**maps[0][key], "state": rolled})
    return archive(row, history, captured_at)
