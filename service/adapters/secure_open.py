#!/usr/bin/python3
"""Open the exact user-reviewed file through Linux's document portal.

The candidate was found by the native Euglena companion inside a folder picked
by the person. This helper opens each path component beneath that folder with
O_NOFOLLOW and passes the resulting descriptor to OpenURI.OpenFile. Renaming or
replacing the pathname after this check cannot change what the portal gets.
"""

from __future__ import annotations

import os
from pathlib import PurePosixPath
import secrets
import stat
import sys


def open_verified(root: str, candidate: str) -> int:
    folder, file = PurePosixPath(root), PurePosixPath(candidate)
    if not folder.is_absolute() or not file.is_absolute():
        raise ValueError("absolute folder and file required")
    folder_parts = folder.parts[1:]
    file_parts = file.parts[1:]
    if not folder_parts or len(file_parts) <= len(folder_parts):
        raise ValueError("file is not beneath the chosen folder")
    if file_parts[:len(folder_parts)] != folder_parts:
        raise ValueError("file is outside the chosen folder")
    if any(part in ("", ".", "..") for part in folder_parts + file_parts):
        raise ValueError("path traversal is not allowed")

    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in folder_parts + file_parts[len(folder_parts):-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        descriptor = os.open(file_parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("candidate is not a regular file")
        return descriptor
    finally:
        os.close(directory)


def open_in_portal(descriptor: int) -> None:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    sender = connection.get_unique_name().lstrip(":").replace(".", "_")
    token = "mike" + secrets.token_hex(12)
    expected = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    result = {"code": None}
    loop = GLib.MainLoop()

    def answered(_connection, _sender, _path, _interface, _signal, parameters):
        result["code"] = parameters.unpack()[0]
        if loop.is_running():
            loop.quit()

    subscription = connection.signal_subscribe(
        "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request", "Response",
        expected, None, Gio.DBusSignalFlags.NONE, answered,
    )
    fd_list = Gio.UnixFDList.new()
    index = fd_list.append(descriptor)
    options = {"handle_token": GLib.Variant("s", token), "writable": GLib.Variant("b", False)}
    parameters = GLib.Variant("(sha{sv})", ("", index, options))
    try:
        reply, _fds = connection.call_with_unix_fd_list_sync(
            "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.OpenURI", "OpenFile", parameters,
            GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE, 15000, fd_list, None,
        )
        handle = reply.unpack()[0]
        if handle != expected:
            connection.signal_unsubscribe(subscription)
            subscription = connection.signal_subscribe(
                "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request", "Response",
                handle, None, Gio.DBusSignalFlags.NONE, answered,
            )
        if result["code"] is None:
            GLib.timeout_add_seconds(120, lambda: loop.quit() or False)
            loop.run()
        if result["code"] is None:
            try:
                connection.call_sync(
                    "org.freedesktop.portal.Desktop", handle,
                    "org.freedesktop.portal.Request", "Close", None, None,
                    Gio.DBusCallFlags.NONE, 5000, None,
                )
            except GLib.Error:
                pass
            raise RuntimeError("document portal did not answer")
        if result["code"] != 0:
            raise RuntimeError("file opening was cancelled or unavailable")
    finally:
        connection.signal_unsubscribe(subscription)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("secure_open expects a chosen folder and reviewed file")
    root, candidate = sys.argv[1:]
    try:
        descriptor = open_verified(root, candidate)
        try:
            # The live end-to-end runner replaces only the portal's OS effect.
            # It still has to pass the descriptor-based scope check above.
            log = os.environ.get("MIKE_OPEN_LOG")
            if log:
                with open(log, "w", encoding="utf-8") as record:
                    record.write(candidate + "\n")
            else:
                open_in_portal(descriptor)
        finally:
            os.close(descriptor)
    except Exception as error:
        print(f"Mike could not open the reviewed file: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
