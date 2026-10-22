#!/usr/bin/env python3
"""
One-time download of everything the app needs to run offline.
Run by the installer. Safe to run again: it skips what's already there.

  python setup_models.py            # download all
  python setup_models.py --check    # just report what's present
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
VOICES_DIR = HERE / "models" / "voices"
WHISPER_DIR = HERE / "models" / "whisper"

VOICE_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
             "{fam}/{code}/{name}/{q}/{code}-{name}-{q}{ext}?download=true")

GREEN, RED, DIM, END = ("\033[32m", "\033[31m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("",) * 4
if os.name == "nt":
    os.system("")  # enable colors in Windows console


def say(msg, color=""):
    print(f"{color}{msg}{END}", flush=True)


def download(url, dest: Path):
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        last = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total and time.time() - last > 0.5:
                last = time.time()
                print(f"\r    {got / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    if total:
        print(f"\r    {got / 1e6:6.1f} / {total / 1e6:.1f} MB")
    tmp.replace(dest)


def get_voices(check=False):
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for v in CONFIG.get("voices_to_install", []):
        onnx = VOICES_DIR / f"{v}.onnx"
        js = VOICES_DIR / f"{v}.onnx.json"
        if onnx.exists() and js.exists():
            say(f"  ✓ voice {v}", GREEN)
            ok += 1
            continue
        if check:
            say(f"  ✗ voice {v} missing", RED)
            continue
        code, name, q = v.split("-", 2)
        fam = code.split("_")[0]
        try:
            say(f"  ↓ voice {v}")
            download(VOICE_URL.format(fam=fam, code=code, name=name, q=q, ext=".onnx.json"), js)
            download(VOICE_URL.format(fam=fam, code=code, name=name, q=q, ext=".onnx"), onnx)
            say(f"  ✓ voice {v}", GREEN)
            ok += 1
        except Exception as e:  # noqa
            say(f"  ✗ voice {v}: {e}", RED)
    return ok > 0


def get_whisper(check=False):
    name = CONFIG["whisper_model"]
    dest = WHISPER_DIR / name
    if (dest / "model.bin").exists():
        say(f"  ✓ speech recognizer {name}", GREEN)
        return True
    if check:
        say(f"  ✗ speech recognizer {name} missing", RED)
        return False
    try:
        os.environ.pop("HF_HUB_OFFLINE", None)
        from faster_whisper.utils import download_model
        say(f"  ↓ speech recognizer {name} (this can take a few minutes)")
        dest.mkdir(parents=True, exist_ok=True)
        download_model(name, output_dir=str(dest))
        say(f"  ✓ speech recognizer {name}", GREEN)
        return True
    except Exception as e:  # noqa
        say(f"  ✗ speech recognizer {name}: {e}", RED)
        return False


def ollama_models():
    try:
        with urllib.request.urlopen(CONFIG["ollama_url"].rstrip("/") + "/api/tags", timeout=4) as r:
            return [m["name"] for m in json.loads(r.read())["models"]]
    except Exception:
        return None


def get_llms(check=False):
    if CONFIG.get("llm_backend") == "openai":
        say("  • using an OpenAI-style server (LM Studio etc.) — load a model there yourself.", DIM)
        return True
    have = ollama_models()
    if have is None:
        exe = shutil.which("ollama")
        if exe and not check:
            say("  … starting Ollama")
            subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(20):
                time.sleep(1)
                have = ollama_models()
                if have is not None:
                    break
    if have is None:
        say("  ✗ Ollama isn't running or isn't installed. Get it from https://ollama.com then run setup again.", RED)
        return False
    all_ok = True
    for m in CONFIG.get("models_to_install", [CONFIG["model"]]):
        tag = m if ":" in m else m + ":latest"
        if tag in have or m in have:
            say(f"  ✓ model {m}", GREEN)
            continue
        if check:
            say(f"  ✗ model {m} missing", RED)
            all_ok = False
            continue
        say(f"  ↓ model {m} (big download, please wait)")
        r = subprocess.run([shutil.which("ollama") or "ollama", "pull", m])
        if r.returncode == 0:
            say(f"  ✓ model {m}", GREEN)
        else:
            say(f"  ✗ model {m} failed to download", RED)
            all_ok = False
    return all_ok


def main():
    check = "--check" in sys.argv
    say(f"\n{CONFIG['app_name']} — {'checking' if check else 'getting'} offline parts\n")
    say("Voices (Piper):")
    v = get_voices(check)
    say("\nEars (Whisper):")
    w = get_whisper(check)
    say("\nBrain (local AI model):")
    b = get_llms(check)
    print()
    if v and w and b:
        say(f"All set. {CONFIG['app_name']} can now run with no internet.", GREEN)
        return 0
    say("Some parts are missing (see above). You can run this again any time.", RED)
    return 1


if __name__ == "__main__":
    sys.exit(main())
