---
name: web-animation
roles: planner, tester, coder, reviewer
keywords: animated, animation, website, landing, portfolio, scroll, reveal, hero, parallax, svg, canvas, css
---
Animated websites without libraries:
- Scroll reveals: elements start with class `reveal` (opacity 0, translateY(24px)); an IntersectionObserver in
  js/main.js adds `is-visible` once (then unobserve). Stagger with a CSS variable: `transition-delay: calc(var(--i) * 70ms)`.
- Hero: a CSS @keyframes entrance (fade + rise), an animated gradient or a small canvas particle field
  (requestAnimationFrame, capped particle count, paused when the tab is hidden).
- SVG line drawing: `stroke-dasharray` + `stroke-dashoffset` animated to 0 for logos/illustrations.
- Hover/press: transform: translateY(-2px) / scale(1.02) with 150-200 ms ease-out; focus-visible styles too.
- Performance: animate only transform and opacity; `will-change` only on what's animating; no layout thrash in
  scroll handlers (use IntersectionObserver, not scroll events).
- Always include `@media (prefers-reduced-motion: reduce)` that removes the motion (content stays visible).
