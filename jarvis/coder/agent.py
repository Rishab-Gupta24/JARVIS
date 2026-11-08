"""The coding loop: the local model plans, calls tools, writes files, and says when it's done."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from jarvis.coder.tools import SPECS, ToolProblem, Workspace

MAX_STEPS = 40
SYSTEM = """You are Jarvis's coding agent, working in the project folder {root} on Windows with Python 3.12.
Do the user's task completely:
1. Look at what's in the folder (list_files) and read files you will change.
2. If you're unsure how an API or library works, check search_references first (trusted repos), then
   search_web and read_webpage for official documentation.
3. Change existing files with replace_in_file (copy the exact current text from read_file); write new files
   complete with write_file (never placeholders like "rest of code here").
4. Test Python with run_python when it makes sense, fix errors you see.
5. Finish with a short summary of what you made and how to run it. No tool call in that last message.
Only standard-library Python unless the task needs a package; say which packages to install."""


_FENCE = re.compile(r"```(?:json|tool_call)?\s*|</?tool_call>")
KEEP_FULL = 3  # the latest tool results stay whole; older ones shrink to a line


def compact(messages: list[dict], keep: int = KEEP_FULL) -> None:
    """Keep the conversation inside the model's context: tool results older than the last few are cut to
    their first line, and old file contents written by the model are dropped (the file is on disk). Without
    this a few file reads pushed the system prompt (the rules) out of the window."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-keep]:
        text = messages[i]["content"]
        if len(text) > 300:
            messages[i] = {**messages[i], "content": text.splitlines()[0][:200] + " … (older result shortened)"}
    for m in messages[:-6]:
        for call in m.get("tool_calls") or []:
            args = call.get("function", {}).get("arguments")
            if isinstance(args, dict):
                short = {k: "(written to the file; read it again if needed)" for k in ("content", "old", "new")
                         if len(str(args.get(k, ""))) > 400}
                if short:
                    call["function"]["arguments"] = {**args, **short}


def calls_in_text(content: str, allowed: set[str]) -> list[dict]:
    """Tool calls a model wrote as JSON text: {"name": ..., "arguments": {...}}, alone, in ``` fences or
    <tool_call> tags, one or several. Only names of offered tools count, so ordinary JSON in an answer
    isn't mistaken for a call."""
    text = _FENCE.sub("\n", content)
    decoder, calls, i = json.JSONDecoder(), [], 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        i = end
        if isinstance(obj, dict) and obj.get("name") in allowed:
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"function": {"name": obj["name"], "arguments": args if isinstance(args, dict) else {}}})
    return calls


def run(task: str, root: Path, llm, confirm: Callable[[str], bool], say: Callable[[str], None] = print,
        max_steps: int = MAX_STEPS, system: str | None = None, workspace: Workspace | None = None,
        specs: list[dict] | None = None) -> str:
    """The tool loop. The studio's roles pass their own system prompt, workspace (extra tools, write
    rules) and tool list; the plain coder uses the defaults plus the skills that match the task."""
    ws = workspace or Workspace(root, confirm)
    specs = specs if specs is not None else SPECS
    if system is None:
        from jarvis.skills import select

        system = SYSTEM.format(root=root) + select(task, "coder")
    allowed = {s["function"]["name"] for s in specs}
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    for _ in range(max_steps):
        compact(messages)
        msg = llm.chat(messages, specs)
        calls = msg.get("tool_calls") or []
        if not calls and (found := calls_in_text(msg.get("content") or "", allowed)):
            # qwen2.5-coder writes its tool calls as JSON in the reply instead of Ollama's tool_calls
            # (found 7 Nov 2026: the studio's tester and coder "finished" without writing anything)
            calls, msg = found, {**msg, "content": ""}
        if msg.get("content"):
            say(msg["content"].strip())
        messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": calls})
        if not calls:
            return msg.get("content", "").strip()
        for call in calls:
            fn = call.get("function", {})
            name, args = fn.get("name", ""), fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            shown = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items() if k != "content")
            say(f"  → {name}({shown})")
            try:
                tool = getattr(ws, name, None) if name in allowed else None
                if tool is None:
                    raise ToolProblem(f"There is no tool called {name}.")
                result = tool(**args)
            except ToolProblem as e:
                result = f"Problem: {e}"
            except TypeError as e:
                result = f"Problem with the arguments: {e}"
            except Exception as e:  # network down, page missing...
                result = f"Failed: {e!r}"
            messages.append({"role": "tool", "content": str(result)[:8000], "tool_name": name})
    return "I stopped after the maximum number of steps; the files so far are in the folder."
