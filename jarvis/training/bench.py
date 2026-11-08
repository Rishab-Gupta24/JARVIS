"""Score a model in Ollama on code that must actually run and pass tests.

    .venv\\Scripts\\python -m jarvis.training.bench qwen2.5-coder:7b jarvis-coder [--limit 40]

- HumanEval (OpenAI, 164 problems with hidden unit tests): pass@1 at temperature 0.
- Held-out Python functions from our own set: the answer must pass the original function's
  doctests. These were never trained on (datasets.py holds them out).

Generated code runs in a temporary folder with `python -I` and a 15 s timeout per program.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from jarvis.config import ROOT, Settings

HUMANEVAL = ROOT / "data" / "training" / "repos" / "openai_human-eval" / "data" / "HumanEval.jsonl.gz"
HELD_OUT = ROOT / "data" / "training" / "sets" / "python.eval.jsonl"
RESULTS = ROOT / "data" / "training" / "bench"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ask(url: str, model: str, prompt: str) -> str:
    r = httpx.post(f"{url}/api/chat", timeout=300, json={
        "model": model, "stream": False, "keep_alive": "10m",
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 900, "seed": 7},
        "messages": [{"role": "user", "content": prompt}]})
    if r.status_code >= 400:
        # Seen with jarvis-coder: the reply was generated, then Ollama answered 500. Scored as a wrong
        # answer (the coder would fail the same way inside Jarvis), and logged so it can be looked into.
        print(f"  {model}: Ollama {r.status_code} {r.text[:300]!r} for: {prompt[:80]!r}", file=sys.stderr, flush=True)
        ERRORS[model] = ERRORS.get(model, 0) + 1
        return ""
    return r.json()["message"]["content"]


ERRORS: dict[str, int] = {}


def code_of(reply: str) -> str:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", reply, re.S)
    return max(blocks, key=len) if blocks else reply


def runs_ok(program: str, timeout: int = 15) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "prog.py"
        path.write_text(program, encoding="utf-8")
        try:
            done = subprocess.run([sys.executable, "-I", str(path)], cwd=tmp, capture_output=True, timeout=timeout,
                                  creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return False
        return done.returncode == 0


def humaneval(url: str, model: str, limit: int | None) -> tuple[int, int]:
    problems = [json.loads(line) for line in gzip.open(HUMANEVAL, "rt", encoding="utf-8")][:limit]
    passed = 0
    for p in problems:
        reply = ask(url, model, "Complete this Python function. Reply with the whole function (and any imports it "
                                f"needs) in one ```python block.\n\n```python\n{p['prompt']}```")
        code = code_of(reply)
        if f"def {p['entry_point']}" not in code:
            code = p["prompt"] + code
        passed += runs_ok(f"{code}\n\n{p['test']}\ncheck({p['entry_point']})\n")
    return passed, len(problems)


def doctest_program(answer: str, reference: str) -> str | None:
    doc = re.search(r'"""(.*?)"""', code_of(reference), re.S)
    if not doc or ">>>" not in doc.group(1):
        return None
    return (f"{answer}\n\nimport doctest, sys\n_t = doctest.DocTestParser().get_doctest({doc.group(1)!r}, "
            "dict(globals()), 'reference', None, 0)\n_r = doctest.DocTestRunner(optionflags=doctest.ELLIPSIS "
            "| doctest.NORMALIZE_WHITESPACE)\n_r.run(_t, out=lambda s: None)\n"
            "sys.exit(0 if _r.failures == 0 and _r.tries > 0 else 1)\n")


def held_out(url: str, model: str, limit: int | None) -> tuple[int, int]:
    rows = [json.loads(line) for line in HELD_OUT.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if "Write a Python function" in r["instruction"] and ">>>" in r["response"]][:limit]
    passed = total = 0
    for r in rows:
        reply = ask(url, model, r["instruction"] + "\nReply with the function in one ```python block.")
        program = doctest_program(code_of(reply), r["response"])
        if program is None:
            continue
        total += 1
        passed += runs_ok(program)
    return passed, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    url = Settings.from_env().ollama_url
    RESULTS.mkdir(parents=True, exist_ok=True)
    for model in args.models:
        started = time.time()
        he, he_n = humaneval(url, model, args.limit)
        ho, ho_n = held_out(url, model, args.limit)
        result = {"model": model, "humaneval": f"{he}/{he_n}", "humaneval_pct": round(100 * he / max(1, he_n), 1),
                  "held_out_doctests": f"{ho}/{ho_n}", "held_out_pct": round(100 * ho / max(1, ho_n), 1),
                  "ollama_errors": ERRORS.get(model, 0), "minutes": round((time.time() - started) / 60, 1)}
        suffix = f"-limit{args.limit}" if args.limit else ""  # a quick check never overwrites a full run
        (RESULTS / f"{model.replace(':', '_').replace('/', '_')}{suffix}.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
