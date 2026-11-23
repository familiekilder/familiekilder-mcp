from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from familiekilder import browser_bootstrap
from familiekilder.browser_bootstrap import (
    BrowserBootstrapError,
    SPOND_LOGIN_URL,
    SPOND_ORIGIN,
    bootstrap_source,
    bootstrap_spond,
)
from familiekilder.private_state import atomic_write_private_json
from familiekilder.spond_auth import (
    API_ROOT as SPOND_API_ROOT,
    client_config_path as spond_client_config_path,
)


SPOND_ACCESS = "spond-access-secret"
SPOND_REFRESH = "spond-refresh-secret"


class FakeCdp:
    def __init__(
        self,
        origin: str,
        *,
        runtime_value: Any = None,
        matching_targets: int = 1,
        indexed_entries: list[Mapping[str, Any]] | None = None,
        has_more: bool = False,
        record_value: Any = None,
        events: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.origin = origin
        self.runtime_value = runtime_value
        self.matching_targets = matching_targets
        self.indexed_entries = indexed_entries
        self.has_more = has_more
        self.record_value = record_value
        self.events = list(events or [])
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 20.0,
    ) -> Mapping[str, Any]:
        copied = dict(params or {})
        self.calls.append((method, copied, session_id))
        if method == "Target.getTargets":
            targets = [
                {
                    "type": "page",
                    "url": f"{self.origin}/signed-in/{index}",
                    "targetId": f"target-{index}",
                }
                for index in range(self.matching_targets)
            ]
            targets.append(
                {
                    "type": "page",
                    "url": "brave://newtab",
                    "targetId": "irrelevant",
                }
            )
            return {"targetInfos": targets}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "value": self.runtime_value}}
        if method == "IndexedDB.enable":
            return {}
        if method in {"Network.enable", "Page.enable", "Target.setDiscoverTargets"}:
            return {}
        if method == "IndexedDB.requestData":
            return {
                "objectStoreDataEntries": list(self.indexed_entries or []),
                "hasMore": self.has_more,
            }
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "object", "value": self.record_value}}
        raise AssertionError(f"unexpected fake CDP method: {method}")

    def wait_for_event(
        self, methods: frozenset[str], *, timeout: float
    ) -> Mapping[str, Any] | None:
        while self.events:
            event = self.events.pop(0)
            if event.get("method") in methods:
                return event
        return None


class FakeBrowser:
    def __init__(self, cdp: FakeCdp, profile: Path, external: Path | None = None) -> None:
        self.cdp = cdp
        self.profile = profile
        self.closed = False
        nested = profile / "Bootstrap" / "IndexedDB"
        nested.mkdir(parents=True)
        (nested / "browser-state").write_bytes(b"not-a-secret")
        if external is not None:
            (profile / "outside-link").symlink_to(external)

    def close(self) -> None:
        self.closed = True


class BrowserBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _configure_spond(self) -> None:
        atomic_write_private_json(
            spond_client_config_path(self.root),
            {
                "api_root": SPOND_API_ROOT,
                "app_version": "4.62.0",
                "attachment_hosts": [],
            },
        )

    def _assert_bootstrap_empty(self) -> None:
        base = self.root / ".private" / "browser-bootstrap"
        self.assertTrue(base.is_dir())
        self.assertEqual(list(base.iterdir()), [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o700)

    def test_cdp_framer_handles_partial_and_multiple_nul_frames(self) -> None:
        framer = browser_bootstrap._CdpFramer()
        first = browser_bootstrap._CdpFramer.encode({"id": 1, "result": {}})
        second = browser_bootstrap._CdpFramer.encode(
            {"method": "Target.targetInfoChanged", "params": {}}
        )

        self.assertEqual(framer.feed(first[:5]), [])
        self.assertEqual(
            framer.feed(first[5:] + second),
            [
                {"id": 1, "result": {}},
                {"method": "Target.targetInfoChanged", "params": {}},
            ],
        )

    def test_pipe_client_reads_thousands_of_fragments_without_recursion(self):
        message = browser_bootstrap._CdpFramer.encode({'id': 1, 'result': {'value': 'x' * 2000}})
        # The end of the pipe comes last. Windows reads in a thread from the
        # start, so the client is made with os.read already replaced.
        fragments = [bytes([byte]) for byte in message] + [b'']
        with (
            patch.object(browser_bootstrap.select, 'select', return_value=([11], [], [])),
            patch.object(browser_bootstrap.os, 'read', side_effect=fragments),
            patch.object(browser_bootstrap.time, 'monotonic', return_value=1),
        ):
            client = browser_bootstrap._PipeCdpClient(10, 11)
            result = client._next_message(2)
        self.assertEqual(result['result']['value'], 'x' * 2000)

    def test_pipe_client_ignores_events_and_matches_one_response(self) -> None:
        request_read, client_write = os.pipe()
        client_read, response_write = os.pipe()
        client = browser_bootstrap._PipeCdpClient(client_write, client_read)

        def server() -> None:
            request = bytearray()
            while b"\0" not in request:
                request.extend(os.read(request_read, 4096))
            decoded = json.loads(bytes(request).split(b"\0", 1)[0])
            event = browser_bootstrap._CdpFramer.encode(
                {"method": "Target.targetCreated", "params": {}}
            )
            response = browser_bootstrap._CdpFramer.encode(
                {"id": decoded["id"], "result": {"ok": True}}
            )
            os.write(response_write, event + response[:3])
            os.write(response_write, response[3:])
            os.close(request_read)
            os.close(response_write)

        worker = threading.Thread(target=server)
        worker.start()
        try:
            self.assertEqual(client.call("Browser.getVersion"), {"ok": True})
        finally:
            client.close()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_pipe_client_skips_the_late_answer_to_a_timed_out_request(self) -> None:
        request_read, client_write = os.pipe()
        client_read, response_write = os.pipe()
        client = browser_bootstrap._PipeCdpClient(client_write, client_read)
        requests: list[int] = []

        def server() -> None:
            buffer = bytearray()
            while len(requests) < 2:
                buffer.extend(os.read(request_read, 4096))
                while b"\0" in buffer:
                    raw, _, rest = bytes(buffer).partition(b"\0")
                    buffer = bytearray(rest)
                    requests.append(json.loads(raw)["id"])
                    if len(requests) == 2:
                        # The first answer arrives late, after the second request.
                        for request_id, value in ((requests[0], "late"), (requests[1], "now")):
                            os.write(
                                response_write,
                                browser_bootstrap._CdpFramer.encode(
                                    {"id": request_id, "result": {"answer": value}}
                                ),
                            )
            os.close(request_read)
            os.close(response_write)

        worker = threading.Thread(target=server)
        worker.start()
        try:
            with self.assertRaisesRegex(BrowserBootstrapError, "tidsgrensen"):
                client.call("Target.getTargets", timeout=0.05)
            self.assertEqual(client.call("Target.getTargets"), {"answer": "now"})
        finally:
            client.close()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_brave_arguments_use_private_pipe_and_disable_persistent_features(self) -> None:
        executable = Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
        profile = self.root / ".private" / "browser-bootstrap" / f"run-{'a' * 32}"

        arguments = browser_bootstrap._brave_arguments(executable, profile, SPOND_LOGIN_URL)

        self.assertIn("--remote-debugging-pipe", arguments)
        self.assertFalse(
            any(argument.startswith("--remote-debugging-port") for argument in arguments)
        )
        self.assertIn(f"--user-data-dir={profile}", arguments)
        self.assertIn("--profile-directory=Bootstrap", arguments)
        self.assertIn("--disable-sync", arguments)
        self.assertIn("--disable-save-password-bubble", arguments)
        self.assertIn("--disable-password-generation", arguments)
        self.assertTrue(
            any(
                argument.startswith("--disable-features=")
                and "PasswordManagerEnabled" in argument
                and "BraveSync" in argument
                for argument in arguments
            )
        )
        self.assertFalse(any("password-store" in argument for argument in arguments))
        self.assertEqual(arguments[-1], SPOND_LOGIN_URL)

    def test_hosted_browser_uses_app_mode_without_an_address_bar(self) -> None:
        with patch.dict('os.environ', {'FAMILIEKILDER_HOSTED': '1'}):
            arguments = browser_bootstrap._brave_arguments(
                Path('/opt/brave'), self.root, SPOND_LOGIN_URL
            )
        self.assertIn('--app=' + SPOND_LOGIN_URL, arguments)
        self.assertIn('--window-size=1280,800', arguments)
        self.assertIn('--window-position=0,0', arguments)
        self.assertFalse(any(arg.startswith('--force-device-scale-factor') for arg in arguments))
        self.assertNotIn('--start-maximized', arguments)
        self.assertNotIn('--new-window', arguments)
        self.assertNotIn('--no-sandbox', arguments)
        self.assertIn('--remote-debugging-pipe', arguments)

    def test_hosted_window_size_rejects_arbitrary_values_without_affecting_local_browser(
        self,
    ) -> None:
        for value in ('', '99999,99999', '420,840', '500x840', 'private-value'):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {
                        'FAMILIEKILDER_HOSTED': '1',
                        'FAMILIEKILDER_HOSTED_BROWSER_SIZE': value,
                    },
                ),
            ):
                with self.assertRaisesRegex(BrowserBootstrapError, 'Ugyldig størrelse'):
                    browser_bootstrap._brave_arguments(
                        Path('/opt/brave'), self.root, SPOND_LOGIN_URL
                    )
                os.environ['FAMILIEKILDER_HOSTED'] = '0'
                arguments = browser_bootstrap._brave_arguments(
                    Path('/opt/brave'), self.root, SPOND_LOGIN_URL
                )
                self.assertIn('--new-window', arguments)
                self.assertEqual(arguments[-1], SPOND_LOGIN_URL)
                self.assertFalse(any(arg.startswith('--window-') for arg in arguments))

    def test_oauth_callback_is_captured_without_persisting_profile(self) -> None:
        callback = "app://ch-parent-android.vigilo.no?code=secret&state=private"
        cdp = FakeCdp(
            "https://login.example",
            events=[
                {
                    "method": "Page.frameRequestedNavigation",
                    "params": {"url": "https://irrelevant.example/"},
                },
                {
                    "method": "Page.frameRequestedNavigation",
                    "params": {"url": callback},
                },
            ],
        )
        browsers: list[FakeBrowser] = []

        def factory(profile: Path, start_url: str, brave_path: Any) -> FakeBrowser:
            self.assertEqual(start_url, "https://login.example/start")
            browser = FakeBrowser(cdp, profile)
            browsers.append(browser)
            return browser

        captured = browser_bootstrap.capture_oauth_callback(
            self.root,
            authorization_url="https://login.example/start",
            expected_redirect_uri="app://ch-parent-android.vigilo.no",
            browser_factory=factory,
        )

        self.assertEqual(captured, callback)
        self.assertTrue(browsers[0].closed)
        self._assert_bootstrap_empty()

    def test_cancel_oauth_closes_browser_and_removes_temporary_profile(self) -> None:
        browsers = []

        def factory(profile, start_url, brave_path):
            browser = FakeBrowser(FakeCdp("https://login.example"), profile)
            browsers.append(browser)
            return browser

        with self.assertRaises(browser_bootstrap.BrowserBootstrapError):
            browser_bootstrap.capture_oauth_callback(
                self.root,
                authorization_url="https://login.example/start",
                expected_redirect_uri="app://ch-parent-android.vigilo.no",
                browser_factory=factory,
                cancelled=lambda: True,
            )
        self.assertTrue(browsers[0].closed)
        self._assert_bootstrap_empty()

    def test_blocked_navigation_diagnostic_does_not_change_callback(self) -> None:
        callback = 'com.cgi.oslomelding.foresatte://oauth?code=fixture'
        cdp = FakeCdp(
            'https://login.example',
            events=[
                {
                    'method': 'Page.frameNavigated',
                    'params': {
                        'frame': {
                            'url': 'chrome-error://chromewebdata/',
                            'unreachableUrl': 'https://example.test/private?state=fixture',
                        }
                    },
                },
                {'method': 'Page.frameRequestedNavigation', 'params': {'url': callback}},
            ],
        )

        def factory(profile, start_url, brave_path):
            return FakeBrowser(cdp, profile)

        with self.assertLogs('familiekilder.browser_bootstrap', level='WARNING') as captured:
            result = browser_bootstrap.capture_oauth_callback(
                self.root,
                authorization_url='https://login.example/start',
                expected_redirect_uri='com.cgi.oslomelding.foresatte://oauth',
                browser_factory=factory,
            )
        self.assertEqual(result, callback)
        self.assertIn('host=example.test', captured.output[0])
        self.assertNotIn('fixture', captured.output[0])
        self._assert_bootstrap_empty()

    def test_oauth_callback_is_captured_from_https_redirect_response(self) -> None:
        callback = "app://ch-parent-android.vigilo.no?code=secret&state=private"
        cdp = FakeCdp(
            "https://login.example",
            events=[
                {
                    "method": "Network.responseReceived",
                    "params": {
                        "response": {
                            "url": "https://auth.example/signin-idporten",
                            "headers": {"Location": callback},
                        }
                    },
                }
            ],
        )
        browsers: list[FakeBrowser] = []

        def factory(profile: Path, start_url: str, brave_path: Any) -> FakeBrowser:
            browser = FakeBrowser(cdp, profile)
            browsers.append(browser)
            return browser

        captured = browser_bootstrap.capture_oauth_callback(
            self.root,
            authorization_url="https://login.example/start",
            expected_redirect_uri="app://ch-parent-android.vigilo.no",
            browser_factory=factory,
        )

        self.assertEqual(captured, callback)
        self.assertIn(
            ("Network.enable", {}, "session-1"),
            cdp.calls,
        )
        self.assertTrue(browsers[0].closed)
        self._assert_bootstrap_empty()

    def test_oauth_callback_rejects_ambiguous_location_headers(self) -> None:
        self.assertIsNone(
            browser_bootstrap._header_location(
                {
                    "Location": "app://ch-parent-android.vigilo.no?code=one",
                    "location": "app://ch-parent-android.vigilo.no?code=two",
                }
            )
        )

    def test_oauth_callback_rejects_wrong_redirect_without_exposing_it(self) -> None:
        self.assertFalse(
            browser_bootstrap._matches_callback(
                "app://wrong.example?code=secret",
                "app://ch-parent-android.vigilo.no",
            )
        )

    def test_oauth_callback_accepts_provider_root_slash_but_no_other_path(self) -> None:
        registered = "app://ch-parent-android.vigilo.no"
        self.assertTrue(
            browser_bootstrap._matches_callback(
                f"{registered}/?code=one&session_state=two&state=three", registered
            )
        )
        self.assertFalse(
            browser_bootstrap._matches_callback(f"{registered}/other?code=one", registered)
        )
        self.assertFalse(
            browser_bootstrap._matches_callback(
                "app://ch-parent-android.vigilo.no/?code=one",
                "app://ch-parent-android.vigilo.no/expected",
            )
        )

    def test_launcher_maps_devtools_pipe_to_child_fd_three_and_four(self) -> None:
        profile = browser_bootstrap._create_run_directory(self.root)
        child = r"""
import json
import os
import sys

if os.name == "nt":
    # Windows passes the two pipe handles in the last argument.
    import msvcrt

    handles = sys.argv[-1].split("=", 1)[1].split(",")
    read_fd = msvcrt.open_osfhandle(int(handles[0]), os.O_RDONLY)
    write_fd = msvcrt.open_osfhandle(int(handles[1]), 0)
else:
    read_fd, write_fd = 3, 4

pending = bytearray()
while True:
    chunk = os.read(read_fd, 4096)
    if not chunk:
        break
    pending.extend(chunk)
    while b'\0' in pending:
        raw, _, remainder = pending.partition(b'\0')
        pending = bytearray(remainder)
        request = json.loads(raw)
        response = {'id': request['id'], 'result': {'pipe_ok': True}}
        os.write(write_fd, json.dumps(response, separators=(',', ':')).encode() + b'\0')
        if request['method'] == 'Browser.close':
            raise SystemExit(0)
"""
        with (
            patch.object(
                browser_bootstrap,
                "_official_brave_path",
                return_value=Path(sys.executable),
            ),
            patch.object(
                browser_bootstrap,
                "_brave_arguments",
                return_value=[sys.executable, "-c", child],
            ),
            patch.object(browser_bootstrap, "_require_display"),
        ):
            handle = browser_bootstrap._launch_brave(profile, SPOND_LOGIN_URL, None)
            try:
                self.assertEqual(handle.cdp.call("Browser.getVersion"), {"pipe_ok": True})
            finally:
                handle.close()
                browser_bootstrap._cleanup_run_directory(self.root, profile)

        self._assert_bootstrap_empty()

    def test_malformed_cdp_frame_does_not_echo_secret(self) -> None:
        secret = "do-not-echo-this-token"
        framer = browser_bootstrap._CdpFramer()

        with self.assertRaises(BrowserBootstrapError) as raised:
            framer.feed(f'{{"token":"{secret}",}}\0'.encode())

        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))

    def test_spond_bootstrap_reads_only_exact_keys_closes_then_imports_and_cleans(self) -> None:
        self._configure_spond()
        cdp = FakeCdp(
            SPOND_ORIGIN,
            runtime_value={
                "token": SPOND_ACCESS,
                "tokenExpiration": "2033-05-18T04:33:20+00:00",
                "refreshToken": SPOND_REFRESH,
                "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
            },
        )
        external = self.root / "must-survive"
        external.write_text("outside", encoding="utf-8")
        holder: dict[str, Any] = {}

        def factory(profile: Path, url: str, brave_path: Path | str | None) -> FakeBrowser:
            self.assertEqual(url, SPOND_LOGIN_URL)
            self.assertIsNone(brave_path)
            self.assertEqual(
                profile.parent,
                (self.root / ".private" / "browser-bootstrap").resolve(),
            )
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o700)
            browser = FakeBrowser(cdp, profile, external)
            holder["browser"] = browser
            return browser

        def importer(
            root: Path,
            session_file: Path,
            *,
            replace_existing: bool,
        ) -> Mapping[str, Any]:
            browser = holder["browser"]
            self.assertTrue(browser.closed)
            self.assertEqual(root, self.root)
            self.assertTrue(replace_existing)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(session_file.stat().st_mode), 0o600)
            imported = json.loads(session_file.read_text(encoding="utf-8"))
            self.assertEqual(
                imported,
                {
                    "token": SPOND_ACCESS,
                    "tokenExpiration": "2033-05-18T04:33:20+00:00",
                    "refreshToken": SPOND_REFRESH,
                    "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
                },
            )
            return {
                "configured": True,
                "authenticated": True,
                "remote_verified": True,
                "expires_at": "2033-05-18T04:33:20+00:00",
                "refresh_expires_at": "2033-05-19T04:33:20+00:00",
                "attachment_hosts": 0,
                "refresh_marker_cleanup_pending": False,
                "unexpected_secret": SPOND_ACCESS,
            }

        prompts: list[str] = []
        result = bootstrap_spond(
            self.root,
            replace_existing=True,
            wait_for_user=prompts.append,
            browser_factory=factory,
            importer=importer,
        )

        self.assertEqual(result["source"], "spond")
        self.assertNotIn("unexpected_secret", result)
        self.assertNotIn(SPOND_ACCESS, repr(result))
        self.assertEqual(len(prompts), 1)
        self.assertTrue(external.exists())
        self._assert_bootstrap_empty()
        evaluate = next(call for call in cdp.calls if call[0] == "Runtime.evaluate")
        expression = evaluate[1]["expression"]
        for key in browser_bootstrap._SPOND_STORAGE_KEYS:
            self.assertIn(f'"{key}"', expression)
        self.assertNotIn("Object.keys", expression)
        self.assertNotIn("cookie", expression.lower())
        self.assertEqual(evaluate[2], "session-1")

    def test_spond_bootstrap_rejects_missing_or_malformed_profile_before_browser(self) -> None:
        calls: list[Path] = []

        def factory(profile: Path, url: str, path: Path | str | None) -> FakeBrowser:
            calls.append(profile)
            raise AssertionError("browser must not start")

        with self.assertRaisesRegex(BrowserBootstrapError, "providerprofilen"):
            bootstrap_spond(
                self.root,
                wait_for_user=lambda _prompt: None,
                browser_factory=factory,
                importer=lambda *args, **kwargs: {},
            )
        self.assertEqual(calls, [])
        self.assertFalse((self.root / ".private" / "browser-bootstrap").exists())

        atomic_write_private_json(
            spond_client_config_path(self.root),
            {
                "api_root": "https://attacker.invalid/core/v1",
                "app_version": "4.62.0",
                "attachment_hosts": [],
            },
        )
        with self.assertRaisesRegex(BrowserBootstrapError, "providerprofilen"):
            bootstrap_spond(
                self.root,
                wait_for_user=lambda _prompt: None,
                browser_factory=factory,
                importer=lambda *args, **kwargs: {},
            )
        self.assertEqual(calls, [])
        self.assertFalse((self.root / ".private" / "browser-bootstrap").exists())

    def test_spond_unknown_layout_fails_closed_redacts_and_cleans(self) -> None:
        self._configure_spond()
        secret = "layout-secret-must-not-escape"
        cdp = FakeCdp(
            SPOND_ORIGIN,
            runtime_value={
                "token": secret,
                "tokenExpiration": "2033-05-18T04:33:20+00:00",
                "refreshToken": SPOND_REFRESH,
                "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
                "password": "must-never-be-read",
            },
        )
        holder: dict[str, FakeBrowser] = {}

        def factory(profile: Path, url: str, brave_path: Path | str | None) -> FakeBrowser:
            browser = FakeBrowser(cdp, profile)
            holder["browser"] = browser
            return browser

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(BrowserBootstrapError) as raised:
                bootstrap_spond(
                    self.root,
                    wait_for_user=lambda prompt: None,
                    browser_factory=factory,
                    importer=lambda *args, **kwargs: {},
                )

        self.assertTrue(holder["browser"].closed)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))
        self.assertNotIn(secret, output.getvalue())
        self._assert_bootstrap_empty()

    def test_spond_duplicate_matching_page_fails_before_storage_read(self) -> None:
        self._configure_spond()
        cdp = FakeCdp(SPOND_ORIGIN, matching_targets=2)

        with self.assertRaises(BrowserBootstrapError):
            bootstrap_spond(
                self.root,
                wait_for_user=lambda prompt: None,
                browser_factory=lambda profile, url, path: FakeBrowser(cdp, profile),
                importer=lambda *args, **kwargs: {},
            )

        self.assertEqual([call[0] for call in cdp.calls], ["Target.getTargets"])
        self._assert_bootstrap_empty()

    def test_import_failure_is_redacted_and_profile_is_removed(self) -> None:
        self._configure_spond()
        cdp = FakeCdp(
            SPOND_ORIGIN,
            runtime_value={
                "token": SPOND_ACCESS,
                "tokenExpiration": "2033-05-18T04:33:20+00:00",
                "refreshToken": SPOND_REFRESH,
                "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
            },
        )

        def failing_importer(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
            raise RuntimeError(f"provider echoed {SPOND_ACCESS}")

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(BrowserBootstrapError) as raised:
                bootstrap_spond(
                    self.root,
                    wait_for_user=lambda prompt: None,
                    browser_factory=lambda profile, url, path: FakeBrowser(cdp, profile),
                    importer=failing_importer,
                )

        self.assertNotIn(SPOND_ACCESS, str(raised.exception))
        self.assertNotIn(SPOND_ACCESS, repr(raised.exception))
        self.assertNotIn(SPOND_ACCESS, output.getvalue())
        self._assert_bootstrap_empty()

    def test_cleanup_refuses_unmarked_or_out_of_scope_directory(self) -> None:
        run_path = browser_bootstrap._create_run_directory(self.root)
        (run_path / browser_bootstrap._OWNER_MARKER).unlink()
        with self.assertRaises(BrowserBootstrapError):
            browser_bootstrap._cleanup_run_directory(self.root, run_path)
        self.assertTrue(run_path.exists())

        outside = self.root / ".private" / f"run-{'a' * 32}"
        with self.assertRaises(BrowserBootstrapError):
            browser_bootstrap._cleanup_run_directory(self.root, outside)

    def test_dispatch_rejects_unknown_source_without_creating_private_state(self) -> None:
        with self.assertRaises(BrowserBootstrapError):
            bootstrap_source(self.root, "unknown")
        self.assertFalse((self.root / ".private").exists())


SPOND_FIELDS = {
    "token": SPOND_ACCESS,
    "tokenExpiration": "2033-05-18T04:33:20+00:00",
    "refreshToken": SPOND_REFRESH,
    "refreshTokenExpiration": "2033-05-19T04:33:20+00:00",
}


class LoginCdp(FakeCdp):
    """A Spond window the user signs in to while the bootstrap waits.

    `probe_answers` are the page's answers to the presence check, in order;
    the last one repeats. An answer that is an exception is raised instead.
    `extractions` are the stored fields the full read sees, in order.
    """

    def __init__(
        self,
        *,
        probe_answers: list[Any],
        extractions: list[Any] | None = None,
        pages: list[list[str]] | None = None,
    ) -> None:
        super().__init__(SPOND_ORIGIN)
        self.probe_expression = browser_bootstrap._spond_probe().expression
        self.probe_answers = list(probe_answers)
        self.extractions = list(extractions or [dict(SPOND_FIELDS)])
        # The page URLs each Target.getTargets sees, in order; the last repeats.
        self.pages = list(pages or [[f"{SPOND_ORIGIN}/client"]])
        self.probes = 0
        self.reads = 0

    @staticmethod
    def _next(values: list[Any]) -> Any:
        return values.pop(0) if len(values) > 1 else values[0]

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 20.0,
    ) -> Mapping[str, Any]:
        self.calls.append((method, dict(params or {}), session_id))
        if method == "Target.getTargets":
            urls = self._next(self.pages)
            if isinstance(urls, BaseException):
                raise urls
            return {
                "targetInfos": [
                    {"type": "page", "url": url, "targetId": f"target-{index}"}
                    for index, url in enumerate(urls)
                ]
            }
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "Runtime.evaluate":
            if params["expression"] == self.probe_expression:
                self.probes += 1
                answer = self._next(self.probe_answers)
                if isinstance(answer, BaseException):
                    raise answer
                return {"result": {"type": "boolean", "value": answer}}
            self.reads += 1
            return {"result": {"type": "object", "value": self._next(self.extractions)}}
        raise AssertionError(f"unexpected fake CDP method: {method}")


