# Jarvis — local voice assistant

Personal Jarvis-style voice assistant that runs locally on a Windows PC (NVIDIA GPU) and is usable from an iPhone, online and offline. Its main jobs right now: manage its own calendar from voice/text/phone input, remind the user of anything marked important, and answer questions from files the user sends (e.g. a weekly mess menu → "what's for breakfast this Wednesday?").

Full design write-up: `docs/architecture.md`.

## Key decisions (already made — don't revisit without asking)

- **Brain runs on the Windows PC.** All AI, speech and data stay local. One internet *download* is allowed: public festival/holiday calendars (user asked, 6 Nov 2026). Nothing about the user is sent.
- **Jarvis owns the calendar.** A SQLite database on the PC is the source of truth — not iCloud or Google Calendar.
- **iPhone works both ways:** full Jarvis via the PC when reachable; a small offline fallback when not.
- **No Mac assumed.** The iPhone side is Apple Shortcuts + a web app (PWA), not a native iOS app.
- User's time zone: **Asia/Dubai (UTC+4)**. Every date the model sees or produces is interpreted in this zone.
- **Hardware:** NVIDIA GeForce RTX 5050 Laptop GPU, **8 GB VRAM**. Default model `qwen3:8b` (~5 GB); drop to `qwen3:4b` if it has to share VRAM with Whisper `large-v3-turbo`. Vision model `qwen2.5vl:7b` (6 GB) only loads for photos; Ollama swaps models, one at a time.
- **Installed:** Ollama 0.40.14 at `%LOCALAPPDATA%\Programs\Ollama\ollama.exe` (runs at login as a tray app; the server listens on 127.0.0.1:11434).

## Architecture

```
iPhone (Shortcut / web app) ──Tailscale──► jarvis-core (FastAPI, Python)
PC mic ─► wake word ─► VAD ─► STT ──────►      │
                                               ├─► Ollama (local LLM, tool calling)
                                               ├─► SQLite calendar (events, reminders)
                                               └─► Scheduler ─► ntfy server ─► ntfy.sh/APNs wake-up ─► iPhone ntfy app
iPhone offline: Dictate ─► Shortcuts "Use Model" (on-device) ─► queue file + Apple Reminders mirror ─► sync later via POST /sync
```

### PC components

| Job | Tool |
| --- | --- |
| Wake word | openWakeWord (pre-trained "hey jarvis") |
| End-of-speech | Silero VAD |
| Speech to text | faster-whisper (`large-v3-turbo`, or `small` if VRAM is tight) |
| LLM + actions | Ollama, Qwen3-class instruct model, tool calling via `tools` on `/api/chat` |
| Text to speech | Kokoro (quality) or Piper (speed) |
| API | FastAPI |
| Scheduler | the `reminders` table itself, polled every 20 s (no APScheduler: the table already persists every fire time) |
| Push to phone | self-hosted ntfy 2.37.2 (native Windows) with `upstream-base-url` for iOS instant delivery, reached over Tailscale |

### Request flow

1. Text or audio arrives (PC mic or iPhone).
2. Transcribe if audio; send text to the model with current date/time, time zone and tool list.
3. Model calls a tool: `create_event`, `list_events`, `update_event`, `delete_event`, `set_important`.
4. **Python validates arguments** (real date, end after start, etc.) before touching the DB. The model never writes to the DB directly.
5. Reply with a spoken-style confirmation ("Added DBMS quiz, Monday 9 November at 10 am, marked important").

### How dates are resolved (implemented in `jarvis/core/dates.py`)

The model copies the user's own words into tool arguments (`date="next friday"`, `start_time="2pm"`); Python turns them into dates. The model does not do date arithmetic. Rules, relative to today in Asia/Dubai:

- `friday` (bare weekday) = the next Friday **after** today; `this friday` / `coming friday` may be today.
- `next friday` / `friday next week` = Friday of next week (weeks start Monday). `friday after next` = one week later.
- `12 nov`, `12/11` (day/month order), `the 12th`. No year → this year, unless that is more than 30 days ago → next year. Day only → this month, or next month if already past.
- Times: `10am`, `4:30 pm`, `14:00`, `1430`, `noon`, `midnight` (= 23:59, a deadline), `morning` 9:00, `afternoon` 15:00, `evening` 18:00, `tonight` 20:00. Bare hour: 1–7 → pm, 8–11 → am, 12 → noon; a leading zero (`07:00`) means 24-hour. In a range, `8 to 10pm` → 20:00–22:00.
- No time → all-day event. No end → 1 hour long, cut off at midnight. Only an end time ("by Sunday 11:59pm") → a deadline at that time.
- Every confirmation reads back the resolved date ("Friday 13 November") so a wrong guess is caught.

