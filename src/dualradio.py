#!/usr/bin/env python3
"""
DualRadio - Wi-Fi + Bluetooth manager for the M5Stack CardputerZero (APPLaunch app).

Built-in radio and USB dongle side by side: connect / disconnect each or both,
saved networks per radio (passwords remembered and shared between radios),
band filter, full network details, main-network selection for other apps,
monitor mode with an all-band AP survey and pcap capture, hotspot from the
built-in radio, ping / speed test, and Bluetooth scan / details / pair /
connect / trust / remove / signal hunt on each Bluetooth adapter separately.

Draws to the ST7789V framebuffer (found via /proc/fb) and reads the TCA8418
keyboard via evdev.  Esc = back, hold Esc = exit.

  --preview DIR   render sample screens to PNG (no hardware)
  --keytest       print raw key codes
"""
import glob
import os
import queue
import select
import signal
import struct
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, APP_DIR)
import btcore  # noqa: E402
import moncore  # noqa: E402
import netcore as nc  # noqa: E402
import wifimgr as wm  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

try:
    import numpy as np
except ImportError:
    np = None

VERSION = "1.4.1"
W, H = 320, 170
LONG_PRESS_S = 0.7

# ================================================================ theme
FONT_DIRS = [os.path.join(APP_DIR, "fonts"), "/usr/share/fonts/truetype/dejavu"]
THEME_FILE = "/etc/dualradio/theme"

THEMES = {
    # clean dark slate, Inter, coloured state stripe on cards
    "slate": dict(BG=(13, 17, 23), PANEL=(22, 27, 36), PANEL_SEL=(30, 39, 54), ACCENT=(88, 166, 255),
                  TEXT=(230, 237, 243), MUTED=(139, 148, 158), DIM=(56, 63, 75), OK=(63, 185, 80),
                  WARN=(210, 153, 34), BAD=(248, 81, 73), HEAD=(13, 17, 23), PURPLE=(188, 140, 255),
                  SEL_BG=(31, 111, 235), SEL_FG=(255, 255, 255), LINE=(33, 38, 45), STYLE="stripe",
                  FONTS=("Inter-Regular.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf", "AnonymousPro-Regular.ttf"),
                  BUMP=0),
    # cyberpunk: near-black, cyan/magenta neon outlines, monospace terminal type
    "neon": dict(BG=(4, 5, 12), PANEL=(9, 11, 24), PANEL_SEL=(18, 8, 32), ACCENT=(0, 234, 255),
                 TEXT=(214, 252, 255), MUTED=(92, 128, 150), DIM=(32, 44, 64), OK=(57, 255, 120),
                 WARN=(255, 214, 0), BAD=(255, 46, 99), HEAD=(4, 5, 12), PURPLE=(255, 0, 170),
                 SEL_BG=(18, 8, 32), SEL_FG=(255, 0, 170), LINE=(255, 0, 170), STYLE="neon",
                 FONTS=("AnonymousPro-Regular.ttf", "AnonymousPro-Bold.ttf", "AnonymousPro-Bold.ttf",
                        "AnonymousPro-Regular.ttf"),
                 BUMP=1),
    # light, high-contrast (readable outdoors), Inter
    "daylight": dict(BG=(241, 243, 247), PANEL=(255, 255, 255), PANEL_SEL=(228, 238, 255), ACCENT=(37, 99, 235),
                     TEXT=(17, 24, 39), MUTED=(100, 108, 122), DIM=(203, 208, 216), OK=(22, 163, 74),
                     WARN=(202, 138, 4), BAD=(220, 38, 38), HEAD=(255, 255, 255), PURPLE=(124, 58, 237),
                     SEL_BG=(37, 99, 235), SEL_FG=(255, 255, 255), LINE=(222, 226, 233), STYLE="stripe",
                     FONTS=("Inter-Regular.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf", "AnonymousPro-Regular.ttf"),
                     BUMP=0),
}
TEXT_SIZES = {"normal": dict(title=14, bold=13, body=13, small=11, mono=13, big=36),
              "large": dict(title=16, bold=15, body=15, small=12, mono=15, big=40)}
SIZE_FILE = "/etc/dualradio/textsize"
TEXT_SIZE = "normal"
# layout metrics derived from the text size (set by apply_theme)
HDR, FTR, ROW, KVROW, CL2, CL3, CARD_H = 19, 28, 18, 16, 19, 36, 54
THEME_NAMES = list(THEMES)
THEME = "slate"
# theme colours / style (filled in by apply_theme)
BG = PANEL = PANEL_SEL = ACCENT = TEXT = MUTED = DIM = OK = WARN = BAD = HEAD = PURPLE = SEL_BG = SEL_FG = LINE = None
STYLE = "stripe"
F_TITLE = F_BOLD = F_BODY = F_SMALL = F_MONO = F_BIG = F_SYM = F_SYM_B = None


