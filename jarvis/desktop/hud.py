"""The desktop interface: an animated orb with today's agenda, system gauges and the
conversation, and a small orb that follows the mouse while the interface is closed.

Original design in a holographic, arc-light style (not a copy of any film's artwork).
Drawn with QPainter: no web engine.
"""

from __future__ import annotations

import math
import random
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPen,
                           QRadialGradient)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                               QSizePolicy, QSystemTrayIcon, QVBoxLayout, QWidget)

from jarvis.config import ROOT, TZ
from jarvis.desktop.bus import BUS

STATE_COLORS = {
    "idle": QColor(56, 189, 248),       # sky
    "listening": QColor(125, 211, 252),  # brighter
    "thinking": QColor(167, 139, 250),   # violet
    "speaking": QColor(34, 211, 238),    # cyan
}
TEXT = "#cfe8ff"
MUTED = "#6f8fb3"
ACCENT = "#38bdf8"
FONT = "Bahnschrift"  # Windows' DIN-style font: technical, clean


# ---------- the orb ----------

class Orb(QWidget):
    def __init__(self, compact: bool = False, parent=None):
        super().__init__(parent)
        self.compact = compact
        self.state = "idle"
        self.level = 0.0       # mic level, smoothed
        self._target_level = 0.0
        self._t0 = time.perf_counter()
        self._spin = 0.0       # accumulated rotation; speed depends on state
        self._color = QColor(STATE_COLORS["idle"])
        rng = random.Random(7)
        self._sparks = [(rng.uniform(0, 2 * math.pi), rng.uniform(0.35, 1.0), rng.uniform(0.9, 1.08))
                        for _ in range(6 if compact else 18)]
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._timer = QTimer(self, interval=16, timeout=self._tick)
        self._timer.start()

    def set_state(self, state: str) -> None:
        self.state = state if state in STATE_COLORS else "idle"

    def set_level(self, value: float) -> None:
        self._target_level = max(0.0, min(1.0, value))

    def _tick(self) -> None:
        if not self.isVisible():
            return
        speed = {"idle": 0.35, "listening": 0.8, "thinking": 2.4, "speaking": 1.0}[self.state]
        self._spin += speed * 0.016
        self.level += (self._target_level - self.level) * 0.25
        self._target_level *= 0.9  # decays unless new mic levels keep arriving
        target = STATE_COLORS[self.state]
        c = self._color
        self._color = QColor(int(c.red() + (target.red() - c.red()) * 0.08),
                             int(c.green() + (target.green() - c.green()) * 0.08),
                             int(c.blue() + (target.blue() - c.blue()) * 0.08))
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        r = min(w, h) * 0.42
        t = time.perf_counter() - self._t0
        col = self._color
        breathe = 0.5 + 0.5 * math.sin(t * (6.0 if self.state == "speaking" else 1.6))
        energy = self.level if self.state == "listening" else (0.35 * breathe if self.state == "speaking" else 0.08)

        def pen(alpha: int, width: float, style=Qt.SolidLine) -> QPen:
            c = QColor(col)
            c.setAlpha(alpha)
            pe = QPen(c, width, style)
            pe.setCapStyle(Qt.RoundCap)
            return pe

        # glow
        glow = QRadialGradient(QPointF(cx, cy), r * (1.25 + energy * 0.25))
        g = QColor(col)
        g.setAlpha(int(70 + 80 * energy))
        glow.setColorAt(0.0, g)
        g.setAlpha(0)
        glow.setColorAt(1.0, g)
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), r * 1.25, r * 1.25)

        spin = math.degrees(self._spin)
        p.save()
        p.translate(cx, cy)

        # outer ring
        p.setBrush(Qt.NoBrush)
        p.setPen(pen(120, max(1.0, r * 0.012)))
        p.drawEllipse(QPointF(0, 0), r, r)

        # dashed segments, rotating
        seg = r * 0.88
        p.setPen(pen(200, max(1.5, r * 0.035)))
        n = 8 if self.compact else 12
        for i in range(n):
            start = spin + i * 360 / n
            p.drawArc(QRectF(-seg, -seg, 2 * seg, 2 * seg), int(-start * 16), int(-(360 / n) * 0.55 * 16))

        if not self.compact:
            # tick dial, counter-rotating
            p.save()
            p.rotate(-spin * 0.6)
            dial_out, dial_in = r * 0.78, r * 0.72
            for i in range(72):
                p.setPen(pen(170 if i % 6 == 0 else 70, 1.4 if i % 6 == 0 else 1.0))
                long = dial_in - (r * 0.04 if i % 6 == 0 else 0)
                a = math.radians(i * 5)
                p.drawLine(QPointF(math.cos(a) * long, math.sin(a) * long),
                           QPointF(math.cos(a) * dial_out, math.sin(a) * dial_out))
            p.restore()

        # sweeping arcs (fast when thinking)
        arc = r * (0.62 if not self.compact else 0.66)
        p.setPen(pen(230, max(2.0, r * 0.045)))
        sweep = 70 + 40 * math.sin(t * 2)
        for k in range(2):
            start = spin * 2.2 + k * 180
            p.drawArc(QRectF(-arc, -arc, 2 * arc, 2 * arc), int(-start * 16), int(-sweep * 16))

        # inner ring that swells with the voice
        inner = r * (0.46 + 0.06 * energy)
        p.setPen(pen(150, max(1.0, r * 0.02)))
        p.drawEllipse(QPointF(0, 0), inner, inner)

        # sparks orbiting
        p.setPen(Qt.NoPen)
        for phase, speed, radius in self._sparks:
            a = phase + self._spin * speed * 1.6
            sx, sy = math.cos(a) * r * radius, math.sin(a) * r * radius
            c = QColor(col)
            c.setAlpha(200)
            p.setBrush(c)
            p.drawEllipse(QPointF(sx, sy), max(1.2, r * 0.018), max(1.2, r * 0.018))

        # core
        core = r * (0.26 + 0.07 * energy)
        grad = QRadialGradient(QPointF(0, 0), core)
        grad.setColorAt(0.0, QColor(240, 250, 255))
        mid = QColor(col)
        mid.setAlpha(230)
        grad.setColorAt(0.55, mid)
        mid.setAlpha(0)
        grad.setColorAt(1.0, mid)
        p.setBrush(grad)
        p.drawEllipse(QPointF(0, 0), core, core)
        p.restore()
        p.end()


