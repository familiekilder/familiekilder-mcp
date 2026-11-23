"""`sources.register` adds a service defined outside the package.

The fictional service in `familiekilder_testing.added_source` is registered
after the package's modules are imported, as an application that embeds the
package would do it. Every module that keeps a list of services must then
include it, and its fields must be honoured.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder import auth_lifecycle, core, live_traffic, source_cli
from familiekilder.core import FamiliekilderError, normalize_source
from familiekilder.live_config import SOURCES, Config
from familiekilder.private_state import atomic_write_private_json, credential_path
from familiekilder.read_cache import account_scope
from familiekilder.source_adapter import adapter_sources, auth_module_name, get_adapter
from familiekilder.source_server import Runtime, build_server
from familiekilder.sources import REGISTRY, Source, SourceNames, register, session_file
from familiekilder_testing import added_source
from familiekilder_testing.added_source import ITEM, NAME, SOURCE


class RegisterTests(unittest.TestCase):
    def setUp(self):
        register(SOURCE)
        self.addCleanup(REGISTRY.pop, NAME)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_refuses_duplicates_other_objects_and_short_module_names(self):
        with self.assertRaisesRegex(ValueError, "already registered"):
            register(SOURCE)
        with self.assertRaises(TypeError):
            register({"name": "other"})
        with self.assertRaisesRegex(ValueError, "full module path"):
            register(Source("other", "Annen", "other_auth", added_source.MODULE))
        self.assertNotIn("other", REGISTRY)

    def test_lists_kept_by_modules_include_the_service(self):
        self.assertEqual(SOURCES[-1], NAME)
        self.assertIs(SOURCES, core.SOURCES)
        self.assertIn(NAME, adapter_sources())
        self.assertEqual(normalize_source(" Notes "), NAME)
        self.assertNotIn(NAME, core.PROVIDER_SOURCES)
        self.assertNotIn(NAME, live_traffic.SOURCES)
        self.assertNotIn(NAME, auth_lifecycle.SOURCES)
        self.assertEqual(auth_module_name(NAME), added_source.MODULE)
        self.assertEqual(get_adapter(NAME).auth_status(self.root)["configured"], True)

    def test_source_names_follow_the_registry(self):
        names = SourceNames(lambda source: source.login == "fictional", first=("first",))
        self.assertEqual(tuple(names), ("first", NAME))
        self.assertEqual(names, ("first", NAME))
        self.assertEqual(len(names), 2)
        self.assertEqual(names[-1], NAME)
        REGISTRY.pop(NAME)
        try:
            self.assertEqual(names, ("first",))
        finally:
            register(SOURCE)

    def test_configuration_accepts_the_service_but_no_write_permissions(self):
        Config(self.root, {NAME: frozenset()}).save()
        self.assertEqual(Config.load(self.root).services, {NAME: frozenset()})
        with self.assertRaisesRegex(FamiliekilderError, "Notater støtter bare lesetilgang"):
            Config(self.root, {NAME: frozenset({"messages"})}).save()
        path = self.root / ".private/mcp-config.json"
        path.write_text(json.dumps({"version": 1, "services": {NAME: {"write": ["messages"]}}}))
        with self.assertRaisesRegex(FamiliekilderError, "Notater støtter bare lesetilgang"):
            Config.load(self.root)

    def test_login_file_binds_the_read_cache_and_ownership(self):
        self.assertEqual(session_file(NAME), "notes.json")
        self.assertEqual(session_file("spond"), "spond-session.json")
        self.assertIsNone(account_scope(self.root, NAME))
        atomic_write_private_json(
            credential_path(self.root, "notes.json"), {"refresh_token": "fake"}
        )
        first = account_scope(self.root, NAME)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        atomic_write_private_json(
            credential_path(self.root, "notes.json"), {"refresh_token": "other"}
        )
        self.assertNotEqual(account_scope(self.root, NAME), first)
        # Ownership and moved markers apply; the auth journal does not.
        auth_lifecycle.bind_owner(self.root, NAME)
        self.assertTrue(credential_path(self.root, "notes-auth-owner.json").exists())
        auth_lifecycle.record(self.root, NAME, "refresh_started")
        self.assertFalse(credential_path(self.root, "notes-auth-history.json").exists())
        # Spond keeps a journal.
        auth_lifecycle.record(self.root, "spond", "refresh_started")
        self.assertTrue(credential_path(self.root, "spond-auth-history.json").exists())

    def test_reads_wait_for_the_source_lock_as_long_as_the_service_asks(self):
        from familiekilder import live_readers

        calls = []

        @contextlib.contextmanager
        def lock(root, source, *, wait=False, timeout=None):
            calls.append((source, wait, timeout))
            yield

        with patch.object(live_readers, "private_source_lock", lock):
            item = live_readers.get_live(self.root, NAME, {"kind": "note", "id": "1"})
        self.assertEqual(item["source_id"], "note:1")
        self.assertEqual(calls, [(NAME, True, 2.5)])
        with self.assertRaisesRegex(
            FamiliekilderError, "Notater støtter målrettet notes_search, ikke brede listeoppslag"
        ):
            live_readers.list_live(self.root, NAME, "2026-09-01T00:00:00+00:00")

    def test_server_offers_reads_without_a_listing_and_asks_the_read_hook(self):
        config = Config(self.root, {NAME: frozenset()})
        asked = []

        class Checked(Runtime):
            def require_source_allowed(self, source):
                asked.append(source)

        runtime = Checked(config, getter=lambda *_: dict(ITEM))
        server, runtime = build_server(config, runtime=runtime)
        try:
            names = {tool.name for tool in asyncio.run(server.list_tools())}
            detail = asyncio.run(
                server.call_tool("notes_get_item", {"reference": ITEM["reference"]})
            )
            with self.assertRaisesRegex(FamiliekilderError, "målrettet notes_search"):
                runtime.list_items(NAME)
        finally:
            runtime.close()
        self.assertIn("notes_get_item", names)
        self.assertIn("notes_read_attachment", names)
        self.assertNotIn("notes_list_items", names)
        self.assertEqual(json.loads(detail.content[0].text)["body"], ITEM["body"])
        self.assertEqual(asked, [NAME])

    def test_auth_commands_go_to_the_service_module_when_it_has_none_listed(self):
        Config(self.root, {NAME: frozenset()}).save()
        added_source.AUTH_COMMANDS.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            code = source_cli.main(["--root", str(self.root), "auth", NAME, "status"])
        self.assertEqual(code, 0)
        self.assertEqual([command for command, _ in added_source.AUTH_COMMANDS], ["status"])


if __name__ == "__main__":
    unittest.main()
