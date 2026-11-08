"""Training data and benchmark plumbing (no GPU, no model)."""

from __future__ import annotations

import json

import pytest

from jarvis.training import bench, datasets


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "REPOS", tmp_path)
    (tmp_path / "algos").mkdir()
    (tmp_path / "algos" / "maths.py").write_text('''"""Simple maths helpers for the examples."""


def add_numbers(a, b):
    """Return the sum of two numbers, for integers and floats alike.

    >>> add_numbers(2, 3)
    5
    """
    return a + b


def _private(x):
    """A private helper that should never become an example."""
    return x


def tiny(x):
    """Too short."""
    return x
''', encoding="utf-8")
    (tmp_path / "algos" / "test_maths.py").write_text('def test_add():\n    """Tests are skipped entirely."""\n')
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "strings.md").write_text(
        "# Searching for strings\n\nUse the `iz` command to list strings in the data section of the binary, "
        "and `izz` to search the whole file. This works on any architecture radare2 supports.\n\n"
        "```\n[0x00000000]> iz\n```\n\n# Tiny\n\nshort\n", encoding="utf-8")
    return tmp_path


def test_python_functions_from_docstrings(repo):
    rows = datasets.python_functions(repo / "algos")
    assert [r["instruction"] for r in rows] == [
        "Write a Python function `add_numbers(a, b)`: Return the sum of two numbers, for integers and floats alike."]
    assert rows[0]["response"].startswith("```python\ndef add_numbers(a, b):") and "return a + b" in rows[0]["response"]
    assert rows[0]["source"].endswith("maths.py")


def test_whole_programs_and_docs_sections(repo):
    files = datasets.python_files(repo / "algos", "Write a Python program")
    assert [r["instruction"] for r in files] == ["Write a Python program: Simple maths helpers for the examples."]
    docs = datasets.markdown_sections(repo / "docs", "radare2")
    assert [r["instruction"] for r in docs] == ["In radare2: Searching for strings. Explain how, with an example."]


def test_write_holds_out_a_share_and_never_overlaps(tmp_path):
    sets = {"python": [{"instruction": f"q{i}", "response": "a", "source": "x"} for i in range(40)]}
    assert datasets.write(sets, tmp_path) == {"python": (38, 2)}
    train = {json.loads(line)["instruction"] for line in (tmp_path / "python.jsonl").read_text().splitlines()}
    held = {json.loads(line)["instruction"] for line in (tmp_path / "python.eval.jsonl").read_text().splitlines()}
    assert len(train) == 38 and len(held) == 2 and not train & held


def test_too_long_examples_are_skipped_not_cut(repo):
    assert datasets._ex("q", "x" * (datasets.MAX_CHARS + 1), repo / "algos" / "maths.py") is None


def test_bench_extracts_code_and_checks_doctests():
    reply = "Sure!\n```python\ndef add_numbers(a, b):\n    return a + b\n```\nDone."
    code = bench.code_of(reply)
    reference = '```python\ndef add_numbers(a, b):\n    """Add.\n\n    >>> add_numbers(2, 3)\n    5\n    """\n```'
    assert bench.runs_ok(bench.doctest_program(code, reference))
    wrong = "def add_numbers(a, b):\n    return a - b\n"
    assert not bench.runs_ok(bench.doctest_program(wrong, reference))
    assert bench.doctest_program(code, "```python\ndef f():\n    pass\n```") is None  # nothing to check against


def test_bench_times_out_runaway_code():
    assert not bench.runs_ok("while True:\n    pass\n", timeout=2)


def test_quantize_shrinks_matrices_and_keeps_metadata(tmp_path):
    from jarvis.training import quantize

    if not (quantize.GGUF_PY / "gguf").exists():
        pytest.skip("llama.cpp's gguf-py isn't in data/training/llama.cpp")
    import sys

    sys.path.insert(0, str(quantize.GGUF_PY))
    import gguf
    import numpy as np

    src, dst = tmp_path / "in.gguf", tmp_path / "out.gguf"
    w = gguf.GGUFWriter(str(src), arch="qwen2")
    w.add_name("tiny")
    w.add_file_type(gguf.LlamaFileType.MOSTLY_F16)
    rng = np.random.default_rng(0)
    w.add_tensor("blk.0.attn_q.weight", rng.standard_normal((8, 64)).astype(np.float16))
    w.add_tensor("output.weight", rng.standard_normal((4, 64)).astype(np.float16))
    w.add_tensor("blk.0.attn_norm.weight", np.ones(64, dtype=np.float32))
    w.add_tensor("blk.0.attn_q.bias", np.zeros(8, dtype=np.float32))
    w.write_header_to_file(); w.write_kv_data_to_file(); w.write_tensors_to_file(); w.close()

    report = quantize.quantize_file(src, dst)
    assert report["tensors"] == {"Q4_1": 1, "Q8_0": 1, "kept": 2}
    r = gguf.GGUFReader(str(dst))
    types = {t.name: t.tensor_type.name for t in r.tensors}
    assert types == {"blk.0.attn_q.weight": "Q4_1", "output.weight": "Q8_0",
                     "blk.0.attn_norm.weight": "F32", "blk.0.attn_q.bias": "F32"}
    assert r.fields["general.name"].contents() == "tiny"
    assert r.fields["general.file_type"].contents() == int(gguf.LlamaFileType.MOSTLY_Q4_1)
    q = next(t for t in r.tensors if t.name == "blk.0.attn_q.weight")
    back = gguf.quants.dequantize(q.data, q.tensor_type).reshape(8, 64)
    orig = next(t for t in gguf.GGUFReader(str(src)).tensors if t.name == "blk.0.attn_q.weight").data
    assert np.abs(back - orig.astype(np.float32)).max() < 0.5  # 4-bit rounding, not garbage


def test_functions_carry_their_imports_and_half_are_stub_completions(tmp_path, monkeypatch):
    monkeypatch.setattr(datasets, "REPOS", tmp_path)
    (tmp_path / "geo.py").write_text('''from __future__ import annotations

import math
from itertools import combinations
from typing import List


def closest_pair_distance(points: List[float]) -> float:
    """Return the smallest distance between any two of the given points on a line.

    >>> closest_pair_distance([1.0, 4.0, 4.5])
    0.5
    """
    return min(math.fabs(a - b) for a, b in combinations(points, 2))
''', encoding="utf-8")
    monkeypatch.setattr(datasets, "_completion_turn", lambda path, name: True)
    [row] = datasets.python_functions(tmp_path)
    assert row["instruction"].startswith("Finish the implementation of this Python function:\n\n```python\n"
                                         "def closest_pair_distance(")
    assert "import" not in row["instruction"] and "return min" not in row["instruction"]
    assert row["response"].startswith("```python\nimport math\nfrom itertools import combinations\n"
                                      "from typing import List\n\n\ndef closest_pair_distance(")
    assert "__future__" not in row["response"] and row["response"].endswith("combinations(points, 2))\n```")
    monkeypatch.setattr(datasets, "_completion_turn", lambda path, name: False)
    [row] = datasets.python_functions(tmp_path)
    assert row["instruction"].startswith("Write a Python function `closest_pair_distance(points)`")