# ---------- the main window ----------

def _label(text: str = "", size: int = 11, color: str = TEXT, bold: bool = False, spacing: float = 0) -> QLabel:
    lab = QLabel(text)
    f = QFont(FONT, size)
    f.setBold(bold)
    if spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    lab.setFont(f)
    lab.setStyleSheet(f"color: {color}; background: transparent;")
    lab.setWordWrap(True)
    return lab


class Panel(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("panel")
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(16, 14, 16, 14)
        self.box.setSpacing(6)
        self.box.addWidget(_label(title, 9, MUTED, True, 2.5))


class Gauge(QWidget):
    """A thin labelled bar."""

    def __init__(self, name: str):
        super().__init__()
        self.name, self.value, self.text = name, 0.0, "-"
        self.setMinimumHeight(30)

    def set(self, value: float | None, text: str) -> None:
        self.value = 0.0 if value is None else max(0.0, min(1.0, value))
        self.text = text
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(QFont(FONT, 9))
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(0, 0, self.width(), 14), Qt.AlignLeft, self.name)
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(0, 0, self.width(), 14), Qt.AlignRight, self.text)
        track = QRectF(0, 19, self.width(), 5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(30, 50, 80))
        p.drawRoundedRect(track, 2.5, 2.5)
        p.setBrush(QColor(ACCENT if self.value < 0.85 else "#f59e0b"))
        p.drawRoundedRect(QRectF(0, 19, self.width() * self.value, 5), 2.5, 2.5)
        p.end()


