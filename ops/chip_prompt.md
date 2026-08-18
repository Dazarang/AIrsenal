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

## Your research

Use web search to check, for THIS gameweek:
1. Injury/rotation/suspension news for the squad players flagged in the
   context and premium captaincy options (press conferences, reliable FPL news
   sites).
2. Whether the FPL community consensus sees this as a chip week (blank/double
   gameweek strategy articles for the current gameweek).
3. Fixture difficulty for the top teams this gameweek.

## Decision policy

- Be conservative: the expected value of playing the chip must CLEARLY beat
  holding it. Most gameweeks the right answer is no chip.
- wildcard: squad crisis (3+ unavailable/flagged starters) or a decisive
  fixture swing; play with a multi-week outlook.
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
