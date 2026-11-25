from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from familiekilder import flyt_auth
from familiekilder import setup_page
from familiekilder.browser_bootstrap import BrowserClosedError
from familiekilder.client_defaults import default_client_config
from familiekilder.connection_status import auth_module
from familiekilder.setup_service import Job, SetupError, SetupService
from familiekilder.live_config import Config
from familiekilder.private_state import (
    atomic_write_private_bytes, atomic_write_private_json, ensure_private_directory,
)
from familiekilder.setup_web import SetupServer
from familiekilder.source_adapter import SourceUnavailable
from familiekilder.sources import REGISTRY
from familiekilder_testing.flyt import FakeMunicipalityTransport

AUTOMATIC_SOURCES = [source for source in REGISTRY if default_client_config(source) is not None]


def store_fake_session(path):
    # Only the presence of the file matters to the wizard.
    ensure_private_directory(path.parent)
    atomic_write_private_json(path, {"refresh_token": "fictional-refresh-token"})


def default_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from default_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from default_strings(item)
    elif isinstance(value, str):
        yield value


class SetupServerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.service = SetupService(self.root, home=self.home)
        self.server = SetupServer(self.root, service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, *, method="GET", value=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"X-Setup-Token": self.server.token}
        body = None
        if value is not None:
            body = json.dumps(value)
            request_headers.update({"Content-Type": "application/json", "Origin": self.server.origin})
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result


