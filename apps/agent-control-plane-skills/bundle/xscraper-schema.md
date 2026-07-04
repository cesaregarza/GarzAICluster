---
id: xscraper-schema
version: 2026.07.03
---
# XScraper Schema Guidance

XScraper stores Splatoon X Power leaderboard, season, alias, weapon, and schedule data.
Use the rendered capability facts for the authoritative schema allowlist, column names,
enum domains, row caps, and SQL guardrails.

## Common Joins

- Latest player rows: join `xscraper.player_latest` to `xscraper.players` on
  `player_id`, `mode`, and `timestamp`.
- Season metadata: join `xscraper.players` to `xscraper.player_season` on
  `player_id` and `season_number`.
- Historical season results connect to aliases by `player_id` and displayed
  `splashtag`.

## Query Patterns

- Use current/latest tables for "current", "latest", or "now" questions.
- Use `season_results` for historical leaderboard placements and previous seasons.
- Use `weapon_leaderboard` for weapon-specific rankings or games-played shares.
- Use `schedules` for rotation timing, stages, Splatfest flags, and scheduled modes.
