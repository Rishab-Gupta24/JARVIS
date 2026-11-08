# Jarvis

A personal voice assistant that runs entirely on a Windows PC with an NVIDIA GPU and can be used from an iPhone. The language model, speech recognition, voice and data all stay on the PC; nothing about you is sent to a cloud service.

Say **"Hey Jarvis"** and talk to it, type to it, or use it from your phone.

## What it does

- **Calendar it owns.** Add, move, cancel and look up events in plain speech ("DBMS quiz next Monday at 10, it's important"). Dates are worked out in Python, not by the model, and every answer reads the date back so a wrong guess is caught. Festivals and public holidays can be imported automatically.
- **Reminders on your phone.** Anything marked important sends push notifications to the iPhone (a day, an hour and 10 minutes before) through a self-hosted [ntfy](https://ntfy.sh) server.
- **Alarms and timers.** "Wake me up at 7", "alarm every weekday at 6:30", "pasta timer for 10 minutes". They ring on the PC and on the phone; saying "Hey Jarvis" stops a ringing alarm.
- **Answers from your files.** Send it a mess menu (PDF or Excel), a timetable or a notice and ask about it later: "What's for dinner tomorrow?" → "Tomorrow for dinner you have dal makhani and paneer, with rice and chapatti, and for dessert to go with it, gulab jamun." Menu answers only ever come from the file, never from the model's imagination.
- **PC control.** Open and close apps, open projects, folders and websites, media keys, lock the screen, take screenshots. Anything else is proposed as a PowerShell command and only runs after you say yes; destructive commands are refused outright.
- **Coding.** "Build me a snake game" creates a project folder and starts a [Claude Code](https://claude.com/claude-code) session on it.
- **Looking things up.** Definitions, facts and things it doesn't know are searched online (dictionary, DuckDuckGo, Wikipedia), and the answer names its source. Only the search words leave the PC.
- **Autocorrect for names.** Misheard or misspelt app and project names are fixed ("open could code" → Claude Code); when it isn't sure it asks "Did you mean …?" and remembers your answer.
- **Desktop app.** An animated orb interface with today's events, the next meal, important items and system stats; close it and a small orb follows the mouse while Jarvis keeps listening.
- **iPhone.** Apple Shortcuts ("Jarvis" to talk, "Send to Jarvis" from the share sheet), a home-screen web app with a mic button, and a calendar feed the iPhone Calendar can subscribe to.

## How it works

```
iPhone (Shortcut / web app) ──Tailscale──► Jarvis core (FastAPI, Python)
PC mic ─► wake word ─► VAD ─► Whisper ───►      │
                                                ├─► Ollama (local LLM with tool calling)
                                                ├─► SQLite (events, reminders, alarms, documents)
                                                └─► ntfy ─► push to the iPhone
```

The model picks a tool and copies your words into it; Python validates everything (real dates, end after start, which app, which file) before anything changes. Guards catch the model's known mistakes, for example answering about food, alarms or PC actions without actually calling a tool.

| Job | Tool |
| --- | --- |
| Wake word | openWakeWord ("hey jarvis") |
| End of speech | Silero VAD |
| Speech to text | faster-whisper `large-v3-turbo` on the GPU |
| Language model | Ollama, `qwen3:8b` |
| Voice | Kokoro (`bm_george`), or Piper for speed |
| API | FastAPI |
| Phone push | self-hosted ntfy, reached over Tailscale |
| Desktop interface | PySide6 |

More detail: [docs/architecture.md](docs/architecture.md). iPhone setup: [docs/iphone-setup.md](docs/iphone-setup.md).

## Requirements

- Windows 10/11, Python 3.12
- An NVIDIA GPU with 8 GB of VRAM (tested on an RTX 5050 Laptop GPU)
- [Ollama](https://ollama.com) with `qwen3:8b` pulled
- Optional: [Tailscale](https://tailscale.com) for the iPhone, ntfy for push notifications, the Claude Code CLI for coding tasks

## Setup

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
copy .env.example .env          # then fill in your tokens and settings
ollama pull qwen3:8b
.venv\Scripts\python -m jarvis check
```

## Running

```
.venv\Scripts\python -m jarvis up              # everything, with the desktop interface
.venv\Scripts\python -m jarvis autostart on    # start at Windows login
.venv\Scripts\python -m jarvis                 # typed chat in the terminal
.venv\Scripts\python -m jarvis voice           # voice only
.venv\Scripts\python -m jarvis serve           # API only (127.0.0.1:8765)
.venv\Scripts\python -m jarvis ingest menu.pdf # add a file
.venv\Scripts\python -m jarvis ntfy-setup      # set up phone notifications
.venv\Scripts\python -m jarvis phone-setup     # set up the iPhone Shortcuts
.venv\Scripts\python -m jarvis mic-test        # check the microphone and wake word
```

## Tests

```
.venv\Scripts\python -m pytest                 # everything (model-backed tests skip without Ollama)
.venv\Scripts\python -m pytest -m llm -v       # just the tests that use the real model
```

Tests pin the clock to a fixed date, so date handling is checked exactly.

## Privacy and security

- The API listens on localhost only and is shared with your own devices through Tailscale; every request needs a token.
- Tokens and passwords live in `.env`, which is never committed.
- PC control is only offered for your own voice or typing, never for messages or files you forward (those carry other people's words).
- Internet use is limited to holiday calendars, look-ups and searches you ask for; only search words are sent.

## Status

Working: calendar, documents and menus, PC voice, phone reminders, iPhone Shortcuts, web app and calendar feed, desktop app, PC control, alarms, look-ups and autocorrect.
Planned: an offline fallback on the iPhone, recurring events, clash warnings and a spoken morning briefing.

The repository also contains two experiments that are paused: a local coding agent with a multi-agent "game studio", and fine-tuning a coding model (the tuned model was worse than the stock one, so the stock model is used).
