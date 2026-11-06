"""The ntfy server on this PC: `python -m jarvis ntfy-setup`, then `python -m jarvis ntfy-server`.

Setup writes data/ntfy/server.yml with everything declared in it (ntfy creates the
users, permissions and token at startup), so it can be re-run safely:
- a private random topic;
- user "jarvis" may only post to it (with an access token, kept in .env);
- user "phone" may only read it (the password you type into the iPhone ntfy app, in .env);
- everyone else is denied;
- upstream ntfy.sh only relays a content-free "poll" signal so iOS wakes the app.
"""

from __future__ import annotations

import json
import re
import secrets
import string
import subprocess
from pathlib import Path

from jarvis.config import ROOT

NTFY_DIR = ROOT / "data" / "ntfy"
EXE = NTFY_DIR / "ntfy.exe"
CONFIG = NTFY_DIR / "server.yml"
PORT = 2586
LOCAL_URL = f"http://127.0.0.1:{PORT}"
TAILSCALE = Path(r"C:\Program Files\Tailscale\tailscale.exe")
_READABLE = "".join(c for c in string.ascii_lowercase + string.digits if c not in "l1o0")


def tailscale_url() -> str | None:
    """https://<this-pc>.<tailnet>.ts.net when Tailscale is signed in, else None."""
    if not TAILSCALE.exists():
        return None
    try:
        out = subprocess.run([str(TAILSCALE), "status", "--json"], capture_output=True, text=True, timeout=10)
        name = json.loads(out.stdout).get("Self", {}).get("DNSName", "").rstrip(".")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return f"https://{name}" if name.endswith(".ts.net") else None


def _hash(password: str) -> str:
    # Bytes, not text: in text mode Windows turns "\n" into "\r\n" and ntfy would
    # hash the password with a stray carriage return (found when the phone got 401).
    r = subprocess.run([str(EXE), "user", "hash"], input=f"{password}\n{password}\n".encode(),
                       capture_output=True, timeout=30)
    match = re.search(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}", r.stdout.decode(errors="replace"))
    if not match:
        raise RuntimeError(f"ntfy couldn't hash the password: {r.stderr.decode(errors='replace').strip()[-200:]}")
    return match[0]


def new_token() -> str:
    return "tk_" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(29))


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Set keys in .env, keeping every other line (and comment) as it was."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.partition("=")[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in updates:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    missing = [k for k in updates if k not in done]
    if missing:
        lines += ["", "# ntfy reminders (written by python -m jarvis ntfy-setup)"]
        lines += [f"{k}={updates[k]}" for k in missing]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def config_text(public_url: str, topic: str, jarvis_hash: str, phone_hash: str, token: str) -> str:
    folder = NTFY_DIR.as_posix()
    return f"""# Written by `python -m jarvis ntfy-setup`; re-run that instead of editing.
base-url: "{public_url}"
listen-http: "127.0.0.1:{PORT}"
behind-proxy: true
cache-file: "{folder}/cache.db"
cache-duration: "48h"
auth-file: "{folder}/user.db"
auth-default-access: "deny-all"
enable-signup: false
auth-users:
  - "jarvis:{jarvis_hash}:user"
  - "phone:{phone_hash}:user"
auth-access:
  - "jarvis:{topic}:write-only"
  - "phone:{topic}:read-only"
auth-tokens:
  - "jarvis:{token}:Jarvis reminders"
# iOS: ntfy.sh relays only a content-free poll request; the phone fetches the text from here.
upstream-base-url: "https://ntfy.sh"
"""


def setup(env_path: Path = ROOT / ".env") -> dict[str, str]:
    if not EXE.exists():
        raise RuntimeError(f"ntfy isn't installed at {EXE}.")
    env = read_env(env_path)
    public = tailscale_url() or env.get("NTFY_PUBLIC_URL") or LOCAL_URL
    values = {
        "NTFY_URL": LOCAL_URL,
        "NTFY_PUBLIC_URL": public,
        "NTFY_TOPIC": env.get("NTFY_TOPIC") or f"jarvis-{secrets.token_hex(8)}",
        "NTFY_TOKEN": env.get("NTFY_TOKEN") or new_token(),
        "NTFY_PHONE_USER": "phone",
        "NTFY_PHONE_PASSWORD": env.get("NTFY_PHONE_PASSWORD")
        or "".join(secrets.choice(_READABLE) for _ in range(16)),
    }
    jarvis_password = secrets.token_urlsafe(24)  # never used: Jarvis posts with the token
    NTFY_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(config_text(public, values["NTFY_TOPIC"], _hash(jarvis_password),
                                  _hash(values["NTFY_PHONE_PASSWORD"]), values["NTFY_TOKEN"]), encoding="utf-8")
    write_env(env_path, values)
    return values


def serve() -> int:
    if not CONFIG.exists():
        print("Run `python -m jarvis ntfy-setup` first.")
        return 1
    return subprocess.call([str(EXE), "serve", "--config", str(CONFIG)])
