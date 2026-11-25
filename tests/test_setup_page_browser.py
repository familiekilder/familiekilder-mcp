"""Real-browser acceptance tests for the static GitHub Pages setup page.

Exercises the shipped `pages/` files at their exact public origin
(`https://familiekilder.github.io`, mapped via `--host-resolver-rules` and
`--ip-address-space-overrides` to a local HTTPS server with a runtime
certificate trusted by `--ignore-certificate-errors-spki-list`) against the
real `familiekilder` setup CLI under `python -I`.

Every test runs in an isolated temporary home/config/cache tree, a throwaway
browser profile, and a free loopback port (never port 28417 locally). The
unmodified real-port 28417 test runs only when
`FAMILIEKILDER_TEST_REAL_SETUP_PORT=1` (CI).
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from familiekilder import private_lock, setup_page, setup_web
from familiekilder.private_state import atomic_write_private_json
from familiekilder_testing import headless_browser
from familiekilder_testing import hoopit as fake_hoopit
from familiekilder_testing import setup_page_harness as harness

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAGES_DIR = PROJECT_ROOT / "pages"
REAL_PORT_ENV = "FAMILIEKILDER_TEST_REAL_SETUP_PORT"


def _expected_display_root(root: Path, home: Path) -> str:
    try:
        relative = root.relative_to(home)
    except ValueError:
        return str(root)
    return "~" if relative == Path(".") else str(Path("~") / relative)


class _BrowserAcceptanceBase(unittest.TestCase):
    def setUp(self) -> None:
        self.executable = headless_browser.browser_or_skip(self)
        self.temp_dir = tempfile.TemporaryDirectory(prefix="familiekilder-pages-e2e-")
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.home = self.base / "home"
        self.root = self.home / "familie-data"
        self.env = harness.isolated_environment(self.base, root=self.root)
        self._wizards: list[harness.WizardProcess] = []
        self.addCleanup(self._stop_all_wizards)

    def _track(self, wizard: harness.WizardProcess) -> harness.WizardProcess:
        self._wizards.append(wizard)
        return wizard

    def _stop_all_wizards(self) -> None:
        for wizard in reversed(self._wizards):
            try:
                wizard.stop()
            except Exception:
                pass
        self._wizards.clear()

    def _open_browser(
        self, pages_server: harness.LocalPagesServer
    ) -> headless_browser.HeadlessBrowser:
        return headless_browser.HeadlessBrowser(
            self.executable,
            extra_args=pages_server.browser_flags(),
            env=self.env,
        )

    def _load_pages(self, page: headless_browser.BrowserPage) -> None:
        page.navigate(setup_page.PAGES_URL)
        page.wait_for(
            "location.origin === "
            + json.dumps(setup_page.PAGES_ORIGIN)
            + " && document.readyState === 'complete'"
            + " && document.getElementById('check-panel') !== null"
            + " && !document.getElementById('check-panel').hidden"
            + " && document.getElementById('check-panel').dataset.state === 'idle'"
        )

    def _wait_for_wizard_ready(
        self,
        page: headless_browser.BrowserPage,
        expected_origin: str,
        *,
        timeout: float = 15.0,
    ) -> None:
        page.wait_for(
            "location.origin === "
            + json.dumps(expected_origin)
            + " && document.readyState === 'complete'"
            + " && !!document.getElementById('data-root')"
            + " && document.getElementById('data-root').textContent.startsWith('Datamappe: ')"
            + " && document.querySelectorAll('#services article.service').length > 0",
            timeout=timeout,
        )

    def _hoopit_status(self, page: headless_browser.BrowserPage) -> str | None:
        return page.evaluate(
            """(() => {
              for (const card of document.querySelectorAll('#services article.service')) {
                const heading = card.querySelector('h2');
                if (heading && heading.textContent.trim() === 'Hoopit') {
                  const status = card.querySelector('.status');
                  return status ? status.textContent.trim() : null;
                }
              }
              return null;
            })()"""
        )

    def _assert_lna_control_blocks(
        self,
        page: headless_browser.BrowserPage,
        status_log: Path,
        *,
        local_port: int,
    ) -> None:
        """Step 0: prove Local Network Access blocks loopback fetch before permission is granted."""

        self._load_pages(page)
        # Clear any startup probes from the CLI itself before clicking.
        before_origin_hits = [
            entry
            for entry in harness.read_status_log(status_log)
            if entry.get("origin") == setup_page.PAGES_ORIGIN
        ]
        self.assertEqual(before_origin_hits, [])

        # Confirm the wizard really is up on loopback before testing the browser block.
        harness.wait_for_status(local_port)

        page.click("#check-button")
        state = page.wait_for(
            "(() => {"
            "  const s = document.getElementById('check-panel').dataset.state;"
            "  return (s === 'no-contact' || s === 'contact') ? s : null;"
            "})()"
        )
        origin_hits = [
            entry
            for entry in harness.read_status_log(status_log)
            if entry.get("origin") == setup_page.PAGES_ORIGIN
        ]
        if state != "no-contact" or origin_hits:
            self.fail(
                "Local Network Access did not block loopback fetch without permission: "
                f"page state={state!r}, server received {len(origin_hits)} request(s) "
                f"from {setup_page.PAGES_ORIGIN}"
            )


class SetupPageBrowserAcceptanceTests(_BrowserAcceptanceBase):
    def test_full_reopen_lifecycle_in_real_browser(self) -> None:
        """Steps 0-4: LNA control, open wizard, save settings, hard-kill, reload, reopen."""

        local_port = harness.free_port()
        local_origin = f"http://127.0.0.1:{local_port}"
        browser_log = self.base / "opened-urls.txt"
        status_log = self.base / "status-hits.jsonl"
        expected_footer = f"Datamappe: {_expected_display_root(self.root, self.home)}"

        with harness.LocalPagesServer(PAGES_DIR, local_port=local_port) as pages_server:
            # Step 1a: start the real CLI without --port; verify captured URL is PAGES_URL.
            wizard1 = self._track(
                harness.spawn_cli_wizard(
                    self.root,
                    self.env,
                    setup_port=local_port,
                    browser_log=browser_log,
                    status_log=status_log,
                    fake_hoopit=True,
                )
            )
            bound_port = wizard1.wait_for_port()
            self.assertEqual(bound_port, local_port)
            harness.wait_for_status(local_port)
            opened_url = harness.wait_for_browser_url(browser_log)
            self.assertEqual(opened_url, setup_page.PAGES_URL)

            with self._open_browser(pages_server) as browser:
                pages_page = browser.page
                pages_target_id = pages_page.target_id

                # Step 0: control — LNA blocks fetch while the wizard is up and gets 0 hits.
                self._assert_lna_control_blocks(pages_page, status_log, local_port=local_port)

                # Step 1b: reload to fresh idle state, grant permission, check, real-click link.
                pages_page.reload()
                pages_page.wait_for(
                    "document.readyState === 'complete'"
                    " && document.getElementById('check-panel')?.dataset.state === 'idle'"
                    " && document.getElementById('check-button')?.textContent.trim() === 'Sjekk tilkoblingen'"
                )
                self.assertEqual(pages_page.target_id, pages_target_id)

                browser.set_loopback_permission(setup_page.PAGES_ORIGIN, "granted")
                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'contact'"
                )
                self.assertEqual(
                    pages_page.evaluate(
                        "document.getElementById('status-title').textContent.trim()"
                    ),
                    "Familiekilder kjører på denne maskinen",
                )
                self.assertFalse(
                    pages_page.evaluate("document.getElementById('continue-link').hidden")
                )
                self.assertFalse(
                    pages_page.evaluate("document.getElementById('other-address-note').hidden")
                )

                known_targets = browser.target_ids()
                pages_page.click("#continue-link")
                old_wizard = browser.wait_for_new_page(known_targets, url_prefix=f"{local_origin}/")
                self.assertNotEqual(old_wizard.target_id, pages_target_id)
                self._wait_for_wizard_ready(old_wizard, local_origin)
                self.assertEqual(
                    old_wizard.evaluate("document.getElementById('data-root').textContent.trim()"),
                    expected_footer,
                )

                # Step 2: save login browser choice, Codex connection, and fictional Hoopit token.
                selectable_browsers: list[str] = old_wizard.wait_for(
                    "(() => {"
                    "  const opts = Array.from(document.getElementById('login-browser').options)"
                    "    .map((o) => o.value).filter((v) => v && v !== 'auto');"
                    "  return opts.length > 0 ? opts : null;"
                    "})()"
                )
                chosen_browser = selectable_browsers[0]
                old_wizard.select_option("#login-browser", chosen_browser)
                old_wizard.wait_for(
                    "document.getElementById('login-browser').value === "
                    + json.dumps(chosen_browser)
                )
                # Confirm choice actually reached disk under the isolated root.
                browser_choice_file = self.root / ".private" / "login-browser.json"
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and not browser_choice_file.exists():
                    time.sleep(0.05)
                self.assertTrue(browser_choice_file.exists(), "login-browser.json was not written")

                # Connect fictional Hoopit token through the token dialog.
                old_wizard.click_text("#services article.service button", "Koble til med token")
                old_wizard.wait_for("document.getElementById('token-dialog').open === true")
                old_wizard.fill("#token", fake_hoopit.TOKEN)
                old_wizard.click("#token-submit")
                old_wizard.wait_for("document.getElementById('token-dialog').open === false")
                old_wizard.wait_for(
                    """(() => {
                      for (const card of document.querySelectorAll('#services article.service')) {
                        const h = card.querySelector('h2');
                        if (h && h.textContent.trim() === 'Hoopit') {
                          const s = card.querySelector('.status');
                          return s && s.textContent.trim() === 'Token lagret';
                        }
                      }
                      return false;
                    })()"""
                )
                self.assertEqual(self._hoopit_status(old_wizard), "Token lagret")

                # Save Codex connection through Step 2 UI.
                old_wizard.click('button.step[data-step="2"]')
                old_wizard.wait_for("!document.getElementById('step-2').hidden")
                old_wizard.click('button.connect[data-client="codex"]')
                old_wizard.wait_for(
                    "document.getElementById('codex-status').textContent.trim() === 'Koblingen er lagt til.'"
                )
                self.assertTrue((self.home / ".codex" / "config.toml").is_file())

                # Step 3: hard-kill the wizard process itself.
                wizard1.kill()

                # The old wizard tab must show the Pages link message (offline on fixed port).
                old_wizard.wait_for(
                    "(() => {"
                    "  const box = document.getElementById('error');"
                    "  if (!box || box.hidden) return false;"
                    "  const link = box.querySelector('a');"
                    "  return !!link && link.href === " + json.dumps(setup_page.PAGES_URL) + ";"
                    "})()"
                )
                self.assertIn(
                    "Oppsettet svarer ikke lenger",
                    old_wizard.evaluate("document.getElementById('error').textContent"),
                )

                # Reload the SAME Pages target (same targetId); must visibly show agent message & direct link.
                pages_page.reload()
                pages_page.wait_for(
                    "document.readyState === 'complete'"
                    " && document.getElementById('check-panel')?.dataset.state === 'idle'"
                )
                self.assertEqual(pages_page.target_id, pages_target_id)
                visible_help = pages_page.evaluate(
                    """(() => {
                      const msg = document.getElementById('agent-message');
                      const link = document.getElementById('direct-link');
                      const isVisible = (el) => {
                        if (!el || el.hidden) return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                      };
                      return {
                        msgVisible: isVisible(msg),
                        msgText: msg ? msg.textContent : '',
                        linkVisible: isVisible(link),
                        linkHref: link ? link.href : '',
                        linkText: link ? link.textContent.trim() : '',
                      };
                    })()"""
                )
                self.assertTrue(visible_help["msgVisible"])
                self.assertIn("setup --no-open", visible_help["msgText"])
                self.assertTrue(visible_help["linkVisible"])
                self.assertEqual(visible_help["linkHref"], f"{local_origin}/")
                self.assertEqual(visible_help["linkText"], "Åpne veiviseren direkte")

                # Check -> "Vi får ikke kontakt …"
                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'no-contact'"
                )
                self.assertIn(
                    "Vi får ikke kontakt",
                    pages_page.evaluate("document.getElementById('status-title').textContent"),
                )
                self.assertTrue(
                    pages_page.evaluate("document.getElementById('status-denied').hidden")
                )

                # Step 4: start the same installation again with --no-open; nothing opened.
                previous_browser_opens = len(
                    [
                        line
                        for line in browser_log.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                )
                wizard2 = self._track(
                    harness.spawn_cli_wizard(
                        self.root,
                        self.env,
                        cli_args=("--no-open",),
                        setup_port=local_port,
                        browser_log=browser_log,
                        status_log=status_log,
                        fake_hoopit=True,
                    )
                )
                self.assertEqual(wizard2.wait_for_port(), local_port)
                harness.wait_for_status(local_port)
                current_browser_opens = len(
                    [
                        line
                        for line in browser_log.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                )
                self.assertEqual(
                    current_browser_opens,
                    previous_browser_opens,
                    "--no-open must not call webbrowser.open",
                )

                # Click "Prøv igjen" in the SAME target without reload -> contact.
                self.assertEqual(pages_page.target_id, pages_target_id)
                self.assertEqual(
                    pages_page.evaluate(
                        "document.getElementById('check-button').textContent.trim()"
                    ),
                    "Prøv igjen",
                )
                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'contact'"
                )

                # Open the link -> new target shows saved token, client, browser choice and data folder.
                known_targets = browser.target_ids()
                pages_page.click("#continue-link")
                new_wizard = browser.wait_for_new_page(known_targets, url_prefix=f"{local_origin}/")
                self.assertNotIn(new_wizard.target_id, {pages_target_id, old_wizard.target_id})
                self._wait_for_wizard_ready(new_wizard, local_origin)

                self.assertEqual(self._hoopit_status(new_wizard), "Token lagret")
                self.assertEqual(
                    new_wizard.evaluate("document.getElementById('login-browser').value"),
                    chosen_browser,
                )
                self.assertEqual(
                    new_wizard.evaluate("document.getElementById('data-root').textContent.trim()"),
                    expected_footer,
                )
                new_wizard.click('button.step[data-step="2"]')
                new_wizard.wait_for("!document.getElementById('step-2').hidden")
                self.assertEqual(
                    new_wizard.evaluate(
                        "document.getElementById('codex-status').textContent.trim()"
                    ),
                    "Koblingen er lagt til.",
                )

                # Old wizard tab receives 403 from the restarted server -> says reload, and reloading works.
                old_wizard.wait_for(
                    "(() => {"
                    "  const box = document.getElementById('error');"
                    "  if (!box || box.hidden) return false;"
                    "  const btn = box.querySelector('button');"
                    "  return !!btn && btn.textContent.trim() === 'Last siden på nytt';"
                    "})()"
                )
                self.assertIsNone(
                    old_wizard.evaluate("document.getElementById('error').querySelector('a')")
                )
                old_wizard.click_text("#error button", "Last siden på nytt")
                old_wizard.wait_for(
                    "document.readyState === 'complete'"
                    " && document.getElementById('error').hidden === true"
                    " && document.getElementById('data-root').textContent.trim() === "
                    + json.dumps(expected_footer)
                )
                self.assertEqual(self._hoopit_status(old_wizard), "Token lagret")

    def test_permission_denied_shows_note_and_direct_link_still_opens_wizard(self) -> None:
        local_port = harness.free_port()
        local_origin = f"http://127.0.0.1:{local_port}"
        expected_footer = f"Datamappe: {_expected_display_root(self.root, self.home)}"

        wizard = self._track(
            harness.spawn_cli_wizard(
                self.root,
                self.env,
                cli_args=("--no-open",),
                setup_port=local_port,
            )
        )
        self.assertEqual(wizard.wait_for_port(), local_port)
        harness.wait_for_status(local_port)

        with harness.LocalPagesServer(PAGES_DIR, local_port=local_port) as pages_server:
            with self._open_browser(pages_server) as browser:
                pages_page = browser.page
                self._load_pages(pages_page)
                browser.set_loopback_permission(setup_page.PAGES_ORIGIN, "denied")

                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'no-contact'"
                )
                self.assertFalse(
                    pages_page.evaluate("document.getElementById('status-denied').hidden")
                )
                self.assertIn(
                    "blokkert",
                    pages_page.evaluate("document.getElementById('status-denied').textContent"),
                )
                self.assertTrue(
                    pages_page.evaluate("document.getElementById('continue-link').hidden")
                )

                # Direct link still opens the wizard even when loopback permission is denied.
                known_targets = browser.target_ids()
                pages_page.click("#direct-link")
                wizard_page = browser.wait_for_new_page(
                    known_targets, url_prefix=f"{local_origin}/"
                )
                self._wait_for_wizard_ready(wizard_page, local_origin)
                self.assertEqual(
                    wizard_page.evaluate("document.getElementById('data-root').textContent.trim()"),
                    expected_footer,
                )

    def test_foreign_http_server_on_port_shows_no_contact_and_wizard_falls_back(self) -> None:
        local_port = harness.free_port()
        browser_log = self.base / "foreign-opened-urls.txt"

        class ForeignHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = b'{"status":"foreign-service"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", setup_page.PAGES_ORIGIN)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        class ForeignServer(http.server.ThreadingHTTPServer):
            allow_reuse_address = False

            def server_bind(self) -> None:
                if os.name == "nt":
                    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                    if exclusive is not None:
                        self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                super().server_bind()

        foreign = ForeignServer(("127.0.0.1", local_port), ForeignHandler)
        foreign_thread = threading.Thread(target=foreign.serve_forever, daemon=True)
        foreign_thread.start()
        self.addCleanup(foreign_thread.join, 5)
        self.addCleanup(foreign.server_close)
        self.addCleanup(foreign.shutdown)

        # Start wizard without --port: falls back to a free port, prints it, opens local URL directly.
        wizard = self._track(
            harness.spawn_cli_wizard(
                self.root,
                self.env,
                setup_port=local_port,
                browser_log=browser_log,
            )
        )
        fallback_port = wizard.wait_for_port(timeout=20.0)
        self.assertNotEqual(fallback_port, local_port)
        fallback_origin = f"http://127.0.0.1:{fallback_port}"
        harness.wait_for_status(fallback_port)
        opened_url = harness.wait_for_browser_url(browser_log)
        self.assertEqual(opened_url, fallback_origin)
        self.assertIn(fallback_origin, wizard.stdout_text)
        self.assertIn(f"Port {local_port} er opptatt", wizard.stdout_text)

        with harness.LocalPagesServer(PAGES_DIR, local_port=local_port) as pages_server:
            with self._open_browser(pages_server) as browser:
                pages_page = browser.page
                self._load_pages(pages_page)
                browser.set_loopback_permission(setup_page.PAGES_ORIGIN, "granted")

                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'no-contact'"
                )
                self.assertTrue(
                    pages_page.evaluate("document.getElementById('continue-link').hidden")
                )
                self.assertTrue(
                    pages_page.evaluate("document.getElementById('status-denied').hidden")
                )

                # Confirm the fallback local address really serves the wizard.
                fallback_page = browser.new_page(f"{fallback_origin}/")
                self._wait_for_wizard_ready(fallback_page, fallback_origin)

    def test_unresponsive_listener_times_out_after_five_seconds(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(0.5)
        local_port = int(listener.getsockname()[1])

        stop_event = threading.Event()
        accepted: list[socket.socket] = []

        def accept_and_hang() -> None:
            while not stop_event.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                accepted.append(conn)

        thread = threading.Thread(target=accept_and_hang, daemon=True)
        thread.start()

        def cleanup_listener() -> None:
            stop_event.set()
            listener.close()
            for conn in accepted:
                try:
                    conn.close()
                except OSError:
                    pass
            thread.join(timeout=5)

        self.addCleanup(cleanup_listener)

        with harness.LocalPagesServer(PAGES_DIR, local_port=local_port) as pages_server:
            with self._open_browser(pages_server) as browser:
                pages_page = browser.page
                self._load_pages(pages_page)
                browser.set_loopback_permission(setup_page.PAGES_ORIGIN, "granted")

                started = time.monotonic()
                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'checking'"
                )
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'no-contact'",
                    timeout=12.0,
                )
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 4.5)
                self.assertLess(elapsed, 11.0)
                self.assertIn(
                    "Vi får ikke kontakt",
                    pages_page.evaluate("document.getElementById('status-title').textContent"),
                )
                self.assertTrue(
                    pages_page.evaluate("document.getElementById('status-denied').hidden")
                )

    def test_second_root_falls_back_and_page_shows_other_address_note(self) -> None:
        local_port = harness.free_port()
        local_origin = f"http://127.0.0.1:{local_port}"
        root_a = self.home / "profil-a"
        root_b = self.home / "profil-b"
        status_log_a = self.base / "root-a-status.jsonl"
        root_b_browser_log = self.base / "root-b-opened-urls.txt"

        # Root A holds the fixed port.
        wizard_a = self._track(
            harness.spawn_cli_wizard(
                root_a,
                self.env,
                cli_args=("--no-open",),
                setup_port=local_port,
                status_log=status_log_a,
            )
        )
        self.assertEqual(wizard_a.wait_for_port(), local_port)
        harness.wait_for_status(local_port)

        # Give Root B a stale marker pointing to Root A's port and hold Root B's setup lock
        # until Root B probes Root A's /status with its stale X-Setup-Instance. If the
        # server's instance check were bypassed, Root B would wrongly reuse Root A.
        root_b.mkdir(parents=True, exist_ok=True)
        atomic_write_private_json(
            setup_web.marker_path(root_b),
            {
                "port": local_port,
                "instance": "stale-root-b-instance-id",
                "protocol": setup_page.STATUS_PROTOCOL,
                "code_sha256": setup_web._code_fingerprint(),
            },
        )
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_root_b_lock_until_probed() -> None:
            with private_lock.private_named_lock(root_b, "setup", wait=False):
                lock_acquired.set()
                release_lock.wait(timeout=10.0)

        lock_thread = threading.Thread(target=hold_root_b_lock_until_probed, daemon=True)
        lock_thread.start()
        self.addCleanup(lock_thread.join, 5)
        self.addCleanup(release_lock.set)
        self.assertTrue(lock_acquired.wait(timeout=5.0))

        prior_instance_probes = sum(
            1 for entry in harness.read_status_log(status_log_a) if entry.get("instance")
        )
        wizard_b = self._track(
            harness.spawn_cli_wizard(
                root_b,
                self.env,
                setup_port=local_port,
                browser_log=root_b_browser_log,
            )
        )
        probe_deadline = time.monotonic() + 10.0
        while time.monotonic() < probe_deadline:
            current_probes = sum(
                1 for entry in harness.read_status_log(status_log_a) if entry.get("instance")
            )
            if current_probes > prior_instance_probes or wizard_b.process.poll() is not None:
                break
            time.sleep(0.05)
        release_lock.set()
        lock_thread.join(timeout=5)
        self.assertGreater(
            sum(1 for entry in harness.read_status_log(status_log_a) if entry.get("instance")),
            prior_instance_probes,
            "Root B never probed Root A with its stale X-Setup-Instance",
        )

        port_b = wizard_b.wait_for_port(timeout=20.0)
        self.assertNotEqual(port_b, local_port)
        origin_b = f"http://127.0.0.1:{port_b}"
        harness.wait_for_status(port_b)
        self.assertEqual(harness.wait_for_browser_url(root_b_browser_log), origin_b)
        self.assertIn(f"Port {local_port} er opptatt", wizard_b.stdout_text)

        with harness.LocalPagesServer(PAGES_DIR, local_port=local_port) as pages_server:
            with self._open_browser(pages_server) as browser:
                pages_page = browser.page
                self._load_pages(pages_page)
                browser.set_loopback_permission(setup_page.PAGES_ORIGIN, "granted")

                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'contact'"
                )
                self.assertFalse(
                    pages_page.evaluate("document.getElementById('other-address-note').hidden")
                )
                note_text = " ".join(
                    pages_page.evaluate(
                        "document.getElementById('other-address-note').textContent"
                    ).split()
                )
                self.assertIn("annen profil", note_text)

                # Opening via Pages reaches Root A; opening Root B's fallback URL reaches Root B.
                known_targets = browser.target_ids()
                pages_page.click("#continue-link")
                page_a = browser.wait_for_new_page(known_targets, url_prefix=f"{local_origin}/")
                self._wait_for_wizard_ready(page_a, local_origin)
                self.assertEqual(
                    page_a.evaluate("document.getElementById('data-root').textContent.trim()"),
                    f"Datamappe: {_expected_display_root(root_a, self.home)}",
                )

                page_b = browser.new_page(f"{origin_b}/")
                self._wait_for_wizard_ready(page_b, origin_b)
                self.assertEqual(
                    page_b.evaluate("document.getElementById('data-root').textContent.trim()"),
                    f"Datamappe: {_expected_display_root(root_b, self.home)}",
                )

    def test_cross_site_page_cannot_read_status_or_wizard_or_frame_wizard(self) -> None:
        local_port = harness.free_port()
        local_origin = f"http://127.0.0.1:{local_port}"
        status_log = self.base / "cross-site-status.jsonl"

        wizard = self._track(
            harness.spawn_cli_wizard(
                self.root,
                self.env,
                cli_args=("--no-open",),
                setup_port=local_port,
                status_log=status_log,
            )
        )
        self.assertEqual(wizard.wait_for_port(), local_port)
        harness.wait_for_status(local_port)

        with harness.LocalPagesServer(PAGES_DIR, local_port=local_port) as pages_server:
            with self._open_browser(pages_server) as browser:
                cross_page = browser.page
                cross_page.navigate(harness.CROSS_SITE_URL)
                cross_page.wait_for(
                    "location.origin === "
                    + json.dumps(harness.CROSS_SITE_ORIGIN)
                    + " && document.readyState === 'complete'"
                )
                # Grant loopback permission to the foreign public origin so fetch actually reaches
                # the wizard and proves the wizard's Origin/CORS/guard rules block reading.
                browser.set_loopback_permission(harness.CROSS_SITE_ORIGIN, "granted")

                for path in ("/status", "/"):
                    with self.subTest(path=path):
                        outcome = cross_page.evaluate(
                            f"""(async () => {{
                              try {{
                                const response = await fetch({json.dumps(local_origin + path)}, {{
                                  credentials: 'omit',
                                  cache: 'no-store',
                                }});
                                const text = await response.text();
                                return {{ ok: true, status: response.status, len: text.length }};
                              }} catch (error) {{
                                return {{ ok: false, name: error.name, message: String(error) }};
                              }}
                            }})()""",
                            await_promise=True,
                        )
                        self.assertFalse(
                            outcome["ok"], f"Cross-site fetch to {path} succeeded: {outcome}"
                        )

                # Confirm /status really saw the foreign Origin and rejected it rather than LNA hiding it.
                foreign_hits = [
                    entry
                    for entry in harness.read_status_log(status_log)
                    if entry.get("origin") == harness.CROSS_SITE_ORIGIN
                ]
                self.assertGreaterEqual(len(foreign_hits), 1)

                # Attempt to frame the wizard in an <iframe>; must be blocked.
                cross_page.evaluate(
                    f"""(() => {{
                      window.__iframeLoaded = false;
                      const frame = document.createElement('iframe');
                      frame.id = 'attack-frame';
                      frame.src = {json.dumps(local_origin + '/')};
                      frame.addEventListener('load', () => {{ window.__iframeLoaded = true; }});
                      document.body.appendChild(frame);
                      return true;
                    }})()"""
                )
                cross_page.wait_for("window.__iframeLoaded === true", timeout=10.0)

                # Cross-origin iframes run as out-of-process iframe targets under Site Isolation.
                iframe_targets = browser.iframe_targets(cross_page.target_id)
                if iframe_targets:
                    iframe_page = browser.attach(str(iframe_targets[0]["targetId"]))
                    child = iframe_page.frame_tree()["frameTree"]["frame"]
                    self.assertNotEqual(child.get("url"), f"{local_origin}/")
                    self.assertEqual(child.get("unreachableUrl"), f"{local_origin}/")
                    self.assertIsNone(
                        iframe_page.evaluate("document.querySelector('meta[name=\"setup-token\"]')")
                    )
                else:
                    tree = cross_page.frame_tree()["frameTree"]
                    child_frames = tree.get("childFrames") or []
                    self.assertEqual(len(child_frames), 1)
                    child = child_frames[0]["frame"]
                    self.assertNotEqual(child.get("url"), f"{local_origin}/")
                    self.assertEqual(child.get("unreachableUrl"), f"{local_origin}/")


@unittest.skipUnless(
    os.environ.get(REAL_PORT_ENV) == "1",
    f"Real fixed-port 28417 test runs only when {REAL_PORT_ENV}=1 (CI)",
)
class SetupPageRealPortBrowserTests(_BrowserAcceptanceBase):
    def test_real_setup_port_unmodified_pages_and_cli(self) -> None:
        """CI-only: unmodified pages/ and unmodified `python -I -m familiekilder` on port 28417."""

        local_port = setup_page.SETUP_PORT
        local_origin = f"http://127.0.0.1:{local_port}"
        expected_footer = f"Datamappe: {_expected_display_root(self.root, self.home)}"

        wizard1 = self._track(
            harness.spawn_cli_wizard(
                self.root,
                self.env,
                cli_args=("--no-open",),
                unmodified_cli=True,
            )
        )
        self.assertEqual(wizard1.wait_for_port(), local_port)
        harness.wait_for_status(local_port)
        self.assertIn(f"Fast oppsettsside: {setup_page.PAGES_URL}", wizard1.stdout_text)

        with harness.LocalPagesServer(PAGES_DIR, local_port=local_port) as pages_server:
            with self._open_browser(pages_server) as browser:
                pages_page = browser.page
                pages_target_id = pages_page.target_id

                # Control: LNA blocks before grant.
                self._load_pages(pages_page)
                pages_page.click("#check-button")
                state = pages_page.wait_for(
                    "(() => {"
                    "  const s = document.getElementById('check-panel').dataset.state;"
                    "  return (s === 'no-contact' || s === 'contact') ? s : null;"
                    "})()"
                )
                if state != "no-contact":
                    self.fail(f"Local Network Access did not block on real port {local_port}")

                # Grant permission, check, open unmodified wizard on port 28417.
                browser.set_loopback_permission(setup_page.PAGES_ORIGIN, "granted")
                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'contact'"
                )
                known_targets = browser.target_ids()
                pages_page.click("#continue-link")
                old_wizard = browser.wait_for_new_page(known_targets, url_prefix=f"{local_origin}/")
                self._wait_for_wizard_ready(old_wizard, local_origin)
                self.assertEqual(
                    old_wizard.evaluate("document.getElementById('data-root').textContent.trim()"),
                    expected_footer,
                )

                # Hard-kill unmodified wizard -> old tab links to Pages, Pages check fails.
                wizard1.kill()
                old_wizard.wait_for(
                    "(() => {"
                    "  const box = document.getElementById('error');"
                    "  const link = box && !box.hidden && box.querySelector('a');"
                    "  return !!link && link.href === " + json.dumps(setup_page.PAGES_URL) + ";"
                    "})()"
                )
                pages_page.reload()
                pages_page.wait_for(
                    "document.readyState === 'complete'"
                    " && document.getElementById('check-panel')?.dataset.state === 'idle'"
                )
                self.assertEqual(pages_page.target_id, pages_target_id)
                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'no-contact'"
                )

                # Restart unmodified CLI on real port 28417 -> retry in same tab succeeds.
                wizard2 = self._track(
                    harness.spawn_cli_wizard(
                        self.root,
                        self.env,
                        cli_args=("--no-open",),
                        unmodified_cli=True,
                    )
                )
                self.assertEqual(wizard2.wait_for_port(), local_port)
                harness.wait_for_status(local_port)
                pages_page.click("#check-button")
                pages_page.wait_for(
                    "document.getElementById('check-panel').dataset.state === 'contact'"
                )
                old_wizard.wait_for(
                    "(() => {"
                    "  const box = document.getElementById('error');"
                    "  const btn = box && !box.hidden && box.querySelector('button');"
                    "  return !!btn && btn.textContent.trim() === 'Last siden på nytt';"
                    "})()"
                )
                old_wizard.click_text("#error button", "Last siden på nytt")
                old_wizard.wait_for(
                    "document.readyState === 'complete'"
                    " && document.getElementById('error').hidden === true"
                    " && document.getElementById('data-root').textContent.trim() === "
                    + json.dumps(expected_footer)
                )


if __name__ == "__main__":
    unittest.main()
