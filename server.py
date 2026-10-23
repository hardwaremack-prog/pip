#!/usr/bin/env python3
"""
Offline voice chat server — shared by Halo Converse (AMD 395) and Pip (Pi 500).

Pipeline:  headset mic -> browser -> /api/stt (faster-whisper)
           text -> /api/chat (local Ollama or LM Studio, streamed)
           reply sentences -> /api/tts (Piper) -> browser -> headset

Everything runs on this machine. After setup, no internet is needed.
"""
import io
import ipaddress
import json
import socket
import ssl
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
try:
    VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    VERSION = "1.0.0"
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
    "share_on_network": False,   # let other devices on the home network use it
    "share_port": 8443,          # HTTPS port for other devices (mic needs HTTPS)
    "unload_on_switch": True,    # free the old model's RAM when switching
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


_stt_lock = threading.Lock()
_tts_lock = threading.Lock()


def transcribe(data: bytes) -> str:
    with _stt_lock:
        return _transcribe(data)


def _transcribe(data: bytes) -> str:
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
    with _tts_lock, wave.open(buf, "wb") as wf:
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


def switch_model(new, old=None, is_host=True):
    """Load the new model (and, on the host, save it and free the old one)."""
    old = old or get_settings().get("model")
    models, err = list_models()
    if err:
        raise RuntimeError("Can't reach the AI model server. Is Ollama running?")
    if new not in models:
        raise RuntimeError(f"'{new}' isn't installed. Install it with: ollama pull {new}")
    if is_host:
        save_settings({"model": new})
    secs = None
    if CONFIG["llm_backend"] == "ollama":
        base = CONFIG["ollama_url"].rstrip("/")
        if is_host and CONFIG.get("unload_on_switch", True) and old and old != new:
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
        if n > 20_000_000:
            raise RuntimeError("Request too large")
        return self.rfile.read(n) if n else b""

    @property
    def is_host(self):
        try:
            return ipaddress.ip_address(self.client_address[0].split("%")[0]).is_loopback
        except ValueError:
            return False

    # Answer this computer, and (when sharing) other devices on the home network only
    def _local_only(self):
        raw = self.headers.get("Host") or ""
        if raw.startswith("["):                       # [ipv6]:port
            host = raw[1:raw.find("]")]
        else:
            host = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
        host = host.lower()
        if self.is_host:
            ok = host in ("127.0.0.1", "localhost", "::1") or host in NET["names"]
        else:
            ok = CONFIG.get("share_on_network") and is_private(self.client_address[0]) and (
                host in NET["names"] or is_ip(host))
        if not ok:
            self._send(403, {"error": "Not allowed from here"})
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
                "is_host": self.is_host,
                "share_urls": NET["urls"] if self.is_host else [],
                "version": VERSION,
                "can_update": bool(CONFIG.get("update_repo")) and os.name != "nt",
                "share_error": NET["error"] if self.is_host else None,
            })
        if path == "/api/settings":
            return self._send(200, get_settings())
        if path == "/api/update":
            return self._send(200, update_info())
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
                if not self.is_host:
                    return self._send(403, {"error": "Only the host computer can change shared settings"})
                return self._send(200, save_settings(json.loads(self._body() or b"{}")))
            if path == "/api/update/check":
                check_update(force=True)
                return self._send(200, update_info())
            if path == "/api/update/run":
                if not self.is_host:
                    return self._send(403, {"error": "Only the main computer can update"})
                run_update()
                return self._send(200, update_info())
            if path == "/api/model":
                req = json.loads(self._body() or b"{}")
                return self._send(200, switch_model(req.get("model", ""), req.get("previous"), self.is_host))
            if path == "/api/chat":
                return self._chat(json.loads(self._body() or b"{}"))
        except (BrokenPipeError, ConnectionResetError):
            return
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


# --------------------------------------------------------------------------
# Sharing on the home network
# --------------------------------------------------------------------------
NET = {"names": set(), "urls": [], "error": None}


