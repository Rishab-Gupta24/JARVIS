"""The agent loop: text in, tool calls out, a short spoken-style reply back."""

from __future__ import annotations

import re
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jarvis.config import TZ
from jarvis.core import tools
from jarvis.core.dates import now_local
from jarvis.core.llm import ChatModel
from jarvis.core.toolkit import FOOD
from jarvis.documents import store as doc_store

SYSTEM_PROMPT = """You are Jarvis, a voice assistant that manages the user's personal calendar.
Right now it is {now:%A %d %B %Y, %H:%M} in the user's time zone ({tz}).

How to work:
- To add, change, move, cancel or flag an event, call the matching tool. Never say you did something unless a tool did it.
- Dates and times: copy the user's own words into the tool arguments, for example date="next friday", start_time="2pm", end_time="3pm". Do not work out calendar dates yourself; the tool does that.
- Set important=true only when the user asks for it: important, urgent, "don't let me forget", "remind me". A deadline or due date on its own is not important; leave important out.
- Titles are short and contain no date or time words: "DBMS quiz", "Dentist appointment".
- To change or delete an existing event, identify it with match_title (words from its title). Add match_date only if the user named the day; never guess one.
- For questions about the schedule ("what's on tomorrow", "what do I have on Friday", "am I free"), call list_events with the period the user asked about, then answer only from the result.
- For "when is X?" (a festival, holiday or one of the user's events), call find_event. The calendar already holds festivals and public holidays for India and the UAE; never give a date from your own knowledge.
- For anything about mess food (breakfast, lunch, snacks, dinner, what's to eat), call get_menu or find_in_menu. Never answer about food from your own knowledge.
- For questions about other files the user sent (notices, timetables, exam schedules), call search_documents and answer only from the excerpts.
- If a tool returns an error, fix the arguments and call it again, or ask the user one short question.
- If something is outside the calendar and the user's files (general knowledge, news), say briefly that you don't know. Don't offer unrelated lookups.
- Reply in one or two short spoken sentences. No markdown, no lists, no emojis.{extra}"""

MAX_STEPS = 5
_FOOD = FOOD
_NUDGE = ("That answer did not come from a tool. Answers about food must come from get_menu or "
          "find_in_menu. Call the right one now with the user's own day words.")
NO_GUESSING = "I can only tell you what's on the menus you've sent me. Ask me about a day, like 'what's for dinner on Friday?'"


@dataclass
class Reply:
    text: str
    actions: list[dict] = field(default_factory=list)  # one entry per tool call


class SessionMemory:
    """Keeps the last few exchanges per session, so "which one?" follow-ups work."""

    def __init__(self, max_turns: int = 3, ttl: timedelta = timedelta(minutes=10)):
        self._max = max_turns
        self._ttl = ttl
        self._data: dict[str, deque] = {}
        self._lock = threading.Lock()

    def history(self, session: str, now: datetime) -> list[dict]:
        with self._lock:
            turns = [t for t in self._data.get(session, ()) if now - t[0] <= self._ttl]
        messages = []
        for _, user, reply in turns:
            messages += [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]
        return messages

    def add(self, session: str, now: datetime, user: str, reply: str) -> None:
        with self._lock:
            self._data.setdefault(session, deque(maxlen=self._max)).append((now, user, reply))


def _clean_reply(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"[*_`#]", "", text)
    return " ".join(text.split())


