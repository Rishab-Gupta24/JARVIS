---
name: web-canvas-game
roles: planner, tester, coder, reviewer
keywords: web, html, canvas, javascript, browser, iphone, phone, touch, mobile
---
HTML5 canvas games (no npm packages, no build step; plain ES modules):
- src/engine.js has Scene, Game and Input (held/pressed Sets of action names). Logic modules in src/
  (fighter.js, combat.js...) never touch the DOM or canvas, so `node --test` can run them.
- index.html draws with a 2D canvas scaled to the window (devicePixelRatio aware) and shows on-screen
  touch buttons for phones; keyboard works too. Use requestAnimationFrame with a fixed 1/60 s update.
- Tests: tests/<task id>.test.mjs using `import test from "node:test"` and `import assert from
  "node:assert/strict"`, importing from "../src/....js" (always with the .js extension).
- smoke.html (already there) imports the game and runs it headless; keep src/scenes.js exporting the
  scene table it uses.
