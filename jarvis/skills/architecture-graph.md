---
name: architecture-graph
roles: planner, reviewer
keywords: architecture, module, graph, design
always: true
---
Architecture for a small game:
- Modules have one job each: config/constants, entities (fighter, projectile), rules (combat, rounds),
  ai, scenes (one per screen), drawing helpers, and main (wiring only).
- Dependencies point one way: main -> scenes -> rules/entities/ai -> config. Logic modules never import
  drawing or pygame/DOM, so tests can run them without a window.
- List modules with their purpose and which modules they use; list scenes and the transitions between
  them with the trigger ("menu -> play on confirm", "play -> results when a fighter wins 2 rounds").
- Every scene in the list must be reachable from the first scene.
