---
name: enemy-ai
roles: planner, tester, coder, reviewer
keywords: enemy, enemies, ai, opponent, bot, cpu, boss, npc
---
Computer opponents:
- The AI produces the same Input object a player would (held/pressed action sets) from the game state;
  it never moves the character directly. That keeps rules identical for player and AI.
- Simple state machine: approach, keep distance, attack when in reach, block when the player's attack is
  active and close, retreat when low on health. Add reaction delay (frames) and a random factor.
- Difficulty is numbers: reaction frames, block chance, aggression. Bosses get better numbers or moves.
- Deterministic in tests: pass a seeded random.Random into the AI.
