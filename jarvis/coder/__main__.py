"""python -m jarvis.coder <project folder> "<task>"   (Jarvis starts this in a terminal window)"""

from __future__ import annotations

import sys
from pathlib import Path

from jarvis.coder import agent
from jarvis.config import Settings
from jarvis.core.llm import OllamaClient


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    root = Path(argv[0]).resolve()
    task = " ".join(argv[1:]) or input(f"What should I build in {root.name}? ").strip()
    if not task:
        return 0
    settings = Settings.from_env()
    llm = OllamaClient(settings.ollama_url, settings.coder_model, timeout=600)
    print(f"Jarvis coder ({settings.coder_model}) in {root}\nTask: {task}\n")

    def confirm(question: str) -> bool:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")

    try:
        agent.run(task, root, llm, confirm)
    except KeyboardInterrupt:
        print("\nStopped.")
    input("\nPress Enter to close this window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
