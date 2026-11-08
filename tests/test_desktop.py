"""Desktop pieces: the event bus, autostart shortcut, the interface rendering offscreen."""

from __future__ import annotations

import os
import sys

import pytest

from jarvis.desktop.bus import Bus


def test_bus_delivers_and_survives_broken_listener():
    bus, got = Bus(), []
    bus.subscribe(lambda e: 1 / 0)
    bus.subscribe(got.append)
    bus.publish("state", state="listening")
    assert got == [{"type": "state", "state": "listening"}]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcut")
def test_autostart_on_off(tmp_path):
    from jarvis.config import ROOT
    from jarvis.desktop import autostart

    if not (ROOT / ".venv" / "Scripts" / "pythonw.exe").exists():
        pytest.skip("no venv")
    assert not autostart.is_enabled(tmp_path)
    link = autostart.enable(tmp_path)
    assert link.exists() and autostart.is_enabled(tmp_path)
    assert autostart.disable(tmp_path) and not autostart.is_enabled(tmp_path)


def test_show_interface_tool(monkeypatch):
    from datetime import datetime

    from jarvis.config import TZ
    from jarvis.core.toolkit import ToolContext
    from jarvis.desktop import bus as busmod
    from jarvis.pc import tools as pc

    ctx = ToolContext(conn=None, now=datetime(2026, 11, 5, 9, tzinfo=TZ), source="voice", allow_pc=True)
    fresh = Bus()
    monkeypatch.setattr(busmod, "BUS", fresh)
    assert "isn't running" in pc.show_interface(pc.InterfaceArgs(), ctx).confirmation
    seen = []
    fresh.subscribe(seen.append)
    fresh.ui_running = True
    assert pc.show_interface(pc.InterfaceArgs(action="show"), ctx).confirmation == "Here I am."
    pc.show_interface(pc.InterfaceArgs(action="hide"), ctx)
    assert [e["action"] for e in seen] == ["show", "hide"]


@pytest.fixture
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    qt = pytest.importorskip("PySide6.QtWidgets")
    return qt.QApplication.instance() or qt.QApplication([])


@pytest.mark.parametrize("state", ["idle", "listening", "thinking", "speaking"])
def test_orb_renders(qapp, state):
    from jarvis.desktop.hud import Orb

    orb = Orb()
    orb.resize(300, 300)
    orb.set_state(state)
    orb.set_level(0.8)
    orb.show()
    for _ in range(5):
        orb._tick()
    img = orb.grab().toImage()
    centre = img.pixelColor(150, 150)
    assert centre.lightness() > 180  # the bright core is drawn
    out = os.environ.get("JARVIS_RENDER_DIR")
    if out:
        orb.grab().save(os.path.join(out, f"orb-{state}.png"))


def test_window_renders_with_data(qapp):
    from jarvis.desktop.hud import CursorOrb, HudWindow

    win = HudWindow()
    win.events.setText("10:00   ★ DBMS quiz\n14:00   Lab report")
    win.meal.setText("Lunch: Rajma, rice, salad")
    win.important.setText("★ DBMS quiz\n   Today at 10 am")
    win.cpu.set(0.23, "23%")
    win.ram.set(0.55, "8.7 / 16 GB")
    win.gpu.set(0.10, "10%")
    win.vram.set(0.8, "6.4 / 8 GB")
    win.add_line("YOU", "what's on today?")
    win.add_line("JARVIS", "You have DBMS quiz at 10 am, marked important.")
    win.set_state("speaking")
    win.status_line.setText("● API    ● VOICE    ● NTFY    ● MODEL")
    win.show()
    closed = []
    win.closed_to_orb.connect(lambda: closed.append(True))
    img = win.grab()
    out = os.environ.get("JARVIS_RENDER_DIR")
    if out:
        img.save(os.path.join(out, "hud.png"))
    win.close()  # closing hides to the orb, never quits
    assert closed and not win.isVisible()
    cur = CursorOrb()
    cur.start()
    cur._follow()
    assert cur.isVisible()
    cur.stop()
    assert not cur.isVisible()