### Guards against model mistakes (in `jarvis/core/tools.py`, found running the phrase suite on qwen3:8b)

- `important=true` on a new event only counts if the user's words contain a cue (important, urgent, remind, forget, priority, critical, ASAP, must…). The model marks due dates important on its own and prompt wording didn't stop it. Direct API calls (no raw words) are trusted.
- If the model adds a `match_date` the user never said (e.g. "today") and nothing matches on that day, the search is retried without it. A day the user did say is always respected.
- "Push/move it to 6pm" sent as `new_end_time` only, with no end cue in the user's words (until, till, ends, finish, extend…), is treated as a new start time. Found after loading the vision model changed qwen3's behaviour on this borderline phrase; temperature 0 is only deterministic for one GPU state. A change to just the end reads back as "X now ends at 6 pm".
- `get_menu` refuses unless the user's words mention food (breakfast, lunch, dinner, snacks, mess, menu, eat, food, meal: `toolkit.FOOD`) and tells the model to use `list_events`. Found by voice: Whisper wrote "what's on tomorrow." and qwen3 read it as "what's on the menu". The question being answered counts too (`ToolContext.replying_to`, Jarvis's last reply if it ended in "?"): a "Yes" to "Shall I look up the mess menu?" was refused before that, found live.
- A food question answered without any tool call is sent back to the model once ("call get_menu"); if it still doesn't call one, Jarvis says it can only answer from sent menus. Found when the model invented "dinner on 20 November" mid-conversation (`agent.py`, `_FOOD`).
- Tools that produce a finished answer (event read-backs, `get_menu`, `find_in_menu`) set `ToolResult.confirmation`; when every call in a round has one, that text is the reply word for word and the model is not asked again. `list_events` and `search_documents` return data for the model to phrase.

### Voice on the PC — `jarvis/voice/`, run with `python -m jarvis voice`

- Loop (`loop.py`): wait for "hey jarvis" → record until 0.8 s of quiet (gives up after 4 s of nothing, cuts at 15 s) → Whisper → `agent.handle` (session "voice") → speak. **No chime while the user keeps talking:** the mic array's echo cancellation mutes input whenever the speakers play, and a chime right after the wake word cut "what's on" off "what's on tomorrow" in 2 of 3 live tries. The chime plays only after 0.5 s of quiet following the wake word. Just "hey jarvis" then a pause → "Yes?" and listen again; a one-word fragment right after the wake word → "Sorry, I only caught 'tomorrow'…" and listen again. A reply ending in "?" → listen again without the wake word. Mic audio heard while Jarvis talks is flushed, so it never hears itself. Near-misses print "(close to 'hey jarvis': 0.32…)" to help tune `WAKE_THRESHOLD`.
- Wake word + VAD (`listen.py`): openWakeWord ONNX `hey_jarvis` and its bundled Silero VAD, CPU. Imported via `oww.py`, which stubs openwakeword's verifier-training module: **Windows Smart App Control blocks scikit-learn's DLLs on this PC** and Jarvis never trains verifiers. Don't try to change the Windows setting.
- STT (`stt.py`): faster-whisper `large-v3-turbo`, `int8_float16` on CUDA (~1 GB VRAM next to qwen3:8b; total ~6.6 of 8 GB). `cuda.py` adds the nvidia pip wheels' DLL folders and raises `CUDA_CACHE_MAXSIZE`: the RTX 5050 needs a one-time ~1 min kernel compile, then 0.3 s per utterance. Whisper's stock phrases on silence ("Thank you.") count as nothing; a leading "hey jarvis" is stripped.
- TTS (`tts.py`, `audio.py`): Kokoro `bm_george` by default (pronounces dish names right; ~1 s per sentence on CPU, spoken sentence by sentence while the next is made), Piper `en_GB-alan-medium` as the instant option. `for_speech()` rewrites times, a.m./p.m. and "tea/coffee" for reading aloud.
- Ollama is warmed at startup with `keep_alive=-1` so voice answers never wait for a cold load.
- Models live in `data/models/` (whisper, kokoro, piper, openwakeword); voice samples in `data/voice-samples/`.

### Reminders to the phone — `jarvis/notify/`

