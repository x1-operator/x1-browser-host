"""X1 avatar host — the app's own talking-portrait service.

Nothing here is rented: one container holds HER image exactly as uploaded (no moderation, no
re-encode), turns text into speech with a local voice model, and re-syncs her mouth to that audio
with Wav2Lip. One bearer token guards everything.
"""

import json
import os
import subprocess
import threading
import time
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


# ---- speech: the same engine, offered as a voice service (parity with voice-host/) -----------
VOICES = DATA / "voices"
VOICES.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

VOICE_PRESETS = [
    {"id": "preset:river", "name": "River", "kind": "preset", "note": "calm, neutral"},
    {"id": "preset:honey", "name": "Honey", "kind": "preset", "note": "warm, soft"},
    {"id": "preset:sunny", "name": "Sunny", "kind": "preset", "note": "bright, upbeat"},
    {"id": "preset:storm", "name": "Storm", "kind": "preset", "note": "formal, authoritative"},
]


@app.get("/voices")
def list_voices(authorization: Optional[str] = Header(None)):
    auth(authorization)
    clones = []
    for item in sorted(VOICES.glob("*.json")):
        try:
            clones.append(json.loads(item.read_text()))
        except Exception:
            continue
    return {"ok": True, "voices": VOICE_PRESETS + clones}


@app.post("/speak")
def speak_text(payload: dict, authorization: Optional[str] = Header(None)):
    auth(authorization)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > MAX_CHARS:
        raise HTTPException(400, f"text is limited to {MAX_CHARS} characters")
    voice_id = str(payload.get("voice_id") or "preset:river")
    if voice_id.startswith("clone-"):
        raise HTTPException(
            409,
            "Clone synthesis needs the XTTS-v2 engine, which this container does not carry. "
            "The reference audio is stored and kept on the volume.",
        )
    job_id = f"sp-{uuid.uuid4().hex[:12]}"
    out = RENDERS / f"{job_id}.wav"
    speak(text, out)
    return {
        "ok": True,
        "job_id": job_id,
        "voice_id": voice_id,
        "bytes": out.stat().st_size,
        "url": f"/voice/{job_id}.wav",
        "chars": len(text),
    }


@app.post("/clone")
async def upload_clone(
    file: UploadFile = File(...),
    name: str = Form("Unnamed clone"),
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
    (VOICES / f"{voice_id}.wav").write_bytes(payload)
    meta = {
        "id": voice_id,
        "name": (name or "Unnamed clone")[:80],
        "kind": "clone",
        "bytes": len(payload),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "reference audio is private to this volume",
    }
    (VOICES / f"{voice_id}.json").write_text(json.dumps(meta))
    return {"ok": True, "voice": meta}


# The .wav route is registered before the plain id route: a path parameter matches dots, so the
# other order answers a download with a JSON lookup.
@app.get("/voice/{job_id}.wav")
def get_voice_wav(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    path = RENDERS / f"{job_id}.wav"
    if not path.exists():
        raise HTTPException(404, "No such render")
    return FileResponse(path, media_type="audio/wav", filename=f"{job_id}.wav")


@app.get("/voice/{job_id}")
def get_voice_meta(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    path = RENDERS / f"{job_id}.wav"
    if not path.exists():
        raise HTTPException(404, "No such render")
    return {"ok": True, "job_id": job_id, "bytes": path.stat().st_size, "url": f"/voice/{job_id}.wav"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)