"""Claude Usage HUD - a pixel-art game HUD for your Windows taskbar.

Shows Claude's 5-hour session limit and 7-day weekly limit as two chunky,
segmented game bars. Hover for details, drag to move, right-click for options.

No third-party packages: Python 3.10+ standard library only (tkinter).

Usage:
    pythonw claude_usage_hud.pyw            # run the widget
    python  claude_usage_hud.pyw --demo     # fake data, cycles through levels
    python  claude_usage_hud.pyw --snapshot out.png [--demo]   # render a PNG preview
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "ClaudeUsageHUD"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
POLL_SECONDS = 180
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

# --------------------------------------------------------------------------
# Palette (original pixel-art palette: slate stone panel, gem/sand accents)
# --------------------------------------------------------------------------
C = {
    "outline": "#0d0d12",
    "bevel_hi": "#7a7a8c",
    "bevel_lo": "#25252e",
    "panel": "#3d3d4c",
    "panel_dark": "#33333f",
    "slot": "#1a1a22",
    "slot_hi": "#24242e",
    "text": "#f4f1e8",
    "text_dim": "#a9a6b8",
    "shadow": "#15151b",
    "title_session": "#ffd166",
    "title_week": "#7cc8ff",
    # bar fills: (highlight, main, shade)
    "green": ("#a8f0c6", "#4cc38a", "#2b8a5c"),
    "blue": ("#bfe6ff", "#46a6f0", "#2468b0"),
    "amber": ("#ffe0a0", "#f0a030", "#b06a14"),
    "red": ("#ffb0a8", "#e5484d", "#9c2226"),
    "empty": ("#2a2a35", "#1f1f28", "#1a1a22"),
}

# 6x6 icons. Letters map to colors; '.' is transparent.
ICONS = {
    "hourglass": (
        ["WWWWWW",
         ".WSSW.",
         "..WS..",
         "..WW..",
         ".WSSW.",
         "WWWWWW"],
        {"W": "#c58b4e", "S": "#f2d27a"},
    ),
    "gem": (
        ["..GG..",
         ".GHGG.",
         "GHGGGG",
         "GGGGGD",
         ".GGDD.",
         "..DD.."],
        {"G": "#46a6f0", "H": "#e2f4ff", "D": "#1f62a8"},
    ),
    "skull": (  # shown when signed out
        [".WWWW.",
         "WWWWWW",
         "W.WW.W",
         "WWWWWW",
         ".W.W..",
         ".WWWW."],
        {"W": "#d8d4c8"},
    ),
}

# 3x5 pixel font (wide letters M/W/N use more columns).
FONT = {
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "011", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
    "%": ["101", "001", "010", "100", "101"], ":": ["0", "1", "0", "1", "0"],
    ".": ["0", "0", "0", "0", "1"], " ": ["00", "00", "00", "00", "00"],
    "-": ["000", "000", "111", "000", "000"], "/": ["001", "001", "010", "100", "100"],
    "!": ["1", "1", "1", "0", "1"], "?": ["111", "001", "011", "000", "010"],
    "A": ["010", "101", "111", "101", "101"], "B": ["110", "101", "110", "101", "110"],
    "C": ["011", "100", "100", "100", "011"], "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "110", "100", "111"], "F": ["111", "100", "110", "100", "100"],
    "G": ["011", "100", "101", "101", "011"], "H": ["101", "101", "111", "101", "101"],
    "I": ["111", "010", "010", "010", "111"], "J": ["001", "001", "001", "101", "010"],
    "K": ["101", "101", "110", "101", "101"], "L": ["100", "100", "100", "100", "111"],
    "M": ["10001", "11011", "10101", "10001", "10001"],
    "N": ["1001", "1101", "1011", "1001", "1001"], "O": ["010", "101", "101", "101", "010"],
    "P": ["110", "101", "110", "100", "100"], "Q": ["010", "101", "101", "110", "011"],
    "R": ["110", "101", "110", "101", "101"], "S": ["011", "100", "010", "001", "110"],
    "T": ["111", "010", "010", "010", "010"], "U": ["101", "101", "101", "101", "111"],
    "V": ["101", "101", "101", "101", "010"],
    "W": ["10001", "10001", "10101", "11011", "10001"],
    "X": ["101", "101", "010", "101", "101"], "Y": ["101", "101", "010", "010", "010"],
    "Z": ["111", "001", "010", "100", "111"],
}

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass
class Limit:
    pct: float | None = None
    resets_at: datetime | None = None


@dataclass
class Usage:
    session: Limit
    week: Limit
    updated: datetime | None = None
    status: str = "loading"  # loading | ok | auth | offline | error


def credentials_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")
    return Path(base) / ".credentials.json"


def read_token() -> str | None:
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (OSError, ValueError):
        return None


def parse_limit(obj) -> Limit:
    if not isinstance(obj, dict):
        return Limit()
    pct = obj.get("utilization")
    resets = obj.get("resets_at")
    when = None
    if isinstance(resets, str):
        try:
            when = datetime.fromisoformat(resets.replace("Z", "+00:00"))
        except ValueError:
            when = None
    return Limit(float(pct) if isinstance(pct, (int, float)) else None, when)


def fetch_usage(previous: Usage) -> Usage:
    """Call the (unofficial) usage endpoint that Claude Code itself uses."""
    token = read_token()
    if not token:
        return Usage(previous.session, previous.week, previous.updated, "auth")
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-usage-hud",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        status = "auth" if e.code in (401, 403) else "error"
        return Usage(previous.session, previous.week, previous.updated, status)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return Usage(previous.session, previous.week, previous.updated, "offline")
    return Usage(
        parse_limit(body.get("five_hour")),
        parse_limit(body.get("seven_day")),
        datetime.now(),
        "ok",
    )


def demo_usage(tick: int) -> Usage:
    levels = [(14, 26), (47, 33), (63, 58), (85, 71), (96, 91), (3, 92)]
    s, w = levels[tick % len(levels)]
    now = datetime.now(timezone.utc)
    return Usage(
        Limit(s, datetime.fromtimestamp(now.timestamp() + 2 * 3600 + 7 * 60, timezone.utc)),
        Limit(w, datetime.fromtimestamp(now.timestamp() + 3 * 86400 + 5 * 3600, timezone.utc)),
        datetime.now(),
        "ok",
    )


def fmt_countdown(when: datetime | None) -> str:
    if not when:
        return "--"
    secs = int((when - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "NOW"
    h, m = divmod(secs // 60, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}D {h}H"
    return f"{h}H {m:02d}M"


def fmt_reset_day(when: datetime | None) -> str:
    if not when:
        return "--"
    local = when.astimezone()
    return f"{WEEKDAYS[local.weekday()]} {local:%H:%M}"


# --------------------------------------------------------------------------
# Pixel canvas: everything is drawn into a grid of colors, then scaled up.
# --------------------------------------------------------------------------
class Pixels:
    def __init__(self, w: int, h: int, fill: str | None = None):
        self.w, self.h = w, h
        self.px = [[fill] * w for _ in range(h)]

    def set(self, x: int, y: int, color: str | None):
        if 0 <= x < self.w and 0 <= y < self.h and color:
            self.px[y][x] = color

    def rect(self, x: int, y: int, w: int, h: int, color: str):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.set(xx, yy, color)

    def panel(self, x: int, y: int, w: int, h: int):
        """Beveled stone panel: dark outline, light top-left, dark bottom-right."""
        self.rect(x, y, w, h, C["outline"])
        self.rect(x + 1, y + 1, w - 2, h - 2, C["bevel_lo"])
        self.rect(x + 1, y + 1, w - 3, h - 3, C["bevel_hi"])
        self.rect(x + 2, y + 2, w - 4, h - 4, C["panel"])
        # subtle stone speckle
        for i in range(x + 4, x + w - 4, 7):
            self.set(i, y + 3 + (i * 5) % max(1, h - 6), C["panel_dark"])
        # cut the outer corners for a chunkier game look
        for cx, cy in ((x, y), (x + w - 1, y), (x, y + h - 1), (x + w - 1, y + h - 1)):
            self.px[cy][cx] = None

    def icon(self, x: int, y: int, name: str):
        rows, colors = ICONS[name]
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch != ".":
                    self.set(x + dx, y + dy, colors[ch])

    def text(self, x: int, y: int, s: str, color: str, shadow: bool = True) -> int:
        """Draws text; returns the x after the last glyph."""
        for ch in s.upper():
            glyph = FONT.get(ch, FONT["?"])
            for dy, row in enumerate(glyph):
                for dx, bit in enumerate(row):
                    if bit == "1":
                        if shadow:
                            self.set(x + dx + 1, y + dy + 1, C["shadow"])
                        self.set(x + dx, y + dy, color)
            x += len(glyph[0]) + 1
        return x

    def bar(self, x: int, y: int, segments: int, pct: float | None, base: str, blink: bool):
        """Segmented bar. 3-px segments, 1-px gaps, framed slot. Height 6."""
        w = segments * 4 + 1
        self.rect(x, y, w, 6, C["outline"])
        self.rect(x + 1, y + 1, w - 2, 4, C["slot"])
        units = 0 if pct is None else round(max(0.0, min(100.0, pct)) / 100 * segments * 3)
        fill = C[color_for(pct, base)]
        if blink:
            fill = (fill[0], fill[0], fill[1])
        for i in range(segments):
            sx = x + 1 + i * 4
            for u in range(3):
                filled = i * 3 + u < units
                hi, mid, lo = fill if filled else C["empty"]
                self.set(sx + u, y + 1, hi)
                self.set(sx + u, y + 2, mid)
                self.set(sx + u, y + 3, mid)
                self.set(sx + u, y + 4, lo)
            self.set(sx + 3, y + 1, C["outline"])
            for yy in range(y + 2, y + 5):
                self.set(sx + 3, yy, C["slot_hi"])


def text_width(s: str) -> int:
    return sum(len(FONT.get(ch, FONT["?"])[0]) + 1 for ch in s.upper()) - 1


def color_for(pct: float | None, base: str) -> str:
    if pct is None:
        return "empty"
    if pct >= 80:
        return "red"
    if pct >= 50:
        return "amber"
    return base


def pct_label(pct: float | None, status: str) -> str:
    if pct is None:
        return "??" if status == "auth" else "--"
    return f"{round(pct)}%"


# --------------------------------------------------------------------------
# Scenes
# --------------------------------------------------------------------------
SEGMENTS = 10
HUD_W, HUD_H = 76, 20


def render_hud(u: Usage, blink_on: bool) -> Pixels:
    p = Pixels(HUD_W, HUD_H)
    p.panel(0, 0, HUD_W, HUD_H)
    rows = ((3, "hourglass", u.session, "green"), (11, "gem", u.week, "blue"))
    for y, icon, lim, base in rows:
        p.icon(4, y, "skull" if u.status == "auth" else icon)
        blink = blink_on and lim.pct is not None and lim.pct >= 90
        p.bar(12, y, SEGMENTS, lim.pct, base, blink)
        label = pct_label(lim.pct, u.status)
        tx = HUD_W - 4 - text_width(label)
        p.text(tx, y, label, C["text"] if u.status == "ok" else C["text_dim"])
    if u.status in ("offline", "error"):  # tiny red "no signal" pixel
        p.set(HUD_W - 3, 2, C["red"][1])
    return p


def render_popup(u: Usage) -> Pixels:
    lines: list[tuple[str, str, str | None]] = []  # (text, color, icon)
    if u.status == "auth":
        lines += [("SIGNED OUT", C["red"][0], "skull"), ("", "", None),
                  ("OPEN CLAUDE CODE", C["text"], None), ("TO LOG IN AGAIN", C["text_dim"], None)]
    else:
        s, w = u.session, u.week
        lines += [
            (f"SESSION  {pct_label(s.pct, u.status)}", C["title_session"], "hourglass"),
            (f"RESETS IN {fmt_countdown(s.resets_at)}", C["text_dim"], None),
            ("", "", None),
            (f"WEEKLY   {pct_label(w.pct, u.status)}", C["title_week"], "gem"),
            (f"RESETS {fmt_reset_day(w.resets_at)}", C["text_dim"], None),
        ]
    lines.append(("", "", None))
    status = {"ok": "UPDATED", "offline": "OFFLINE SINCE", "error": "ERROR SINCE",
              "loading": "LOADING", "auth": "LAST OK"}[u.status]
    if u.updated:
        lines.append((f"{status} {u.updated:%H:%M}", C["text_dim"], None))
    else:
        lines.append(("LOADING" if u.status == "loading" else "NO DATA YET", C["text_dim"], None))

    line_h = 8
    inner_w = max(text_width(t) + (9 if ic else 0) for t, _, ic in lines)
    inner_w = max(inner_w, SEGMENTS * 2 * 4 + 1)
    has_bars = u.status != "auth"
    bars_h = 10 if has_bars else 0
    w = inner_w + 10
    h = len(lines) * line_h + 8 + bars_h * 2
    p = Pixels(w, h)
    p.panel(0, 0, w, h)
    y = 5
    for text, color, ic in lines:
        x = 5
        if ic:
            p.icon(x, y - 1, ic)
            x += 9
        if text:
            p.text(x, y, text, color)
        y += line_h
        if has_bars and text.startswith(("SESSION", "WEEKLY")):
            lim = u.session if text.startswith("SESSION") else u.week
            p.bar(5, y - 1, SEGMENTS * 2, lim.pct, "green" if lim is u.session else "blue", False)
            y += bars_h
    return p


# --------------------------------------------------------------------------
# PNG export (for previews / README screenshots), stdlib only
# --------------------------------------------------------------------------
def save_png(pix: Pixels, path: str, scale: int = 4, bg: str = "#202024"):
    def rgb(c):
        c = c or bg
        return bytes(int(c[i:i + 2], 16) for i in (1, 3, 5))

    raw = bytearray()
    for row in pix.px:
        line = b"".join(rgb(c) * scale for c in row)
        for _ in range(scale):
            raw += b"\x00" + line
    w, h = pix.w * scale, pix.h * scale

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def stack(*scenes: Pixels, gap: int = 4) -> Pixels:
    w = max(s.w for s in scenes)
    out = Pixels(w, sum(s.h for s in scenes) + gap * (len(scenes) - 1))
    y = 0
    for s in scenes:
        for yy, row in enumerate(s.px):
            for xx, c in enumerate(row):
                out.set(xx, y + yy, c)
        y += s.h + gap
    return out


# --------------------------------------------------------------------------
# Windows helpers
# --------------------------------------------------------------------------
def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def taskbar_rect() -> tuple[int, int, int, int] | None:
    """Returns (left, top, right, bottom) of the taskbar in physical pixels."""
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

    class APPBARDATA(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p), ("uCallbackMessage", ctypes.c_uint),
                    ("uEdge", ctypes.c_uint), ("rc", RECT), ("lParam", ctypes.c_ssize_t)]

    try:
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(APPBARDATA)
        if ctypes.windll.shell32.SHAppBarMessage(5, ctypes.byref(abd)):  # ABM_GETTASKBARPOS
            r = abd.rc
            return r.l, r.t, r.r, r.b
    except Exception:
        pass
    return None


def tray_left() -> int | None:
    """Left edge of the notification area (the 'show hidden icons' arrow)."""
    try:
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        u.FindWindowExW.restype = ctypes.c_void_p
        u.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p]
        u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        tb = u.FindWindowW("Shell_TrayWnd", None)
        tray = u.FindWindowExW(tb, None, "TrayNotifyWnd", None) if tb else None
        if not tray:
            return None
        r = (ctypes.c_long * 4)()
        if u.GetWindowRect(tray, r):
            return r[0]
    except Exception:
        pass
    return None


TRAY_GAP = 8
RUN_KEY =r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            exe = Path(sys.executable)
            pythonw = exe.with_name("pythonw.exe")
            cmd = f'"{pythonw if pythonw.exists() else exe}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------
# The widget
# --------------------------------------------------------------------------
class HudApp:
    def __init__(self, demo: bool):
        import tkinter as tk

        self.tk = tk
        self.demo = demo
        self.usage = Usage(Limit(), Limit())
        self.results: queue.Queue[Usage] = queue.Queue()
        self.blink_on = False
        self.demo_tick = 0
        self.cfg = load_config()

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#ff00fe")
        self.root.attributes("-transparentcolor", "#ff00fe")  # cut-corner transparency

        tb = taskbar_rect()
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        tb_h = (tb[3] - tb[1]) if tb and tb[3] - tb[1] < screen_h // 3 else 48
        self.scale = max(1, (tb_h - 4) // HUD_H)
        self.w, self.h = HUD_W * self.scale, HUD_H * self.scale
        tb_top = tb[1] if tb else screen_h - tb_h
        self.anchor_y = tb_top + (tb_h - self.h) // 2
        # Default: docked just left of the tray arrow, following it as icons come and go.
        self.docked = "x" not in self.cfg
        x, y = self.cfg.get("x", 0), self.cfg.get("y", self.anchor_y)
        if self.docked or not (0 <= x < screen_w and 0 <= y < screen_h):
            self.docked = True
            x, y = self.docked_pos()
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")

        self.label = tk.Label(self.root, bd=0, bg="#ff00fe", cursor="hand2")
        self.label.pack()
        self.popup = None

        self.label.bind("<ButtonPress-1>", self.on_press)
        self.label.bind("<B1-Motion>", self.on_drag)
        self.label.bind("<ButtonRelease-1>", self.on_release)
        self.label.bind("<Enter>", lambda e: self.show_popup())
        self.label.bind("<Leave>", lambda e: self.hide_popup())
        self.label.bind("<Button-3>", self.on_menu)

        self.autostart = tk.BooleanVar(value=autostart_enabled())
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Refresh now", command=self.refresh)
        self.menu.add_checkbutton(label="Start with Windows", variable=self.autostart,
                                  command=lambda: set_autostart(self.autostart.get()))
        self.menu.add_command(label="Dock next to tray", command=self.reset_position)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.root.destroy)

        self.redraw()
        self.refresh()
        self.root.after(250, self.pump)
        self.root.after(500, self.blink)
        self.root.after(1500, self.keep_on_top)
        self.root.after(60_000, self.redraw_tick)

    # --- drawing -----------------------------------------------------------
    def to_photo(self, pix: Pixels):
        img = self.tk.PhotoImage(width=pix.w, height=pix.h)
        rows = " ".join("{" + " ".join(c or "#ff00fe" for c in row) + "}" for row in pix.px)
        img.put(rows, to=(0, 0))
        return img.zoom(self.scale, self.scale)

    def redraw(self):
        self.photo = self.to_photo(render_hud(self.usage, self.blink_on))
        self.label.configure(image=self.photo)
        if self.popup:
            self.show_popup()

    def redraw_tick(self):  # countdown text moves even without new data
        if self.popup:
            self.show_popup()
        self.root.after(60_000, self.redraw_tick)

    def blink(self):
        hot = any(l.pct is not None and l.pct >= 90 for l in (self.usage.session, self.usage.week))
        if hot or self.blink_on:
            self.blink_on = not self.blink_on if hot else False
            self.redraw()
        self.root.after(500, self.blink)

    def keep_on_top(self):
        # Clicking the taskbar raises it above us; quietly take the top spot back.
        self.root.attributes("-topmost", True)
        self.root.lift()
        if self.docked and not getattr(self, "moved", False):
            x, y = self.docked_pos()
            if (x, y) != (self.root.winfo_x(), self.root.winfo_y()):
                self.root.geometry(f"+{x}+{y}")
        self.root.after(1500, self.keep_on_top)

    def docked_pos(self) -> tuple[int, int]:
        left = tray_left()
        if left is None:
            left = self.root.winfo_screenwidth() - 280
        return left - self.w - TRAY_GAP, self.anchor_y

    # --- popup ---------------------------------------------------------------
    def show_popup(self):
        tk = self.tk
        pix = render_popup(self.usage)
        photo = self.to_photo(pix)
        if not self.popup:
            self.popup = tk.Toplevel(self.root)
            self.popup.overrideredirect(True)
            self.popup.attributes("-topmost", True)
            self.popup.configure(bg="#ff00fe")
            self.popup.attributes("-transparentcolor", "#ff00fe")
            self.popup_label = tk.Label(self.popup, bd=0, bg="#ff00fe")
            self.popup_label.pack()
        self.popup_photo = photo
        self.popup_label.configure(image=photo)
        pw, ph = pix.w * self.scale, pix.h * self.scale
        x = self.root.winfo_x() + self.w - pw
        x = max(0, min(x, self.root.winfo_screenwidth() - pw))
        y = self.root.winfo_y() - ph - 6
        if y < 0:  # taskbar at the top of the screen
            y = self.root.winfo_y() + self.h + 6
        self.popup.geometry(f"{pw}x{ph}+{x}+{y}")

    def hide_popup(self):
        if self.popup:
            self.popup.destroy()
            self.popup = None

    # --- data ----------------------------------------------------------------
    def refresh(self):
        if self.demo:
            self.results.put(demo_usage(self.demo_tick))
            self.demo_tick += 1
            return
        prev = self.usage
        threading.Thread(target=lambda: self.results.put(fetch_usage(prev)), daemon=True).start()

    def pump(self):
        try:
            while True:
                self.usage = self.results.get_nowait()
                self.redraw()
        except queue.Empty:
            pass
        if not hasattr(self, "_next_poll") or time.monotonic() >= self._next_poll:
            if hasattr(self, "_next_poll"):
                self.refresh()
            self._next_poll = time.monotonic() + (3 if self.demo else POLL_SECONDS)
        self.root.after(250, self.pump)

    # --- interaction -----------------------------------------------------------
    def on_press(self, e):
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())
        self.moved = False
        self.hide_popup()

    def on_drag(self, e):
        self.moved = True
        self.root.geometry(f"+{e.x_root - self.drag[0]}+{e.y_root - self.drag[1]}")

    def on_release(self, e):
        if self.moved:
            self.moved = False
            self.docked = False
            self.cfg.update(x=self.root.winfo_x(), y=self.root.winfo_y())
            save_config(self.cfg)
        else:
            self.refresh()  # a simple click refreshes

    def on_menu(self, e):
        self.hide_popup()
        self.menu.tk_popup(e.x_root, e.y_root)

    def reset_position(self):
        self.docked = True
        x, y = self.docked_pos()
        self.root.geometry(f"+{x}+{y}")
        self.cfg.pop("x", None)
        self.cfg.pop("y", None)
        save_config(self.cfg)

    def run(self):
        self.root.mainloop()


def main():
    args = sys.argv[1:]
    demo = "--demo" in args
    if "--snapshot" in args:
        out = args[args.index("--snapshot") + 1]
        if demo:
            scenes = []
            for t in range(6):
                u = demo_usage(t)
                scenes.append(render_hud(u, False))
            scenes.append(render_popup(demo_usage(0)))
            scenes.append(render_hud(Usage(Limit(), Limit(), None, "auth"), False))
            scenes.append(render_popup(Usage(Limit(), Limit(), None, "auth")))
        else:
            u = fetch_usage(Usage(Limit(), Limit()))
            scenes = [render_hud(u, False), render_popup(u)]
        save_png(stack(*scenes), out)
        print(f"wrote {out}")
        return
    # One widget at a time: a named mutex tells us if another copy is running.
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    enable_dpi_awareness()
    HudApp(demo).run()


if __name__ == "__main__":
    main()
