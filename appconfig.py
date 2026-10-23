"""
Loads config.json plus a hardware profile (profiles/<name>.json) on top of it.

The profile is picked automatically from the board the app runs on, so one copy
of the app works on a Raspberry Pi 500 and on a smaller Raspberry Pi 4.
Force one with  "profile": "pi4"  in config.json, or turn it off with "none".
"""
import json
from pathlib import Path

# Board name (from /proc/device-tree/model) -> profile file
BOARDS = [
    ("Raspberry Pi 4", "pi4"),
    ("Raspberry Pi 400", "pi4"),
]


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def board_name():
    try:
        return Path("/proc/device-tree/model").read_text(errors="ignore").strip("\x00 \n")
    except OSError:
        return ""


def detect_profile():
    model = board_name()
    for prefix, name in BOARDS:
        if model.startswith(prefix):
            # A Pi 4 with 8 GB can run Pip's normal setup; only small ones need the slim profile
            if name == "pi4" and total_ram_gb() > 6:
                return ""
            return name
    return ""


def total_ram_gb():
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 0.0


def load_config(here: Path):
    cfg = _load(here / "config.json")
    choice = str(cfg.get("profile", "auto")).strip().lower()
    name = detect_profile() if choice in ("", "auto") else ("" if choice == "none" else choice)
    prof = _load(here / "profiles" / f"{name}.json") if name else {}
    label = prof.pop("edition", "")
    prof.pop("_about", None)
    cfg.update(prof)
    cfg["profile_active"] = name
    cfg["edition"] = label
    return cfg


if __name__ == "__main__":   # handy for checking:  python3 appconfig.py
    c = load_config(Path(__file__).resolve().parent)
    print("Board:  ", board_name() or "(unknown)")
    print("RAM:    ", f"{total_ram_gb():.1f} GB")
    print("Profile:", c["profile_active"] or "standard")
    print("Model:  ", c.get("model"), "| Ears:", c.get("whisper_model"), "| Voice:", c.get("voice"))
