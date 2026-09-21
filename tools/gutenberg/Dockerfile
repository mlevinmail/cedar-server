# The `classics` helper: fills the book store from Project Gutenberg, then exits.
# Separate from the server image on purpose — rsync, curl and Pillow are needed
# for one download, not for serving. See get-classics.sh.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl rsync \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pillow

WORKDIR /tools
COPY gutenberg_ingest.py gutenberg_extract_txt.py gutenberg_debrand_covers.py get-classics.sh ./
RUN chmod 0755 get-classics.sh

ENV CEDAR_DATA=/data
ENTRYPOINT ["/tools/get-classics.sh"]
