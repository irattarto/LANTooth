"""Persisted settings (shared by GUI and CLI) — remembered phone IPs, audio
devices and stream options."""

import json
import os
import tempfile

from appdata import app_data_dir

CONFIG_PATH = os.path.join(app_data_dir(), "gui_config.json")

MAX_REMEMBERED_IPS = 10

_DEFAULTS = {
    "android_ip": "",       # last phone connected to (auto-connect target on GUI start)
    "recent_ips": [],       # phones successfully connected to, most recent first
    "capture_device_name": "",
    "playback_device_name": "",
    "stereo": True,
}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {**_DEFAULTS, "recent_ips": []}  # fresh list, not the shared default
    cfg = {**_DEFAULTS, **{k: v for k, v in data.items() if k in _DEFAULTS}}
    if not isinstance(cfg["recent_ips"], list):
        cfg["recent_ips"] = []
    return cfg


def remember_ip(cfg: dict, ip: str) -> None:
    """Record a successful connection to `ip` (moves it to the front) and save."""
    recent = [x for x in cfg.get("recent_ips", []) if x != ip]
    cfg["recent_ips"] = [ip] + recent[:MAX_REMEMBERED_IPS - 1]
    cfg["android_ip"] = ip
    save_config(cfg)


def forget_ip(cfg: dict, ip: str) -> None:
    cfg["recent_ips"] = [x for x in cfg.get("recent_ips", []) if x != ip]
    if cfg.get("android_ip") == ip:
        cfg["android_ip"] = cfg["recent_ips"][0] if cfg["recent_ips"] else ""
    save_config(cfg)


def save_config(cfg: dict) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass
