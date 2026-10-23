#!/usr/bin/env python3
"""
Offline voice chat server — shared by Halo Converse (AMD 395) and Pip (Pi 500).

Pipeline:  headset mic -> browser -> /api/stt (faster-whisper)
           text -> /api/chat (local Ollama or LM Studio, streamed)
           reply sentences -> /api/tts (Piper) -> browser -> headset

Everything runs on this machine. After setup, no internet is needed.
"""
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
import webbrowser
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
SETTINGS_PATH = HERE / "data" / "settings.json"
MODELS_DIR = HERE / "models"
VOICES_DIR = MODELS_DIR / "voices"
WHISPER_DIR = MODELS_DIR / "whisper"

# Never let a library try to phone home while chatting.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


CONFIG = load_json(CONFIG_PATH, {})
DEFAULTS = {
    "app_name": "Voice Chat",
    "theme": "halo",
    "port": 8765,
    "llm_backend": "ollama",                 # "ollama" or "openai" (LM Studio, llama.cpp server)
    "ollama_url": "http://127.0.0.1:11434",
    "openai_url": "http://127.0.0.1:1234/v1",
    "model": "llama3.2:3b",
    "whisper_model": "base.en",
    "whisper_threads": 4,
    "whisper_beam": 1,
    "voice": "",
    "speech_speed": 1.0,
    "system_prompt": "You are a helpful voice assistant. Keep replies short and conversational.",
    "max_history": 20,
    "open_browser": True,
}
for k, v in DEFAULTS.items():
    CONFIG.setdefault(k, v)

# User-changed settings (from the gear menu) live in data/settings.json
USER_KEYS = ["model", "voice", "speech_speed", "system_prompt", "mode",
             "vad_threshold", "silence_ms", "speak_replies", "barge_in"]


def get_settings():
    s = {k: CONFIG.get(k) for k in USER_KEYS if k in CONFIG}
    s.update({"mode": "ptt", "vad_threshold": 0.02, "silence_ms": 900,
              "speak_replies": True, "barge_in": True})
    s.update({k: v for k, v in load_json(SETTINGS_PATH, {}).items() if k in USER_KEYS})
    for k in ("model", "voice", "speech_speed", "system_prompt"):
        s.setdefault(k, CONFIG.get(k))
    return s


def save_settings(new):
    cur = load_json(SETTINGS_PATH, {})
    for k, v in new.items():
        if k in USER_KEYS:
            cur[k] = v
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=2)
    return get_settings()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# --------------------------------------------------------------------------
# Speech to text (faster-whisper)
# --------------------------------------------------------------------------
_whisper = None
_whisper_err = None
_whisper_lock = threading.Lock()


def get_whisper():
    global _whisper, _whisper_err
    with _whisper_lock:
        if _whisper is not None or _whisper_err is not None:
            return _whisper
        try:
            from faster_whisper import WhisperModel
            name = CONFIG["whisper_model"]
            local = WHISPER_DIR / name
            src = str(local) if (local / "model.bin").exists() else name
            log(f"Loading speech recognizer ({name}) ...")
            _whisper = WhisperModel(
                src, device="cpu", compute_type="int8",
                cpu_threads=int(CONFIG["whisper_threads"]),
                download_root=str(WHISPER_DIR),
                local_files_only=(src != name) or os.environ.get("HF_HUB_OFFLINE") == "1",
            )
            log("Speech recognizer ready.")
        except ImportError as e:
            _whisper_err = f"Speech library missing ({e}). Run the installer again."
            log("Speech recognizer failed:", _whisper_err)
        except Exception as e:  # noqa
            if "LocalEntryNotFound" in type(e).__name__ or "outgoing traffic" in str(e):
                _whisper_err = (f"The '{CONFIG['whisper_model']}' speech recognizer isn't downloaded yet. "
                                "Run the installer again (needs internet once).")
            else:
                _whisper_err = f"{type(e).__name__}: {e}"
            log("Speech recognizer failed:", _whisper_err)
        return _whisper


