# Official provider status feed

Read official provider status feeds and publish a public JSON snapshot for
`https://www.respan.ai/llm-status`. Python's standard library is the only runtime
dependency. No Supabase, database, inference calls, or paid monitoring service is
required. Better Stack synchronization remains optional during the transition.

## Data flow

1. A public GitHub Actions workflow runs about every five minutes.
2. `providers.py` fetches current status from official JSON endpoints, selecting
   API/model components where a provider also reports consumer applications.
3. `snapshot.py` builds `public-status.json`, preserving current unknowns, source
   links, expiry times and bounded daily observations from the previous snapshot.
4. `persist_state.py` publishes this file to the public repository's `state`
   branch before any optional Better Stack work. Source failures are published
   as unknown and then fail the availability check for enabled adapters.
5. When enabled, `betterstack.py` also reconciles its managed Providers rows and
   preserves separate reconciliation state. Subscriber notifications are disabled.
   There are no Actions artifact or cache uploads.

## Public snapshot

After the first publication, the landing page can fetch without credentials:

<https://raw.githubusercontent.com/respanai/provider-status/state/public-status.json>

The snapshot has `schema_version: 1`, `generated_at`, `expires_at`,
`stale_after_seconds: 900`, and a `providers` array.

Publication includes only verified official live feeds: a reviewed public HTTPS
`status_url`, a supported current-state adapter with a public HTTPS endpoint,
a valid `verified_on` review date, and an explicit reviewed service `scope`.
The current 52-provider inventory contains 29 eligible feeds. Official websites
without a usable current feed and providers with no verified official status
resource stay in the audit inventory but are omitted from this public snapshot,
including any entries previously published. A temporary fetch or parsing failure
keeps an eligible provider in the snapshot as `unknown`. Its source link always
remains the configured official status page; a company homepage is never used
as a replacement status source.

Each provider contains:

```json
{
  "id": "example",
  "name": "Example API",
  "state": "unknown",
  "enabled": true,
  "observed_at": "2026-10-16T12:00:00+00:00",
  "expires_at": "2026-10-16T12:15:00+00:00",
  "source_url": "https://status.example.com/",
  "detail": "Official status source unavailable or timed out",
  "last_confirmed_state": "operational",
  "last_confirmed_at": "2026-10-16T11:55:00+00:00",
  "history": [{"date": "2026-10-16", "state": "unknown", "uptime": null}]
}
```

Known states are `operational`, `degraded`, `downtime` and `maintenance`. A failed
eligible source is `unknown`; unsupported sources are not published. An already
expired known observation is
`stale`. The consumer must also compare its current time with both provider and
snapshot `expires_at`: a known badge becomes stale when either expires, even if
the scheduled collector stops. Unknown remains unknown. Last-confirmed fields
provide context and must never replace an unknown or stale badge with green.
Every published provider has `enabled: true`, even during a temporary source failure.

History contains the worst status observed on each UTC day in the last 90 calendar
days. Unobserved days remain absent, and every `uptime` is null. Unknown observations
cannot erase a degradation or downtime already observed that day; recovery also
retains the day's observed degradation. An unknown observation changes a previously
healthy daily entry to unknown. These are daily status observations, not continuous
uptime measurements, incident onset times, or a backfilled uptime record. The landing
page may retain its 90-day bar layout by rendering absent days as unobserved.

## Stop Better Stack writes after landing launch

Set the GitHub Actions repository variable `BETTERSTACK_SYNC_ENABLED` to `false`
after the landing page is verified against the published snapshot. This skips
Better Stack configuration validation and synchronization while preserving the
same five-minute collection, public snapshot publication and availability checks.
No Better Stack token is required in this mode. Existing Better Stack resources
and reconciliation state are retained; this switch does not delete or alter them.
Unset or any value other than the exact string `false` preserves the existing
Better Stack integration during the transition.

The current theme, section layout, status badges, and responsive behavior remain
under Better Stack's control. Providers initially use the native **Current status
only** widget: Better Stack pads new manual history rows with green days before
tracking began, which would imply an unverified 90-day uptime record. Native
reports still retain real observations for a future history display. Existing
Platform and Enterprise resources are not managed by this integration.

