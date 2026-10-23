/* Offline voice chat front end — Halo Converse / Pip */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const APP = document.title;

  // ---------------- State ----------------
  let settings = {};
  let status = {};
  let messages = loadHistory();
  let state = "off";            // off | idle | listening | transcribing | thinking | speaking
  let ctx = null, micStream = null, micNode = null, micSource = null;
  let outAnalyser = null, outGain = null;
  let recording = false, recChunks = [], recSamples = 0;
  let preRoll = [], preRollMax = 0;
  let pttHeld = false;
  let hf = { inSpeech: false, loud: 0, silentMs: 0, speechMs: 0, bargeLoud: 0 };
  let chatAbort = null;
  let ttsQueue = [];            // [{text, promise}]
  let playing = null;           // current AudioBufferSourceNode
  let speakingChain = Promise.resolve();
  let replyId = 0;
  let browserTTS = false;       // fall back to built-in voices if Piper is missing

  // ---------------- Helpers ----------------
  function setState(s, label) {
    state = s;
    const av = $("avatar");
    av.className = "avatar state-" + (s === "transcribing" ? "thinking" : s);
    const labels = {
      off: "Microphone off", idle: settings.mode === "hf" ? "Listening for you…" : "Ready",
      listening: "Listening…", transcribing: "Got it…", thinking: "Thinking…", speaking: "Speaking…",
    };
    $("stateLabel").textContent = label || labels[s] || "";
    $("talkBtn").classList.toggle("active", s === "listening");
  }

  function esc(s) { return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  function render(md) {
    let h = esc(md);
    h = h.replace(/```(\w*)\n?([\s\S]*?)```\n?/g, (_, l, c) => `<pre><code>${c.replace(/\n$/, "")}</code></pre>`);
    h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    return h;
  }

  function loadHistory() {
    try { return JSON.parse(localStorage.getItem("vc_history_" + document.title) || "[]"); } catch { return []; }
  }
  function saveHistory() {
    try { localStorage.setItem("vc_history_" + document.title, JSON.stringify(messages.slice(-60))); } catch {}
  }
  function lsGet(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

  function addBubble(role, text, cls = "") {
    $("emptyNote").style.display = "none";
    const d = document.createElement("div");
    d.className = `msg ${role} ${cls}`;
    d.innerHTML = render(text);
    $("transcript").appendChild(d);
    scrollDown();
    return d;
  }
  function scrollDown() { const t = $("transcript"); t.scrollTop = t.scrollHeight; }
  function showError(text) { addBubble("error", text); }

  function renderHistory() {
    $("transcript").querySelectorAll(".msg").forEach((n) => n.remove());
    messages.forEach((m) => addBubble(m.role, m.content));
    $("emptyNote").style.display = messages.length ? "none" : "";
  }

  async function api(path, body) {
    const r = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r.json();
  }
  const saveSetting = (patch) => { Object.assign(settings, patch); api("/api/settings", patch).catch(() => {}); };

  // ---------------- Status / settings UI ----------------
  async function refreshStatus(first) {
    try { status = await api("/api/status"); } catch { return; }
    if (first) settings = status.settings;
    const sel = $("modelSel");
    if (!switching) {
      const cur = settings.model;
      const models = status.models.length ? status.models : [cur];
      sel.innerHTML = models.map((m) => `<option ${m === cur ? "selected" : ""}>${esc(m)}</option>`).join("");
      if (!models.includes(cur) && status.models.length) { sel.value = status.models[0]; saveSetting({ model: sel.value }); }
    }

    const dot = (id, cls, tip) => { const e = $(id); e.className = "dot " + cls; e.title = tip; };
    const loaded = (status.running || []).length ? ` · in memory now: ${status.running.join(", ")}` : "";
    dot("hBrain", status.llm_error ? "bad" : (status.models.length ? "ok" : "wait"),
      status.llm_error ? "AI model not reachable — is Ollama running?" : `Using ${settings.model}${loaded}`);
    $("hBrain").textContent = status.llm_error ? "Brain" : "Brain: " + shortModel(settings.model);
    dot("hEars", status.whisper_error ? "bad" : (status.whisper_ready ? "ok" : "wait"),
      status.whisper_error || (status.whisper_ready ? "Speech recognition ready (" + status.whisper_model + ")" : "Loading speech recognition…"));
    browserTTS = !status.voices.length || !!status.piper_error;
    dot("hVoice", status.voices.length && !status.piper_error ? "ok" : "wait",
      status.voices.length ? (status.piper_error || "Piper voice ready") : "No Piper voice found — using the built-in voice");

    const vs = $("voiceSel");
    vs.innerHTML = (status.voices.length ? status.voices : ["(built-in)"]).map((v) =>
      `<option value="${esc(v)}" ${v === settings.voice ? "selected" : ""}>${esc(prettyVoice(v))}</option>`).join("");
    $("about").textContent = `Brain: ${status.backend === "openai" ? "LM Studio / OpenAI-style server" : "Ollama"} · Ears: Whisper ${status.whisper_model} · Voice: Piper. All offline.`;
    if (!status.whisper_ready && !status.whisper_error) setTimeout(() => refreshStatus(false), 2500);
  }
  // "llama3.2:1b" -> "1b",  "gemma3:27b" -> "gemma3 27b"
  function shortModel(m) {
    if (!m) return "?";
    const [name, tag] = m.split(":");
    if (/^llama3\.2$/i.test(name) && tag) return tag.toUpperCase();
    return tag && tag !== "latest" ? `${name} ${tag}` : name;
  }
  // how the voice should say a model name
  function spokenModel(m) {
    return m.replace(/:latest$/, "").replace(/[:_\/-]+/g, " ")
      .replace(/([a-z])(\d)/gi, "$1 $2").replace(/(\d)b\b/gi, "$1 B");
  }

  // Switch models and tell the user (on screen and out loud) when it's done
  let switching = false;
  async function switchModel(model) {
    const sel = $("modelSel");
    const before = settings.model;
    if (!model || model === before) return;
    stopReply();
    switching = true;
    sel.disabled = true;
    setState("thinking", `Switching to ${model}…`);
    try {
      const r = await api("/api/model", { model });
      settings.model = r.model;
      const secs = r.load_secs != null ? ` (ready in ${r.load_secs}s)` : "";
      addNote(`Switched brain: ${before} → ${r.model}${secs}`);
      setState("idle");
      if (ctx && settings.speak_replies !== false) queueSpeech(`Okay. I'm now using ${spokenModel(r.model)}.`, replyId);
    } catch (err) {
      sel.value = before;
      showError(`Couldn't switch to ${model}: ${err.message}`);
      setState(ctx ? "idle" : "off");
    } finally {
      switching = false;
      sel.disabled = false;
      refreshStatus(false);
    }
  }
  function addNote(text) {
    const d = addBubble("note", text);
    return d;
  }

  function prettyVoice(v) {
    const m = v.match(/^([a-z]{2})_([A-Z]{2})-(.+)-(\w+)$/);
    if (!m) return v;
    const name = m[3].replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    return `${name} (${m[2]}, ${m[4]})`;
  }

  function applySettingsToUI() {
    $("speed").value = settings.speech_speed || 1; $("speedVal").textContent = (+$("speed").value).toFixed(2) + "×";
    $("speak").checked = settings.speak_replies !== false;
    $("barge").checked = settings.barge_in !== false;
    $("thr").value = settings.vad_threshold || 0.02; $("thrVal").textContent = (+$("thr").value).toFixed(3);
    $("sil").value = settings.silence_ms || 900; $("silVal").textContent = (($("sil").value) / 1000).toFixed(1) + " s";
    $("sysPrompt").value = settings.system_prompt || "";
    updateThrMarker();
    setMode(settings.mode || "ptt", false);
  }
  function updateThrMarker() { $("micThr").style.left = Math.min(100, lvlToPct(+$("thr").value)) + "%"; }
  const lvlToPct = (rms) => Math.min(100, Math.sqrt(rms / 0.2) * 100);

  function setMode(mode, save = true) {
    settings.mode = mode;
    const ptt = mode !== "hf";
    $("modePtt").setAttribute("aria-checked", ptt);
    $("modeHf").setAttribute("aria-checked", !ptt);
    const btn = ptt ? $("modePtt") : $("modeHf");
    const pill = document.querySelector(".mode-pill");
    pill.style.width = btn.offsetWidth + "px";
    pill.style.transform = `translateX(${btn.offsetLeft - 4}px)`;
    $("talkText").textContent = ptt ? "Hold to talk" : "Hands-free on";
    $("talkBtn").classList.toggle("hf-on", !ptt);
    $("hint").innerHTML = ptt
      ? "Hold <kbd>Space</kbd> or the button while you talk. <kbd>Esc</kbd> stops a reply."
      : `Just talk — ${esc(APP)} answers when you pause. Hold <kbd>Space</kbd> to talk anyway. <kbd>Esc</kbd> stops a reply.`;
    hf.inSpeech = false; hf.loud = 0;
    if (recording && !pttHeld) stopRecording(false);
    if (save) saveSetting({ mode });
    if (state === "idle") setState("idle");
  }

  // ---------------- Audio setup ----------------
  async function startAudio() {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    outGain = ctx.createGain();
    outAnalyser = ctx.createAnalyser(); outAnalyser.fftSize = 512;
    outGain.connect(outAnalyser); outAnalyser.connect(ctx.destination);
    await openMic(lsGet("vc_mic", ""));
    await listDevices();
    const out = lsGet("vc_out", "");
    if (out && ctx.setSinkId) ctx.setSinkId(out).catch(() => {});
    requestAnimationFrame(animate);
  }

  async function openMic(deviceId) {
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    if (micSource) micSource.disconnect();
    if (micNode) micNode.disconnect();
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1,
      },
    });
    micSource = ctx.createMediaStreamSource(micStream);
    micNode = ctx.createScriptProcessor(2048, 1, 1);
    micNode.onaudioprocess = onMicFrame;
    micSource.connect(micNode);
    micNode.connect(ctx.destination); // required for processing; outputs silence
    preRollMax = Math.round(ctx.sampleRate * 0.35);
  }

  async function listDevices() {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const ins = devs.filter((d) => d.kind === "audioinput");
    const outs = devs.filter((d) => d.kind === "audiooutput");
    const curIn = micStream.getAudioTracks()[0]?.getSettings().deviceId;
    $("micSel").innerHTML = ins.map((d, i) =>
      `<option value="${d.deviceId}" ${d.deviceId === curIn ? "selected" : ""}>${esc(d.label || "Microphone " + (i + 1))}</option>`).join("");
    if (ctx.setSinkId && outs.length) {
      const curOut = lsGet("vc_out", "");
      $("outSel").innerHTML = outs.map((d, i) =>
        `<option value="${d.deviceId}" ${d.deviceId === curOut ? "selected" : ""}>${esc(d.label || "Output " + (i + 1))}</option>`).join("");
    } else {
      $("outRow").style.display = "none";
    }
  }

  // ---------------- Microphone frames ----------------
  let micLevel = 0;
  function onMicFrame(e) {
    const data = e.inputBuffer.getChannelData(0);
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
    const rms = Math.sqrt(sum / data.length);
    micLevel = micLevel * 0.6 + rms * 0.4;
    const frameMs = (data.length / ctx.sampleRate) * 1000;
    const copy = new Float32Array(data);

    if (recording) {
      recChunks.push(copy); recSamples += copy.length;
      if (recSamples > ctx.sampleRate * 45) stopRecording(true); // safety cap
    } else {
      preRoll.push(copy);
      let n = preRoll.reduce((a, c) => a + c.length, 0);
      while (n > preRollMax && preRoll.length > 1) n -= preRoll.shift().length;
    }

    if (settings.mode !== "hf" || pttHeld) return;
    const thr = +settings.vad_threshold || 0.02;

    // Talking over a reply interrupts it (best with a headset)
    if (state === "speaking" || state === "thinking") {
      if (settings.barge_in !== false && rms > thr * 2.2) {
        if (++hf.bargeLoud >= 4) { hf.bargeLoud = 0; stopReply(); startRecording(true); hf.inSpeech = true; hf.silentMs = 0; hf.speechMs = 0; }
      } else hf.bargeLoud = 0;
      return;
    }
    if (state !== "idle" && state !== "listening") return;

    if (!hf.inSpeech) {
      hf.loud = rms > thr ? hf.loud + 1 : 0;
      if (hf.loud >= 2) { hf.inSpeech = true; hf.silentMs = 0; hf.speechMs = 0; startRecording(true); }
    } else {
      hf.speechMs += frameMs;
      if (rms < thr * 0.75) hf.silentMs += frameMs; else hf.silentMs = 0;
      if (hf.silentMs >= (+settings.silence_ms || 900)) {
        hf.inSpeech = false; hf.loud = 0;
        stopRecording(hf.speechMs - hf.silentMs > 350);
      }
    }
  }

  function startRecording(withPreRoll) {
    if (!ctx) return;
    recChunks = withPreRoll ? preRoll.slice() : [];
    recSamples = recChunks.reduce((a, c) => a + c.length, 0);
    preRoll = [];
    recording = true;
    setState("listening");
  }

  function stopRecording(send) {
    if (!recording) return;
    recording = false;
    const chunks = recChunks; recChunks = [];
    const total = chunks.reduce((a, c) => a + c.length, 0);
    if (!send || total < ctx.sampleRate * 0.35) { setState("idle"); return; }
    const wav = encodeWav(chunks, ctx.sampleRate);
    transcribeAndSend(wav);
  }

  function encodeWav(chunks, rate) {
    const target = 16000;
    const total = chunks.reduce((a, c) => a + c.length, 0);
    const all = new Float32Array(total);
    let o = 0; for (const c of chunks) { all.set(c, o); o += c.length; }
    const ratio = rate / target;
    const n = Math.floor(total / ratio);
    const pcm = new Int16Array(n);
    for (let i = 0; i < n; i++) {
      // average the samples that fold into this one (cheap low-pass)
      const s = Math.floor(i * ratio), e = Math.min(total, Math.floor((i + 1) * ratio));
      let acc = 0; for (let j = s; j < e; j++) acc += all[j];
      const v = Math.max(-1, Math.min(1, acc / Math.max(1, e - s)));
      pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    const buf = new ArrayBuffer(44 + pcm.length * 2);
    const dv = new DataView(buf);
    const w = (p, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(p + i, s.charCodeAt(i)); };
    w(0, "RIFF"); dv.setUint32(4, 36 + pcm.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
    dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
    dv.setUint32(24, target, true); dv.setUint32(28, target * 2, true);
    dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); w(36, "data"); dv.setUint32(40, pcm.length * 2, true);
    new Int16Array(buf, 44).set(pcm);
    return new Blob([buf], { type: "audio/wav" });
  }

  async function transcribeAndSend(wav) {
    setState("transcribing");
    try {
      const r = await fetch("/api/stt", { method: "POST", body: wav });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "Speech recognition failed");
      const text = (j.text || "").trim();
      if (!text) { setState("idle", "Didn't catch that"); setTimeout(() => state === "idle" && setState("idle"), 1600); return; }
      sendMessage(text);
    } catch (err) {
      showError("Couldn't understand the audio: " + err.message);
      setState("idle");
    }
  }

  // ---------------- Chat + speech ----------------
  async function sendMessage(text) {
    stopReply();
    const myId = ++replyId;
    messages.push({ role: "user", content: text });
    addBubble("user", text);
    saveHistory();
    setState("thinking");

    const bubble = addBubble("assistant", "", "typing");
    let full = "", pending = "", usedModel = settings.model;
    const t0 = performance.now();
    chatAbort = new AbortController();
    try {
      const r = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages, model: settings.model }), signal: chatAbort.signal,
      });
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
          if (!line.trim()) continue;
          const obj = JSON.parse(line);
          if (obj.error) throw new Error(obj.error);
          if (obj.model) usedModel = obj.model;
          if (obj.t) {
            full += obj.t; pending += obj.t;
            bubble.innerHTML = render(full.replace(/^\s+/, ""));
            scrollDown();
            pending = flushSentences(pending, false, myId);
          }
        }
      }
      flushSentences(pending, true, myId);
      bubble.classList.remove("typing");
      const secs = ((performance.now() - t0) / 1000).toFixed(1);
      bubble.insertAdjacentHTML("beforeend", `<span class="meta">answered by ${esc(usedModel)} · ${secs}s</span>`);
      full = full.trim();
      if (full) { messages.push({ role: "assistant", content: full }); saveHistory(); }
      if (!speakingQueued(myId)) setState("idle");
    } catch (err) {
      bubble.classList.remove("typing");
      if (err.name === "AbortError") {
        if (full.trim()) { messages.push({ role: "assistant", content: full.trim() + " …" }); saveHistory(); }
        else bubble.remove();
        return;
      }
      if (!full) bubble.remove();
      showError(err.message);
      setState("idle");
    } finally {
      if (myId === replyId) chatAbort = null;
    }
  }

  // Split streamed text into sentences so speech can start right away
  function flushSentences(text, final, id) {
    if (settings.speak_replies === false) return "";
    // finished code blocks are shown, not read out
    let out = text.replace(/```[\s\S]*?```/g, " I put that on the screen for you. ");
    const re = /([\s\S]*?[.!?…:;])(\s+|$)/y;
    for (;;) {
      if (/```/.test(out) && !final) return out; // still inside a code block
      re.lastIndex = 0;
      const m = re.exec(out);
      if (!m || (!final && m[2] === "")) break;
      if (m[1].trim().length < 12 && !final && out.length - m[0].length < 1) break;
      queueSpeech(m[1], id);
      out = out.slice(m[0].length);
    }
    // break very long sentences at a comma
    if (!final && out.length > 180) {
      const c = out.lastIndexOf(", ", 160);
      if (c > 40) { queueSpeech(out.slice(0, c + 1), id); out = out.slice(c + 2); }
    }
    if (final && out.trim()) { queueSpeech(out, id); out = ""; }
    return out;
  }

  let queuedFor = new Map();
  const speakingQueued = (id) => (queuedFor.get(id) || 0) > 0;

  function queueSpeech(text, id) {
    if (!text.replace(/[^\p{L}\p{N}]/gu, "")) return;
    queuedFor.set(id, (queuedFor.get(id) || 0) + 1);
    const fetchAudio = browserTTS ? Promise.resolve(null) : fetch("/api/tts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: settings.voice, speed: settings.speech_speed }),
    }).then(async (r) => {
      if (!r.ok) throw new Error("tts");
      const ab = await r.arrayBuffer();
      return ab.byteLength > 44 ? ctx.decodeAudioData(ab) : null;
    }).catch(() => "fallback");

    speakingChain = speakingChain.then(async () => {
      if (id !== replyId) return;
      const audio = await fetchAudio;
      if (id !== replyId) return;
      setState("speaking");
      if (audio === "fallback" || (browserTTS && audio === null)) await speakBrowser(text);
      else if (audio) await playBuffer(audio);
    }).finally(() => {
      const left = (queuedFor.get(id) || 1) - 1;
      queuedFor.set(id, left);
      if (left <= 0 && id === replyId && !chatAbort && state === "speaking") setState("idle");
    });
  }

  function playBuffer(buffer) {
    return new Promise((resolve) => {
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(outGain);
      src.onended = () => { if (playing === src) playing = null; resolve(); };
      playing = src;
      src.start();
    });
  }

  function speakBrowser(text) {
    return new Promise((resolve) => {
      if (!("speechSynthesis" in window)) return resolve();
      const u = new SpeechSynthesisUtterance(text.replace(/[*_#`>]/g, ""));
      u.rate = +settings.speech_speed || 1;
      u.onend = u.onerror = resolve;
      speechSynthesis.speak(u);
    });
  }

  function stopReply() {
    replyId++;
    if (chatAbort) { chatAbort.abort(); chatAbort = null; }
    if (playing) { try { playing.stop(); } catch {} playing = null; }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    speakingChain = Promise.resolve();
    if (state === "speaking" || state === "thinking") setState("idle");
  }

  // ---------------- Animation ----------------
  const outData = new Uint8Array(256);
  function animate() {
    let lvl = 0;
    if (state === "speaking" && outAnalyser) {
      outAnalyser.getByteTimeDomainData(outData);
      let s = 0; for (let i = 0; i < outData.length; i++) { const v = (outData[i] - 128) / 128; s += v * v; }
      lvl = Math.min(1, Math.sqrt(s / outData.length) * 4);
    } else if (state === "listening" || state === "idle") {
      lvl = Math.min(1, micLevel * 9);
    }
    $("avatar").style.setProperty("--lvl", lvl.toFixed(3));
    $("micMeter").style.width = lvlToPct(micLevel) + "%";
    requestAnimationFrame(animate);
  }

  // ---------------- Push to talk controls ----------------
  function pttDown() {
    if (!ctx || pttHeld) return;
    pttHeld = true;
    if (state === "speaking" || state === "thinking") stopReply();
    hf.inSpeech = false;
    startRecording(true);
  }
  function pttUp() {
    if (!pttHeld) return;
    pttHeld = false;
    stopRecording(true);
  }

  const typing = () => ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  window.addEventListener("keydown", (e) => {
    if (e.code === "Escape") { stopReply(); if (recording) { pttHeld = false; stopRecording(false); } closeDrawer(); }
    if (e.code === "Space" && !typing() && !e.repeat) { e.preventDefault(); pttDown(); }
  });
  window.addEventListener("keyup", (e) => { if (e.code === "Space" && !typing()) { e.preventDefault(); pttUp(); } });
  window.addEventListener("blur", pttUp);

  const tb = $("talkBtn");
  tb.addEventListener("pointerdown", (e) => { e.preventDefault(); tb.setPointerCapture(e.pointerId); pttDown(); });
  tb.addEventListener("pointerup", pttUp);
  tb.addEventListener("pointercancel", pttUp);

  $("modePtt").onclick = () => setMode("ptt");
  $("modeHf").onclick = () => setMode("hf");
  $("stopBtn").onclick = () => stopReply();
  $("typeForm").onsubmit = (e) => {
    e.preventDefault();
    const t = $("typeBox").value.trim();
    if (!t) return;
    $("typeBox").value = "";
    $("typeBox").blur();
    sendMessage(t);
  };
  $("newChat").onclick = () => { stopReply(); messages = []; saveHistory(); renderHistory(); setState(ctx ? "idle" : "off"); };
  $("modelSel").onchange = (e) => switchModel(e.target.value);

  // ---------------- Drawer ----------------
  const openDrawer = () => { $("drawer").classList.add("open"); $("scrim").classList.add("show"); };
  function closeDrawer() { $("drawer").classList.remove("open"); $("scrim").classList.remove("show"); }
  $("gear").onclick = openDrawer;
  $("closeDrawer").onclick = closeDrawer;
  $("scrim").onclick = closeDrawer;
  $("micSel").onchange = async (e) => { lsSet("vc_mic", e.target.value); await openMic(e.target.value); };
  $("outSel").onchange = (e) => { lsSet("vc_out", e.target.value); ctx.setSinkId?.(e.target.value).catch(() => {}); };
  $("voiceSel").onchange = (e) => { saveSetting({ voice: e.target.value }); sample(); };
  $("speed").oninput = (e) => { $("speedVal").textContent = (+e.target.value).toFixed(2) + "×"; };
  $("speed").onchange = (e) => { saveSetting({ speech_speed: +e.target.value }); sample(); };
  $("speak").onchange = (e) => saveSetting({ speak_replies: e.target.checked });
  $("barge").onchange = (e) => saveSetting({ barge_in: e.target.checked });
  $("thr").oninput = (e) => { $("thrVal").textContent = (+e.target.value).toFixed(3); settings.vad_threshold = +e.target.value; updateThrMarker(); };
  $("thr").onchange = (e) => saveSetting({ vad_threshold: +e.target.value });
  $("sil").oninput = (e) => { $("silVal").textContent = (e.target.value / 1000).toFixed(1) + " s"; };
  $("sil").onchange = (e) => saveSetting({ silence_ms: +e.target.value });
  $("savePrompt").onclick = () => { saveSetting({ system_prompt: $("sysPrompt").value }); $("savePrompt").textContent = "Saved ✓"; setTimeout(() => ($("savePrompt").textContent = "Save personality"), 1500); };

  function sample() {
    if (!ctx) return;
    stopReply();
    const id = replyId;
    queueSpeech(`Hi, I'm ${APP}. This is how I sound.`, id);
  }

  // ---------------- Start ----------------
  $("emptyNote").innerHTML = settings && APP
    ? `Say hello to <strong>${esc(APP)}</strong>.<br>Pick <em>Push to talk</em> or <em>Hands-free</em> below.`
    : "";
  renderHistory();
  refreshStatus(true).then(applySettingsToUI);
  setInterval(() => refreshStatus(false), 15000);
  setState("off", "Press Start");

  $("gateBtn").onclick = async () => {
    try {
      await startAudio();
      $("gate").classList.add("hidden");
      setState("idle");
      setMode(settings.mode || "ptt", false);
    } catch (err) {
      $("gate").querySelector("p").textContent = "Couldn't open the microphone: " + err.message + ". Check that your headset is plugged in and allowed.";
    }
  };
  window.addEventListener("resize", () => setMode(settings.mode || "ptt", false));
})();
