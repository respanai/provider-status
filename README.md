# Official provider status on Better Stack

Read official provider status feeds and synchronize native rows in the existing
Better Stack **Providers** section. Python's standard library is the only runtime
dependency. No Supabase, database, inference calls, or paid monitoring service is
required.

## Data flow

1. A public GitHub Actions workflow runs about every five minutes.
2. `providers.py` fetches current status from official JSON endpoints, selecting
   API/model components where a provider also reports consumer applications.
3. `betterstack.py` creates or reconciles only rows managed by this integration
   in the Providers section of the configured status page.
4. Degradation and recovery become native Better Stack status reports and
   updates. Subscriber notifications are explicitly disabled.
5. Non-secret reconciliation state is committed to a separate `state` branch.
   There are no Actions artifact or cache uploads.

The current theme, section layout, history widget, and responsive behavior remain
under Better Stack's control. Existing Platform and Enterprise resources are not
managed by this integration.

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
3. Add the numeric status page ID as the Actions variable
   `BETTERSTACK_STATUS_PAGE_ID` (Respan's existing page: `176303`).
4. Store a dedicated Respan-team Better Stack Incidents API token as the encrypted
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
  rows retain their last confirmed state and an explanation of the unavailable
  observation. Better Stack manual resources have no writable Unknown badge.
- Native maintenance reports require authoritative start and end times. The
  integration skips maintenance observations without those times.
- History begins when valid observations are synchronized. The collector does not
  invent previous uptime or import unsupported historical estimates.
- Public state includes provider observations and Better Stack resource/report
  IDs only. Credentials and request headers must never appear there.

The source registry retains unsupported providers so gaps are explicit. A provider
with an official status website may still need a current-state adapter; a published
incident RSS feed alone is insufficient.

## Operational recovery

The workflow has one concurrency group to prevent overlapping writers. Ambiguous
POST results are reconciled by reading Better Stack before another write is
attempted; unresolved pending operations remain blocked rather than blindly
creating duplicate reports. Preserve the `state` branch when rerunning a failed
job. Investigate a failed sync before clearing pending operations manually.

Inspect GitHub Actions failures and the public status page after changing feed
adapters. Keep the collector on the default branch for scheduled execution. If
GitHub disables its schedule, re-enable it after reviewing the integration.
