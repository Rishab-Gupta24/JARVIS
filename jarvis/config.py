"""Settings, read from the environment and `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Every date the model sees or produces is interpreted in this zone.
TZ = ZoneInfo(os.getenv("JARVIS_TZ", "Asia/Dubai"))


@dataclass(frozen=True)
class Settings:
    token: str = ""
    phone_token: str = ""  # shorter token for the iPhone Shortcuts (python -m jarvis phone-setup)
    feed_token: str = ""   # read-only, goes in the calendar.ics address (iOS Calendar can't send headers)
    public_url: str = ""   # https://<pc>.<tailnet>.ts.net:8443
    db_path: str = str(ROOT / "data" / "jarvis.db")
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    vision_model: str = "qwen2.5vl:7b"  # reads photos and scanned PDFs
    files_dir: str = str(ROOT / "data" / "files")
    inbox_dir: str = str(ROOT / "inbox")
    host: str = "127.0.0.1"
    port: int = 8765
    # voice (milestone 3)
    models_dir: str = str(ROOT / "data" / "models")
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "cuda"          # or "cpu" (use whisper_model=small there)
    whisper_compute: str = "int8_float16"  # ~1 GB of VRAM next to qwen3:8b
    tts_engine: str = "kokoro"            # or "piper" (much faster, rougher)
    tts_voice: str = "bm_george"          # kokoro voice, or a piper voice name like en_GB-alan-medium
    tts_speed: float = 0.95               # a touch slower than normal: calm, measured delivery
    wake_threshold: float = 0.4
    mic_device: str | None = None         # None = Windows default; or a name/index from `jarvis devices`
    keep_alive: str = "30m"               # how long Ollama keeps the model loaded; voice mode uses -1
    # festivals and public holidays fetched from the internet (jarvis/calendar/holidays.py)
    holiday_regions: tuple[str, ...] = ("india", "uae")
    holiday_refresh_days: int = 7
    # reminders to the phone (milestone 4; written by `python -m jarvis ntfy-setup`)
    ntfy_url: str = ""
    ntfy_topic: str = ""
    ntfy_token: str = ""
    # PC control: projects Jarvis can open by name ("jarvis=C:\path;other=D:\path"), plus git repos it finds
    projects: dict = field(default_factory=lambda: {"Jarvis": str(ROOT)})
    # where "build me a game" puts a new project
    new_projects_dir: str = str(Path.home() / "Projects")
    # who does the coding: "claude" (Claude Code, the default) or "local" (jarvis.coder with a local model)
    coder: str = "claude"
    coder_model: str = "qwen2.5-coder:7b"
    # the game studio (jarvis/studio): planner/researcher/reviewer model, plugins, time budget, helper
    studio_planner_model: str = "qwen3:8b"
    # The studio's tester/coder. Empty = the planner model: one model shared with Jarvis's voice brain, so the
    # GPU never swaps and Jarvis keeps answering during a build (seen 7 Nov 2026: answers timed out at 300 s
    # while qwen2.5-coder held the GPU). Set to qwen2.5-coder:7b when Jarvis isn't needed meanwhile.
    studio_coder_model: str = ""
    # The thinker: plan, design, split, test check, playtest analysis, with Qwen3's thinking on. Empty = the
    # planner model. A bigger model here (qwen3:14b) swaps in only at those rare decisions.
    studio_thinker_model: str = ""
    studio_plugins: tuple = ("ponytail", "test_first", "review", "playtest", "vision", "phone_updates")
    studio_ponytail: str = "full"
    studio_hours: float = 6.0
    studio_helper: str = "claude"  # "claude" or "none"
    studio_helper_calls: int = 3   # Claude Code calls per project: a last resort after test checks and splitting

    @classmethod
    def from_env(cls) -> Settings:
        def path(var: str, default: str) -> str:
            p = Path(os.getenv(var, default))
            return str(p if p.is_absolute() else ROOT / p)

        return cls(
            token=os.getenv("JARVIS_TOKEN", ""),
            phone_token=os.getenv("JARVIS_PHONE_TOKEN", ""),
            feed_token=os.getenv("JARVIS_FEED_TOKEN", ""),
            public_url=os.getenv("JARVIS_PUBLIC_URL", ""),
            db_path=path("JARVIS_DB", cls.db_path),
            files_dir=path("JARVIS_FILES", cls.files_dir),
            inbox_dir=path("JARVIS_INBOX", cls.inbox_dir),
            ollama_url=os.getenv("OLLAMA_URL", cls.ollama_url),
            ollama_model=os.getenv("OLLAMA_MODEL", cls.ollama_model),
            vision_model=os.getenv("OLLAMA_VISION_MODEL", cls.vision_model),
            models_dir=path("JARVIS_MODELS", cls.models_dir),
            whisper_model=os.getenv("WHISPER_MODEL", cls.whisper_model),
            whisper_device=os.getenv("WHISPER_DEVICE", cls.whisper_device),
            whisper_compute=os.getenv("WHISPER_COMPUTE", cls.whisper_compute),
            tts_engine=os.getenv("TTS_ENGINE", cls.tts_engine),
            tts_voice=os.getenv("TTS_VOICE", cls.tts_voice),
            tts_speed=float(os.getenv("TTS_SPEED", cls.tts_speed)),
            wake_threshold=float(os.getenv("WAKE_THRESHOLD", cls.wake_threshold)),
            mic_device=os.getenv("MIC_DEVICE") or None,
            keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", cls.keep_alive),
            holiday_regions=tuple(r.strip() for r in os.getenv("HOLIDAY_REGIONS", ",".join(cls.holiday_regions))
                                  .split(",") if r.strip()),
            holiday_refresh_days=int(os.getenv("HOLIDAY_REFRESH_DAYS", cls.holiday_refresh_days)),
            ntfy_url=os.getenv("NTFY_URL", ""),
            ntfy_topic=os.getenv("NTFY_TOPIC", ""),
            ntfy_token=os.getenv("NTFY_TOKEN", ""),
            projects={"Jarvis": str(ROOT), **{
                k.strip(): v.strip() for k, _, v in
                (p.partition("=") for p in os.getenv("JARVIS_PROJECTS", "").split(";") if "=" in p)}},
            new_projects_dir=os.getenv("JARVIS_NEW_PROJECTS_DIR", str(Path.home() / "Projects")),
            coder=os.getenv("JARVIS_CODER", cls.coder).strip().lower(),
            coder_model=os.getenv("CODER_MODEL", cls.coder_model),
            studio_planner_model=os.getenv("STUDIO_PLANNER_MODEL", cls.studio_planner_model),
            studio_coder_model=os.getenv("STUDIO_CODER_MODEL", cls.studio_coder_model).strip(),
            studio_thinker_model=os.getenv("STUDIO_THINKER_MODEL", cls.studio_thinker_model).strip(),
            studio_plugins=tuple(p.strip().lower() for p in os.getenv("STUDIO_PLUGINS", ",".join(cls.studio_plugins))
                                 .split(",") if p.strip()),
            studio_ponytail=os.getenv("STUDIO_PONYTAIL", cls.studio_ponytail).strip().lower(),
            studio_hours=float(os.getenv("STUDIO_HOURS", cls.studio_hours)),
            studio_helper=os.getenv("STUDIO_HELPER", cls.studio_helper).strip().lower(),
            studio_helper_calls=int(os.getenv("STUDIO_HELPER_CALLS", cls.studio_helper_calls)),
            host=os.getenv("JARVIS_HOST", cls.host),
            port=int(os.getenv("JARVIS_PORT", cls.port)),
        )
