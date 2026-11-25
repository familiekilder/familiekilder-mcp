"""Single-instance locking, marker reuse, busy ports, opener isolation and hard-stop reopen.

Every test runs against a temporary data root, temporary home folder and a free
loopback port (never port 28417), with webbrowser.open patched out.
"""

from __future__ import annotations

import contextlib
import errno
from http.client import HTTPConnection, HTTPException
import io
import json
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from familiekilder import core, private_lock, setup_page, setup_web, source_cli
from familiekilder.private_state import atomic_write_private_json, read_private_json

URL_PATTERN = re.compile(r"Åpne lokalt oppsett: (http://127\.0\.0\.1:(\d+))")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def close_wizard(port: int) -> None:
    """Close a running wizard cleanly via GET / (for token) + POST /api/close."""

    origin = f"http://127.0.0.1:{port}"
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise AssertionError(f"GET / returned {response.status}")
        match = re.search(rb'<meta name="setup-token" content="([^"]+)"', body)
        if not match:
            raise AssertionError("setup token missing from /")
        token = match.group(1).decode("ascii")
        connection.request(
            "POST",
            "/api/close",
            body=b"{}",
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": origin,
                "X-Setup-Token": token,
                "Content-Type": "application/json",
            },
        )
        close_response = connection.getresponse()
        close_response.read()
        if close_response.status != 200:
            raise AssertionError(f"POST /api/close returned {close_response.status}")
    finally:
        connection.close()


def probe_status(port: int, *, instance_id: str | None = None) -> tuple[int, bytes]:
    headers = {"Host": f"127.0.0.1:{port}"}
    if instance_id is not None:
        headers["X-Setup-Instance"] = instance_id
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", setup_page.STATUS_PATH, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def wait_for_status(port: int, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = probe_status(port)
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"server on 127.0.0.1:{port} did not answer /status within {timeout}s")


def wait_for_marker(root: Path, *, timeout: float = 10.0) -> dict:
    marker = setup_web.marker_path(root)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                return read_private_json(marker)
            except Exception:
                pass
        time.sleep(0.05)
    raise AssertionError(f"marker {marker} did not appear within {timeout}s")


_SUBPROCESS_LAUNCHER = """
import json, sys, webbrowser
from pathlib import Path
from familiekilder import setup_page, setup_web

setup_page.SETUP_PORT = int(sys.argv[1])
root = Path(sys.argv[2])
port_arg = None if sys.argv[3] == "none" else int(sys.argv[3])
open_browser = sys.argv[4] == "1"
local = sys.argv[5] == "1"
log_file = sys.argv[6]

def fake_open(url):
    if log_file:
        with open(log_file, "a", encoding="utf-8") as handle:
            handle.write(url + "\\n")
    return True

webbrowser.open = fake_open
raise SystemExit(
    setup_web.serve(root, port=port_arg, open_browser=open_browser, local=local)
)
"""


def spawn_wizard(
    fixed_port: int,
    root: Path,
    home: Path,
    *,
    port: int | None = None,
    open_browser: bool = False,
    local: bool = False,
    browser_log: Path | None = None,
) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SUBPROCESS_LAUNCHER,
            str(fixed_port),
            str(root),
            "none" if port is None else str(port),
            "1" if open_browser else "0",
            "1" if local else "0",
            "" if browser_log is None else str(browser_log),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def wait_for_printed_port(
    process: subprocess.Popen[str], *, timeout: float = 10.0
) -> tuple[int, list[str]]:
    lines: list[str] = []
    found: queue.Queue[int] = queue.Queue()

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line)
            match = URL_PATTERN.search(line)
            if match:
                found.put(int(match.group(2)))
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        port = found.get(timeout=timeout)
    except queue.Empty:
        stderr = process.stderr.read() if process.stderr and process.poll() is not None else ""
        raise AssertionError(
            f"wizard did not print local URL within {timeout}s; stdout={lines!r} stderr={stderr!r}"
        ) from None
    return port, lines


class SetupReopenCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "data"
        self.home = self.base / "home"
        self.home.mkdir()
        self.fixed_port = free_port()
        self.opened: list[str] = []
        self._processes: list[subprocess.Popen[str]] = []
        self.addCleanup(self._cleanup_processes)

    def _cleanup_processes(self) -> None:
        for process in self._processes:
            if process.poll() is None:
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()

    def track(self, process: subprocess.Popen[str]) -> subprocess.Popen[str]:
        self._processes.append(process)
        return process


class ServeLifecycleAndOrderTests(SetupReopenCase):
    def test_startup_order_lock_delete_marker_renew_bind_write_marker_and_cleanup(self) -> None:
        events: list[str] = []
        self.root.mkdir()
        marker = setup_web.marker_path(self.root)
        atomic_write_private_json(
            marker,
            {
                "port": free_port(),
                "instance": "stale-old-instance",
                "protocol": 1,
                "code_sha256": "0" * 64,
            },
        )
        real_renew = setup_web.refresh_stored_defaults
        real_server_cls = setup_web.SetupServer

        def traced_renew(root: Path):
            self.assertFalse(marker.exists(), "old marker must be deleted before renewal")
            # Confirm the setup lock is already held by another thread failing to acquire it.
            busy_check: list[bool] = []

            def probe_lock() -> None:
                try:
                    with private_lock.private_named_lock(root, "setup", wait=False):
                        busy_check.append(False)
                except private_lock.PrivateLockBusy:
                    busy_check.append(True)

            t = threading.Thread(target=probe_lock)
            t.start()
            t.join(timeout=5)
            self.assertEqual(busy_check, [True], "setup lock must be held before renewal")
            events.append("renew")
            return real_renew(root)

        class TracedServer(real_server_cls):
            def server_bind(self) -> None:
                events.append("bind")
                super().server_bind()

        opened_event = threading.Event()

        def fake_open(url: str) -> bool:
            self.opened.append(url)
            opened_event.set()
            return True

        stdout = io.StringIO()
        exit_codes: list[int] = []

        def run_serve() -> None:
            with contextlib.redirect_stdout(stdout):
                exit_codes.append(
                    setup_web.serve(self.root, port=None, open_browser=True, local=False)
                )

        with (
            patch.object(setup_page, "SETUP_PORT", self.fixed_port),
            patch.object(setup_web, "refresh_stored_defaults", side_effect=traced_renew),
            patch.object(setup_web, "SetupServer", TracedServer),
            patch("webbrowser.open", side_effect=fake_open),
        ):
            thread = threading.Thread(target=run_serve, daemon=True)
            thread.start()
            self.assertTrue(opened_event.wait(timeout=10))
            marker_data = wait_for_marker(self.root)
            self.assertEqual(set(marker_data), {"port", "instance", "protocol", "code_sha256"})
            self.assertEqual(marker_data["port"], self.fixed_port)
            self.assertEqual(marker_data["protocol"], setup_page.STATUS_PROTOCOL)
            self.assertNotIn(str(self.root), json.dumps(marker_data))
            self.assertNotIn(str(self.home), json.dumps(marker_data))
            if os.name != "nt":
                self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.opened, [setup_page.PAGES_URL])
            close_wizard(self.fixed_port)
            thread.join(timeout=10)

        self.assertEqual(exit_codes, [0])
        self.assertEqual(events, ["renew", "bind"])
        self.assertFalse(marker.exists(), "marker must be deleted on clean exit")
        printed = stdout.getvalue()
        self.assertIn(f"Åpne lokalt oppsett: http://127.0.0.1:{self.fixed_port}", printed)
        self.assertIn(setup_page.PAGES_URL, printed)

    def test_reuse_never_renews_and_respects_local_flag(self) -> None:
        stdout1 = io.StringIO()
        opened: list[str] = []

        with (
            patch.object(setup_page, "SETUP_PORT", self.fixed_port),
            patch("webbrowser.open", side_effect=lambda url: opened.append(url) or True),
        ):
            thread = threading.Thread(
                target=lambda: self._run_into(stdout1, self.root, open_browser=False),
                daemon=True,
            )
            thread.start()
            wait_for_status(self.fixed_port)
            wait_for_marker(self.root)
            try:
                # Default reuse on fixed port opens Pages and never renews defaults.
                stdout2 = io.StringIO()
                with (
                    patch.object(
                        setup_web,
                        "refresh_stored_defaults",
                        side_effect=AssertionError("reuse must never renew defaults"),
                    ),
                    contextlib.redirect_stdout(stdout2),
                ):
                    code = setup_web.serve(self.root, port=None, open_browser=True, local=False)
                self.assertEqual(code, 0)
                self.assertEqual(opened, [setup_page.PAGES_URL])
                self.assertIn(
                    f"Åpne lokalt oppsett: http://127.0.0.1:{self.fixed_port}", stdout2.getvalue()
                )
                self.assertIn(setup_page.PAGES_URL, stdout2.getvalue())

                # Reuse with --local opens the local wizard URL directly.
                opened.clear()
                stdout3 = io.StringIO()
                with (
                    patch.object(
                        setup_web,
                        "refresh_stored_defaults",
                        side_effect=AssertionError("reuse must never renew defaults"),
                    ),
                    contextlib.redirect_stdout(stdout3),
                ):
                    code = setup_web.serve(self.root, port=None, open_browser=True, local=True)
                self.assertEqual(code, 0)
                self.assertEqual(opened, [f"http://127.0.0.1:{self.fixed_port}"])

                # --port 0 promises the local wizard, also when one is reused.
                opened.clear()
                with contextlib.redirect_stdout(io.StringIO()):
                    code = setup_web.serve(self.root, port=0, open_browser=True)
                self.assertEqual(code, 0)
                self.assertEqual(opened, [f"http://127.0.0.1:{self.fixed_port}"])
            finally:
                close_wizard(self.fixed_port)
                thread.join(timeout=10)

    def _run_into(self, stream: io.StringIO, root: Path, **kwargs) -> int:
        with contextlib.redirect_stdout(stream):
            return setup_web.serve(root, **kwargs)

    def test_changed_code_fingerprint_fails_reuse_with_explanation_and_keeps_running(self) -> None:
        # Also verify _code_fingerprint is deterministic and detects edits to .py and data/setup.
        fake_pkg = self.base / "pkg"
        (fake_pkg / "data" / "setup").mkdir(parents=True)
        (fake_pkg / "mod.py").write_text("x = 1\n", encoding="utf-8")
        (fake_pkg / "data" / "setup" / "setup.js").write_text("const a = 1;\n", encoding="utf-8")
        fp1 = setup_web._code_fingerprint(fake_pkg)
        self.assertEqual(fp1, setup_web._code_fingerprint(fake_pkg))
        (fake_pkg / "data" / "setup" / "setup.js").write_text("const a = 2;\n", encoding="utf-8")
        self.assertNotEqual(fp1, setup_web._code_fingerprint(fake_pkg))

        stdout1 = io.StringIO()
        with (
            patch.object(setup_page, "SETUP_PORT", self.fixed_port),
            patch("webbrowser.open", side_effect=AssertionError("must not open on mismatch")),
        ):
            thread = threading.Thread(
                target=lambda: self._run_into(stdout1, self.root, open_browser=False),
                daemon=True,
            )
            thread.start()
            wait_for_status(self.fixed_port)
            wait_for_marker(self.root)
            try:
                stderr = io.StringIO()
                with (
                    patch.object(setup_web, "_code_fingerprint", return_value="f" * 64),
                    patch.object(
                        setup_web,
                        "refresh_stored_defaults",
                        side_effect=AssertionError("must not renew"),
                    ),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = setup_web.serve(self.root, port=None, open_browser=True)
                self.assertEqual(code, 1)
                self.assertIn(f"http://127.0.0.1:{self.fixed_port}", stderr.getvalue())
                self.assertIn("Avslutt", stderr.getvalue())
                # Original wizard is still alive.
                self.assertEqual(probe_status(self.fixed_port)[0], 200)
            finally:
                close_wizard(self.fixed_port)
                thread.join(timeout=10)

    def test_explicit_port_different_from_running_wizard_exits_1(self) -> None:
        other_port = free_port()
        stdout1 = io.StringIO()
        with patch.object(setup_page, "SETUP_PORT", self.fixed_port):
            thread = threading.Thread(
                target=lambda: self._run_into(stdout1, self.root, open_browser=False),
                daemon=True,
            )
            thread.start()
            wait_for_status(self.fixed_port)
            wait_for_marker(self.root)
            try:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = setup_web.serve(self.root, port=other_port, open_browser=False)
                self.assertEqual(code, 1)
                self.assertIn(f"http://127.0.0.1:{self.fixed_port}", stderr.getvalue())
                self.assertEqual(probe_status(self.fixed_port)[0], 200)
            finally:
                close_wizard(self.fixed_port)
                thread.join(timeout=10)

    def test_stale_marker_when_another_root_owns_the_port_never_opens_other_root(self) -> None:
        root_a = self.base / "root-a"
        root_b = self.base / "root-b"
        stdout_b = io.StringIO()

        with patch.object(setup_page, "SETUP_PORT", self.fixed_port):
            thread_b = threading.Thread(
                target=lambda: self._run_into(stdout_b, root_b, open_browser=False),
                daemon=True,
            )
            thread_b.start()
            wait_for_status(self.fixed_port)
            marker_b = wait_for_marker(root_b)
            try:
                # Write a stale marker in root_a pointing to root_b's port with a stale instance id.
                root_a.mkdir()
                atomic_write_private_json(
                    setup_web.marker_path(root_a),
                    {
                        "port": self.fixed_port,
                        "instance": "stale-root-a-instance",
                        "protocol": setup_page.STATUS_PROTOCOL,
                        "code_sha256": setup_web._code_fingerprint(),
                    },
                )

                # Subcase 1: root_a's lock is held by a stuck process; reuse gets 409 from root_b
                # and NEVER opens root_b's wizard.
                stderr = io.StringIO()
                opened: list[str] = []
                lock_held = threading.Event()
                release_lock = threading.Event()

                def hold_a() -> None:
                    with private_lock.private_named_lock(root_a, "setup", wait=False):
                        lock_held.set()
                        release_lock.wait(10)

                holder = threading.Thread(target=hold_a, daemon=True)
                holder.start()
                self.assertTrue(lock_held.wait(5))
                try:
                    with (
                        patch.object(setup_web, "REUSE_WAIT_SECONDS", 0.35),
                        patch(
                            "webbrowser.open", side_effect=lambda url: opened.append(url) or True
                        ),
                        contextlib.redirect_stderr(stderr),
                    ):
                        code = setup_web.serve(root_a, port=None, open_browser=True)
                    self.assertEqual(code, 1)
                    self.assertEqual(opened, [])
                    self.assertEqual(
                        probe_status(self.fixed_port, instance_id=marker_b["instance"])[0], 200
                    )
                finally:
                    release_lock.set()
                    holder.join(timeout=5)

                # Subcase 2: lock free on root_a; root_a deletes stale marker, sees port busy,
                # binds a different free port and opens its own local address directly.
                stdout_a = io.StringIO()
                opened_a_event = threading.Event()

                def open_a(url: str) -> bool:
                    opened.append(url)
                    opened_a_event.set()
                    return True

                with (
                    patch("webbrowser.open", side_effect=open_a),
                    patch.object(setup_web, "WINDOWS_BIND_RETRY_SECONDS", 0.2),
                ):
                    thread_a = threading.Thread(
                        target=lambda: self._run_into(stdout_a, root_a, open_browser=True),
                        daemon=True,
                    )
                    thread_a.start()
                    self.assertTrue(opened_a_event.wait(timeout=10))
                    marker_a = wait_for_marker(root_a)
                    try:
                        self.assertNotEqual(marker_a["port"], self.fixed_port)
                        self.assertEqual(opened, [f"http://127.0.0.1:{marker_a['port']}"])
                        self.assertIn(setup_page.PAGES_URL, stdout_a.getvalue())
                        self.assertIn(
                            f"Åpne lokalt oppsett: http://127.0.0.1:{marker_a['port']}",
                            stdout_a.getvalue(),
                        )
                    finally:
                        close_wizard(marker_a["port"])
                        thread_a.join(timeout=10)
            finally:
                close_wizard(self.fixed_port)
                thread_b.join(timeout=10)

    def test_lost_close_response_still_shuts_down_and_releases_the_lock(self) -> None:
        real_reply = setup_web.Handler._reply

        def lose_close_response(handler, *args, **kwargs):
            if handler.path == "/api/close":
                raise BrokenPipeError("the tab is gone")
            return real_reply(handler, *args, **kwargs)

        with (
            patch.object(setup_page, "SETUP_PORT", self.fixed_port),
            patch.object(setup_web.Handler, "_reply", lose_close_response),
            patch.object(setup_web.SetupServer, "handle_error"),
        ):
            thread = threading.Thread(
                target=lambda: self._run_into(io.StringIO(), self.root, open_browser=False),
                daemon=True,
            )
            thread.start()
            wait_for_status(self.fixed_port)
            with contextlib.suppress(OSError, HTTPException):
                close_wizard(self.fixed_port)
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        with private_lock.private_named_lock(self.root, "setup"):
            pass

    def test_marker_with_an_instance_id_we_never_write_is_ignored(self) -> None:
        for instance in ("bl\u00e5b\u00e6r-\u2603", "has space", "short", "line\r\nbreak"):
            with self.subTest(instance=instance):
                marker = {
                    "port": 1,
                    "instance": instance,
                    "protocol": 1,
                    "code_sha256": "0" * 64,
                }
                self.root.mkdir(exist_ok=True)
                atomic_write_private_json(setup_web.marker_path(self.root), marker)
                self.assertIsNone(setup_web._read_marker(self.root))

    def test_closing_wizard_answers_503_and_waiting_starter_takes_over(self) -> None:
        release_close = threading.Event()
        entered_close = threading.Event()
        real_close = setup_web.SetupService.close

        def slow_close(service_self) -> None:
            if not entered_close.is_set():
                entered_close.set()
                release_close.wait(5)
            real_close(service_self)

        stdout1 = io.StringIO()
        stdout2 = io.StringIO()
        code2: list[int] = []

        with patch.object(setup_page, "SETUP_PORT", self.fixed_port):
            with patch.object(setup_web.SetupService, "close", slow_close):
                t1 = threading.Thread(
                    target=lambda: self._run_into(stdout1, self.root, open_browser=False),
                    daemon=True,
                )
                t1.start()
                wait_for_status(self.fixed_port)
                marker1 = wait_for_marker(self.root)

                closer = threading.Thread(target=close_wizard, args=(self.fixed_port,), daemon=True)
                closer.start()
                self.assertTrue(entered_close.wait(5))
                # While closing, /status must answer 503.
                self.assertEqual(
                    probe_status(self.fixed_port, instance_id=marker1["instance"])[0], 503
                )

                # Start second wizard while first is still closing; it must wait and take over.
                t2 = threading.Thread(
                    target=lambda: code2.append(
                        self._run_into(stdout2, self.root, open_browser=False)
                    ),
                    daemon=True,
                )
                t2.start()
                time.sleep(0.25)
                self.assertTrue(t2.is_alive())
                release_close.set()
                closer.join(timeout=5)
                t1.join(timeout=10)

            # Wait until Wizard 2 binds and writes a new marker with a new instance id.
            deadline = time.monotonic() + 10.0
            marker2 = None
            while time.monotonic() < deadline:
                candidate = setup_web._read_marker(self.root)
                if candidate and candidate["instance"] != marker1["instance"]:
                    marker2 = candidate
                    break
                time.sleep(0.05)
            self.assertIsNotNone(marker2, "second wizard should have taken over")
            try:
                assert marker2 is not None
                self.assertEqual(marker2["port"], self.fixed_port)
                self.assertEqual(
                    probe_status(self.fixed_port, instance_id=marker2["instance"])[0], 200
                )
            finally:
                close_wizard(self.fixed_port)
                t2.join(timeout=10)
        self.assertEqual(code2, [0])

    def test_busy_fixed_port_explicit_port_and_non_bind_eacces(self) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        busy = blocker.getsockname()[1]

        # Explicit --port N busy -> exit 1.
        stderr = io.StringIO()
        with (
            patch.object(setup_page, "SETUP_PORT", busy),
            contextlib.redirect_stderr(stderr),
        ):
            code = setup_web.serve(self.root, port=busy, open_browser=False)
        self.assertEqual(code, 1)
        self.assertIn(str(busy), stderr.getvalue())

        # Fixed port (port=None) busy -> fallback to free port, direct local open.
        opened: list[str] = []
        opened_event = threading.Event()
        stdout = io.StringIO()
        with (
            patch.object(setup_page, "SETUP_PORT", busy),
            patch.object(setup_web, "WINDOWS_BIND_RETRY_SECONDS", 0.2),
            patch(
                "webbrowser.open",
                side_effect=lambda url: (opened.append(url), opened_event.set(), True)[-1],
            ),
        ):
            thread = threading.Thread(
                target=lambda: self._run_into(stdout, self.root, port=None, open_browser=True),
                daemon=True,
            )
            thread.start()
            self.assertTrue(opened_event.wait(timeout=10))
            marker = wait_for_marker(self.root)
            try:
                self.assertNotEqual(marker["port"], busy)
                self.assertEqual(opened, [f"http://127.0.0.1:{marker['port']}"])
                self.assertIn(setup_page.PAGES_URL, stdout.getvalue())
            finally:
                close_wizard(marker["port"])
                thread.join(timeout=10)

        # Non-bind EACCES (e.g. from SetupService) must NOT be treated as port busy.
        with (
            patch.object(setup_page, "SETUP_PORT", self.fixed_port),
            patch.object(
                setup_web,
                "SetupService",
                side_effect=PermissionError(errno.EACCES, "cannot read data"),
            ),
        ):
            with self.assertRaises(PermissionError):
                setup_web.serve(self.root, port=None, open_browser=False)

    def test_slow_failing_and_false_browser_opener_never_block_or_stop_server(self) -> None:
        # Case 1: Slow opener — server already answers inside opener and while opener blocks.
        opener_started = threading.Event()
        release_opener = threading.Event()
        inside_status: list[int] = []

        def slow_open(url: str) -> bool:
            inside_status.append(probe_status(self.fixed_port)[0])
            opener_started.set()
            release_opener.wait(10)
            return True

        with (
            patch.object(setup_page, "SETUP_PORT", self.fixed_port),
            patch("webbrowser.open", side_effect=slow_open),
        ):
            stdout = io.StringIO()
            thread = threading.Thread(
                target=lambda: self._run_into(stdout, self.root, open_browser=True),
                daemon=True,
            )
            thread.start()
            self.assertTrue(opener_started.wait(timeout=10))
            try:
                self.assertEqual(inside_status, [200])
                # While webbrowser.open is still blocked, the server answers and closes cleanly.
                self.assertEqual(probe_status(self.fixed_port)[0], 200)
                close_wizard(self.fixed_port)
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            finally:
                release_opener.set()

        # Case 2 & 3: Opener raises or returns False — wizard stays up until closed.
        for behavior in (RuntimeError("opener failed"), lambda _url: False):
            with self.subTest(behavior=behavior):
                called = threading.Event()

                def run_open(url: str):
                    called.set()
                    if isinstance(behavior, Exception):
                        raise behavior
                    return behavior(url)

                with (
                    patch.object(setup_page, "SETUP_PORT", self.fixed_port),
                    patch("webbrowser.open", side_effect=run_open),
                ):
                    stdout = io.StringIO()
                    codes: list[int] = []
                    thread = threading.Thread(
                        target=lambda: codes.append(
                            self._run_into(stdout, self.root, open_browser=True)
                        ),
                        daemon=True,
                    )
                    thread.start()
                    self.assertTrue(called.wait(timeout=10))
                    try:
                        self.assertEqual(probe_status(self.fixed_port)[0], 200)
                    finally:
                        close_wizard(self.fixed_port)
                        thread.join(timeout=10)
                    self.assertEqual(codes, [0])

    def test_windows_bind_uses_exclusive_addruse_and_bounded_retry(self) -> None:
        # Simulate Windows socket option setup in server_bind without touching real Windows APIs.
        sock_calls: list[tuple[int, int, int]] = []
        fake_exclusive = 0x1005
        real_setsockopt = socket.socket.setsockopt

        def spy_setsockopt(self_sock, level, optname, value, *rest):
            sock_calls.append((level, optname, value))
            if optname == fake_exclusive:
                return None
            return real_setsockopt(self_sock, level, optname, value, *rest)

        with (
            patch.object(setup_web, "_is_windows", return_value=True),
            patch.object(setup_web.socket, "SO_EXCLUSIVEADDRUSE", fake_exclusive, create=True),
            patch.object(socket.socket, "setsockopt", spy_setsockopt),
        ):
            server = setup_web.SetupServer(self.root, port=0)
            try:
                self.assertFalse(server.allow_reuse_address)
                self.assertIn((socket.SOL_SOCKET, fake_exclusive, 1), sock_calls)
                self.assertNotIn((socket.SOL_SOCKET, socket.SO_REUSEADDR, 1), sock_calls)
            finally:
                server.server_close()

        # Simulate Windows bounded retry succeeding on the 3rd attempt.
        attempts = 0
        real_cls = setup_web.SetupServer

        def flaky_server(root, *, port=0, **kwargs):
            nonlocal attempts
            attempts += 1
            if port == self.fixed_port and attempts < 3:
                raise setup_web._PortBusyError(10048, "busy")
            return real_cls(root, port=port, **kwargs)

        with (
            patch.object(setup_web, "_is_windows", return_value=True),
            patch.object(setup_page, "SETUP_PORT", self.fixed_port),
            patch.object(setup_web, "WINDOWS_BIND_RETRY_SECONDS", 1.0),
            patch.object(setup_web, "WINDOWS_BIND_POLL_SECONDS", 0.02),
            patch.object(setup_web, "SetupServer", side_effect=flaky_server),
        ):
            server, fell_back = setup_web._bind_server(
                self.root, port=None, root_source=None, instance_id="inst-win-retry"
            )
            try:
                self.assertFalse(fell_back)
                self.assertEqual(server.server_port, self.fixed_port)
                self.assertEqual(attempts, 3)
            finally:
                server.server_close()

    def test_concurrent_private_directory_and_windows_lock_byte_initialization(self) -> None:
        # _ensure_private_directory tolerates another process creating .private simultaneously.
        private = self.root / ".private"
        self.root.mkdir()
        real_mkdir = Path.mkdir

        def racing_mkdir(path_self: Path, *args, **kwargs):
            if path_self == private:
                real_mkdir(path_self, *args, **kwargs)
                raise FileExistsError("created concurrently")
            return real_mkdir(path_self, *args, **kwargs)

        with patch.object(Path, "mkdir", racing_mkdir):
            core._ensure_private_directory(private)
        self.assertTrue(private.is_dir())

        # Windows lock-byte initialization tolerates another process locking byte 0 mid-write.
        lock_file = self.base / "win.lock"
        lock_file.write_bytes(b"")
        fake_msvcrt = types.SimpleNamespace(
            LK_LOCK=1,
            LK_NBLCK=2,
            LK_UNLCK=0,
            locking=lambda _fd, _mode, _nbytes: (_ for _ in ()).throw(PermissionError("locked")),
        )
        with (
            open(lock_file, "r+b") as handle,
            patch.object(private_lock.os, "name", "nt"),
            patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
            patch.object(private_lock.os, "write", side_effect=PermissionError("locked byte 0")),
        ):
            with self.assertRaises(private_lock.PrivateLockBusy):
                private_lock._acquire(handle, lock_file, "setup", wait=False, timeout=None)

    def test_cli_flags_default_port_none_and_local_forwarded(self) -> None:
        parser = source_cli.build_parser()
        defaults = parser.parse_args(["setup"])
        self.assertIsNone(defaults.port)
        self.assertFalse(defaults.local)
        self.assertFalse(defaults.no_open)

        with patch("familiekilder.setup_web.serve", return_value=0) as serve_mock:
            code = source_cli.main(
                ["--root", str(self.root), "setup", "--local", "--no-open", "--port", "0"]
            )
        self.assertEqual(code, 0)
        serve_mock.assert_called_once()
        self.assertEqual(serve_mock.call_args.kwargs["port"], 0)
        self.assertFalse(serve_mock.call_args.kwargs["open_browser"])
        self.assertTrue(serve_mock.call_args.kwargs["local"])


class SubprocessReopenTests(SetupReopenCase):
    """Real multi-process tests: concurrent empty-root startup and hard-stop with open sockets."""

    def test_two_processes_starting_concurrently_on_empty_root(self) -> None:
        empty_root = self.base / "brand-new-empty-root"
        self.assertFalse(empty_root.exists())
        browser_log = self.base / "opened-urls.txt"

        p1 = self.track(
            spawn_wizard(
                self.fixed_port, empty_root, self.home, open_browser=True, browser_log=browser_log
            )
        )
        p2 = self.track(
            spawn_wizard(
                self.fixed_port, empty_root, self.home, open_browser=True, browser_log=browser_log
            )
        )

        # Wait for one process to exit 0 (the reuser) while the other stays alive serving.
        deadline = time.monotonic() + 15.0
        reuser = None
        winner = None
        while time.monotonic() < deadline:
            if p1.poll() is not None:
                reuser, winner = p1, p2
                break
            if p2.poll() is not None:
                reuser, winner = p2, p1
                break
            time.sleep(0.05)
        self.assertIsNotNone(reuser, "one concurrent process must reuse the other and exit")
        assert reuser is not None and winner is not None
        reuser_out, reuser_err = reuser.communicate(timeout=5)
        self.assertEqual(reuser.returncode, 0, f"stdout={reuser_out!r} stderr={reuser_err!r}")
        self.assertIn(f"Åpne lokalt oppsett: http://127.0.0.1:{self.fixed_port}", reuser_out)
        self.assertIsNone(winner.poll(), "winner must remain running")
        wait_for_status(self.fixed_port)

        close_wizard(self.fixed_port)
        winner_out, winner_err = winner.communicate(timeout=10)
        self.assertEqual(winner.returncode, 0, f"stdout={winner_out!r} stderr={winner_err!r}")
        self.assertIn(f"Åpne lokalt oppsett: http://127.0.0.1:{self.fixed_port}", winner_out)
        self.assertFalse(setup_web.marker_path(empty_root).exists())

    def test_hard_kill_with_open_connection_and_immediate_reopen_on_same_port(self) -> None:
        p1 = self.track(spawn_wizard(self.fixed_port, self.root, self.home, open_browser=False))
        port1, _ = wait_for_printed_port(p1)
        self.assertEqual(port1, self.fixed_port)
        wait_for_status(self.fixed_port)
        marker1 = wait_for_marker(self.root)

        # A connection with an unfinished request, held open by the server.
        raw_socket = socket.create_connection(("127.0.0.1", self.fixed_port), timeout=5)
        self.addCleanup(raw_socket.close)
        raw_socket.sendall(b"GET /status HTTP/1.1\r\n")
        # Connections are accepted in order, so an answer here means that one was accepted.
        self.assertEqual(probe_status(self.fixed_port)[0], 200)

        # Hard-kill while the accepted connection is open.
        p1.kill()
        p1.wait(timeout=10)
        self.assertTrue(
            setup_web.marker_path(self.root).exists(),
            "hard kill leaves stale marker that the next start must clean up",
        )

        # Immediately reopen on the exact same port while the old client socket is still open.
        p2 = self.track(spawn_wizard(self.fixed_port, self.root, self.home, open_browser=False))
        port2, _ = wait_for_printed_port(p2, timeout=15.0)
        self.assertEqual(port2, self.fixed_port)
        wait_for_status(self.fixed_port)
        marker2 = wait_for_marker(self.root)
        self.assertEqual(marker2["port"], self.fixed_port)
        self.assertNotEqual(marker2["instance"], marker1["instance"])
        self.assertEqual(probe_status(self.fixed_port, instance_id=marker2["instance"])[0], 200)

        close_wizard(self.fixed_port)
        out2, err2 = p2.communicate(timeout=10)
        self.assertEqual(p2.returncode, 0, f"stdout={out2!r} stderr={err2!r}")
        self.assertFalse(setup_web.marker_path(self.root).exists())


if __name__ == "__main__":
    unittest.main()
