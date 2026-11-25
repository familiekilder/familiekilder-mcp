"""Loopback-only web setup with origin, host, per-process CSRF and single-instance checks."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import webbrowser

from . import setup_page
from .client_defaults import refresh_stored_defaults
from .core import project_paths
from .private_lock import PrivateLockBusy, private_named_lock
from .private_state import (
    PrivateStateError,
    atomic_write_private_json,
    read_private_json,
    remove_private_file,
)
from .setup_clients import ClientSetupError
from .setup_service import SetupError, SetupService

ASSETS = Path(__file__).parent / "data/setup"
MAX_BODY_BYTES = 2 * 1024 * 1024
MARKER_FILENAME = "setup-server.json"
REUSE_WAIT_SECONDS = 15.0
REUSE_POLL_SECONDS = 0.1
WINDOWS_BIND_RETRY_SECONDS = 10.0
WINDOWS_BIND_POLL_SECONDS = 0.2
PROBE_TIMEOUT_SECONDS = 1.5
READY_WAIT_SECONDS = 5.0
READY_POLL_SECONDS = 0.05
REUSE_BROWSER_JOIN_SECONDS = 2.0

_PORT_BUSY_ERRNOS = frozenset(
    {
        errno.EADDRINUSE,
        errno.EACCES,
        getattr(errno, "WSAEADDRINUSE", 10048),
        getattr(errno, "WSAEACCES", 10013),
    }
)
_PORT_BUSY_WINERRORS = frozenset({10048, 10013})


# The format secrets.token_urlsafe writes; it is also sent as a header.
_INSTANCE_ID = re.compile(r"[A-Za-z0-9_-]{8,200}")


def _is_windows() -> bool:
    return os.name == "nt"


class _PortBusyError(OSError):
    """Raised only when socket.bind fails because the requested port is unavailable."""


def _is_port_busy(error: OSError) -> bool:
    return (
        error.errno in _PORT_BUSY_ERRNOS
        or getattr(error, "winerror", None) in _PORT_BUSY_WINERRORS
    )


def marker_path(root: Path) -> Path:
    """Path to the atomic local setup marker under ``.private``."""

    return project_paths(root).private / MARKER_FILENAME


def _code_fingerprint(package_root: Path | None = None) -> str:
    """Cheap, deterministic SHA-256 over the installed package's .py and data/setup files."""

    base = Path(__file__).resolve().parent if package_root is None else Path(package_root).resolve()
    files: dict[str, Path] = {}
    for path in base.rglob("*.py"):
        if "__pycache__" not in path.parts and path.is_file():
            files[path.relative_to(base).as_posix()] = path
    setup_assets = base / "data" / "setup"
    if setup_assets.is_dir():
        for path in setup_assets.rglob("*"):
            if path.is_file():
                files[path.relative_to(base).as_posix()] = path
    digest = hashlib.sha256()
    for relative, path in sorted(files.items()):
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_marker(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PrivateStateError("Oppsettsmarkøren har ugyldig format")
    port = value.get("port")
    instance = value.get("instance")
    protocol = value.get("protocol")
    code_sha256 = value.get("code_sha256")
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or not isinstance(instance, str)
        or not _INSTANCE_ID.fullmatch(instance)
        or type(protocol) is not int
        or not isinstance(code_sha256, str)
        or not code_sha256
    ):
        raise PrivateStateError("Oppsettsmarkøren har ugyldige felter")
    return {
        "port": port,
        "instance": instance,
        "protocol": protocol,
        "code_sha256": code_sha256,
    }


def _read_marker(root: Path) -> dict[str, Any] | None:
    try:
        return read_private_json(marker_path(root), validator=_validate_marker)
    except (PrivateStateError, OSError):
        return None


