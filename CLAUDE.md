# Jarvis — local voice assistant

Personal Jarvis-style voice assistant that runs locally on a Windows PC (NVIDIA GPU) and is usable from an iPhone, online and offline. Its main jobs right now: manage its own calendar from voice/text/phone input, remind the user of anything marked important, and answer questions from files the user sends (e.g. a weekly mess menu → "what's for breakfast this Wednesday?").

Full design write-up: `docs/architecture.md`.

## Key decisions (already made — don't revisit without asking)

- **Brain runs on the Windows PC.** All AI, speech and data stay local. Internet use allowed: downloading public festival/holiday calendars (user asked, 6 Nov 2026), and the local coding agent's web search (DuckDuckGo) and page reading for references (user asked, 7 Nov 2026), and the voice brain's `look_up` for definitions and facts it doesn't know (user asked, 8 Nov 2026). Only search words and page requests leave the PC; nothing about the user (files, calendar, menus) is sent. Exception the user asked for (7 Nov 2026): when the game studio is stuck, Claude Code sees that game's folder (never Jarvis data).
- **Jarvis owns the calendar.** A SQLite database on the PC is the source of truth — not iCloud or Google Calendar.
- **iPhone works both ways:** full Jarvis via the PC when reachable; a small offline fallback when not.
- **No Mac assumed.** The iPhone side is Apple Shortcuts + a web app (PWA), not a native iOS app.
- User's time zone: **Asia/Dubai (UTC+4)**. Every date the model sees or produces is interpreted in this zone.
- **Hardware:** NVIDIA GeForce RTX 5050 Laptop GPU, **8 GB VRAM**. Default model `qwen3:8b` (~5 GB); drop to `qwen3:4b` if it has to share VRAM with Whisper `large-v3-turbo`. Vision model `qwen2.5vl:7b` (6 GB) only loads for photos; Ollama swaps models, one at a time.
- **Installed:** Ollama 0.40.14 at `%LOCALAPPDATA%\Programs\Ollama\ollama.exe` (runs at login as a tray app; the server listens on 127.0.0.1:11434).
- **Ollama memory settings (8 Nov 2026, user-level environment variables, then restart the tray app):** `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_MAX_LOADED_MODELS=1`. Every Jarvis client sends `num_ctx` 16384 (`core/llm.default_num_ctx`, override `OLLAMA_NUM_CTX`; vision 8192). Measured: qwen3:8b at 16k = 6.3 GB, 7.3 of 8.15 GB with Whisper, 100% GPU, ~950 tok/s prompt, ~17.5 tok/s writing (same as 8k). Ollama's default 4096 had been silently cutting the studio's ~3200-token prompts. All clients must use the same num_ctx or Ollama reloads the model.
- **qwen3:14b tried and rejected (8 Nov 2026):** 10 GB, 44% CPU / 56% GPU, 2.4 tok/s writing (7× slower), 388 tok/s prompt, a 6-minute diagnosis vs 51 s, 1.1 GB RAM left, first load crashed (CUDA error). Its circular-import diagnosis was closer than 8b's but still incomplete. Layer streaming from disk (AirLLM-style) was ruled out: ~4–13 s per token here. The studio's "thinker" stays qwen3:8b with thinking on (`STUDIO_THINKER_MODEL` empty); a bigger one plugs in there if hardware allows.
- **Processes started from Claude Code's own tools die when the Claude app quits** (seen twice, 8 Nov 2026: Jarvis and a benchmark). Start Jarvis, Ollama and long studio runs through `explorer.exe` (e.g. the Startup `Jarvis.lnk`, or a `.cmd` launcher) so they outlive it.

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
- Other answers that must come from a tool (`agent._MUST_LOOK`, only for the user's own requests): alarms/timers, definitions and "who is/what is" (→ `look_up`), and PC verbs (open, close, quit, play, lock…). A reply with no tool call is sent back once. Found live: "Can you close VS Code?" → "Closing VS Code." with no tool, "close spotify" → "Closed Spotify.", "what alarms do I have?" answered from nothing.
- "What's for the next meal?" got "which meal?" back (live, 8 Nov 2026): `documents/tools._day_and_meal` works "next" out from the clock (`MEAL_ENDS`: breakfast until 10:00, lunch 15:00, snacks 18:00, dinner 22:00, then tomorrow's breakfast); "the next breakfast" is today's if not over, else tomorrow's.
- Tools that produce a finished answer (event read-backs, `get_menu`, `find_in_menu`) set `ToolResult.confirmation`; when every call in a round has one, that text is the reply word for word and the model is not asked again. `list_events` and `search_documents` return data for the model to phrase.

### Voice on the PC — `jarvis/voice/`, run with `python -m jarvis voice`

- Loop (`loop.py`): wait for "hey jarvis" → record until 0.8 s of quiet (gives up after 4 s of nothing, cuts at 15 s) → Whisper → `agent.handle` (session "voice") → speak. **No chime while the user keeps talking:** the mic array's echo cancellation mutes input whenever the speakers play, and a chime right after the wake word cut "what's on" off "what's on tomorrow" in 2 of 3 live tries. The chime plays only after 0.5 s of quiet following the wake word. Just "hey jarvis" then a pause → "Yes?" and listen again; a one-word fragment right after the wake word → "Sorry, I only caught 'tomorrow'…" and listen again. A reply ending in "?" → listen again without the wake word. Mic audio heard while Jarvis talks is flushed, so it never hears itself. Near-misses print "(close to 'hey jarvis': 0.32…)" to help tune `WAKE_THRESHOLD`.
- Wake word + VAD (`listen.py`): openWakeWord ONNX `hey_jarvis` and its bundled Silero VAD, CPU. Imported via `oww.py`, which stubs openwakeword's verifier-training module: **Windows Smart App Control blocks scikit-learn's DLLs on this PC** and Jarvis never trains verifiers. Don't try to change the Windows setting.
- STT (`stt.py`): faster-whisper `large-v3-turbo`, `int8_float16` on CUDA (~1 GB VRAM next to qwen3:8b; total ~6.6 of 8 GB). `cuda.py` adds the nvidia pip wheels' DLL folders and raises `CUDA_CACHE_MAXSIZE`: the RTX 5050 needs a one-time ~1 min kernel compile, then 0.3 s per utterance. Whisper's stock phrases on silence ("Thank you.") count as nothing; a leading "hey jarvis" is stripped.
- TTS (`tts.py`, `audio.py`): Kokoro `bm_george` by default (pronounces dish names right; ~1 s per sentence on CPU, spoken sentence by sentence while the next is made), Piper `en_GB-alan-medium` as the instant option. `for_speech()` rewrites times, a.m./p.m. and "tea/coffee" for reading aloud.
- Ollama is warmed at startup with `keep_alive=-1` so voice answers never wait for a cold load.
- **Prompt cache (8 Nov 2026):** the system prompt + tool list (~4000 tokens) never change between requests, so Ollama reuses what it read; the clock goes at the start of the user's message (`[Now: Sunday 08 November 2026, 11:20]`). With the time at the top of the system prompt, every new minute forced a full re-read (~4 s on this GPU); measured after: 0.1 s to read, answers 0.3–2 s with one model call, 2–5 s with two (look-ups). `OllamaClient.last_stats` keeps Ollama's token counts and timings for checks like this.
- `WAKE_THRESHOLD` default 0.4 (was 0.5): the logs showed real attempts scoring 0.40–0.48 and being missed. A video playing nearby triggered it once even at 0.5.
- Models live in `data/models/` (whisper, kokoro, piper, openwakeword); voice samples in `data/voice-samples/`.

### iPhone online — `/ask`, `/send`, `jarvis/core/share.py`, `jarvis/core/phone.py`

- Jarvis is served to the tailnet on HTTPS **:8443** (`python -m jarvis phone-setup`: `tailscale serve --https=8443` → 127.0.0.1:8765; ntfy stays on :443). Address in `.env` as `JARVIS_PUBLIC_URL`.
- `JARVIS_PHONE_TOKEN`: 20 lowercase letters and digits (no l/1/o/0), typed into the Shortcuts, revocable without touching `JARVIS_TOKEN`. Compared **case-insensitively** and with spacing ignored, and "Bearer " is optional, because the iPhone keyboard capitalises (seen live: 401s). The main token stays exact.
- `/ask` and `/send` exist so the Shortcuts stay tiny: request body in, **plain text** out (speak or show as-is). They accept JSON `{"text"}`, form text, a form file or a raw body. Shortcuts sends an **empty** body for a text variable in a "File" body (seen live), so the guide uses JSON for Jarvis and Form (text + file fields) for Send to Jarvis. An empty request logs its content type and length.
- `/send` sniffs the type from the first bytes (`readers.sniff`): PDF, JPEG, PNG, HEIC (converted with pillow-heif), WebP, xlsx, else text. Photos are read in a background thread and the result pushed via ntfy (a cold vision model takes ~2 min, longer than a Shortcut waits).
- Routing (`share.py`): shared text that looks like a menu/timetable/exam schedule is stored as a document; other text goes to `agent.handle(shared=True)`, which wraps it in `SHARED_PROMPT` ("add every event it mentions") while the guards see only the message. WhatsApp chat prefixes (`[06/11/26, 8:14 PM] Name:`) are stripped: they're send times, not event dates. The food guard is off for shared messages ("dinner party at 8" is an event). A shared file that isn't a known kind (a notice, a poster) is stored and its events added.
- **Menus from photos (or scanned PDFs) are refused** with `ingest.PHOTO_MENU` (user's choice). Real menu PDFs are read by **`documents/layout.py`**: word positions, not table detection. That handles spaced headings ("B R E A K F A S T"), "MONDAY / Date-2/11/2026" labels, one dish per line, cell borders drawn as rectangles, and per-line shading. Row and column borders are edges covering ≥95% of the table. Used when a page has ≥2 meal headings and ≥3 day names. The extra **PS11** column is ignored (user's choice).
- `ingest.reread(conn, doc_id, ...)` re-reads a stored file after a parser fix (`store.delete` + ingest).

### Web app and calendar feed — `jarvis/web/`, `jarvis/calendar/ics.py`

- `GET /app` serves one HTML file, plain JavaScript with no build step: phone-first, dark and light themes, PWA manifest and icons (drawn by `jarvis/web/make_icons.py`). The page, manifest and icons need no token; all data does. The phone token is typed once and kept in localStorage.
- Cards: Ask (text → `/ask`; mic → `/ask-audio`), Today (events + `/meals` with the next meal highlighted), Next 7 days (festivals shown muted), Important (30 days), ☆ toggle (PATCH) and ✕ delete, and "Add Jarvis to iPhone Calendar" (`/feed-url` → `webcal://`).
- **Mic:** MediaRecorder (AAC/MP4 on iPhone Safari, Opus/WebM on Chrome) → `/ask-audio` → `stt.decode_audio` (our own PyAV decode to 16 kHz: faster-whisper 1.2.1 passes `metadata_errors=`, which PyAV 19 removed, and every upload failed) → Whisper, loaded on first use in `serve` (~1 GB VRAM) → agent.
- **Voice:** replies are spoken in **Jarvis's own voice**: `/speak` returns WAV from Kokoro `TTS_VOICE` (user chose **bm_george**, British; `TTS_SPEED=0.95`), played through Web Audio, unlocked on a tap because iOS blocks sound that doesn't start from a tap. Fallback: a British iPhone voice. The earlier en-IN phone voice was replaced at the user's request. Kokoro can't sound like the film actor, and real voices must not be cloned.
- **Feed:** `GET /calendar/<JARVIS_FEED_TOKEN>.ics` (iOS Calendar can't send headers, so a separate read-only token goes in the address; a wrong token gets 404). Events from 60 days ago to 400 days ahead; all-day events as DATE, timed ones in UTC; important ones start with "★ " and have PRIORITY:1; lines folded at 75 octets. The uvicorn access log masks the token (`_HideFeedToken`).
- Tested in a browser against a throwaway copy of Jarvis on :8799 with sample data and a made-up token, never the real calendar.

### Reminders to the phone — `jarvis/notify/`

- Path: Jarvis → ntfy on `127.0.0.1:2586` → (content-free poll request) ntfy.sh → Apple push → iPhone ntfy app → fetches the text from the PC's Tailscale HTTPS name, `NTFY_PUBLIC_URL` in `.env` (`tailscale serve` → port 2586, tailnet only). Keep that name out of committed files: the GitHub repo is public. Without Tailscale on the phone it shows only "New message".
- `ntfy-setup` (`server.py`) writes `data/ntfy/server.yml` with users, permissions and the token declared in it (re-runnable): private random topic; `jarvis` write-only via token `NTFY_TOKEN`; `phone` read-only with `NTFY_PHONE_PASSWORD`; everyone else denied. Password hashes come from `ntfy user hash`, fed as **bytes**: in text mode Windows adds `\r` and the phone got 401.
- Dispatcher (`reminders.py`) runs in `serve` and `voice` (voice also speaks it; a lock in `Speaker` stops it talking over an answer). Rows are claimed atomically (pending → sending), so both processes running never double-send; a crash-left `sending` row goes back to pending on start. After the PC was off: only the latest overdue reminder per event is sent, earlier ones marked `missed`; nothing is sent once a timed event has started (or an all-day event's day is over). A failed push is retried up to 3 times, then `failed` with `last_error`.
- Messages: title = event title; body "In 10 minutes, at 10 am" / "Tomorrow at 10 am" / "Today" (all-day), plus location; priority high within 15 min or for an all-day event today.
- Migration 4 rebuilt `reminders` with statuses `pending/sending/sent/acknowledged/missed/failed`, `attempts`, `sent_at`, `last_error`. Acknowledging from the phone ("Got it" button calling back to Jarvis) isn't built yet.
- Tailscale 1.104.5 is installed and signed in on the PC; HTTPS certificates and Serve are enabled on the tailnet. iPhone steps: `docs/iphone-setup.md`.

### PC control — `jarvis/pc/`

- Offered only when the user speaks or types to Jarvis directly (sources terminal, voice, phone, web, hud; `agent.PC_SOURCES`). Never for shared messages, files or offline items: those carry other people's words. `tools.specs_for(allow_pc)` hides the tools and every PC tool refuses without `ctx.allow_pc`.
- Safe actions run at once (`actions.py`, no shell): `open_app` (built-ins + Start Menu shortcuts), `close_app` (8 Nov 2026: finds the app's visible windows by program name via `APP_EXES`, then window title, then a close match; posts WM_CLOSE like clicking X, so it can still ask to save; never the Windows shell, `_KEEP_RUNNING`, or Jarvis's own windows; "file explorer"/"folders" closes folder windows only), `open_project` (VS Code; with a task, hands over to `start_coding`), `open_folder`, `open_website` (http/https only), `media_control`, `lock_screen`, `take_screenshot`, `show_interface`. Projects: `JARVIS_PROJECTS` in `.env` ("name=path;name=path") plus git repos found in the usual folders.
- Anything else is `run_command`, which only **proposes** a PowerShell command and asks. It runs only if the user's very next words in the same session are a clear yes (`confirm.YES`, checked in Python, within 2 minutes); anything else drops it. Disk wiping, deleting drives/system folders, boot and HKLM registry edits are refused even with a yes (`confirm.NEVER`).
- **Coder:** **Claude Code by default** (`JARVIS_CODER=claude`, user's choice 8 Nov 2026, back to the original plan: the local coder and the studio produced confused, weak code). Every coding request, new games and websites included, opens a Claude Code session in Windows Terminal in the project folder with the task as its first prompt (it asks before editing). `JARVIS_CODER=local` brings back Jarvis's own coder. The studio is no longer routed to and `project_status` was removed; `python -m jarvis.studio` still works by hand. Claude Code CLI (`claude`) installed 7 Nov 2026 (npm, run from a terminal tab; signed in with `claude.cmd auth login`).
- **Coding requests go to `start_coding(task, project)`** (found from the voice logs, 7 Nov 2026: "build me a website/game" got follow-up questions, "I don't know a project called Game", or w3schools opened). qwen3 never writes code; the coder does. A named existing project → the coder there; otherwise a new git-initialised folder in `JARVIS_NEW_PROJECTS_DIR` (default `~/Projects`) named from the request ("website-shopify-store", `-2` if taken). A "task" with no work in it ("open Claude Code", "start coding") opens Claude Code or asks what to build, never makes a project. The prompt says: call it at once, no follow-up questions.
- `open_website` only opens a site whose name the user said (`_site_named`); otherwise it tells the model to use `start_coding`.
- **Autocorrect** (`jarvis/core/autocorrect.py`, user asked 8 Nov 2026, "first run it through me"): runs in `agent.handle` on the user's own requests (typed or spoken, never shared messages) before the model. Known names = core list + projects + Start Menu apps (versions/brackets stripped), cached 10 min. Only the 1–3 words after a command word (open, close, launch, start, use, with, in, on, play… and "and" continuing a list) are compared, so ordinary sentences aren't touched; everyday words (`_PLAIN`, `_STOP`) never are. Score = mean of letter similarity and a rough sound key (`sound()`: "Claude"/"could", "Spotify"/"spot if I" match). ≥0.90 → fixed silently ("open blunder" → Blender, "close the curser window" → Cursor); 0.78–0.90 → "Did you mean Steam?" (`autocorrect.PENDING`, 2 min): yes → fixed request, no → as heard, "no, I meant Discord" → with Discord, anything else → a new request. Answers are remembered in `meta` key `autocorrect.learned` (confirmed = silent next time, rejected = never asked again). `FIXED` holds regex fixes for mishearings no score catches ("could/cloud/clod code" → Claude Code, "vs cold", "you tube", "shop fi"); `stt.fix_names` applies them to every transcript.
- Whisper hints: `Transcriber.hotwords` = `autocorrect.hotwords()` (Jarvis, Claude Code, VS Code, Spotify, YouTube, WhatsApp, then the user's projects and less common apps, system tools skipped, ≤400 chars). Tests pin the name list (`conftest.fixed_vocabulary`).
- qwen3 (the calendar brain) is never retrained: that would put the tested tool-calling at risk. Only the separate coder model is trained.

### Local coding agent — `jarvis/coder/`, run with `python -m jarvis.coder <folder> "<task>"`

- Opened by `start_coding` in Windows Terminal (`actions._local_coder_argv`). Ollama tool-calling loop (`agent.py`, max 40 steps) on `CODER_MODEL` (default `qwen2.5-coder:7b`; it swaps with qwen3 in VRAM, both can't stay loaded).
- Tools (`tools.py`): list/read/write files fenced to the project folder (asks before overwriting), `run_python` (asks first, `-I`, 60 s), `search_references` (keyword search over the training repos in `data/training/repos`), `search_web` (DuckDuckGo HTML, `uddg` links resolved, top 6), `read_webpage` (http/https only, text capped at 8000 chars).

### Alarms and timers — `jarvis/calendar/alarms.py`, `alarm_tools.py` (8 Nov 2026)

- Table `alarms` (migration 6): fire_at, label, repeat (daily / weekdays), kind (alarm / timer), status pending / ringing / done / missed / cancelled. A repeating alarm keeps one row and moves its fire_at on.
- Tools (only for the user's own requests, like PC control): `set_alarm(time, date, label, repeat)`, `set_timer(duration, label)`, `list_alarms`, `cancel_alarm(time, label, kind, all)`, `stop_alarm(snooze_minutes)`. The model copies words; Python resolves: a bare hour means the next one ("7" at 9 am → 7 pm, at 11 pm → 7 am), a named day means am ("7 tomorrow" → 7 am), no day → the next time it comes round; durations like "an hour and a half", "90 seconds", max 24 h. A label of just "wake up" is dropped (the model added it, seen live). "every weekday" in the user's words makes it repeat even if the model leaves `repeat` out.
- `AlarmClock` runs wherever reminders do (`serve`, `voice`, `up`), every 5 s; rows are claimed atomically. Rings by pushing to the phone (ntfy, urgent) and, with a speaker, beeping (`alarm_tone`, three rising beeps) and speaking "It's 7 am. Time to get up." about a minute at most. **"Hey Jarvis" while it rings just silences it** (the voice loop calls `alarms.silence()`); "stop"/"snooze" via `stop_alarm` also work from the phone. More than 10 minutes late (PC was off) → missed, not rung.

### Looking things up — `jarvis/core/web.py` (8 Nov 2026)

- `look_up(query)`, only for the user's own requests (a shared message could steer a search). One-word definitions go to the Free Dictionary API (dictionaryapi.dev, ~1 s); everything else to DuckDuckGo, then Wikipedia's summary, else the first readable page's most relevant paragraphs (video/social sites skipped), ~2.5 s. The model answers in a sentence or two and names the source.
- The client name is honest (`JarvisPersonalAssistant/1.0`). Wikipedia refuses clients with no contact (403), so without `JARVIS_WEB_CONTACT` in `.env` it uses Wikipedia's search snippet instead of the summary. Never pretend to be a browser.

### Game studio — `jarvis/studio/`, `python -m jarvis.studio new "<request>"` (paused 8 Nov 2026: user went back to the original plan; no longer routed from voice)

User asked 7 Nov 2026: say "make me a game like Shadow Fight 2" and Jarvis plans (PRD, features, tasks), graphs the project, and builds it with several agents in a loop until done, asking Claude Code only when stuck; plus plugins (Ponytail-style) and skills. Choices: Claude Code as the only helper (ChatGPT later), pygame and HTML5 stacks (Godot later), Jarvis's own Ponytail (nothing downloaded), test-first, review, phone updates.
- **Routing:** `start_coding` sends a *new* project whose task mentions a game (`pc/tools._GAME`) to `actions.start_studio` (Windows Terminal, `python -m jarvis.studio new …`), in Python, not left to the model. Existing projects stay with the single coder; saying "Claude" still means Claude Code. `project_status` answers "how's my game going?" (`studio/status.describe`); the HUD shows `status.hud_line`.
- **"Like X":** `project.reference_of` pulls X from the request. The researcher studies X from public pages only (never downloads or unpacks the game) into `docs/reference.md`; `validate_plan` rejects a title or character name sharing a non-generic word with X (`GENERIC`: fight, battle…). Original name, characters, story and art; mechanics borrowed.
- **Playtest** (`playtest.py`, user asked 7 Nov 2026 after Echo Duel's hollow fights): a harness written into `.studio/` explores scenes with input probes (confirm, back, pause, attacks, menu navigation, letting timers run), then autoplays ~4000 frames recording per scene which numbers on the scene/`game.data` change (gameplay values like health/score/position listed first), distinct screens, `pygame.draw` / canvas call counts and a screenshot; crashes with tracebacks. Python files the certain problems (crash, unreachable planned scene, frozen screen); qwen3 adds evidenced ones against the PRD (health never drops, rectangles only → `kind: visual`), duplicates filtered; they become tasks P1, P2… (feature PT); 2 playtest rounds. `python -m jarvis.studio playtest <project>` replays it on a finished game. First Echo Duel playtest (8 s) found: shop/pause unreachable, health never changes, score/coins never update, rectangles only.
- **Project folder** (`~/Projects/<slug of the title>`, renamed after planning): `docs/` (the six documents below, plus request, reference), `project.html` (live page, refreshes every 20 s: board, SVG graphs from `graph.py` with the last play-through's scene path, timeline); `.studio/state.json` (atomic), `log.jsonl`, `STOP` (pause). Git commit per task (identity "Jarvis Studio" via `-c`, never the user's).
- **Loop** (`loop.py`): research → plan (+ design/rules) → per task in dependency order: tester writes `tests/test_<id>.py` first (may write only that file; visual tasks skip it) → coder rounds (may not touch tests/docs) until all tests + smoke run pass and plugin checks are clean (3 rounds) → reviewer (qwen3 JSON; 2 rejections max) → commit → playtest → integrate. Stuck (`Studio.unstick`, user asked 7 Nov 2026 to break tasks down instead of falling back to Claude Code): (1) qwen3 judges the task's test against its criteria (`agents.check_tests`); a wrong test is rewritten and the coder gets 2 more rounds; (2) qwen3 splits the task into 2-4 one-file steps (`agents.split`, `project.split_into`: ids T5a, T5b…, deeper T5a1; chained; the parent then depends on them and re-checks the whole; its test file is parked as `*.parked` meanwhile, otherwise every step's "all tests pass" fails); split depth 2; (3) Claude Code (`helper.py`: `claude.exe -p --permission-mode acceptEdits --allowedTools Read Edit Write Glob Grep Bash(python -m pytest:*)…`, prompt via stdin, cwd = game folder) only as a last resort: 1 per task, `STUDIO_HELPER_CALLS` (3) per project; (4) `blocked`, dependants `skipped`, the rest continues. Integrate: final checks, one `INT` fix task if needed (it can split too), README. Budget `STUDIO_HOURS` (6); `resume` carries on.
- **Project documents** (`docs.py`, the user's list, 7 Nov 2026), in `docs/`: `PRD.md` (what, problem, audience, core features, included / not included, loop, controls), `Architecture.md` (overview, flow, scene graph, directory tree with notes from `stack.layout`, tech stack, connections table incl. payment/GitHub/LLM "none" unless planned, modules), `Rules.md` (stack `must`/`avoid` + the designer's, approved vs prohibited libraries, Ponytail ladder, originality), `memory.md` (`Project.remember`: context, decisions, changes with files, bugs; the agents get the latest ~1800 chars), `design.md` (palette table, typography, shapes, layout, motion, must-not-look-like; second planner call `agents.design`, palette also written into the game's config as `PALETTE`), `tasks.md` (step by step, split steps indented). `agents.context` puts Rules, the palette (full design for drawing tasks) and memory into the tester, coder, reviewer and splitter prompts.
- **Stacks** (`stacks.py`) write a working skeleton first (Game/Scene/Input with action names, fixed step, `Autoplay`): **pygame** (pytest, smoke = `main.py --autoplay --frames N --trace` with SDL dummy drivers; pygame 2.6.1 in `.venv`, Smart App Control allows it) and **web** (ES modules + canvas + touch buttons; `node --test --test-reporter=tap`; smoke = headless Edge `--dump-dom` on `smoke.html` served from 127.0.0.1). Default pygame unless the request says web/iPhone/browser.
- **Plugins** (`plugins.py`, `STUDIO_PLUGINS`): `ponytail` (decision ladder in prompts; checker: non-allowed imports, unused top-level functions, 8-line duplicates, file size; `STUDIO_PONYTAIL` lite/full/ultra/off), `test_first`, `review`, `phone_updates` (ntfy: plan ready, feature done, asking Claude Code, blocked, game ready, paused).
- **Skills** (`jarvis/skills/*.md`, header name/roles/keywords/always; `skills.select(text, role)`, 4000 chars by default): PRD, task breakdown, architecture, reference analysis, game loop, fighting game, enemy AI, procedural animation, balancing, pygame testing, web canvas, engineering practice, design principles, colour and contrast, motion design, game feel, web layout, web animation. The plain coder loads them too.
- Agents reuse `coder/agent.run` (now takes `system`, `workspace`, `specs`); `Workspace.refuse_write` enforces who may write what. Decisions (plan, design, split, test check, playtest analysis) go through `Team.think`: the thinker model (`STUDIO_THINKER_MODEL`, default the planner) with Qwen3's thinking on (`OllamaClient.json(..., think=True)`); the tool loop, review and Jarvis keep thinking off. Models: `STUDIO_PLANNER_MODEL` (qwen3:8b) researches/plans/reviews and, by default, also tests/codes (`STUDIO_CODER_MODEL` empty): one model shared with Jarvis's voice brain and pinned (`keep_alive=-1`), because with qwen2.5-coder the GPU swapped and Jarvis's own answers timed out at 300 s during a build; shared, a question mid-build took 9.6 s.
- Found in the first live runs (7 Nov 2026):
  - **qwen2.5-coder writes tool calls as JSON text** in its reply, not Ollama's `tool_calls`; `coder/agent.calls_in_text` reads them (plain, ```json fences, `<tool_call>` tags; only offered tool names). Before this, the plain local coder never wrote a file either.
  - The skeleton's own tests pass on an empty task, so a round counts only if code outside tests/docs changed or the task's own test file exists (`loop.made_progress`, `NO_CHANGE` feedback).
  - The 7B coder stalls (rewrites one file, then explains in prose): two identical failures → the last local round goes to qwen3 ("second opinion") → Claude Code. Coder rounds are 16 steps (30 took 10+ min). Helper budget is at least tasks + 3.
  - Research is done in Python (`agents.gather_sources`: 3 DuckDuckGo searches, up to 4 pages, video/store sites skipped), then qwen3 writes `reference.md` without tools: given the tools it used all 14 steps searching and never wrote.
  - The small tester sometimes writes wrong tests (the coder may not edit them); Claude Code fixes those when called. First Pong run: 15/15 tasks, 47 tests, 13 helper calls; the game is basic (one paddle), the plan's thinness is the limit.
  - "Make me a game like Shadow Fight 2" → **Echo Duel** (`~/Projects/echo-duel`, 7 Nov 2026): 10/10 tasks, 50 tests, 10 helper calls, local rounds succeeded 2 of 36, ~70 min. Plays menu → fight → results → menu, but it's a prototype: fighters are rectangles, the play scene moves on `pressed` instead of `held` so they barely move and no hit lands in a full round, and shop/pause are never reached. Unit tests per task can't see that; the smoke run (600 frames) only checks that it doesn't crash. Missing: an end-to-end playtest (long autoplay, game-level checks like "hits land", "every scene reachable") that turns failures into fix tasks.
- **8 Nov 2026 upgrade (user asked: think like an engineer, detailed graphs, sketches, design, animated websites):**
  - *Engineer thinking:* before each task's tests, `agents.approach` (thinker, thinking on) plans files/changes/edge cases/verification against the real code map (`agents.code_outline`); it's in `describe_task` for tester and coder and in memory.md. After every failed round `Studio.diagnosed` → `agents.diagnose` reads the files named in the traceback and returns root cause + evidence + fix steps; the coder gets that plus trimmed raw output, and split/test-check get it too ("look deeper" if the last diagnosis didn't fix it). Coder prompt: state the cause before changing code. Skill `engineering-practice` (incl. the circular-import rule found on Echo Duel: low-level modules never import scenes).
  - *Graphs* (`graph.py`): `code_map` (Python via `ast`: classes, methods, functions, calls through `self.`, imported classes and module aliases; JS via imports), `code_map_svg` (module boxes listing members, "n calls" arrows), `code_map_mermaid` (classDiagram), `frame_flow` (main loop → Game.step → each scene's update → what it calls, 2 deep → draw), `task_graph` (status colours), `scene_edge_classes` (playtest-found = walked, planned-but-unreached = dashed red, unplanned = green). project.html: Concept vs built, Code map, One frame, Task graph; Architecture.md gets the Mermaid versions.
  - *Concept sketches* (`sketch.py`): the designer call returns `screens` (per scene/section: elements with kind, label, x/y/w/h on a 0-100 grid, palette colour; cleaned by `agents.clean_screens`); rendered as hand-drawn SVG wireframes (seeded jitter, stick figures) to `docs/sketches/<scene>.svg` (+ `-phone.svg` for sites) before building; design.md lists them; drawing tasks and the vision review get the layout text.
  - *Design without training* (user declined training after two failed attempts): skills design-principles, color-and-contrast, motion-design, game-feel, web-layout, web-animation; `colors.fix_contrast` moves text to ≥4.5:1 and characters/accents to ≥3:1 against the background before building (noted in design.md and memory); `vision.py` plugin `vision` (default on, user's choice): after each playtest qwen2.5vl reviews up to 4 screenshots, one per request, against design + sketch → visual fix tasks. Colour comments are dropped (`_about_colour`): live, it called the palette's own background/text "not from the palette"; the placeholder-rectangles finding was right. 49 s for 2 shots.
  - *Animated websites* (`site.py`, stack `site`): skeleton index.html (header/nav, hero, footer), css/styles.css (palette as CSS variables with --bg/--text/--accent aliases, reveal transitions, keyframes, phone breakpoint, reduced motion), js/main.js (`reveal(root, Observer)`, `navToggle(root)` taking fakes), tests/site.test.mjs (6 checks). Page check `.studio/site_check.html`: the page in a same-origin srcdoc iframe with an error hook, at 1280×800 and 390×844, scrolled through; reports errors, sections/ids, keyframes, transitions, overflow, alt text, broken anchors, external resources (the injected `<base>` excluded). Screenshots: desktop 1280×800, phone 390×844, overview 1280×2400 (vh sections grow in tall windows). Headless iframes under-count IntersectionObserver reveals, so reveal visibility isn't a pass/fail signal. Site playtest problems: script errors, missing planned sections, phone overflow, no motion, no reduced-motion rule, broken anchors, external resources, missing alt. Planner/researcher have site variants (`Stack.what`, `Stack.planning`, `SITE_RESEARCH_SYSTEM`).
  - *Found on the first website run:* (1) the tester wrote JS tests that didn't parse (regex with an unmatched `)`, then unescaped quotes); now `Stack.syntax_error` (`node --check` / `compile`) runs right after the tester and before any judgement, `Studio.tests_must_parse` has it rewritten with the exact error twice, then drops the file (memory note); the splitter is told steps never edit tests. (2) Given a precise diagnosis ("reveal()'s parameters are swapped") three times, whole-file rewrites kept breaking/undoing code; the coder now has `replace_in_file(path, old, new)` (exact, unique match; helpful errors when the text isn't there or appears twice; respects `refuse_write`) and is told to use it for existing files, `write_file` only for new ones. qwen3:8b used it correctly on that bug (one miss, read the file, then the exact fix, rest intact).
  - *Routing:* `project.stack_for` → `site` for website/landing page/portfolio/home page/web page (no game words); `pc/tools.start_coding` sends every new website to the studio too (user's choice), "Claude" by name still means Claude Code; reply "I'm starting the studio on: …". `/sites/<folder>/` serves site-stack projects like `/games/`.
- **Web games on the iPhone:** `GET /games/<folder>/…` serves a web-stack studio project's files (only folders with `.studio/state.json` and `"stack": "web"`, never `.studio`, `.git`, `docs`, `tests`; no token, like `/app`); the web stack's play text gives `JARVIS_PUBLIC_URL/games/<name>/`.

### Coder training — `jarvis/training/`, separate venv `.venv-train`

- User asked 7 Nov 2026: multi-LoRA, one adapter per skill trained on trusted GitHub repos only, then combined. Repos (in `data/training/repos`): TheAlgorithms/Python (python), free-python-games + pygame examples (games), MDN learning-area + dom-examples (web), radare2-book, angr-doc + solves, GhidraSnippets, Capstone bindings (reverse).
- `datasets.py` → `data/training/sets/{skill}.jsonl` (+ 5% `.eval.jsonl` held out; examples over 3500 chars are skipped, never cut).
- `train.py`: QLoRA on `unsloth/Qwen2.5-Coder-7B-Instruct-bnb-4bit`, r=16 on all attention/MLP projections, plain PyTorch loop. **Smart App Control blocks pyarrow's `_acero` DLL**, so Unsloth / TRL / HF `datasets` can't be used. To fit 8 GB: input embedding on the CPU (`llm_int8_enable_fp32_cpu_offload` set inside the checkpoint's own config), gradient checkpointing, loss only on answer tokens in 256-row chunks. Stop Jarvis first (needs the whole GPU, peak 7.7 GB).
- `merge.py`: TIES (trim to 50%, sign election, disjoint mean) of each layer's ΔW, SVD back to rank 64.
- `export.py`: **Ollama 0.40 dropped LoRA adapters**, so the adapter is baked into bf16 weights (NF4 dequantised tensor by tensor). Ollama 0.40 also sends safetensors imports to its MLX engine ("unsupported MLX architecture: Qwen2ForCausalLM") and won't quantize a GGUF at create time, so: llama.cpp's `convert_hf_to_gguf.py` (shallow clone in `data/training/llama.cpp`, user approved running it 7 Nov 2026) → f16 GGUF → `quantize.py` → Q4_1 (4.66 GB; output layer Q8_0; gguf-py can't write K-quants and llama-quantize would need a C++ build) → `ollama create` with the stock model's Modelfile. The baseline (`jarvis-coder-base`) goes through the same steps without an adapter. Temporary files: ~14 GB bf16 + 15 GB f16, deleted afterwards unless `--keep`.
- `bench.py`: HumanEval pass@1 at temperature 0 plus held-out TheAlgorithms functions checked against their doctests; generated code runs with `python -I` in a temp folder, 15 s timeout. Results in `data/training/bench/`.
- `data/training/run_all.sh` trains all four; `data/training/after_training.sh` merges, exports, benchmarks.
- **Result, 7 Nov 2026: the tuned model is worse; Jarvis keeps the stock `qwen2.5-coder:7b`.** Held-out loss fell for every skill (python 0.75→0.50, web 1.01→0.69, reverse 2.25→1.87, games 0.72→0.13), but HumanEval pass@1: stock 83.5%, `jarvis-coder-base` (same export, no adapter) 84.1%, merged `jarvis-coder` 12.8%; the Python adapter alone 2/20 vs base 19/20. Not an export or merge bug: on training-style prompts ("Write a Python function `is_prime(number)`: …") the tuned model writes correct TheAlgorithms-style code. Cause: "function from a one-line description" pairs taught it to reproduce the repo's docstrings, so given a prompt that already has the docstring (every HumanEval problem) it echoes it and stops, and it lost the base's general instruction-following. Lower held-out loss on repo-style answers didn't mean better coding; only a behaviour benchmark shows that. The repos still help through `search_references` (lookup at run time, no weight change).
- **Retrain, same day (user asked), also failed; stopped after two gated attempts.** Fixes tried: half the function examples given as stubs to finish (`_completion_turn`), each function's module imports added to its answer (1 in 3 had lost them, teaching `floor()` without `import`), lr 2e-4 → 5e-5, games at most 2 passes, 600 examples, dropout 0.05. Python adapter on 20 HumanEval problems: 4/20, then 3/20 (base 19/20); with the training system prompt 6/20. Checked and ruled out: export (PyTorch generation shows the same stub-echo-then-stop), Ollama's repeat penalty, system prompt. Teacher-forced loss on HumanEval's correct answers *improves* with the adapter (0.17 → 0.11) while free generation gets worse: the adapter tips the "body vs close the block after the docstring" choice. Conclusion: a few hundred repo snippets on 8 GB won't beat an 84% coder; don't repeat without a new idea. `data/training/retrain.sh` has the gated loop (Python first, stops below 17/20).

### Desktop app — `python -m jarvis up`, `jarvis/desktop/`

- One process runs everything (`runtime.py`): API (uvicorn thread), "hey jarvis" voice loop (thread), ntfy (child process, or reuses one already running), with one shared Ollama client, Whisper and Kokoro. A lock file (`data/jarvis.lock`) stops a second copy. Without a console (pythonw at login) output goes to `data/logs/jarvis.log`.
- Interface (`hud.py`, PySide6 widgets drawn with QPainter; QtWebEngine isn't available): an animated orb that changes with the voice state (idle / listening with mic level / thinking / speaking), today's events, next meal, important items, CPU/memory/GPU/VRAM (`sysinfo.py`, ctypes + nvidia-smi, no psutil because of Smart App Control), the last exchanges (voice, phone, web app and typed), a text box and a Listen button. Original design, not copied from any film.
- Closing it (✕, Alt+F4) hides it and a small orb follows the mouse (click-through, always on top). **Ctrl+Alt+J** or the tray icon brings it back; tray menu → Quit Jarvis stops everything.
- Components talk through `desktop/bus.py` (state, level, heard, reply, status, ui events); the voice loop's `on_event` and the API's `on_exchange` feed it.
- Starts at login: `python -m jarvis autostart on|off|status` writes/removes `Jarvis.lnk` in the Startup folder (`pythonw -m jarvis up`). Turned on 7 Nov 2026.

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
7. Answers sound like a person (`speak.py`, user asked 8 Nov 2026): "Tomorrow for dinner you have dal makhani and paneer, with rice and chapatti, and for dessert to go with it, gulab jamun." Every dish is still the row's own words (lower-cased); it drops everyday fillers (bread/butter/jam, plain tea/coffee/milk, pickle, papad, generic fruit), joins "With cheese sauce" to the dish before it, puts rice and breads after "with", and gives desserts ("for something sweet" at breakfast/snacks) and special drinks (lassi, lemonade) their own clause. Only fillers → read everything. "Diwali Special Menu" → "it's the Diwali special menu".

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
5. **iPhone online** — Tailscale, "Jarvis" Shortcut (Dictate → POST /ask → Speak), "Send to Jarvis" Share Sheet Shortcut (POST /send). **✅ Done 6 Nov 2026 (one gap):** both Shortcuts built; Jarvis answers by voice on the iPhone; from the phone, the user's real menu PDF was stored exactly (28/28, dated 2–8 Nov), and a shared department email (PDF) became an event (Mon 9 Nov), with a resend caught as a duplicate. Gap: a WhatsApp *text* share hasn't been seen from the phone yet; the same `/send` text path passes over Tailscale HTTPS from the PC and in 5 real-model WhatsApp tests.
   Done when: a forwarded WhatsApp message becomes an event, and a shared menu ~~photo~~ **PDF** becomes a stored menu. *Changed by the user, 6 Nov 2026: menus from photos are refused (the local vision model read only ~72% of dish lines exactly on a real notice-board photo, even day by day with JSON output); photos of notices/posters still add their events.*
6. **Web app + calendar feed** — agenda page, mic button over HTTPS, `calendar.ics`. **✅ Done 6 Nov 2026:** on the user's iPhone, added to the Home Screen; the mic asked and answered (`/ask-audio` → Whisper → qwen3 → `/speak` in Jarvis's voice), and the iPhone Calendar subscribed to the feed (97 events incl. festivals).
7. **Offline fallback** — Shortcut offline branch (on-device Use Model → Dictionary output), `jarvis-queue.txt`, sync, Apple Reminders "Jarvis" list mirror for important items.
   Done when: an event captured in airplane mode appears on the PC after reconnecting, exactly once.
8. **Polish** — recurring events, clash warnings, spoken morning briefing with today's meals, ~~auto-start at Windows login~~ (done with `jarvis up`), timetable files → recurring events.

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
.venv\Scripts\python -m jarvis phone-setup     # phone token + tailscale serve :8443 for the iPhone Shortcuts
.venv\Scripts\python -m jarvis up              # everything + the desktop interface (what runs at login); --hidden, --no-voice
.venv\Scripts\python -m jarvis autostart on    # start at Windows login (off / status)
.venv\Scripts\python -m jarvis.studio new "make me a game like Shadow Fight 2"   # the game studio (also: resume, status, graph, list)
```

## Open questions (ask the user before depending on these)

- Which iPhone model? Offline parsing needs an Apple Intelligence-capable iPhone on iOS 26+; otherwise the offline branch uses a text prompt + date picker.
- Any Mac access later? Would allow a native iOS app.