## Free deployment

Use a dedicated **public** GitHub repository and the included standard
`ubuntu-24.04` runner. Standard hosted runners are free for public repositories:
<https://docs.github.com/en/billing/concepts/product-billing/github-actions>.
The workflow refuses private repositories and does not use larger runners.

GitHub scheduled workflows are best effort. Five minutes is the minimum interval;
runs can be delayed or dropped. Public workflows may be disabled after 60 days
without repository activity. This is near-real-time polling, not a guaranteed
five-minute delivery service:
<https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule>.

Before enabling the workflow:

1. Publish this collector to the reviewed public repository.
2. Initialize a `state` branch containing `state.json` copied from
   `state.example.json` (update its page ID when targeting another page).
3. For the public landing feed alone, set `BETTERSTACK_SYNC_ENABLED=false`.
   The remaining Better Stack configuration is required only when that integration
   is enabled. Add the numeric status page ID as the Actions variable
   `BETTERSTACK_STATUS_PAGE_ID` (Respan's existing page: `176303`).
4. Store a Respan-team Better Stack Incidents API token, preferably dedicated, as the encrypted
   Actions secret `BETTERSTACK_API_TOKEN`. Do not commit it or put it in a public
   repository variable. Token creation and storage require account-owner approval.
5. Run **Sync official provider status** manually, verify the published page, then
   confirm a scheduled run succeeds.

The Better Stack token is team-scoped; Better Stack does not document a
status-page-only token scope. The client restricts mutations to its own rows in the
configured page's Providers section. The GitHub workflow needs `contents: write`
solely to preserve reconciliation state in the `state` branch. It does not run on
pull requests or expose secrets to forked pull requests.

## Local checks

```sh
python3 -m unittest discover -s tests -v
python3 providers.py --output observations.json
python3 snapshot.py --input observations.json --previous public-status.json --output public-status.json
python3 providers.py --check observations.json
python3 betterstack.py --input observations.json --state state.json --page-id 176303 --offline
```

The final command is a dry run by default. Without a token it uses an empty
offline account snapshot; account conflicts and existing rows must be reconciled
against Better Stack before applying. Live writes require `--apply` and the token
in the environment:

```sh
python3 betterstack.py --input observations.json --state state.json --page-id 176303 --apply
```

## Status conventions

- `operational`, `degraded`, and `downtime` come from successfully parsed current
  provider status. Only relevant API components contribute to the result.
- Unsupported formats, failed requests, unrecognized schemas, and paused source
  monitors produce `unknown`. Empty RSS feeds do not establish current health.
- Unknown observations do not create rows or resolve incidents. Existing managed
  rows display **(status unavailable)** in their name, retain their last confirmed
  badge, and explain the last successful observation. Better Stack manual resources
  have no writable Unknown badge. The name returns to normal after a verified feed
  recovery. Unexpected failures of enabled feeds fail the workflow after the other
  providers have synchronized and reconciliation state has been preserved.
- Native maintenance reports require authoritative start and end times. The
  integration skips maintenance observations without those times.
- History begins when valid observations are synchronized. The collector does not
  invent previous uptime or import unsupported historical estimates.
- The public snapshot includes provider observations, source URLs and daily status
  observations only. Separate reconciliation state may include Better Stack
  resource/report IDs. Credentials and request headers must never appear in either.

The source registry retains unsupported providers for internal coverage review;
they are not part of the public status snapshot. A provider with an official
status website may still need a current-state adapter; a published incident RSS
feed alone is insufficient for publication.

## Operational recovery

The workflow has one concurrency group to prevent overlapping writers. Ambiguous
POST results are reconciled by reading Better Stack before another write is
attempted; unresolved pending operations remain blocked rather than blindly
creating duplicate reports. Preserve the `state` branch when rerunning a failed
job. Investigate a failed sync before clearing pending operations manually.

Inspect GitHub Actions failures and the public status page after changing feed
adapters. Keep the collector on the default branch for scheduled execution. If
GitHub disables its schedule, re-enable it after reviewing the integration.
