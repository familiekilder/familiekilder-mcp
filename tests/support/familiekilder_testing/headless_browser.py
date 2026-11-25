"""A headless Chromium-family browser for tests that need real browser rules.

Some rules exist only in a browser, such as a page's Content-Security-Policy
applied to the redirect after a form submission, or Local Network Access
blocking loopback requests from a public HTTPS origin. An HTTP client in a
test does not enforce them. This module starts Brave, Chrome or Edge headless
with a new temporary profile and drives it over the DevTools pipe, never a TCP
port. It never uses the user's own profile and opens no window.

Browser discovery follows the platform catalog in `familiekilder.browsers`:
- macOS: Brave, then Chrome, then Edge (unchanged default order for Brave/Chrome)
- Linux: Chrome first, then Brave, then Edge
- Windows: Edge first, then Chrome, then Brave

Set `FAMILIEKILDER_TEST_BROWSER=brave|chrome|edge` to force one browser.
Set `FAMILIEKILDER_REQUIRE_BROWSER=1` so a missing browser fails instead of
skipping.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from familiekilder import browser_bootstrap, browsers
from familiekilder.browser_bootstrap import BrowserBootstrapError

TEST_BROWSER_ENV = "FAMILIEKILDER_TEST_BROWSER"
REQUIRE_BROWSER_ENV = "FAMILIEKILDER_REQUIRE_BROWSER"

_PLATFORM_ORDER: dict[str, tuple[str, ...]] = {
    browsers.MACOS: (browsers.BRAVE, browsers.CHROME, browsers.EDGE),
    browsers.LINUX: (browsers.CHROME, browsers.BRAVE, browsers.EDGE),
    browsers.WINDOWS: (browsers.EDGE, browsers.CHROME, browsers.BRAVE),
}

_LOOPBACK_PERMISSIONS = ("loopback-network", "local-network-access")


def require_browser_enabled() -> bool:
    """Whether tests must fail instead of skipping when no browser is usable."""

    return os.environ.get(REQUIRE_BROWSER_ENV) == "1"


def _requested_browser(explicit: str | None) -> str | None:
    raw = explicit if explicit is not None else os.environ.get(TEST_BROWSER_ENV)
    if raw is None:
        return None
    choice = raw.strip().lower()
    if not choice:
        return None
    if choice not in browsers.BROWSER_IDS:
        expected = ", ".join(browsers.BROWSER_IDS)
        raise ValueError(f"Unknown browser {choice!r} in {TEST_BROWSER_ENV} (expected {expected})")
    return choice


def _candidate_paths(browser_id: str) -> tuple[Path, ...]:
    platform = browsers.platform_name()
    if platform == browsers.MACOS:
        if browser_id == browsers.BRAVE:
            return tuple(browser_bootstrap._BRAVE_PATHS)
        if browser_id == browsers.CHROME:
            return tuple(browser_bootstrap._CHROME_PATHS)
    for app in browsers.catalog():
        if app.id == browser_id:
            return tuple(Path(item) for item in app.executables)
    return ()


def find_browser(browser: str | None = None) -> Path | None:
    """Return the installed test browser executable, or None if unavailable.

    Raises RuntimeError instead of returning None when
    `FAMILIEKILDER_REQUIRE_BROWSER=1`.
    """

    platform = browsers.platform_name()
    order = _PLATFORM_ORDER.get(platform)
    choice = _requested_browser(browser)
    if order is not None:
        search_order = (choice,) if choice is not None else order
        for browser_id in search_order:
            for candidate in _candidate_paths(browser_id):
                if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                    return candidate
    if require_browser_enabled():
        if choice is not None:
            raise RuntimeError(
                f"Requested browser {choice!r} ({TEST_BROWSER_ENV}) is not installed on {sys.platform} "
                f"({REQUIRE_BROWSER_ENV}=1)"
            )
        raise RuntimeError(
            f"No supported Chromium browser (Brave, Chrome or Edge) is installed on {sys.platform} "
            f"({REQUIRE_BROWSER_ENV}=1)"
        )
    return None


def browser_or_skip(test: unittest.TestCase, *, browser: str | None = None) -> Path:
    """Return a usable browser executable, or fail/skip according to env."""

    choice = _requested_browser(browser)
    try:
        executable = find_browser(choice)
    except RuntimeError as exc:
        test.fail(str(exc))
    if executable is not None:
        return executable
    if choice is not None:
        reason = (
            f"Requested browser {choice!r} ({TEST_BROWSER_ENV}) is not installed on {sys.platform}"
        )
    else:
        reason = f"No supported Chromium browser (Brave, Chrome or Edge) is installed on {sys.platform}"
    test.skipTest(reason)


def _arguments(
    executable: Path,
    profile: Path,
    *,
    extra_args: Sequence[str] = (),
    window_size: tuple[int, int] | None = None,
) -> list[str]:
    arguments = [
        str(executable),
        f"--user-data-dir={profile}",
        "--headless",
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-extensions",
        "--disable-component-update",
        "--disable-background-mode",
        "--disable-crash-reporter",
        "--disable-breakpad",
        # Never ask the macOS keychain for the browser's storage key.
        "--use-mock-keychain",
        "--password-store=basic",
    ]
    if window_size is not None:
        arguments.append(f"--window-size={window_size[0]},{window_size[1]}")
    arguments.extend(extra_args)
    arguments.append("about:blank")
    return arguments


class BrowserPage:
    """One attached page target inside a `HeadlessBrowser`."""

    def __init__(self, browser: HeadlessBrowser, target_id: str, session_id: str) -> None:
        self._browser = browser
        self.target_id = target_id
        self.session_id = session_id

    def cdp_call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 20.0,
    ) -> Mapping[str, Any]:
        return self._browser._call(method, params, session_id=self.session_id, timeout=timeout)

    def navigate(self, url: str) -> None:
        self.cdp_call("Page.navigate", {"url": url})

    def reload(self, *, ignore_cache: bool = False) -> None:
        self.cdp_call("Page.reload", {"ignoreCache": ignore_cache})

    def bring_to_front(self) -> None:
        self.cdp_call("Page.bringToFront")

    def evaluate(
        self,
        expression: str,
        *,
        await_promise: bool = False,
        timeout: float = 20.0,
    ) -> Any:
        params: dict[str, Any] = {"expression": expression, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        result = self.cdp_call("Runtime.evaluate", params, timeout=timeout)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("text") or "JavaScript evaluation error"
            exception = details.get("exception")
            description = (
                exception.get("description") if isinstance(exception, Mapping) else None
            )
            raise BrowserBootstrapError(f"{text}: {description}" if description else str(text))
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, *, timeout: float = 15.0) -> Any:
        """Evaluate until the value is truthy. Navigation may interrupt a call."""

        deadline = time.monotonic() + timeout
        while True:
            try:
                value = self.evaluate(expression)
            except BrowserBootstrapError:
                value = None
            if value:
                return value
            if time.monotonic() > deadline:
                detail = ""
                try:
                    href = self.evaluate("location.href")
                    snippet = self.evaluate(
                        "(document.body && document.body.innerText || '').slice(0, 300)"
                    )
                    detail = f" (url={href!r}, body={snippet!r})"
                except BrowserBootstrapError:
                    pass
                raise AssertionError(f"The page never satisfied: {expression}{detail}")
            time.sleep(0.05)

    def _click_point(self, x: float, y: float) -> None:
        self.bring_to_front()
        self.cdp_call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0},
        )
        self.cdp_call(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": x,
                "y": y,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        self.cdp_call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
        )

    def _wait_for_center(self, locator_js: str, *, timeout: float = 15.0) -> tuple[float, float]:
        expression = (
            "(() => {"
            f"  const el = {locator_js};"
            "  if (!el || !el.isConnected || el.hidden || el.disabled) return null;"
            "  const style = getComputedStyle(el);"
            "  if (style.display === 'none' || style.visibility === 'hidden') return null;"
            "  el.scrollIntoView({ block: 'center', inline: 'center' });"
            "  const rect = el.getBoundingClientRect();"
            "  if (rect.width <= 0 || rect.height <= 0) return null;"
            "  return [rect.left + rect.width / 2, rect.top + rect.height / 2];"
            "})()"
        )
        coords = self.wait_for(expression, timeout=timeout)
        return float(coords[0]), float(coords[1])

    def click(self, selector: str, *, timeout: float = 15.0) -> None:
        """Click the first visible enabled element matching `selector` with real CDP mouse input."""

        x, y = self._wait_for_center(f"document.querySelector({json.dumps(selector)})", timeout=timeout)
        self._click_point(x, y)

    def click_text(self, selector: str, text: str, *, timeout: float = 15.0) -> None:
        """Click the first visible enabled element matching `selector` whose trimmed text equals `text`."""

        locator = (
            f"Array.from(document.querySelectorAll({json.dumps(selector)})).find("
            f"(node) => (node.textContent || '').trim() === {json.dumps(text)}"
            ")"
        )
        x, y = self._wait_for_center(locator, timeout=timeout)
        self._click_point(x, y)

    def fill(self, selector: str, text: str, *, timeout: float = 15.0) -> None:
        """Focus an input with a real click, clear it, and insert `text` via CDP Input.insertText."""

        self.click(selector, timeout=timeout)
        self.evaluate(
            "(() => {"
            f"  const el = document.querySelector({json.dumps(selector)});"
            "  el.focus();"
            "  el.value = '';"
            "  return true;"
            "})()"
        )
        self.cdp_call("Input.insertText", {"text": text})

    def select_option(self, selector: str, value: str, *, timeout: float = 15.0) -> None:
        """Choose an option in a native <select> and dispatch real input/change events."""

        self.wait_for(
            "(() => {"
            f"  const el = document.querySelector({json.dumps(selector)});"
            "  if (!el || el.disabled) return false;"
            f"  return Array.from(el.options).some((opt) => opt.value === {json.dumps(value)});"
            "})()",
            timeout=timeout,
        )
        self.evaluate(
            "(() => {"
            f"  const el = document.querySelector({json.dumps(selector)});"
            "  el.focus();"
            f"  el.value = {json.dumps(value)};"
            "  el.dispatchEvent(new Event('input', { bubbles: true }));"
            "  el.dispatchEvent(new Event('change', { bubbles: true }));"
            "  return el.value;"
            "})()"
        )

    def frame_tree(self) -> Mapping[str, Any]:
        return self.cdp_call("Page.getFrameTree")

    def close(self) -> None:
        self._browser._call("Target.closeTarget", {"targetId": self.target_id})


class HeadlessBrowser:
    """One headless browser with a throwaway profile, for the length of a `with` block."""

    def __init__(
        self,
        executable: Path,
        *,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        window_size: tuple[int, int] | None = (1280, 900),
    ) -> None:
        self.executable = executable
        self._extra_args = tuple(extra_args)
        self._env = dict(env) if env is not None else None
        self._window_size = window_size
        self._profile: tempfile.TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._cdp: Any = None
        self._page: BrowserPage | None = None

    def __enter__(self) -> HeadlessBrowser:
        self._profile = tempfile.TemporaryDirectory(prefix="familiekilder-headless-")
        try:
            self._launch(Path(self._profile.name))
            self._page = self.new_page("about:blank")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def page(self) -> BrowserPage:
        if self._page is None:
            raise RuntimeError("HeadlessBrowser is not open")
        return self._page

    def _call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 20.0,
    ) -> Mapping[str, Any]:
        if self._cdp is None:
            raise RuntimeError("HeadlessBrowser is not open")
        return self._cdp.call(method, params, session_id=session_id, timeout=timeout)

    def _launch(self, profile: Path) -> None:
        arguments = _arguments(
            self.executable,
            profile,
            extra_args=self._extra_args,
            window_size=self._window_size,
        )
        if os.name == "nt":
            self._launch_windows(arguments)
            return
        self._launch_posix(arguments)

    def _launch_posix(self, arguments: list[str]) -> None:
        # The same pipe setup as the browser bootstrap: the browser reads
        # DevTools requests on descriptor 3 and writes replies on 4. The
        # child ends are moved above 10 so the wrapper's dup2 cannot clash.
        raw_read, parent_write = os.pipe()
        parent_read, raw_write = os.pipe()
        child_read = browser_bootstrap._duplicate_child_descriptor(raw_read)
        child_write = browser_bootstrap._duplicate_child_descriptor(raw_write)
        os.close(raw_read)
        os.close(raw_write)
        try:
            self._process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    browser_bootstrap._PIPE_EXEC_WRAPPER,
                    str(child_read),
                    str(child_write),
                    *arguments,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(child_read, child_write),
                start_new_session=True,
                env=self._env,
            )
        except BaseException:
            os.close(parent_write)
            os.close(parent_read)
            raise
        finally:
            os.close(child_read)
            os.close(child_write)
        self._cdp = browser_bootstrap._PipeCdpClient(parent_write, parent_read)

    def _launch_windows(self, arguments: list[str]) -> None:
        # Same inherited-handle pipe method as browser_bootstrap._launch_windows,
        # with optional isolated environment variables.
        import msvcrt

        env = self._env
        if env is not None and "USERPROFILE" in os.environ:
            # With another USERPROFILE, Chromium takes the temporary profile for the default one
            # and refuses DevTools. --user-data-dir still keeps the real profile out of it.
            env = {**env, "USERPROFILE": os.environ["USERPROFILE"]}
        child_read, parent_write = os.pipe()
        parent_read, child_write = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        try:
            read_handle = msvcrt.get_osfhandle(child_read)
            write_handle = msvcrt.get_osfhandle(child_write)
            os.set_handle_inheritable(read_handle, True)
            os.set_handle_inheritable(write_handle, True)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [read_handle, write_handle]}
            command = [*arguments, f"--remote-debugging-io-pipes={read_handle},{write_handle}"]
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                startupinfo=startup,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                env=env,
            )
            self._process = process
            self._cdp = browser_bootstrap._PipeCdpClient(parent_write, parent_read)
        finally:
            for descriptor in (child_read, child_write):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if process is None:
                for descriptor in (parent_write, parent_read):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def page_targets(self) -> list[dict[str, Any]]:
        infos = self._call("Target.getTargets").get("targetInfos")
        if not isinstance(infos, list):
            return []
        return [
            dict(info)
            for info in infos
            if isinstance(info, Mapping) and info.get("type") == "page"
        ]

    def iframe_targets(self, parent_target_id: str | None = None) -> list[dict[str, Any]]:
        infos = self._call("Target.getTargets").get("targetInfos")
        if not isinstance(infos, list):
            return []
        result: list[dict[str, Any]] = []
        for info in infos:
            if not isinstance(info, Mapping) or info.get("type") != "iframe":
                continue
            if parent_target_id is not None and info.get("parentId") != parent_target_id:
                continue
            result.append(dict(info))
        return result

    def target_ids(self) -> set[str]:
        return {str(info["targetId"]) for info in self.page_targets() if info.get("targetId")}

    def attach(self, target_id: str) -> BrowserPage:
        attached = self._call(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        return BrowserPage(self, target_id, str(attached["sessionId"]))

    def new_page(self, url: str = "about:blank") -> BrowserPage:
        created = self._call("Target.createTarget", {"url": url})
        return self.attach(str(created["targetId"]))

    def wait_for_new_page(
        self,
        known_target_ids: set[str],
        *,
        url_prefix: str | None = None,
        timeout: float = 15.0,
    ) -> BrowserPage:
        """Wait for a new page target outside `known_target_ids` and attach to it."""

        deadline = time.monotonic() + timeout
        while True:
            for info in self.page_targets():
                target_id = str(info.get("targetId") or "")
                url = str(info.get("url") or "")
                if not target_id or target_id in known_target_ids:
                    continue
                if url_prefix is not None and not url.startswith(url_prefix):
                    continue
                return self.attach(target_id)
            if time.monotonic() > deadline:
                seen = [(t.get("targetId"), t.get("url")) for t in self.page_targets()]
                raise AssertionError(
                    f"No new page target matching url_prefix={url_prefix!r} appeared; targets={seen!r}"
                )
            time.sleep(0.05)

    def set_permission(self, origin: str, permission: str, setting: str) -> None:
        self._call(
            "Browser.setPermission",
            {
                "origin": origin,
                "permission": {"name": permission},
                "setting": setting,
            },
        )

    def set_loopback_permission(self, origin: str, setting: str) -> str:
        """Set the browser's Local Network Access permission for `origin`."""

        last_error: Exception | None = None
        for name in _LOOPBACK_PERMISSIONS:
            try:
                self.set_permission(origin, name, setting)
                return name
            except BrowserBootstrapError as exc:
                last_error = exc
        raise BrowserBootstrapError(
            f"Browser rejected loopback permission names {_LOOPBACK_PERMISSIONS}: {last_error}"
        )

    def reset_permissions(self) -> None:
        self._call("Browser.resetPermissions")

    # Compatibility helpers delegating to the initial page (used by test_remote_http.py).
    def navigate(self, url: str) -> None:
        self.page.navigate(url)

    def evaluate(self, expression: str) -> Any:
        return self.page.evaluate(expression)

    def wait_for(self, expression: str, *, timeout: float = 15.0) -> Any:
        return self.page.wait_for(expression, timeout=timeout)

    def close(self) -> None:
        process, self._process = self._process, None
        self._page = None
        if process is not None:
            if process.poll() is None and os.name == "nt":
                # Edge's child processes outlive a polite close and keep profile files open,
                # so end the whole tree while its root still exists.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
            elif process.poll() is None:
                try:
                    self._cdp.call("Browser.close", timeout=3.0)
                except Exception:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if hasattr(os, "killpg"):
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except OSError:
                            process.kill()
                    else:
                        process.kill()
                    process.wait(timeout=5)
        if self._cdp is not None:
            self._cdp.close()
            self._cdp = None
        if self._profile is not None:
            profile, self._profile = self._profile, None
            # Windows browsers can hold profile files briefly after exit.
            deadline = time.monotonic() + (10.0 if os.name == "nt" else 2.0)
            while True:
                try:
                    profile.cleanup()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - name required by BaseHTTPRequestHandler
        self.server.received.put(self.path)  # type: ignore[attr-defined]
        body = b"Oppdiktet assistent fikk svaret."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@contextlib.contextmanager
def callback_server() -> Iterator[tuple[str, queue.Queue[str]]]:
    """A local stand-in for an assistant's OAuth callback.

    Yields its base URL (its own port on 127.0.0.1, so its own origin) and a
    queue with the path and query of every GET it receives.
    """

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.received = queue.Queue()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.received  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