def _font(name, size):
    for d in FONT_DIRS:
        try:
            return ImageFont.truetype(os.path.join(d, name), size)
        except OSError:
            continue
    for fallback in ("DejaVuSans.ttf", "DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(os.path.join(FONT_DIRS[-1], fallback), size)
        except OSError:
            continue
    return ImageFont.load_default()


def apply_theme(name, size=None):
    global THEME, TEXT_SIZE, F_TITLE, F_BOLD, F_BODY, F_SMALL, F_MONO, F_BIG
    global HDR, FTR, ROW, KVROW, CL2, CL3, CARD_H
    t = THEMES.get(name, THEMES["slate"])
    THEME = name if name in THEMES else "slate"
    TEXT_SIZE = size if size in TEXT_SIZES else TEXT_SIZE
    globals().update({k: v for k, v in t.items() if k not in ("FONTS", "BUMP")})
    reg, semi, bold, mono = t["FONTS"]
    z = {k: v + (t["BUMP"] if k in ("body", "small", "bold") else 0) for k, v in TEXT_SIZES[TEXT_SIZE].items()}
    HDR, FTR = z["title"] + 5, 2 * z["small"] + 7
    ROW, KVROW = z["body"] + 5, z["body"] + 3
    CL2 = 3 + z["bold"] + 4
    CL3 = CL2 + z["body"] + 4
    CARD_H = CL3 + z["small"] + 6
    F_TITLE, F_BOLD, F_BODY = _font(bold, z["title"]), _font(semi, z["bold"]), _font(reg, z["body"])
    F_SMALL, F_MONO, F_BIG = _font(reg, z["small"]), _font(mono, z["mono"]), _font(bold, z["big"])
    global F_SYM, F_SYM_B
    F_SYM, F_SYM_B = _font("DejaVuSans.ttf", z["body"]), _font("DejaVuSans-Bold.ttf", z["bold"])


def load_theme():
    try:
        with open(THEME_FILE) as f:
            return f.read().strip()
    except OSError:
        return "slate"


def load_size():
    try:
        with open(SIZE_FILE) as f:
            return f.read().strip()
    except OSError:
        return "normal"


def save_size(name):
    try:
        os.makedirs(os.path.dirname(SIZE_FILE), exist_ok=True)
        with open(SIZE_FILE, "w") as f:
            f.write(name + "\n")
    except OSError:
        pass


def save_theme(name):
    try:
        os.makedirs(os.path.dirname(THEME_FILE), exist_ok=True)
        with open(THEME_FILE, "w") as f:
            f.write(name + "\n")
    except OSError:
        pass


apply_theme(load_theme(), load_size())

# ================================================================ keyboard
EV_FMT = "llHHi"
EV_SIZE = struct.calcsize(EV_FMT)
KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER, KEY_KPENTER = 1, 14, 15, 28, 96
KEY_LEFTSHIFT, KEY_RIGHTSHIFT, KEY_CAPSLOCK = 42, 54, 58
KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 103, 108, 105, 106
KEY_PAGEUP, KEY_PAGEDOWN, KEY_HOME, KEY_END = 104, 109, 102, 107
_BASE = {2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0", 12: "-", 13: "=",
         16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u", 23: "i", 24: "o", 25: "p", 26: "[",
         27: "]", 30: "a", 31: "s", 32: "d", 33: "f", 34: "g", 35: "h", 36: "j", 37: "k", 38: "l", 39: ";",
         40: "'", 41: "`", 43: "\\", 44: "z", 45: "x", 46: "c", 47: "v", 48: "b", 49: "n", 50: "m", 51: ",",
         52: ".", 53: "/", 57: " ", 55: "*", 71: "7", 72: "8", 73: "9", 74: "-", 75: "4", 76: "5", 77: "6",
         78: "+", 79: "1", 80: "2", 81: "3", 82: "0", 83: "."}
_SHIFT = {"1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
          "-": "_", "=": "+", "[": "{", "]": "}", ";": ":", "'": '"', "`": "~", "\\": "|", ",": "<", ".": ">",
          "/": "?"}
_KEYPAD = {55, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83}


class Key:
    __slots__ = ("code", "char")

    def __init__(self, code, char):
        self.code, self.char = code, char

    @property
    def c(self):
        return (self.char or "").lower()

    # Cardputer arrows without Fn: F up, X down, Z left, C right (j/k kept as aliases)
    def up(self):
        return self.code == KEY_UP or self.c in ("f", "k")

    def down(self):
        return self.code == KEY_DOWN or self.c in ("x", "j")

    def left(self):
        return self.code == KEY_LEFT or self.c == "z"

    def right(self):
        return self.code == KEY_RIGHT or self.c == "c"

    def enter(self):
        return self.code in (KEY_ENTER, KEY_KPENTER)

    def esc(self):
        return self.code == KEY_ESC


class Keyboard:
    def __init__(self):
        self.fds, seen = [], set()
        paths = [os.environ.get("DUALRADIO_INPUT", ""), "/dev/input/by-path/platform-3f804000.i2c-event"]
        paths += sorted(glob.glob("/dev/input/event*"))
        for p in paths:
            if not p:
                continue
            real = os.path.realpath(p)
            if real in seen or not os.path.exists(real):
                continue
            seen.add(real)
            try:
                self.fds.append(os.open(real, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                continue
            if p.endswith("i2c-event"):
                break
        self.shift = self.caps = False
        self.esc_down_at = None

    def poll(self, timeout):
        out = []
        if not self.fds:
            time.sleep(timeout)
            return out
        try:
            ready, _, _ = select.select(self.fds, [], [], timeout)
        except (OSError, ValueError):
            return out
        for fd in ready:
            try:
                data = os.read(fd, EV_SIZE * 64)
            except OSError:
                continue
            for off in range(0, len(data) - EV_SIZE + 1, EV_SIZE):
                _, _, et, code, val = struct.unpack(EV_FMT, data[off:off + EV_SIZE])
                if et != 1:
                    continue
                if code in (KEY_LEFTSHIFT, KEY_RIGHTSHIFT):
                    self.shift = val != 0
                elif code == KEY_CAPSLOCK:
                    if val == 1:
                        self.caps = not self.caps
                elif code == KEY_ESC:
                    if val == 1:
                        self.esc_down_at = time.time()
                    elif val == 0 and self.esc_down_at is not None:
                        held = time.time() - self.esc_down_at
                        self.esc_down_at = None
                        if held < LONG_PRESS_S:
                            out.append(Key(KEY_ESC, None))
                elif val in (1, 2):
                    out.append(Key(code, self._char(code)))
        return out

    def esc_long(self):
        return self.esc_down_at is not None and time.time() - self.esc_down_at >= LONG_PRESS_S

    def _char(self, code):
        c = _BASE.get(code)
        if c is None:
            return None
        if code in _KEYPAD:
            return c
        if c.isalpha():
            return c.upper() if self.shift != self.caps else c
        return _SHIFT.get(c, c) if self.shift else c

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass


# ================================================================ framebuffer
def find_fb():
    env = os.environ.get("DUALRADIO_FB") or os.environ.get("LV_LINUX_FBDEV_DEVICE")
    if env:
        return env
    try:
        with open("/proc/fb") as f:
            for line in f:
                parts = line.split(None, 1)
                if len(parts) == 2 and "st7789" in parts[1].lower():
                    return f"/dev/fb{parts[0]}"
    except OSError:
        pass
    for dev in sorted(glob.glob("/sys/class/graphics/fb*")):
        try:
            with open(os.path.join(dev, "virtual_size")) as f:
                if f.read().strip() in ("320,170", "170,320"):
                    return "/dev/" + os.path.basename(dev)
        except OSError:
            continue
    return "/dev/fb0"


class Framebuffer:
    def __init__(self, path):
        self.path = path
        sysd = f"/sys/class/graphics/{os.path.basename(path)}"

        def rd(n, d):
            try:
                with open(os.path.join(sysd, n)) as f:
                    return f.read().strip()
            except OSError:
                return d

        vw, vh = (int(x) for x in rd("virtual_size", f"{W},{H}").split(","))
        self.fw, self.fh = vw, vh
        self.bpp = int(rd("bits_per_pixel", "16"))
        self.rotate = (vw, vh) == (H, W)
        self.stride = int(rd("stride", str(vw * self.bpp // 8)))
        self.f = open(path, "r+b", buffering=0)

    def show(self, img):
        if self.rotate:
            img = img.transpose(Image.Transpose.ROTATE_90)
        if img.size != (self.fw, self.fh):
            c = Image.new("RGB", (self.fw, self.fh), BG)
            c.paste(img, (0, 0))
            img = c
        row = self.fw * self.bpp // 8
        if self.bpp == 16:
            if np is not None:
                a = np.asarray(img, dtype=np.uint16)
                raw = (((a[:, :, 0] & 0xF8) << 8) | ((a[:, :, 1] & 0xFC) << 3) | (a[:, :, 2] >> 3)).astype("<u2").tobytes()
            else:
                buf = bytearray(self.fw * self.fh * 2)
                for i, (r, g, b) in enumerate(img.getdata()):
                    v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                    buf[2 * i], buf[2 * i + 1] = v & 0xFF, v >> 8
                raw = bytes(buf)
        else:
            raw = img.convert("RGBA").tobytes("raw", "BGRA")
        if self.stride != row:
            pad = b"\x00" * (self.stride - row)
            raw = b"".join(raw[y * row:(y + 1) * row] + pad for y in range(self.fh))
        self.f.seek(0)
        self.f.write(raw)

    def close(self):
        try:
            self.show(Image.new("RGB", (W, H), (0, 0, 0)))
            self.f.close()
        except OSError:
            pass


# ================================================================ drawing helpers
def clean(s):
    return str(s).replace("\r", " ").replace("\n", " ").replace("\t", " ")


def tw(d, s, f):
    return d.textlength(clean(s), font=f)


def fit(d, s, f, maxw):
    s = clean(s)
    if tw(d, s, f) <= maxw:
        return s
    while s and tw(d, s + "…", f) > maxw:
        s = s[:-1]
    return s + "…"


def wrap_chars(d, s, f, maxw):
    lines, cur = [], ""
    for ch in str(s):
        if tw(d, cur + ch, f) > maxw and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines or [""]


def wrap_words(d, s, f, maxw):
    lines, cur = [], ""
    for w_ in str(s).split():
        t = (cur + " " + w_).strip()
        if tw(d, t, f) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def short_sec(s):
    if s in ("--", "OPEN", ""):
        return "open"
    return s.replace("WPA2/WPA3", "WPA2/3").replace("WPA/WPA2", "WPA1/2").replace("/802.1X", " Ent").replace("802.1X", "Ent")


def dbm_bars(dbm):
    if dbm is None:
        return 0
    return 4 if dbm >= -55 else 3 if dbm >= -65 else 2 if dbm >= -75 else 1 if dbm >= -85 else 0


def bars(d, x, y, n, color, h=10):
    for i in range(4):
        bh = 3 + i * (h - 3) // 3
        d.rectangle([x + i * 4, y + h - bh, x + i * 4 + 2, y + h], fill=color if i < n else DIM)


def icon_wifi(d, x, y, color, s=12):
    cx, cy = x + s // 2, y + s - 2
    for r in (s - 2, (s - 2) * 2 // 3, (s - 2) // 3):
        d.arc([cx - r, cy - r, cx + r, cy + r], start=225, end=315, fill=color, width=2)
    d.ellipse([cx - 1, cy - 1, cx + 1, cy + 1], fill=color)


def icon_bt(d, x, y, color, s=12):
    cx, t, b, o = x + s // 2, y, y + s, s // 4
    d.line([(cx, t), (cx, b)], fill=color, width=1)
    d.line([(cx - o, t + o), (cx + o, b - o), (cx, b)], fill=color, width=1)
    d.line([(cx - o, b - o), (cx + o, t + o), (cx, t)], fill=color, width=1)


_batt = {"t": 0, "v": None}


def battery():
    if time.time() - _batt["t"] > 30:
        _batt["t"], _batt["v"] = time.time(), None
        for d_ in glob.glob("/sys/class/power_supply/*"):
            try:
                with open(os.path.join(d_, "type")) as f:
                    if f.read().strip() != "Battery":
                        continue
                with open(os.path.join(d_, "capacity")) as f:
                    cap = int(f.read().strip())
                chg = ""
                try:
                    with open(os.path.join(d_, "status")) as f:
                        chg = f.read().strip()
                except OSError:
                    pass
                _batt["v"] = (cap, chg == "Charging")
                break
            except (OSError, ValueError):
                continue
    return _batt["v"]


def draw_battery(d, xr, y):
    b = battery()
    if not b:
        return xr
    cap, chg = b
    x0 = xr - 18
    col = BAD if cap <= 15 else WARN if cap <= 30 else (OK if chg else MUTED)
    d.rectangle([x0, y + 1, x0 + 15, y + 8], outline=col)
    d.rectangle([x0 + 16, y + 3, x0 + 17, y + 6], fill=col)
    d.rectangle([x0 + 2, y + 3, x0 + 2 + int(11 * cap / 100), y + 6], fill=col)
    txt = f"{cap}%"
    d.text((x0 - 3 - tw(d, txt, F_SMALL), y - 1), txt, font=F_SMALL, fill=MUTED)
    return x0 - 6 - tw(d, txt, F_SMALL)


def badge(d, xr, y, label, color, fg=None):
    w_ = tw(d, label, F_SMALL)
    x0 = xr - w_ - 8
    if STYLE == "neon":
        d.rectangle([x0, y, xr, y + 12], outline=color)
        d.text((x0 + 4, y), label, font=F_SMALL, fill=color)
    else:
        d.rounded_rectangle([x0, y, xr, y + 12], radius=6, fill=color)
        d.text((x0 + 4, y), label, font=F_SMALL, fill=fg or (255, 255, 255) if THEME == "daylight" else fg or (10, 14, 20))
    return x0 - 3


def outline_tag(d, xr, y, label, color):
    w_ = tw(d, label, F_SMALL)
    x0 = xr - w_ - 8
    d.rounded_rectangle([x0, y, xr, y + 12], radius=3 if STYLE == "neon" else 6, outline=color)
    d.text((x0 + 4, y), label, font=F_SMALL, fill=color)
    return x0 - 3


def ysm(y):
    """y for small text vertically centred on a body-text row."""
    return y + 1 + (F_BODY.size - F_SMALL.size) // 2


def rcols(d, y, cols, xr=None, gap=6):
    """Draw columns right-to-left: cols = [(text, font, colour), ...] (rightmost first). Returns left edge."""
    x = xr if xr is not None else W - 12
    for text, font, colour in cols:
        if not text:
            continue
        w_ = tw(d, text, font)
        d.text((x - w_, ysm(y) if font is F_SMALL else y + 1), text, font=font, fill=colour)
        x -= w_ + gap
    return x


def lead_marks(d, x, y, fg, sel, dot=False, mark=None, mark_col=None):
    """Optional connected dot + symbol before a row's name. Returns x after them."""
    h = F_BODY.size
    if dot:
        r = max(3, h // 4)
        cy = y + 1 + h // 2
        d.ellipse([x, cy - r, x + 2 * r, cy + r], fill=fg if sel else OK)
        x += 2 * r + 4
    if mark:
        d.text((x, y + 1), mark, font=F_SYM, fill=fg if sel else mark_col)
        x += tw(d, mark, F_SYM) + 3
    return x


def card(d, box, sel, state_color):
    x0, y0, x1, y1 = box
    if STYLE == "neon":
        if sel:
            d.rectangle([x0 - 1, y0 - 1, x1 + 1, y1 + 1], outline=(70, 0, 50))
        d.rectangle(box, fill=PANEL_SEL if sel else PANEL, outline=PURPLE if sel else DIM)
        L = 7
        for (cx, cy, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            d.line([(cx, cy + dy * L), (cx, cy), (cx + dx * L, cy)], fill=ACCENT if sel else state_color, width=2)
        d.rectangle([x0 + 3, y0 + 4, x0 + 4, y1 - 4], fill=state_color)
    else:
        d.rounded_rectangle(box, radius=8, fill=PANEL_SEL if sel else PANEL,
                            outline=ACCENT if sel else (LINE if THEME == "daylight" else None), width=1)
        d.rounded_rectangle([x0 + 1, y0 + 7, x0 + 4, y1 - 7], radius=2, fill=state_color)


def sel_row(d, box):
    """Draw the selected-row highlight; returns the text colour to use on it."""
    x0, y0, x1, y1 = box
    if STYLE == "neon":
        d.rectangle(box, fill=PANEL_SEL, outline=PURPLE)
        d.rectangle([x0, y0, x0 + 2, y1], fill=PURPLE)
        return ACCENT
    d.rounded_rectangle(box, radius=5, fill=SEL_BG)
    return SEL_FG


def header(d, title, right="", tabs=None, active=0, warn=""):
    if STYLE == "neon":
        d.line([0, HDR - 2, W, HDR - 2], fill=PURPLE)
        d.line([0, HDR - 1, W // 3, HDR - 1], fill=ACCENT)
    else:
        d.rectangle([0, 0, W, HDR - 2], fill=HEAD)
        d.line([0, HDR - 2, W, HDR - 2], fill=LINE)
    if tabs:
        x = 4
        for i, t in enumerate(tabs):
            w_ = tw(d, t, F_TITLE) + 14
            if i == active:
                if STYLE == "neon":
                    d.rectangle([x, 1, x + w_, 14], outline=ACCENT)
                    d.text((x + 7, 1), t, font=F_TITLE, fill=ACCENT)
                else:
                    d.rounded_rectangle([x, 1, x + w_, 14], radius=7, fill=SEL_BG)
                    d.text((x + 7, 1), t, font=F_TITLE, fill=SEL_FG)
            else:
                d.text((x + 7, 1), t, font=F_TITLE, fill=MUTED)
            x += w_ + 2
    xr = W - 5
    if right:
        right = fit(d, right, F_SMALL, W // 2 + 20)
        d.text((xr - tw(d, right, F_SMALL), 3), right, font=F_SMALL, fill=MUTED)
        xr -= tw(d, right, F_SMALL) + 8
    if not tabs:
        d.text((6, 1), fit(d, title, F_TITLE, xr - 10), font=F_TITLE, fill=ACCENT if STYLE == "neon" else TEXT)
    if tabs:
        draw_battery(d, xr, 3)
    if warn:
        d.rectangle([0, HDR - 1, W, HDR + F_SMALL.size + 2], fill=BAD)
        d.text((6, HDR - 1), fit(d, warn, F_SMALL, W - 12), font=F_SMALL, fill=(255, 255, 255))


def footer(d, hint, msg="", color=None):
    d.line([0, H - FTR, W, H - FTR], fill=LINE if STYLE != "neon" else DIM)
    lh = F_SMALL.size + 2
    if msg:
        d.text((5, H - FTR + 2), fit(d, msg, F_SMALL, W - 10), font=F_SMALL, fill=color or MUTED)
    d.text((5, H - FTR + 2 + lh), fit(d, hint, F_SMALL, W - 10), font=F_SMALL, fill=MUTED)


def modal(base, title, body, hint, color=None, big=None):
    color = color or ACCENT
    shade = (0, 0, 0) if THEME != "daylight" else (90, 96, 110)
    img = Image.blend(base, Image.new("RGB", (W, H), shade), 0.6)
    d = ImageDraw.Draw(img)
    lines = wrap_words(d, body, F_BODY, W - 50)[:5]
    bh = 30 + 14 * len(lines) + (38 if big else 0) + 16
    y0 = max(2, (H - bh) // 2)
    if STYLE == "neon":
        d.rectangle([16, y0, W - 17, y0 + bh], fill=PANEL, outline=color, width=1)
        d.line([16, y0, 60, y0], fill=PURPLE, width=3)
    else:
        d.rounded_rectangle([16, y0, W - 17, y0 + bh], radius=10, fill=PANEL, outline=color, width=2)
    d.text((26, y0 + 6), title, font=F_BOLD, fill=color)
    for n, line in enumerate(lines):
        d.text((26, y0 + 23 + 14 * n), line, font=F_BODY, fill=TEXT)
    if big:
        y = y0 + 23 + 14 * len(lines)
        d.text(((W - tw(d, big, F_BIG)) / 2, y - 4), big, font=F_BIG, fill=TEXT)
    d.text((26, y0 + bh - 15), hint, font=F_SMALL, fill=MUTED)
    return img


# ================================================================ screen framework
class Screen:
    live = None  # seconds between forced re-renders

    def __init__(self, app):
        self.app = app
        self.dirty = True
        self.last_render = 0

    def render(self):
        return Image.new("RGB", (W, H), BG)

    def key(self, k):
        pass

    def tick(self):
        pass

    def on_close(self):
        pass


class ListScreen(Screen):
    row_h = None
    hint = "F/X move  Enter select  Esc back"

    def __init__(self, app, title):
        super().__init__(app)
        self.title, self.idx, self.rows = title, 0, []

    def right_text(self):
        return f"{self.idx + 1}/{len(self.rows)}" if self.rows else ""

    def render_row(self, d, row, y, fg, sel):
        d.text((8, y + 1), fit(d, str(row), F_BODY, W - 20), font=F_BODY, fill=fg)

    def empty_text(self):
        return "Nothing here."

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        header(d, self.title, self.right_text())
        rh = self.row_h or ROW
        top_y, bottom = HDR + 1, H - FTR - 1
        vis = max(1, (bottom - top_y) // rh)
        if not self.rows:
            d.text((10, 50), self.empty_text(), font=F_BODY, fill=MUTED)
        top = max(0, min(self.idx - vis // 2, len(self.rows) - vis))
        for r in range(vis):
            j = top + r
            if j >= len(self.rows):
                break
            y = top_y + r * rh
            sel = j == self.idx
            fg = sel_row(d, [2, y, W - 8, y + rh - 1]) if sel else TEXT
            self.render_row(d, self.rows[j], y, fg, sel)
        if len(self.rows) > vis:
            tr = bottom - top_y
            th = max(8, tr * vis // len(self.rows))
            ty = top_y + (tr - th) * top // max(1, len(self.rows) - vis)
            d.rectangle([W - 5, top_y, W - 3, bottom], fill=PANEL)
            d.rectangle([W - 5, ty, W - 3, ty + th], fill=MUTED)
        footer(d, self.hint, *self.app.toast_now())
        return img

    def key(self, k):
        n = len(self.rows)
        if k.up():
            self.idx = max(0, self.idx - 1)
        elif k.down():
            self.idx = min(max(0, n - 1), self.idx + 1)
        elif k.code == KEY_PAGEUP:
            self.idx = max(0, self.idx - 8)
        elif k.code == KEY_PAGEDOWN:
            self.idx = min(max(0, n - 1), self.idx + 8)
        elif k.esc():
            self.app.pop()
            return
        elif k.enter() and self.rows:
            self.on_enter(self.rows[self.idx])
        else:
            self.on_char(k)
        self.dirty = True

    def on_enter(self, row):
        pass

    def on_char(self, k):
        pass


class MenuScreen(ListScreen):
    """items: (label, callable or None-if-disabled[, hint])"""
    hint = "F/X move  Enter choose  Esc back"

    def __init__(self, app, title, items):
        super().__init__(app, title)
        self.rows = items

    def right_text(self):
        return ""

    def render_row(self, d, row, y, fg, sel):
        label, fn = row[0], row[1]
        d.text((10, y + 1), fit(d, label, F_BODY, W - 90), font=F_BODY,
               fill=fg if fn else (DIM if not sel else (40, 50, 70)))
        if len(row) > 2 and row[2]:
            d.text((W - 14 - tw(d, row[2], F_SMALL), y + 3), row[2], font=F_SMALL, fill=fg if sel else MUTED)

    def on_enter(self, row):
        if row[1]:
            self.app.pop()
            row[1]()

    def on_char(self, k):
        for row in self.rows:
            if len(row) > 2 and row[2] and row[1] and k.c == row[2].lower():
                self.app.pop()
                row[1]()
                return


class KVScreen(Screen):
    """Scrollable key/value detail view."""
    hint = "F/X scroll  Esc back"
    live = None

    def __init__(self, app, title):
        super().__init__(app)
        self.title, self.off, self._lines = title, 0, []

    def rows(self):
        return []

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        lines = []
        for k_, v in self.rows():
            for i, part in enumerate(wrap_chars(d, v, F_BODY, W - 128)):
                lines.append((k_ if i == 0 else "", part))
        self._lines = lines
        header(d, self.title, f"{min(self.off + 1, len(lines))}-{min(self.off + 9, len(lines))}/{len(lines)}")
        vis = max(1, (H - FTR - HDR - 2) // KVROW)
        self.off = max(0, min(self.off, len(lines) - vis))
        for r in range(vis):
            j = self.off + r
            if j >= len(lines):
                break
            y = HDR + 1 + r * KVROW
            k_, v = lines[j]
            d.text((6, y + 1), fit(d, k_, F_SMALL, 112), font=F_SMALL, fill=MUTED)
            col = WARN if k_ == "Note" else ACCENT if k_ == "▶" else TEXT
            d.text((122, y), v, font=F_BODY, fill=col)
        footer(d, self.hint, *self.app.toast_now())
        return img

    def key(self, k):
        if k.up():
            self.off = max(0, self.off - 1)
        elif k.down():
            self.off += 1
        elif k.code == KEY_PAGEUP:
            self.off = max(0, self.off - 8)
        elif k.code == KEY_PAGEDOWN:
            self.off += 8
        elif k.esc():
            self.app.pop()
            return
        else:
            self.on_char(k)
        self.dirty = True

    def on_char(self, k):
        pass


class ConfirmScreen(Screen):
    def __init__(self, app, title, body, on_yes, on_no=None, color=None, big=None,
                 hint="Enter = yes    Esc = no"):
        super().__init__(app)
        color = color or WARN
        self.base = app.last_frame or Image.new("RGB", (W, H), BG)
        self.title, self.body, self.on_yes, self.on_no = title, body, on_yes, on_no
        self.color, self.big, self.hint = color, big, hint

    def render(self):
        return modal(self.base, self.title, self.body, self.hint, self.color, self.big)

    def key(self, k):
        if k.enter() or k.c == "y":
            self.app.pop()
            if self.on_yes:
                self.on_yes()
        elif k.esc() or k.c == "n":
            self.app.pop()
            if self.on_no:
                self.on_no()


class InfoScreen(ConfirmScreen):
    def __init__(self, app, title, body, big=None, color=None, on_close=None):
        super().__init__(app, title, body, on_close, on_close, color or ACCENT, big, "any key to close")

    def key(self, k):
        self.app.pop()
        if self.on_yes:
            self.on_yes()


class TextInputScreen(Screen):
    def __init__(self, app, title, label, on_done, initial="", secret=False, numeric=False, maxlen=63,
                 allow_empty=False):
        super().__init__(app)
        self.allow_empty = allow_empty
        self.title, self.label, self.on_done = title, label, on_done
        self.buf, self.secret, self.numeric, self.maxlen = initial, secret, numeric, maxlen
        self.show = not secret

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        header(d, self.title)
        d.text((10, 24), fit(d, self.label, F_BOLD, W - 20), font=F_BOLD, fill=TEXT)
        d.rounded_rectangle([8, 44, W - 9, 68], radius=5, fill=PANEL, outline=ACCENT, width=2)
        s = self.buf if self.show else "•" * len(self.buf)
        while s and tw(d, s, F_MONO) > W - 34:
            s = s[1:]
        d.text((16, 49), s, font=F_MONO, fill=TEXT)
        cx = 16 + tw(d, s, F_MONO) + 1
        d.rectangle([cx, 48, cx + 1, 64], fill=ACCENT)
        kb = self.app.kb
        st = f"{len(self.buf)} chars" + ("  SHIFT" if kb.shift else "") + ("  CAPS" if kb.caps else "")
        d.text((10, 74), st, font=F_SMALL, fill=MUTED)
        if not self.numeric:
            d.text((10, 88), "Symbols: Sym layer or Shift+digit", font=F_SMALL, fill=MUTED)
        footer(d, "Esc cancel  (Backspace on empty = cancel)",
               ("Tab show/hide   " if self.secret else "") + "Enter OK")
        return img

    def key(self, k):
        if k.enter():
            if self.buf or not self.secret or self.allow_empty:
                self.app.pop()
                self.on_done(self.buf)
        elif k.esc() or (k.code == KEY_BACKSPACE and not self.buf):
            self.app.pop()
            self.on_done(None)
        elif k.code == KEY_BACKSPACE:
            self.buf = self.buf[:-1]
        elif k.code == KEY_TAB and self.secret:
            self.show = not self.show
        elif k.char and len(self.buf) < self.maxlen and (not self.numeric or k.char.isdigit()):
            self.buf += k.char
        self.dirty = True


class BusyScreen(Screen):
    live = 0.15

    def __init__(self, app, text, fn, args, done):
        super().__init__(app)
        self.base = app.last_frame or Image.new("RGB", (W, H), BG)
        self.text, self.done = text, done
        self.result, self.error, self.tickn = None, None, 0
        self.thread = threading.Thread(target=self._work, args=(fn, args), daemon=True)
        self.thread.start()

    def _work(self, fn, args):
        try:
            self.result = fn(*args)
        except Exception as e:  # reported via done()
            self.error = e

    def render(self):
        self.tickn += 1
        return modal(self.base, "Working " + "|/-\\"[self.tickn % 4], self.text, "please wait…", WARN)

    def tick(self):
        if not self.thread.is_alive():
            self.app.pop(self)
            self.done(self.result, self.error)


# ================================================================ app
class App:
    def __init__(self, fb, kb):
        self.fb, self.kb = fb, kb
        self.stack = []
        self.last_frame = None
        self.toast_msg, self.toast_color, self.toast_at = "", None, 0
        self.lock = threading.Lock()
        self.ifs, self.adapters = [], []
        self.bt, self.bt_error = None, ""
        self.pw_requests = queue.Queue()
        self.stop = threading.Event()
        self.survey = None
        self.wifi_ok = True
        self.rooted = True
        threading.Thread(target=self._init_bt, daemon=True).start()
        threading.Thread(target=self._collector, daemon=True).start()

    # ---------- background
    def _init_bt(self):
        try:
            self.bt = btcore.Bt()
            if not getattr(self.bt, "agent_ok", True):
                self.bt_error = "pairing agent: " + getattr(self.bt, "agent_error", "?")
        except Exception as e:
            self.bt_error = str(e)

    def _collector(self):
        watcher = nc.Watcher()
        try:
            nc.apply_arp_fix()
        except Exception as e:
            wm.log(f"arp fix: {e!r}")
        while not self.stop.is_set():
            try:
                watcher.poll()
            except Exception as e:
                wm.log(f"watcher: {e!r}")
            self.refresh()
            self.stop.wait(3)

    def refresh(self):
        try:
            ifs = nc.collect()
        except Exception as e:
            wm.log(f"collect: {e!r}")
            ifs = self.ifs
        ads = self.adapters
        if self.bt:
            try:
                ads = self.bt.adapters()
            except Exception as e:
                self.bt_error = str(e)
        with self.lock:
            changed = ifs != self.ifs or ads != self.adapters
            self.ifs, self.adapters = ifs, ads
        if changed and self.stack:
            self.stack[0].dirty = True

    def iface(self, name):
        with self.lock:
            return next((dict(i) for i in self.ifs if i["name"] == name), None)

    # ---------- stack
    def push(self, s):
        self.stack.append(s)
        s.dirty = True

    def pop(self, s=None):
        if not self.stack:
            return
        if s is not None and s in self.stack:
            self.stack.remove(s)
            s.on_close()
        elif len(self.stack) > 1:
            self.stack.pop().on_close()
        if self.stack:
            self.stack[-1].dirty = True

    def pop_to_home(self):
        while len(self.stack) > 1:
            self.stack.pop().on_close()
        self.stack[0].dirty = True

    def toast(self, msg, color=None):
        self.toast_msg, self.toast_color, self.toast_at = msg, color or MUTED, time.time()
        if self.stack:
            self.stack[-1].dirty = True

    def toast_now(self):
        if time.time() - self.toast_at < 8:
            return self.toast_msg, self.toast_color or MUTED
        return "", None

    def job(self, text, fn, *args, done=None, ok_toast=True):
        def _done(res, err):
            if err is not None:
                msg = str(err) or err.__class__.__name__
                self.toast(msg, MUTED if msg == "cancelled" else BAD)
                if not isinstance(err, (wm.WifiError, btcore.BtError)):
                    wm.log(f"job error: {err!r}")
            elif ok_toast and isinstance(res, str):
                self.toast(res, OK)
            self.refresh_soon()
            if done:
                done(res, err)
        self.push(BusyScreen(self, text, fn, args, _done))

    def refresh_soon(self):
        threading.Thread(target=self.refresh, daemon=True).start()

    def ask_password(self, ssid):
        """Called from a worker thread; blocks until the UI answers."""
        ans = queue.Queue()
        self.pw_requests.put((ssid, ans))
        return ans.get()

    def confirm(self, title, body, on_yes, color=None):
        self.push(ConfirmScreen(self, title, body, on_yes, color=color))

    def ssh_guard(self, ifc, verb, then):
        i = self.iface(ifc)
        if i and i.get("ssh"):
            self.confirm("SSH warning", f"{ifc} carries your SSH session. {verb} may drop it. Continue?", then)
        else:
            then()

    # ---------- loop
    def _pump_requests(self):
        try:
            ssid, ans = self.pw_requests.get_nowait()
            self.push(TextInputScreen(self, "Wi-Fi password", ssid, lambda v: ans.put(v), secret=True))
        except queue.Empty:
            pass
        if self.bt:
            try:
                p = self.bt.prompts.get_nowait()
            except queue.Empty:
                p = None
            if p:
                handle_agent_prompt(self, p)

    def run(self, home):
        self.push(home)
        self.refresh()
        while not self.stop.is_set():
            top = self.stack[-1]
            try:
                self._pump_requests()
                top = self.stack[-1]
                top.tick()
                top = self.stack[-1]
                now = time.time()
                if top.dirty or (top.live and now - top.last_render >= top.live) or now - top.last_render > 30:
                    img = top.render()
                    self.fb.show(img)
                    self.last_frame = img
                    top.dirty, top.last_render = False, now
                for k in self.kb.poll(0.08):
                    top = self.stack[-1]
                    self.stack[-1].key(k)
            except Exception as e:  # never let one screen take the whole app down
                import traceback
                wm.log(f"UI error in {type(top).__name__}: {e!r}\n" + traceback.format_exc())
                if len(self.stack) > 1 and self.stack[-1] is top:
                    self.pop()
                self.toast(f"error in {type(top).__name__}: {e} (logged)", BAD)
                time.sleep(0.2)
            if self.kb.esc_long():
                return

    def shutdown(self):
        self.stop.set()
        if self.survey:
            self.survey.stop()
        while len(self.stack) > 1:
            self.stack.pop().on_close()
        if self.bt:
            self.bt.close()


# ================================================================ home
class HomeScreen(Screen):
    live = 1.0

    def __init__(self, app):
        super().__init__(app)
        self.tab, self.sel = 0, [0, 0]

    def items(self):
        with self.app.lock:
            return list(self.app.ifs if self.tab == 0 else self.app.adapters)

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        warn = "" if self.app.rooted else "NOT ROOT — actions will fail. See /var/log/dualradio.log"
        header(d, "", time.strftime("%H:%M"), tabs=["Wi-Fi", "Bluetooth"], active=self.tab, warn=warn)
        items = self.items()
        sel = min(self.sel[self.tab], max(0, len(items) - 1))
        if self.tab == 1 and not items:
            msg = self.app.bt_error or ("Starting Bluetooth…" if not self.app.bt else "No Bluetooth adapters")
            for n, line in enumerate(wrap_words(d, msg, F_BODY, W - 20)[:5]):
                d.text((10, 40 + 14 * n), line, font=F_BODY, fill=BAD if self.app.bt_error else MUTED)
        if self.tab == 0 and not items:
            d.text((10, 50), "No wireless interfaces found.", font=F_BODY, fill=BAD)
        top = HDR + (F_SMALL.size + 5 if warn else 2)
        ch = min(CARD_H + 3, (H - FTR - top) // max(1, min(len(items), 3)))
        y = top
        for n, it in enumerate(items[:3]):
            (self.wifi_card if self.tab == 0 else self.bt_card)(d, it, y, ch, n == sel, n)
            y += ch
        hint = ("Enter actions  S scan  D disc  P main  M survey  Z/C tab" if self.tab == 0
                else "Enter actions  S scan  L paired  O power  Z/C tab")
        footer(d, hint, *self.app.toast_now())
        return img

    def wifi_card(self, d, i, y, ch, sel, n):
        if i["mode"] == "monitor":
            col, lab = WARN, "MONITOR"
        elif i.get("hotspot"):
            col, lab = PURPLE, "HOTSPOT"
        elif i["nm_state"] == "connected":
            col, lab = OK, "CONNECTED"
        elif i["nm_state"].startswith("connecting"):
            col, lab = ACCENT, "CONNECTING"
        else:
            col, lab = MUTED, "IDLE"
        if not i["up"] and i["mode"] != "monitor":
            col, lab = BAD, "DOWN"
        card(d, [3, y, W - 4, y + ch - 3], sel, col)
        xr = badge(d, W - 10, y + 4, lab, col)
        if i.get("is_main"):
            xr = badge(d, xr, y + 4, "MAIN", ACCENT)
        icon_wifi(d, 10, y + 4, col)
        d.text((26, y + 3), i["name"], font=F_BOLD, fill=TEXT)
        nx = 30 + tw(d, i["name"], F_BOLD)
        d.text((nx, y + 4), fit(d, f"{i['role']} · {i.get('chip') or i['driver']}", F_SMALL, xr - nx - 4),
               font=F_SMALL, fill=MUTED)
        if i["mode"] == "monitor":
            l2, c2 = "Raw monitor mode — M for survey", WARN
        elif i.get("hotspot"):
            l2, c2 = f"Hotspot {i['ssid'] or ''}", PURPLE
        elif i["ssid"]:
            l2, c2 = i["ssid"], TEXT
        else:
            l2, c2 = "Not connected", MUTED
        right = 0
        if i["mode"] != "monitor" and i["signal"] is not None and not i.get("hotspot"):
            s = f"{i['signal']} dBm"
            bars(d, W - 26, y + CL2 + 2, dbm_bars(i["signal"]), OK)
            d.text((W - 31 - tw(d, s, F_SMALL), y + CL2 + 1), s, font=F_SMALL, fill=MUTED)
            right = 80
        if ch >= CARD_H - 6:
            d.text((12, y + CL2), fit(d, l2, F_BODY, W - 24 - right), font=F_BODY, fill=c2)
        parts = []
        if i["channel"]:
            parts.append(f"ch{i['channel']} {nc.band_of(i['freq'])}" + (f" {i['width']}MHz" if i["width"] else ""))
        if i["txrate"] and i["mode"] != "monitor":
            parts.append(i["txrate"] + "b/s")
        if i["ip"]:
            parts.append(i["ip"])
        x_end = W - 10
        if i["ssh"]:
            x_end = outline_tag(d, W - 10, y + CL3 - 1, "SSH", ACCENT)
        if ch >= CARD_H - 6:
            d.text((12, y + CL3), fit(d, "  ".join(parts) or "—", F_SMALL, x_end - 16), font=F_SMALL, fill=MUTED)

    def bt_card(self, d, a, y, ch, sel, n):
        card(d, [3, y, W - 4, y + ch - 3], sel, (OK if a["powered"] else MUTED) if not a.get("blocked") else BAD)
        xr = badge(d, W - 10, y + 4, "ON" if a["powered"] else "OFF", OK if a["powered"] else MUTED)
        if a.get("blocked"):
            xr = badge(d, xr, y + 4, "BLOCKED", BAD)
        if a["discovering"]:
            xr = badge(d, xr, y + 4, "SCANNING", WARN)
        icon_bt(d, 10, y + 3, ACCENT if a["powered"] else MUTED)
        d.text((26, y + 3), a["hci"], font=F_BOLD, fill=TEXT)
        nx = 30 + tw(d, a["hci"], F_BOLD)
        d.text((nx, y + 4), fit(d, f"{a['role']} · {a['bus'].upper()}", F_SMALL, xr - nx - 4), font=F_SMALL, fill=MUTED)
        d.text((12, y + CL2), fit(d, a["alias"] or a["name"], F_BODY, W - 24), font=F_BODY, fill=TEXT)
        d.text((12, y + CL3), f"{a['address']}   paired: {a['devices']}", font=F_SMALL, fill=MUTED)

    def key(self, k):
        items = self.items()
        n = len(items)
        if k.left() or k.right() or k.code == KEY_TAB:
            self.tab = 1 - self.tab
        elif k.up():
            self.sel[self.tab] = max(0, self.sel[self.tab] - 1)
        elif k.down():
            self.sel[self.tab] = min(max(0, n - 1), self.sel[self.tab] + 1)
        elif k.c in ("1", "2", "3") and int(k.c) <= n:
            self.sel[self.tab] = int(k.c) - 1
        elif k.c in ("h", "?"):
            self.app.push(HelpScreen(self.app, self.tab))
        elif k.c == "y":
            settings_menu(self.app)
        elif n:
            it = items[min(self.sel[self.tab], n - 1)]
            if self.tab == 0:
                wifi_shortcut(self.app, it, k)
            else:
                bt_shortcut(self.app, it, k)
        self.dirty = True


HELP = [
    ["Wi-Fi tab", ("F X Z C", "up / down / left / right"), ("Enter", "actions for radio"), ("S / N", "scan / saved networks"),
     ("D / A", "disconnect / both radios"), ("P", "make main for other apps"), ("M", "survey (2.4 + 5 GHz)"),
     ("O / T", "hotspot / speed test"), ("Z C Tab", "switch tab"), ("Y", "settings: theme, text, dup guard")],
    ["Bluetooth tab", ("F X Z C", "up / down / left / right"), ("Enter", "actions for adapter"),
     ("S / L", "scan / paired list"), ("O", "power on/off"), ("In device:", "P pair  O connect  T trust"),
     ("", "E remove  H hunt"), ("Esc", "back  (hold = exit app)")],
]


def settings_menu(app, idx=0):
    def cycle_theme():
        name = THEME_NAMES[(THEME_NAMES.index(THEME) + 1) % len(THEME_NAMES)]
        apply_theme(name)
        save_theme(name)
        settings_menu(app, 0)

    def toggle_size():
        size = "large" if TEXT_SIZE == "normal" else "normal"
        apply_theme(THEME, size)
        save_size(size)
        settings_menu(app, 1)

    def toggle_nodup():
        nc.set_nodup(not nc.nodup_enabled())
        if nc.nodup_enabled():
            threading.Thread(target=nc.enforce_nodup, daemon=True).start()
        settings_menu(app, 2)

    m = MenuScreen(app, "Settings", [(f"Theme: {THEME.capitalize()}", cycle_theme, "Y"),
                                     (f"Text size: {TEXT_SIZE.capitalize()}", toggle_size, "T"),
                                     ("Duplicate guard: " + ("On" if nc.nodup_enabled() else "Off"),
                                      toggle_nodup, "G"),
                                     ("Re-test dongle survey modes", lambda: (moncore.clear_caps(), app.toast(
                                         "dongles will be re-tested on the next survey", OK)), "R")])
    m.idx = idx
    m.hint = "Enter change  Esc back"
    app.push(m)


class HelpScreen(Screen):
    def __init__(self, app, tab):
        super().__init__(app)
        self.tab = tab

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        page = HELP[self.tab]
        header(d, "Help · " + page[0], "v" + VERSION)
        for n, (a, b) in enumerate(page[1:]):
            d.text((10, 20 + 14 * n), a, font=F_SMALL, fill=ACCENT)
            d.text((90, 20 + 14 * n), b, font=F_SMALL, fill=TEXT)
        footer(d, "any key to return", "Esc = back   hold Esc = exit")
        return img

    def key(self, k):
        self.app.pop()


# ================================================================ Wi-Fi actions
def wifi_shortcut(app, i, k):
    acts = {"enter": wifi_menu, "s": wifi_scan, "n": wifi_saved, "d": wifi_disconnect,
            "a": wifi_disconnect_all, "p": wifi_set_main, "m": wifi_monitor_toggle,
            "o": wifi_hotspot, "t": wifi_test}
    key = "enter" if k.enter() else k.c
    if key in acts:
        acts[key](app, i)


def wifi_menu(app, i):
    ifc, mon = i["name"], i["mode"] == "monitor"
    con = i["nm_state"] == "connected"
    items = [
        ("Scan networks", None if mon else lambda: wifi_scan(app, i), "S"),
        ("Saved networks", None if mon else lambda: wifi_saved(app, i), "N"),
        ("Join hidden network", None if mon else lambda: wifi_join_hidden(app, i), "H"),
        ("Disconnect", (lambda: wifi_disconnect(app, i)) if con else None, "D"),
        ("Disconnect both radios", lambda: wifi_disconnect_all(app, i), "A"),
        ("Set as main network", (lambda: wifi_set_main(app, i)) if con and not i.get("is_main") else None, "P"),
    ]
    if i["mon_ok"]:
        items.append(("Survey: APs, clients, capture", lambda: wifi_monitor_toggle(app, i, survey=True), "M"))
        items.append(("Raw monitor mode " + ("OFF" if mon else "ON (other tools)"),
                      lambda: wifi_monitor_toggle(app, i, survey=False), ""))
    if i.get("ap_ok") and not mon:
        items.append(("Hotspot", lambda: wifi_hotspot(app, i), "O"))
    items.append(("Ping + speed test", (lambda: wifi_test(app, i)) if con and not i.get("hotspot") else None, "T"))
    app.push(MenuScreen(app, f"{ifc} · {i.get('chip') or i['role']} · {i['driver']}", items))


def wifi_scan(app, i):
    if i["mode"] == "monitor":
        app.toast("monitor mode: use the AP survey (M)", WARN)
        return
    ifc = i["name"]

    def work():
        return nc.scan_full(ifc), nc.saved_ssids_for(ifc)

    app.job(f"Scanning on {ifc}…", work,
            done=lambda r, e: r and app.push(NetListScreen(app, ifc, r[0], r[1])), ok_toast=False)


class NetListScreen(ListScreen):
    BANDS = ["ALL", "2G", "5G"]
    hint = "Enter details  B band  R rescan  Esc back"

    def __init__(self, app, ifc, bsses, saved):
        super().__init__(app, "")
        self.ifc, self.bsses, self.saved = ifc, bsses, saved
        self.band = "ALL"
        self.regroup()

    def regroup(self):
        self.rows = nc.group_networks(self.bsses, self.saved, self.band)
        self.idx = min(self.idx, max(0, len(self.rows) - 1))
        self.title = f"{self.ifc} networks · {self.band if self.band != 'ALL' else 'all bands'}"

    def empty_text(self):
        return f"No networks on {self.band}. Press B to change band."

    def render_row(self, d, n, y, fg, sel):
        bars(d, 6, y + (ROW - 12) // 2, dbm_bars(n["signal"]), fg if sel else OK)
        left = rcols(d, y, [(short_sec(n["security"]), F_SMALL, fg if sel else MUTED),
                            (f"ch{n['chan']}", F_SMALL, fg if sel else MUTED),
                            (n["band"], F_SMALL, fg if sel else ACCENT)])
        x = lead_marks(d, 26, y, fg, sel, dot=n["inuse"], mark="★" if n["saved"] else None, mark_col=WARN)
        d.text((x, y + 1), fit(d, n["ssid"], F_BODY, left - x - 4), font=F_BODY, fill=fg)

    def on_enter(self, n):
        self.app.push(NetDetailScreen(self.app, self.ifc, n))

    def on_char(self, k):
        if k.c == "b":
            self.band = self.BANDS[(self.BANDS.index(self.band) + 1) % len(self.BANDS)]
            self.regroup()
        elif k.c == "r":
            def done(r, e):
                if r:
                    self.bsses, self.saved = r
                    self.regroup()
                    self.dirty = True
            self.app.job(f"Rescanning {self.ifc}…", lambda: (nc.scan_full(self.ifc), nc.saved_ssids_for(self.ifc)),
                         done=done, ok_toast=False)


class NetDetailScreen(KVScreen):
    def __init__(self, app, ifc, net):
        super().__init__(app, f"{net['ssid']} · {net['band']}")
        self.ifc, self.net = ifc, net
        self.hint = ("Enter options  E forget  F/X scroll  Esc back" if net["inuse"]
                     else "Enter connect  " + ("E forget  " if net["saved"] else "") + "F/X scroll  Esc back")

    def rows(self):
        with self.app.lock:
            others = [i for i in self.app.ifs if i["name"] != self.ifc and i["mode"] != "monitor"]
        act = ("Enter: reconnect / disconnect / forget" if self.net["inuse"]
               else f"Enter: connect with {self.ifc}")
        return [("▶", act)] + nc.network_detail_lines(self.net, others)

    def key(self, k):
        net, ifc, app = self.net, self.ifc, self.app

        def go():
            app.job(f"Connecting {ifc} to {net['ssid']}…", nc.connect, ifc, net,
                    lambda: app.ask_password(net["ssid"]),
                    done=lambda r, e: r and app.pop_to_home())
        if k.enter() and not net["inuse"] and nc.nodup_enabled():
            me = app.iface(ifc) or {}
            with app.lock:
                dup = next((x for x in app.ifs if x["name"] != ifc and x["nm_state"] == "connected"
                            and x["ssid"] == net["ssid"] and x.get("freq")
                            and nc.band_of(x["freq"]) == net["band"]), None)
            if dup and me.get("bus") == "usb":
                ob = nc.other_band(net["band"])
                if net.get("dual") and ob:
                    alt = dict(net, band=ob)
                    app.confirm("Same network", f"{dup['name']} is already on {net['ssid']} {net['band']}. "
                                f"Connect {ifc} on {ob} instead?",
                                lambda: app.ssh_guard(ifc, "Connecting", lambda: app.job(
                                    f"Connecting {ifc} to {net['ssid']} {ob}…", nc.connect, ifc, alt,
                                    lambda: app.ask_password(net["ssid"]),
                                    done=lambda r, e: r and app.pop_to_home())), color=ACCENT)
                else:
                    app.toast(f"blocked: {dup['name']} already on {net['ssid']} {net['band']} (Y > Settings)", BAD)
                return
            if dup:
                app.confirm("Same network", f"{dup['name']} (dongle) is on {net['ssid']} {net['band']}. "
                            "It will move to the other band or disconnect. Continue?",
                            lambda: app.ssh_guard(ifc, "Connecting", go))
                return
        if k.enter():
            if net["inuse"]:
                app.push(MenuScreen(app, f"{net['ssid']} on {ifc}", [
                    ("Reconnect", lambda: app.ssh_guard(ifc, "Reconnecting", go), "R"),
                    ("Disconnect", lambda: app.ssh_guard(ifc, "Disconnecting", lambda: app.job(
                        f"Disconnecting {ifc}…", wm.disconnect, ifc, done=lambda r, e: r and app.pop_to_home())), "D"),
                    ("Forget on this radio", lambda: forget_flow(app, ifc, net["ssid"]), "E")]))
            else:
                app.ssh_guard(ifc, "Connecting", go)
        elif k.c == "e":
            forget_flow(app, ifc, net["ssid"])
        else:
            super().key(k)


def forget_flow(app, ifc, ssid):
    bound, shared = nc.forget_targets(ifc, ssid)
    if bound:
        app.confirm("Forget network", f"Forget '{ssid}' on {ifc} only? The other radio keeps its copy.",
                    lambda: app.job("Forgetting…", nc.forget, bound))
    elif shared:
        app.confirm("Shared profile", f"'{ssid}' is saved as one profile used by both radios. Forget it on both?",
                    lambda: app.job("Forgetting…", nc.forget, shared))
    else:
        app.toast(f"'{ssid}' isn't saved on {ifc}")


def wifi_join_hidden(app, i):
    ifc = i["name"]

    def got_ssid(ssid):
        if not ssid:
            return

        def got_pw(pw):
            if pw is None:
                return
            net = {"ssid": ssid, "band": "?", "security": "WPA2" if pw else "--", "dual": False, "hidden": True}
            app.ssh_guard(ifc, "Connecting", lambda: app.job(
                f"Joining hidden {ssid} on {ifc}…", nc.connect, ifc, net, lambda: pw,
                done=lambda r, e: r and app.pop_to_home()))
        app.push(TextInputScreen(app, "Hidden network", f"Password for {ssid} (empty = open)", got_pw,
                                 secret=True, allow_empty=True))
    app.push(TextInputScreen(app, "Hidden network", "Network name (SSID)", got_ssid, maxlen=32))


def wifi_saved(app, i):
    ifc = i["name"]
    app.job("Loading saved networks…", nc.profiles_for, ifc,
            done=lambda r, e: r is not None and app.push(SavedScreen(app, ifc, r)), ok_toast=False)


class SavedScreen(ListScreen):
    hint = "Enter connect  E forget  Esc back"

    def __init__(self, app, ifc, profs):
        super().__init__(app, f"Saved on {ifc}")
        self.ifc, self.rows = ifc, profs

    def empty_text(self):
        return "No saved networks for this radio."

    def render_row(self, d, p, y, fg, sel):
        d.text((10, y + 1), "★", font=F_SYM, fill=fg if sel else WARN)
        d.text((24, y + 1), fit(d, p["ssid"] or p["name"], F_BODY, 180), font=F_BODY, fill=fg)
        tag = "this radio" if p["ifname"] == self.ifc else "shared"
        if p["band"]:
            tag += " · " + {"a": "5G", "bg": "2G"}.get(p["band"], p["band"])
        d.text((W - 14 - tw(d, tag, F_SMALL), y + 3), tag, font=F_SMALL, fill=fg if sel else MUTED)

    def on_enter(self, p):
        app, ifc = self.app, self.ifc
        app.ssh_guard(ifc, "Connecting", lambda: app.job(
            f"Connecting {ifc} to {p['ssid']}…", nc.connect_profile, ifc, p,
            done=lambda r, e: r and app.pop_to_home()))

    def on_char(self, k):
        if k.c == "e" and self.rows:
            p = self.rows[self.idx]

            def gone(r, e):
                if r:
                    self.rows = [x for x in self.rows if x["uuid"] != p["uuid"]]
                    self.idx = max(0, min(self.idx, len(self.rows) - 1))
            msg = (f"Forget '{p['ssid']}' on {self.ifc}?" if p["ifname"] == self.ifc else
                   f"'{p['ssid']}' is shared by both radios. Forget on both?")
            self.app.confirm("Forget network", msg, lambda: self.app.job("Forgetting…", nc.forget, [p], done=gone))


def wifi_disconnect(app, i):
    if i["nm_state"] != "connected":
        app.toast(f"{i['name']} isn't connected")
        return
    app.ssh_guard(i["name"], "Disconnecting",
                  lambda: app.job(f"Disconnecting {i['name']}…", wm.disconnect, i["name"]))


def wifi_disconnect_all(app, i):
    with app.lock:
        ssh = any(x.get("ssh") for x in app.ifs)
    body = "Disconnect BOTH Wi-Fi radios?" + (" Your SSH session WILL drop." if ssh else "")
    app.confirm("Disconnect both", body, lambda: app.job("Disconnecting both…", nc.disconnect_all))


def wifi_set_main(app, i):
    if i["nm_state"] != "connected":
        app.toast("connect this radio first")
        return
    app.job(f"Making {i['name']} main…", nc.set_main, i["name"])


def wifi_monitor_toggle(app, i, survey=True):
    """M: open the survey. It picks scan-sweep (2.4 / all bands, dongle stays in normal mode)
    or true monitor mode (5 GHz) by itself."""
    ifc = i["name"]
    if not i["mon_ok"]:
        app.toast(f"{ifc} ({i['driver']}) can't do monitor mode", BAD)
        return
    if not survey:
        if i["mode"] == "monitor":
            wifi_monitor_off(app, i)
        else:
            app.ssh_guard(ifc, "Monitor mode", lambda: app.job(f"{ifc}: monitor mode on…", wm.mon_on, ifc))
        return
    app.push(MonitorScreen(app, i, band="5G" if i["mode"] == "monitor" else "ALL"))


def wifi_monitor_off(app, i):
    app.job(f"{i['name']}: back to managed…", wm.mon_off, i["name"])


def _iface_mode(ifc):
    return wm.parse_iw_info(wm.run(["iw", "dev", ifc, "info"])[1])["mode"]


# ---------------------------------------------------------------- monitor survey
# Drivers proven (on device) to need a USB re-init before each 2.4 GHz monitor session.
RESET24_DRIVERS = {"rtw88_8821cu", "rtw_8821cu"}


class MonitorScreen(ListScreen):
    live = 0.7
    hint = "B band  L lock ch  R record  M exit  Enter info"

    def __init__(self, app, i, band="ALL"):
        super().__init__(app, "")
        self.i = i
        self.survey = None
        self.pending = band
        self.band = band
        self.made_monitor = False   # restore normal mode on exit if the survey switched it
        self.chans = []
        self.caps = moncore.caps_for(nc.caps_key(i))
        if i.get("bus") == "usb" and i.get("driver") in RESET24_DRIVERS and (self.caps or {}).get("reset24") is not False:
            # known: deaf on 2.4 GHz in monitor mode until the dongle is re-initialised -> reset first, no probe
            self.caps = dict(self.caps or {}, mon24=True, sweep=True, reset24=True)
        c = self.caps or {}
        if c.get("fallback") == "live" and "reset24" not in c:
            # saved by 1.3.4 before the USB-reset remedy existed: give true monitor one more go
            self.caps = dict(c, mon24=True)
        self.fell_back = False
        self.reset_tried = False
        self.reset_next = False     # replug-style USB reset before the next monitor start
        self.reset_used = False     # the running survey started right after a USB reset
        self.started = time.time()
        self.prior = ""
        try:
            self.chans = wm.phy_channels(i["phy"])
        except Exception:
            self.chans = []
        present = {nc.band_of(f) for _, f in self.chans}
        self.bands = ["ALL"] + [b for b in ("2G", "5G", "6G") if b in present]

    def mode_for(self, band):
        """Pick the survey method from what this driver proved it can do (probe results)."""
        if band in ("5G", "6G"):
            return "monitor"
        c = self.caps or {}
        if c.get("mon24"):
            return "monitor"          # good chipsets: true monitor on every band
        if c.get("sweep"):
            return "sweep"            # e.g. rtw88_8821cu: deaf on 2.4 GHz in monitor mode
        return None                   # can't survey 2.4 GHz at all

    def _launch(self, band):
        app, i, ifc = self.app, self.i, self.i["name"]
        needs_probe = self.caps is None and band in ("ALL", "2G") and "2G" in self.bands

        def prep():
            nonlocal i, ifc
            if needs_probe and i.get("bus") == "usb":
                # test the chip from a freshly initialised state (same as a replug)
                try:
                    ifc = wm.usb_reset(ifc)
                    wm.wait_nm_ready(ifc, 10)
                    fresh = next((x for x in nc.collect() if x["name"] == ifc), None)
                    if fresh:
                        self.i = i = fresh
                        self.chans = wm.phy_channels(i["phy"]) or self.chans
                except wm.WifiError as e:
                    wm.log(f"probe: reset skipped: {e}")
            if needs_probe:
                self.caps = moncore.probe(ifc, i["phy"], self.chans)
                moncore.save_caps(nc.caps_key(i), self.caps)
            b = band
            want = self.mode_for(b)
            if want is None:
                b, want = ("5G", "monitor") if "5G" in self.bands else (b, None)
                if want is None:
                    raise wm.WifiError(f"{i['driver']} can't survey on this hardware")
            covers24 = b in ("ALL", "2G") and "2G" in self.bands
            self.reset_used = False
            if (want == "monitor" and covers24 and i.get("bus") == "usb"
                    and (self.reset_next or (self.caps or {}).get("reset24"))):
                self.reset_next = False
                wm.run(["iw", "dev", ifc, "scan", "abort"], timeout=5)
                try:
                    new_ifc = wm.usb_reset(ifc)
                    wm.wait_nm_ready(new_ifc, 10)
                    fresh = next((x for x in nc.collect() if x["name"] == new_ifc), None)
                    if fresh:
                        self.i = i = fresh
                        self.chans = wm.phy_channels(i["phy"]) or self.chans
                    ifc = new_ifc
                    self.reset_used = True
                except wm.WifiError as e:
                    wm.log(f"survey {ifc}: reset skipped: {e}")
            now = _iface_mode(ifc)
            lk = wm.parse_iw_link(wm.run(["iw", "dev", ifc, "link"])[1]) if now != "monitor" else {}
            reg = wm.run(["iw", "reg", "get"])[1].split("country ", 1)[-1][:2] or "?"
            self.prior = (f"was {now or '?'}" + (f" on {lk.get('freq')} MHz" if lk.get("freq") else " unlinked")
                          + f", reg {reg}")
            wm.log(f"survey {ifc}: start {b} mode={want} caps={self.caps} {self.prior}")
            if want == "monitor" and now != "monitor":
                wm.run(["iw", "dev", ifc, "scan", "abort"], timeout=5)   # a sweep/NM scan must not be in flight
                time.sleep(0.5)
                wm.mon_on(ifc)
                self.made_monitor = True
            elif want == "sweep" and now == "monitor":
                wm.mon_off(ifc)
                wm.wait_nm_ready(ifc, 8)
            if want == "monitor":
                first = next((str(c) for c, f in self.chans if b == "ALL" or nc.band_of(f) == b), "36")
                for attempt in range(6):   # make sure the radio accepts channel changes before hopping
                    if wm.run(["iw", "dev", ifc, "set", "channel", first], timeout=3)[0] == 0:
                        break
                    if attempt == 2:
                        for c in (["ip", "link", "set", ifc, "down"], ["iw", "dev", ifc, "set", "type", "monitor"],
                                  ["ip", "link", "set", ifc, "up"]):
                            wm.run(c, timeout=5)
                    time.sleep(0.5)
            sv = moncore.Survey(ifc, self.chans, band=b, mode=want, phy=i["phy"])
            sv.start()
            return sv

        def done(sv, err):
            if sv:
                self.survey = app.survey = sv
                self.started = time.time()
                if sv.band != band:
                    app.toast(f"{i['driver']} can't survey {band} - showing {sv.band}", WARN)
                elif sv.mode == "sweep":
                    app.toast(f"scan-sweep: {ifc} stays connected, listening on {sv.cap_if}", OK)
                else:
                    app.toast(f"true monitor mode ({sv.band}): continuous listening", OK)
            elif err:
                app.toast(f"survey failed: {err}", BAD)
        if needs_probe:
            label = f"Testing {i.get('chip') or i['driver']} (one time, ~15 s)…"
        elif self.mode_for(band) == "monitor" and i.get("bus") == "usb" and band in ("ALL", "2G") and (
                self.reset_next or (self.caps or {}).get("reset24")):
            label = f"Resetting {ifc} (like a replug) for 2.4 GHz monitor…"
        else:
            label = "Starting " + ("scan-sweep" if self.mode_for(band) == "sweep" else "monitor mode") + f" ({band})…"
        go = lambda: app.job(label, prep, done=done, ok_toast=False)
        if self.i.get("ssh") and (needs_probe or self.mode_for(band) == "monitor"):
            app.ssh_guard(ifc, "Monitor mode", go)
        else:
            go()

    def _stop_survey(self):
        if self.survey:
            res = self.survey.stop_capture()
            if res:
                self.app.toast(f"saved {res[1]} frames → {res[0]}", OK)
            self.survey.stop()
            self.app.survey = None
            self.survey = None

    def tick(self):
        if self.pending:
            band, self.pending = self.pending, None
            self._launch(band)
            return
        s = self.survey
        if s and self.live:
            self.rows = s.snapshot()
            self.idx = min(self.idx, max(0, len(self.rows) - 1))
            self._check_24_silent(s)

    # Live check: the one-time probe can pass and true monitor mode still be deaf on 2.4 GHz
    # later (rtw88 behaviour depends on the radio's prior state). If a monitor survey covering
    # 2.4 GHz has heard no 2.4 GHz frame after a full pass, switch to scan-sweep and remember it.
    SILENT_24 = {"2G": 12, "ALL": 18}

    def _check_24_silent(self, s):
        if s.mode != "monitor" or s.band not in self.SILENT_24 or "2G" not in self.bands:
            return
        if s.band_frames.get("2G"):
            if self.reset_used and not (self.caps or {}).get("reset24"):
                # a fresh (replugged) radio hears 2.4 GHz -> do this before every 2.4 GHz monitor survey
                self.caps = dict(self.caps or {}, mon24=True, reset24=True)
                moncore.save_caps(nc.caps_key(self.i), self.caps)
                wm.log(f"survey {self.i['name']}: 2.4 GHz heard after USB reset - reset24 enabled")
            return
        if self.fell_back or (s.locked and s.locked > 14):
            return
        if time.time() - self.started < self.SILENT_24[s.band]:
            return
        band = s.band
        if self.i.get("bus") == "usb" and not self.reset_used and not self.reset_tried:
            # rtw88 dongles hear 2.4 GHz in monitor mode only on a freshly initialised radio
            self.reset_tried = True
            wm.log(f"survey {self.i['name']}: 0 frames on 2.4 GHz ({self.prior}) - trying a USB reset")
            self._stop_survey()
            self.rows = []
            self.app.toast("2.4 GHz silent - resetting the dongle and retrying", WARN)
            self.reset_next = True
            self.pending = band
            return
        self.fell_back = True
        wm.log(f"survey {self.i['name']}: 0 frames on 2.4 GHz after {self.SILENT_24[band]} s of true monitor "
               f"(5G frames {s.band_frames.get('5G', 0)}, reset {self.reset_used}, {self.prior})"
               " - switching to scan-sweep")
        self.caps = dict(self.caps or {}, mon24=False, reset24=False, sweep=True, fallback="live")
        moncore.save_caps(nc.caps_key(self.i), self.caps)
        self._stop_survey()
        self.rows = []
        self.app.toast("2.4 GHz silent in monitor mode - switching to scan-sweep", WARN)
        self.pending = band

    def right_text(self):
        s = self.survey
        if not s:
            return "starting…"
        bf = s.band_frames
        if getattr(s, "note", ""):
            return s.note
        mode = "SWEEP" if s.mode == "sweep" else "MON"
        counts = " ".join(f"{b}:{_k(bf[b])}" for b in ("2G", "5G", "6G")
                          if bf.get(b) and (s.band == "ALL" or s.band == b)) or "0 frames"
        t = f"{mode} ch{s.cur_chan or '?'}{'L' if s.locked else ''} {counts}"
        if s.pcap:
            t = f"REC {s.pcap_bytes // 1024}k " + t
        return t

    def render(self):
        s = self.survey
        self.title = f"{s.band if s else ''} · {len(self.rows)} APs"
        return super().render()

    def empty_text(self):
        return "Listening for beacons…"

    def render_row(self, d, a, y, fg, sel):
        rs = a["rssi"]
        cw = tw(d, "149", F_MONO)
        ch = str(a["chan"] or "?")
        d.text((6 + cw - tw(d, ch, F_MONO), y + 1), ch, font=F_MONO, fill=fg)
        x = 6 + cw + 5
        d.text((x, ysm(y)), a["band"], font=F_SMALL, fill=fg if sel else ACCENT)
        x += tw(d, "5G", F_SMALL) + 6
        rtxt = str(rs if rs is not None else "?")
        rw = tw(d, "-100", F_MONO)
        d.text((x + rw - tw(d, rtxt, F_MONO), y + 1), rtxt, font=F_MONO, fill=fg if sel else (
            OK if rs and rs > -65 else WARN if rs and rs > -80 else MUTED))
        x += rw + 7
        left = rcols(d, y, [(f"{a['clients']}c" if a["clients"] else "", F_SMALL, fg if sel else PURPLE),
                            (short_sec(a["security"]), F_SMALL, fg if sel else MUTED)])
        name = a["ssid"] or (f"(hidden) · {a['hint']}" if a.get("hint") else "(hidden)")
        col = MUTED if not a["ssid"] else PURPLE if a.get("hidden") else fg
        d.text((x, y + 1), fit(d, name, F_BODY, left - x - 4), font=F_BODY, fill=fg if sel else col)

    def on_enter(self, a):
        self.app.push(APDetailScreen(self.app, self.survey, a["bssid"]))

    def on_char(self, k):
        s = self.survey
        if not s:
            return
        if k.c == "b":
            cur = s.band if s.band in self.bands else "ALL"
            nb = self.bands[(self.bands.index(cur) + 1) % len(self.bands)]
            if self.mode_for(nb) == s.mode:
                s.set_band(nb)
                self.app.toast(f"survey band: {nb}")
            else:
                self._stop_survey()
                self.rows = []
                self.pending = nb
        elif k.c == "l":
            if s.locked:
                s.lock_channel(None)
                self.app.toast("channel hopping resumed")
            else:
                chans = [(c, f) for c, f in s.all_channels if s.band == "ALL" or nc.band_of(f) == s.band]
                cur = self.rows[self.idx]["chan"] if self.rows else None
                self.app.push(ChannelPicker(self.app, chans, cur, lambda ch: (
                    s.lock_channel(ch), self.app.toast(f"locked on ch{ch}"))))
        elif k.c == "r":
            if s.pcap:
                path, n, b = s.stop_capture()
                self.app.toast(f"saved {n} frames → {path}", OK)
            else:
                try:
                    s.start_capture()
                    self.app.toast(f"recording → {s.pcap_path}", WARN)
                except OSError as e:
                    self.app.toast(f"capture failed: {e}", BAD)
        elif k.c == "m":
            self.leave()

    def key(self, k):
        if k.esc():
            self.leave()
            return
        super().key(k)

    def leave(self):
        app, ifc = self.app, self.i["name"]
        in_monitor = (self.survey and self.survey.mode == "monitor") or self.made_monitor
        if not in_monitor:
            app.pop(self)
            return

        def keep():
            self.keep_monitor = True
            app.pop(self)
            app.toast(f"{ifc} left in monitor mode — use it in other tools (Enter > Raw monitor OFF to undo)", WARN)
        app.push(ConfirmScreen(app, "Leave survey", f"Keep {ifc} in monitor mode for other tools "
                               "(airodump-ng, Wireshark, Kismet)?", keep, lambda: app.pop(self), color=ACCENT,
                               hint="Enter = keep monitor    Esc = back to normal"))

    def on_close(self):
        mode = self.survey.mode if self.survey else None
        self._stop_survey()
        if getattr(self, "keep_monitor", False) or self.app.stop.is_set():
            return   # user chose to keep it, or the app is exiting: leave the dongle exactly as it is
        if self.made_monitor or mode == "monitor":
            ifc = self.i["name"]
            threading.Thread(target=lambda: wm.mon_off(ifc), daemon=True).start()
            self.app.toast(f"{ifc} back to normal mode", OK)


def _k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class ChannelPicker(ListScreen):
    def __init__(self, app, chans, current, on_pick):
        super().__init__(app, "Lock to channel")
        self.rows, self.on_pick = chans, on_pick
        self.idx = next((n for n, (c, _) in enumerate(chans) if c == current), 0)
        self.digits, self.last = "", 0

    def render_row(self, d, c, y, fg, sel):
        cw = tw(d, "149", F_MONO)
        d.text((10 + cw - tw(d, str(c[0]), F_MONO), y + 1), str(c[0]), font=F_MONO, fill=fg)
        d.text((22 + cw, y + 1), f"{c[1]} MHz", font=F_BODY, fill=fg)
        d.text((34 + cw + tw(d, "5825 MHz", F_BODY), ysm(y)), nc.band_of(c[1]), font=F_SMALL, fill=fg if sel else ACCENT)

    def on_enter(self, c):
        self.app.pop()
        self.on_pick(c[0])

    def on_char(self, k):
        if k.c.isdigit():
            now = time.time()
            self.digits = (self.digits if now - self.last < 1.5 else "") + k.c
            self.last = now
            for n, (c, _) in enumerate(self.rows):
                if str(c).startswith(self.digits):
                    self.idx = n
                    break


class APDetailScreen(KVScreen):
    live = 1.0

    def __init__(self, app, survey, bssid):
        super().__init__(app, "AP details")
        self.survey, self.bssid = survey, bssid

    def rows(self):
        a = next((x for x in self.survey.snapshot() if x["bssid"] == self.bssid), None) if self.survey else None
        if not a:
            return [("BSSID", self.bssid), ("Status", "no longer seen")]
        self.title = a["ssid"] or (f"(hidden) · {a['hint']}" if a.get("hint") else "(hidden)")
        return moncore.ap_detail_lines(a)


# ---------------------------------------------------------------- hotspot
def wifi_hotspot(app, i):
    if not i.get("ap_ok"):
        app.toast(f"{i['name']} can't run a hotspot", BAD)
        return
    app.push(HotspotScreen(app, i["name"]))


class HotspotScreen(Screen):
    live = 2.0

    def __init__(self, app, ifc):
        super().__init__(app)
        self.ifc = ifc
        self.conf = nc.hotspot_conf()

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        i = self.app.iface(self.ifc) or {}
        on = bool(i.get("hotspot"))
        header(d, f"Hotspot on {self.ifc}", "ON" if on else "off")
        lx = 10 + tw(d, "Password", F_SMALL) + 10
        gap = F_BODY.size + 6
        y = HDR + 3
        if on:
            st, col = f"running · {nc.hotspot_clients(self.ifc)} client(s)", OK
        else:
            st, col = "stopped", MUTED
        with self.app.lock:
            up = [x for x in self.app.ifs if x["name"] != self.ifc and x["nm_state"] == "connected"]
        rows = [("Name", self.conf["ssid"], F_BOLD, TEXT), ("Password", self.conf["psk"], F_MONO, TEXT),
                ("Status", st, F_BODY, col),
                ("Internet", f"via {up[0]['name']} ({up[0]['ssid']})" if up else "no other radio connected",
                 F_BODY, TEXT if up else WARN)]
        for lab, val, font, colr in rows:
            d.text((10, ysm(y)), lab, font=F_SMALL, fill=MUTED)
            d.text((lx, y), fit(d, val, font, W - lx - 8), font=font, fill=colr)
            y += gap
        for n, line in enumerate(wrap_words(d, f"Starting disconnects {self.ifc} from its network. "
                                               "Clients get NAT'd internet via the other radio.", F_SMALL, W - 20)):
            if y + (F_SMALL.size + 2) * (n + 1) < H - FTR - 2:
                d.text((10, y + 2 + (F_SMALL.size + 2) * n), line, font=F_SMALL, fill=MUTED)
        footer(d, "Enter start/stop  N name  K password  Esc back", *self.app.toast_now())
        return img

    def key(self, k):
        app, ifc = self.app, self.ifc
        i = app.iface(ifc) or {}
        if k.esc():
            app.pop()
        elif k.enter():
            if i.get("hotspot"):
                app.job("Stopping hotspot…", nc.hotspot_stop, ifc)
            else:
                app.ssh_guard(ifc, "Starting the hotspot", lambda: app.job("Starting hotspot…", nc.hotspot_start, ifc))
        elif k.c == "n":
            app.push(TextInputScreen(app, "Hotspot name", "SSID", self._set("ssid"), initial=self.conf["ssid"], maxlen=32))
        elif k.c == "k":
            app.push(TextInputScreen(app, "Hotspot password", "8-63 characters", self._set("psk"),
                                     initial=self.conf["psk"], secret=True))
        self.dirty = True

    def _set(self, field):
        def done(v):
            if v is None:
                return
            c = dict(self.conf, **{field: v})
            try:
                nc.hotspot_save(c)
                self.conf = c
                self.app.toast("saved — restart the hotspot to apply", OK)
            except wm.WifiError as e:
                self.app.toast(str(e), BAD)
        return done


# ---------------------------------------------------------------- tests
def wifi_test(app, i):
    if i["nm_state"] != "connected" or i.get("hotspot"):
        app.toast("connect this radio to a network first")
        return
    app.push(TestScreen(app, i["name"]))


class TestScreen(Screen):
    live = 0.3

    def __init__(self, app, ifc):
        super().__init__(app)
        self.ifc = ifc
        self.start()

    def start(self):
        self.ping = self.speed = self.err = None
        self.phase, self.prog = "ping", (0, 0.0)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self.ping = nc.ping_test(self.ifc)
            self.phase = "speed"
            self.speed = nc.speed_test(self.ifc, progress=lambda b, t: setattr(self, "prog", (b, t)))
            self.phase = "done"
        except Exception as e:
            self.err, self.phase = str(e), "done"

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        i = self.app.iface(self.ifc) or {}
        header(d, f"Link test · {self.ifc}", i.get("ssid", ""))
        p = self.ping
        d.text((10, 22), "Ping 1.1.1.1", font=F_SMALL, fill=MUTED)
        if p:
            txt = (f"{p.get('avg', 0):.0f} ms avg  ({p.get('min', 0):.0f}-{p.get('max', 0):.0f})   "
                   f"loss {p['loss']:.0f}%") if "avg" in p else f"loss {p['loss']:.0f}%"
            d.text((10, 34), txt, font=F_BODY, fill=OK if p["loss"] == 0 else WARN)
        else:
            d.text((10, 34), "running…" if self.phase == "ping" else "-", font=F_BODY, fill=MUTED)
        d.text((10, 56), "Download (Cloudflare)", font=F_SMALL, fill=MUTED)
        if self.speed:
            d.text((10, 66), f"{self.speed['mbps']:.1f}", font=F_BIG, fill=TEXT)
            d.text((14 + tw(d, f"{self.speed['mbps']:.1f}", F_BIG), 86), "Mbit/s", font=F_BODY, fill=MUTED)
        elif self.phase == "speed":
            b, t = self.prog
            mb = b * 8 / max(t, 0.01) / 1e6 if t else 0
            d.text((10, 70), f"{b / 1e6:.1f} MB  ~{mb:.1f} Mbit/s", font=F_BODY, fill=WARN)
            d.rectangle([10, 90, 10 + int((W - 20) * min(1, b / 10_000_000)), 96], fill=ACCENT)
        if self.err:
            for n, line in enumerate(wrap_words(d, self.err, F_SMALL, W - 20)[:2]):
                d.text((10, 108 + 12 * n), line, font=F_SMALL, fill=BAD)
        footer(d, "R run again  Esc back", "via SO_BINDTODEVICE — traffic forced through this radio")
        return img

    def key(self, k):
        if k.esc():
            self.app.pop()
        elif k.c == "r" and self.phase == "done":
            self.start()


# ================================================================ Bluetooth actions
def bt_shortcut(app, a, k):
    if not app.bt:
        app.toast(app.bt_error or "Bluetooth not ready", BAD)
        return
    acts = {"enter": bt_menu, "s": bt_scan, "l": bt_paired, "o": bt_power}
    key = "enter" if k.enter() else k.c
    if key in acts:
        acts[key](app, a)


def bt_menu(app, a):
    items = [("Scan devices", (lambda: bt_scan(app, a)) if a["powered"] else None, "S"),
             ("Paired devices", lambda: bt_paired(app, a), "L"),
             ("Power " + ("off" if a["powered"] else "on"), lambda: bt_power(app, a), "O")]
    app.push(MenuScreen(app, f"{a['hci']} ({a['role']}) actions", items))


def bt_power(app, a):
    app.job(f"{a['hci']} power…", app.bt.power, a["path"], not a["powered"])


def bt_scan(app, a):
    if not a["powered"]:
        app.toast(f"{a['hci']} is off — press O", WARN)
        return
    app.push(BtListScreen(app, a, scan=True))


def bt_paired(app, a):
    app.push(BtListScreen(app, a, scan=False))


class BtListScreen(ListScreen):
    live = 1.0

    def __init__(self, app, a, scan):
        super().__init__(app, "")
        self.a, self.scan, self.named_only = a, scan, False
        self.hint = "Enter details  N named only  Esc back" if scan else "Enter details  Esc back"
        if scan:
            try:
                app.bt.start_scan(a["path"])
            except Exception as e:
                app.toast(f"scan: {e}", BAD)
        self.tick()

    def tick(self):
        try:
            devs = self.app.bt.devices(self.a["path"], paired_only=not self.scan)
        except Exception:
            devs = self.rows
        if self.named_only:
            devs = [d for d in devs if d["name"]]
        sel_path = self.rows[self.idx]["path"] if self.rows and self.idx < len(self.rows) else None
        self.rows = devs
        if sel_path:
            self.idx = next((n for n, d in enumerate(devs) if d["path"] == sel_path), min(self.idx, len(devs) - 1))
        self.idx = max(0, self.idx)
        mode = "scanning" if self.scan else "paired"
        self.title = f"{self.a['hci']} {self.a['role']} · {mode} · {len(devs)}" + (" · named" if self.named_only else "")

    def empty_text(self):
        return "Scanning… make your device discoverable." if self.scan else "No paired devices."

    def render_row(self, d, x, y, fg, sel):
        bars(d, 6, y + (ROW - 12) // 2, dbm_bars(x["rssi"]), fg if sel else OK)
        left = rcols(d, y, [(str(x["rssi"]) if x["rssi"] is not None else "--", F_MONO, fg if sel else MUTED),
                            (btcore.kind_of(x), F_SMALL, fg if sel else PURPLE)])
        xx = lead_marks(d, 26, y, fg, sel, dot=x["connected"], mark="✓" if x["paired"] else None, mark_col=ACCENT)
        d.text((xx, y + 1), fit(d, x["alias"] or x["address"], F_BODY, left - xx - 4), font=F_BODY,
               fill=fg if x["name"] else (fg if sel else MUTED))

    def on_enter(self, x):
        self.app.push(BtDetailScreen(self.app, self.a, x["path"]))

    def on_char(self, k):
        if k.c == "n" and self.scan:
            self.named_only = not self.named_only
            self.tick()

    def on_close(self):
        if self.scan and self.app.bt:
            self.app.bt.stop_scan(self.a["path"])


class BtDetailScreen(KVScreen):
    live = 2.0

    def __init__(self, app, a, path):
        super().__init__(app, "Device")
        self.a, self.path, self.dev = a, path, None
        self.hint = "P pair  O connect  T trust  E remove  H hunt"

    def rows(self):
        try:
            self.dev = self.app.bt.device(self.path)
        except Exception:
            return [("Device", "gone (removed or out of range)")]
        self.title = self.dev["alias"] or self.dev["address"]
        return btcore.device_detail_lines(self.dev)

    def on_char(self, k):
        app, bt, d = self.app, self.app.bt, self.dev
        if not d:
            return
        if k.c == "p":
            if d["paired"]:
                app.toast("already paired")
            else:
                app.job(f"Pairing {self.title}… (answer prompts)", bt.pair, self.path)
        elif k.c == "o":
            if d["connected"]:
                app.job("Disconnecting…", bt.disconnect, self.path)
            else:
                app.job(f"Connecting {self.title}…", bt.connect, self.path)
        elif k.c == "t":
            try:
                app.toast(bt.trust(self.path, not d["trusted"]), OK)
            except Exception as e:
                app.toast(str(e), BAD)
        elif k.c == "e":
            app.confirm("Remove device", f"Remove {self.title} from {self.a['hci']}? It will need pairing again.",
                        lambda: app.job("Removing…", bt.remove, self.a["path"], self.path,
                                        done=lambda r, e: r and app.pop(self)))
        elif k.c == "h":
            app.push(BtHuntScreen(app, self.a, self.path, self.title))


class BtHuntScreen(Screen):
    live = 0.3

    def __init__(self, app, a, path, title):
        super().__init__(app)
        self.a, self.path, self.title = a, path, title
        self.hist, self.last = [], 0
        try:
            app.bt.start_scan(a["path"])
        except Exception:
            pass

    def tick(self):
        if time.time() - self.last >= 0.5:
            self.last = time.time()
            r = self.app.bt.rssi(self.path)
            if r is not None:
                self.hist = (self.hist + [r])[-90:]

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        header(d, f"Hunt · {self.title}", self.a["hci"])
        r = self.hist[-1] if self.hist else None
        s = f"{r}" if r is not None else "--"
        d.text((12, 22), s, font=F_BIG, fill=TEXT)
        d.text((18 + tw(d, s, F_BIG), 44), "dBm", font=F_BODY, fill=MUTED)
        tr = btcore.trend(self.hist)
        arrow, col = {1: ("▲ warmer", OK), -1: ("▼ colder", BAD), 0: ("● steady", MUTED)}[tr]
        d.text((200, 28), arrow, font=F_SYM_B, fill=col)
        pct = btcore.rssi_bar_pct(r)
        d.rounded_rectangle([12, 66, W - 12, 78], radius=4, fill=PANEL)
        d.rounded_rectangle([12, 66, 12 + int((W - 24) * pct / 100), 78], radius=4,
                            fill=OK if pct > 60 else WARN if pct > 30 else BAD)
        if len(self.hist) > 1:
            lo, hi = -100, -30
            pts = []
            for n, v in enumerate(self.hist):
                x = 12 + (W - 24) * n / 89
                y = 136 - (max(lo, min(hi, v)) - lo) * 50 / (hi - lo)
                pts.append((x, y))
            d.line(pts, fill=ACCENT, width=2)
        d.line([12, 137, W - 12, 137], fill=DIM)
        footer(d, "Esc back", "walk around — bar grows as you get closer")
        return img

    def key(self, k):
        if k.esc():
            self.app.pop()


def handle_agent_prompt(app, p):
    kind, dev, val = p["kind"], p["device"], p["value"]
    bt = app.bt
    if kind == "cancel":
        if isinstance(app.stack[-1], (ConfirmScreen, TextInputScreen)):
            app.pop()
        app.toast("pairing cancelled by device", WARN)
    elif kind == "confirm":
        app.push(ConfirmScreen(app, "Confirm pairing", f"Does {dev} show this code?",
                               lambda: bt.reply(p, True), lambda: bt.reply(p, False), ACCENT, big=val,
                               hint="Enter = matches    Esc = reject"))
    elif kind == "show":
        app.push(InfoScreen(app, "Enter this code", f"Type this on {dev}:", big=val))
    elif kind in ("pin", "passkey"):
        app.push(TextInputScreen(app, "Pairing", f"{'PIN' if kind == 'pin' else 'Passkey'} for {dev}",
                                 lambda v: bt.reply(p, v is not None, v), numeric=(kind == "passkey"),
                                 maxlen=16 if kind == "pin" else 6))
    elif kind in ("authorize", "service"):
        body = f"Allow {dev}" + (f" to use {val}?" if val else " to pair?")
        app.push(ConfirmScreen(app, "Authorize", body, lambda: bt.reply(p, True), lambda: bt.reply(p, False), ACCENT))


# ================================================================ modes
def keytest():
    kb = Keyboard()
    print(f"{len(kb.fds)} input device(s). Ctrl+C to stop.")
    try:
        while True:
            r, _, _ = select.select(kb.fds, [], [], 1) if kb.fds else ([], [], [])
            for fd in r:
                data = os.read(fd, EV_SIZE * 64)
                for off in range(0, len(data) - EV_SIZE + 1, EV_SIZE):
                    _, _, t, code, val = struct.unpack(EV_FMT, data[off:off + EV_SIZE])
                    if t == 1:
                        print(f"code={code:<4} value={val}")
    except KeyboardInterrupt:
        pass


def ensure_root(argv):
    """APPLaunch normally starts apps as root. If not, fall back to passwordless sudo when the
    user has it (Raspberry Pi OS's first user does); otherwise the app shows a not-root banner."""
    if os.geteuid() == 0:
        return True
    me = os.path.realpath(__file__)
    try:
        if not argv and subprocess.run(["sudo", "-n", "-l", me], capture_output=True, timeout=5).returncode == 0:
            os.execvp("sudo", ["sudo", "-n", me])
        if subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0:
            os.execvp("sudo", ["sudo", "-n", sys.executable, me] + argv)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def launch_info():
    try:
        with open(f"/proc/{os.getppid()}/comm") as f:
            parent = f.read().strip()
    except OSError:
        parent = "?"
    return f"uid={os.getuid()} euid={os.geteuid()} parent={parent}({os.getppid()}) user={os.environ.get('USER', '?')}"


def main():
    args = sys.argv[1:]
    if args[:1] == ["--preview"]:
        import preview
        preview.run(args[1] if len(args) > 1 else "preview")
        return 0
    if args[:1] in (["--version"], ["-V"]):
        print(VERSION)
        return 0
    rooted = ensure_root(args)
    if args[:1] == ["--keytest"]:
        keytest()
        return 0
    fb, kb = Framebuffer(find_fb()), Keyboard()
    app = App(fb, kb)
    app.rooted = rooted
    wm.log(f"dualradio launch: {launch_info()}")
    if not rooted:
        app.toast("not running as root — see /var/log/dualradio.log", BAD)

    def on_signal(_s, _f):
        app.stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    wm.log(f"dualradio {VERSION} start fb={fb.path} {fb.fw}x{fb.fh}@{fb.bpp} inputs={len(kb.fds)}")
    try:
        app.run(HomeScreen(app))
    finally:
        app.shutdown()
        fb.close()
        kb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
