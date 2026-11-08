"""Thin client for Ollama's /api/chat: tool calling, JSON-shaped answers, and images."""

from __future__ import annotations

import base64
import json
import os
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


def default_num_ctx() -> int:
    """Context window for every request. Ollama's default is 4096 tokens, and the game studio's coder prompt
    alone is ~3200 (found 8 Nov 2026): one file read past that and Ollama silently drops the START of the
    prompt, rules included. With Ollama's flash attention and 8-bit KV cache (user environment variables
    OLLAMA_FLASH_ATTENTION=1, OLLAMA_KV_CACHE_TYPE=q8_0) qwen3:8b at 16384 takes 6.3 GB, 7.3 of 8.15 GB with
    Whisper, at the same speed as 8192 (measured 8 Nov 2026). Every client uses the same value, because a
    different num_ctx makes Ollama reload the model."""
    try:
        return max(2048, int(os.getenv("OLLAMA_NUM_CTX", "16384")))
    except ValueError:
        return 16384


VISION_NUM_CTX = 8192  # a photo needs ~1-2k tokens; the 6 GB vision model is tight on memory already


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float = 300.0, keep_alive: str | int = "30m",
                 num_ctx: int | None = None):
        self.model = model
        # A bare number is seconds; -1 keeps the model loaded until Ollama stops.
        self.keep_alive = int(keep_alive) if str(keep_alive).lstrip("-").isdigit() else keep_alive
        self.num_ctx = num_ctx or default_num_ctx()
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._send_think = True  # Qwen3 thinks by default; turning it off makes tool calls much faster

    def _post(self, payload: dict, think: bool = False) -> dict:
        payload = {"model": self.model, "stream": False, "keep_alive": self.keep_alive,
                   "options": {"temperature": 0, "seed": 0, "num_ctx": self.num_ctx}, **payload}
        if self._send_think:
            payload["think"] = think
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
        body = r.json()
        # For speed checks: tokens read (prompt_eval_count; small when Ollama reused its cache) and written.
        self.last_stats = {k: body.get(k) for k in ("prompt_eval_count", "prompt_eval_duration", "eval_count",
                                                    "eval_duration", "total_duration")}
        return body.get("message") or {}

    def chat(self, messages: list[dict], tools: list[dict], think: bool = False) -> dict:
        return self._post({"messages": messages, "tools": tools}, think)

    def json(self, messages: list[dict], schema: dict, think: bool = False) -> dict:
        """think=True lets Qwen3 reason first (slower, more accurate): for decisions, never the tool loop."""
        content = self._post({"messages": messages, "format": schema}, think).get("content", "")
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
            r = self._http.post("/api/chat", json={"model": self.model, "messages": [], "keep_alive": self.keep_alive,
                                                   "options": {"num_ctx": self.num_ctx}})  # same size: no reload
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
