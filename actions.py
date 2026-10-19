"""Shared helpers for the desktop utilities in this project.

Handles config loading and the low-level "do something" actions (launch a
program, open a URL, run a hotkey, scroll, type) used by the dock and radial
menu as well as the clipboard history popup.
"""

import copy
import json
import os
import subprocess
import sys
import webbrowser

from pynput.keyboard import Controller as KeyController, Key
from pynput.mouse import Controller as MouseController

FROZEN = getattr(sys, "frozen", False)
RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
# In a PyInstaller onefile build __file__ points at a temp folder that gets
# deleted when the app exits, so writable files live next to the .exe instead.
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else RESOURCE_DIR

KEYBOARD = KeyController()
MOUSE = MouseController()

SPECIAL = {
    "ctrl": Key.ctrl, "shift": Key.shift, "alt": Key.alt, "win": Key.cmd,
    "enter": Key.enter, "esc": Key.esc, "tab": Key.tab, "space": Key.space,
    "backspace": Key.backspace, "delete": Key.delete,
    "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
    "home": Key.home, "end": Key.end, "pageup": Key.page_up, "pagedown": Key.page_down,
}
for _i in range(1, 13):
    SPECIAL[f"f{_i}"] = getattr(Key, f"f{_i}")


def _deep_merge(base, extra):
    """Recursively merge *extra* into *base* (dicts win over scalars)."""
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def load_config(defaults=None):
    """Return config.json merged on top of *defaults*.

    The bundled copy supplies defaults, then any config.json sitting next to
    the executable overrides it. A missing or malformed file is not fatal -
    the app still starts with whatever defaults were passed in.
    """
    merged = copy.deepcopy(defaults) if isinstance(defaults, dict) else {}
    for folder in (RESOURCE_DIR, BASE_DIR):
        path = os.path.join(folder, "config.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            _deep_merge(merged, data)
    return merged


def press_combo(combo):
    """Press keys like 'ctrl+shift+v' together, then release them."""
    keys = [SPECIAL.get(p.strip().lower(), p.strip()) for p in combo.split("+")]
    for k in keys:
        KEYBOARD.press(k)
    for k in reversed(keys):
        KEYBOARD.release(k)


def run_action(action):
    """Carry out a single action dict, e.g. {'type': 'url', 'target': ...}."""
    kind = action.get("type", "")
    target = action.get("target", "").replace("{dir}", BASE_DIR)
    if kind == "app":
        os.startfile(target)
    elif kind == "url":
        webbrowser.open(target)
    elif kind == "command":
        subprocess.Popen(target, shell=True)
    elif kind in ("hotkey", "key"):
        press_combo(target)
    elif kind == "scroll":
        MOUSE.scroll(action.get("dx", 0), action.get("dy", 0))
    elif kind == "type":
        KEYBOARD.type(target)
