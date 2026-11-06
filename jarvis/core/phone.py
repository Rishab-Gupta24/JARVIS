"""`python -m jarvis phone-setup`: make Jarvis reachable from the iPhone.

- JARVIS_PHONE_TOKEN: a token for the iPhone Shortcuts, readable enough to type once
  (20 characters, no look-alikes), kept in .env and revocable on its own.
- `tailscale serve` on HTTPS port 8443 -> Jarvis on 127.0.0.1:8765. Tailnet only, like
  ntfy on 443. Repeating it is harmless.
"""

from __future__ import annotations

import secrets
import subprocess

from jarvis.config import ROOT
from jarvis.notify.server import TAILSCALE, _READABLE, read_env, tailscale_url, write_env

JARVIS_PORT = 8765
HTTPS_PORT = 8443


def new_phone_token() -> str:
    return "".join(secrets.choice(_READABLE) for _ in range(20))


def setup(env_path=ROOT / ".env") -> dict[str, str]:
    base = tailscale_url()
    if base is None:
        raise RuntimeError("Tailscale isn't signed in on this PC.")
    env = read_env(env_path)
    values = {
        "JARVIS_PHONE_TOKEN": env.get("JARVIS_PHONE_TOKEN") or new_phone_token(),
        "JARVIS_PUBLIC_URL": f"{base}:{HTTPS_PORT}",
    }
    out = subprocess.run([str(TAILSCALE), "serve", "--bg", "--yes", f"--https={HTTPS_PORT}",
                          f"http://127.0.0.1:{JARVIS_PORT}"], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"tailscale serve failed: {(out.stderr or out.stdout).strip()[-300:]}")
    write_env(env_path, values)
    return values
