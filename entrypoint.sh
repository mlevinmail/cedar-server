#!/bin/sh
# cedar-server container entrypoint.
#
# The image starts as root only long enough to make sure the data directory is
# owned by the unprivileged `cedar` user (a fresh volume already is; one from a
# release that ran as root is taken over once), then drops to that user for
# good — uid/gid, supplementary groups, and every capability, so nothing the
# server does can get root back. With `user:` set in compose the process is
# already unprivileged and this is a plain exec.
set -eu

DATA="${CEDAR_DATA:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA"
    if [ "$(stat -c %u "$DATA")" != "$(id -u cedar)" ]; then
        echo "entrypoint: taking ownership of $DATA for the cedar user" >&2
        chown -R cedar:cedar "$DATA"
    fi
    exec setpriv --reuid=cedar --regid=cedar --init-groups \
                 --inh-caps=-all --bounding-set=-all "$@"
fi

exec "$@"