def _probe_status(
    port: int, instance_id: str, *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> tuple[int | None, dict[str, Any] | None]:
    """Query local /status with X-Setup-Instance, without proxies or redirects."""

    connection = HTTPConnection("127.0.0.1", port, timeout=max(0.1, timeout))
    try:
        connection.request(
            "GET",
            setup_page.STATUS_PATH,
            headers={
                "Host": f"127.0.0.1:{port}",
                "X-Setup-Instance": instance_id,
            },
        )
        response = connection.getresponse()
        raw = response.read(4096)
        status = response.status
    except (OSError, HTTPException):
        return None, None
    finally:
        connection.close()
    if status != 200:
        return status, None
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return status, None
    if not isinstance(body, dict):
        return status, None
    return status, body


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(
        self,
        root: Path,
        *,
        port: int = 0,
        service=None,
        root_source=None,
        instance_id: str | None = None,
    ):
        self.service = service or SetupService(root, root_source=root_source)
        self.token = secrets.token_urlsafe(32)
        self.instance_id = instance_id or secrets.token_urlsafe(24)
        self.closing_event = threading.Event()
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def server_bind(self) -> None:
        if _is_windows():
            self.allow_reuse_address = False
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            super().server_bind()
        except OSError as error:
            if _is_port_busy(error):
                raise _PortBusyError(error.errno, error.strerror) from error
            raise

    def mark_closing(self) -> None:
        self.closing_event.set()

    @property
    def closing(self) -> bool:
        return self.closing_event.is_set() or self.service.closed

    def shutdown(self) -> None:
        self.mark_closing()
        super().shutdown()

    def server_close(self) -> None:
        self.mark_closing()
        self.service.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_args):
        pass

    def _single_header(self, name: str) -> tuple[bool, str | None]:
        values = self.headers.get_all(name)
        if not values:
            return True, None
        if len(values) > 1:
            return False, None
        return True, values[0]

    def _raw_target(self) -> str:
        # BaseHTTPRequestHandler.parse_request collapses leading "//" on self.path;
        # keep the raw request target for exact path checks.
        parts = self.requestline.split()
        return parts[1] if len(parts) >= 2 else self.path

    def _reply(self, status, payload, mime="application/json; charset=utf-8", *,
               extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, dict) else payload
        self.send_response(status)
        for key, value in {
            "Content-Type": mime, "Content-Length": str(len(body)), "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                       "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                                       "frame-ancestors 'none'; form-action 'self'",
            **(extra_headers or {}),
        }.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _handle_status(self) -> None:
        base_headers = {"Vary": "Origin"}
        host_ok, host = self._single_header("Host")
        if not host_ok or host != urlsplit(self.server.origin).netloc:
            self._reply(
                403,
                {"error": "Oppsettet er bare tilgjengelig på denne maskinen."},
                extra_headers=base_headers,
            )
            return
        origin_ok, origin = self._single_header("Origin")
        pages_origin = setup_page.PAGES_ORIGIN
        if not origin_ok or (
            origin is not None and origin not in {pages_origin, self.server.origin}
        ):
            self._reply(
                403,
                {"error": "Åpne oppsettet i det opprinnelige nettleservinduet."},
                extra_headers=base_headers,
            )
            return
        sfs_ok, sfs = self._single_header("Sec-Fetch-Site")
        if not sfs_ok or (origin is None and sfs not in {None, "none", "same-origin"}):
            self._reply(
                403,
                {"error": "Åpne oppsettet i det opprinnelige nettleservinduet."},
                extra_headers=base_headers,
            )
            return
        inst_ok, instance = self._single_header("X-Setup-Instance")
        if not inst_ok or (instance is not None and origin is not None):
            self._reply(
                403,
                {"error": "Åpne oppsettet i det opprinnelige nettleservinduet."},
                extra_headers=base_headers,
            )
            return
        headers = dict(base_headers)
        if origin == pages_origin:
            headers["Access-Control-Allow-Origin"] = pages_origin
        if instance is not None and not hmac.compare_digest(
            instance.encode(), self.server.instance_id.encode()
        ):
            self._reply(409, {"error": "Feil oppsettsinstans."}, extra_headers=headers)
            return
        if self.server.closing:
            self._reply(503, {"error": "Oppsettet avsluttes."}, extra_headers=headers)
            return
        self._reply(
            200,
            {"app": setup_page.STATUS_APP, "protocol": setup_page.STATUS_PROTOCOL},
            extra_headers=headers,
        )

    def _guard(self, *, api=False):
        host_ok, host = self._single_header("Host")
        if not host_ok or host != urlsplit(self.server.origin).netloc:
            self._reply(403, {"error": "Oppsettet er bare tilgjengelig på denne maskinen."})
            return False
        origin_ok, origin = self._single_header("Origin")
        sfs_ok, sfs = self._single_header("Sec-Fetch-Site")
        sfm_ok, sfm = self._single_header("Sec-Fetch-Mode")
        sfd_ok, sfd = self._single_header("Sec-Fetch-Dest")
        if not (origin_ok and sfs_ok and sfm_ok and sfd_ok):
            self._reply(403, {"error": "Åpne oppsettet i det opprinnelige nettleservinduet."})
            return False
        cross_site_root_nav = (
            not api
            and self.command == "GET"
            and self.path == "/"
            and self._raw_target() == "/"
            and origin is None
            and sfs == "cross-site"
            and sfm == "navigate"
            and sfd == "document"
        )
        if not cross_site_root_nav and (
            (origin is not None and origin != self.server.origin) or sfs == "cross-site"
        ):
            self._reply(403, {"error": "Åpne oppsettet i det opprinnelige nettleservinduet."})
            return False
        if api:
            token_ok, token = self._single_header("X-Setup-Token")
            if not token_ok or not hmac.compare_digest((token or "").encode(), self.server.token.encode()):
                self._reply(403, {"error": "Oppsettsvinduet er utløpt. Last siden på nytt."})
                return False
        return True

    def do_GET(self):
        if self.path == setup_page.STATUS_PATH and self._raw_target() == setup_page.STATUS_PATH:
            self._handle_status()
            return
        path = urlsplit(self.path).path
        if not self._guard(api=path.startswith("/api/")):
            return
        if path == "/api/state":
            try:
                self._reply(200, self.server.service.state())
            except Exception:
                self._reply(500, {"error": "Den lokale profilen kunne ikke leses."})
        elif path == "/api/doctor":
            try:
                self._reply(200, self.server.service.doctor())
            except Exception:
                self._reply(500, {"error": "Forhåndssjekken kunne ikke kjøres."})
        elif path == "/api/flyt-municipalities":
            try:
                self._reply(200, {"municipalities": self.server.service.flyt_municipalities()})
            except SetupError as error:
                self._reply(502, {"error": str(error)})
            except Exception:
                self._reply(500, {"error": "Kommunelisten kunne ikke vises."})
        elif path in {"/", "/setup.js", "/setup.css"}:
            name = "index.html" if path == "/" else path[1:]
            payload = (ASSETS / name).read_bytes().replace(b"__SETUP_TOKEN__", self.server.token.encode())
            mime = {"index.html": "text/html", "setup.js": "text/javascript", "setup.css": "text/css"}[name]
            extra = {"Cross-Origin-Opener-Policy": "same-origin"} if path == "/" else None
            self._reply(200, payload, mime + "; charset=utf-8", extra_headers=extra)
        elif path == "/favicon.ico":
            self._reply(204, b"", "image/x-icon")
        else:
            self._reply(404, {"error": "Siden finnes ikke."})

    def do_POST(self):
        if not self._guard(api=True):
            return
        if self.headers.get("Origin") != self.server.origin:
            self._reply(403, {"error": "Forespørselen må komme fra oppsettsvinduet."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY_BYTES or self.headers.get("Transfer-Encoding"):
            self._reply(413, {"error": "Filen er tom eller for stor."})
            return
        if self.headers.get_content_type() != "application/json":
            self._reply(415, {"error": "Oppsettet forventer JSON."})
            return
        try:
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "Forespørselen kunne ikke leses."})
            return
        path = urlsplit(self.path).path
        service = self.server.service
        try:
            if path == "/api/start":
                service.start(value.get("action"), value.get("source", ""),
                              consent=value.get("consent") is True, replace=value.get("replace") is True,
                              municipality=value.get("municipality"))
            elif path == "/api/token":
                service.connect_token(value.get("source", ""), value.get("token"),
                                      replace=value.get("replace") is True)
            elif path == "/api/job":
                if value.get("action") not in {"continue", "cancel"}:
                    raise SetupError("Ukjent oppsettshandling.")
                service.signal(value.get("id"), cancel=value["action"] == "cancel")
            elif path == "/api/connect":
                service.connect_client(value.get("client"))
            elif path == "/api/service":
                service.set_enabled(value.get("source"), value.get("enabled"))
            elif path == "/api/browser":
                service.set_browser(value.get("browser"))
            elif path == "/api/close":
                self.server.mark_closing()
                try:
                    service.close()
                    self._reply(200, {"ok": True})
                finally:
                    # Also when the tab is gone: a wizard that only says 503 holds the lock.
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                self._reply(404, {"error": "Handlingen finnes ikke."})
                return
            self._reply(200, {"ok": True})
        except (SetupError, ClientSetupError) as error:
            self._reply(400, {"error": str(error)})
        except Exception:
            self._reply(500, {"error": "Steget kunne ikke fullføres. Ingen innloggingsdata vises her."})


