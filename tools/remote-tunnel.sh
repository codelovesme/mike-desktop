#!/bin/sh
# On a second computer, put the Euglena host behind an authenticated SSH
# tunnel. Keep the native net_client URLs on loopback, never plain remote HTTP.
set -eu
if [ "$#" -ne 1 ]; then
    printf 'Usage: %s user@euglena-server\n' "$0" >&2
    exit 2
fi
exec ssh -N -o ExitOnForwardFailure=yes \
    -L 127.0.0.1:18899:127.0.0.1:8899 "$1"