class SetupHttpTests(SetupServerCase):
    def test_page_and_state_are_local_and_do_not_disclose_credentials(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _, body = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["services"]), len(REGISTRY))
        self.assertNotIn(b"client_secret", body)
        self.assertFalse(self.root.exists())

    def test_state_says_how_each_service_is_set_up_without_its_values(self):
        status, _, body = self.request("/api/state")
        self.assertEqual(status, 200)
        services = {row["id"]: row for row in json.loads(body)["services"]}
        # A source whose default is withdrawn is shown as unavailable.
        expected = {source: "unavailable" for source in services}
        expected.update({source: "automatic" for source in AUTOMATIC_SOURCES})
        expected.update({"flyt": "municipality", "hoopit": "token"})
        self.assertEqual({source: row["setup"] for source, row in services.items()}, expected)
        self.assertIn("spond", AUTOMATIC_SOURCES)
        for row in services.values():
            self.assertNotIn("automatic_setup", row)
        text = body.decode()
        for source in AUTOMATIC_SOURCES:
            for value in default_strings(default_client_config(source)):
                if len(value) >= 12:
                    self.assertNotIn(value, text)

    def test_a_source_without_built_in_setup_is_unavailable_in_the_wizard(self):
        with patch("familiekilder.setup_service.default_client_config", return_value=None):
            rows = {row["id"]: row for row in self.service.state()["services"]}
            self.assertEqual(rows["vigilo"]["setup"], "unavailable")
            status, _, body = self.request(
                "/api/start", method="POST", value={"action": "login", "source": "vigilo", "consent": True})
        self.assertEqual(status, 400)
        self.assertIn("kan ikke settes opp i veiviseren", json.loads(body)["error"])
        self.assertIsNone(self.service.job)

    def test_dns_rebinding_cross_origin_and_missing_token_are_rejected(self):
        for headers in (
            {"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"},
            {"X-Setup-Token": "wrong"}, {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/state", headers=headers)[0], 403)
                self.assertEqual(self.request("/api/connect", method="POST", value={"client": "codex"}, headers=headers)[0], 403)
        self.assertFalse((self.home / ".codex").exists())

    def test_client_registration_needs_explicit_valid_post(self):
        self.assertEqual(self.request("/api/connect?client=codex")[0], 404)
        self.assertFalse((self.home / ".codex").exists())
        status, _, _ = self.request("/api/connect", method="POST", value={"client": "codex"})
        self.assertEqual(status, 200)
        self.assertTrue((self.home / ".codex/config.toml").exists())

    def test_login_browser_is_shown_and_chosen_by_explicit_post(self):
        from familiekilder.browsers import BrowserApp, saved_browser

        apps = (BrowserApp("chrome", "Google Chrome", ()), BrowserApp("edge", "Microsoft Edge", ()))
        with patch("familiekilder.setup_service.installed_browsers", return_value=apps):
            state = json.loads(self.request("/api/state")[2])
            self.assertEqual(
                state["browsers"],
                {
                    "installed": [
                        {"id": "chrome", "name": "Google Chrome"},
                        {"id": "edge", "name": "Microsoft Edge"},
                    ],
                    "chosen": "auto",
                    "used": "chrome",
                },
            )
            self.assertTrue(state["dependencies"]["browser"])
            status, _, _ = self.request("/api/browser", method="POST", value={"browser": "edge"})
            self.assertEqual(status, 200)
            self.assertEqual(saved_browser(self.root), "edge")
            self.assertEqual(json.loads(self.request("/api/state")[2])["browsers"]["used"], "edge")
            for value in ("/usr/bin/evil", "firefox", None):
                status, _, _ = self.request("/api/browser", method="POST", value={"browser": value})
                self.assertEqual(status, 400)
            self.assertEqual(saved_browser(self.root), "edge")
            self.request("/api/browser", method="POST", value={"browser": "auto"})
            self.assertIsNone(saved_browser(self.root))

    def test_doctor_gives_the_same_report_as_the_command_behind_the_same_protection(self):
        from familiekilder import browsers, doctor
        from familiekilder_testing.browsers import FILE, FakeBrowserSystem, stat_result

        local = r"C:\Brukere\Qzorbatnik\AppData\Local"
        system = FakeBrowserSystem(
            browsers.WINDOWS,
            {
                local + r"\Google\Chrome\Application\chrome.exe": stat_result(FILE),
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe": stat_result(FILE),
            },
            home=r"C:\Brukere\Qzorbatnik",
            environ={"USERNAME": "Qzorbatnik"},
            local_app_data=local,
        )
        with (
            patch.object(browsers, "System", return_value=system),
            patch("familiekilder.desktop_platform.password_backend", return_value=object()),
        ):
            for headers in ({"X-Setup-Token": "wrong"}, {"Host": "attacker.invalid"}):
                with self.subTest(headers=headers):
                    self.assertEqual(self.request("/api/doctor", headers=headers)[0], 403)
            self.assertEqual(self.request("/api/doctor", method="POST", value={})[0], 404)
            status, headers, body = self.request("/api/doctor")
            expected = doctor.render(
                doctor.run(self.root, root_source=self.service.root_source, client_home=self.home)
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        result = json.loads(body)
        self.assertEqual(result["text"], expected)
        self.assertIn("familiekilder-mcp doctor", result["text"])
        self.assertNotIn("qzorbatnik", body.decode().lower())
        chrome = next(item for item in result["report"]["browsers"]["items"] if item["browser"] == "chrome")
        self.assertEqual(chrome["status"], "rejected")
        self.assertEqual(chrome["reason"], "Google Chrome er bare installert for din bruker, under AppData.")
        self.assertEqual(result["report"]["browsers"]["candidate"], "edge")
        self.assertIsNone(result["report"]["browsers"]["selected"])
        self.assertTrue(system.calls)
        self.assertTrue(all("Get-AuthenticodeSignature" not in call[-1] for call in system.calls))
        self.assertFalse(self.root.exists())

    def test_the_wizard_takes_no_client_files(self):
        content = json.dumps({"client": "fictional"})
        for source in REGISTRY:
            with self.subTest(source=source):
                status, _, _ = self.request("/api/client-file", method="POST", value={"source": source, "content": content})
                self.assertEqual(status, 404)
        self.assertFalse((self.root / ".private/credentials").exists())
        self.assertEqual(self.request("/help/client")[0], 404)

    def test_ist_home_app_import_is_no_longer_a_wizard_action(self):
        status, _, body = self.request("/api/start", method="POST", value={"action": "ist-client"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "Ukjent oppsettshandling.")
        self.assertIsNone(self.service.job)

    def test_login_requires_consent_and_existing_session_confirmation(self):
        with patch("familiekilder.setup_service.auth_module") as module:
            module.return_value.auth_status.return_value = {"configured": True}
            module.return_value.session_path.return_value.exists.return_value = True
            self.assertEqual(self.request("/api/start", method="POST", value={"action": "login", "source": "spond"})[0], 400)
            self.assertEqual(self.request("/api/start", method="POST", value={"action": "login", "source": "spond", "consent": True})[0], 400)
        self.assertIsNone(self.service.job)

    def test_spond_prepares_its_public_web_configuration_without_a_client_file(self):
        from familiekilder.spond_auth import load_client_config

        with patch.object(self.service, "_login"):
            self.service.start("login", "spond", consent=True)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        config = load_client_config(self.root)
        self.assertEqual(config.app_version, "web")
        self.assertEqual(config.api_root, "https://api.spond.com/core/v1")
        self.assertEqual(Config.load(self.root).services, {"spond": frozenset()})

    def test_login_without_setup_imports_the_built_in_setup(self):
        with patch.object(self.service, "_login") as login:
            for source in AUTOMATIC_SOURCES:
                with self.subTest(source=source):
                    module = auth_module(source)
                    self.assertFalse(module.auth_status(self.root)["configured"])
                    status, _, _ = self.request(
                        "/api/start", method="POST", value={"action": "login", "source": source, "consent": True})
                    self.assertEqual(status, 200)
                    self.service.worker.join(timeout=3)
                    self.assertEqual(self.service.job.phase, "done")
                    self.assertTrue(module.auth_status(self.root)["configured"])
                    stored = module.load_client_config(self.root).as_private_json()
                    for key, value in default_client_config(source).items():
                        expected = sorted(value) if isinstance(value, list) else value
                        actual = sorted(stored[key]) if isinstance(stored[key], list) else stored[key]
                        self.assertEqual(actual, expected, key)
        self.assertEqual(login.call_count, len(AUTOMATIC_SOURCES))
        self.assertEqual(list((self.root / ".private/setup-imports").iterdir()), [])

    def test_existing_setup_is_never_overwritten_by_the_built_in_setup(self):
        from familiekilder.spond_auth import load_client_config

        stored = {**default_client_config("spond"), "app_version": "fictional-9.9"}
        self.root.mkdir()
        client_file = Path(self.temporary.name) / ".private/spond-client.json"
        atomic_write_private_bytes(client_file, json.dumps(stored).encode())
        auth_module("spond").configure_client(self.root, client_file)
        with patch.object(self.service, "_login"):
            self.service.start("login", "spond", consent=True)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(load_client_config(self.root).app_version, "fictional-9.9")

    def test_unreadable_stored_setup_is_not_replaced_by_the_built_in_setup(self):
        module = auth_module("vigilo")
        path = module.client_config_path(self.root)
        self.root.mkdir()
        ensure_private_directory(self.root / ".private")
        ensure_private_directory(path.parent)
        atomic_write_private_bytes(path, b"{not json")
        self.assertFalse(module.auth_status(self.root)["configured"])
        with patch.object(self.service, "_login") as login:
            with self.assertRaisesRegex(SetupError, "kan ikke leses"):
                self.service.start("login", "vigilo", consent=True)
        login.assert_not_called()
        self.assertEqual(path.read_bytes(), b"{not json")

    def test_successful_login_enables_reads_only(self):
        with (
            patch("familiekilder.setup_service.auth_module") as module,
            patch.object(self.service, "_login"),
        ):
            module.return_value.auth_status.return_value = {"configured": True}
            module.return_value.session_path.return_value.exists.return_value = False
            status, _, _ = self.request("/api/start", method="POST", value={"action": "login", "source": "spond", "consent": True})
            self.assertEqual(status, 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(Config.load(self.root).services, {"spond": frozenset()})

    def test_cancelling_waiting_login_does_not_enable_source(self):
        def wait(job, **_):
            job.phase = "waiting"
            job.cancel.wait(3)

        with patch("familiekilder.setup_service.auth_module") as module, patch.object(self.service, "_login", side_effect=wait):
            module.return_value.auth_status.return_value = {"configured": True}
            module.return_value.session_path.return_value.exists.return_value = False
            self.service.start("login", "spond", consent=True)
            self.service.signal(self.service.job.id, cancel=True)
            self.service.worker.join(timeout=4)
        self.assertEqual(self.service.job.phase, "cancelled")
        self.assertEqual(Config.load(self.root).services, {})


class BrowserLoginWaitTests(unittest.TestCase):
    """The wizard's wait for a Spond login in the separate browser window."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data"
        self.service = SetupService(self.root, home=Path(self.temporary.name))
        self.job = Job(id="fictional-job", source="spond")
        self.service.job = self.job
        self.waits = []
        check = patch("familiekilder.setup_service.LOGIN_CHECK_SECONDS", 0.0)
        check.start()
        self.addCleanup(check.stop)

    def bootstrap(self, ready):
        """A browser bootstrap that only waits; `ready` stands in for the login check."""

        def fake(root, source, *, replace_existing, wait_for_login):
            self.assertEqual((root, source), (self.root, "spond"))
            wait_for_login("fictional prompt", ready)
            self.waits.append((self.job.phase, self.job.message))
            return {"source": source}

        return patch("familiekilder.browser_bootstrap.bootstrap_source", side_effect=fake)

    def login_in_background(self):
        errors = []

        def run():
            try:
                self.service._login(self.job, replace=False)
            except Exception as error:  # noqa: BLE001 - the test inspects it
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        while self.job.phase != "waiting" and worker.is_alive():
            self.job.cancel.wait(0.01)
        return worker, errors

    def test_a_detected_login_continues_without_the_button(self):
        answers = iter([False, False, True])
        with self.bootstrap(lambda: next(answers)):
            self.service._login(self.job, replace=False)
        self.assertEqual(self.waits, [("working", "Henter innloggingen fra nettleservinduet …")])
        self.assertFalse(self.job.complete.is_set())

    def test_the_waiting_message_says_the_login_is_detected_and_the_button_is_a_fallback(self):
        seen = []

        def ready():
            seen.append((self.job.phase, self.job.message))
            return True

        with self.bootstrap(ready):
            self.service._login(self.job, replace=False)
        phase, message = seen[0]
        self.assertEqual(phase, "waiting")
        self.assertIn("Vi merker selv når du er logget inn", message)
        self.assertIn("«Jeg er logget inn»", message)

    def test_the_button_still_continues(self):
        with self.bootstrap(lambda: False):
            worker, errors = self.login_in_background()
            self.service.signal(self.job.id, cancel=False)
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.waits[0][0], "working")

    def test_a_click_just_after_detection_is_ignored(self):
        self.job.phase = "working"
        self.service.signal(self.job.id, cancel=False)
        self.assertFalse(self.job.complete.is_set())

    def test_cancel_stops_the_wait(self):
        with self.bootstrap(lambda: False):
            worker, errors = self.login_in_background()
            self.service.signal(self.job.id, cancel=True)
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual([str(error) for error in errors], ["Innloggingen ble avbrutt."])
        self.assertEqual(self.waits, [])

    def test_the_wait_ends_at_the_deadline(self):
        with (
            patch("familiekilder.setup_service.LOGIN_WAIT_SECONDS", 0.3),
            self.bootstrap(lambda: False),
        ):
            with self.assertRaisesRegex(SetupError, "avbrutt"):
                self.service._login(self.job, replace=False)

    def test_a_closed_window_gives_its_own_message(self):
        def closed():
            raise BrowserClosedError("Nettleservinduet ble lukket før innloggingen var ferdig")

        with self.bootstrap(closed):
            with self.assertRaises(SetupError) as raised:
                self.service._login(self.job, replace=False)
        self.assertIn("Nettleservinduet ble lukket", str(raised.exception))
        self.assertIn("«Logg inn»", str(raised.exception))


class FlytSetupTests(SetupServerCase):
    """Flyt asks for the municipality; the public list comes from a fake transport."""

    def setUp(self):
        super().setUp()
        self.held_lock = []
        self.transport = FakeMunicipalityTransport(
            on_call=lambda: self.held_lock.append(self.service.lock._is_owned()))
        transport_patch = patch.object(flyt_auth, "default_transport", return_value=self.transport)
        transport_patch.start()
        self.addCleanup(transport_patch.stop)

    def start_flyt(self, **fields):
        value = {"action": "login", "source": "flyt", "consent": True, **fields}
        return self.request("/api/start", method="POST", value=value)

    def test_login_without_municipality_is_rejected(self):
        for fields in ({}, {"municipality": ""}, {"municipality": "   "}):
            with self.subTest(fields=fields):
                status, _, body = self.start_flyt(**fields)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"], "Velg kommune før du logger inn.")
        self.assertIsNone(self.service.job)
        self.assertEqual(self.transport.calls, [])

    def test_invalid_municipality_values_are_rejected_before_any_lookup(self):
        for value in (912345678, ["912345678"], "9" * 201):
            with self.subTest(value=value):
                self.assertEqual(self.start_flyt(municipality=value)[0], 400)
        status, _, _ = self.request("/api/start", method="POST", value={
            "action": "login", "source": "spond", "consent": True, "municipality": "912345678"})
        self.assertEqual(status, 400)
        self.assertIsNone(self.service.job)
        self.assertEqual(self.transport.calls, [])

    def test_municipality_is_stored_in_the_worker_before_the_login(self):
        with patch.object(self.service, "_login") as login:
            login.side_effect = lambda job, **_: self.assertTrue(
                auth_module("flyt").auth_status(self.root)["configured"])
            status, _, _ = self.start_flyt(municipality="923456789")
            self.assertEqual(status, 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(login.call_count, 1)
        self.assertEqual(self.transport.threads, [self.service.worker])
        self.assertEqual(self.held_lock, [False])
        config = flyt_auth.load_client_config(self.root)
        self.assertEqual(config.municipality.organization_number, "923456789")
        self.assertEqual(Config.load(self.root).services, {"flyt": frozenset()})

    def test_unknown_municipality_gives_its_own_message(self):
        with patch.object(self.service, "_login") as login:
            self.assertEqual(self.start_flyt(municipality="999999999")[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "error")
        self.assertIn("Fant ingen kommune", self.service.job.message)
        self.assertNotIn("Steget kunne ikke fullføres", self.service.job.message)
        login.assert_not_called()
        self.assertFalse(auth_module("flyt").auth_status(self.root)["configured"])
        self.assertEqual(Config.load(self.root).services, {})

    def test_unreachable_municipality_list_gives_its_own_message(self):
        self.transport.failures.append(SourceUnavailable("fictional outage"))
        with patch.object(self.service, "_login") as login:
            self.assertEqual(self.start_flyt(municipality="923456789")[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "error")
        self.assertIn("Kommunelisten fra Flyt kunne ikke hentes", self.service.job.message)
        login.assert_not_called()

    def store_municipality(self, number):
        self.root.mkdir(exist_ok=True)
        flyt_auth.configure_municipality(self.root, number, transport=self.transport)
        self.transport.calls.clear()
        self.transport.threads.clear()
        self.held_lock.clear()
        return flyt_auth.client_config_path(self.root)

    def stored_number(self):
        return flyt_auth.load_client_config(self.root).municipality.organization_number

    def test_a_stored_municipality_is_kept_when_none_is_chosen(self):
        self.store_municipality("912345678")
        with patch.object(self.service, "_login"):
            self.assertEqual(self.start_flyt()[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.stored_number(), "912345678")

    def test_a_new_municipality_replaces_the_stored_one_without_a_session(self):
        self.store_municipality("912345678")
        with patch.object(self.service, "_login") as login:
            login.side_effect = lambda job, **_: self.assertEqual(self.stored_number(), "923456789")
            self.assertEqual(self.start_flyt(municipality="923456789")[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(login.call_count, 1)
        self.assertEqual(self.transport.threads, [self.service.worker])
        self.assertEqual(self.held_lock, [False])
        self.assertEqual(self.stored_number(), "923456789")

    def logged_in_with(self, number):
        path = self.store_municipality(number)
        store_fake_session(flyt_auth.session_path(self.root))
        return path, path.read_bytes()

    def record_steps(self):
        """Record lookup, logout and store in the order the worker takes them."""

        steps = []
        real_logout, real_configure = flyt_auth.logout, flyt_auth.configure_client

        def logout(root):
            steps.append("logout")
            return real_logout(root)

        def configure(*args, **kwargs):
            steps.append("store")
            return real_configure(*args, **kwargs)

        self.transport.on_call = lambda: steps.append("lookup")
        for name, replacement in (("logout", logout), ("configure_client", configure)):
            replaced = patch.object(flyt_auth, name, side_effect=replacement)
            replaced.start()
            self.addCleanup(replaced.stop)
        return steps

    def test_a_new_municipality_on_relogin_looks_up_then_logs_out_then_stores(self):
        self.logged_in_with("912345678")
        steps = self.record_steps()
        session = flyt_auth.session_path(self.root)

        def login(job, *, replace):
            steps.append("login")
            self.assertTrue(replace)
            self.assertFalse(session.exists())
            self.assertEqual(self.stored_number(), "923456789")

        with patch.object(self.service, "_login", side_effect=login):
            self.assertEqual(self.start_flyt(municipality="923456789", replace=True)[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(steps, ["lookup", "logout", "store", "login"])
        self.assertEqual(self.transport.threads, [self.service.worker])
        self.assertEqual(self.stored_number(), "923456789")
        self.assertEqual(Config.load(self.root).services, {"flyt": frozenset()})
        row, _ = self.flyt_row()
        self.assertEqual(row["municipality"], {"id": "923456789", "name": "Bergli kommune"})
        self.assertEqual(
            set(row), {"id", "label", "description", "state", "enabled", "has_session", "setup", "municipality",
            "renewal_pending"})

    def test_a_failed_lookup_on_relogin_keeps_login_and_municipality(self):
        for failure, municipality, message in (
            (None, "999999999", "Fant ingen kommune i Flyt som passer «999999999»."),
            (SourceUnavailable("fictional outage"), "923456789",
             "Kommunelisten fra Flyt kunne ikke hentes. Kontroller nettforbindelsen og prøv igjen."),
        ):
            with self.subTest(municipality=municipality):
                path, before = self.logged_in_with("912345678")
                session = flyt_auth.session_path(self.root).read_bytes()
                if failure is not None:
                    self.transport.failures.append(failure)
                with (
                    patch.object(flyt_auth, "logout") as logout,
                    patch.object(self.service, "_login") as login,
                ):
                    self.assertEqual(self.start_flyt(municipality=municipality, replace=True)[0], 200)
                    self.service.worker.join(timeout=3)
                self.assertEqual(self.service.job.phase, "error")
                self.assertEqual(self.service.job.message, message)
                self.assertEqual(len(self.transport.calls), 1)
                logout.assert_not_called()
                login.assert_not_called()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(flyt_auth.session_path(self.root).read_bytes(), session)

    def test_a_new_municipality_with_a_session_needs_the_replace_confirmation(self):
        path, before = self.logged_in_with("912345678")
        for fields in ({}, {"replace": False}, {"replace": "true"}):
            with self.subTest(fields=fields), patch.object(flyt_auth, "logout") as logout:
                status, _, body = self.start_flyt(municipality="923456789", **fields)
                self.assertEqual(status, 400)
                self.assertEqual(
                    json.loads(body)["error"],
                    "Flyt har en lagret innlogging. Vil du bytte kommune, krysser du av for "
                    "«Erstatt den eksisterende innloggingen». Da logges du ut av Flyt med en gang.",
                )
                logout.assert_not_called()
        self.assertIsNone(self.service.job)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(flyt_auth.session_path(self.root).exists())

    def test_the_same_municipality_on_relogin_keeps_the_login_until_the_new_one(self):
        path, before = self.logged_in_with("912345678")
        stat = path.stat().st_mtime_ns, path.stat().st_ino
        session = flyt_auth.session_path(self.root)
        with (
            patch.object(flyt_auth, "logout") as logout,
            patch.object(self.service, "_login") as login,
        ):
            login.side_effect = lambda job, **_: self.assertTrue(session.exists())
            self.assertEqual(self.start_flyt(municipality="912345678", replace=True)[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(login.call_count, 1)
        self.assertEqual(login.call_args.kwargs, {"replace": True})
        logout.assert_not_called()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((path.stat().st_mtime_ns, path.stat().st_ino), stat)
        self.assertTrue(session.exists())

    def test_cancelling_during_the_lookup_changes_nothing(self):
        path, before = self.logged_in_with("912345678")
        self.transport.on_call = lambda: self.service.job.cancel.set()
        with (
            patch.object(flyt_auth, "logout") as logout,
            patch.object(self.service, "_login") as login,
        ):
            self.assertEqual(self.start_flyt(municipality="923456789", replace=True)[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "cancelled")
        logout.assert_not_called()
        login.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(flyt_auth.session_path(self.root).exists())

    def test_cancelling_the_login_after_the_logout_keeps_the_change(self):
        self.logged_in_with("912345678")

        def wait(job, **_):
            job.phase = "waiting"
            job.cancel.wait(3)

        with patch.object(self.service, "_login", side_effect=wait):
            self.assertEqual(self.start_flyt(municipality="923456789", replace=True)[0], 200)
            while self.service.job.phase != "waiting" and self.service.worker.is_alive():
                self.service.job.cancel.wait(0.01)
            self.service.signal(self.service.job.id, cancel=True)
            self.service.worker.join(timeout=4)
        self.assertEqual(self.service.job.phase, "cancelled")
        self.assertFalse(flyt_auth.session_path(self.root).exists())
        self.assertEqual(self.stored_number(), "923456789")
        self.assertEqual(Config.load(self.root).services, {})

    def test_the_same_municipality_needs_no_lookup_and_no_rewrite(self):
        path = self.store_municipality("912345678")
        before = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino
        with patch.object(self.service, "_login") as login:
            self.assertEqual(self.start_flyt(municipality="912345678")[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "done")
        self.assertEqual(login.call_count, 1)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino), before)

    def test_an_unknown_new_municipality_keeps_the_stored_one(self):
        path = self.store_municipality("912345678")
        before = path.read_bytes()
        with patch.object(self.service, "_login") as login:
            self.assertEqual(self.start_flyt(municipality="999999999")[0], 200)
            self.service.worker.join(timeout=3)
        self.assertEqual(self.service.job.phase, "error")
        self.assertEqual(self.service.job.message, "Fant ingen kommune i Flyt som passer «999999999».")
        login.assert_not_called()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(Config.load(self.root).services, {})

    def flyt_row(self):
        status, _, body = self.request("/api/state")
        self.assertEqual(status, 200)
        rows = {row["id"]: row for row in json.loads(body)["services"]}
        for source, row in rows.items():
            if source != "flyt":
                self.assertNotIn("municipality", row)
        return rows["flyt"], body.decode()

    def test_state_shows_the_stored_municipality_and_nothing_else_from_the_setup(self):
        row, _ = self.flyt_row()
        self.assertIsNone(row["municipality"])
        path = self.store_municipality("912345678")
        row, text = self.flyt_row()
        self.assertEqual(row["municipality"], {"id": "912345678", "name": "Fjordvik kommune"})
        self.assertEqual(
            set(row), {"id", "label", "description", "state", "enabled", "has_session", "setup", "municipality",
            "renewal_pending"})
        for value in ("fjordvik", "Vismaconnect998877665", flyt_auth.CLIENT_ID, flyt_auth.REDIRECT_URI):
            self.assertNotIn(json.dumps(value), text)
        atomic_write_private_bytes(path, b"{not json")
        row, _ = self.flyt_row()
        self.assertIsNone(row["municipality"])

    def test_municipality_list_is_sorted_by_name_and_fetched_once(self):
        for _ in range(2):
            status, _, body = self.request("/api/flyt-municipalities")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"municipalities": [
                {"id": "923456789", "name": "Bergli kommune"},
                {"id": "912345678", "name": "Fjordvik kommune"},
                {"id": "934567890", "name": "Åsby kommune"},
            ]})
        self.assertEqual(len(self.transport.calls), 1)

    def test_failed_municipality_fetch_is_not_cached(self):
        self.transport.failures.append(SourceUnavailable("fictional outage"))
        status, _, body = self.request("/api/flyt-municipalities")
        self.assertEqual(status, 502)
        self.assertNotIn("fictional outage", json.loads(body)["error"])
        status, _, body = self.request("/api/flyt-municipalities")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["municipalities"]), 3)
        self.assertEqual(len(self.transport.calls), 2)

    def test_municipality_list_has_the_same_protection_as_the_state(self):
        for headers in (
            {"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"},
            {"X-Setup-Token": "wrong"}, {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/flyt-municipalities", headers=headers)[0], 403)
        self.assertEqual(self.transport.calls, [])


class StatusAndGuardTests(SetupServerCase):
    """Matrix for /status, duplicate security headers, cross-site root navigation, and data_root."""

    def raw_request(self, method: str, raw_path: str, headers: list[tuple[str, str]], body: bytes = b""):
        """Send an HTTP/1.1 request with exact raw path and arbitrary (including duplicate) headers."""

        lines = [f"{method} {raw_path} HTTP/1.1"]
        for name, value in headers:
            lines.append(f"{name}: {value}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        lines.append("Connection: close")
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5) as sock:
            sock.sendall(payload)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        raw = b"".join(chunks)
        head, _, response_body = raw.partition(b"\r\n\r\n")
        head_lines = head.decode("latin-1").split("\r\n")
        status = int(head_lines[0].split(" ", 2)[1])
        response_headers: dict[str, str] = {}
        for line in head_lines[1:]:
            key, _, value = line.partition(":")
            response_headers[key.strip().lower()] = value.strip()
        return status, response_headers, response_body

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def test_status_matrix_for_allowed_callers_cors_and_exact_body(self):
        expected_body = {"app": setup_page.STATUS_APP, "protocol": setup_page.STATUS_PROTOCOL}
        cases = [
            ("local_plain", [("Host", self.host)], None),
            ("local_sfs_none", [("Host", self.host), ("Sec-Fetch-Site", "none")], None),
            ("local_sfs_same_origin", [("Host", self.host), ("Sec-Fetch-Site", "same-origin")], None),
            ("local_instance", [("Host", self.host), ("X-Setup-Instance", self.server.instance_id)], None),
            ("own_origin", [("Host", self.host), ("Origin", self.server.origin)], None),
            (
                "pages_origin",
                [("Host", self.host), ("Origin", setup_page.PAGES_ORIGIN), ("Sec-Fetch-Site", "cross-site")],
                setup_page.PAGES_ORIGIN,
            ),
        ]
        for name, req_headers, expected_acao in cases:
            with self.subTest(case=name):
                status, headers, body = self.raw_request("GET", "/status", req_headers)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), expected_body)
                self.assertEqual(set(json.loads(body)), {"app", "protocol"})
                self.assertEqual(headers.get("vary"), "Origin")
                self.assertEqual(headers.get("access-control-allow-origin"), expected_acao)
                self.assertNotIn("access-control-allow-credentials", headers)
                self.assertEqual(headers.get("cross-origin-resource-policy"), "same-origin")
                self.assertEqual(headers.get("cache-control"), "no-store")
                text = body.decode()
                self.assertNotIn(str(self.root), text)
                self.assertNotIn(str(self.home), text)
                self.assertNotIn(self.server.token, text)
                self.assertNotIn(self.server.instance_id, text)

    def test_status_rejects_bad_headers_duplicates_and_wrong_instance(self):
        rejected = [
            ("missing_host", []),
            ("wrong_host", [("Host", "attacker.invalid")]),
            ("localhost_host", [("Host", f"localhost:{self.server.server_port}")]),
            ("dup_host", [("Host", self.host), ("Host", self.host)]),
            ("evil_origin", [("Host", self.host), ("Origin", "https://attacker.invalid")]),
            ("null_origin", [("Host", self.host), ("Origin", "null")]),
            ("http_pages_origin", [("Host", self.host), ("Origin", "http://familiekilder.github.io")]),
            (
                "dup_origin",
                [("Host", self.host), ("Origin", setup_page.PAGES_ORIGIN), ("Origin", setup_page.PAGES_ORIGIN)],
            ),
            (
                "dup_origin_mixed",
                [("Host", self.host), ("Origin", setup_page.PAGES_ORIGIN), ("Origin", "https://attacker.invalid")],
            ),
            ("no_origin_cross_site", [("Host", self.host), ("Sec-Fetch-Site", "cross-site")]),
            ("no_origin_same_site", [("Host", self.host), ("Sec-Fetch-Site", "same-site")]),
            ("no_origin_empty_sfs", [("Host", self.host), ("Sec-Fetch-Site", "")]),
            (
                "dup_sfs",
                [("Host", self.host), ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")],
            ),
            (
                "instance_with_pages_origin",
                [
                    ("Host", self.host),
                    ("Origin", setup_page.PAGES_ORIGIN),
                    ("X-Setup-Instance", self.server.instance_id),
                ],
            ),
            (
                "instance_with_own_origin",
                [
                    ("Host", self.host),
                    ("Origin", self.server.origin),
                    ("X-Setup-Instance", self.server.instance_id),
                ],
            ),
            (
                "dup_instance",
                [
                    ("Host", self.host),
                    ("X-Setup-Instance", self.server.instance_id),
                    ("X-Setup-Instance", self.server.instance_id),
                ],
            ),
        ]
        for name, req_headers in rejected:
            with self.subTest(case=name):
                status, headers, _ = self.raw_request("GET", "/status", req_headers)
                self.assertEqual(status, 403)
                self.assertEqual(headers.get("vary"), "Origin")
                self.assertNotIn("access-control-allow-origin", headers)
                self.assertNotIn("access-control-allow-credentials", headers)

        status, headers, _ = self.raw_request(
            "GET", "/status", [("Host", self.host), ("X-Setup-Instance", "wrong-instance-id")]
        )
        self.assertEqual(status, 409)
        self.assertEqual(headers.get("vary"), "Origin")
        self.assertNotIn("access-control-allow-origin", headers)

    def test_non_ascii_tokens_are_rejected_without_a_crash(self):
        value = "\xe6\xf8\xe5"
        self.assertEqual(self.request("/status", headers={"X-Setup-Instance": value})[0], 409)
        self.assertEqual(self.request("/api/state", headers={"X-Setup-Token": value})[0], 403)

    def test_status_returns_503_while_closing(self):
        self.server.mark_closing()
        status, headers, _ = self.raw_request(
            "GET", "/status", [("Host", self.host), ("X-Setup-Instance", self.server.instance_id)]
        )
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("vary"), "Origin")
        self.assertNotIn("access-control-allow-origin", headers)

        status, headers, _ = self.raw_request(
            "GET", "/status", [("Host", self.host), ("Origin", setup_page.PAGES_ORIGIN)]
        )
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("access-control-allow-origin"), setup_page.PAGES_ORIGIN)
        self.assertEqual(headers.get("vary"), "Origin")

    def test_status_requires_exact_raw_path_and_get_method(self):
        for raw_path in ("/status?x=1", "/status/", "//status", "/%73tatus"):
            with self.subTest(path=raw_path):
                status, headers, _ = self.raw_request(
                    "GET", raw_path, [("Host", self.host), ("Origin", setup_page.PAGES_ORIGIN)]
                )
                self.assertEqual(status, 403)
                self.assertNotIn("access-control-allow-origin", headers)
                status, _, _ = self.raw_request("GET", raw_path, [("Host", self.host)])
                self.assertEqual(status, 404)

        self.assertEqual(self.raw_request("POST", "/status", [("Host", self.host)], b"{}")[0], 403)
        self.assertEqual(
            self.raw_request(
                "POST",
                "/status",
                [
                    ("Host", self.host),
                    ("Origin", self.server.origin),
                    ("X-Setup-Token", self.server.token),
                    ("Content-Type", "application/json"),
                ],
                b"{}",
            )[0],
            404,
        )
        self.assertEqual(
            self.raw_request(
                "OPTIONS", "/status", [("Host", self.host), ("Origin", setup_page.PAGES_ORIGIN)]
            )[0],
            501,
        )

    def test_cross_site_navigation_exception_only_allows_exact_root_get_and_sets_coop(self):
        nav_headers = [
            ("Host", self.host),
            ("Sec-Fetch-Site", "cross-site"),
            ("Sec-Fetch-Mode", "navigate"),
            ("Sec-Fetch-Dest", "document"),
        ]
        status, headers, body = self.raw_request("GET", "/", nav_headers)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cross-origin-opener-policy"), "same-origin")
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertIn("frame-ancestors 'none'", headers.get("content-security-policy", ""))
        self.assertIn(self.server.token.encode(), body)

        # Same-origin GET / also sets Cross-Origin-Opener-Policy.
        status, headers, _ = self.raw_request("GET", "/", [("Host", self.host)])
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cross-origin-opener-policy"), "same-origin")

        rejected = [
            ("root_with_query", "GET", "/?x=1", nav_headers),
            ("double_slash", "GET", "//", nav_headers),
            ("setup_js", "GET", "/setup.js", nav_headers),
            ("setup_css", "GET", "/setup.css", nav_headers),
            (
                "api_state_with_token",
                "GET",
                "/api/state",
                [*nav_headers, ("X-Setup-Token", self.server.token)],
            ),
            ("post_root", "POST", "/", nav_headers),
            ("with_origin", "GET", "/", [*nav_headers, ("Origin", setup_page.PAGES_ORIGIN)]),
            (
                "wrong_mode",
                "GET",
                "/",
                [
                    ("Host", self.host),
                    ("Sec-Fetch-Site", "cross-site"),
                    ("Sec-Fetch-Mode", "no-cors"),
                    ("Sec-Fetch-Dest", "document"),
                ],
            ),
            (
                "wrong_dest",
                "GET",
                "/",
                [
                    ("Host", self.host),
                    ("Sec-Fetch-Site", "cross-site"),
                    ("Sec-Fetch-Mode", "navigate"),
                    ("Sec-Fetch-Dest", "iframe"),
                ],
            ),
            (
                "missing_dest",
                "GET",
                "/",
                [("Host", self.host), ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate")],
            ),
            (
                "wrong_host",
                "GET",
                "/",
                [
                    ("Host", "attacker.invalid"),
                    ("Sec-Fetch-Site", "cross-site"),
                    ("Sec-Fetch-Mode", "navigate"),
                    ("Sec-Fetch-Dest", "document"),
                ],
            ),
            ("dup_sfs", "GET", "/", [*nav_headers, ("Sec-Fetch-Site", "cross-site")]),
            ("dup_sfm", "GET", "/", [*nav_headers, ("Sec-Fetch-Mode", "navigate")]),
            ("dup_sfd", "GET", "/", [*nav_headers, ("Sec-Fetch-Dest", "document")]),
            ("dup_host", "GET", "/", [*nav_headers, ("Host", self.host)]),
        ]
        for name, method, raw_path, req_headers in rejected:
            with self.subTest(case=name):
                status, _, _ = self.raw_request(method, raw_path, req_headers)
                self.assertEqual(status, 403)

    def test_api_rejects_duplicate_security_headers(self):
        cases = [
            (
                "dup_token",
                "GET",
                "/api/state",
                [
                    ("Host", self.host),
                    ("X-Setup-Token", self.server.token),
                    ("X-Setup-Token", self.server.token),
                ],
                b"",
            ),
            (
                "dup_host",
                "GET",
                "/api/state",
                [("Host", self.host), ("Host", self.host), ("X-Setup-Token", self.server.token)],
                b"",
            ),
            (
                "dup_origin_post",
                "POST",
                "/api/connect",
                [
                    ("Host", self.host),
                    ("Origin", self.server.origin),
                    ("Origin", self.server.origin),
                    ("X-Setup-Token", self.server.token),
                    ("Content-Type", "application/json"),
                ],
                b'{"client":"codex"}',
            ),
        ]
        for name, method, path, headers, body in cases:
            with self.subTest(case=name):
                self.assertEqual(self.raw_request(method, path, headers, body)[0], 403)
        self.assertFalse((self.home / ".codex").exists())

    def test_state_reports_data_root_with_tilde_for_home(self):
        nested = self.home / "Library" / "Application Support" / "familiekilder-mcp"
        service = SetupService(nested, home=self.home)
        self.assertEqual(
            service.state()["data_root"],
            str(Path("~") / "Library" / "Application Support" / "familiekilder-mcp"),
        )
        self.assertFalse(nested.exists())
        self.assertEqual(SetupService(self.home, home=self.home).state()["data_root"], "~")
        self.assertEqual(self.service.state()["data_root"], str(self.root))

    @unittest.skipUnless(shutil.which("node"), "Node is required to test setup.js behavior")
    def test_setup_js_distinguishes_403_from_network_error_and_renders_footer(self):
        script_path = Path(setup_page.__file__).parent / "data/setup/setup.js"
        harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');

function makeNode(tag, id) {
  const listeners = new Map();
  const node = {
    tagName: tag.toUpperCase(),
    id: id || '',
    className: '',
    hidden: false,
    disabled: false,
    checked: false,
    value: '',
    href: '',
    content: '',
    open: false,
    dataset: {},
    children: [],
    options: [],
    _text: '',
    classList: { toggle() {} },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    showModal() { this.open = true; },
    close() { this.open = false; },
    addEventListener(type, fn) { listeners.set(type, fn); },
    click() { const fn = listeners.get('click'); if (fn) return fn({ target: this, preventDefault() {} }); },
    replaceChildren(...items) {
      this.children = items.map((item) => (typeof item === 'string' ? { textContent: item } : item));
      this._text = '';
    },
    append(...items) {
      for (const item of items) {
        this.children.push(typeof item === 'string' ? { textContent: item } : item);
      }
    },
    get textContent() {
      return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text;
    },
    set textContent(val) {
      this.children = [];
      this._text = String(val);
    },
  };
  return node;
}

function runScenario(port, responses) {
  const nodes = new Map();
  const byId = (id) => {
    if (!nodes.has(id)) nodes.set(id, makeNode('div', id));
    return nodes.get(id);
  };
  let reloaded = false;
  let callIndex = 0;
  const document = {
    getElementById: byId,
    querySelector(sel) {
      if (sel === 'meta[name="setup-token"]') return { content: 'tok' };
      return makeNode('button');
    },
    querySelectorAll() { return []; },
    createElement(tag) { return makeNode(tag); },
  };
  const context = vm.createContext({
    document,
    window: {
      location: { port: String(port), reload() { reloaded = true; } },
      confirm() { return true; },
    },
    fetch(path) {
      if (path === '/api/doctor') {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ text: '', report: {} }) });
      }
      if (path === '/api/close') {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) });
      }
      const next = responses[Math.min(callIndex++, responses.length - 1)];
      if (next.networkError) return Promise.reject(new TypeError('Failed to fetch'));
      return Promise.resolve({
        ok: next.status >= 200 && next.status < 300,
        status: next.status,
        json: () => Promise.resolve(next.body),
      });
    },
    setInterval() { return 1; },
    console,
  });
  const source = fs.readFileSync(process.argv[1], 'utf-8');
  vm.runInContext(source, context);
  return { context, nodes, getReloaded: () => reloaded };
}

function findByTag(node, tag) {
  const out = [];
  if (node.tagName === tag) out.push(node);
  for (const child of node.children || []) out.push(...findByTag(child, tag));
  return out;
}

(async () => {
  const okState = {
    version: '1.0.0',
    data_root: '~/familiekilder-test',
    services: [],
    job: null,
    tested: false,
    clients: [],
    browsers: { installed: [], chosen: 'auto', used: null },
    dependencies: { browser: false, pdf: false, word: false },
  };

  // Fixed port: ok -> 403 -> network error -> closeSetup.
  const fixed = runScenario(28417, [
    { status: 200, body: okState },
    { status: 403, body: { error: 'Oppsettsvinduet er utløpt. Last siden på nytt.' } },
    { networkError: true },
  ]);
  await new Promise((r) => setImmediate(r));
  const footerText = fixed.nodes.get('data-root').textContent;
  await fixed.context.refresh();
  const expiredBox = fixed.nodes.get('error');
  const expiredButtons = findByTag(expiredBox, 'BUTTON');
  expiredButtons[0].click();
  const expiredResult = {
    text: expiredBox.textContent,
    buttonLabel: expiredButtons[0].textContent,
    reloaded: fixed.getReloaded(),
    links: findByTag(expiredBox, 'A').map((a) => a.href),
  };
  await fixed.context.refresh();
  const offlineFixed = {
    text: fixed.nodes.get('error').textContent,
    links: findByTag(fixed.nodes.get('error'), 'A').map((a) => a.href),
  };
  await fixed.context.closeSetup();
  const closedFixed = {
    text: fixed.nodes.get('main').textContent,
    links: findByTag(fixed.nodes.get('main'), 'A').map((a) => a.href),
  };

  // Non-fixed port: network error and closeSetup must NOT link to Pages.
  const custom = runScenario(55555, [{ networkError: true }]);
  await new Promise((r) => setImmediate(r));
  const offlineCustom = {
    text: custom.nodes.get('error').textContent,
    links: findByTag(custom.nodes.get('error'), 'A').map((a) => a.href),
  };
  await custom.context.closeSetup();
  const closedCustom = {
    text: custom.nodes.get('main').textContent,
    links: findByTag(custom.nodes.get('main'), 'A').map((a) => a.href),
  };

  process.stdout.write(JSON.stringify({
    footerText, expiredResult, offlineFixed, closedFixed, offlineCustom, closedCustom,
  }));
})();
"""
        completed = subprocess.run(
            ["node", "-e", harness, str(script_path)],
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=15,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["footerText"], "Datamappe: ~/familiekilder-test")
        self.assertEqual(result["expiredResult"]["buttonLabel"], "Last siden på nytt")
        self.assertTrue(result["expiredResult"]["reloaded"])
        self.assertEqual(result["expiredResult"]["links"], [])
        self.assertEqual(result["offlineFixed"]["links"], [setup_page.PAGES_URL])
        self.assertEqual(result["closedFixed"]["links"], [setup_page.PAGES_URL])
        self.assertEqual(result["offlineCustom"]["links"], [])
        self.assertIn("familiekilder-mcp setup", result["offlineCustom"]["text"])
        self.assertEqual(result["closedCustom"]["links"], [])
        self.assertIn("familiekilder-mcp setup", result["closedCustom"]["text"])


if __name__ == "__main__":
    unittest.main()