class HudWindow(QWidget):
    closed_to_orb = Signal()
    send_text = Signal(str)
    listen = Signal()

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Window)
        self.setWindowTitle("Jarvis")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(1180, 720)
        self._drag: QPoint | None = None
        self.setStyleSheet(f"""
            #frame {{ background: rgba(4, 10, 22, 250); border: 1px solid rgba(56,189,248,90); border-radius: 22px; }}
            #panel {{ background: rgba(12, 26, 48, 200); border: 1px solid rgba(56,189,248,45); border-radius: 16px; }}
            QLineEdit {{ background: rgba(12, 26, 48, 220); color: {TEXT}; border: 1px solid rgba(56,189,248,70);
                         border-radius: 14px; padding: 10px 14px; font-family: {FONT}; font-size: 13px; }}
            QLineEdit:focus {{ border-color: {ACCENT}; }}
            QPushButton {{ color: {TEXT}; background: transparent; border: 1px solid rgba(56,189,248,70);
                           border-radius: 14px; padding: 8px 14px; font-family: {FONT}; }}
            QPushButton:hover {{ background: rgba(56,189,248,40); }}
            #chrome {{ border: 0; font-size: 16px; padding: 4px 10px; color: {MUTED}; }}
            #chrome:hover {{ color: {TEXT}; }}
        """)
        frame = QFrame(self)
        frame.setObjectName("frame")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        root = QVBoxLayout(frame)
        root.setContentsMargins(22, 14, 22, 20)
        root.setSpacing(14)

        # title bar
        bar = QHBoxLayout()
        bar.addWidget(_label("JARVIS", 15, TEXT, True, 6))
        self.status_line = _label("starting…", 9, MUTED, spacing=1)
        bar.addSpacing(16)
        bar.addWidget(self.status_line, 1)
        for text, slot in (("–", self.showMinimized), ("✕", self.close_to_orb)):
            b = QPushButton(text)
            b.setObjectName("chrome")
            b.clicked.connect(slot)
            bar.addWidget(b)
        root.addLayout(bar)

        # middle: left panel, orb, right panel
        mid = QHBoxLayout()
        mid.setSpacing(18)
        left = Panel("TODAY")
        self.clock = _label("--:--", 34, TEXT, True, 2)
        self.date = _label("", 11, MUTED)
        left.box.addWidget(self.clock)
        left.box.addWidget(self.date)
        left.box.addSpacing(6)
        self.events = _label("Loading…", 11)
        self.events.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        left.box.addWidget(self.events, 1)
        left.box.addWidget(_label("NEXT MEAL", 9, MUTED, True, 2.5))
        self.meal = _label("", 11)
        left.box.addWidget(self.meal)
        left.setFixedWidth(300)
        mid.addWidget(left)

        centre = QVBoxLayout()
        self.orb = Orb()
        self.orb.setMinimumSize(380, 380)
        self.orb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        centre.addWidget(self.orb, 1)
        self.state_label = _label("Say “Hey Jarvis”", 12, MUTED, spacing=1.5)
        self.state_label.setAlignment(Qt.AlignHCenter)
        centre.addWidget(self.state_label)
        mid.addLayout(centre, 1)

        right = Panel("SYSTEM")
        self.cpu, self.ram, self.gpu, self.vram = Gauge("CPU"), Gauge("MEMORY"), Gauge("GPU"), Gauge("VRAM")
        for g in (self.cpu, self.ram, self.gpu, self.vram):
            right.box.addWidget(g)
        right.box.addSpacing(8)
        right.box.addWidget(_label("UP NEXT · IMPORTANT", 9, MUTED, True, 2.5))
        self.important = _label("", 11)
        self.important.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        right.box.addWidget(self.important, 1)
        self.studio = _label("", 10, MUTED)  # "Studio: Ink Duel 7/18 · T8" while the game studio works
        self.studio.setWordWrap(True)
        right.box.addWidget(self.studio)
        right.setFixedWidth(300)
        mid.addWidget(right)
        root.addLayout(mid, 1)

        # conversation + input
        self.transcript = _label("", 11)
        self.transcript.setTextFormat(Qt.RichText)
        self.transcript.setMinimumHeight(64)
        self.transcript.setAlignment(Qt.AlignBottom | Qt.AlignLeft)
        root.addWidget(self.transcript)
        inp = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask or tell Jarvis… (Enter to send)")
        self.input.returnPressed.connect(self._send)
        mic = QPushButton("●  Listen")
        mic.clicked.connect(self.listen.emit)
        inp.addWidget(self.input, 1)
        inp.addWidget(mic)
        root.addLayout(inp)
        self._lines: list[str] = []

        self._clock_timer = QTimer(self, interval=1000, timeout=self._update_clock)
        self._clock_timer.start()
        self._update_clock()

    def _update_clock(self) -> None:
        now = datetime.now(TZ)
        self.clock.setText(now.strftime("%H:%M"))
        self.date.setText(now.strftime("%A, %d %B"))

    def _send(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.send_text.emit(text)

    def add_line(self, who: str, text: str) -> None:
        colour = ACCENT if who == "JARVIS" else MUTED
        safe = text.replace("&", "&amp;").replace("<", "&lt;")
        self._lines = (self._lines + [f"<span style='color:{colour}; letter-spacing:2px'>{who}</span>"
                                      f"&nbsp;&nbsp;<span style='color:{TEXT}'>{safe}</span>"])[-3:]
        self.transcript.setText("<br>".join(self._lines))

    def set_state(self, state: str) -> None:
        self.orb.set_state(state)
        self.state_label.setText({"idle": "Say “Hey Jarvis”", "listening": "Listening…",
                                  "thinking": "Thinking…", "speaking": "Speaking…"}.get(state, ""))

    def close_to_orb(self) -> None:
        self.hide()
        self.closed_to_orb.emit()

    def closeEvent(self, event) -> None:  # Alt+F4 also just hides
        event.ignore()
        self.close_to_orb()

    # drag anywhere on the window
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None


# ---------- the orb that follows the mouse ----------

class CursorOrb(QWidget):
    """Glides beside the mouse pointer while the interface is closed. Clicks pass straight
    through it, so it never gets in the way."""

    SIZE = 58
    OFFSET = QPoint(26, 22)

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(self.SIZE, self.SIZE)
        self.orb = Orb(compact=True, parent=self)
        self.orb.setGeometry(0, 0, self.SIZE, self.SIZE)
        self._pos = QPointF(QCursor.pos())
        self._timer = QTimer(self, interval=16, timeout=self._follow)

    def start(self) -> None:
        self._pos = QPointF(QCursor.pos() + self.OFFSET)
        self.move(self._pos.toPoint())
        self.show()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _follow(self) -> None:
        target = QPointF(QCursor.pos() + self.OFFSET)
        self._pos += (target - self._pos) * 0.22
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(self._pos.x(), area.left()), area.right() - self.SIZE)
        y = min(max(self._pos.y(), area.top()), area.bottom() - self.SIZE)
        self.move(int(x), int(y))


# ---------- global hotkey (Ctrl+Alt+J) ----------

class Hotkey(QAbstractNativeEventFilter):
    WM_HOTKEY = 0x0312

    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.ok = False
        if sys.platform == "win32":
            import ctypes

            MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
            self.ok = bool(ctypes.windll.user32.RegisterHotKey(None, 0x4A41, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                                                               ord("J")))

    def nativeEventFilter(self, event_type, message):
        if event_type == b"windows_generic_MSG" or event_type == "windows_generic_MSG":
            import ctypes
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == self.WM_HOTKEY and msg.wParam == 0x4A41:
                self.callback()
                return True, 0
        return False, 0


# ---------- data for the panels ----------

def snapshot(db_path: str, projects_dir: str | None = None) -> dict:
    """Today's events, the next meal and important things coming up, read straight from the DB; and the
    game studio's newest project, if any."""
    from jarvis.calendar import db, repo
    from jarvis.documents import store
    from jarvis.studio.status import hud_line

    now = datetime.now(TZ)
    today = now.date()
    conn = db.connect(db_path)
    try:
        todays = repo.list_on_day(conn, today)
        upcoming = [e for e in repo.list_events(conn, now, now + timedelta(days=14)) if e.important][:5]
        _, rows = store.menu_for_day(conn, today)
    finally:
        conn.close()
    hour = now.hour
    order = ["breakfast", "lunch", "snacks", "dinner"]
    next_name = "breakfast" if hour < 10 else "lunch" if hour < 15 else "snacks" if hour < 18 else "dinner"
    meals = {r.meal: r.items for r in rows}
    later = [m for m in order[order.index(next_name):] if m in meals]
    return {
        "events": [("All day" if e.all_day else e.start.strftime("%H:%M"), e.title, e.important) for e in todays],
        "meal": (later[0], meals[later[0]]) if later else None,
        "important": [(e.when(today), e.title) for e in upcoming],
        "studio": hud_line(Path(projects_dir)) if projects_dir else "",
    }


# ---------- putting it together ----------

class _Bridge(QObject):
    event = Signal(dict)
    data = Signal(dict)
    stats = Signal(dict)


