"""Export reviewed provider-published daily history for a dated landing archive.

This manual, read-only command does not replace the current-status collector.
Review component scope, coverage, and source continuity before importing output.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path

from providers import fetch_json, load_config
from snapshot import publishable_provider
from official_history_statuspage import SUPPORTED, extract_provider, fetch_public
from official_history_betterstack import extract_betterstack_history, extract_friendli_history
from official_history_custom import extract_deepinfra_history, extract_empiriolabs_history


def export_provider(row, captured):
    stamp = captured.isoformat(timespec="seconds")
    try:
        if row["adapter"] in SUPPORTED:
            result = extract_provider(row, now=captured)
        elif row["adapter"] == "betterstack":
            result = extract_betterstack_history(fetch_json(row["url"]), row, stamp)
        elif row["id"] == "friendliai":
            result = extract_friendli_history(fetch_public(row["status_url"]), row, stamp)
        elif row["id"] == "deepinfra":
            result = extract_deepinfra_history(fetch_public(row["status_url"]), row, stamp)
        elif row["id"] == "empiriolabs":
            result = extract_empiriolabs_history(fetch_public(row["status_url"]), row, stamp)
        else:
            return {"id": row["id"], "history": [], "reason": "No reviewed daily history adapter"}
        return {**result, "name": row["name"], "source_url": row["status_url"],
                "scope": row["scope"], "captured_at": stamp}
    except (ValueError, TypeError, KeyError, OSError):
        return {"id": row["id"], "history": [], "reason": "Official history unavailable or schema not verified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    captured = datetime.now(timezone.utc)
    rows = [row for row in load_config() if publishable_provider(row, captured)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda row: export_provider(row, captured), rows))
    archive = {"schema_version": 1, "source_kind": "official_provider_published",
               "captured_at": captured.isoformat(timespec="seconds"), "providers": results}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(archive, indent=2) + "\n")
    complete = [row["id"] for row in results if len(row["history"]) == 90
                and all(day["state"] != "unknown" for day in row["history"])]
    print(f"Exported {len(results)} reviewed providers; {len(complete)} have 90 known daily records")
    print("Review coverage and component-ID continuity before importing this dated archive.")


if __name__ == "__main__":
    main()