def _open_url_safely(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _open_browser_after_ready(
    port: int, instance_id: str, url: str, closing: threading.Event
) -> None:
    deadline = time.monotonic() + READY_WAIT_SECONDS
    while not closing.is_set() and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        status, body = _probe_status(port, instance_id, timeout=min(1.0, max(0.1, remaining)))
        if status == 200 and body and body.get("app") == setup_page.STATUS_APP:
            if not closing.is_set():
                _open_url_safely(url)
            return
        if closing.wait(min(READY_POLL_SECONDS, max(0.0, deadline - time.monotonic()))):
            return


def _target_browser_url(origin: str, actual_port: int, *, local: bool) -> str:
    if actual_port == setup_page.SETUP_PORT and not local:
        return setup_page.PAGES_URL
    return origin


def _bind_server(
    root: Path,
    *,
    port: int | None,
    root_source: str | None,
    instance_id: str,
) -> tuple[SetupServer, bool]:
    """Bind SetupServer, retrying the fixed port on Windows before falling back to port 0."""

    if port is not None:
        return (
            SetupServer(root, port=port, root_source=root_source, instance_id=instance_id),
            False,
        )
    fixed_port = setup_page.SETUP_PORT
    deadline = time.monotonic() + WINDOWS_BIND_RETRY_SECONDS if _is_windows() else None
    while True:
        try:
            return (
                SetupServer(
                    root, port=fixed_port, root_source=root_source, instance_id=instance_id
                ),
                False,
            )
        except _PortBusyError:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(WINDOWS_BIND_POLL_SECONDS, remaining))
                    continue
            return (
                SetupServer(root, port=0, root_source=root_source, instance_id=instance_id),
                True,
            )


