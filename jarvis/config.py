"""Settings, read from the environment and `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass
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
    wake_threshold: float = 0.5
    mic_device: str | None = None         # None = Windows default; or a name/index from `jarvis devices`
    keep_alive: str = "30m"               # how long Ollama keeps the model loaded; voice mode uses -1
    # festivals and public holidays fetched from the internet (jarvis/calendar/holidays.py)
    holiday_regions: tuple[str, ...] = ("india", "uae")
    holiday_refresh_days: int = 7
    # reminders to the phone (milestone 4; written by `python -m jarvis ntfy-setup`)
    ntfy_url: str = ""
    ntfy_topic: str = ""
    ntfy_token: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        def path(var: str, default: str) -> str:
            p = Path(os.getenv(var, default))
            return str(p if p.is_absolute() else ROOT / p)

        return cls(
            token=os.getenv("JARVIS_TOKEN", ""),
            phone_token=os.getenv("JARVIS_PHONE_TOKEN", ""),
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
            wake_threshold=float(os.getenv("WAKE_THRESHOLD", cls.wake_threshold)),
            mic_device=os.getenv("MIC_DEVICE") or None,
            keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", cls.keep_alive),
            holiday_regions=tuple(r.strip() for r in os.getenv("HOLIDAY_REGIONS", ",".join(cls.holiday_regions))
                                  .split(",") if r.strip()),
            holiday_refresh_days=int(os.getenv("HOLIDAY_REFRESH_DAYS", cls.holiday_refresh_days)),
            ntfy_url=os.getenv("NTFY_URL", ""),
            ntfy_topic=os.getenv("NTFY_TOPIC", ""),
            ntfy_token=os.getenv("NTFY_TOKEN", ""),
            host=os.getenv("JARVIS_HOST", cls.host),
            port=int(os.getenv("JARVIS_PORT", cls.port)),
        )
