---
name: balancing
roles: planner, tester, coder, reviewer
keywords: balance, damage, health, upgrade, upgrades, difficulty, coins, shop, level, progression, unlock
---
Balancing and progression:
- Put every tunable number in one config module. Rewards grow a little faster than costs early, slower
  later. Upgrades give +10-20% per level with a cap; no single upgrade should make the game trivial.
- Progression saves to a small JSON file next to the game (save.json); load safely with defaults when
  the file is missing or broken.
- Test balance rules with numbers: "a fully upgraded fighter still needs at least 4 hits to KO a level-1
  opponent", "the shop never sells an upgrade the player can't see".
