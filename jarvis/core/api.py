"""HTTP API. Binds to localhost; reach it from the phone through `tailscale serve`."""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Callable, Iterator

from fastapi import (APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response,
                     UploadFile)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from pathlib import Path

from jarvis.calendar import db, holidays, ics, repo
from jarvis.calendar.repo import local_midnight
from jarvis.config import TZ, Settings
from jarvis.core import agent
from jarvis.core.dates import DateParseError, now_local
from jarvis.core.llm import VISION_NUM_CTX, ChatModel, LLMError, OllamaClient, Transcriber
from jarvis.documents import store as doc_store
from jarvis.documents.ingest import MAX_BYTES, InboxWatcher, ingest
from jarvis.documents.readers import UnreadableFile, sniff
from jarvis.core.share import is_image, share_file, share_text
from jarvis.notify.app import push_from, start_alarms, start_reminders


class CaptureIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=100)
    source: str = Field(default="phone", max_length=30)
    shared: bool = False  # a forwarded message: add the events it mentions


class CaptureOut(BaseModel):
    reply: str
    actions: list[dict]


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    start: str  # ISO date or datetime; no offset means Asia/Dubai
    end: str | None = None
    all_day: bool = False
    location: str | None = None
    notes: str | None = None
    important: bool = False
    client_uuid: str | None = Field(default=None, max_length=100)


class EventPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    start: str | None = None
    end: str | None = None
    all_day: bool | None = None
    location: str | None = None
    notes: str | None = None
    important: bool | None = None


