---
id: xscraper-glossary
version: 2026.07.03
requires:
  - xscraper-schema
---
# XScraper Glossary

## Brands And Regions

- Tentatek is a weapon brand, not a ranked mode.
- In XScraper region columns, `region=false` means the Tentatek division and
  `region=true` means the Takoroka division.
- Tentatek/Takoroka should not be compared to ranked battle modes. Treat them as
  regional/division filters when the task asks for a region or division.

## Seasons

- `season_number` identifies the XScraper season snapshot used for historical
  rankings and weapon leaderboards.
- "Current" or "latest" season questions should prefer latest/current player
  state when available, then use explicit season tables only when the task asks
  for season history.
- A player's best historical result can require historical ranking or
  weapon-specific context, not only the latest observed state.

## Players And Aliases

- A splashtag is a player-facing handle string such as `name#1234`.
- `player_id` is the stable join key; splashtag, name, and alias strings are
  display or lookup fields and can change over time.
- Alias history tracks previously observed splashtags for player lookup.

## Common Pitfalls

- Do not treat a weapon brand, player alias, stage name, or division name as a
  battle mode value.
- If a named player is not found in the current state, check alias history
  before concluding there is no match.
- Use regional boolean filters only when the task explicitly asks for a division
  or region, or when a leaderboard table requires the distinction.
