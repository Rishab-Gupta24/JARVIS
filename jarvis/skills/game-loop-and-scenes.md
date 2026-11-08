---
name: game-loop-and-scenes
roles: planner, tester, coder, reviewer
keywords: game, scene, menu, level, play, pause, screen
---
Game loop and scenes (the skeleton already provides these; extend, don't replace):
- Fixed time step: update(inp, dt) with dt = 1/60 s, drawing separate. Logic never reads the clock or
  the keyboard directly: it gets an Input with `held` and `pressed` sets of action names
  (left, right, up, down, jump, attack, kick, block, special, confirm, back, pause).
- One Scene subclass per screen. update() returns the NAME of the next scene to switch to, or None to
  stay. Shared data (chosen fighter, coins, progress) lives on game.data (a dict), not in globals.
- Register every scene in the game's scene table; names are lowercase strings that match the plan.
- Autoplay: the smoke run drives the game with scripted input (confirm presses plus random moves), so
  every scene must be leavable with confirm/attack/back and timers - never wait for one exact key combo.
- Keep numbers (speeds, damage, timings) in a config module, not scattered in code.
