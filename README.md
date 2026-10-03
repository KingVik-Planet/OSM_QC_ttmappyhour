# OSM #ttmappyhour hourly quality check

This is a replica of the working `#tt_event` quality-check pipeline, built
from the most current, fully-verified version of every fix applied across
this project to date. Verified before delivery: every file compiles, and
the following are all confirmed present:

- All 17 check categories
- Overpass circuit breaker (trips after 1 failure, 45s timeout) with a
  300-item-per-run retry cap
- The changeset-discovery scan-retry queue (`pending_changeset_scans.json`),
  capped at 20 ranges per run, for when the OSM API itself (not Overpass)
  gets persistently stuck on a specific narrow time slice
- The `covered=yes` exception for node-connects-highway-and-building
- Live Overpass re-verification before confirming a "floating highway"
- Misattribution fix: node-connects-highway-and-building and
  broken-highway-continuity now require at least one side of any flagged
  pair to actually belong to the current changeset's own edit
- The `hashtag` column (always the last column) on every CSV row
- Crash-safe checkpointing and a clean exit on unexpected errors
- The `.github/workflows/hourly.yml` `if: always()` fix
- A `.gitignore` for `__pycache__`

Changed from the original in exactly two places:

1. **Hashtag**: watches for `#ttmappyhour` instead of `#tt_event`
   (`config.py` → `HASHTAG`).
2. **Start date**: `data/state.json` is pre-seeded with
   `"last_run_end_utc": "2026-07-01T00:00:00"`.

## Important: this will backfill slowly, on purpose

Every run processes **at most one hour** of data, once per hour. A
backfill from **July 1, 2026** to today will take roughly as many
real-world hours as there are hours in that gap before it catches up to
live, real-time monitoring -- weeks to months, not days. This is by
design (see `main.py`'s `determine_window()`).

## Setup

1. Push this repository to GitHub.
2. Confirm `.github/workflows/hourly.yml` is at the repo root under
   `.github/workflows/`.
3. In **Settings → Actions → General → Workflow permissions**, select
   **Read and write permissions**.
4. Optionally add repository secrets: `OSMCHA_TOKEN`, `SLACK_BOT_TOKEN`,
   `SLACK_CHANNEL_ID`.
5. Trigger the workflow once manually to confirm a clean first run.

## Output

Same 13-column CSV structure as every other repo in this family:
`s_no, error_type, username, user_id, osm_location_link, changeset_id,
changeset_link, osm_object_type, osm_object_id, time_utc, country, detail,
hashtag`

`hashtag` is always the last column, populated dynamically from
`config.HASHTAG` -- for this deployment, every row reads `ttmappyhour`.

`quality_check_1.csv` rotates to `_2.csv` at 40MB.
