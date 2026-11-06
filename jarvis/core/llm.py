"""Thin client for Ollama's /api/chat: tool calling, JSON-shaped answers, and images."""

from __future__ import annotations

import base64
import json
from typing import Protocol

import httpx


class LLMError(RuntimeError):
    """The language model could not be reached or returned an error."""


class ChatModel(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """Return the assistant message: {"role", "content", "tool_calls"?}."""
        ...


class JsonModel(Protocol):
    def json(self, messages: list[dict], schema: dict) -> dict:
        """Return the model's answer, constrained to the JSON schema."""
        ...


class Transcriber(Protocol):
    def transcribe(self, image: bytes) -> str:
        """Return the text in an image, with tables as Markdown tables."""
        ...


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float = 300.0, keep_alive: str | int = "30m"):
        self.model = model
        # A bare number is seconds; -1 keeps the model loaded until Ollama stops.
        self.keep_alive = int(keep_alive) if str(keep_alive).lstrip("-").isdigit() else keep_alive
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._send_think = True  # Qwen3 thinks by default; turning it off makes tool calls much faster

    def _post(self, payload: dict) -> dict:
        payload = {"model": self.model, "stream": False, "keep_alive": self.keep_alive,
                   "options": {"temperature": 0, "seed": 0}, **payload}
        if self._send_think:
            payload["think"] = False
        try:
            r = self._http.post("/api/chat", json=payload)
            if r.status_code == 400 and self._send_think and "think" in r.text.lower():
                self._send_think = False  # model without a thinking switch
                payload.pop("think")
                r = self._http.post("/api/chat", json=payload)
        except httpx.HTTPError as e:
            raise LLMError(f"Can't reach Ollama at {self._http.base_url}: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama returned {r.status_code}: {r.text[:300]}")
        return r.json().get("message") or {}

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        return self._post({"messages": messages, "tools": tools})

    def json(self, messages: list[dict], schema: dict) -> dict:
        content = self._post({"messages": messages, "format": schema}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"The model didn't return valid JSON: {content[:200]}") from e

    def transcribe(self, image: bytes) -> str:
        prompt = (
            "Transcribe all the text in this image exactly as written. If it contains a table, "
            "output it as a Markdown table with the same rows and columns, one table row per line. "
            "Copy every word exactly; do not translate, summarise, correct, or add anything."
        )
        message = {"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}
        return self._post({"messages": [message]}).get("content", "")

    def warm(self) -> None:
        """Load the model now (an empty chat loads it), so the first real request isn't slow."""
        try:
            r = self._http.post("/api/chat", json={"model": self.model, "messages": [], "keep_alive": self.keep_alive})
        except httpx.HTTPError as e:
            raise LLMError(f"Can't reach Ollama at {self._http.base_url}: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"Ollama returned {r.status_code}: {r.text[:300]}")

    def status(self) -> str | None:
        """None when Ollama is up and the model is pulled, otherwise what's wrong."""
        try:
            r = self._http.get("/api/tags", timeout=5)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return f"Ollama isn't reachable at {self._http.base_url} ({e.__class__.__name__}). Is it running?"
        names = {m.get("name") for m in r.json().get("models", [])}
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in names:
            return f"Model {self.model} isn't pulled yet. Run: ollama pull {self.model}"
        return None