def imported_metadata(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
    return {
        "configured": True,
        "authenticated": True,
        "remote_verified": True,
        "expires_at": "2033-05-18T04:33:20+00:00",
        "refresh_expires_at": "2033-05-19T04:33:20+00:00",
        "attachment_hosts": 0,
        "refresh_marker_cleanup_pending": False,
    }


def wait_until_ready(limit: int = 20):
    """A wait that never gets a confirmation and only polls the check."""

    calls: list[str] = []

    def wait(prompt: str, ready) -> None:
        calls.append(prompt)
        for _ in range(limit):
            if ready():
                return
        raise AssertionError("the login was never detected")

    return wait, calls


class LoginDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        atomic_write_private_json(
            spond_client_config_path(self.root),
            {
                "api_root": SPOND_API_ROOT,
                "app_version": "4.62.0",
                "attachment_hosts": [],
            },
        )
        self.browsers: list[FakeBrowser] = []

    def bootstrap(self, cdp: LoginCdp, **options: Any) -> dict[str, Any]:
        def factory(profile: Path, url: str, path: Path | str | None) -> FakeBrowser:
            browser = FakeBrowser(cdp, profile)
            self.browsers.append(browser)
            return browser

        return bootstrap_spond(
            self.root,
            browser_factory=factory,
            importer=imported_metadata,
            **options,
        )

    def assert_cleaned(self) -> None:
        self.assertTrue(all(browser.closed for browser in self.browsers))
        base = self.root / ".private" / "browser-bootstrap"
        self.assertEqual(list(base.iterdir()), [])

    def test_detected_login_continues_without_confirmation(self) -> None:
        cdp = LoginCdp(probe_answers=[False, False, True])
        wait, prompts = wait_until_ready()

        result = self.bootstrap(cdp, wait_for_login=wait)

        self.assertEqual(result["source"], "spond")
        self.assertEqual(len(prompts), 1)
        self.assertIn("av oss selv", prompts[0])
        # Ready only after two positive checks in a row, then one full read.
        self.assertEqual(cdp.probes, 4)
        self.assertEqual(cdp.reads, 1)
        self.assert_cleaned()

    def test_one_positive_check_is_not_enough(self) -> None:
        cdp = LoginCdp(probe_answers=[True, False, True, True])
        wait, _ = wait_until_ready()

        self.bootstrap(cdp, wait_for_login=wait)

        self.assertEqual(cdp.probes, 4)
        self.assertEqual(cdp.reads, 1)

    def test_probe_asks_only_whether_the_values_are_there(self) -> None:
        probe = browser_bootstrap._spond_probe()
        expression = probe.expression
        for key in browser_bootstrap._SPOND_STORAGE_KEYS:
            self.assertIn(f'"{key}"', expression)
        self.assertIn("keys.every(", expression)
        self.assertIn('typeof value === "string" && value.length > 0', expression)
        self.assertNotIn("result", expression)
        self.assertNotIn("cookie", expression.lower())

        # Anything other than the boolean true is "not ready", and nothing is kept.
        cdp = LoginCdp(probe_answers=[dict(SPOND_FIELDS), SPOND_ACCESS, "true", 1, True])
        answers = [probe(cdp) for _ in range(5)]
        self.assertEqual(answers, [False, False, False, False, True])
        self.assertTrue(all(isinstance(answer, bool) for answer in answers))
        self.assertNotIn(SPOND_ACCESS, repr(vars(probe)))
        self.assertEqual(cdp.reads, 0)
        evaluations = [call for call in cdp.calls if call[0] == "Runtime.evaluate"]
        self.assertTrue(all(call[1]["returnByValue"] for call in evaluations))
        self.assertTrue(
            all(call[1]["expression"] == expression for call in evaluations)
        )
        # The session for the page is attached once and reused.
        attaches = [call for call in cdp.calls if call[0] == "Target.attachToTarget"]
        self.assertEqual(len(attaches), 1)

    def test_probe_errors_and_other_pages_mean_not_yet(self) -> None:
        cdp = LoginCdp(
            pages=[
                ["https://login.example.invalid/authorize"],
                [f"{SPOND_ORIGIN}/client", f"{SPOND_ORIGIN}/client"],
                BrowserBootstrapError("Nettleseren svarte ikke innen tidsgrensen"),
                [f"{SPOND_ORIGIN}/client"],
            ],
            probe_answers=[
                BrowserBootstrapError("Nettleseren avviste DevTools-forespørselen"),
                True,
            ],
        )
        wait, _ = wait_until_ready()

        result = self.bootstrap(cdp, wait_for_login=wait)

        self.assertEqual(result["source"], "spond")
        self.assertEqual(cdp.reads, 1)
        # The failed evaluation dropped the session, so the next check attached
        # again; the full read attaches once more.
        attaches = [call for call in cdp.calls if call[0] == "Target.attachToTarget"]
        self.assertEqual(len(attaches), 3)
        self.assert_cleaned()

    def test_confirmation_still_continues_without_detection(self) -> None:
        cdp = LoginCdp(probe_answers=[False])
        prompts: list[str] = []

        def confirmed(prompt: str, ready) -> None:
            prompts.append(prompt)
            self.assertFalse(ready())

        result = self.bootstrap(cdp, wait_for_login=confirmed)

        self.assertEqual(result["source"], "spond")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(cdp.reads, 1)
        self.assert_cleaned()

    def test_old_confirmation_callback_is_unchanged(self) -> None:
        cdp = LoginCdp(probe_answers=[True])
        prompts: list[str] = []

        self.bootstrap(cdp, wait_for_user=prompts.append)

        self.assertEqual(len(prompts), 1)
        self.assertEqual(cdp.probes, 0)
        self.assertEqual(cdp.reads, 1)

    def test_both_ways_of_waiting_are_refused(self) -> None:
        cdp = LoginCdp(probe_answers=[True])
        with self.assertRaises(BrowserBootstrapError):
            self.bootstrap(
                cdp,
                wait_for_user=lambda prompt: None,
                wait_for_login=lambda prompt, ready: None,
            )
        self.assertEqual(self.browsers, [])

    def test_detected_login_that_cannot_be_read_yet_waits_again(self) -> None:
        incomplete = dict(SPOND_FIELDS)
        del incomplete["refreshToken"]
        cdp = LoginCdp(probe_answers=[True], extractions=[incomplete, dict(SPOND_FIELDS)])
        wait, prompts = wait_until_ready()

        result = self.bootstrap(cdp, wait_for_login=wait)

        self.assertEqual(result["source"], "spond")
        self.assertEqual(len(prompts), 2)
        self.assertEqual(cdp.reads, 2)
        self.assert_cleaned()

    def test_confirmed_login_that_cannot_be_read_fails(self) -> None:
        incomplete = dict(SPOND_FIELDS)
        del incomplete["refreshToken"]
        cdp = LoginCdp(probe_answers=[False], extractions=[incomplete])

        with self.assertRaises(BrowserBootstrapError) as raised:
            self.bootstrap(cdp, wait_for_login=lambda prompt, ready: None)

        self.assertNotIn(SPOND_ACCESS, str(raised.exception))
        self.assertEqual(cdp.reads, 1)
        self.assert_cleaned()

    def test_detection_stops_after_repeated_failed_reads(self) -> None:
        incomplete = dict(SPOND_FIELDS)
        del incomplete["refreshToken"]
        cdp = LoginCdp(probe_answers=[True], extractions=[incomplete])
        rounds: list[bool] = []

        def wait(prompt: str, ready) -> None:
            for _ in range(4):
                if ready():
                    rounds.append(True)
                    return
            # No longer detected: this is the user's confirmation.
            rounds.append(False)

        with self.assertRaises(BrowserBootstrapError):
            self.bootstrap(cdp, wait_for_login=wait)

        automatic = browser_bootstrap._AUTOMATIC_READS
        self.assertEqual(rounds, [True] * automatic + [False])
        self.assertEqual(cdp.reads, automatic + 1)
        self.assert_cleaned()

    def test_closed_window_stops_the_wait(self) -> None:
        spond = [f"{SPOND_ORIGIN}/client"]
        cdp = LoginCdp(probe_answers=[False], pages=[[], spond, spond, []])
        wait, _ = wait_until_ready()

        with self.assertRaises(browser_bootstrap.BrowserClosedError) as raised:
            self.bootstrap(cdp, wait_for_login=wait)

        self.assertIn("lukket", str(raised.exception))
        # The empty list before the first page is a slow start, not a closed window.
        listings = [call for call in cdp.calls if call[0] == "Target.getTargets"]
        self.assertGreaterEqual(len(listings), 2 + browser_bootstrap._CLOSED_CHECKS)
        self.assertEqual(cdp.reads, 0)
        self.assert_cleaned()

    def test_broken_devtools_pipe_stops_the_wait(self) -> None:
        cdp = LoginCdp(
            probe_answers=[False],
            pages=[browser_bootstrap._CdpPipeClosed("DevTools-røret ble lukket uventet")],
        )
        wait, _ = wait_until_ready()

        with self.assertRaises(browser_bootstrap.BrowserClosedError):
            self.bootstrap(cdp, wait_for_login=wait)

        self.assert_cleaned()

    def test_a_wait_that_raises_cancels_the_login(self) -> None:
        cdp = LoginCdp(probe_answers=[False])

        def cancelled(prompt: str, ready) -> None:
            raise BrowserBootstrapError("Innloggingen ble avbrutt")

        with self.assertRaisesRegex(BrowserBootstrapError, "avbrutt"):
            self.bootstrap(cdp, wait_for_login=cancelled)

        self.assertEqual(cdp.reads, 0)
        self.assert_cleaned()


class FakeAutofill:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.steps = 0

    def step(self, cdp: Any) -> bool:
        self.steps += 1
        return False


class TerminalWaitTests(unittest.TestCase):
    def wait(self, autofill: FakeAutofill, ready, *, lines: list[bool], typed: str = "\n"):
        stdin = io.StringIO(typed)
        stdin.isatty = lambda: True  # type: ignore[method-assign]
        output = io.StringIO()
        with (
            patch.object(sys, "stdin", stdin),
            patch.object(browser_bootstrap, "_line_ready", side_effect=lines),
            contextlib.redirect_stdout(output),
        ):
            browser_bootstrap._wait_in_terminal("Logg inn: ", autofill, object(), ready)
        return output.getvalue()

    def test_terminal_continues_by_itself_and_keeps_filling_the_password(self) -> None:
        autofill = FakeAutofill(enabled=True)
        answers = iter([False, False, True])

        output = self.wait(autofill, lambda: next(answers), lines=[False, False])

        self.assertIn("Innloggingen er funnet.", output)
        self.assertEqual(autofill.steps, 3)

    def test_enter_still_continues(self) -> None:
        autofill = FakeAutofill(enabled=False)

        output = self.wait(autofill, lambda: False, lines=[False, True])

        self.assertNotIn("funnet", output)
        self.assertEqual(autofill.steps, 0)

    def test_closed_input_cancels(self) -> None:
        with self.assertRaisesRegex(BrowserBootstrapError, "avbrutt"):
            self.wait(FakeAutofill(enabled=False), lambda: False, lines=[True], typed="")

    def test_closed_window_stops_the_terminal_wait(self) -> None:
        def closed() -> bool:
            raise browser_bootstrap.BrowserClosedError("Nettleservinduet ble lukket")

        with self.assertRaises(browser_bootstrap.BrowserClosedError):
            self.wait(FakeAutofill(enabled=False), closed, lines=[False])

    def test_without_a_terminal_only_enter_counts(self) -> None:
        stdin = io.StringIO("\n")
        stdin.isatty = lambda: False  # type: ignore[method-assign]
        ready_calls: list[bool] = []

        def ready() -> bool:
            ready_calls.append(True)
            return True

        with patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(io.StringIO()):
            browser_bootstrap._wait_in_terminal(
                "Logg inn: ", FakeAutofill(enabled=False), object(), ready
            )
            with self.assertRaisesRegex(BrowserBootstrapError, "interaktiv"):
                browser_bootstrap._wait_in_terminal(
                    "Logg inn: ", FakeAutofill(enabled=True), object(), ready
                )
        self.assertEqual(ready_calls, [])


if __name__ == "__main__":
    unittest.main()
