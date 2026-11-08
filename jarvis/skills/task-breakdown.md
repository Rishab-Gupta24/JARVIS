---
name: task-breakdown
roles: planner
keywords: task, tasks, plan
always: true
---
Breaking a game into tasks for small coding agents:
- 8 to 20 tasks. Each one is a single change of 1-3 files that a test can check, done in under an hour.
- Order: pure logic first (data, rules, physics, combat maths), then scenes that use it, then drawing,
  then polish (sound, effects, balancing). The engine skeleton (scene manager, main loop, input,
  autoplay smoke run) already exists: build on it, don't recreate it.
- Every task lists depends_on (ids of tasks it needs), files it creates or changes, and 2-4 acceptance
  criteria written as checks a test can make WITHOUT a window or a human: "Fighter.take_hit(10) lowers
  health by 10 and never below 0", "after 3 round wins the match scene switches to 'results'".
- Never write "make it fun" or "looks good" as a criterion; say what is measurable.
- No task depends on a later task. No cycles.
