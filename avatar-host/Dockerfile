FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg git curl libgl1 libglib2.0-0 libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir torch==2.2.2 --index-url https://download.pytorch.org/whl/cpu

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Wav2Lip calls librosa.filters.mel positionally, which librosa 0.10+ refuses
# ("mel() takes 0 positional arguments but 2 were given"). Patch the call at build so a
# render works on whichever librosa the image resolves.
RUN git clone --depth 1 https://github.com/Rudrabha/Wav2Lip /opt/wav2lip \
    && mkdir -p /opt/wav2lip/checkpoints /opt/wav2lip/face_detection/detection/sfd /opt/voices \
    && curl -fsSL -o /opt/wav2lip/checkpoints/wav2lip_gan.pth \
         https://huggingface.co/camenduru/Wav2Lip/resolve/main/checkpoints/wav2lip_gan.pth \
    && curl -fsSL -o /opt/wav2lip/face_detection/detection/sfd/s3fd.pth \
         https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth \
    && curl -fsSL -o /opt/voices/voice.onnx \
         https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx \
    && curl -fsSL -o /opt/voices/voice.onnx.json \
         https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json
    && sed -i 's/librosa\.filters\.mel(hp\.sample_rate, hp\.n_fft/librosa.filters.mel(sr=hp.sample_rate, n_fft=hp.n_fft/' \
        /opt/wav2lip/audio.py \
    && grep -n "mel(sr=hp.sample_rate" /opt/wav2lip/audio.py

COPY server.py ./

ENV PORT=8080 \
    DATA_DIR=/data \
    WAV2LIP_DIR=/opt/wav2lip \
    VOICE=/opt/voices/voice.onnx

EXPOSE 8080
CMD ["python", "server.py"]
