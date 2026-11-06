# Jarvis — Local Voice Assistant Architecture

Nov 5, 2026

## Goals and constraints

Jarvis's brain runs on the Windows PC with an NVIDIA GPU. The iPhone is a remote for it when it can reach the PC, and a small offline helper when it can't.

What Jarvis must do:

- **Talk:** say a wake word, speak naturally, hear the answer aloud on the PC.
- **Own the calendar:** events live in Jarvis's own database on the PC, not iCloud or Google.
- **Capture from the phone:** send text, a voice note or shared content (a message, a screenshot's text) and it lands on the calendar.
- **Answer from files:** send a mess menu, timetable or schedule once, then ask things like "what's for breakfast this Wednesday?"
- **Remind about important things:** anything flagged important alerts the iPhone at set lead times, even away from the PC.
- **Work on iPhone both ways:** full Jarvis through the PC when reachable; basic capture and reminders when not.

Constraints that shape the design:

- **Local first:** speech, the AI model and calendar data stay on the user's devices. The one exception is a content-free wake-up through Apple's push service so alerts reach the iPhone.
- **No Mac assumed:** a native iOS app needs Xcode on a Mac, so the iPhone side uses Shortcuts plus a web app.
- **Offline AI on iPhone needs Apple Intelligence:** the on-device model in Shortcuts only exists on Apple Intelligence-capable iPhones running iOS 26 or later.

## How it fits together

Everything smart happens in `jarvis-core` on the PC; the iPhone either talks to it over Tailscale or, when it can't, captures locally and syncs later. Voice or phone input reaches jarvis-core, which asks Ollama what to do and writes to the calendar. The scheduler pushes important alerts out to the phone, and the phone's own Reminders mirror covers the times the PC is off.

## PC brain: the voice pipeline

One Python service, jarvis-core, chains five open-source pieces. Each is swappable, and all run offline once downloaded.

| Job | Tool | Why this one | Runs on |
| --- | --- | --- | --- |
| Wake word | openWakeWord | Ships a ready-made "hey jarvis" model; trainable for other phrases | CPU |
| End-of-speech detection | Silero VAD | Knows when you've stopped talking, so no button | CPU |
| Speech to text | faster-whisper (`large-v3-turbo`, or `small` if VRAM is tight) | Accurate, fast on CUDA, handles Indian-English accents well | GPU |
| Thinking + actions | Ollama serving a Qwen3-class model with tool calling | One-command install on Windows; tool calling via the `tools` array on `/api/chat` | GPU |
| Text to speech | Kokoro (natural) or Piper (very fast) | Both local; Piper runs well on CPU | GPU / CPU |

Request flow:

1. Audio or text arrives (PC microphone, or the iPhone over the network).
2. Speech becomes text; the text goes to the model with today's date, the time zone (Asia/Dubai) and a list of tools.
3. The model picks a tool (`create_event`, `list_events`, `update_event`, `delete_event`, `set_important`) and fills in its arguments as JSON.
4. Python validates the arguments (a real date, end after start) before anything touches the calendar. The model never writes to the database directly.
5. Jarvis reads back what it did: "Added DBMS quiz, Monday 10 am, marked important."

The same service exposes a small HTTP API (FastAPI) so the iPhone, a browser or a future app all use the same brain.

## Calendar and reminders engine

The calendar is a SQLite file on the PC; a scheduler inside jarvis-core turns "important" events into phone alerts, with a phone-side backup so a sleeping PC never means a missed reminder.

| Table | Key fields | Notes |
| --- | --- | --- |
| events | id, title, start, end, all_day, location, notes, important, recurrence (RRULE), source, client_uuid, updated_at | client_uuid lets the phone resend an offline item without creating a duplicate |
| reminders | event_id, fire_at, channel, status (pending / sent / acknowledged) | Built automatically from the event; important events default to 1 day, 1 hour and 10 minutes before |

| Endpoint | Does |
| --- | --- |
| `POST /capture` | Free text or audio → model → creates or edits events → spoken-style confirmation |
| `GET /events?from=&to=` | Lists events, e.g. "what's on tomorrow" |
| `POST` / `PATCH` / `DELETE /events` | Direct edits without the AI, for the web app's buttons |
| `POST /sync` | Phone uploads its offline queue; gets back the next 7 days of important items |
| `GET /calendar.ics` | Read-only feed to subscribe to on the iPhone |

How an important reminder reaches the user:

1. **At the PC:** a loop checks the `reminders` table every 20 seconds (it lives in the same SQLite file, so it survives restarts), sends what's due, and in voice mode Jarvis says it aloud.
2. **On the iPhone:** the same moment publishes to a private ntfy topic; the ntfy iOS app shows it as a notification. A self-hosted ntfy server can't wake an iPhone on its own, so it forwards a content-free poll request through ntfy.sh to Apple's push service, and the phone then fetches the message from the user's server.
3. **Safety net when the PC is off:** on every sync, the phone copies upcoming important items into a "Jarvis" list in Apple Reminders with alert times. Those fire from the iPhone itself. Jarvis stays the source of truth; the Reminders list is only a mirror.

## Documents: menus, timetables and other files

Send Jarvis a file once and it turns it into data it can answer from, so "what's for breakfast this Wednesday?" becomes an exact lookup, not a guess.

1. **Receive:** iPhone Share Sheet ("Send to Jarvis") or a watched `inbox/` folder on the PC.
2. **Read:** text PDFs and Excel/CSV parsed directly. Photos go through a local vision model in Ollama, or the iPhone's Extract Text from Image before upload.
3. **Classify:** mess menu, class timetable, exam schedule, or other.
4. **Structure:** a menu becomes rows (day, meal, items). A timetable becomes recurring events, added only after confirmation. Anything else is kept as searchable text.
5. **Confirm:** "Got the mess menu for 9 to 15 Nov, 21 meals." If the file doesn't say which week or whether it repeats, Jarvis asks.

| Table | Key fields | Notes |
| --- | --- | --- |
| documents | id, kind, filename, received_at, valid_from, valid_to, repeats_weekly, raw_text | Original file kept in `data/files/` |
| menu_items | document_id, weekday, date, meal, items | `date` set for a dated menu; a rotating menu uses weekday only |

| Name | Does |
| --- | --- |
| `POST /documents` | Uploads a file from the phone or web app |
| `get_menu(date, meal)` | Turns "this Wednesday" into a date in Asia/Dubai time and returns that meal |
| `find_in_menu(dish)` | Answers "when is biryani this week?" |
| `search_documents(query)` | Answers questions about any other stored file |

Rules that keep answers honest:

- Menu answers come only from stored rows, never from the model's guess.
- If no menu covers the date asked about, Jarvis says so rather than reusing an old week.
- When two menus overlap, the newest wins.
- The spoken morning briefing can include today's breakfast.

## iPhone online mode

When the iPhone can reach the PC it is a microphone and screen for the PC's brain, connected through Tailscale so it works from campus, home or anywhere.

- **"Hey Siri, Jarvis" Shortcut** (also on the Action Button or Back Tap): Dictate Text → `POST /capture` with the token → Speak Text with the reply. If the request fails, the same Shortcut drops into offline mode instead of erroring.
- **"Send to Jarvis" in the Share Sheet:** share a WhatsApp message, an email or a screenshot. For images the Shortcut runs Extract Text from Image first, then sends the text with a note like "add this to my calendar".
- **Jarvis web app** (served by jarvis-core, added to the Home Screen): today's agenda, important items, edit buttons, and a mic button that uploads audio so the PC's Whisper transcribes it.
- Plus the read-only `calendar.ics` subscription so Jarvis's events appear in Apple's Calendar app.

The web app's microphone only works over HTTPS. `tailscale serve` gives the PC an HTTPS address inside the private network with a valid certificate, so no domain or port-forwarding is needed.

## iPhone offline fallback

With no connection to the PC, the iPhone captures and reminds on its own using Apple's on-device model, then hands everything to Jarvis at the next sync. It does not try to be the full Jarvis.

1. **Hear it:** Dictate Text, on-device.
2. **Understand it:** the Use Model action set to On-Device, prompted with today's date and the words. It returns a structured Dictionary (title, start, end, important).
3. **Queue it:** append one JSON line to `jarvis-queue.txt` in the Shortcuts folder: parsed fields, raw words, and a unique id.
4. **Protect it:** if important, add it to the "Jarvis" Reminders list with alerts.
5. **Confirm it:** speak "Saved offline, I'll sync it with your PC."

Sync happens whenever an online request succeeds, plus Shortcuts automations on joining home Wi-Fi and every morning. The PC dedupes by unique id and re-reads the raw words with its larger model. Each sync saves the next 7 days to a file, so "what's on tomorrow?" still works offline. Without Apple Intelligence, step 2 becomes two quick prompts ("What is it?" and a date picker).

## Networking and security

- Tailscale on the PC and iPhone: one private network across Wi-Fi, mobile data and campus networks. No port-forwarding.
- jarvis-core listens only on localhost, published to the tailnet by `tailscale serve`.
- A long random API token lives in the Shortcut and the web app; requests without it are rejected.
- ntfy topics are password-protected.
- Nightly backup of `jarvis.db`.

| Data | Leaves your devices? |
| --- | --- |
| Voice audio and transcripts | Never |
| Calendar events | Never; phone and PC talk over encrypted Tailscale |
| AI processing | Never; Ollama on the PC, Apple's on-device model on the phone |
| Reminder alerts | Only a content-free wake-up via ntfy.sh and Apple's push service |
| Reminders mirror | Through iCloud, if iCloud Reminders is on |
| Holiday calendars | Downloaded only: public India/UAE holiday files from Google, weekly. Nothing about you is sent beyond an ordinary web request |

## Risks

| Risk | Effect | Mitigation |
| --- | --- | --- |
| GPU memory is tight (8 GB) | Model and Whisper compete for VRAM; replies slow down | Smaller model or Whisper small; Ollama unloads idle models |
| PC sleeps or is off | No voice, no PC alerts | "Never sleep when plugged in"; the iPhone Reminders mirror still fires |
| Model misreads a date | Event on the wrong day | Python resolves dates, spoken read-back, raw words kept for correction |
| iOS can't listen for a wake word in the background | No always-on "Hey Jarvis" on the phone | "Hey Siri, Jarvis" or the Action Button |
| ntfy on iOS can be flaky | A late or missed phone alert | The Reminders mirror is the backstop for important items |

## Sources

- Best local LLMs for tool calling, LocalAI Master
- Self-hosted ntfy push alerts, StackHarbor
- The Use Model action in Shortcuts, MacStories
- Apple Intelligence in Shortcuts on iOS 26, 9to5Mac
