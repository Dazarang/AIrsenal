# AIrsenal Pi automation

Unattended FPL pipeline on the Raspberry Pi (`dazpi`): 3h before every
gameweek deadline it pulls this repo, runs a headless-Claude chip-research
step, runs the AIrsenal pipeline (update DB → predictions → optimization →
transfers + lineup via the API), verifies server-side, and reports on
Telegram (reusing the pquant bot token read-only). 1h before the deadline a
reminder fires if a chip was decided.

Full design/plan: `~/.claude/plans/what-we-want-to-quiet-aurora.md` (on the Mac).

## Layout

- `check_deadline.sh` — cron entry (every 15 min): live deadline from
  bootstrap-static, fires `run_gameweek.sh` at T-3h (max 2 attempts), chip
  reminder at T-1h, post-mortem alert if a deadline passes without a
  completed run.
- `run_gameweek.sh <gw> <deadline_epoch> [--dry-run]` — the orchestrator.
- `chip_research.sh` — `claude -p` with WebSearch; safe default "none".
- `helpers/gw_context.py` — context JSON for the research prompt + chip
  state (`~/airsenal-data/state/chips_state.json`).
- `helpers/post_summary.py` — suggestion inspection, Telegram text,
  server-side verification.
- State/logs under `~/airsenal-data/`: `run/` markers
  (`gwN.started/.posted/.applied/.done/.reminded/.attempts/...`), `logs/cron/`,
  `backups/` (sqlite backups, keep 8). `.posted` = a transfer POST was
  attempted (never re-POST); `.applied` = the POST was confirmed client-side.

## Setup (Pi)

1. SSH key: `ssh-keygen -t ed25519 -f ~/.ssh/airsenal_github -N ""`, add the
   pub key as a read-only deploy key on `Dazarang/AIrsenal`, append to
   `~/.ssh/config` (do not touch the pquant block):

   ```
   Host github-airsenal
     HostName github.com
     User git
     IdentityFile ~/.ssh/airsenal_github
     IdentitiesOnly yes
   ```

2. `git clone git@github-airsenal:Dazarang/AIrsenal.git ~/airsenal/app`
3. `cd ~/airsenal/app && ./ops/install_pi.sh`
4. Fill `~/airsenal-data/env` (ABSOLUTE paths only; values from
   `uv run airsenal_env get` on the Mac; `CLAUDE_CODE_OAUTH_TOKEN` from
   `claude setup-token` on the Mac).
5. Initialise DB:
   `set -a; source ~/airsenal-data/env; set +a && .venv/bin/airsenal_setup_initial_db && .venv/bin/airsenal_update_db`
6. Dry run (see below), then `./ops/install_pi_crontab.sh`.

## Manual operations

```bash
# dry run for the next GW (no POSTs, [DRY RUN] Telegram messages)
gw=$(curl -s https://fantasy.premierleague.com/api/bootstrap-static/ | python3 -c 'import json,sys;e=[e for e in json.load(sys.stdin)["events"] if e["is_next"]][0];print(e["id"], int(__import__("datetime").datetime.strptime(e["deadline_time"],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=__import__("datetime").timezone.utc).timestamp()))')
./ops/run_gameweek.sh ${gw% *} ${gw#* } --dry-run
tail -f ~/airsenal-data/logs/cron/gw*-dryrun-*.log

# real run now (ignores the schedule; markers still apply)
./ops/run_gameweek.sh <gw> <deadline_epoch>

# chip research only
./ops/chip_research.sh <gw> <deadline_epoch>

# telegram test
./ops/notify.sh "test message"
```

## Chip semantics

- Decisions live in `~/airsenal-data/state/chips_state.json`; ground truth
  for played chips is the public `/entry/{id}/history/` endpoint.
- wildcard / free_hit are applied via the transfers API payload and verified
  server-side afterwards.
- triple_captain / bench_boost CANNOT be played via the API — you activate
  them on the website after the ACTION REQUIRED Telegram message. **Never
  run `airsenal_set_lineup` after activating a chip manually** (it posts
  `chip: None` and re-picks the captain).
- If a decided TC/BB is never activated, the next run notices (history vs
  state), sends a note, and returns the chip to the pool.

## Upstream sync (Mac)

```bash
git fetch upstream && git merge upstream/main && git push
```

The Pi pulls `origin/main` (this fork) at the start of every run.

## Maintenance

- `claude setup-token` expires after ~1 year → regenerate on the Mac, update
  `CLAUDE_CODE_OAUTH_TOKEN` in `~/airsenal-data/env`.
- Stuck gameweek: inspect/remove `~/airsenal-data/run/gwN.*` markers
  (`.attempts` caps retries at 2; `.done` blocks re-runs; `.posted`/`.applied`
  mean a transfer POST was attempted/confirmed and must never be re-POSTed).
  Markers auto-reset when a new season reuses the gameweek number.
- Chip state repair: edit `chips_state.json` (set `"lapsed": true` to return
  a chip to the pool).
