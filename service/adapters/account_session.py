#!/usr/bin/env python3
"""Private, atomic service account storage. No credential in argv or errors."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

LIMIT = 131072


def private(info, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    return kind(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)


def store(directory, action, payload=None):
    directory = Path(directory)
    if not directory.is_absolute() or not private(directory.lstat(), True):
        raise ValueError('private directory required')
    target = directory / 'account-session.json'
    if action == 'remove':
        # Unlink a planted symlink itself, never its destination.
        target.unlink(missing_ok=True)
        return {}
    if action == 'read':
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd) as source:
            if not private(os.fstat(source.fileno())):
                raise ValueError('private regular file required')
            raw = source.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('oversized account session')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('invalid account session')
        return value
    if action != 'write' or not isinstance(payload, dict):
        raise ValueError('invalid operation')
    raw = json.dumps(payload, separators=(',', ':'))
    if len(raw) > LIMIT:
        raise ValueError('oversized account session')
    # A random exclusive sibling prevents fixed .new symlink attacks.
    fd, temporary = tempfile.mkstemp(prefix='.account-session-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as destination:
            destination.write(raw)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
        parent = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {}


if __name__ == '__main__':
    try:
        action, directory = sys.argv[1:]
        payload = json.loads(sys.stdin.read(LIMIT + 1)) if action == 'write' else None
        print(json.dumps(store(directory, action, payload)))
    except Exception:
        # Errors must not include input, token, or a stack trace.
        print('account session unavailable', file=sys.stderr)
        sys.exit(1)
