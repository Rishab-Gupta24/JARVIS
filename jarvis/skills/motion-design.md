---
name: motion-design
roles: planner, coder, reviewer
keywords: animation, animate, animated, motion, transition, tween, ease, scroll, reveal, hover, effect
---
Motion that feels good:
- Purpose: motion shows cause and effect (a hit, a button press, a section arriving), it doesn't decorate.
- Durations: small UI 150-250 ms, bigger moves 300-500 ms; game feedback a few frames (hit flash 2-4 frames).
- Easing: ease-out for things arriving, ease-in for leaving, never linear for UI; a slight overshoot for playful.
- Stagger lists/cards by 40-80 ms so they arrive one after another.
- Animate transform and opacity (cheap); avoid animating layout properties (width, top, margin) on the web.
- Respect reduced motion: `@media (prefers-reduced-motion: reduce)` turns animations off or to simple fades.