def _reuse_running(
    root: Path,
    *,
    port: int | None,
    open_browser: bool,
    local: bool,
    fingerprint: str,
    lock_stack: contextlib.ExitStack,
) -> int | None:
    """Wait up to REUSE_WAIT_SECONDS for a running wizard or a released lock.

    Returns an exit code (0 or 1) if reuse handled the call, or None if the
    setup lock was acquired instead (still held on ``lock_stack``).
    """

    deadline = time.monotonic() + REUSE_WAIT_SECONDS
    while True:
        marker = _read_marker(root)
        if marker is not None:
            remaining = max(0.1, deadline - time.monotonic())
            status, body = _probe_status(
                marker["port"],
                marker["instance"],
                timeout=min(PROBE_TIMEOUT_SECONDS, remaining),
            )
            if (
                status == 200
                and body is not None
                and body.get("app") == setup_page.STATUS_APP
                and type(body.get("protocol")) is int
            ):
                running_port = marker["port"]
                origin = f"http://127.0.0.1:{running_port}"
                if (
                    marker["code_sha256"] != fingerprint
                    or marker["protocol"] != setup_page.STATUS_PROTOCOL
                    or body["protocol"] != setup_page.STATUS_PROTOCOL
                ):
                    print(
                        f"En annen versjon av oppsettet kjører allerede på {origin}. "
                        "Avslutt den kjørende veiviseren (med «Avslutt oppsett» eller Ctrl+C) "
                        "og prøv igjen.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1
                if port is not None and port != 0 and port != running_port:
                    print(
                        f"Oppsettet for denne datamappen kjører allerede på {origin}, "
                        f"ikke på port {port}. Avslutt den kjørende veiviseren "
                        "(med «Avslutt oppsett» eller Ctrl+C) først hvis du vil bruke en annen port.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1
                print(f"Åpne lokalt oppsett: {origin}", flush=True)
                if running_port == setup_page.SETUP_PORT:
                    print(f"Fast oppsettsside: {setup_page.PAGES_URL}", flush=True)
                if open_browser:
                    target_url = _target_browser_url(
                        origin, running_port, local=local or port == 0
                    )
                    opener = threading.Thread(
                        target=_open_url_safely, args=(target_url,), daemon=True
                    )
                    opener.start()
                    opener.join(timeout=REUSE_BROWSER_JOIN_SECONDS)
                return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(REUSE_POLL_SECONDS, remaining))
        try:
            lock_stack.enter_context(private_named_lock(root, "setup", wait=False))
            return None
        except PrivateLockBusy:
            continue
    print(
        "Oppsettet for denne datamappen er allerede låst av en annen prosess, men svarer ikke. "
        "Vent litt eller avslutt den andre veiviseren manuelt, og prøv igjen.",
        file=sys.stderr,
        flush=True,
    )
    return 1


def serve(
    root: Path,
    *,
    port: int | None = None,
    open_browser: bool = True,
    local: bool = False,
    root_source: str | None = None,
) -> int:
    if port is not None and (type(port) is not int or not 0 <= port <= 65535):
        print("Ugyldig portnummer. Oppgi et tall mellom 0 og 65535.", file=sys.stderr, flush=True)
        return 1
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fingerprint = _code_fingerprint()

    with contextlib.ExitStack() as lock_stack:
        try:
            lock_stack.enter_context(private_named_lock(root, "setup", wait=False))
        except PrivateLockBusy:
            reused = _reuse_running(
                root,
                port=port,
                open_browser=open_browser,
                local=local,
                fingerprint=fingerprint,
                lock_stack=lock_stack,
            )
            if reused is not None:
                return reused

        # Lock held: clear any stale marker, renew built-in defaults, then bind.
        marker = marker_path(root)
        with contextlib.suppress(PrivateStateError, OSError):
            remove_private_file(marker, missing_ok=True)
        # A newer built-in setup replaces one the wizard stored; never raises.
        refresh_stored_defaults(root)

        instance_id = secrets.token_urlsafe(24)
        try:
            server, fell_back = _bind_server(
                root, port=port, root_source=root_source, instance_id=instance_id
            )
        except _PortBusyError:
            print(
                f"Port {port} er allerede i bruk av et annet program. "
                "Velg en annen port eller bruk standardporten.",
                file=sys.stderr,
                flush=True,
            )
            return 1

        try:
            with server as active:
                actual_port = active.server_port
                atomic_write_private_json(
                    marker,
                    {
                        "port": actual_port,
                        "instance": instance_id,
                        "protocol": setup_page.STATUS_PROTOCOL,
                        "code_sha256": fingerprint,
                    },
                )
                print(f"Åpne lokalt oppsett: {active.origin}", flush=True)
                fixed_port = setup_page.SETUP_PORT
                if actual_port == fixed_port:
                    print(f"Fast oppsettsside: {setup_page.PAGES_URL}", flush=True)
                elif fell_back:
                    print(
                        f"Port {fixed_port} er opptatt, så {setup_page.PAGES_URL} når ikke denne "
                        f"veiviseren. Bruk adressen over: {active.origin}",
                        flush=True,
                    )
                print("Lukk med «Avslutt oppsett» eller Ctrl+C.", flush=True)

                if open_browser:
                    target_url = _target_browser_url(active.origin, actual_port, local=local)
                    threading.Thread(
                        target=_open_browser_after_ready,
                        args=(actual_port, instance_id, target_url, active.closing_event),
                        daemon=True,
                    ).start()
                try:
                    active.serve_forever(poll_interval=0.2)
                except KeyboardInterrupt:
                    pass
                finally:
                    active.mark_closing()
        finally:
            with contextlib.suppress(PrivateStateError, OSError):
                remove_private_file(marker, missing_ok=True)
    return 0
