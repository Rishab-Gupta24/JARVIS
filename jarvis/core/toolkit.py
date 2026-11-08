"""Shared pieces for model-callable tools (calendar, documents, ...)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from jarvis.core.dates import DateParseError

_EMPTY = {"", "none", "null", "n/a", "na", "nil", "unknown"}

# Words that make a request about mess food rather than the calendar.
FOOD = re.compile(r"\b(breakfast|lunch|dinner|supper|snacks?|tiffin|menu|mess|food|eat|eating|meals?)\b", re.I)


class ToolError(Exception):
    """A problem the model should hear about and fix, or ask the user about."""


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    now: datetime
    source: str = "text"
    raw_text: str | None = None
    # Jarvis's previous reply in this conversation, when the user is answering it
    # ("Yes" to "Shall I look up the mess menu?").
    replying_to: str | None = None
    # Set for offline items: events made from them get client_uuid "<key>:1", "<key>:2"...
    # so a crash halfway through can never create them twice.
    client_key: str | None = None
    created: int = 0
    # PC control: only for the user's own voice/typing/phone, never shared messages or files.
    allow_pc: bool = False
    session: str | None = None

    @property
    def today(self) -> date:
        return self.now.date()

    def mentions(self, pattern: re.Pattern) -> bool:
        """Do the user's words, or the question they're answering, match? No words = trust the caller."""
        if self.raw_text is None:
            return True
        return bool(pattern.search(self.raw_text) or (self.replying_to and pattern.search(self.replying_to)))


@dataclass
class ToolResult:
    ok: bool
    data: dict
    # A finished spoken-style answer. When every call in a round has one, it is the
    # reply, word for word, and the model isn't asked to rephrase it.
    confirmation: str | None = None
    mutated: bool = False
    event_ids: list[int] = field(default_factory=list)

    def for_model(self) -> str:
        return json.dumps(self.data, ensure_ascii=False)


class Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        # Small models often send "" or "none" for fields they mean to leave out.
        if isinstance(v, str) and v.strip().lower() in _EMPTY:
            return None
        return v


def string(description: str) -> dict:
    return {"type": "string", "description": description}


def spec(name: str, description: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


Tool = tuple[type[Args], Callable[[Any, ToolContext], ToolResult]]


def run(tools: dict[str, Tool], name: str, raw_args: Any, ctx: ToolContext) -> ToolResult:
    """Run one tool call. Problems come back as an error result for the model, never an exception."""
    entry = tools.get(name)
    if entry is None:
        return ToolResult(False, {"error": f"There is no tool called {name}."})
    model, fn = entry
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            return ToolResult(False, {"error": "The arguments were not valid JSON."})
    try:
        args = model.model_validate(raw_args or {})
        return fn(args, ctx)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'arguments'}: {err['msg']}" for err in e.errors())
        return ToolResult(False, {"error": f"Bad arguments: {problems}"})
    except (ToolError, DateParseError, ValueError, LookupError) as e:
        return ToolResult(False, {"error": str(e)})
