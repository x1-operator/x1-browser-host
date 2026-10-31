"""X1 avatar host — the app's own talking-portrait service.

Nothing here is rented: one container holds HER image exactly as uploaded (no moderation, no
re-encode), turns text into speech with a local voice model, and re-syncs her mouth to that audio
with Wav2Lip. One bearer token guards everything.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
import uvicorn

DATA = Path(os.environ.get("DATA_DIR", "/data"))
RENDERS = DATA / "renders"
AVATAR = DATA / "avatar.bin"
AVATAR_META = DATA / "avatar.json"
WAV2LIP = Path(os.environ.get("WAV2LIP_DIR", "/opt/wav2lip"))
VOICE = os.environ.get("VOICE", "/opt/voices/voice.onnx")
TOKEN = os.environ.get("HOST_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
MAX_RENDER_SECONDS = int(os.environ.get("MAX_RENDER_SECONDS", "7200"))
MAX_CHARS = int(os.environ.get("MAX_CHARS", "20000"))

DATA.mkdir(parents=True, exist_ok=True)
RENDERS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="X1 avatar host")
JOBS = {}
LOCK = threading.Lock()
USAGE = {"renders": 0, "seconds": 0.0, "started_at": time.time()}


def auth(authorization: Optional[str]):
    if not TOKEN:
        raise HTTPException(500, "HOST_TOKEN is not set on this container")
    if not authorization or authorization.replace("Bearer ", "").strip() != TOKEN:
        raise HTTPException(401, "Unauthorized")


def tail(text: bytes, size: int = 400) -> str:
    return text.decode("utf-8", "replace")[-size:]


def speak(text: str, wav: Path):
    """Text → 16 kHz mono WAV with the local voice model."""
    raw = wav.with_suffix(".raw.wav")
    proc = subprocess.run(
        ["piper", "--model", VOICE, "--output_file", str(raw)],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"voice model failed: {tail(proc.stderr)}")
    conv = subprocess.run(
        ["ffmpeg", "-y", "-i", str(raw), "-ar", "16000", "-ac", "1", str(wav)],
        capture_output=True,
        timeout=180,
    )
    raw.unlink(missing_ok=True)
    if conv.returncode != 0:
        raise RuntimeError(f"audio convert failed: {tail(conv.stderr)}")
    if not wav.exists() or wav.stat().st_size == 0:
        raise RuntimeError("the voice model produced no audio")


def animate(wav: Path, out: Path):
    """Her exact still + that audio → an mp4 whose mouth follows the words."""
    cmd = [
        "python",
        "inference.py",
        "--checkpoint_path",
        "checkpoints/wav2lip_gan.pth",
        "--face",
        str(AVATAR),
        "--audio",
        str(wav),
        "--outfile",
        str(out),
        "--pads",
        "0",
        "12",
        "0",
        "0",
        "--nosmooth",
    ]
    proc = subprocess.run(cmd, cwd=str(WAV2LIP), capture_output=True, timeout=MAX_RENDER_SECONDS)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"animation failed: {tail(proc.stderr or proc.stdout)}")


def run_job(job_id: str, text: str):
    job = JOBS[job_id]
    wav = RENDERS / f"{job_id}.wav"
    out = RENDERS / f"{job_id}.mp4"
    started = time.time()
    try:
        job["status"] = "speaking"
        speak(text, wav)
        job["status"] = "animating"
        animate(wav, out)
        job["status"] = "done"
        job["video_url"] = f"/renders/{job_id}.mp4"
        with LOCK:
            USAGE["renders"] += 1
            USAGE["seconds"] += round(time.time() - started, 1)
    except Exception as error:  # surfaced to the caller, never swallowed
        job["status"] = "failed"
        job["error"] = str(error)[:400]
    finally:
        job["ended_at"] = time.time()
        wav.unlink(missing_ok=True)


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "x1-avatar-host",
        "avatar": AVATAR.exists(),
        "engine": {
            "checkpoint": (WAV2LIP / "checkpoints" / "wav2lip_gan.pth").exists(),
            "detector": (WAV2LIP / "face_detection" / "detection" / "sfd" / "s3fd.pth").exists(),
            "voice": Path(VOICE).exists(),
        },
        "renders": len(JOBS),
        "uptime_seconds": round(time.time() - USAGE["started_at"]),
    }


@app.post("/avatar")
async def upload_avatar(file: UploadFile = File(...), authorization: Optional[str] = Header(None)):
    auth(authorization)
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "The image arrived empty")
    AVATAR.write_bytes(payload)  # byte-for-byte, never re-encoded
    AVATAR_META.write_text(
        f'{{"name":"{file.filename or "avatar"}","bytes":{len(payload)},"at":{int(time.time())}}}'
    )
    return {"ok": True, "bytes": len(payload), "name": file.filename, "stored": str(AVATAR)}


@app.get("/avatar")
def get_avatar(authorization: Optional[str] = Header(None)):
    auth(authorization)
    if not AVATAR.exists():
        raise HTTPException(404, "No image has been uploaded yet")
    return FileResponse(AVATAR)


@app.post("/render")
def render(payload: dict, authorization: Optional[str] = Header(None)):
    auth(authorization)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > MAX_CHARS:
        raise HTTPException(400, f"text is limited to {MAX_CHARS} characters")
    if not AVATAR.exists():
        raise HTTPException(400, "Upload her image first (POST /avatar)")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "id": job_id,
        "status": "queued",
        "text": text[:200],
        "chars": len(text),
        "created_at": time.time(),
    }
    threading.Thread(target=run_job, args=(job_id, text), daemon=True).start()
    return {"ok": True, "job": JOBS[job_id]}


@app.get("/renders")
def list_renders(authorization: Optional[str] = Header(None)):
    auth(authorization)
    items = sorted(JOBS.values(), key=lambda j: j["created_at"], reverse=True)
    return {"ok": True, "count": len(items), "items": items[:50], "usage": USAGE}


# Registered before the plain job route: "/renders/<id>" also matches "/renders/<id>.mp4",
# so the video route has to win the match or every download 404s as an unknown job.
@app.get("/renders/{job_id}.mp4")
def get_video(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    path = RENDERS / f"{job_id}.mp4"
    if not path.exists():
        raise HTTPException(404, "That render has no video yet")
    return FileResponse(path, media_type="video/mp4")


@app.get("/renders/{job_id}")
def get_render(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such render")
    return {"ok": True, "job": job}


# ---- the voice system: catalogue, prosody, emotion, long-form, cloning, history ---------------
VOICES = DATA / "voices"
PIPER = DATA / "piper"
for path in (VOICES, PIPER):
    path.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
HF_VOICES = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Emotion is prosody, not a vendor call: it moves rate, pitch, gain and pause length together.
EMOTIONS = {
    "neutral": {"rate": 1.0, "pitch": 0.0, "gain": 1.0, "pause": 1.0},
    "warm": {"rate": 0.96, "pitch": -0.6, "gain": 1.0, "pause": 1.15},
    "excited": {"rate": 1.12, "pitch": 1.8, "gain": 1.12, "pause": 0.85},
    "calm": {"rate": 0.92, "pitch": -1.0, "gain": 0.96, "pause": 1.25},
    "serious": {"rate": 0.95, "pitch": -1.8, "gain": 1.04, "pause": 1.1},
    "sad": {"rate": 0.9, "pitch": -2.2, "gain": 0.92, "pause": 1.3},
}

PRESETS = [
    {"id": "preset:river", "name": "River", "kind": "preset", "language": "en-US",
     "note": "calm, neutral", "defaults": {"speed": 1.0, "pitch": 0, "volume": 1.0, "emotion": "neutral"}},
    {"id": "preset:honey", "name": "Honey", "kind": "preset", "language": "en-US",
     "note": "warm, soft", "defaults": {"speed": 0.96, "pitch": -0.8, "volume": 0.98, "emotion": "warm"}},
    {"id": "preset:sunny", "name": "Sunny", "kind": "preset", "language": "en-US",
     "note": "bright, upbeat", "defaults": {"speed": 1.08, "pitch": 1.6, "volume": 1.05, "emotion": "excited"}},
    {"id": "preset:storm", "name": "Storm", "kind": "preset", "language": "en-US",
     "note": "formal, authoritative", "defaults": {"speed": 0.95, "pitch": -1.8, "volume": 1.04, "emotion": "serious"}},
]


def piper_models():
    found = []
    baked = Path(VOICE)
    if baked.exists():
        found.append({"id": "model:default", "name": "Amy (baked)", "path": baked, "kind": "model",
                      "language": "en-US", "note": "shipped in the image"})
    for onnx in sorted(PIPER.glob("*.onnx")):
        found.append({"id": f"model:{onnx.stem}", "name": onnx.stem, "path": onnx, "kind": "model",
                      "language": onnx.stem.split("-")[0].replace("_", "-"), "note": "installed on the volume"})
    return found


def model_path_for(voice_id: str):
    if voice_id.startswith("model:"):
        key = voice_id.split(":", 1)[1]
        if key == "default":
            return Path(VOICE)
        candidate = PIPER / f"{key}.onnx"
        return candidate if candidate.exists() else Path(VOICE)
    return Path(VOICE)


def clone_meta(voice_id: str):
    item = VOICES / f"{voice_id}.json"
    if not item.exists():
        return None
    try:
        return json.loads(item.read_text())
    except Exception:
        return None


def clone_registry():
    items = []
    for item in sorted(VOICES.glob("*.json")):
        try:
            items.append(json.loads(item.read_text()))
        except Exception:
            continue
    return items


def voice_catalog():
    return PRESETS + piper_models() + clone_registry()


def ffmpeg(args, seconds=300):
    proc = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, timeout=seconds)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {tail(proc.stderr)}")


def piper_say(text: str, model: Path, raw: Path):
    proc = subprocess.run(
        ["piper", "--model", str(model), "--output_file", str(raw)],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=max(180, int(len(text) / 8)),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"voice model failed: {tail(proc.stderr)}")
    if not raw.exists() or raw.stat().st_size == 0:
        raise RuntimeError("the voice model produced no audio")


BREAK = re.compile(r'<break\s+time="?(\d+(?:\.\d+)?)(ms|s)"?\s*/?>', re.IGNORECASE)


def split_script(text: str):
    parts = []
    cursor = 0
    for match in BREAK.finditer(text):
        chunk = text[cursor:match.start()].strip()
        if chunk:
            parts.append(("say", chunk))
        amount = float(match.group(1)) / (1000.0 if match.group(2).lower() == "ms" else 1.0)
        parts.append(("pause", max(0.05, min(10.0, amount))))
        cursor = match.end()
    rest = text[cursor:].strip()
    if rest:
        parts.append(("say", rest))
    return parts or [("say", text)]


def chunks_of(text: str, size: int = 700):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out, current = [], ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(current) + len(sentence) + 1 <= size:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                out.append(current)
            current = sentence[:size]
    if current:
        out.append(current)
    return out


def prosody_of(payload: dict, voice_id: str):
    defaults = {}
    if voice_id.startswith("preset:"):
        preset = next((item for item in PRESETS if item["id"] == voice_id), None)
        defaults = preset.get("defaults", {}) if preset else {}
    elif voice_id.startswith("clone-"):
        defaults = (clone_meta(voice_id) or {}).get("defaults", {}) or {}

    emotion = str(payload.get("emotion") or defaults.get("emotion") or "neutral").lower()
    curve = EMOTIONS.get(emotion, EMOTIONS["neutral"])

    def pick(name, low, high, fallback):
        raw = payload.get(name)
        if raw in (None, ""):
            raw = defaults.get(name, fallback)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(fallback)
        return max(low, min(high, value))

    speed = pick("speed", 0.5, 2.0, 1.0) * curve["rate"]
    pitch = pick("pitch", -12.0, 12.0, 0.0) + curve["pitch"]
    volume = pick("volume", 0.2, 2.0, 1.0) * curve["gain"]
    pause = pick("pause_scale", 0.25, 3.0, 1.0) * curve["pause"]
    return {
        "emotion": emotion if emotion in EMOTIONS else "neutral",
        "speed": round(max(0.5, min(2.0, speed)), 3),
        "pitch": round(max(-12.0, min(12.0, pitch)), 2),
        "volume": round(max(0.2, min(2.0, volume)), 3),
        "pause": round(max(0.25, min(3.0, pause)), 2),
    }


def render_speech(text: str, voice_id: str, prosody: dict, out: Path, fmt: str, sample_rate: int):
    model = model_path_for(voice_id)
    work = RENDERS / f"tmp-{uuid.uuid4().hex[:10]}"
    work.mkdir(parents=True, exist_ok=True)
    pieces = []
    try:
        index = 0
        for kind, value in split_script(text):
            index += 1
            if kind == "pause":
                target = work / f"part-{index:04d}.wav"
                seconds = value * prosody["pause"]
                ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", f"{seconds:.2f}", str(target)])
                pieces.append(target)
                continue
            for chunk in chunks_of(value):
                index += 1
                raw = work / f"raw-{index:04d}.wav"
                spoken = work / f"part-{index:04d}.wav"
                piper_say(chunk, model, raw)
                ffmpeg(["-i", str(raw), "-ar", "22050", "-ac", "1", str(spoken)])
                raw.unlink(missing_ok=True)
                pieces.append(spoken)

        if not pieces:
            raise RuntimeError("nothing to speak")

        listing = work / "parts.txt"
        listing.write_text("".join(f"file '{piece.name}'\n" for piece in pieces))
        joined = work / "joined.wav"
        ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), str(joined)])

        chain = []
        speed = prosody["speed"]
        while speed > 2.0:
            chain.append("atempo=2.0")
            speed /= 2.0
        while speed < 0.5:
            chain.append("atempo=0.5")
            speed /= 0.5
        if abs(speed - 1.0) > 0.01:
            chain.append(f"atempo={speed:.3f}")
        if abs(prosody["pitch"]) > 0.01:
            factor = 2 ** (prosody["pitch"] / 12.0)
            keep = max(0.5, min(2.0, 1.0 / factor))
            chain.append(f"asetrate={int(sample_rate * factor)}")
            chain.append(f"aresample={sample_rate}")
            chain.append(f"atempo={keep:.3f}")
        chain.append(f"volume={prosody['volume']:.3f}")
        codec = ["-codec:a", "libmp3lame", "-b:a", "192k"] if fmt == "mp3" else ["-codec:a", "pcm_s16le"]
        ffmpeg(["-i", str(joined), "-af", ",".join(chain), "-ar", str(sample_rate), "-ac", "1", *codec, str(out)])
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("the render produced no audio")
    finally:
        for leftover in work.glob("*"):
            leftover.unlink(missing_ok=True)
        work.rmdir()


def voice_entry(job_id: str, fmt: str):
    return {"job_id": job_id, "format": fmt, "url": f"/voice/{job_id}.{fmt}"}


@app.get("/voices")
def list_voices(authorization: Optional[str] = Header(None)):
    auth(authorization)
    return {
        "ok": True,
        "voices": voice_catalog(),
        "emotions": list(EMOTIONS.keys()),
        "max_chars": MAX_CHARS,
    }


@app.post("/voices/install")
def install_voice(payload: dict, authorization: Optional[str] = Header(None)):
    auth(authorization)
    key = str(payload.get("voice") or "").strip()
    if not key or key.count("-") < 2:
        raise HTTPException(400, "voice must look like en_US-lessac-medium")
    locale, name, quality = key.split("-", 2)
    family = locale.split("_")[0]
    base = f"{HF_VOICES}/{family}/{locale}/{name}/{quality}/{key}"
    target = PIPER / f"{key}.onnx"
    config = PIPER / f"{key}.onnx.json"
    try:
        for url, destination in ((f"{base}.onnx", target), (f"{base}.onnx.json", config)):
            if destination.exists():
                continue
            with urllib.request.urlopen(url, timeout=180) as response:
                destination.write_bytes(response.read())
    except Exception as exc:
        target.unlink(missing_ok=True)
        config.unlink(missing_ok=True)
        raise HTTPException(404, f"that voice is not in the Piper catalogue ({type(exc).__name__})")
    return {"ok": True, "voice": {"id": f"model:{key}", "name": key, "kind": "model", "bytes": target.stat().st_size}}


@app.post("/speak")
def speak_text(payload: dict, authorization: Optional[str] = Header(None)):
    auth(authorization)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > MAX_CHARS:
        raise HTTPException(400, f"text is limited to {MAX_CHARS} characters")
    voice_id = str(payload.get("voice_id") or "preset:river")
    fmt = "mp3" if str(payload.get("format", "wav")).lower() == "mp3" else "wav"
    try:
        sample_rate = int(str(payload.get("sample_rate") or 24000))
    except (TypeError, ValueError):
        sample_rate = 24000
    sample_rate = max(16000, min(48000, sample_rate))
    prosody = prosody_of(payload, voice_id)

    job_id = f"sp-{uuid.uuid4().hex[:12]}"
    out = RENDERS / f"{job_id}.{fmt}"
    try:
        render_speech(text, voice_id, prosody, out, fmt, sample_rate)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    meta = {
        **voice_entry(job_id, fmt),
        "voice_id": voice_id,
        "voice_name": next((item["name"] for item in voice_catalog() if item["id"] == voice_id), voice_id),
        "prosody": prosody,
        "chars": len(text),
        "bytes": out.stat().st_size,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (RENDERS / f"{job_id}.json").write_text(json.dumps(meta))
    return {"ok": True, **meta}


@app.post("/clone")
async def upload_clone(
    file: UploadFile = File(...),
    name: str = Form("Unnamed clone"),
    speed: str = Form("1.0"),
    pitch: str = Form("0"),
    volume: str = Form("1.0"),
    emotion: str = Form("neutral"),
    authorization: Optional[str] = Header(None),
):
    auth(authorization)
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "empty upload")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"reference audio is limited to {MAX_UPLOAD_BYTES} bytes")
    if payload[:4] != b"RIFF":
        raise HTTPException(400, "reference audio must be a WAV file")
    voice_id = f"clone-{uuid.uuid4().hex[:12]}"
    reference = VOICES / f"{voice_id}.wav"
    reference.write_bytes(payload)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(reference)],
        capture_output=True,
        timeout=60,
    )
    try:
        duration = round(float(probe.stdout.decode().strip() or 0), 2)
    except ValueError:
        duration = 0.0
    meta = {
        "id": voice_id,
        "name": (name or "Unnamed clone")[:80],
        "kind": "clone",
        "language": "en-US",
        "bytes": len(payload),
        "reference_seconds": duration,
        "defaults": {
            "speed": float(speed or 1.0),
            "pitch": float(pitch or 0),
            "volume": float(volume or 1.0),
            "emotion": emotion if emotion in EMOTIONS else "neutral",
        },
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "reference audio is private to this volume",
    }
    (VOICES / f"{voice_id}.json").write_text(json.dumps(meta))
    return {"ok": True, "voice": meta}


@app.delete("/clone/{voice_id}")
def delete_clone(voice_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    if not clone_meta(voice_id):
        raise HTTPException(404, "No such clone")
    (VOICES / f"{voice_id}.json").unlink(missing_ok=True)
    (VOICES / f"{voice_id}.wav").unlink(missing_ok=True)
    return {"ok": True, "deleted": voice_id}


@app.get("/clone/{voice_id}/reference")
def clone_reference(voice_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    path = VOICES / f"{voice_id}.wav"
    if not path.exists():
        raise HTTPException(404, "No reference audio for that clone")
    return FileResponse(path, media_type="audio/wav", filename=f"{voice_id}.wav")


@app.get("/voice/history")
def voice_history(limit: int = 40, authorization: Optional[str] = Header(None)):
    auth(authorization)
    items = []
    for item in RENDERS.glob("sp-*.json"):
        try:
            items.append(json.loads(item.read_text()))
        except Exception:
            continue
    items.sort(key=lambda entry: entry.get("created_at", ""), reverse=True)
    return {"ok": True, "count": len(items), "items": items[: max(1, min(200, limit))]}


@app.delete("/voice/{job_id}")
def delete_voice(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    removed = False
    for suffix in ("wav", "mp3", "json"):
        path = RENDERS / f"{job_id}.{suffix}"
        if path.exists():
            path.unlink()
            removed = True
    if not removed:
        raise HTTPException(404, "No such render")
    return {"ok": True, "deleted": job_id}


@app.get("/voice/{job_id}.wav")
def get_voice_wav(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    path = RENDERS / f"{job_id}.wav"
    if not path.exists():
        raise HTTPException(404, "No such render")
    return FileResponse(path, media_type="audio/wav", filename=f"{job_id}.wav")


@app.get("/voice/{job_id}.mp3")
def get_voice_mp3(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    path = RENDERS / f"{job_id}.mp3"
    if not path.exists():
        raise HTTPException(404, "No such render")
    return FileResponse(path, media_type="audio/mpeg", filename=f"{job_id}.mp3")


@app.get("/voice/{job_id}")
def get_voice_meta(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    meta = RENDERS / f"{job_id}.json"
    if meta.exists():
        return {"ok": True, **json.loads(meta.read_text())}
    path = RENDERS / f"{job_id}.wav"
    if not path.exists():
        raise HTTPException(404, "No such render")
    return {"ok": True, "job_id": job_id, "bytes": path.stat().st_size, "url": f"/voice/{job_id}.wav"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)