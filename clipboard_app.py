"""Clipboard History for Windows.

Watches the system clipboard in the background and lets you search and paste
back anything you copied earlier with a single global hotkey.

    Ctrl + Shift + V        open the history popup
    Ctrl + Shift + Alt + Q  quit the app (or use the Quit button)

Everything is configurable in config.json next to the executable.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import queue
import sys
import tempfile
import time
import tkinter as tk
from datetime import datetime

import keyboard
from pynput.keyboard import Controller, Key

from actions import load_config

try:  # keeps text crisp on high-DPI screens
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

APP_NAME = "ClipboardHistory"
IS_FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(sys.executable) if IS_FROZEN else os.path.dirname(os.path.abspath(__file__))
ERROR_ALREADY_EXISTS = 183

DEFAULT_CONFIG = {
    "enabled": True,
    "hotkey": "ctrl+shift+v",
    "quit_hotkey": "ctrl+shift+alt+q",
    "max_items": 200,
    "max_chars": 20000,
    "ignore_if_contains": [],
    "auto_paste": True,
    "paste_delay_ms": 250,
    "poll_interval_ms": 400,
    "preview_chars": 120,
    "popup_width": 520,
    "popup_height": 420,
    "show_timestamps": True,
    "data_dir": "",
}

DATA_DIR = None


def log(message):
    """Append a line to error.log. A noconsole exe has nowhere else to report."""
    folder = DATA_DIR or tempfile.gettempdir()
    try:
        with open(os.path.join(folder, "error.log"), "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (datetime.now().isoformat(timespec="seconds"), message))
    except OSError:
        pass


def keep_single_instance():
    """Return False when another copy of the app is already running."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.HANDLE, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW(None, False, "Local\\" + APP_NAME)
    # use_last_error snapshots the value for this exact call, so intervening
    # Python/C activity can't clobber it the way kernel32.GetLastError() can.
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def notify(message, title=APP_NAME):
    """Small info dialog. argtypes matter: without them ctypes passes str as
    bytes and the W-suffixed API reads garbage. MB_SETFOREGROUND|MB_TOPMOST
    keeps it in front of an already-busy desktop."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = wintypes.INT
    flags = 0x40 | 0x10000 | 0x40000  # MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
    user32.MessageBoxW(None, message, title, flags)


def pick_data_dir(configured=""):
    """Prefer a data folder next to the app, else fall back to %LOCALAPPDATA%.

    Running from a read-only location (Program Files, a network share) would
    otherwise crash the app the first time it tries to save.
    """
    candidates = [configured] if configured else [
        os.path.join(APP_DIR, "data"),
        os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), APP_NAME),
    ]
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write-test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
            return candidate
        except OSError:
            continue
    return candidates[-1]


def relative_time(ts):
    """Compact age label: 'now', '4m', '3h', '2d'."""
    if not ts:
        return ""
    age = time.time() - ts
    if age < 60:
        return "now"
    if age < 3600:
        return "%dm" % (age // 60)
    if age < 86400:
        return "%dh" % (age // 3600)
    return "%dd" % (age // 86400)


class ClipboardApp:
    def __init__(self, cfg, data_dir):
        self.cfg = cfg
        self.max_items = max(1, int(cfg.get("max_items", 200)))
        self.max_chars = max(200, int(cfg.get("max_chars", 20000)))
        self.preview_chars = max(20, int(cfg.get("preview_chars", 120)))
        self.ignore = [s.lower() for s in cfg.get("ignore_if_contains", []) if s]
        self.auto_paste = bool(cfg.get("auto_paste", True))
        self.paste_delay = max(0, int(cfg.get("paste_delay_ms", 250)))
        self.interval = max(100, int(cfg.get("poll_interval_ms", 400)))
        self.show_ts = bool(cfg.get("show_timestamps", True))
        self.popup_w = max(280, int(cfg.get("popup_width", 520)))
        self.popup_h = max(200, int(cfg.get("popup_height", 420)))

        self.data_file = os.path.join(data_dir, "clipboard_history.json")
        self.items = self.load()
        self.last = self.items[0]["text"] if self.items else None
        self.visible = []
        self.popup = None
        self.keys = Controller()
        self.q = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        keyboard.add_hotkey(cfg.get("hotkey", "ctrl+shift+v"), lambda: self.q.put("show"))
        if cfg.get("quit_hotkey"):
            keyboard.add_hotkey(cfg["quit_hotkey"], lambda: self.q.put("quit"))
        self.root.after(self.interval, self.watch)
        self.root.after(50, self.poll)
        self.root.mainloop()

    # ---------------------------------------------------------------- storage

    def load(self):
        try:
            with open(self.data_file, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log("could not read history, starting fresh: %s" % exc)
            return []

        items = []
        for entry in raw if isinstance(raw, list) else []:
            if isinstance(entry, str):  # format from older versions
                text, ts = entry, 0
            elif isinstance(entry, dict) and isinstance(entry.get("text"), str):
                text, ts = entry["text"], entry.get("ts") or 0
            else:
                continue
            items.append({"text": text, "ts": ts})
        return items[: self.max_items]

    def save(self):
        os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
        tmp = self.data_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.items, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.data_file)  # atomic: never leaves a half-written file

    # ------------------------------------------------------------- clipboard

    def add(self, text):
        if len(text) > self.max_chars:
            text = text[: self.max_chars]
        self.items = [i for i in self.items if i["text"] != text]
        self.items.insert(0, {"text": text, "ts": time.time()})
        del self.items[self.max_items:]
        self.save()

    def watch(self):
        try:
            try:
                text = self.root.clipboard_get()
            except tk.TclError:
                text = None  # nothing usable on the clipboard (e.g. an image)
            if text and text.strip() and text != self.last:
                self.last = text
                if not any(s in text.lower() for s in self.ignore):
                    self.add(text)
        except Exception as exc:  # keep the watcher alive no matter what
            log("watch error: %r" % (exc,))
        self.root.after(self.interval, self.watch)

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg == "show":
                    self.open_popup()
                elif msg == "quit":
                    self.quit()
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    # ------------------------------------------------------------------- ui

    def open_popup(self):
        self.close()
        x, y = self.root.winfo_pointerxy()
        p = tk.Toplevel(self.root)
        p.title("Clipboard History")
        p.geometry("%dx%d+%d+%d" % (self.popup_w, self.popup_h, x, y))
        p.attributes("-topmost", True)
        self.popup = p

        self.search = tk.Entry(p, font=("Segoe UI", 11))
        self.search.pack(fill="x", padx=8, pady=(8, 4))

        self.lb = tk.Listbox(p, font=("Consolas", 10), activestyle="none",
                             selectbackground="#0e639c", selectforeground="#ffffff")
        self.lb.pack(fill="both", expand=True, padx=8, pady=4)

        bar = tk.Frame(p)
        bar.pack(fill="x", padx=8, pady=(4, 8))
        self.count = tk.Label(bar, text="", font=("Segoe UI", 9), fg="#888888")
        self.count.pack(side="left")
        for label, command in (("Paste", self.choose), ("Delete", self.delete),
                               ("Clear", self.clear_all), ("Quit", self.quit)):
            tk.Button(bar, text=label, font=("Segoe UI", 9), command=command).pack(side="right", padx=(6, 0))

        self.search.bind("<KeyRelease>", lambda e: self.fill())
        self.search.bind("<Return>", lambda e: self.choose())
        self.search.bind("<Down>", lambda e: (self.lb.focus_set(), self.lb.selection_clear(0, "end"),
                                              self.lb.selection_set(0) if self.visible else None))
        self.lb.bind("<Return>", lambda e: self.choose())
        self.lb.bind("<Double-Button-1>", lambda e: self.choose())
        self.lb.bind("<Delete>", lambda e: self.delete())
        self.lb.bind("<Up>", lambda e: self.search.focus_set() if self.lb.curselection() == (0,) else None)
        p.bind("<Escape>", lambda e: self.close())
        p.bind("<MouseWheel>", lambda e: self.lb.yview_scroll(-1 * (e.delta // 120), "units"))
        for digit in range(1, 10):
            p.bind("<Control-%d>" % digit, lambda e, d=digit: self.choose_index(d - 1))

        self.fill()
        self.search.focus_force()

    def fill(self):
        needle = self.search.get().lower()
        self.visible = [i for i in self.items if needle in i["text"].lower()]
        self.lb.delete(0, "end")
        for item in self.visible:
            prefix = relative_time(item["ts"]).ljust(4) + "  " if self.show_ts else ""
            preview = " ".join(item["text"].split())[: self.preview_chars]
            self.lb.insert("end", prefix + preview)
        if not self.visible:
            self.lb.insert("end", "  (no matches)")
        self.count.configure(text="%d of %d entries" % (len(self.visible), len(self.items)))

    # -------------------------------------------------------------- actions

    def choose(self):
        sel = self.lb.curselection()
        self.choose_index(sel[0] if sel else 0)

    def choose_index(self, index):
        if index >= len(self.visible):
            return
        text = self.visible[index]["text"]
        self.close()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.last = text
        if self.auto_paste:
            self.root.after(self.paste_delay, self.paste)

    def paste(self):
        self.keys.press(Key.ctrl)
        self.keys.press("v")
        self.keys.release("v")
        self.keys.release(Key.ctrl)

    def delete(self):
        sel = self.lb.curselection()
        if not sel:
            return
        self.items = [i for i in self.items if i is not self.visible[sel[0]]]
        self.save()
        self.fill()

    def clear_all(self):
        self.items = []
        self.last = None
        self.save()
        self.fill()

    def close(self):
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None

    def quit(self):
        self.close()
        self.save()
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.root.destroy()


def main():
    cfg = load_config(DEFAULT_CONFIG)

    if not keep_single_instance():
        hotkey = " + ".join(p.title() for p in cfg.get("hotkey", "ctrl+shift+v").split("+"))
        notify("Clipboard History is already running.\n\nPress %s to open it." % hotkey)
        return

    if not cfg.get("enabled", True):
        return

    global DATA_DIR
    DATA_DIR = pick_data_dir(cfg.get("data_dir", ""))
    app = ClipboardApp(cfg, DATA_DIR)
    return app


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("fatal: %r" % (exc,))
        raise
