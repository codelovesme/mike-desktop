"""Exercise OpenFile's descriptor and Response signal against a private D-Bus.

Run explicitly with MIKE_TEST_PORTAL=1 under dbus-run-session. The normal
unittest suite skips this to avoid taking the real desktop portal's bus name.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
XML = """<node><interface name="org.freedesktop.portal.OpenURI">
  <method name="OpenFile">
    <arg type="s" direction="in"/><arg type="h" direction="in"/>
    <arg type="a{sv}" direction="in"/><arg type="o" direction="out"/>
  </method>
</interface></node>"""


@unittest.skipUnless(os.environ.get("MIKE_TEST_PORTAL") == "1", "requires a private test bus")
class PortalIpcTest(unittest.TestCase):
    def test_verified_descriptor_and_response(self):
        with tempfile.TemporaryDirectory(prefix="mike-fake-portal-") as temporary:
            folder = Path(temporary)
            candidate = folder / "invoice.txt"
            candidate.write_text("verified descriptor")
            received = []
            ready = threading.Event()
            loop = GLib.MainLoop()
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

            def method(connection, sender, _object, _interface, name, parameters, invocation):
                self.assertEqual(name, "OpenFile")
                _parent, index, options = parameters.unpack()
                handle = "/org/freedesktop/portal/desktop/request/" + sender.lstrip(":").replace(".", "_") + "/" + options["handle_token"]
                fd = invocation.get_message().get_unix_fd_list().get(index)
                try:
                    received.append(os.read(fd, 128))
                finally:
                    os.close(fd)
                invocation.return_value(GLib.Variant("(o)", (handle,)))

                def answer():
                    connection.emit_signal(None, handle, "org.freedesktop.portal.Request", "Response", GLib.Variant("(ua{sv})", (0, {})))
                    return False

                GLib.timeout_add(100, answer)

            info = Gio.DBusNodeInfo.new_for_xml(XML)

            def acquired(connection, _name):
                connection.register_object(
                    "/org/freedesktop/portal/desktop", info.interfaces[0], method, None, None,
                )
                ready.set()

            owner = Gio.bus_own_name(
                Gio.BusType.SESSION, "org.freedesktop.portal.Desktop",
                Gio.BusNameOwnerFlags.NONE, acquired, None, None,
            )
            thread = threading.Thread(target=loop.run, daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(5), "fake portal did not acquire the bus name")
                result = subprocess.run(
                    ["/usr/bin/python3", str(ROOT / "ui/secure_open.py"), str(folder), str(candidate)],
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(received, [b"verified descriptor"])
            finally:
                Gio.bus_unown_name(owner)
                loop.quit()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
