"""Tests for the temporary LogT config permissions (DEBT-008).

The password-bearing ``config.json`` must be readable only by the current user.
On Windows that means an explicit, verified DACL; if the restriction cannot be
applied the file must be deleted and the import refused, never left on disk
with unknown permissions.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import logt_adapter


class WritePrivateConfigTestBase(unittest.TestCase):
    """Create a disposable staging directory for every test."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="logt-dacl-test-"))
        self.addCleanup(shutil.rmtree, self.directory, True)

    @property
    def config_path(self) -> Path:
        """Path of the file the function under test writes."""
        return self.directory / "config.json"


class TestWritePrivateConfigFailsClosed(WritePrivateConfigTestBase):
    """A failure to protect the file must abort the import."""

    def test_permission_failure_removes_the_secret_file(self):
        with mock.patch.object(
            logt_adapter, "_restrict_to_current_user", side_effect=OSError("denied")
        ):
            with self.assertRaises(logt_adapter.AdapterError) as caught:
                logt_adapter._write_private_config(
                    self.directory, {"password": "secret"}
                )
        self.assertIn("refusing", str(caught.exception).lower())
        self.assertFalse(self.config_path.exists())

    def test_successful_write_returns_the_protected_path(self):
        with mock.patch.object(
            logt_adapter, "_restrict_to_current_user"
        ) as restrict:
            path = logt_adapter._write_private_config(
                self.directory, {"password": "secret"}
            )
        restrict.assert_called_once_with(path)
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, self.directory)


@unittest.skipUnless(os.name == "nt", "Windows DACL verification")
class TestWindowsDacl(WritePrivateConfigTestBase):
    """The Windows path sets and verifies a single-user DACL."""

    def test_current_user_sid_is_a_sid(self):
        sid = logt_adapter._current_user_sid()
        self.assertRegex(sid, re.compile(r"^S-1-\d+(-\d+)+$"))

    def test_written_config_has_a_verified_single_user_dacl(self):
        sid = logt_adapter._current_user_sid()
        path = logt_adapter._write_private_config(
            self.directory, {"password": "secret"}
        )
        self.assertTrue(logt_adapter._windows_dacl_matches(path, sid))

    def test_extra_ace_is_detected(self):
        """Verification must fail when another account also has access."""
        import subprocess

        sid = logt_adapter._current_user_sid()
        path = logt_adapter._write_private_config(
            self.directory, {"password": "secret"}
        )
        # BUILTIN\Users has the well-known SID S-1-5-32-545.
        completed = subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-5-32-545:R"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if completed.returncode != 0:  # pragma: no cover - policy-dependent
            self.skipTest("cannot add a test ACE on this host")
        self.assertFalse(logt_adapter._windows_dacl_matches(path, sid))

    def test_dacl_application_failure_is_fatal(self):
        self.config_path.write_text("{}", encoding="utf-8")
        with mock.patch.object(
            logt_adapter,
            "_set_windows_private_dacl",
            side_effect=OSError("denied"),
        ):
            with self.assertRaises(OSError) as caught:
                logt_adapter._restrict_to_current_user(self.config_path)
        self.assertIn("denied", str(caught.exception))

    def test_failed_verification_is_fatal(self):
        self.config_path.write_text("{}", encoding="utf-8")
        with mock.patch.object(logt_adapter, "_set_windows_private_dacl"):
            with mock.patch.object(
                logt_adapter, "_windows_dacl_matches", return_value=False
            ):
                with self.assertRaises(OSError) as caught:
                    logt_adapter._restrict_to_current_user(self.config_path)
        self.assertIn("verification", str(caught.exception).lower())


@unittest.skipUnless(os.name != "nt", "POSIX permission bits")
class TestPosixPermissions(WritePrivateConfigTestBase):
    """The POSIX path sets and verifies ``0o600``."""

    def test_permissions_are_set_to_owner_only(self):
        self.config_path.write_text("{}", encoding="utf-8")
        logt_adapter._restrict_to_current_user(self.config_path)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_group_access_after_chmod_is_fatal(self):
        self.config_path.write_text("{}", encoding="utf-8")
        fake_stat = types.SimpleNamespace(st_mode=0o100644)
        with mock.patch.object(logt_adapter.os, "stat", return_value=fake_stat):
            with self.assertRaises(OSError):
                logt_adapter._restrict_to_current_user(self.config_path)


if __name__ == "__main__":
    unittest.main()
