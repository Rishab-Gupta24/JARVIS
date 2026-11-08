---
name: game-feel
roles: planner, coder, reviewer
keywords: game, fight, hit, juice, feel, effect, shake, flash, particle, impact, combat
---
Game feel ("juice") that makes actions readable:
- Every hit: a 2-4 frame white flash on the target, a small knockback, a short screen shake (3-6 px, ~150 ms) and a
  few spark particles at the contact point. Blocked hits: a different colour spark, no shake.
- Hit stop: freeze both fighters for 3-5 frames on a heavy hit; it sells impact.
- Health bars: a fast red part and a slower trailing part so damage is visible; flash when low.
- Anticipation and follow-through in animations (wind-up before a strike, recovery after).
- Sound or screen cue for every important event (round start "Fight!", KO slow-motion, win banner).
- Keep effects in drawing/scene code with numbers in config; logic stays testable.
