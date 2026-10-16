"""Build the public landing-page snapshot from official provider observations.

History records the worst status observed on each UTC day. It is not a continuous
uptime measurement. Missing days remain missing and uptime is always null.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlparse

from providers import DEFAULT_CONFIG, load_config


KNOWN_STATES = {"operational", "degraded", "downtime", "maintenance"}
OBSERVED_STATES = KNOWN_STATES | {"unknown"}
HISTORY_RANK = {"operational": 0, "unknown": 1, "maintenance": 2,
                "degraded": 3, "downtime": 4}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def public_url(row, observation):
    value = (row.get("status_url") or observation.get("source_url")
             or row.get("official_url") or row.get("url"))
    parsed = urlparse(value or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        return None
    return value


def history_for(prior, observation, observed, now):
    today = now.date()
    cutoff = today - timedelta(days=89)
    daily = {}
    for entry in prior.get("history", []):
        if not isinstance(entry, dict) or entry.get("state") not in OBSERVED_STATES:
            continue
        try:
            day = date.fromisoformat(entry.get("date", ""))
        except (TypeError, ValueError):
            continue
        if str(day) != entry["date"] or not cutoff <= day <= today:
            continue
        previous = daily.get(day, "operational")
        daily[day] = max(previous, entry["state"], key=HISTORY_RANK.get)
    if observed and observed <= now + timedelta(seconds=120):
        day = observed.date()
        state = observation.get("state")
        if cutoff <= day <= today and state in OBSERVED_STATES:
            previous = daily.get(day, "operational")
            daily[day] = max(previous, state, key=HISTORY_RANK.get)
    return [{"date": str(day), "state": daily[day], "uptime": None}
            for day in sorted(daily)]


def effective_state(provider, snapshot, now=None):
    """Consumer contract: known badges expire even when scheduled polling stops."""
    now = now or datetime.now(timezone.utc)
    state = provider.get("state")
    if state == "unknown":
        return "unknown"
    if state == "stale":
        return "stale"
    if state not in KNOWN_STATES:
        return "unknown"
    provider_expiry = timestamp(provider.get("expires_at"))
    snapshot_expiry = timestamp(snapshot.get("expires_at"))
    if not provider_expiry or not snapshot_expiry or now >= min(provider_expiry, snapshot_expiry):
        return "stale"
    return state


def build_snapshot(config, observations, previous=None, now=None, stale_after_seconds=900):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Snapshot generation time must include a timezone")
    now = now.astimezone(timezone.utc)
    if not isinstance(stale_after_seconds, int) or stale_after_seconds <= 0:
        raise ValueError("Snapshot expiry must be a positive number of seconds")
    rows = config["providers"] if isinstance(config, dict) else config
    config_ids = [row["id"] for row in rows]
    if len(set(config_ids)) != len(config_ids):
        raise ValueError("Configured provider IDs must be unique")
    if not isinstance(observations, list) or not all(isinstance(x, dict) for x in observations):
        raise ValueError("Observations must be a list of provider objects")
    observations_by_id = {}
    for observation in observations:
        id = observation.get("id")
        if not isinstance(id, str) or id in observations_by_id or id not in config_ids:
            raise ValueError("Observation IDs must be configured and unique")
        observations_by_id[id] = observation
    if previous is not None and (not isinstance(previous, dict) or previous.get("schema_version") != 1):
        raise ValueError("Unsupported previous public snapshot")
    prior_by_id = {row["id"]: row for row in (previous or {}).get("providers", [])
                   if isinstance(row, dict) and isinstance(row.get("id"), str)}
    providers = []
    for row in rows:
        prior = prior_by_id.get(row["id"], {})
        observation = observations_by_id.get(row["id"], {})
        state = observation.get("state", "unknown")
        detail = observation.get("detail") or row.get("unavailable_reason") or "No observation was produced"
        if row.get("adapter") == "unavailable":
            state = "unknown"
            detail = row.get("unavailable_reason") or detail
            observation = {**observation, "state": "unknown"}
        observed = timestamp(observation.get("observed_at"))
        valid_time = observed and observed <= now + timedelta(seconds=120)
        if state not in OBSERVED_STATES:
            state, detail = "unknown", "Collector returned an invalid provider state"
        elif state in KNOWN_STATES and not valid_time:
            state, detail = "unknown", "Observation timestamp is missing, invalid or in the future"
        expires = observed + timedelta(seconds=stale_after_seconds) if valid_time else None
        last_confirmed_state = prior.get("last_confirmed_state")
        last_confirmed = timestamp(prior.get("last_confirmed_at"))
        if (last_confirmed_state not in KNOWN_STATES or not last_confirmed
                or last_confirmed > now + timedelta(seconds=120)):
            last_confirmed_state, last_confirmed = None, None
        if state in KNOWN_STATES and (last_confirmed is None or observed >= last_confirmed):
            last_confirmed_state, last_confirmed = state, observed
        if state in KNOWN_STATES and now >= expires:
            state = "stale"
        providers.append({
            "id": row["id"], "name": row["name"], "state": state,
            "enabled": row.get("adapter") != "unavailable",
            "observed_at": iso(observed) if valid_time else None,
            "expires_at": iso(expires) if expires else None,
            "source_url": public_url(row, observation), "detail": str(detail)[:400],
            "last_confirmed_state": last_confirmed_state,
            "last_confirmed_at": iso(last_confirmed) if last_confirmed else None,
            "history": history_for(prior, observation, observed if valid_time else None, now),
        })
    return {"schema_version": 1, "generated_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=stale_after_seconds)),
            "stale_after_seconds": stale_after_seconds, "providers": providers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Normalized observations JSON")
    parser.add_argument("--output", required=True, help="Public snapshot JSON destination")
    parser.add_argument("--previous", help="Previous public snapshot; missing file is allowed on first run")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--stale-after-seconds", type=int, default=900)
    args = parser.parse_args()
    prior_path = Path(args.previous) if args.previous else None
    previous = json.loads(prior_path.read_text()) if prior_path and prior_path.exists() else None
    snapshot = build_snapshot(load_config(args.config), json.loads(Path(args.input).read_text()),
                              previous, stale_after_seconds=args.stale_after_seconds)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2) + "\n")
    temporary.replace(destination)
    print(f"Public status snapshot built for {len(snapshot['providers'])} providers")


if __name__ == "__main__":
    main()
