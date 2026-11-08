---
name: procedural-animation
roles: planner, coder, reviewer
keywords: animation, animate, character, sprite, draw, art, look, visual, fighter
---
Drawing characters in code (no image downloads; the art is original and made by the game itself):
- A character is a small skeleton: hip, torso, head, upper/lower arms and legs as line segments or
  rounded shapes, with joint angles. Poses are dicts of angles; animations interpolate between poses
  over frames (idle breathing, walk cycle, punch extend/retract, kick, block, hit recoil, ko fall).
- Pick a distinctive original style: flat bright colours with outlines, ink-brush strokes, geometric
  low-poly shapes, paper cut-outs... not a copy of a known game's look.
- Effects are cheap and readable: hit flash (white for 2 frames), screen shake, sparks (short-lived
  particles), dust on landing. Keep drawing code out of logic modules.
