"""Publish to the ntfy server running on this PC.

Jarvis -> ntfy on 127.0.0.1 -> (content-free poll request) ntfy.sh -> Apple push
-> iPhone ntfy app -> fetches the message from this PC over Tailscale.
"""

from __future__ import annotations

import httpx

PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


class NtfyError(RuntimeError):
    pass


class NtfySender:
    def __init__(self, url: str, topic: str, token: str, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None):
        if not (url and topic and token):
            raise ValueError("NTFY_URL, NTFY_TOPIC and NTFY_TOKEN must all be set.")
        self.url, self.topic = url.rstrip("/"), topic
        self._http = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {token}"},
                                  transport=transport)

    def publish(self, title: str, message: str, priority: str = "default", tags: list[str] | None = None) -> str:
        """Returns the message id. Raises NtfyError when the server says no or isn't there."""
        body = {"topic": self.topic, "title": title, "message": message,
                "priority": PRIORITY.get(priority, 3), "tags": tags or []}
        try:
            r = self._http.post(self.url, json=body)
        except httpx.HTTPError as e:
            raise NtfyError(f"Can't reach the ntfy server at {self.url}: {e}") from e
        if r.status_code != 200:
            raise NtfyError(f"ntfy said {r.status_code}: {r.text[:200]}")
        return r.json().get("id", "")
