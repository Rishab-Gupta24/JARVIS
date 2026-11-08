---
name: pygame-testing
roles: tester, coder, reviewer
keywords: pygame
---
Testing pygame games (tests run with pytest, no window, SDL dummy drivers):
- Import logic modules directly and test them as plain Python: `from game.fighter import Fighter`.
- Scenes: build the Game from game.engine with its scene table, call game.step(Input(...)) a number of
  times, assert on game.scene_name and game.data. Never call pygame.display or the event queue in tests.
- Use `from game.engine import Input`; Input(held={"right"}, pressed={"attack"}).
- Put tests in tests/test_<task id>.py with plain asserts; each test checks one acceptance criterion.
- Run the suite with run_tests; read the failure output and fix the code, not the tests.
