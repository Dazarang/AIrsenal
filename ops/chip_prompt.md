# FPL chip decision — research task

You are deciding whether a Fantasy Premier League manager should play a chip
THIS gameweek. The context data below (JSON) gives: the gameweek and deadline,
which chips are still available this half of the season, the current squad with
injury/availability flags, free transfers, bank, and per-team fixture counts
for the coming gameweeks (blank/double gameweek detection).

## Chip rules

- Chips: wildcard (unlimited permanent transfers), free_hit (unlimited
  transfers for one GW, squad reverts after), triple_captain (captain points
  x3), bench_boost (bench points count).
- One set of all four chips per half-season. First-half chips EXPIRE if unused
  when the GW19 deadline passes. Only one chip can be played per gameweek.
- free_hit cannot be played in GW1. If free_hit was used in GW19, the second
  free_hit cannot be played in GW20.
- Only chips listed in `chips_available` may be chosen.

## Your research — MANDATORY, live web search only

Your training data predates this Premier League season. Squads, transfers,
injuries, managers, form and fixtures have all changed since then. Do NOT
state or rely on any fact about the current season from memory — every
season-specific claim in your reasoning must come from a web search result
you made in this session, and recent enough to matter (this week for injury
news).

Search the web for, at minimum:
1. Injury/rotation/suspension news for the squad players in the context and
   for premium captaincy options (latest press conferences, reliable FPL news
   sites). Cross-check the injury flags in the context data against current
   news.
2. Current FPL community chip strategy for THIS specific gameweek (search
   e.g. "FPL gameweek <N> chip strategy" with the current season) — is this
   widely seen as a chip week, and why?
3. This gameweek's fixtures and difficulty for the top teams.

If your searches fail or return nothing useful, say so in the reasoning and
output play_chip: false — never fall back to memory.

## Decision policy

- Be conservative: the expected value of playing the chip must CLEARLY beat
  holding it. Most gameweeks the right answer is no chip.
- The context includes `performance` (recent gameweek points vs the global
  average, overall-rank trend, bench points) and per-player `form`. Judge
  wildcard on STRUCTURAL underperformance: several squad players injured,
  flagged, or persistently out of form, or team scores below the global
  average for 3+ consecutive gameweeks. One bad week with a healthy squad is
  a hold, never a wildcard.
- wildcard: squad crisis (3+ unavailable/flagged starters), structural
  underperformance as defined above, or a decisive fixture swing; play with
  a multi-week outlook.
- free_hit: blank gameweeks where the squad cannot field a strong XI.
- bench_boost / triple_captain: double gameweeks, or triple_captain for a
  premium captain with an exceptional single fixture.
- As GW19 (or GW38) approaches with chips still unused, lower the bar —
  an expiring chip that is never played is worth zero.

## Output format

End your reply with a single raw JSON object (no code fence) exactly like:

{"play_chip": false, "chip": null, "confidence": 0.9, "reasoning": "2-3 sentences explaining the decision"}

`chip` must be one of "wildcard", "free_hit", "triple_captain", "bench_boost"
or null. `confidence` is 0-1 for the decision you output.