class _HideFeedToken(logging.Filter):
    """The calendar feed's token is in its address; keep it out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            args[2] = re.sub(r"/calendar/[^/?\s]+\.ics", "/calendar/<feed-token>.ics", str(args[2]))
            record.args = tuple(args)
        return True


def _parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"Not an ISO date or datetime: {value!r}") from None
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def create_app(
    settings: Settings | None = None,
    llm: ChatModel | None = None,
    clock: Callable[[], datetime] = now_local,
    vision: Transcriber | None = None,
    watch_inbox: bool = True,
    fetch_holidays: bool = True,
    send_reminders: bool = True,
    stt=None,  # speech to text for the web app's mic; loaded on first use when None
    voice=None,  # Jarvis's voice for /speak; loaded on first use when None
    reminder_say: Callable[[str], None] | None = None,  # `jarvis up`: also say reminders aloud on the PC
    alarm_ring=None,  # `jarvis up`: ring alarms on the PC speakers (alarms.ring_with)
    on_exchange: Callable[[str, str, str], None] | None = None,  # (source, heard, reply) for the interface
) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.token:
        raise RuntimeError("Set JARVIS_TOKEN in .env before starting the API.")
    llm = llm or OllamaClient(settings.ollama_url, settings.ollama_model)
    json_llm = llm if hasattr(llm, "json") else None  # classifies files and reads free-text menus
    vision = vision or OllamaClient(settings.ollama_url, settings.vision_model, num_ctx=VISION_NUM_CTX)
    memory = agent.SessionMemory()
    db.connect(settings.db_path).close()  # create the file and tables up front
    # The main token, plus a shorter one typed into the iPhone Shortcuts (revocable on its own).
    # The phone token is all lowercase, so compare it case-insensitively: the iPhone keyboard
    # likes to capitalise the first letter of what you type (seen live: two 401s).
    exact = [t.encode() for t in (settings.token, settings.phone_token) if t]
    any_case = [t.lower().encode() for t in (settings.phone_token,) if t]

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_token(authorization: str | None = Header(default=None)) -> None:
        # "Bearer TOKEN", "bearer  TOKEN " or just "TOKEN" all count; only the token matters.
        value = " ".join((authorization or "").split())
        token = value[7:] if value.lower().startswith("bearer ") else value
        ok = any(secrets.compare_digest(token.encode(), t) for t in exact) or any(
            secrets.compare_digest(token.lower().encode(), t) for t in any_case)
        if not token or not ok:
            raise HTTPException(401, "Missing or wrong token.", headers={"WWW-Authenticate": "Bearer"})

    def push_later(title: str, message: str) -> None:
        if (sender := push_from(settings)) is not None:
            try:
                sender.send_text(title, message)
            except Exception as e:  # the result is in the calendar anyway; just say so in the log
                print(f"share: couldn't push the result: {e}", flush=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        watcher = None
        if watch_inbox:
            watcher = InboxWatcher(lambda: db.connect(settings.db_path), settings.inbox_dir,
                                   settings.files_dir, clock, vision, json_llm)
            watcher.start()
        if fetch_holidays and settings.holiday_regions:
            holidays.refresh_in_background(lambda: db.connect(settings.db_path), list(settings.holiday_regions),
                                           clock, timedelta(days=settings.holiday_refresh_days))
        reminders = start_reminders(settings, clock, say=reminder_say) if send_reminders else None
        alarm_clock = start_alarms(settings, clock, ring=alarm_ring) if send_reminders else None
        yield
        if watcher:
            watcher.stop()
        if reminders:
            reminders.stop()
        if alarm_clock:
            alarm_clock.stop()

    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _HideFeedToken) for f in access_log.filters):
        access_log.addFilter(_HideFeedToken())
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, lifespan=lifespan)
    api = APIRouter(dependencies=[Depends(require_token)])

    @api.post("/documents", status_code=201)
    def upload_document(
        response: Response,
        file: UploadFile | None = File(default=None),
        text: str | None = Form(default=None, max_length=200_000),
        filename: str | None = Form(default=None, max_length=200),
        week: str | None = Form(default=None, max_length=100),
        repeats_weekly: bool | None = Form(default=None),
        session_id: str | None = Form(default=None, max_length=100),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        """A file from the Share Sheet or web app, or text already extracted on the phone."""
        if file is not None:
            data = file.file.read(MAX_BYTES + 1)
            name = file.filename or filename or "upload"
        elif text:
            data, name = text.encode(), filename or "shared-text.txt"
        else:
            raise HTTPException(422, "Send a file or some text.")
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "That file is over 20 MB.")
        now = clock()
        try:
            result = ingest(conn, data, name, now, settings.files_dir, source="phone", vision=vision,
                            llm=json_llm, week=week, repeats_weekly=repeats_weekly)
        except (UnreadableFile, DateParseError) as e:
            raise HTTPException(422, str(e)) from e
        except LLMError as e:
            raise HTTPException(503, str(e)) from e
        if result.duplicate:
            response.status_code = 200
        memory.add(session_id or "default", now, f"(sent the file {name})", result.reply)
        return {"reply": result.reply, "meals": result.meals, "duplicate": result.duplicate,
                "document": result.document.to_dict()}

    @api.post("/share")
    def share(
        response: Response,
        file: UploadFile | None = File(default=None),
        text: str | None = Form(default=None, max_length=20_000),
        session_id: str | None = Form(default=None, max_length=100),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        """The iPhone Share Sheet: a forwarded message (text) or a file/photo. Jarvis decides
        whether it's events for the calendar, a document to keep, or both."""
        session = session_id or "phone"
        now = clock()
        if file is None:
            if not text or not text.strip():
                raise HTTPException(422, "Send some text or a file.")
            try:
                result = share_text(conn, llm, text, now, settings.files_dir, session=session, memory=memory)
            except LLMError as e:
                raise HTTPException(503, str(e)) from e
            return {"reply": result.reply, "events_added": result.events_added, "document": result.stored}

        data = file.file.read(MAX_BYTES + 1)
        name = file.filename or "shared"
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "That file is over 20 MB.")
        if is_image(name) and push_from(settings) is not None:
            # Reading a photo can take ~2 min if the vision model has to load, longer than
            # a Shortcut waits. Answer now; push the result to the phone when it's done.
            def work() -> None:
                c = db.connect(settings.db_path)
                try:
                    r = share_file(c, llm, data, name, clock(), settings.files_dir, vision=vision,
                                   session=session, memory=memory)
                    push_later("Jarvis", r.reply)
                except (UnreadableFile, LLMError) as e:
                    push_later("Jarvis", f"I couldn't read {name}: {e}")
                finally:
                    c.close()

            threading.Thread(target=work, name="share", daemon=True).start()
            response.status_code = 202
            return {"reply": "Got the photo. I'm reading it now and will send you a notification when it's done.",
                    "events_added": 0, "document": None}
        try:
            result = share_file(conn, llm, data, name, now, settings.files_dir, vision=vision,
                                session=session, memory=memory)
        except UnreadableFile as e:
            raise HTTPException(422, str(e)) from e
        except LLMError as e:
            raise HTTPException(503, str(e)) from e
        return {"reply": result.reply, "events_added": result.events_added, "document": result.stored}

    # Two endpoints shaped for the simplest possible iPhone Shortcuts. They accept every way
    # a Shortcut can send something (JSON field, form field, form file, raw body), because
    # Shortcuts silently sends an empty body for a text variable in a "File" body (seen live).
    # The reply is plain text to speak or show as-is.

    async def payload(request: Request) -> bytes:
        ctype = request.headers.get("content-type", "").lower()
        if "application/json" in ctype:
            try:
                obj = json.loads(await request.body() or b"null")
            except ValueError:
                return b""
            if isinstance(obj, dict):
                obj = obj.get("text") or next((v for v in obj.values() if isinstance(v, str)), "")
            return obj.encode() if isinstance(obj, str) else b""
        if "multipart/form-data" in ctype or "x-www-form-urlencoded" in ctype:
            form = await request.form()
            files = [v for v in form.values() if hasattr(v, "read")]
            contents = [await f.read() for f in files]
            for data in contents:  # a real photo or PDF wins
                if data.strip() and sniff(data) != ".txt":
                    return data
            for value in form.values():  # then a text field
                if isinstance(value, str) and value.strip():
                    return value.encode()
            return next((d for d in contents if d.strip()), b"")  # then a text file
        return await request.body()

    def log_empty(path: str, request: Request) -> None:
        print(f"{path}: nothing received (content-type {request.headers.get('content-type')!r}, "
              f"length {request.headers.get('content-length')!r})", flush=True)

    @api.post("/ask", response_class=PlainTextResponse)
    async def ask(request: Request, session: str = Query(default="iphone", max_length=100)) -> str:
        """Body: what the user said. Reply: what Jarvis says back."""
        text = (await payload(request)).decode("utf-8", errors="replace").strip()[:4000]
        if not text:
            log_empty("/ask", request)
            return "I didn't hear anything."

        def run() -> str:
            conn = db.connect(settings.db_path)
            try:
                reply = agent.handle(text, conn, llm, now=clock(), source="phone", session=session,
                                     memory=memory).text
            except LLMError:
                reply = "Sorry, I can't reach my language model right now."
            finally:
                conn.close()
            if on_exchange:
                on_exchange("phone", text, reply)
            return reply

        return await run_in_threadpool(run)

    @api.post("/send", response_class=PlainTextResponse)
    async def send(request: Request, session: str = Query(default="iphone", max_length=100)) -> str:
        """Body: whatever was shared (message text, photo, PDF, spreadsheet). Jarvis works
        out which from its first bytes. Reply: one sentence to show on the phone."""
        data = await payload(request)
        if not data.strip():
            log_empty("/send", request)
            return "Nothing was shared."
        if len(data) > MAX_BYTES:
            return "That file is over 20 MB."
        ext = sniff(data)

        def run() -> str:
            conn = db.connect(settings.db_path)
            try:
                if ext == ".txt":
                    return share_text(conn, llm, data.decode("utf-8", errors="replace"), clock(),
                                      settings.files_dir, session=session, memory=memory).reply
                return share_file(conn, llm, data, f"shared{ext}", clock(), settings.files_dir,
                                  vision=vision, session=session, memory=memory).reply
            except UnreadableFile as e:
                return str(e)
            except LLMError:
                return "Sorry, I can't reach my language model right now."
            finally:
                conn.close()

        if is_image(f"shared{ext}") and push_from(settings) is not None:
            def later() -> None:
                push_later("Jarvis", run())

            threading.Thread(target=later, name="send", daemon=True).start()
            return "Got the photo. I'm reading it now and will send you a notification when it's done."
        return await run_in_threadpool(run)

    # ---------- the web app (milestone 6) ----------

    stt_holder: dict = {"stt": stt}
    stt_lock = threading.Lock()

    def get_stt():
        """Whisper loads on the first spoken question, not at startup (~1 GB of VRAM)."""
        with stt_lock:
            if stt_holder["stt"] is None:
                from jarvis.voice.stt import Transcriber

                stt_holder["stt"] = Transcriber(settings.whisper_model, settings.whisper_device,
                                                settings.whisper_compute, settings.models_dir)
            return stt_holder["stt"]

    @api.post("/ask-audio")
    async def ask_audio(request: Request, session: str = Query(default="web", max_length=100)) -> dict:
        """The web app's mic: a recording (form file or raw body) -> what was heard, and the reply."""
        ctype = request.headers.get("content-type", "").lower()
        if "multipart/form-data" in ctype:
            form = await request.form()
            files = [v for v in form.values() if hasattr(v, "read")]
            data = await files[0].read() if files else b""
        else:
            data = await request.body()
        if len(data) < 100:
            return {"heard": "", "reply": "I didn't hear anything."}
        if len(data) > MAX_BYTES:
            return {"heard": "", "reply": "That recording is too long."}

        def run() -> dict:
            from jarvis.voice.stt import is_nothing, strip_wake

            try:
                heard = strip_wake(get_stt().transcribe_file(data))
            except Exception as e:  # a recording PyAV can't decode, or no GPU
                print(f"/ask-audio: couldn't transcribe: {e!r}", flush=True)
                return {"heard": "", "reply": "Sorry, I couldn't make out that recording."}
            if is_nothing(heard):
                return {"heard": "", "reply": "I didn't catch that."}
            conn = db.connect(settings.db_path)
            try:
                reply = agent.handle(heard, conn, llm, now=clock(), source="web", session=session,
                                     memory=memory).text
            except LLMError:
                reply = "Sorry, I can't reach my language model right now."
            finally:
                conn.close()
            if on_exchange:
                on_exchange("web", heard, reply)
            return {"heard": heard, "reply": reply}

        return await run_in_threadpool(run)

    voice_holder: dict = {"voice": voice}
    voice_lock = threading.Lock()  # loading
    speak_lock = threading.Lock()  # speaking

    def get_voice():
        """Jarvis's own voice (Kokoro, TTS_VOICE), loaded on the first spoken reply."""
        with voice_lock:
            if voice_holder["voice"] is None:
                from jarvis.voice.tts import make_voice

                voice_holder["voice"] = make_voice(settings.tts_engine, settings.models_dir,
                                                   settings.tts_voice, settings.tts_speed)
            return voice_holder["voice"]

    @api.post("/speak")
    async def speak(request: Request) -> Response:
        """Text in, Jarvis's voice out (WAV), so the web app sounds like Jarvis on the PC
        rather than whatever voice the phone has."""
        text = (await payload(request)).decode("utf-8", errors="replace").strip()[:2000]
        if not text:
            raise HTTPException(422, "Nothing to say.")

        def run() -> bytes:
            from jarvis.voice.tts import to_wav

            v = get_voice()
            with speak_lock:  # one synthesis at a time; Kokoro runs on the CPU
                return to_wav(v, text)

        try:
            wav = await run_in_threadpool(run)
        except Exception as e:
            print(f"/speak: {e!r}", flush=True)
            raise HTTPException(503, "Jarvis's voice isn't available.") from e
        return Response(wav, media_type="audio/wav")

    @api.get("/meals")
    def meals(day: date | None = Query(default=None, alias="date"),
              conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """One day's mess menu exactly as stored, for the web app's 'Today' card."""
        d = day or clock().date()
        doc, rows = doc_store.menu_for_day(conn, d)
        return {"date": d.isoformat(), "meals": {r.meal: r.items for r in rows} if doc else None}

    @api.get("/feed-url")
    def feed_url() -> dict:
        """Where the iPhone Calendar subscribes. Needs the phone token, so the page can show it."""
        if not (settings.feed_token and settings.public_url):
            raise HTTPException(404, "Run `python -m jarvis phone-setup` to create the calendar feed.")
        https = f"{settings.public_url}/calendar/{settings.feed_token}.ics"
        return {"https": https, "webcal": "webcal://" + https.split("://", 1)[1]}

    @app.get("/calendar/{token}.ics")
    def calendar_feed(token: str, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        """The subscription feed. iOS Calendar can't send headers, so the (read-only) token
        is in the address. Anything wrong gets the same 404, so it reveals nothing."""
        if not settings.feed_token or not secrets.compare_digest(token.encode(), settings.feed_token.encode()):
            raise HTTPException(404)
        now = clock()
        events = repo.list_events(conn, now - ics.FEED_PAST, now + ics.FEED_FUTURE)
        return Response(ics.calendar_ics(events, now), media_type="text/calendar; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    web = Path(__file__).resolve().parent.parent / "web"

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/app")

    @app.get("/app", include_in_schema=False)
    def web_app() -> FileResponse:
        return FileResponse(web / "app.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(web / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon-{size}.png", include_in_schema=False)
    def icon(size: int) -> FileResponse:
        if size not in (180, 192, 512):
            raise HTTPException(404)
        return FileResponse(web / f"icon-{size}.png", media_type="image/png")

    @app.get("/games/{name}/{path:path}", include_in_schema=False)
    @app.get("/sites/{name}/{path:path}", include_in_schema=False)
    def game_file(name: str, path: str = "") -> Response:
        """Web games and websites made by the studio, for the iPhone over the tailnet. Only folders the
        studio made (with .studio/state.json) for the web or site stack; only files inside them; never .studio,
        .git, docs or tests. They hold no personal data, so no token (like /app)."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", name):
            raise HTTPException(404)
        game = Path(settings.new_projects_dir) / name
        state = game / ".studio" / "state.json"
        try:
            stack = json.loads(state.read_text(encoding="utf-8")).get("stack") if state.is_file() else None
        except (ValueError, OSError):
            stack = None
        if stack not in ("web", "site"):
            raise HTTPException(404)
        target = (game / (path or "index.html")).resolve()
        if game.resolve() not in target.parents or not target.is_file() or \
                target.relative_to(game.resolve()).parts[0] in (".studio", ".git", "docs", "tests"):
            raise HTTPException(404)
        types = {".js": "text/javascript", ".mjs": "text/javascript", ".html": "text/html", ".css": "text/css",
                 ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml", ".wav": "audio/wav",
                 ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}
        return FileResponse(target, media_type=types.get(target.suffix.lower(), "application/octet-stream"),
                            headers={"Cache-Control": "no-cache"})

    @app.get("/games/{name}", include_in_schema=False)
    def game_home(name: str) -> RedirectResponse:
        return RedirectResponse(f"/games/{name}/")

    @app.get("/sites/{name}", include_in_schema=False)
    def site_home(name: str) -> RedirectResponse:
        return RedirectResponse(f"/sites/{name}/")

    @api.get("/documents")
    def get_documents(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [d.to_dict() for d in doc_store.list_all(conn)]

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @api.post("/capture")
    def capture(body: CaptureIn, conn: sqlite3.Connection = Depends(get_conn)) -> CaptureOut:
        try:
            r = agent.handle(body.text, conn, llm, now=clock(), source=body.source,
                             session=body.session_id or "default", memory=memory, shared=body.shared)
        except LLMError as e:
            raise HTTPException(503, str(e)) from e
        return CaptureOut(reply=r.text, actions=r.actions)

    @api.get("/events")
    def get_events(
        from_: date | None = Query(default=None, alias="from"),
        to: date | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> list[dict]:
        today = clock().date()
        first = from_ or today
        last = to or first + timedelta(days=7)
        if last < first:
            raise HTTPException(422, "'to' is before 'from'.")
        events = repo.list_events(conn, local_midnight(first), local_midnight(last + timedelta(days=1)))
        return [e.to_dict(today) for e in events]

    @api.post("/events", status_code=201)
    def post_event(body: EventCreate, response: Response, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        now = clock()
        if body.client_uuid and (existing := repo.get_by_client_uuid(conn, body.client_uuid)):
            response.status_code = 200
            return existing.to_dict(now.date())
        start = _parse_dt(body.start)
        if body.all_day:
            start = local_midnight(start.date())
            end = local_midnight(_parse_dt(body.end).date()) if body.end else start + timedelta(days=1)
            if end <= start:  # an inclusive last day was sent
                end += timedelta(days=1)
        else:
            end = _parse_dt(body.end) if body.end else start + timedelta(hours=1)
        new = repo.NewEvent(title=body.title, start=start, end=end, all_day=body.all_day,
                            location=body.location, notes=body.notes, important=body.important,
                            source="api", client_uuid=body.client_uuid)
        try:
            return repo.create_event(conn, new, now).to_dict(now.date())
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @api.patch("/events/{event_id}")
    def patch_event(event_id: int, body: EventPatch, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        now = clock()
        event = repo.get_event(conn, event_id)
        if event is None:
            raise HTTPException(404, "No such event.")
        changes = body.model_dump(exclude_unset=True)
        all_day = changes.get("all_day", event.all_day)
        length = event.end - event.start
        if "start" in changes:
            changes["start"] = _parse_dt(changes["start"])
        if "end" in changes:
            changes["end"] = _parse_dt(changes["end"])
        if all_day:
            if "start" in changes or "all_day" in changes:
                changes["start"] = local_midnight(changes.get("start", event.start).date())
            if "end" in changes:
                changes["end"] = local_midnight(changes["end"].date())
            elif "start" in changes:
                changes["end"] = changes["start"] + (length if event.all_day else timedelta(days=1))
        elif "start" in changes and "end" not in changes:
            changes["end"] = changes["start"] + (timedelta(hours=1) if event.all_day else length)
        try:
            return repo.update_event(conn, event_id, now, **changes).to_dict(now.date())
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @api.delete("/events/{event_id}", status_code=204)
    def remove_event(event_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
        try:
            repo.delete_event(conn, event_id, clock())
        except LookupError:
            raise HTTPException(404, "No such event.") from None
        return Response(status_code=204)

    app.include_router(api)
    return app
