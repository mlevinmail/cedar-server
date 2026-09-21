# cedar-server — the API. Kokoro (the voice engine) runs as its own container.
FROM python:3.12-slim

WORKDIR /app

# PyMuPDF ships wheels, so the only system dep is the mp3 encoder: Kokoro's mp3
# is fixed at 128 kbps, and cedar/tts.py re-encodes to CEDAR_AUDIO_BITRATE_KBPS
# to halve what listeners on mobile data pull down. lame rather than ffmpeg:
# ~7 MB installed against ~620 MB, and it writes the LAME/Info gapless header
# the word highlight depends on. Without it the server still runs — Kokoro's
# own mp3 is served — so this is a size/latency dependency, not a correctness one.
#
# The server runs as `cedar` (uid 1000), not root: see entrypoint.sh.
RUN apt-get update \
    && apt-get install -y --no-install-recommends lame \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 cedar \
    && useradd --uid 1000 --gid cedar --no-create-home --shell /usr/sbin/nologin cedar

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cedar ./cedar
COPY entrypoint.sh ./entrypoint.sh
# Compile now: /app is root-owned and read-only to the server, so it could not
# write bytecode at startup. /data is created owned by cedar so a fresh named
# volume inherits that ownership.
RUN python -m compileall -q cedar \
    && chmod 0755 entrypoint.sh \
    && mkdir -p /data && chown cedar:cedar /data

ENV CEDAR_DATA=/data \
    CEDAR_KOKORO_URL=http://kokoro:8880
VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
# --timeout-keep-alive above a typical reverse proxy's origin keep-alive (90 s
# for Cloudflare tunnels): with the 5 s default, uvicorn closed idle connections
# the proxy was about to reuse, which surfaced as a 502 for the listener.
# --no-server-header: no "server: uvicorn" for a port scanner to file away.
CMD ["uvicorn", "cedar.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-keep-alive", "120", "--no-server-header"]
