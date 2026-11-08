---
name: fighting-game
roles: planner, tester, coder, reviewer
keywords: fight, fighter, fighting, combat, punch, kick, sword, weapon, duel, martial, combo, brawl, versus
---
2D fighting game mechanics:
- Fighter state machine: idle, walk, jump, crouch, block, attack (startup -> active -> recovery frames),
  hitstun, blockstun, knockdown, ko. Only idle/walk can start a new action; buffer inputs for ~6 frames.
- Attacks are data: {name, damage, startup, active, recovery, reach, hitstun, knockback, chip}. A hitbox
  (rect in front of the fighter, only during active frames) hits the opponent's hurtbox once per attack.
- Block: holding block (or back) facing the attack reduces damage to chip damage and causes blockstun.
  Low/high attacks may beat standing/crouching block if the design wants depth.
- Facing: fighters always face each other; flip hitboxes with facing. Keep both inside the arena bounds
  and push them apart when they overlap.
- Rounds: health bars, round timer (e.g. 60 s), best of 3; time out -> more health wins.
- Weapons/upgrades change attack data (damage, reach, speed), not code paths.
- Testable without a window: Fighter.update(inp), combat.resolve(attacker, defender), round results.
