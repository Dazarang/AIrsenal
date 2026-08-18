# FPL chip decision — live research task

Decide whether this Fantasy Premier League manager plays ONE chip THIS gameweek.
A wrong chip costs real points; a chip left unplayed when its half ends is worth
exactly zero. Both are real errors — do not simply default to "no".

What your answer triggers (you are NOT picking players):
- wildcard / free_hit → AIrsenal's optimizer rebuilds the squad and POSTs it
  automatically.
- triple_captain / bench_boost → the lineup is optimized for the chip, but a
  human must activate it on the FPL site within ~3 hours, prompted by your
  `reasoning` text. The pipeline picks the captain itself: the squad player
  with the highest `predicted_points_this_gw`. Choose triple_captain only when
  that specific player is the one you want tripled.

## Context JSON (appended below this prompt)

- `gameweek`, `season` ("2627" = 2026/27), `season_name`, `deadline_utc`,
  `gameweeks_left_in_half` (includes this gameweek). Two chip sets per season:
  set 1 dies at the GW19 deadline, set 2 runs GW20-38, no rollover, one chip per
  gameweek.
- `chips_available` — AUTHORITATIVE. It already applies chips used,
  one-per-gameweek, the GW1 and GW19/GW20 restrictions and the FPL server's own
  chip status. Choose only from it, spelled exactly; anything else is silently
  discarded. If it is empty, output play_chip:false immediately and do no
  research.
- `chips_played_this_season`, `free_transfers` (up to 5 can be banked), `bank`
  (£m), `squad_value` (£m, current selling value of the 15). null = unknown.
- `squad` — your 15. `team` is a 3-letter code (ARS, AVL…; search by full club
  name). `status`: a=available, d=doubtful, i=injured, s=suspended,
  u=unavailable, n=not in squad. `chance_next_round` = % chance of playing
  (null = unflagged). `form` = FPL's recent average. `predicted_points_this_gw`
  = AIrsenal's own model prediction — use it as the base number in every EV
  calculation below. FPL's flags lag press conferences — verify them.
- `performance` — recent gameweek points vs the global average, overall-rank
  trend, bench points. The wildcard signal: judge STRUCTURAL underperformance
  (below the global average 3+ consecutive gameweeks, several players out of
  form or flagged), never one bad week with a healthy squad.
- `fixtures_by_gameweek` — `doubles`/`blanks` per gameweek (same team codes)
  for this gameweek and the next five, from AIrsenal's database. THIS gameweek
  is reliable; later gameweeks are not — rounds whose fixtures are not
  scheduled yet show up as mass blanks, and doubles appear only once postponed
  matches are re-dated. Treat an unconfirmed future double as ~50-70% likely,
  and never act on a future blank you have not verified live.
- `notes` — read these FIRST. If a note reports a failure, or `squad` is empty,
  you cannot assess a chip: output play_chip:false and say so.

## Research — live sources only

Whatever you remember about this season is incomplete or out of date:
transfers, loans, managers, penalty and set-piece duties, form, injuries, even
scoring rules. Feeling sure about a current-season fact is not evidence. Every
season-specific claim you make must come from a page you opened in THIS
session — dated within ~7 days for team news, this season for anything else.
If you write a fact you did not read today, delete it.

At least two of your lookups must be WebSearch calls (a decision to play a
chip made with fewer than two is automatically thrown away). Priorities, in
order:
1. This gameweek's actual fixtures — confirm the context's doubles/blanks.
   `https://fantasy.premierleague.com/api/fixtures/?event=<gameweek>` is
   authoritative; its team ids are numeric, but the number of fixtures alone
   tells you whether the round is normal (10), doubled (>10) or blanked (<10).
   Then search for which clubs.
2. Team news from the last 48h for every flagged squad player and for the
   captaincy candidates: press conferences, injury round-ups, suspensions
   (yellow-card totals), plus midweek European fixtures either side of this
   gameweek — the main rotation signal.
3. A numeric read on the captain: bookmaker anytime-scorer and clean-sheet
   odds are the best public proxy for expected points.
4. Chip-strategy consensus for this gameweek this season and its reasons —
   evidence about fixtures, not authority: the decision is about THIS squad.

Check every page's date and that it names the current season and gameweek;
discard the rest, and corroborate anything decisive with a second source.
Ignore ownership %, transfer leaderboards, price-change predictors and pundit
tips: they cost turns and decide nothing.

