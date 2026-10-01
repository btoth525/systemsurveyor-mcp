# System Surveyor MCP

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent work with [System Surveyor](https://www.systemsurveyor.com) floor-plan site surveys: read them, put model numbers and prices on devices, recolor icons and coverage areas, place parts from a BOM, and turn a survey into a priced quote.

It runs as a small Docker container, speaks MCP over HTTP (Bearer-token protected), and is built around one idea: **an AI should be able to edit real customer surveys without being able to wreck them.** Every change is a dry run first, scoped to the owner's own surveys, capped in size, snapshotted, saved through the same lock/sync path the web app uses, and re-read to verify.

> **Unofficial.** System Surveyor has no public write API. This client was reverse-engineered from the web app's network traffic (see [How the API works](#how-the-system-surveyor-api-works)). It can break if they change their backend. Use it with your own account and your own surveys, and check your agreement with them first.

## What it does

```
 AI agent (Claude Code, Claude Desktop, any MCP client)
        |  MCP over HTTP + Bearer token
        v
 +------------------------------ container ------------------------------+
 |  server.py   tools, guardrails, keepalive watchdog, /health            |
 |  ssapi.py    System Surveyor API client (auth, read, lock/sync write)  |
 |  quote.py    BOM + quote math, CSV/XLSX import/export                  |
 |  plan.py     renders the floor plan + marker grid to a PNG             |
 |  /data       tokens.json, backups/, journal.jsonl (a mounted volume)   |
 +------------------------------------------------------------------------+
        |  HTTPS
        v
 System Surveyor  (openapi2 host for reads, openapi host for writes)
```

Typical conversations it enables:

- "Look at the *Acme Warehouse* survey and give me a BOM." -> `bom`, `quote`
- "Put the right Verkada models on every camera." -> `survey_gaps`, `find_products`, `assign_models`
- "Make all the multi-sensor cameras purple with red fields of view." -> `set_colors`
- "Here's a BOM. Where should these go?" -> `propose_placement` (returns **questions**), then `apply_placement`
- "Is the login still good?" -> `status`; "Undo that." -> `list_backups`, `restore_backup`

## Tools

Everything that changes a survey is a **dry run unless `apply=true`**.

| Group | Tools |
|---|---|
| Read | `list_sites`, `list_surveys`, `get_survey`, `survey_summary`, `survey_gaps`, `list_palette`, `list_profiles` (presets), `find_products`, `render_plan`, `download_floorplan`, `raw_get` |
| Change a survey | `assign_models`, `set_colors`, `set_attributes`, `rename_elements`, `place_elements`, `move_elements`, `delete_elements` |
| BOM and quote | `load_bom`, `match_parts`, `match_bom`, `bom`, `quote`, `propose_placement`, `apply_placement` |
| Safety and ops | `status`, `list_backups`, `restore_backup` |

Highlights:

- **`assign_models`** copies manufacturer, model, price and description from a saved product preset. By default it never touches surveyed geometry (mount height, coverage angle, direction, radius, frame rates). `copy_specs=true` also copies product specs, still skipping the protected set (`SURVEYED` in `server.py`).
- **`set_colors`** changes only colors: the icon (attr 530), the coverage color (457) and each lens of a multi-lens camera (a JSON string in attr 633). It can color by model, type, status or system, with a preview of the color map.
- **`propose_placement`** compares a BOM to a survey and returns a plan *plus a list of questions* (which blank elements to use, where the rest go, parts with no preset). The agent asks the human, then calls **`apply_placement`**, which does the assignments and new placements in one guarded save.
- **`quote`** prices the survey. Rates come from environment variables (see below), and anything it cannot price is listed under `gaps` rather than invented.

## Safety model

All writes go through one function, `_commit()` in `server.py`:

1. **Off by default.** `ALLOW_WRITES` must be `true`.
2. **Dry run by default.** Each tool shows exactly what would change; nothing is saved without `apply=true`. The server `INSTRUCTIONS` tell the agent to show the dry run and wait for a yes.
3. **Ownership.** The survey must be on this server's team **and** created by `OWNER_USER_ID`, or be listed in `WRITE_SURVEYS`. Coworkers' surveys are read-only here, even though the same login could technically edit them.
4. **Claim respected.** If someone else has the survey claimed for editing, the save is refused. If the owner has it claimed, the claim is left in place (the web app is not kicked out of edit mode).
5. **Size cap.** `MAX_ELEMENTS_PER_WRITE` (default 40) elements per save.
6. **Concurrency check.** The live `version` is re-read and compared right before saving.
7. **Snapshot first.** The full survey is written to `/data/backups/<id>-<timestamp>.json`.
8. **Verify after.** The survey is re-read and compared to what was sent; the result reports `verified: true/false`.
9. **Journal.** Every save is appended to `/data/journal.jsonl`.
10. **Undo.** `restore_backup` puts a snapshot back (also dry-run first, also guarded and snapshotted).

Selection tools (`set_colors`, `set_attributes`, `assign_models`) refuse to run without a scope (ids, element type, name prefix or model) so "change everything" can't happen by accident.

The agent endpoint requires `Authorization: Bearer $MCP_TOKEN`. File links returned by the server (quotes, floor plan PNGs) are HMAC-signed with that token and expire after 24 hours.

## Keeping the login alive

System Surveyor uses a short-lived access token plus a **rotating refresh token** (every refresh returns a new one, valid about 7 days, and the old one dies). Two things follow:

- **Only the container may refresh.** If your laptop and the container both refresh, they invalidate each other. `relogin.sh` deletes the local copy after pushing it for this reason.
- **The container refreshes on a timer** (`KEEPALIVE_HOURS`, default 2) so the token never lapses, retries on network errors, and distinguishes "can't reach System Surveyor" (transient) from "refresh rejected" (a human must log in).

Logging in can hit a captcha, so a human step is unavoidable when the login does lapse. When it does, the container POSTs a JSON alert to `ALERT_WEBHOOK` (optional; payload `{event, importance, subject, description}`, adapt to Slack, ntfy, Home Assistant, etc.), sends one reminder a day, and `/health` returns 503 until it is fixed. Then:

```sh
./relogin.sh        # runs login.py, pushes the login to the container, removes the local copy
```

The container picks up the new login within about 10 minutes and sends a "back" notification.

## Setup

Requirements: a Docker host reachable over SSH, Python 3.11+ on your workstation (for the one-time login), a System Surveyor account.

```sh
cp .env.example .env          # fill in as you go

python -m pip install httpx
python login.py               # one-time interactive login (password, or paste a refresh token)
python whoami.py              # prints OWNER_USER_ID, SS_ACCOUNT_ID, SS_TEAM_ID for .env

openssl rand -hex 32          # use as MCP_TOKEN in .env

./deploy.sh --migrate-token   # builds the image on DEPLOY_HOST, starts it, copies your login in
curl http://<host>:8797/health
```

`deploy.sh` ships secrets over SSH stdin into a `0600` env file on the host; nothing secret is baked into the image or committed. It uses plain `docker`, so any Linux host works.

Point an MCP client at `http://<host>:8797/mcp` with the header `Authorization: Bearer <MCP_TOKEN>`. For example, with Claude Code:

```sh
claude mcp add --transport http systemsurveyor http://<host>:8797/mcp --header "Authorization: Bearer <MCP_TOKEN>"
```

If you expose it beyond your LAN (a reverse proxy, a tunnel), keep the Bearer token long and random, set `PUBLIC_URL` so file links work, and leave writes off unless you need them.

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `MCP_TOKEN` | Bearer token clients must send. Required. |
| `PUBLIC_URL` | External base URL, used to build signed download links. |
| `ALLOW_WRITES` | `true` to allow saving changes. Default `false` (read-only). |
| `OWNER_USER_ID` | Only surveys created by this user may be written. |
| `WRITE_SURVEYS` | Comma-separated survey ids that may be written regardless of creator. |
| `SS_ACCOUNT_ID`, `SS_TEAM_ID` | Your account and team (run `whoami.py`). |
| `MAX_ELEMENTS_PER_WRITE` | Per-save cap. Default 40. |
| `KEEPALIVE_HOURS` | Token refresh interval. Default 2. |
| `ALERT_WEBHOOK` | Optional URL that receives login-lapsed alerts. |
| `LABOR_RATE`, `CABLE_PER_FT`, `MARKUP_PCT`, `TAX_PCT` | Defaults for `quote`. Unset rates mean "leave unpriced and report a gap". |
| `DEPLOY_HOST`, `REMOTE_DIR`, `SSH_KEY` | Used only by `deploy.sh` / `push_token.sh`. |
| `TZ` | Container time zone. |

## How the System Surveyor API works

Notes for anyone extending this, because none of it is documented:

- **Two hosts.** Reads work on `openapi2.systemsurveyor.com/v3`. Writes only work on `openapi.systemsurveyor.com/v3`; the other host returns 404 for `/lock`.
- **Save sequence:** `POST /survey/{id}/lock`, then `POST /site/{siteId}/survey/{id}/sync` with the whole survey document (returns a job id), then poll `GET /survey/sync/{job}` until it stops returning 202, then `DELETE /survey/{id}/lock`. See `Client.save_survey`. The survey document is sent whole, which is why snapshots and the version check matter.
- **The claim** (web app "edit mode") is the survey's `editor` field, an object `{user_id, first_name, last_name}`.
- **Elements are bags of attributes** keyed by numeric `attribute_id`. The ones used here:

  | id | meaning | id | meaning |
  |---|---|---|---|
  | 141 | element ID (e.g. `FCAM-001`) | 530 | icon color (hex, no `#`) |
  | 138 | status | 457 | coverage (AOC) color |
  | 271 / 305 | manufacturer / model | 298, 299, 459 | coverage angle / direction / radius |
  | 532 | device price | 633 | multi-lens coverage, a JSON string with per-lens color, angle, direction, radius |
  | 531 | quantity | 167 | mount height |
  | 533 | install hours | 524, 521, 526 | cable length, extra length, type |

- **Presets** ("element profiles") are per team. A preset copies product specs onto an element, which is why `assign_models` excludes surveyed attributes by default: applying a preset naively overwrites the measured mount height and coverage angle.
- **Element types** are numeric (`69` fixed camera, `257` multi-lens camera, `65` cable path, ...). `list_palette` shows your team's list.

## Development

```sh
pip install -r requirements.txt pytest
pytest                          # offline tests: quote math, color parsing, write-permission rules
python server.py --http         # run locally (needs MCP_TOKEN and a login)
```

Layout: `server.py` (tools + guardrails + watchdog), `ssapi.py` (API client), `quote.py` (BOM/quote), `plan.py` (floor-plan rendering), `login.py` / `whoami.py` (one-time setup), `deploy.sh` / `push_token.sh` / `relogin.sh` (ops), `tests/`.

When adding a change tool, follow the pattern: select targets with `_select` (which requires a scope), mutate the in-memory survey, return a preview when `apply` is false, and otherwise call `_commit(...)`. Never call `C.save_survey` directly; `_commit` is where every guardrail lives.

## Known limits

- Unofficial API; no stability guarantee.
- Coverage-area *geometry* (angle, direction, radius) is deliberately read-only for the AI. Color and transparency only.
- A login lapse needs a human (captcha), by design the container tells you rather than trying to bypass it.
- One writer at a time (a process-wide lock around saves).
