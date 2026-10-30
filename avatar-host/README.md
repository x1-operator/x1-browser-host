# X1 avatar host - her face, on our own metal

One container. Stores her image exactly as uploaded (no vendor moderation, no re-encode), turns
text into speech with a local voice model, and re-syncs her mouth to that audio with Wav2Lip.

Routes: GET /health, POST /avatar, GET /avatar, POST /render {text}, GET /renders,
GET /renders/:id, GET /renders/:id.mp4. Everything but /health needs Bearer HOST_TOKEN.

Env: HOST_TOKEN (required), PORT, DATA_DIR=/data, MAX_RENDER_SECONDS=900, MAX_CHARS=1200.

Railway: root directory avatar-host, add HOST_TOKEN, volume at /data. Image ~3 GB, first build
10-15 minutes. CPU inference: a short clip renders in minutes, not live.
