---
name: engineering-practice
roles: coder, tester, planner, reviewer
keywords: bug, error, fail, fix, import, refactor
always: true
---
Work like an engineer:
- Read before writing: open the files you'll touch and the code that defines every name you use. Never guess an API.
- Root cause first: follow a traceback to the line that is really wrong. "cannot import name X from partially
  initialized module" means two modules import each other: remove the import that points the wrong way (low-level
  modules like the engine or config never import scenes), don't add another.
- Smallest change that works; don't rewrite files that aren't part of the problem.
- One idea at a time: change, run the tests, read the result, then the next change.
- Keep dependencies one-way: config <- logic <- scenes <- main.
- Edge cases: empty lists, zero/negative numbers, the first and last frame, a key pressed and held, a round ending
  on a draw.
- Done means the tests and the automatic check pass, not that the code looks right.