- Path: Jarvis → ntfy on `127.0.0.1:2586` → (content-free poll request) ntfy.sh → Apple push → iPhone ntfy app → fetches the text from the PC's Tailscale HTTPS name, `NTFY_PUBLIC_URL` in `.env` (`tailscale serve` → port 2586, tailnet only). Keep that name out of committed files: the GitHub repo is public. Without Tailscale on the phone it shows only "New message".
- `ntfy-setup` (`server.py`) writes `data/ntfy/server.yml` with users, permissions and the token declared in it (re-runnable): private random topic; `jarvis` write-only via token `NTFY_TOKEN`; `phone` read-only with `NTFY_PHONE_PASSWORD`; everyone else denied. Password hashes come from `ntfy user hash`, fed as **bytes**: in text mode Windows adds `\r` and the phone got 401.
- Dispatcher (`reminders.py`) runs in `serve` and `voice` (voice also speaks it; a lock in `Speaker` stops it talking over an answer). Rows are claimed atomically (pending → sending), so both processes running never double-send; a crash-left `sending` row goes back to pending on start. After the PC was off: only the latest overdue reminder per event is sent, earlier ones marked `missed`; nothing is sent once a timed event has started (or an all-day event's day is over). A failed push is retried up to 3 times, then `failed` with `last_error`.
- Messages: title = event title; body "In 10 minutes, at 10 am" / "Tomorrow at 10 am" / "Today" (all-day), plus location; priority high within 15 min or for an all-day event today.
- Migration 4 rebuilt `reminders` with statuses `pending/sending/sent/acknowledged/missed/failed`, `attempts`, `sent_at`, `last_error`. Acknowledging from the phone ("Got it" button calling back to Jarvis) isn't built yet.
- Tailscale 1.104.5 is installed and signed in on the PC; HTTPS certificates and Serve are enabled on the tailnet. iPhone steps: `docs/iphone-setup.md`.

### Festivals and public holidays — `jarvis/calendar/holidays.py`

- Source: Google's public holiday calendars as iCalendar files, no account or key (`HOLIDAY_REGIONS=india,uae`; other codes work too). India includes Hindu festivals (Diwali, Naraka Chaturdasi, Govardhan Puja, Bhai Duj, Holi, Dussehra, Janmashtami…) and Christmas; UAE adds Eid, Ramadan, National Day.
- Imports **this year and next** as all-day events, `source="holiday"`, not important, notes "Public holiday (India, UAE)." / "Observance (India). Date is tentative." Same day+name in two lists → one event. Names people use: "Naraka Chaturdasi" → "Choti Diwali (Naraka Chaturdasi)", "Diwali/Deepavali" → "Diwali"; "(tentative)" is moved from the name into the notes.
- Identity is `client_uuid = holiday:<date>:<name-slug>`. A refresh adds new ones and removes holiday events no longer listed (moved dates). The user's deletions stay deleted, their important flags stay, and their own events are never touched.
- Refreshes in the background in `serve` and `voice` when the last sync is >7 days old or from last year (checked every 6 h, `meta` table key `holidays.last_sync`); `python -m jarvis holidays` syncs now. Download failures are logged and retried; partial downloads still import what arrived.
- 6 Nov 2026: 154 imported into the real calendar (76 for 2026, 78 for 2027).
- `find_event(query)` answers "when is X?" from the calendar (festivals and the user's own events): the exact name wins ("Diwali" not "Choti Diwali"), then the nearest upcoming date per name, up to three ("A, B, and C"), "was" for past ones.

### Data model

- `events`: id, title, start_at, end_at (UTC ISO strings), all_day, location, notes, important, recurrence (RRULE), source, client_uuid (unique, for offline dedupe), raw_text (the words it came from), created_at, updated_at, deleted_at (soft delete, so a resent offline item can't resurrect a deleted event)
- `reminders`: event_id, fire_at, channel, status (pending / sent / acknowledged). Important events default to reminders 1 day, 1 hour and 10 minutes before; all-day important events get 20:00 the evening before and 08:00 on the day. Rebuilt whenever the event changes.
- `documents`: id, kind (mess_menu / timetable / exam_schedule / other), filename, received_at, valid_from, valid_to, repeats_weekly, raw_text. Original file stored in `data/files/`.
- `menu_items`: document_id, weekday, date (nullable), meal, items. `date` set for a dated menu; rotating menus use `weekday` only.

### Documents (menus, timetables, other files) — `jarvis/documents/`

1. Receive via `POST /documents` (multipart `file`, or `text` already extracted on the iPhone; optional `week`, `repeats_weekly`, `session_id`), the `inbox/` folder (polled every 5 s while `serve` runs; done files are removed, unreadable ones go to `inbox/failed/`), or `python -m jarvis ingest <file>`. Same file twice (sha256) is stored once.
2. Read (`readers.py`): text PDFs and ruled tables (pdfplumber), Excel via openpyxl (not pandas: mess menus use merged cells, which pandas drops; merges are copied across columns, not down rows), CSV/TSV, text with Markdown tables, photos and scanned PDF pages via the vision model (`OLLAMA_VISION_MODEL`, transcribes to Markdown tables). HEIC is refused; the Shortcut should send JPEG.
3. Structure menus in plain Python first (`menu.py`): a grid with days on one axis and meal names on the other, either orientation, multi-row meals, dates inside day labels ("Mon 16/11"). Cells keep their words; line breaks become ", " only in cells without commas. Only menus written as sentences go to the model (JSON schema output), and any dish whose words aren't ≥80% in the source text is dropped.
4. Classify: a parsed menu grid is a menu; otherwise keyword heuristics (meals + weekdays, exam words + dates, weekdays + times/lecture words); only when unsure, the model. Timetable and exam files are stored and searchable (FTS5); turning timetables into events is milestone 8.
5. Which week: dates in day labels → that range; title text like "9th Nov to 15th Nov 2026", "9-15 November", "week of 9 Nov" → that week; "w.e.f. 9/11" → repeating from that date; "repeats every week" / "same every week" → repeating from this Monday. Otherwise the menu is `needs_week` and the reply asks; the agent's prompt then tells the model to call `set_menu_week` when the user answers.
6. Confirm back ("Got the mess menu for 9 to 15 November, 28 meals.").

Tools (`jarvis/documents/tools.py`): `get_menu(date, meal)`, `find_in_menu(dish, period)`, `search_documents(query)`, `set_menu_week(week, repeats_weekly)`.

Rules:
- Menu answers come only from stored rows — never from the model's own guess.
- If no menu covers the requested date, say so; don't reuse an old week.
- Overlapping menus: newest wins.
- Resolve relative dates ("this Wednesday") in Python using Asia/Dubai time, not in the model (reuse `jarvis/core/dates.py`).

### API

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness, no token |
| `POST /capture` | Free text (audio later) → model → create/edit events or answer questions → confirmation text |
| `POST /documents` | Upload a file (PDF, image, Excel/CSV) for ingestion |
| `GET /events?from=&to=` | List events (local dates, inclusive) |
| `POST /events`, `PATCH` / `DELETE /events/{id}` | Direct edits without the model |
| `POST /sync` | Phone uploads offline queue (dedupe by client_uuid; re-parse raw text with the PC model); returns next 7 days of important items |
| `GET /calendar.ics` | Read-only feed for iPhone Calendar subscription |

### Security

- `jarvis-core` binds to localhost only; exposed to the user's devices with `tailscale serve` (gives HTTPS, needed for the web app mic).
- Every request needs a bearer token (env var, never committed).
- ntfy topics password-protected.
- Nightly backup of `jarvis.db`.

## Build order (one milestone at a time)

1. **Text brain** — Ollama + FastAPI + SQLite, typed input in a terminal. **✅ Done 6 Nov 2026:** all 22 phrases pass on qwen3:8b (Ollama 0.40.14), ~0.8 s per request once loaded; the first request after a cold start takes ~3 min while the model loads.
   Done when: a test file of ~20 phrases (relative dates, "next Friday", edits, deletes) all parse into the right events. Written as pytest tests in `tests/test_phrases_llm.py`.
2. **Documents** — file upload, PDF/Excel/photo reading, menu extraction, `get_menu` / `find_in_menu` / `search_documents` tools. **✅ Done 6 Nov 2026** (`tests/test_documents.py`, `tests/test_documents_llm.py`). The menu photo transcribes 28/28 cells exactly with `qwen2.5vl:7b`: ~15 s warm, ~2 min cold. It doesn't fully fit in 8 GB (Ollama runs ~25% on CPU) and swaps with qwen3:8b; fine for occasional photos.
   Done when: after ingesting a sample mess menu, "what's for breakfast this Wednesday?" returns exactly what the file says, and a week with no menu gets "I don't have that week's menu." Cover with pytest using sample files in `tests/fixtures/`.
3. **Voice on the PC** — openWakeWord, Silero VAD, faster-whisper, Piper/Kokoro around the text brain. **✅ Done 6 Nov 2026, live with the user's voice:** "(wake word) you: What's on tomorrow? jarvis: There are no events scheduled for tomorrow." `tests/test_voice_llm.py` covers the same path with synthesized speech through every real model. A speaker-to-mic test on the laptop itself can't work: the Realtek mic array's echo cancellation removes the laptop's own playback.
   Done when: "Hey Jarvis, what's on tomorrow?" gets a spoken answer.
4. **Important reminders** — scheduler + ntfy + ntfy iOS app. **✅ Done 6 Nov 2026:** `remind-test` event at 19:43; its 10-minute reminder was sent at 19:33:06 and arrived on the iPhone, with text, on mobile data.
   Done when: an important test event 15 min out alerts the phone on mobile data.
5. **iPhone online** — Tailscale, "Jarvis" Shortcut (Dictate → POST /capture → Speak), Share Sheet Shortcut (text and files).
   Done when: a forwarded WhatsApp message becomes an event, and a shared menu photo becomes a stored menu.
6. **Web app + calendar feed** — agenda page, mic button over HTTPS, `calendar.ics`.
7. **Offline fallback** — Shortcut offline branch (on-device Use Model → Dictionary output), `jarvis-queue.txt`, sync, Apple Reminders "Jarvis" list mirror for important items.
   Done when: an event captured in airplane mode appears on the PC after reconnecting, exactly once.
8. **Polish** — recurring events, clash warnings, spoken morning briefing with today's meals, auto-start at Windows login, timetable files → recurring events.

Shortcuts can't be created from code; for milestones 5 and 7, produce clear step-by-step build instructions for the user.

## Conventions

- Windows 11, Python 3.12 venv in `.venv` (3.15 is installed too, but voice libraries lag on wheels), runtime dependencies in `requirements.txt`, test-only ones in `requirements-dev.txt`.
- Database schema changes are appended to `MIGRATIONS` in `jarvis/calendar/db.py`; never edit one that has shipped.
- Sample files live in `tests/fixtures/`, built by `tests/fixtures/make_fixtures.py` (rerun it after changing it; tests compare against the dishes written there).
- Config via `.env` (token, model name, ntfy URL/credentials); ship a `.env.example`.
- All code lives in the `jarvis/` package — a top-level `calendar/` folder would shadow Python's stdlib `calendar` module. Subpackages: `jarvis/core/` (API, agent loop, tools, `toolkit.py` shared tool plumbing, dates), `jarvis/calendar/` (DB + migrations, events, reminders), `jarvis/documents/` (readers, menu parsing, ingest + inbox, store, tools), later `jarvis/voice/`, `jarvis/notify/`.
- Everything that depends on "now" takes a `now` argument, so tests pin the clock (tests use Thursday 5 Nov 2026, 09:00 Dubai).
- Tests with pytest; the date-parsing tests are the most important ones. Tests marked `llm` need Ollama running with the configured model and are skipped otherwise.

## Running

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest                 # all tests (llm ones skip without Ollama)
.venv\Scripts\python -m pytest -m llm -v       # just the model-backed suites
.venv\Scripts\python -m jarvis check           # is Ollama up and the model pulled?
.venv\Scripts\python -m jarvis                 # typed chat in the terminal
.venv\Scripts\python -m jarvis ingest menu.pdf [--week "next week"] [--repeats]
.venv\Scripts\python -m jarvis serve           # API on 127.0.0.1:8765, plus the inbox/ watcher
.venv\Scripts\python -m jarvis voice           # "Hey Jarvis" on the PC mic; add a 16 kHz WAV path to test with a file
.venv\Scripts\python -m jarvis devices         # list mics/speakers for MIC_DEVICE
.venv\Scripts\python -m jarvis mic-test        # press Enter, say the phrase: mic level, wake score, transcript, advice
.venv\Scripts\python -m jarvis holidays        # download this year's and next year's festivals/holidays now
.venv\Scripts\python -m jarvis ntfy-setup      # (re)write ntfy config, topic, token, phone password into .env
.venv\Scripts\python -m jarvis ntfy-server     # run ntfy on 127.0.0.1:2586 (keep running for phone reminders)
.venv\Scripts\python -m jarvis notify-test     # push a test notification now
.venv\Scripts\python -m jarvis remind-test 15  # important event 15 min out; its 10-min reminder fires 5 min later
```

## Open questions (ask the user before depending on these)

- Which iPhone model? Offline parsing needs an Apple Intelligence-capable iPhone on iOS 26+; otherwise the offline branch uses a text prompt + date picker.
- Any Mac access later? Would allow a native iOS app.
