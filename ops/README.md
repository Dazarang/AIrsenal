# AIrsenal Pi automation

Unattended FPL pipeline on the Raspberry Pi (`dazpi`): 3h before every
gameweek deadline it pulls this repo, runs a headless-Claude chip-research
step, runs the AIrsenal pipeline (update DB → predictions → optimization →
transfers + lineup via the API), verifies server-side, and reports on
Telegram via its own send-only bot (`@fplquant_bot`, single recipient chat
id). 1h before the deadline a reminder fires if a chip was decided.

Full design/plan: `~/.claude/plans/what-we-want-to-quiet-aurora.md` (on the Mac).

## Layout

- `check_deadline.sh` — cron entry (every 15 min): live deadline from
  bootstrap-static, fires `run_gameweek.sh` at T-3h (max 2 attempts), chip
  reminder at T-1h, post-mortem alert if a deadline passes without a
  completed run.
- `run_gameweek.sh <gw> <deadline_epoch> [--dry-run]` — the orchestrator.
- `chip_research.sh` — `claude -p` with WebSearch; safe default "none";
  skipped entirely when no chip is available (GW1, chip already active).
- `helpers/gw_context.py` — context JSON for the research prompt + chip
  state (`~/airsenal-data/state/chips_state.json`).
- `helpers/post_summary.py` — suggestion inspection, Telegram text,
  server-side verification.
- State/logs under `~/airsenal-data/`: `run/` markers
  (`gwN.started/.posted/.applied/.done/.reminded/.attempts/...`), `logs/cron/`,
  `backups/` (sqlite backups, keep 8). `.posted` = a transfer POST was
  attempted with unknown outcome (never re-POST; when the client-side
  confirmation is missing the run checks the logged-in my-team view: picks
  match the suggestions → treated as applied, picks unchanged → `.posted`
  removed so the next attempt can retry); `.applied` = the transfers are in
  (confirmed client-side or server-side).

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
   `claude setup-token` on the Mac; `TELEGRAM_BOT_TOKEN` from @BotFather and
   `TELEGRAM_CHAT_ID` = your Telegram user id, after pressing Start on the
   bot once).
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

# chip research only (--dry-run: nothing recorded, [DRY RUN] messages)
./ops/chip_research.sh <gw> <deadline_epoch> [--dry-run]

# telegram test
./ops/notify.sh "test message"
```

## Chip semantics

- Decisions live in `~/airsenal-data/state/chips_state.json` (current season
  only; a run that ends without a chip - including every research failure -
  clears the gameweek's unconfirmed entry). Ground truth for chips played in
  past gameweeks is the public `/entry/{id}/history/` endpoint; for the
  upcoming gameweek only the logged-in `my-team` view shows the active chip
  and the team, so verification and the T-1h reminder read that. A chip that
  is already active on the site (activated by hand) is adopted as the run's
  decision - the optimizer is run for it and no research happens.
- A decision that never shows up in the played history by the next run
  (never activated, or activated and cancelled before the deadline) is lapsed
  and the chip returns to the pool.
- No chip is researched in GW1: the GW1 optimizer builds the squad from
  scratch and ignores every chip flag.
- wildcard / free_hit are applied via the transfers API payload (`chip:
  "wildcard"|"freehit"`) and verified on `my-team` afterwards.
- triple_captain / bench_boost are "team" chips (my-team endpoint, not the
  transfers payload); the pipeline does not activate them — you do it on the
  website after the ACTION REQUIRED Telegram message.
  `airsenal_set_lineup` keeps an active TC/BB (it sends the chip back with the
  lineup; `chip: None` would cancel it), so re-runs are safe.
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
  The scheduler treats a held `run/airsenal.lock` as "run in progress".
  Markers auto-reset when a new season reuses the gameweek number.
- FPL login: `FPLDataFetcher.login` retries the flaky PKCE flow 3x internally;
  the pre-flight check fails the run only when all attempts fail.
- Chip state repair: edit `chips_state.json` (set `"lapsed": true` to return
  a chip to the pool).