def handle(
    text: str,
    conn: sqlite3.Connection,
    llm: ChatModel,
    now: datetime | None = None,
    source: str = "text",
    session: str | None = None,
    memory: SessionMemory | None = None,
    shared: bool = False,
) -> Reply:
    """Run one user request through the model and tools. Raises LLMError if the model is unreachable.

    shared=True: `text` is a message the user forwarded from another app (WhatsApp, email).
    The model is told to add the events it mentions; the guards still see only the message.
    """
    now = now or now_local()
    said = text
    if shared:
        said = clean_shared(text)
        text = SHARED_PROMPT.format(message=said)
    ctx = tools.ToolContext(conn=conn, now=now, source=source, raw_text=said)
    extra = ""
    if waiting := doc_store.waiting_for_week(conn):
        doc = waiting[0]
        extra = (f"\n\nThe user recently sent a mess menu ('{doc.filename}', document {doc.id}) and was asked "
                 "which week it is for. Only if the user is now telling you that week (or that it repeats), "
                 "call set_menu_week. Food questions still go to get_menu.")
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(now=now, tz=TZ.key, extra=extra)}]
    if memory is not None and session:
        history = memory.history(session, now)
        messages += history
        if history and history[-1]["content"].rstrip().endswith("?"):
            ctx.replying_to = history[-1]["content"]  # the user is answering Jarvis's question
    messages.append({"role": "user", "content": text})

    actions: list[dict] = []
    confirmations: list[str] = []
    reply = ""
    food_question = not shared and bool(_FOOD.search(said))  # "dinner party at 8" in a shared message is an event
    nudged = False
    for _ in range(MAX_STEPS):
        msg = llm.chat(messages, tools.TOOL_SPECS)
        calls = msg.get("tool_calls") or []
        if not calls:
            if food_question and not actions:
                # The model answered a food question without looking anything up, which
                # means it made the dishes up. Send it back once; never pass a guess on.
                if not nudged:
                    nudged = True
                    messages.append({"role": "assistant", "content": msg.get("content", "")})
                    messages.append({"role": "system", "content": _NUDGE})
                    continue
                reply = NO_GUESSING
                break
            reply = _clean_reply(msg.get("content", "")) or " ".join(confirmations)
            break
        messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": calls})
        round_finished = True
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            result = tools.execute(name, fn.get("arguments"), ctx)
            actions.append({"tool": name, "arguments": fn.get("arguments"), "ok": result.ok, "result": result.data})
            messages.append({"role": "tool", "tool_name": name, "content": result.for_model()})
            if result.ok and result.confirmation:
                confirmations.append(result.confirmation)
            else:
                round_finished = False
        if round_finished:
            # Every call succeeded with a finished answer (an exact read-back of a change,
            # or a menu straight from stored rows): that is the reply, word for word, and
            # it saves a second trip to the model.
            reply = " ".join(confirmations)
            break
    else:
        reply = " ".join(confirmations) or "Sorry, I couldn't finish that. Could you say it another way?"

    reply = reply or "Sorry, I didn't catch that."
    if memory is not None and session:
        memory.add(session, now, f"(shared a message) {said}" if shared else said, reply)
    return Reply(reply, actions)


# Shared messages ------------------------------------------------------------------

# Its wording avoids the guards' cue words (important, remind, until, ...): the guards
# look only at the message, but the prompt shouldn't suggest those either.
SHARED_PROMPT = """I'm forwarding you a message from another app (like WhatsApp or email).
Add every event, deadline or plan it mentions to my calendar: one create_event call each, copying the day and time words exactly as the message writes them. Use a short title for each. If the message mentions no event, say so in one short sentence and add nothing.

Message:
\"\"\"
{message}
\"\"\""""

# WhatsApp copies look like "[06/11/26, 7:42 PM] Rahul: ..." (iOS) or "06/11/2026, 19:42 - Rahul: ..."
# (Android export). That timestamp is when it was sent, not the event's date, so it goes.
_CHAT_PREFIX = re.compile(
    r"^\s*\[?\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4},?\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?\]?\s*(?:-\s*)?"
    r"(?:[^:\n]{1,40}:\s*)?",
    re.I | re.M,
)


def clean_shared(text: str) -> str:
    text = _CHAT_PREFIX.sub("", text.replace("\r\n", "\n"))
    text = re.sub(r"^\s*(Forwarded|<Media omitted>|This message was deleted)\s*$", "", text, flags=re.I | re.M)
    return "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())[:4000]
