"""Jarvis's local coding agent: the loop, the folder fence, confirmations, references, web parsing."""

from __future__ import annotations

import pytest

from jarvis.coder import agent, tools
from jarvis.coder.tools import ToolProblem, Workspace


class Script:
    """A fake model that replies with the given messages in order."""

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages, specs):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0)


def call(name, **args):
    return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def test_agent_writes_files_and_finishes(tmp_path):
    llm = Script(call("list_files"), call("write_file", path="game/snake.py", content="print('snake')\n"),
                 {"content": "Made game/snake.py. Run it with python game/snake.py."})
    said = []
    out = agent.run("make snake", tmp_path, llm, confirm=lambda q: True, say=said.append)
    assert out.startswith("Made game/snake.py")
    assert (tmp_path / "game" / "snake.py").read_text() == "print('snake')\n"
    assert any("write_file" in s for s in said)
    tool_msgs = [m for m in llm.seen[-1] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "(empty folder)" and tool_msgs[1]["content"].startswith("Wrote game/snake.py")


def test_files_outside_the_project_are_refused(tmp_path):
    ws = Workspace(tmp_path / "proj", confirm=lambda q: True)
    (tmp_path / "proj").mkdir()
    for bad in ("../secret.txt", "C:/Windows/win.ini", "..\\..\\x.py"):
        with pytest.raises(ToolProblem, match="outside the project"):
            ws.write_file(bad, "x")
    assert not (tmp_path / "secret.txt").exists()


def test_overwrite_and_run_ask_first(tmp_path):
    asked = []
    ws = Workspace(tmp_path, confirm=lambda q: asked.append(q) or False)
    ws.write_file("a.py", "print(1)\n")  # new file: no question
    assert asked == []
    assert "said no" in ws.write_file("a.py", "print(2)\n")
    assert (tmp_path / "a.py").read_text() == "print(1)\n"
    assert "said no" in ws.run_python("a.py")
    assert asked == ["Overwrite a.py?", "Run python a.py?"]
    yes = Workspace(tmp_path, confirm=lambda q: True)
    assert yes.run_python("a.py").startswith("Exit code 0.\n1")


def test_bad_tool_calls_are_told_to_the_model(tmp_path):
    llm = Script(call("delete_everything"), call("read_file", path="nope.py"), {"content": "done"})
    agent.run("x", tmp_path, llm, confirm=lambda q: True, say=lambda s: None)
    results = [m["content"] for m in llm.seen[-1] if m["role"] == "tool"]
    assert results == ["Problem: There is no tool called delete_everything.", "Problem: nope.py doesn't exist."]


def test_reference_search(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "REFERENCES", tmp_path)
    (tmp_path / "games").mkdir()
    (tmp_path / "games" / "snake.py").write_text("import turtle\n# snake game: the snake grows when it eats food\n")
    (tmp_path / "other.md").write_text("nothing relevant here")
    out = Workspace(tmp_path, confirm=lambda q: True).search_references("snake turtle")
    assert out.startswith("--- games/snake.py") and "other.md" not in out


def test_web_results_and_page_text(monkeypatch):
    page = ('<div><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org'
            '%2F3%2Flibrary%2Fturtle.html&rut=x">turtle &mdash; Turtle graphics</a>'
            '<a class="result__snippet" href="x">The <b>turtle</b> module provides...</a></div>')

    class R:
        text, headers = page, {"content-type": "text/html"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(tools.httpx, "post", lambda *a, **k: R())
    out = Workspace(tools.ROOT, confirm=lambda q: True).search_web("python turtle")
    assert out == "turtle — Turtle graphics\nhttps://docs.python.org/3/library/turtle.html\nThe turtle module provides..."
    assert tools.page_text("<html><script>x()</script><p>Hello</p><p>World</p></html>") == "Hello\nWorld"
    with pytest.raises(ToolProblem):
        Workspace(tools.ROOT, confirm=lambda q: True).read_webpage("file:///C:/Windows/win.ini")


def test_tool_calls_written_as_text_are_understood():
    from jarvis.coder.agent import calls_in_text

    allowed = {"write_file", "read_file"}
    plain = '{"name": "write_file", "arguments": {"path": "a.py", "content": "x = {1: 2}\\n"}}'
    assert calls_in_text(plain, allowed) == [{"function": {"name": "write_file",
                                                            "arguments": {"path": "a.py", "content": "x = {1: 2}\n"}}}]
    fenced = 'Sure:\n```json\n{"name": "read_file", "arguments": "{\\"path\\": \\"b.py\\"}"}\n```'
    assert calls_in_text(fenced, allowed)[0]["function"]["arguments"] == {"path": "b.py"}
    tagged = '<tool_call>{"name": "read_file", "arguments": {"path": "a"}}</tool_call>\n' \
             '<tool_call>{"name": "write_file", "arguments": {"path": "b", "content": ""}}</tool_call>'
    assert [c["function"]["name"] for c in calls_in_text(tagged, allowed)] == ["read_file", "write_file"]
    assert calls_in_text('{"name": "delete_everything", "arguments": {}}', allowed) == []
    assert calls_in_text('The config is {"debug": true}. Done.', allowed) == []


def test_agent_follows_tool_calls_written_as_text(tmp_path):
    # qwen2.5-coder's habit (7 Nov 2026): the call comes back as JSON in the reply, tool_calls empty
    llm = Script({"content": '{"name": "write_file", "arguments": {"path": "ball.py", "content": "class Ball: pass\\n"}}'},
                 {"content": "Made ball.py."})
    out = agent.run("make a ball", tmp_path, llm, confirm=lambda q: True, say=lambda s: None)
    assert (tmp_path / "ball.py").read_text() == "class Ball: pass\n" and out == "Made ball.py."
    assert [m for m in llm.seen[-1] if m["role"] == "tool"][0]["content"].startswith("Wrote ball.py")


def test_old_tool_results_are_shortened_to_stay_in_the_context_window():
    from jarvis.coder.agent import compact

    big = "line one of a long file\n" + "x" * 5000
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    for i in range(5):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "write_file", "arguments": {"path": f"f{i}.py", "content": "y" * 2000}}}]})
        messages.append({"role": "tool", "content": big, "tool_name": "read_file"})
    compact(messages)
    tools = [m["content"] for m in messages if m["role"] == "tool"]
    assert all(t.endswith("(older result shortened)") and t.startswith("line one") for t in tools[:2])
    assert tools[2:] == [big] * 3  # the latest three stay whole
    written = [m["tool_calls"][0]["function"]["arguments"]["content"] for m in messages if m.get("tool_calls")]
    assert written[0].startswith("(written to the file") and written[-1] == "y" * 2000
    assert messages[0]["content"] == "rules"  # the system prompt is never touched


