"""Local HTTPS Pages server, runtime TLS certificates and CLI test launcher.

Shared by the real-browser acceptance tests for the static GitHub Pages setup
page. Generates throwaway certificates at run time with `cryptography`, serves
the real shipped `pages/` directory (optionally substituting only the loopback
port), and launches the real `familiekilder` CLI under `python -I`.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import http.server
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

PAGES_HOST = "familiekilder.github.io"
CROSS_SITE_HOST = "other-public.example"
CROSS_SITE_ORIGIN = f"https://{CROSS_SITE_HOST}"
CROSS_SITE_URL = f"{CROSS_SITE_ORIGIN}/cross-site.html"

# Without the first letter: Windows writes a pipe in its own code page.
URL_PATTERN = re.compile(r"pne lokalt oppsett: (http://127\.0\.0\.1:(\d+))")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}

_CROSS_SITE_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Cross-site test origin</title>
  </head>
  <body>
    <h1>Cross-site page</h1>
  </body>
</html>
"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def isolated_environment(base: Path, *, root: Path | None = None) -> dict[str, str]:
    """Build a subprocess environment whose home/config/cache dirs live under `base`."""

    home = base / "home"
    local_appdata = base / "localappdata"
    appdata = base / "appdata"
    xdg_config = base / "xdg-config"
    xdg_data = base / "xdg-data"
    xdg_cache = base / "xdg-cache"
    xdg_state = base / "xdg-state"
    for directory in (
        home,
        local_appdata,
        appdata,
        xdg_config,
        xdg_data,
        xdg_cache,
        xdg_state,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FAMILIEKILDER_")
        and key
        not in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "BROWSER",
            "DBUS_SESSION_BUS_ADDRESS",
        }
    }
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "LOCALAPPDATA": str(local_appdata),
            "APPDATA": str(appdata),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_DATA_HOME": str(xdg_data),
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_STATE_HOME": str(xdg_state),
            "FAMILIEKILDER_ROOT": str(root if root is not None else home / "default-data"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


@dataclass(frozen=True)
class RuntimeCertificate:
    cert_path: Path
    key_path: Path
    spki_sha256: str


def generate_runtime_certificate(
    directory: Path,
    *,
    hostnames: Sequence[str] = (PAGES_HOST, CROSS_SITE_HOST),
) -> RuntimeCertificate:
    """Create a self-signed TLS certificate at run time and compute its SPKI pin."""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in hostnames]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    spki_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    spki_sha256 = base64.b64encode(hashlib.sha256(spki_der).digest()).decode("ascii")

    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "pages-cert.pem"
    key_path = directory / "pages-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    if os.name != "nt":
        os.chmod(key_path, 0o600)
    return RuntimeCertificate(cert_path=cert_path, key_path=key_path, spki_sha256=spki_sha256)


class LocalPagesServer:
    """Serve the shipped `pages/` tree over local HTTPS for `https://familiekilder.github.io`."""

    def __init__(
        self,
        pages_dir: Path,
        *,
        local_port: int,
        original_port: int = 28417,
        cert_dir: Path | None = None,
    ) -> None:
        self.pages_dir = Path(pages_dir).resolve()
        self.local_port = local_port
        self.original_port = original_port
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if cert_dir is None:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="familiekilder-pages-tls-")
            cert_dir = Path(self._temp_dir.name)
        self.certificate = generate_runtime_certificate(cert_dir)
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.https_port: int = 0

    def browser_flags(self) -> list[str]:
        """Chromium flags that map the public hostnames to this server and mark it public."""

        rules = ",".join(
            (
                f"MAP {PAGES_HOST}:443 127.0.0.1:{self.https_port}",
                f"MAP {CROSS_SITE_HOST}:443 127.0.0.1:{self.https_port}",
                "MAP * ~NOTFOUND",
                "EXCLUDE 127.0.0.1",
            )
        )
        return [
            f"--host-resolver-rules={rules}",
            f"--ignore-certificate-errors-spki-list={self.certificate.spki_sha256}",
            f"--ip-address-space-overrides=127.0.0.1:{self.https_port}=public",
        ]

    def _resolve_asset(self, host: str, raw_path: str) -> tuple[int, bytes, str]:
        path = urllib.parse.urlsplit(raw_path).path
        clean_host = host.split(":", 1)[0].lower()
        if clean_host == CROSS_SITE_HOST:
            if path in {"/", "/cross-site.html"}:
                return 200, _CROSS_SITE_HTML.encode("utf-8"), _CONTENT_TYPES[".html"]
            return 404, b"Not found", "text/plain; charset=utf-8"

        if clean_host != PAGES_HOST:
            return 404, b"Unknown host", "text/plain; charset=utf-8"

        prefix = "/familiekilder-mcp/"
        if path == "/familiekilder-mcp":
            return 302, b"", "text/plain; charset=utf-8"
        if not path.startswith(prefix):
            return 404, b"Not found", "text/plain; charset=utf-8"
        relative = path.removeprefix(prefix)
        if not relative or relative.endswith("/"):
            relative = relative + "index.html"
        candidate = (self.pages_dir / relative).resolve()
        if not candidate.is_relative_to(self.pages_dir) or not candidate.is_file():
            return 404, b"Not found", "text/plain; charset=utf-8"
        raw = candidate.read_bytes()
        if self.local_port != self.original_port and candidate.suffix in {".html", ".js"}:
            text = raw.decode("utf-8").replace(str(self.original_port), str(self.local_port))
            raw = text.encode("utf-8")
        mime = _CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
        return 200, raw, mime

    def __enter__(self) -> LocalPagesServer:
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                host = self.headers.get("Host", "")
                with parent._lock:
                    parent.requests.append((host, self.path))
                status, body, mime = parent._resolve_asset(host, self.path)
                if status == 302:
                    self.send_response(302)
                    self.send_header("Location", "/familiekilder-mcp/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            certfile=str(self.certificate.cert_path),
            keyfile=str(self.certificate.key_path),
        )
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self.https_port = int(server.server_port)
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None


class WizardProcess:
    """Manage a real CLI setup subprocess with background stdout/stderr capture."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._lock = threading.Lock()
        self._stdout_done = threading.Event()
        self._stderr_done = threading.Event()
        self._stdout_thread = threading.Thread(
            target=self._drain,
            args=(process.stdout, self._lines, self._stdout_done),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain,
            args=(process.stderr, self._stderr_lines, self._stderr_done),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _drain(self, stream: Any, target: list[str], done: threading.Event) -> None:
        try:
            if stream is None:
                return
            for line in stream:
                with self._lock:
                    target.append(line)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            done.set()

    @property
    def stdout_text(self) -> str:
        with self._lock:
            return "".join(self._lines)

    @property
    def stderr_text(self) -> str:
        with self._lock:
            return "".join(self._stderr_lines)

    def wait_for_port(self, *, timeout: float = 20.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = URL_PATTERN.search(self.stdout_text)
            if match:
                return int(match.group(2))
            if self.process.poll() is not None:
                self._stdout_done.wait(timeout=2)
                self._stderr_done.wait(timeout=2)
                match = URL_PATTERN.search(self.stdout_text)
                if match:
                    return int(match.group(2))
                raise AssertionError(
                    f"Wizard exited early with code {self.process.returncode}.\n"
                    f"STDOUT:\n{self.stdout_text}\nSTDERR:\n{self.stderr_text}"
                )
            time.sleep(0.05)
        raise AssertionError(
            f"Wizard never printed its local URL within {timeout}s.\n"
            f"STDOUT:\n{self.stdout_text}\nSTDERR:\n{self.stderr_text}"
        )

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self._stdout_done.wait(timeout=2)
        self._stderr_done.wait(timeout=2)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._stdout_done.wait(timeout=2)
        self._stderr_done.wait(timeout=2)


def spawn_cli_wizard(
    root: Path,
    env: Mapping[str, str],
    *,
    cli_args: Sequence[str] = (),
    setup_port: int | None = None,
    browser_log: Path | None = None,
    status_log: Path | None = None,
    fake_hoopit: bool = False,
    unmodified_cli: bool = False,
) -> WizardProcess:
    """Start the real `familiekilder` setup CLI under `python -I`.

    When `unmodified_cli` is True, runs `python -I -m familiekilder --root ... setup ...`
    directly with zero patching (used for the CI real-port 28417 test).
    Otherwise invokes this module's small launcher under `python -I` so tests
    can substitute `SETUP_PORT`, capture `webbrowser.open`, log `/status`
    requests, and attach the fake Hoopit transport.
    """

    if unmodified_cli:
        command = [
            sys.executable,
            "-I",
            "-m",
            "familiekilder",
            "--root",
            str(root),
            "setup",
            *cli_args,
        ]
    else:
        support_dir = Path(__file__).resolve().parents[1]
        launcher = Path(__file__).resolve()
        command = [
            sys.executable,
            "-I",
            str(launcher),
            "--support-dir",
            str(support_dir),
        ]
        if setup_port is not None:
            command.extend(["--setup-port", str(setup_port)])
        if browser_log is not None:
            command.extend(["--browser-log", str(browser_log)])
        if status_log is not None:
            command.extend(["--status-log", str(status_log)])
        if fake_hoopit:
            command.append("--fake-hoopit")
        command.extend(["--", "--root", str(root), "setup", *cli_args])

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(env),
    )
    return WizardProcess(process)


def wait_for_status(port: int, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = HTTPConnection("127.0.0.1", port, timeout=1.0)
        try:
            connection.request("GET", "/status", headers={"Host": f"127.0.0.1:{port}"})
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return
        except (OSError, HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.05)
    raise AssertionError(f"Wizard on 127.0.0.1:{port} never answered /status 200 within {timeout}s")


def wait_for_browser_url(
    browser_log: Path, *, previous_count: int = 0, timeout: float = 15.0
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if browser_log.exists():
            lines = [
                line.strip()
                for line in browser_log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if len(lines) > previous_count:
                return lines[previous_count]
        time.sleep(0.05)
    raise AssertionError(f"webbrowser.open was not called within {timeout}s (log={browser_log})")


def read_status_log(status_log: Path) -> list[dict[str, Any]]:
    if not status_log.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in status_log.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def _run_launcher(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test launcher for familiekilder setup CLI")
    parser.add_argument("--support-dir", type=Path, required=True)
    parser.add_argument("--setup-port", type=int, default=None)
    parser.add_argument("--browser-log", type=Path, default=None)
    parser.add_argument("--status-log", type=Path, default=None)
    parser.add_argument("--fake-hoopit", action="store_true")
    parser.add_argument("cli_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    cli_args = list(args.cli_args)
    if cli_args and cli_args[0] == "--":
        cli_args = cli_args[1:]

    if str(args.support_dir) not in sys.path:
        sys.path.insert(0, str(args.support_dir))

    import webbrowser
    from familiekilder import hoopit, setup_page, setup_web, source_cli

    original_port = setup_page.SETUP_PORT
    if args.setup_port is not None:
        setup_page.SETUP_PORT = args.setup_port
        original_reply = setup_web.Handler._reply
        old_js_decl = f"const SETUP_PORT = {original_port};".encode("utf-8")
        new_js_decl = f"const SETUP_PORT = {args.setup_port};".encode("utf-8")

        def patched_reply(
            self, status, payload, mime="application/json; charset=utf-8", *, extra_headers=None
        ):
            if (
                status == 200
                and self.path == "/setup.js"
                and isinstance(payload, (bytes, bytearray))
            ):
                payload = bytes(payload).replace(old_js_decl, new_js_decl, 1)
            return original_reply(self, status, payload, mime, extra_headers=extra_headers)

        setup_web.Handler._reply = patched_reply

    if args.status_log is not None:
        log_lock = threading.Lock()
        original_handle_status = setup_web.Handler._handle_status
        status_log_path = args.status_log

        def logged_handle_status(self):
            entry = {
                "path": self.path,
                "origin": self.headers.get("Origin"),
                "sec_fetch_site": self.headers.get("Sec-Fetch-Site"),
                "instance": self.headers.get("X-Setup-Instance") is not None,
            }
            with log_lock:
                with status_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry) + "\n")
            return original_handle_status(self)

        setup_web.Handler._handle_status = logged_handle_status

    if args.browser_log is not None:
        browser_log_path = args.browser_log

        def capture_open(url: str, *_args: Any, **_kwargs: Any) -> bool:
            with browser_log_path.open("a", encoding="utf-8") as handle:
                handle.write(url + "\n")
                handle.flush()
            return True

        webbrowser.open = capture_open

    if args.fake_hoopit:
        from familiekilder_testing import hoopit as fake_hoopit

        hoopit.default_transport = lambda: fake_hoopit.FakeTransport()
        hoopit.PersistentRateLimiter = lambda _root, **_kwargs: fake_hoopit.NoopLimiter()

    return int(source_cli.main(cli_args) or 0)


if __name__ == "__main__":
    raise SystemExit(_run_launcher())