class JarvisDesktop:
    def __init__(self, runtime, start_hidden: bool = False):
        self.runtime = runtime
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        icon = QIcon(str(ROOT / "jarvis" / "web" / "icon-192.png"))
        self.app.setWindowIcon(icon)
        self.hud = HudWindow()
        self.cursor = CursorOrb()
        self.bridge = _Bridge()
        self.bridge.event.connect(self._on_event)
        self.bridge.data.connect(self._on_data)
        self.bridge.stats.connect(self._on_stats)
        BUS.subscribe(self.bridge.event.emit)
        BUS.ui_running = True

        self.hud.closed_to_orb.connect(self.cursor.start)
        self.hud.send_text.connect(lambda text: runtime.ask(text))
        self.hud.listen.connect(runtime.listen)

        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip("Jarvis")
        menu = QMenu()
        for text, slot in (("Show Jarvis   (Ctrl+Alt+J)", self.show_hud), ("Listen now", runtime.listen),
                           ("Quit Jarvis", self.quit)):
            act = QAction(text, menu)
            act.triggered.connect(slot)
            menu.addAction(act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.toggle() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        self.hotkey = Hotkey(self.toggle)
        self.app.installNativeEventFilter(self.hotkey)
        if not self.hotkey.ok:
            runtime.log("interface: Ctrl+Alt+J is taken by another app; use the tray icon instead")

        from jarvis.desktop.sysinfo import CpuMeter

        self._cpu = CpuMeter()
        QTimer(self.hud, interval=30_000, timeout=self._refresh_data).start()
        QTimer(self.hud, interval=3_000, timeout=self._refresh_stats).start()
        self._refresh_data()
        self._refresh_stats()
        if start_hidden:
            self.cursor.start()
        else:
            self.show_hud()

    # window switching
    def show_hud(self) -> None:
        self.cursor.stop()
        self.hud.showNormal()
        self.hud.raise_()
        self.hud.activateWindow()

    def toggle(self) -> None:
        if self.hud.isVisible():
            self.hud.close_to_orb()
        else:
            self.show_hud()

    def quit(self) -> None:
        self.runtime.stop()
        self.tray.hide()
        self.app.quit()

    # background reads
    def _refresh_data(self) -> None:
        def work():
            try:
                self.bridge.data.emit(snapshot(self.runtime.settings.db_path, self.runtime.settings.new_projects_dir))
            except Exception as e:
                self.bridge.event.emit({"type": "log", "text": f"interface: {e!r}"})

        threading.Thread(target=work, daemon=True).start()

    def _refresh_stats(self) -> None:
        cpu = self._cpu.read()

        def work():
            from jarvis.desktop.sysinfo import gpu, memory

            self.bridge.stats.emit({"cpu": cpu, "mem": memory(), "gpu": gpu()})

        threading.Thread(target=work, daemon=True).start()

    # slots
    def _on_data(self, d: dict) -> None:
        events = d["events"]
        self.hud.events.setText("\n".join(f"{t}   {'★ ' if imp else ''}{title}" for t, title, imp in events)
                                or "Nothing on today.")
        self.hud.meal.setText(f"{d['meal'][0].capitalize()}: {d['meal'][1]}" if d["meal"] else "No menu for today.")
        self.hud.important.setText("\n".join(f"★ {title}\n   {when}" for when, title in d["important"])
                                   or "Nothing marked important.")
        self.hud.studio.setText(d.get("studio", ""))

    def _on_stats(self, s: dict) -> None:
        if s["cpu"] is not None:
            self.hud.cpu.set(s["cpu"] / 100, f"{s['cpu']:.0f}%")
        if s["mem"]:
            used, total = s["mem"]
            self.hud.ram.set(used / total, f"{used:.1f} / {total:.0f} GB")
        if s["gpu"]:
            busy, used, total = s["gpu"]
            self.hud.gpu.set(busy / 100, f"{busy:.0f}%")
            self.hud.vram.set(used / total, f"{used:.1f} / {total:.0f} GB")

    def _on_event(self, e: dict) -> None:
        kind = e.get("type")
        if kind == "state":
            self.hud.set_state(e.get("state", "idle"))
            self.cursor.orb.set_state(e.get("state", "idle"))
        elif kind == "level":
            self.hud.orb.set_level(e.get("value", 0.0))
            self.cursor.orb.set_level(e.get("value", 0.0))
        elif kind == "heard":
            src = {"phone": "YOU · PHONE", "web": "YOU · WEB APP"}.get(e.get("source"), "YOU")
            self.hud.add_line(src, e.get("text", ""))
        elif kind == "reply":
            self.hud.add_line("JARVIS", e.get("text", ""))
            self._refresh_data()  # it may have changed the calendar
        elif kind == "status":
            st = e.get("status", {})
            dots = {"ready": "●", "listening": "●", "starting": "◌", "off": "○", "failed": "✕", "offline": "✕"}
            self.hud.status_line.setText("    ".join(f"{dots.get(v, '?')} {k.upper()}" for k, v in st.items()))
        elif kind == "ui":
            {"show": self.show_hud, "hide": self.hud.close_to_orb}.get(e.get("action"), lambda: None)()

    def run(self) -> int:
        return self.app.exec()