def test_every_ollama_request_uses_one_bigger_context(monkeypatch):
    from jarvis.core import llm

    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
    client = llm.OllamaClient("http://127.0.0.1:1", "qwen3:8b")
    sent = {}

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"message": {"content": "{}"}}

    monkeypatch.setattr(client._http, "post", lambda url, json: sent.update(json) or R())
    client.json([{"role": "user", "content": "hi"}], {"type": "object"})
    assert sent["options"]["num_ctx"] == 16384 and sent["think"] is False


def test_thinking_is_off_unless_asked_and_the_default_context_is_16k(monkeypatch):
    from jarvis.core import llm

    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    assert llm.default_num_ctx() == 16384 and llm.VISION_NUM_CTX == 8192
    client = llm.OllamaClient("http://127.0.0.1:1", "qwen3:8b")
    sent = []

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"message": {"content": "{}"}}

    monkeypatch.setattr(client._http, "post", lambda url, json: sent.append(json) or R())
    client.json([{"role": "user", "content": "plan"}], {"type": "object"}, think=True)
    client.chat([{"role": "user", "content": "go"}], [])
    assert [s["think"] for s in sent] == [True, False] and sent[0]["options"]["num_ctx"] == 16384


def test_replace_in_file_changes_one_exact_unique_piece(tmp_path):
    ws = Workspace(tmp_path, confirm=lambda q: True, refuse_write=lambda rel: "tests are fixed" if rel.startswith("tests/") else None)
    (tmp_path / "main.js").write_text("export function reveal(Observer, root) {\n  return 1;\n}\nconst a = 1;\nconst a2 = 1;\n")
    assert ws.replace_in_file("main.js", "reveal(Observer, root)", "reveal(root, Observer)") == "Replaced 1 line(s) in main.js."
    assert "reveal(root, Observer)" in (tmp_path / "main.js").read_text()
    with pytest.raises(ToolProblem, match="appears 2 times"):
        ws.replace_in_file("main.js", "= 1;", "= 2;")
    with pytest.raises(ToolProblem, match="isn't in main.js"):
        ws.replace_in_file("main.js", "function nothing()", "x")
    with pytest.raises(ToolProblem, match="doesn't exist"):
        ws.replace_in_file("other.js", "a", "b")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.mjs").write_text("x")
    with pytest.raises(ToolProblem, match="tests are fixed"):
        ws.replace_in_file("tests/t.mjs", "x", "y")
    assert "replace_in_file" in {s["function"]["name"] for s in tools.SPECS}
