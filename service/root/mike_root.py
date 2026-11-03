#!/usr/bin/python3 -I
"""mike-root: run one root step that the person approved in Mike.

Installed by `mike-service install-root` (which asks the sudo password once)
as /usr/local/libexec/mike-root, owned by root, with a single sudoers line
that lets the service's user run exactly this program, with no arguments,
without a password. Nothing else becomes passwordless; installing,
uninstalling or anything with arguments still asks for the password.

This program runs nothing on its own word. It reads a token on stdin that
Mike's server signed (Ed25519) after the person said yes, and runs the step
only if:

- the signature is the server's, checked with the public key fetched at
  install time into a root-owned file;
- the token is for root steps, has not expired, and lives at most a few
  minutes;
- it names this device;
- its step id was never run before (a root-owned log).

Then it runs exactly the argv and folder the token carries, as root, and
nothing else: no argument from the caller reaches the command. Another
program of the same user can start mike-root, but cannot make a signature,
so it cannot become root through it.

Usage (the first two as root, through sudo):
  mike-root --install --host URL --device ID --user NAME
  mike-root --uninstall
  mike-root            < token
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HELPER = "/usr/local/libexec/mike-root"
CONFIG_DIR = "/etc/mike-root"
PUBLIC_KEY = CONFIG_DIR + "/server.pub"
DEVICE = CONFIG_DIR + "/device"
STATE_DIR = "/var/lib/mike-root"
USED = STATE_DIR + "/used"
SUDOERS = "/etc/sudoers.d/90-mike-root"
OPENSSL = "/usr/bin/openssl"
ROLE = "mike-root"
LONGEST_LIFE = 300  # seconds a token may live, whatever it says
PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# An Ed25519 public key as DER SubjectPublicKeyInfo is this prefix + 32 bytes.
ED25519_DER_PREFIX = bytes.fromhex("302a300506032b6570032100")


class Refused(Exception):
    pass


def b64decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_") + "=" * (-len(text) % 4))


def b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def read_root_file(path: str) -> str:
    """A file only root can have written: owned by root, not writable by others."""
    info = os.stat(path, follow_symlinks=False)
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise Refused(f"{path} is not a root-owned file")
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def verify_signature(public_key: bytes, data: bytes, signature: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    pem = (b"-----BEGIN PUBLIC KEY-----\n"
           + base64.encodebytes(ED25519_DER_PREFIX + public_key)
           + b"-----END PUBLIC KEY-----\n")
    with tempfile.TemporaryDirectory(prefix="mike-root-") as folder:
        paths = {}
        for name, content in (("key.pem", pem), ("data", data), ("sig", signature)):
            paths[name] = os.path.join(folder, name)
            with open(paths[name], "wb") as handle:
                handle.write(content)
        done = subprocess.run(
            [OPENSSL, "pkeyutl", "-verify", "-pubin", "-inkey", paths["key.pem"],
             "-rawin", "-in", paths["data"], "-sigfile", paths["sig"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={"PATH": PATH},
        )
        return done.returncode == 0


def check_token(token: str, public_key: bytes, device: str, now: float) -> dict:
    """The step a genuine, live token for this device carries; Refused otherwise."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise Refused("not a signed step")
    try:
        header = json.loads(b64url(parts[0]))
        claims = json.loads(b64url(parts[1]))
        signature = b64url(parts[2])
    except (ValueError, TypeError):
        raise Refused("not a signed step")
    # The algorithm is not the token's to choose: only EdDSA is ever checked.
    if not isinstance(header, dict) or header.get("alg") != "EdDSA":
        raise Refused("not an EdDSA-signed step")
    if not verify_signature(public_key, (parts[0] + "." + parts[1]).encode(), signature):
        raise Refused("the signature is not Mike's server's")
    if not isinstance(claims, dict) or claims.get("role") != ROLE:
        raise Refused("not a root step")
    issued, expires = claims.get("iat"), claims.get("exp")
    if not isinstance(issued, int) or not isinstance(expires, int):
        raise Refused("the step has no lifetime")
    if expires <= now or expires - issued > LONGEST_LIFE or issued > now + 60:
        raise Refused("the step has expired")
    try:
        step = json.loads(claims.get("sub", ""))
    except (ValueError, TypeError):
        raise Refused("the step is unreadable")
    if not isinstance(step, dict):
        raise Refused("the step is unreadable")
    argv, cwd, step_id = step.get("argv"), step.get("cwd"), step.get("step")
    if step.get("device") != device:
        raise Refused("the step is for another device")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
        raise Refused("the step has no command")
    if not isinstance(cwd, str) or not cwd or not isinstance(step_id, str) or not step_id:
        raise Refused("the step has no folder or id")
    return {"argv": argv, "cwd": cwd, "step": step_id, "exp": expires}


