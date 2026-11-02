#!/usr/bin/python3
"""Validate local request attachments without files or model access."""
import json
import sys
from pathlib import Path

# Both source checkout and packaged service have the shared validator in client/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'client'))
import images as image_validation

try:
    request = json.loads(sys.stdin.buffer.read(9 * 1024 * 1024))
    image_validation.validate(request)
    print(json.dumps({'ok': True}))
except (ValueError, TypeError, KeyError) as error:
    print(json.dumps({'ok': False, 'error': str(error)}))
    sys.exit(1)
