"""
voice-host - the app's own speech service.

Clones the two voice capabilities this app used to rent from HeyGen:
  * a voice catalogue + text-to-speech  (Piper, the engine avatar-host already uses)
  * a voice clone                       (XTTS-v2 when its weights are on the volume)

Contract matches avatar-host on purpose: HOST_TOKEN auth, a /data volume, /health first.
Nothing here calls a vendor, and no reference audio leaves this container.
"""

import io
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

DATA = Path(os.environ.get("DATA_DIR", "/data"))
RENDERS = DATA / "renders"
VOICES = DATA / "voices"
for path in (RENDERS, VOICES):
    path.mkdir(parents=True, exist_ok=True)

TOKEN = os.environ.get("HOST_TOKEN", "")
MAX_CHARS = int(os.environ.get("MAX_CHARS", "20000"))
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "600"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")

app = FastAPI(title="X1 voice host", version="1.0.0")

# XTTS is optional. Without its weights the clone registry still works (reference audio is stored),
# but synthesising a clone answers plainly instead of pretending the engine exists.
_xtts = None
_xtts_error = None


def xtts():
    global _xtts, _xtts_error
    if _xtts is not None or _xtts_error is not None:
        return _xtts
    try:
        from TTS.api import TTS  # type: ignore

        weights = DATA / "xtts"
        _xtts = TTS(model_path=str(weights)) if (weights / "config.json").exists() else TTS(model_name="tts_models/multilingual/multi-dataset/xtts_v2")
        return _xtts
    except Exception as exc:  # honest failure, reported to the caller
        _xtts_error = f"{type(exc).__name__}: {exc}"
        return None


def auth(authorization: Optional[str]):
    if not TOKEN:
        raise HTTPException(500, "HOST_TOKEN is not set on this container")
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad host token")


def meta_path(voice_id: str) -> Path:
    return VOICES / f"{voice_id}.json"


def list_voices():
    presets = [
        {"id": "preset:river", "name": "River", "kind": "preset", "note": "calm, neutral"},
        {"id": "preset:honey", "name": "Honey", "kind": "preset", "note": "warm, soft"},
        {"id": "preset:sunny", "name": "Sunny", "kind": "preset", "note": "bright, upbeat"},
        {"id": "preset:storm", "name": "Storm", "kind": "preset", "note": "formal, authoritative"},
    ]
    clones = []
    for item in sorted(VOICES.glob("*.json")):
        try:
            clones.append(json.loads(item.read_text()))
        except Exception:
            continue
    return presets + clones


def speak_piper(text: str, out: Path):
    """Piper writes a wav; it is the same engine avatar-host renders with."""
    model = Path(os.environ.get("PIPER_MODEL", str(DATA / "piper" / f"{PIPER_VOICE}.onnx")))
    if not model.exists():
        raise HTTPException(503, f"Piper voice model not present at {model}")
    proc = subprocess.run(
        ["piper", "--model", str(model), "--output_file", str(out)],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=MAX_SECONDS,
    )
    if proc.returncode != 0:
        raise HTTPException(502, f"piper failed: {proc.stderr.decode('utf-8', 'ignore')[:300]}")


def speak_clone(text: str, voice_id: str, out: Path):
    engine = xtts()
    if engine is None:
        raise HTTPException(
            409,
            "The clone engine is not installed on this container. Its reference audio is stored and kept; "
            f"synthesis needs XTTS-v2 weights on the volume ({_xtts_error}).",
        )
    reference = VOICES / f"{voice_id}.wav"
    if not reference.exists():
        raise HTTPException(404, f"no reference audio for {voice_id}")
    engine.tts_to_file(text=text, speaker_wav=str(reference), language="en", file_path=str(out))


class SpeakBody(BaseModel):
    text: str
    voice_id: str = "preset:river"


@app.get("/health")
def health():
    engine = xtts()
    return {
        "ok": True,
        "service": "voice-host",
        "clones": len([p for p in VOICES.glob("*.json")]),
        "renders": len(list(RENDERS.glob("*.wav"))),
        "clone_engine": "ready" if engine is not None else "absent",
        "clone_error": _xtts_error,
        "limits": {"max_chars": MAX_CHARS, "max_seconds": MAX_SECONDS},
    }


@app.get("/voices")
def voices(authorization: Optional[str] = Header(None)):
    auth(authorization)
    return {"ok": True, "voices": list_voices()}


@app.post("/clone")
async def clone(
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
    if not payload[:4] == b"RIFF":
        raise HTTPException(400, "reference audio must be a WAV file")

    voice_id = f"clone-{uuid.uuid4().hex[:12]}"
    (VOICES / f"{voice_id}.wav").write_bytes(payload)
    meta = {
        "id": voice_id,
        "name": re.sub(r"\s+", " ", name).strip()[:80],
        "kind": "clone",
        "bytes": len(payload),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "reference audio is private to this volume",
    }
    meta_path(voice_id).write_text(json.dumps(meta))
    return {"ok": True, "voice": meta}


@app.post("/speak")
def speak(body: SpeakBody, authorization: Optional[str] = Header(None)):
    auth(authorization)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > MAX_CHARS:
        raise HTTPException(400, f"text is limited to {MAX_CHARS} characters")

    job_id = f"sp-{uuid.uuid4().hex[:12]}"
    out = RENDERS / f"{job_id}.wav"
    if body.voice_id.startswith("clone-"):
        speak_clone(text, body.voice_id, out)
    else:
        speak_piper(text, out)

    return {
        "ok": True,
        "job_id": job_id,
        "voice_id": body.voice_id,
        "bytes": out.stat().st_size,
        "url": f"/renders/{job_id}.wav",
        "chars": len(text),
    }


@app.get("/renders")
def renders(authorization: Optional[str] = Header(None)):
    auth(authorization)
    items = []
    for path in sorted(RENDERS.glob("*.wav")):
        stat = path.stat()
        items.append({"job_id": path.stem, "bytes": stat.st_size, "created_at": int(stat.st_mtime)})
    return {"ok": True, "items": items}


# .wav is registered before the plain id route on purpose: a path parameter matches dots, so the
# other order answers a download with a JSON lookup (the exact defect avatar-host shipped once).
@app.get("/renders/{job_id}.wav")
def render_wav(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    path = RENDERS / f"{job_id}.wav"
    if not path.exists():
        raise HTTPException(404, "No such render")
    return FileResponse(path, media_type="audio/wav", filename=f"{job_id}.wav")


@app.get("/renders/{job_id}")
def render_meta(job_id: str, authorization: Optional[str] = Header(None)):
    auth(authorization)
    path = RENDERS / f"{job_id}.wav"
    if not path.exists():
        raise HTTPException(404, "No such render")
    stat = path.stat()
    return {"ok": True, "job_id": job_id, "bytes": stat.st_size, "url": f"/renders/{job_id}.wav"}