def claim_once(step_id: str, expires: int, now: float, path: str = USED) -> None:
    """Record the step as run; Refused if it ran before. Expired ids are dropped."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        kept = []
        for line in handle.read().splitlines():
            seen, _, until = line.partition(" ")
            if seen == step_id:
                raise Refused("this step already ran once")
            if until.isdigit() and int(until) > now:
                kept.append(line)
        kept.append(f"{step_id} {expires}")
        handle.seek(0)
        handle.truncate()
        handle.write("\n".join(kept) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def resolve_cwd(cwd: str) -> str:
    """`~` is the home of the person who started the service, not root's."""
    if cwd == "~" or cwd.startswith("~/"):
        name = os.environ.get("SUDO_USER", "")
        if not name:
            raise Refused("cannot tell whose home ~ is")
        cwd = pwd.getpwnam(name).pw_dir + cwd[1:]
    if not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise Refused(f"{cwd} is not a folder")
    return cwd


def run(stdin: str) -> int:
    if os.geteuid() != 0:
        raise Refused("mike-root runs through sudo")
    public_key = b64decode(read_root_file(PUBLIC_KEY))
    device = read_root_file(DEVICE)
    now = time.time()
    step = check_token(stdin, public_key, device, now)
    claim_once(step["step"], step["exp"], now)
    cwd = resolve_cwd(step["cwd"])
    os.chdir(cwd)
    env = {"PATH": PATH, "HOME": "/root", "LANG": os.environ.get("LANG", "C.UTF-8"), "TERM": "dumb"}
    os.execvpe(step["argv"][0], step["argv"], env)
    return 127  # not reached


def fetch_public_key(host: str) -> str:
    if not host.startswith("https://") and not host.startswith("http://127.0.0.1"):
        raise Refused("the Mike server must be https")
    body = json.dumps({"_class": "Open", "particle": {"_class": "RootPublicKey"}}).encode()
    request = urllib.request.Request(host.rstrip("/") + "/api/mike-interface", data=body,
                                     # Cloudflare refuses Python-urllib's own agent (1010).
                                     headers={"content-type": "application/json", "user-agent": "mike-root/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        answer = json.loads(response.read(64 * 1024))
    key = answer.get("public_key") if isinstance(answer, dict) else None
    if not isinstance(key, str) or len(b64decode(key)) != 32:
        raise Refused("the Mike server did not give a root signing key; root steps are not enabled there")
    return key


def write_root_file(path: str, text: str, mode: int) -> None:
    folder = os.path.dirname(path)
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".mike-root-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(text)
    os.chmod(temporary, mode)
    os.chown(temporary, 0, 0)
    os.replace(temporary, path)


def install(host: str, device: str, user: str) -> None:
    if os.geteuid() != 0:
        raise Refused("install through sudo")
    pwd.getpwnam(user)  # an unknown user is an error, not a rule for nobody
    if not device or any(c.isspace() for c in device):
        raise Refused("a device id is required")
    if not os.access(OPENSSL, os.X_OK):
        raise Refused("openssl is required to check signatures")
    key = fetch_public_key(host)
    os.makedirs(CONFIG_DIR, mode=0o755, exist_ok=True)
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(os.path.dirname(HELPER), mode=0o755, exist_ok=True)
    write_root_file(PUBLIC_KEY, key + "\n", 0o644)
    write_root_file(DEVICE, device + "\n", 0o644)
    with open(os.path.abspath(__file__), encoding="utf-8") as source:
        write_root_file(HELPER, source.read(), 0o755)
    # `""` lets the rule run mike-root with no arguments at all: --install and
    # --uninstall through it would hand root to whoever chooses the key, so
    # they keep needing the password.
    rule = f'{user} ALL=(root) NOPASSWD: {HELPER} ""\n'
    handle, temporary = tempfile.mkstemp(dir=os.path.dirname(SUDOERS), prefix=".mike-root-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(rule)
    os.chmod(temporary, 0o440)
    checked = subprocess.run(["visudo", "-cf", temporary], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             env={"PATH": PATH})
    if checked.returncode != 0:
        os.unlink(temporary)
        raise Refused("the sudoers line did not pass visudo: " + checked.stderr.decode(errors="replace"))
    os.replace(temporary, SUDOERS)
    print(f"mike-root: installed for {user} on device {device}; only {HELPER} is passwordless.")


def uninstall() -> None:
    if os.geteuid() != 0:
        raise Refused("uninstall through sudo")
    for path in (SUDOERS, HELPER, PUBLIC_KEY, DEVICE):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    print("mike-root: removed; nothing is passwordless any more.")


def main(args: list[str]) -> int:
    try:
        if args[:1] == ["--install"]:
            options = dict(zip(args[1::2], args[2::2]))
            install(options.get("--host", ""), options.get("--device", ""), options.get("--user", ""))
            return 0
        if args == ["--uninstall"]:
            uninstall()
            return 0
        if args:
            raise Refused("mike-root takes its step on stdin, and nothing else")
        return run(sys.stdin.read(16 * 1024))
    except Refused as refused:
        print(f"mike-root: {refused}; it did not run", file=sys.stderr)
        return 126
    except OSError as failed:
        print(f"mike-root: {failed}; it did not run", file=sys.stderr)
        return 126


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