def wav_to_float(data: bytes):
    import numpy as np
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError("Expected 16-bit WAV")
    a = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != 16000:  # simple linear resample
        n = int(len(a) * 16000 / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return a


# Things Whisper tends to "hear" in silence
HALLUCINATIONS = {"", "you", "thank you.", "thanks for watching!", "thank you for watching.",
                  "bye.", ".", "you.", "thank you", "[blank_audio]", "(silence)"}


def transcribe(data: bytes) -> str:
    model = get_whisper()
    if model is None:
        raise RuntimeError("Speech recognizer not available: " + str(_whisper_err))
    audio = wav_to_float(data)
    if len(audio) < 16000 * 0.3:
        return ""
    segs, _info = model.transcribe(
        audio, language="en", beam_size=int(CONFIG["whisper_beam"]),
        vad_filter=False, condition_on_previous_text=False, without_timestamps=True)
    text = " ".join(s.text.strip() for s in segs).strip()
    if text.lower() in HALLUCINATIONS:
        return ""
    return text


# --------------------------------------------------------------------------
# Text to speech (Piper)
# --------------------------------------------------------------------------
_voices = {}
_voice_lock = threading.Lock()
_piper_err = None


def list_voices():
    if not VOICES_DIR.exists():
        return []
    return sorted(p.name[:-5] for p in VOICES_DIR.glob("*.onnx")
                  if (VOICES_DIR / (p.name + ".json")).exists())


def get_voice(name):
    global _piper_err
    with _voice_lock:
        if name in _voices:
            return _voices[name]
        try:
            from piper import PiperVoice
            path = VOICES_DIR / f"{name}.onnx"
            log(f"Loading voice {name} ...")
            v = PiperVoice.load(str(path))
            _voices[name] = v
            return v
        except Exception as e:  # noqa
            _piper_err = f"{type(e).__name__}: {e}"
            log("Voice failed:", _piper_err)
            return None


def clean_for_speech(text: str) -> str:
    t = re.sub(r"```.*?```", " (code shown on screen) ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"[*_#>|~]+", "", t)
    t = re.sub(r"^\s*[-+•]\s+", "", t, flags=re.M)
    t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def synthesize(text: str, voice_name: str, speed: float) -> bytes:
    voices = list_voices()
    if not voices:
        raise RuntimeError("No Piper voices installed")
    if voice_name not in voices:
        voice_name = voices[0]
    v = get_voice(voice_name)
    if v is None:
        raise RuntimeError("Piper not available: " + str(_piper_err))
    text = clean_for_speech(text)
    if not text:
        return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        cfg = None
        try:
            from piper import SynthesisConfig
            cfg = SynthesisConfig(length_scale=1.0 / max(0.5, min(2.0, float(speed or 1))))
        except Exception:
            cfg = None
        if hasattr(v, "synthesize_wav"):
            v.synthesize_wav(text, wf, syn_config=cfg)
        else:  # older piper-tts
            v.synthesize(text, wf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Language model (Ollama or OpenAI-compatible like LM Studio)
# --------------------------------------------------------------------------
def http_json(url, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def list_models():
    try:
        if CONFIG["llm_backend"] == "openai":
            d = http_json(CONFIG["openai_url"].rstrip("/") + "/models")
            return [m["id"] for m in d.get("data", [])], None
        d = http_json(CONFIG["ollama_url"].rstrip("/") + "/api/tags")
        return sorted(m["name"] for m in d.get("models", [])), None
    except Exception as e:  # noqa
        return [], f"{type(e).__name__}: {e}"


def running_models():
    """Models Ollama currently has loaded in memory."""
    if CONFIG["llm_backend"] != "ollama":
        return []
    try:
        d = http_json(CONFIG["ollama_url"].rstrip("/") + "/api/ps", timeout=3)
        return [m["name"] for m in d.get("models", [])]
    except Exception:
        return []


def switch_model(new):
    """Save the new model, free the old one from memory and load the new one."""
    old = get_settings().get("model")
    models, err = list_models()
    if err:
        raise RuntimeError("Can't reach the AI model server. Is Ollama running?")
    if new not in models:
        raise RuntimeError(f"'{new}' isn't installed. Install it with: ollama pull {new}")
    save_settings({"model": new})
    secs = None
    if CONFIG["llm_backend"] == "ollama":
        base = CONFIG["ollama_url"].rstrip("/")
        if old and old != new:
            try:  # unload the old model so the two don't fight over RAM
                http_json(base + "/api/generate", {"model": old, "keep_alive": 0, "stream": False}, timeout=30)
            except Exception:
                pass
        t0 = time.time()
        http_json(base + "/api/generate",
                  {"model": new, "prompt": "", "keep_alive": "30m", "stream": False}, timeout=300)
        secs = round(time.time() - t0, 1)
    log(f"Switched model: {old} -> {new}" + (f" (loaded in {secs}s)" if secs is not None else ""))
    return {"model": new, "previous": old, "load_secs": secs}


def stream_chat(messages, model):
    """Yield text pieces from the local model."""
    if CONFIG["llm_backend"] == "openai":
        url = CONFIG["openai_url"].rstrip("/") + "/chat/completions"
        body = {"model": model, "messages": messages, "stream": True}
    else:
        url = CONFIG["ollama_url"].rstrip("/") + "/api/chat"
        body = {"model": model, "messages": messages, "stream": True,
                "keep_alive": "30m"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            if CONFIG["llm_backend"] == "openai":
                if not line.startswith("data:"):
                    continue
                line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    piece = json.loads(line)["choices"][0]["delta"].get("content") or ""
                except Exception:
                    piece = ""
            else:
                obj = json.loads(line)
                if obj.get("error"):
                    raise RuntimeError(obj["error"])
                piece = (obj.get("message") or {}).get("content") or ""
                if obj.get("done"):
                    if piece:
                        yield piece
                    break
            if piece:
                yield piece


class ThinkFilter:
    """Hide <think>...</think> reasoning from models like Qwen3 / DeepSeek-R1."""

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, s):
        self.buf += s
        out = ""
        while True:
            if self.inside:
                i = self.buf.find("</think>")
                if i < 0:
                    self.buf = self.buf[-8:]
                    return out
                self.buf = self.buf[i + 8:]
                self.inside = False
            else:
                i = self.buf.find("<think>")
                if i < 0:
                    keep = 0
                    for n in range(1, min(7, len(self.buf) + 1)):  # partial tag at end
                        if "<think>".startswith(self.buf[-n:]):
                            keep = n
                    out += self.buf[:len(self.buf) - keep]
                    self.buf = self.buf[len(self.buf) - keep:]
                    return out
                out += self.buf[:i]
                self.buf = self.buf[i + 7:]
                self.inside = True

    def flush(self):
        out = "" if self.inside else self.buf
        self.buf = ""
        return out


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "VoiceChat/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # Only answer requests made to this computer's own address
    def _local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send(403, {"error": "local only"})
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = (HERE / "web" / "index.html").read_text(encoding="utf-8")
            html = html.replace("{{APP_NAME}}", CONFIG["app_name"]).replace("{{THEME}}", CONFIG["theme"])
            return self._send(200, html, "text/html; charset=utf-8")
        if path.startswith("/static/"):
            f = (HERE / "web" / path[len("/static/"):]).resolve()
            if not str(f).startswith(str((HERE / "web").resolve())) or not f.is_file():
                return self._send(404, {"error": "not found"})
            ctype = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                     ".png": "image/png", ".ico": "image/x-icon"}.get(f.suffix, "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype)
        if path == "/api/status":
            models, merr = list_models()
            return self._send(200, {
                "app_name": CONFIG["app_name"], "theme": CONFIG["theme"],
                "backend": CONFIG["llm_backend"], "models": models, "llm_error": merr,
                "voices": list_voices(), "whisper_model": CONFIG["whisper_model"],
                "whisper_ready": _whisper is not None, "whisper_error": _whisper_err,
                "piper_error": _piper_err, "settings": get_settings(),
                "running": running_models(),
            })
        if path == "/api/settings":
            return self._send(200, get_settings())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._local_only():
            return
        path = self.path.split("?")[0]
        try:
            if path == "/api/stt":
                t0 = time.time()
                text = transcribe(self._body())
                log(f"Heard ({time.time() - t0:.1f}s): {text!r}")
                return self._send(200, {"text": text})
            if path == "/api/tts":
                req = json.loads(self._body() or b"{}")
                s = get_settings()
                wav = synthesize(req.get("text", ""), req.get("voice") or s["voice"],
                                 req.get("speed") or s["speech_speed"])
                return self._send(200, wav, "audio/wav")
            if path == "/api/settings":
                return self._send(200, save_settings(json.loads(self._body() or b"{}")))
            if path == "/api/model":
                req = json.loads(self._body() or b"{}")
                return self._send(200, switch_model(req.get("model", "")))
            if path == "/api/chat":
                return self._chat(json.loads(self._body() or b"{}"))
        except Exception as e:  # noqa
            log("Error:", path, e)
            msg = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
            return self._send(500, {"error": msg})
        return self._send(404, {"error": "not found"})

    def _chat(self, req):
        s = get_settings()
        model = req.get("model") or s["model"]
        history = req.get("messages") or []
        history = history[-int(CONFIG["max_history"]):]
        system = (s.get("system_prompt") or CONFIG["system_prompt"]).strip()
        system += ("\n\nYour words are read aloud through a headset, so answer in plain spoken "
                   "sentences: no markdown, no bullet lists, no emoji, unless the user asks for "
                   "code or a list on screen.")
        messages = [{"role": "system", "content": system}] + [
            {"role": m["role"], "content": m["content"]} for m in history
            if m.get("role") in ("user", "assistant") and m.get("content")]

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()

        flt = ThinkFilter()
        t0 = time.time()
        try:
            for piece in stream_chat(messages, model):
                out = flt.feed(piece)
                if out:
                    emit({"t": out})
            tail = flt.flush()
            if tail:
                emit({"t": tail})
            emit({"done": True, "model": model, "secs": round(time.time() - t0, 1)})
        except (BrokenPipeError, ConnectionResetError):
            log("Reply stopped (interrupted).")
        except urllib.error.URLError as e:
            emit({"error": f"Can't reach the AI model. Is Ollama running? ({e.reason})"})
        except Exception as e:  # noqa
            try:
                emit({"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass


def open_app_window(url):
    """Open in a clean app-style window if Chrome/Edge/Chromium is present."""
    candidates = []
    if sys.platform.startswith("win"):
        pf = [os.environ.get("ProgramFiles", r"C:\Program Files"),
              os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
              os.environ.get("LOCALAPPDATA", "")]
        for base in pf:
            candidates += [os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                           os.path.join(base, r"Google\Chrome\Application\chrome.exe")]
    else:
        for n in ("chromium-browser", "chromium", "google-chrome", "microsoft-edge"):
            p = shutil.which(n)
            if p:
                candidates.append(p)
    for c in candidates:
        if c and os.path.exists(c):
            try:
                subprocess.Popen([c, f"--app={url}", "--window-size=1100,820"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass
    webbrowser.open(url)


def main():
    port = int(CONFIG["port"])
    url = f"http://127.0.0.1:{port}/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        log(f"{CONFIG['app_name']} already seems to be running - opening it.")
        open_app_window(url)
        return
    log(f"{CONFIG['app_name']} running at {url}")
    log("Close this window to quit.")
    threading.Thread(target=get_whisper, daemon=True).start()  # warm up
    if CONFIG.get("open_browser", True) and "--no-browser" not in sys.argv:
        threading.Timer(0.8, open_app_window, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