Research thoroughly — depth beats speed here, and there is ample time. Typical
good runs use 6-15 searches/fetches; use more if the situation is genuinely
ambiguous. Stop when further research would no longer change your decision,
then output the JSON. If your searches fail or return nothing usable, say so
and output play_chip:false. Never fall back to memory.

## Deciding

Count first, from `squad` plus this gameweek's `doubles`/`blanks` (as
corrected by your research): how many of the 15 have no fixture, how many have
two, and how many are genuinely out (`i`/`s`/`u`, or `chance_next_round` <= 25
— a 75% doubt usually plays).

Marginal value = the points the chip adds versus not playing it (use
`predicted_points_this_gw` as the base numbers):
- triple_captain = one extra copy of your captain's expected score. Premium
  with a good single fixture 6-9; a confirmed-fit premium with two fixtures
  11-14. The floor is partly hedged (a captain who plays 0 minutes passes the
  triple to the vice-captain), so in a double the real risk is rotation or a
  suspension, not fixture quality.
- bench_boost = the sum of your 4 bench players' expected points, minus ~1.5
  for the loss of auto-subs. NOT "a double gameweek": it needs four nailed
  starters who all play, and a backup keeper who never plays makes it a
  3-player chip. A fodder bench returns 3-8 even in a double. Ask whether
  `free_transfers` and `bank` can realistically fix the bench this week — a
  bench boost usually needs a preparation week, and you cannot wildcard and
  bench boost in the same gameweek.
- free_hit = (best XI buyable within `squad_value` + `bank`) − (best XI you
  can field now). Triggers: 8 or fewer of your 15 have a fixture (at 9-10 a -4
  hit is cheaper), or a big double gameweek arrives and 5 or fewer of your 15
  are in it. The squad reverts, so it buys one week and fixes nothing
  structural — never a substitute for a needed wildcard.
- wildcard = 4 × max(0, transfers you want − `free_transfers`) + roughly 2-4
  points per gameweek of squad uplift over the rest of the half, justified by
  `performance` showing structural underperformance. Flagged players are a
  transfer problem, not a wildcard trigger, and a bank of 5 free transfers is
  already a mini-wildcard. The wildcard also decays fastest — played in the
  last 1-2 gameweeks of a half it is worth almost nothing, so if it is going
  to be used, use it before the others.

Bar (yardsticks, not measurements). slack = `gameweeks_left_in_half` − number
of `chips_available`:
- slack >= 6: full bar — triple_captain >= 10, bench_boost >= 18, free_hit
  >= 20, wildcard >= 20 (multi-week total).
- slack 3-5: 0.75 × those. slack 1-2: 0.5 × those.
- slack <= 0: a chip will be lost — play the best one available this gameweek.
- First half only: GW1-19 has few double gameweeks and few blanks, so from
  GW10 onwards use at most the 0.75 bar and ask "is this the best week left
  before GW19?" rather than "does it clear an absolute bar". Hoarding all four
  chips is the common failure.
- Otherwise hold only if you can name a specific, likely better week ahead.
- Sequencing when several chips remain: wildcard/free_hit first (they build or
  replace the squad), bench_boost/triple_captain into the weeks that suit
  them; a chip needing a preparation week consumes two consecutive gameweeks.
- There is no reward for activity: you are judged only on net points over the
  half-season. Most gameweeks genuinely clear no bar, so play_chip:false is
  the most common correct output — but reach it by doing the arithmetic above,
  never as a reflex, and never play a chip merely because one is available.

## Output

Finish with a few sentences of reasoning, then exactly ONE raw JSON object:
the last thing in your message, on a single line, no code fence, and no other
JSON object anywhere in that message.

{"play_chip": false, "chip": null, "confidence": 0.8, "sources": ["site - date - fact"], "reasoning": "1-3 sentences"}

- `chip`: a string copied from `chips_available`, or null when `play_chip` is
  false. A chip that is not in that list, or play_chip:true with a null chip,
  is silently discarded.
- `confidence`: your probability that the action you chose beats the
  alternative. play_chip:true is only accepted at >= 0.6 (relaxed to >= 0.5
  automatically once chips are close to expiring, slack <= 2) — if you cannot
  honestly get there, output play_chip:false and say why. Do not inflate the
  number to clear the bar.
- `sources`: 2-6 pages you actually opened this session, each with its date.
- `reasoning`: sent verbatim to the manager's phone — plain text, no newlines,
  no markup. For triple_captain/bench_boost it must say what to activate and
  why (name the captain), because they have to click it themselves before the
  deadline.
