"""A tiny in-process event bus: the voice loop, the API and the PC tools publish what's
happening ("listening", "you said…", "show the interface"); the desktop interface listens.

Events are plain dicts with a "type": state, heard, reply, level, ui, log.
"""

from __future__ import annotations

import threading
from typing import Callable

Listener = Callable[[dict], None]


class Bus:
    ui_running = False  # set by the desktop interface, so tools know a "show" event will be seen

    def __init__(self):
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def publish(self, type: str, **data) -> None:  # noqa: A002 (event field name)
        event = {"type": type, **data}
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # one broken listener mustn't break the voice loop
                pass


BUS = Bus()
