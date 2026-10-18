#!/bin/sh
# Build the fixture stand-ins (gitignored .so files) with the Code that runs
# the fixtures: CODE_BIN, or `code` on PATH.
set -eu
cd "$(dirname -- "$0")"
code=${CODE_BIN:-code}
for mock in process_mock http_mock server_mock timer_mock; do
    "$code" build "$mock.code" -t shared -o "$mock.so"
done
