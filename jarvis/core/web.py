"""Looking things up on the internet: definitions, facts, anything Jarvis doesn't know.

Only the search words leave the PC (DuckDuckGo, then the page it points to;
Wikipedia pages are read through Wikipedia's own summary API, which gives a clean
paragraph). Nothing about the user goes out. Offered only for the user's own
requests (like PC control): a shared message could otherwise steer a search.
The model phrases the answer from the excerpt and names the source.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx
from pydantic import Field

from jarvis.core.toolkit import Args, Tool, ToolContext, ToolError, ToolResult, spec, string

UA = "JarvisPersonalAssistant/1.0 (local voice assistant)"


def user_agent() -> str:
    """An honest client name. Wikipedia only serves clients that give a contact (JARVIS_WEB_CONTACT in .env,
    e.g. a GitHub page); without one Jarvis uses Wikipedia's search snippet instead of its summary."""
    import os

    contact = os.getenv("JARVIS_WEB_CONTACT", "").strip()
    return f"{UA[:-1]}; {contact})" if contact else UA


def _client() -> httpx.Client:
    return httpx.Client(timeout=12, follow_redirects=True, headers={"User-Agent": user_agent()})
NAMES = {"look_up"}
_SKIP_SITES = re.compile(r"youtube\.com|youtu\.be|tiktok\.com|instagram\.com|facebook\.com|pinterest\.|"
                         r"twitter\.com|x\.com/|reddit\.com/media", re.I)
EXCERPT = 1800


@dataclass(frozen=True)
class Result:
    title: str
    url: str
    snippet: str


def search(query: str, http: httpx.Client | None = None, limit: int = 6) -> list[Result]:
    client = http or _client()
    r = client.post("https://html.duckduckgo.com/html/", data={"q": query})
    r.raise_for_status()
    found = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                         r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S):
        url = m.group(1)
        if "uddg=" in url:  # DuckDuckGo's redirect link -> the real address
            url = unquote(parse_qs(urlparse(url).query).get("uddg", [url])[0])
        if "duckduckgo.com/y.js" in url or "ad_domain" in url:  # adverts
            continue
        title, snippet = (html.unescape(re.sub("<.*?>", "", g)).strip() for g in (m.group(2), m.group(3)))
        found.append(Result(title, url, snippet))
        if len(found) >= limit:
            break
    return found


def wikipedia_summary(url: str, http: httpx.Client) -> str | None:
    m = re.match(r"https?://(\w+)\.(?:m\.)?wikipedia\.org/wiki/([^#?]+)", url)
    if not m:
        return None
    r = http.get(f"https://{m[1]}.wikipedia.org/api/rest_v1/page/summary/{quote(unquote(m[2]), safe='')}")
    if r.status_code != 200:
        return None
    return r.json().get("extract") or None


def page_excerpt(url: str, query: str, http: httpx.Client) -> str:
    """The part of a page most about the question: paragraphs with the most query words, in page order."""
    from jarvis.coder.tools import page_text

    r = http.get(url)
    r.raise_for_status()
    if "html" not in r.headers.get("content-type", "html"):
        return r.text[:EXCERPT]
    paras = [p.strip() for p in page_text(r.text).split("\n") if len(p.strip()) > 60]
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    scored = sorted(range(len(paras)), key=lambda i: -sum(w in paras[i].lower() for w in words))
    keep, size = [], 0
    for i in scored:
        if size > EXCERPT:
            break
        keep.append(i)
        size += len(paras[i])
    return "\n".join(paras[i] for i in sorted(keep))[:EXCERPT]


_DEFINE = re.compile(r"^(?:define|definition of|meaning of|what does)?\s*([a-z][a-z'-]{1,40})"
                     r"(?:\s+(?:meaning|mean|means|definition|defined))?\??$", re.I)


def definition(query: str, http: httpx.Client) -> str | None:
    """A one-word 'what does X mean' from the Free Dictionary API (dictionaryapi.dev), two senses at most."""
    m = _DEFINE.match(query.strip())
    if not m or m[1].lower() in ("what", "who", "how", "why", "when", "where"):
        return None
    r = http.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(m[1].lower())}")
    if r.status_code != 200:
        return None
    senses = []
    for entry in r.json()[:1]:
        for meaning in entry.get("meanings", [])[:2]:
            for d in meaning.get("definitions", [])[:1]:
                example = f" Example: {d['example']}" if d.get("example") else ""
                senses.append(f"{m[1]} ({meaning.get('partOfSpeech', '')}): {d.get('definition', '')}{example}")
    return "\n".join(senses) or None


def look_up_web(query: str, http: httpx.Client | None = None) -> dict:
    http = http or _client()
    try:
        if meaning := definition(query, http):
            return {"query": query, "found": True, "source": "the dictionary", "excerpt": meaning}
    except (httpx.HTTPError, ValueError):
        pass
    results = search(query, http)
    if not results:
        return {"query": query, "found": False}
    snippets = [f"{r.title}: {r.snippet}" for r in results[:4]]
    readable = [r for r in results if not _SKIP_SITES.search(r.url)]
    wiki = next((r for r in readable if "wikipedia.org/wiki/" in r.url), None)
    for r in ([wiki] if wiki else []) + [r for r in readable[:2] if r is not wiki]:
        try:
            text = wikipedia_summary(r.url, http) if r is wiki else page_excerpt(r.url, query, http)
        except httpx.HTTPError:
            text = None
        if r is wiki and not text:
            text = r.snippet  # summary refused (no contact set): the search snippet is its first lines
        if text:
            host = urlparse(r.url).netloc.removeprefix("www.")
            return {"query": query, "found": True, "source": "Wikipedia" if r is wiki else host,
                    "excerpt": text, "other_results": snippets}
    return {"query": query, "found": True, "source": "search results", "other_results": snippets}


class LookUpArgs(Args):
    query: str = Field(min_length=2, max_length=150)


def look_up(args: LookUpArgs, ctx: ToolContext) -> ToolResult:
    if not ctx.allow_pc:
        raise ToolError("Looking things up online is only for the user's own questions.")
    try:
        data = look_up_web(args.query)
    except httpx.HTTPError:
        return ToolResult(True, {"found": False}, "I couldn't reach the internet just now.")
    if not data["found"]:
        return ToolResult(True, data, f"I couldn't find anything online about {args.query}.")
    return ToolResult(True, data)  # the model answers from the excerpt


SPECS = [
    spec("look_up", "Search the internet for something you don't know or can't see: a definition ('what does "
         "ephemeral mean?'), a fact, a person, a place, how something works, today's news. Not for the user's "
         "calendar, menus or files.",
         {"query": string("A short search query with only the topic, e.g. 'ephemeral meaning'. Never put the "
                          "user's private details (events, menus, file contents, names of people they know) in it.")},
         ("query",)),
]
TOOLS: dict[str, Tool] = {"look_up": (LookUpArgs, look_up)}
