"""Refresh enclosing IDE context per turn; credentials stay on this machine.

The shared Mike service may have started outside this terminal. Therefore the
model receives an explicit local command with a descriptor path, rather than
assuming that the service's worker inherited the CLI's environment.
"""
import json
import os
from pathlib import Path
import subprocess


def current():
    session_file = os.environ.get('CODELOVESME_IDE_SESSION_FILE')
    helper = os.environ.get('CODELOVESME_IDE_HELPER')
    terminal = os.environ.get('CODELOVESME_IDE_TERMINAL')
    if not session_file and not helper and not terminal:
        return ''
    if not all((session_file, helper, terminal)) or not Path(helper).is_absolute():
        raise ValueError('IDE connection is incomplete; launch Mike again from its IDE terminal')
    try:
        result = subprocess.run(
            ['/usr/bin/python3', helper, '--session-file', session_file,
             '--terminal', terminal, 'context'],
            text=True, capture_output=True, timeout=8,
        )
        if result.returncode:
            raise ValueError('IDE session is unavailable; launch Mike again from a live IDE terminal')
        value = json.loads(result.stdout)['context']
        if not isinstance(value, str) or len(value.encode()) > 4096:
            raise ValueError('invalid IDE context')
        return value
    except (OSError, subprocess.TimeoutExpired, KeyError, json.JSONDecodeError) as exc:
        raise ValueError('could not refresh the IDE session context') from exc