def is_ip(h):
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def is_private(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
        if getattr(a, "ipv4_mapped", None):
            a = a.ipv4_mapped
        return a.is_private or a.is_loopback or a.is_link_local
    except ValueError:
        return False


def local_ips():
    ips = set()
    try:  # the address used to reach the router (no traffic is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127.") and is_private(i))


def make_cert(cert_path: Path, key_path: Path, names, ips):
    """Create a self-signed certificate so other devices can use the microphone."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import datetime
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CONFIG["app_name"])])
    san = [x509.DNSName(n) for n in sorted(names)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (cert_path.parent / "cert_ips.json").write_text(json.dumps(ips))


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        if isinstance(e, (ssl.SSLError, ConnectionError, TimeoutError, OSError)):
            return  # e.g. a browser closing the connection at the certificate warning
        super().handle_error(request, client_address)


def start_network_server():
    host = socket.gethostname().lower()
    NET["names"] = {host, host + ".local", "localhost"}
    ips = local_ips()
    port = int(CONFIG["share_port"])
    data = HERE / "data"
    cert, key = data / "cert.pem", data / "key.pem"
    try:
        old_ips = json.loads((data / "cert_ips.json").read_text()) if cert.exists() else None
        if not cert.exists() or not key.exists() or set(old_ips or []) != set(ips):
            log("Creating a security certificate for network use ...")
            make_cert(cert, key, NET["names"], ips)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        srv = QuietServer(("0.0.0.0", port), Handler)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    except ImportError:
        NET["error"] = "Network sharing needs the 'cryptography' package. Run the installer again."
        log(NET["error"])
        return
    except OSError as e:
        NET["error"] = f"Couldn't open port {port} for the network ({e})."
        log(NET["error"])
        return
    NET["urls"] = [f"https://{i}:{port}/" for i in ips] + [f"https://{host}.local:{port}/"]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("Other devices on your network can use it at:")
    for u in NET["urls"]:
        log("   " + u)
    log("(The first visit shows a certificate warning: choose Advanced -> Continue.)")


# --------------------------------------------------------------------------
# Updates from GitHub
# --------------------------------------------------------------------------
UPDATE = {"latest": None, "notes": "", "checked": 0.0, "error": None, "running": False}
UPDATE_LOG = HERE / "data" / "update.log"


def vtuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    except Exception:
        return (0,)


def notes_since(changes, current):
    """The parts of CHANGES.txt that are newer than this copy."""
    out = []
    for block in re.split(r"\n(?=\d+\.\d+\.\d+)", changes.strip()):
        m = re.match(r"(\d+\.\d+\.\d+)", block)
        if m and vtuple(m.group(1)) <= vtuple(current):
            break
        out.append(block.strip())
    return "\n\n".join(out)


def check_update(force=False):
    repo = CONFIG.get("update_repo")
    if not repo or (not force and time.time() - UPDATE["checked"] < 3600):
        return
    UPDATE["checked"] = time.time()
    base = f"https://raw.githubusercontent.com/{repo}/main/"
    try:
        stamp = f"?t={int(time.time())}"
        with urllib.request.urlopen(base + "VERSION" + stamp, timeout=8) as r:
            latest = r.read().decode().strip()
        notes = ""
        try:
            with urllib.request.urlopen(base + "CHANGES.txt" + stamp, timeout=8) as r:
                notes = notes_since(r.read().decode(), VERSION)
        except Exception:
            pass
        UPDATE.update(latest=latest, notes=notes, error=None)
        if vtuple(latest) > vtuple(VERSION):
            log(f"Update available: {VERSION} -> {latest}")
    except Exception:
        UPDATE["error"] = "Couldn't reach GitHub. Updates need the internet."


def update_info():
    lines = []
    if UPDATE_LOG.exists() and (UPDATE["running"] or time.time() - UPDATE_LOG.stat().st_mtime < 600):
        lines = UPDATE_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-40:]
    latest = UPDATE["latest"]
    return {"version": VERSION, "latest": latest,
            "available": bool(latest) and vtuple(latest) > vtuple(VERSION),
            "notes": UPDATE["notes"], "error": UPDATE["error"],
            "running": UPDATE["running"], "log": lines,
            "supported": bool(CONFIG.get("update_repo")) and os.name != "nt"}


def run_update():
    if UPDATE["running"]:
        return
    script = HERE / "update.sh"
    if os.name == "nt" or not script.exists():
        raise RuntimeError("Updating from inside the app isn't available here.")
    UPDATE["running"] = True
    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    logf = open(UPDATE_LOG, "w", encoding="utf-8")
    log("Updating from GitHub ...")
    # New session so the updater survives when it restarts this server
    subprocess.Popen(["bash", str(script), "--restart"], cwd=str(HERE), stdout=logf,
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)

    def watch():
        time.sleep(600)
        UPDATE["running"] = False  # gave up waiting; normally we've been restarted by now
    threading.Thread(target=watch, daemon=True).start()


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
        httpd = QuietServer(("127.0.0.1", port), Handler)
    except OSError:
        log(f"{CONFIG['app_name']} already seems to be running - opening it.")
        open_app_window(url)
        return
    log(f"{CONFIG['app_name']} {VERSION} running at {url}")
    if CONFIG.get("update_repo"):
        threading.Thread(target=check_update, daemon=True).start()
    if CONFIG.get("share_on_network"):
        start_network_server()
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
