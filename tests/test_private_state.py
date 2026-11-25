from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder_testing.private import readable_by_others

from familiekilder.private_state import (
    PrivateStateError,
    atomic_write_private_bytes,
    atomic_write_private_json,
    credential_path,
    credentials_directory,
    read_private_bytes,
    read_private_json,
    remove_private_file,
)


class PrivateStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_credentials_are_private_bounded_and_validated(self) -> None:
        directory = credentials_directory(self.root)
        target = credential_path(self.root, "vigilo-session.json")
        atomic_write_private_json(target, {"version": 1, "token": "private"})
        if os.name == "nt":
            # Windows ignores the mode; .private's access list keeps it to the user.
            from familiekilder.windows_acl import is_private

            self.assertTrue(is_private(self.root / ".private"))
        else:
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

        def validate(value):
            if value.get("version") != 1:
                raise PrivateStateError("invalid")
            return value["token"]

        self.assertEqual(read_private_json(target, validator=validate), "private")
        with self.assertRaisesRegex(PrivateStateError, "større"):
            read_private_bytes(target, max_bytes=2)
        with self.assertRaises(PrivateStateError):
            credential_path(self.root, "../outside.json")

    def test_read_rejects_symlink_and_broad_permissions(self) -> None:
        external = self.root / "external.json"
        external.write_text('{"token":"must-survive"}', encoding="utf-8")
        external.chmod(0o600)
        credentials_directory(self.root)
        link = credential_path(self.root, "linked.json")
        try:
            link.symlink_to(external)
        except (NotImplementedError, OSError):
            self.skipTest("symbolske lenker støttes ikke")
        with self.assertRaises(PrivateStateError):
            read_private_json(link)
        self.assertIn("must-survive", external.read_text(encoding="utf-8"))

        broad = credential_path(self.root, "broad.json")
        broad.write_text("{}", encoding="utf-8")
        readable_by_others(broad)
        with self.assertRaisesRegex(PrivateStateError, "brede"):
            read_private_json(broad)

    def test_atomic_failure_removes_secret_temporary_and_preserves_target(self) -> None:
        target = credential_path(self.root, "session.json")
        atomic_write_private_bytes(target, b"old-private")
        with patch(
            "familiekilder.private_state.os.replace",
            side_effect=OSError("synthetic"),
        ):
            with self.assertRaises(PrivateStateError):
                atomic_write_private_bytes(target, b"new-private")
        self.assertEqual(target.read_bytes(), b"old-private")
        self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_no_overwrite_and_safe_remove_do_not_follow_symlinks(self) -> None:
        target = credential_path(self.root, "session.json")
        atomic_write_private_json(target, {"version": 1})
        with self.assertRaisesRegex(PrivateStateError, "finnes allerede"):
            atomic_write_private_json(target, {"version": 2}, overwrite=False)
        self.assertEqual(read_private_json(target)["version"], 1)
        self.assertTrue(remove_private_file(target))
        self.assertFalse(remove_private_file(target))

        external = self.root / "external-secret"
        external.write_text("survives", encoding="utf-8")
        link = credential_path(self.root, "orphan-link")
        try:
            link.symlink_to(external)
        except (NotImplementedError, OSError):
            self.skipTest("symbolske lenker støttes ikke")
        self.assertTrue(remove_private_file(link))
        self.assertEqual(external.read_text(encoding="utf-8"), "survives")



def in_use(winerror):
    error = PermissionError(13, "in use")
    error.winerror = winerror
    return error


class ReplaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.target = credentials_directory(Path(temporary.name)) / "state.json"
        atomic_write_private_json(self.target, {"version": 1})

    def write(self, errors, *, windows=True):
        """Write once; os.replace raises each of `errors` first. Returns the calls made."""

        real, calls, left = os.replace, [], list(errors)

        def replace(source, target):
            calls.append(target)
            if left:
                raise left.pop(0)
            real(source, target)

        with (
            patch("familiekilder.private_state._is_windows", return_value=windows),
            patch("familiekilder.private_state.os.replace", replace),
            patch("familiekilder.private_state.WINDOWS_REPLACE_SECONDS", 0.2),
        ):
            atomic_write_private_json(self.target, {"version": 2})
        return len(calls)

    def test_windows_waits_for_a_reader_to_close_the_file(self) -> None:
        self.assertEqual(self.write([in_use(5), in_use(32)]), 3)
        self.assertEqual(read_private_json(self.target), {"version": 2})

    def test_windows_gives_up_and_keeps_the_old_file(self) -> None:
        with self.assertRaises(PrivateStateError):
            self.write([in_use(32)] * 1000)
        self.assertEqual(read_private_json(self.target), {"version": 1})
        self.assertEqual([path.name for path in self.target.parent.iterdir()], ["state.json"])

    def test_other_errors_and_other_systems_are_not_retried(self) -> None:
        for errors, windows in (([in_use(112)], True), ([in_use(5)], False), ([OSError(28, "full")], True)):
            with self.subTest(errors=errors, windows=windows), self.assertRaises(PrivateStateError):
                self.write(errors, windows=windows)

    @unittest.skipUnless(os.name == "nt", "Only Windows refuses to replace an open file")
    def test_a_real_reader_does_not_stop_the_write(self) -> None:
        reader = open(self.target, "rb")
        timer = threading.Timer(0.3, reader.close)
        timer.start()
        self.addCleanup(timer.join)
        atomic_write_private_json(self.target, {"version": 2})
        self.assertEqual(read_private_json(self.target), {"version": 2})


if __name__ == "__main__":
    unittest.main()
