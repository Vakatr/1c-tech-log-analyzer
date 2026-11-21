"""Credential-handling and import-orchestration tests.

Covers review MAJOR 2 (no password in URLs, command lines, logs or child
environments) and MAJOR 1 (LogT stdout is diagnostics only, row counts come from
ClickHouse).
"""

import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

import logt_adapter

SECRET = "sup3r-s3cret-pw"

#: Password supplied by the GUI override.  It is deliberately absent from the
#: environment and the local config, so only the resolved config can mask it.
OVERRIDE_SECRET = "override-SECRET-42"

#: Password supplied through ``CLICKHOUSE_PASSWORD``.
ENV_SECRET = "env-SECRET-77"

#: ``vendor/logt`` is not bundled in the public repository.  Tests that need the
#: real checkout are skipped there; set ``LOGT_ROOT`` and provide it to run them.
LOGT_VENDORED = (
    Path(logt_adapter.REPO_ROOT) / "vendor" / "logt" / "src" / "main.py"
).is_file()


class TestRedaction(unittest.TestCase):
    """The password must never survive into user-visible text."""

    def test_password_is_redacted(self):
        self.assertNotIn(SECRET, logt_adapter.redact(f"boom {SECRET} boom", SECRET))

    def test_redaction_uses_environment_when_secret_omitted(self):
        with mock.patch.dict(os.environ, {"CLICKHOUSE_PASSWORD": SECRET}, clear=False):
            self.assertNotIn(SECRET, logt_adapter.redact(f"password={SECRET}"))

    def test_short_password_only_redacted_in_credential_context(self):
        """A 1-character password must not corrupt ordinary diagnostics."""
        text = "connect to 127.0.0.1:9000 failed, code 210"
        self.assertEqual(logt_adapter.redact(text, "1"), text)
        masked = logt_adapter.redact('password="1" port=9000', "1")
        self.assertNotIn('"1"', masked)
        self.assertIn("9000", masked)

    def test_non_string_input_is_coerced(self):
        self.assertIsInstance(logt_adapter.redact(RuntimeError("boom"), SECRET), str)


class TestConfigResolution(unittest.TestCase):
    """Precedence and secret sourcing for connection settings."""

    def setUp(self):
        patcher = mock.patch.object(logt_adapter, "load_local_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        for name in (
            logt_adapter.ENV_HOST,
            logt_adapter.ENV_PORT,
            logt_adapter.ENV_DATABASE,
            logt_adapter.ENV_USER,
            logt_adapter.ENV_PASSWORD,
        ):
            os.environ.pop(name, None)

    def test_defaults(self):
        config = logt_adapter.resolve_config()
        self.assertEqual(config["host"], "127.0.0.1")
        self.assertEqual(config["port"], 9000)
        self.assertEqual(config["database"], "tracelog")

    def test_environment_supplies_password(self):
        os.environ["CLICKHOUSE_PASSWORD"] = SECRET
        self.assertEqual(logt_adapter.resolve_config()["password"], SECRET)

    def test_overrides_win_over_environment(self):
        os.environ["CLICKHOUSE_PASSWORD"] = "env-secret"
        config = logt_adapter.resolve_config({"password": SECRET})
        self.assertEqual(config["password"], SECRET)

    def test_blank_override_does_not_mask_environment(self):
        """An empty GUI password field must fall back to the environment."""
        os.environ["CLICKHOUSE_PASSWORD"] = SECRET
        config = logt_adapter.resolve_config({"password": ""})
        self.assertEqual(config["password"], SECRET)

    def test_local_config_is_used_when_present(self):
        with mock.patch.object(
            logt_adapter, "load_local_config", return_value={"password": SECRET, "port": 9001}
        ):
            config = logt_adapter.resolve_config()
        self.assertEqual(config["password"], SECRET)
        self.assertEqual(config["port"], 9001)


class TestLocalConfigPermissions(unittest.TestCase):
    """The git-ignored local config is documented and stays out of git."""

    def test_local_config_path_is_inside_config_dir(self):
        path = logt_adapter.LOCAL_CONFIG_PATH
        self.assertEqual(path.name, "clickhouse.local.json")
        self.assertEqual(path.parent.name, "config")

    def test_local_config_is_git_ignored(self):
        gitignore = Path(logt_adapter.REPO_ROOT) / ".gitignore"
        text = gitignore.read_text(encoding="utf-8")
        self.assertIn("config/clickhouse.local.json", text)

    def test_example_config_has_no_real_password(self):
        example = Path(logt_adapter.REPO_ROOT) / "config" / "clickhouse.example.json"
        data = json.loads(example.read_text(encoding="utf-8"))
        self.assertIn("password", data)
        self.assertIn(data["password"], ("", "changeme", "<password>"))


class TestLogTConfigAndCommandLine(unittest.TestCase):
    """The generated LogT config and the child command carry no secrets in argv."""

    def _config(self):
        return logt_adapter.resolve_config(
            {
                "host": "127.0.0.1",
                "port": 9000,
                "database": "tracelog",
                "user": "default",
                "password": SECRET,
            }
        )

    def test_logt_config_targets_http_port(self):
        payload = logt_adapter.build_logt_config("/logs", self._config(), dataset_name="ds")
        url = payload["clickhouse"]["url"]
        self.assertTrue(url.startswith("http://127.0.0.1:8123"))

    def test_password_not_in_url(self):
        payload = logt_adapter.build_logt_config("/logs", self._config(), dataset_name="ds")
        URL_KEYS = ("url", "dsn", "endpoint")
        for key in URL_KEYS:
            value = payload["clickhouse"].get(key)
            if value:
                self.assertNotIn(SECRET, str(value))
                self.assertNotIn("@", str(value))

    def test_config_file_lives_in_a_temp_dir_that_is_deleted(self):
        """The password-bearing config must not survive the import."""
        import shutil
        import uuid

        scratch = Path(logt_adapter.REPO_ROOT) / ".tmp"
        scratch.mkdir(exist_ok=True)
        staging = scratch / f"stage-{uuid.uuid4().hex[:8]}"
        staging.mkdir()
        try:
            payload = logt_adapter.build_logt_config(
                "/logs", self._config(), dataset_name="ds"
            )
            path = logt_adapter._write_private_config(staging, payload)
            self.assertTrue(path.exists())
            self.assertIn(SECRET, path.read_text(encoding="utf-8"))
            # The staging directory is what run_logt_import removes in `finally`;
            # here we only assert the file is confined to it, not written to the
            # repo or the vendored LogT tree (which LogT itself would read).
            self.assertEqual(path.parent, staging)
            self.assertNotEqual(path.parent, Path(logt_adapter.REPO_ROOT))
            self.assertNotIn("vendor", str(path))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def test_run_logt_import_removes_staging_directory(self):
        """Every staging directory created by an import is deleted afterwards."""
        import glob
        import uuid

        original = Path(logt_adapter.REPO_ROOT) / ".tmp"
        original.mkdir(exist_ok=True)
        scratch = original / f"work-{uuid.uuid4().hex[:8]}"
        scratch.mkdir()

        class FakeProcess:
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.stdout = _EmptyPipe()
                self.stderr = _EmptyPipe()
                self.stdin = None
                self.pid = 1
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        try:
            with mock.patch.dict(os.environ, {"LOGT_WORK_DIR": str(scratch)}):
                with mock.patch.object(logt_adapter.subprocess, "Popen", FakeProcess):
                    with mock.patch.object(
                        logt_adapter, "count_tables", return_value={"events": 1}
                    ):
                        with mock.patch.object(
                            logt_adapter,
                            "_resolve_logt_entrypoint",
                            return_value=Path(logt_adapter.REPO_ROOT) / "logt_adapter.py",
                        ):
                            logt_adapter.run_logt_import(
                                ".", self._config(), dataset_name="ds"
                            )
            leftovers = glob.glob(str(scratch / "logt-run-*"))
            self.assertEqual(leftovers, [], msg=f"staging not cleaned: {leftovers}")
        finally:
            import shutil

            shutil.rmtree(scratch, ignore_errors=True)

    def test_run_logt_import_never_puts_password_in_argv(self):
        captured = {}

        class FakeProcess:
            def __init__(self, argv, **kwargs):
                captured["argv"] = argv
                captured["kwargs"] = kwargs
                self.stdout = _EmptyPipe()
                self.stderr = _EmptyPipe()
                self.pid = 1234
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        with mock.patch.object(logt_adapter.subprocess, "Popen", FakeProcess):
            with mock.patch.object(logt_adapter, "count_tables", return_value={"events": 1}):
                with mock.patch.object(
                    logt_adapter,
                    "_resolve_logt_entrypoint",
                    return_value=Path(logt_adapter.REPO_ROOT) / "logt_adapter.py",
                ):
                    with mock.patch.dict(
                        os.environ,
                        {"LOGT_WORK_DIR": str(Path(logt_adapter.REPO_ROOT) / ".tmp")},
                    ):
                        logt_adapter.run_logt_import(
                            ".", self._config(), dataset_name="ds"
                        )
        argv = captured.get("argv") or []
        joined = " ".join(str(part) for part in argv)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn("password", joined.lower())
        # And the child environment must not inherit the password either.
        env = captured.get("kwargs", {}).get("env") or {}
        self.assertNotIn("CLICKHOUSE_PASSWORD", env)

    def test_import_result_redacts_password(self):
        """Failure diagnostics built from child output carry no password."""
        result = _run_scripted_import(
            {"database": "tracelog", "password": OVERRIDE_SECRET},
            stdout_lines=[f"leak {OVERRIDE_SECRET} on stdout\n"],
            stderr_lines=[f"Fatal: cannot insert password={OVERRIDE_SECRET}\n"],
            exit_code=1,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Fatal", result.error)
        rendered = (
            result.stdout,
            result.stderr,
            result.error,
            "\n".join(result.stderr_tail),
        )
        for text in rendered:
            self.assertNotIn(OVERRIDE_SECRET, text)


class _EmptyPipe:
    """File-like stub that reports immediate EOF."""

    def readline(self):
        return ""

    def read(self):
        return ""

    def close(self):
        pass


class TestStdoutIsNotParsed(unittest.TestCase):
    """MAJOR 1: control flow never depends on LogT's stdout format."""

    def test_rows_loaded_comes_from_clickhouse_not_stdout(self):
        """A misleading stdout must not change the reported row count."""
        class FakeProcess:
            def __init__(self, argv, **kwargs):
                self.stdout = _ScriptedPipe(["Inserted 999999 rows\n", "DONE\n"])
                self.stderr = _EmptyPipe()
                self.pid = 1
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        counts = {"events": 7, "operations": 1}
        with mock.patch.object(logt_adapter.subprocess, "Popen", FakeProcess):
            with mock.patch.object(logt_adapter, "count_tables", return_value=counts) as counter:
                with mock.patch.object(
                    logt_adapter,
                    "_resolve_logt_entrypoint",
                    return_value=Path(logt_adapter.REPO_ROOT) / "logt_adapter.py",
                ):
                    with mock.patch.dict(
                        os.environ,
                        {"LOGT_WORK_DIR": str(Path(logt_adapter.REPO_ROOT) / ".tmp")},
                    ):
                        config = logt_adapter.resolve_config(
                            {"database": "tracelog", "password": SECRET}
                        )
                        result = logt_adapter.run_logt_import(
                            ".", config, dataset_name="ds"
                        )
        self.assertTrue(result.success)
        self.assertEqual(result.rows_loaded, 7)
        self.assertEqual(result.table_counts, counts)
        counter.assert_called_once()

    def test_nonzero_exit_reports_code_and_stderr_tail(self):
        class FakeProcess:
            def __init__(self, argv, **kwargs):
                self.stdout = _ScriptedPipe(["[INFO] starting\n"])
                self.stderr = _ScriptedPipe([f"Fatal: cannot insert line {i}\n" for i in range(40)])
                self.pid = 1
                self.returncode = 1

            def poll(self):
                return 1

            def wait(self, timeout=None):
                return 1

            def terminate(self):
                pass

            def kill(self):
                pass

        with mock.patch.object(logt_adapter.subprocess, "Popen", FakeProcess):
            with mock.patch.object(logt_adapter, "count_tables", return_value={}):
                with mock.patch.object(
                    logt_adapter,
                    "_resolve_logt_entrypoint",
                    return_value=Path(logt_adapter.REPO_ROOT) / "logt_adapter.py",
                ):
                    with mock.patch.dict(
                        os.environ,
                        {"LOGT_WORK_DIR": str(Path(logt_adapter.REPO_ROOT) / ".tmp")},
                    ):
                        config = logt_adapter.resolve_config(
                            {"database": "tracelog", "password": SECRET}
                        )
                        result = logt_adapter.run_logt_import(
                            ".", config, dataset_name="ds"
                        )
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("1", result.error)
        # Exactly the tail is included, not the whole stream.
        self.assertLessEqual(len(result.stderr_tail), logt_adapter.STDERR_TAIL_LINES)

    def test_zero_rows_is_reported_as_failure(self):
        """LogT exits 0 even when nothing was written; the adapter must notice."""
        class FakeProcess:
            def __init__(self, argv, **kwargs):
                self.stdout = _ScriptedPipe(["[INFO] done\n"])
                self.stderr = _EmptyPipe()
                self.pid = 1
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        with mock.patch.object(logt_adapter.subprocess, "Popen", FakeProcess):
            with mock.patch.object(logt_adapter, "count_tables", return_value={"events": 0}):
                with mock.patch.object(
                    logt_adapter,
                    "_resolve_logt_entrypoint",
                    return_value=Path(logt_adapter.REPO_ROOT) / "logt_adapter.py",
                ):
                    with mock.patch.dict(
                        os.environ,
                        {"LOGT_WORK_DIR": str(Path(logt_adapter.REPO_ROOT) / ".tmp")},
                    ):
                        config = logt_adapter.resolve_config(
                            {"database": "tracelog", "password": SECRET}
                        )
                        result = logt_adapter.run_logt_import(
                            ".", config, dataset_name="ds"
                        )
        self.assertFalse(result.success)
        self.assertEqual(result.rows_loaded, 0)
        self.assertIn("0", result.error)


class _ScriptedPipe:
    """File-like stub yielding a fixed list of lines then EOF."""

    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return ""

    def read(self):
        return ""

    def close(self):
        pass


def _run_scripted_import(
    config,
    stdout_lines=(),
    stderr_lines=(),
    exit_code=0,
    counts=None,
    progress_callback=None,
    env=None,
):
    """Run an import against a fake child with scripted stdout/stderr.

    Args:
        config: Connection overrides passed to
            :func:`logt_adapter.run_logt_import`.
        stdout_lines: Lines the fake child writes to stdout.
        stderr_lines: Lines the fake child writes to stderr.
        exit_code: Exit code reported by the fake child.
        counts: Value returned by the stubbed ``count_tables``; defaults to a
            single event so the import succeeds.
        progress_callback: Optional callback fed by the import.
        env: Extra environment variables visible to the import.  Unless
            ``CLICKHOUSE_PASSWORD`` is one of them it is removed, so tests stay
            independent of the developer's shell.

    Returns:
        The :class:`logt_adapter.ImportResult`.
    """

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            self.stdout = _ScriptedPipe(stdout_lines)
            self.stderr = _ScriptedPipe(stderr_lines)
            self.stdin = None
            self.pid = 1
            self.returncode = exit_code

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            pass

        def kill(self):
            pass

    scratch = Path(logt_adapter.REPO_ROOT) / ".tmp"
    scratch.mkdir(exist_ok=True)
    environ = {"LOGT_WORK_DIR": str(scratch)}
    environ.update(env or {})
    table_counts = {"events": 1} if counts is None else counts
    with mock.patch.dict(os.environ, environ, clear=False):
        if logt_adapter.ENV_PASSWORD not in (env or {}):
            os.environ.pop(logt_adapter.ENV_PASSWORD, None)
        with mock.patch.object(logt_adapter, "load_local_config", return_value={}):
            with mock.patch.object(logt_adapter.subprocess, "Popen", FakeProcess):
                with mock.patch.object(
                    logt_adapter, "count_tables", return_value=table_counts
                ):
                    with mock.patch.object(
                        logt_adapter,
                        "_resolve_logt_entrypoint",
                        return_value=Path(logt_adapter.REPO_ROOT) / "logt_adapter.py",
                    ):
                        return logt_adapter.run_logt_import(
                            ".",
                            config,
                            progress_callback=progress_callback,
                            dataset_name="ds",
                        )


class TestProgressRedaction(unittest.TestCase):
    """P1-00d: progress messages use the resolved password, not just the env."""

    def test_progress_callback_redacts_override_password(self):
        """A GUI-supplied password never reaches the progress callback."""
        messages = []
        result = _run_scripted_import(
            {"database": "tracelog", "password": OVERRIDE_SECRET},
            stdout_lines=[
                f"[INFO] password={OVERRIDE_SECRET}\n",
                f"[INFO] dsn clickhouse://default:{OVERRIDE_SECRET}@127.0.0.1:8123\n",
                "[INFO] inserted 42 rows\n",
            ],
            progress_callback=messages.append,
        )

        self.assertTrue(messages)
        joined = "\n".join(messages)
        self.assertNotIn(OVERRIDE_SECRET, joined)
        self.assertIn("42 rows", joined)  # ordinary diagnostics survive
        self.assertTrue(any("***" in message for message in messages))
        self.assertNotIn(OVERRIDE_SECRET, result.stdout)
        self.assertNotIn(OVERRIDE_SECRET, result.stderr)
        self.assertNotIn(OVERRIDE_SECRET, result.error)

    def test_progress_callback_redacts_environment_password(self):
        """``CLICKHOUSE_PASSWORD`` output is masked in progress messages too."""
        messages = []
        result = _run_scripted_import(
            {"database": "tracelog"},
            stdout_lines=[
                f"[INFO] password={ENV_SECRET}\n",
                "[INFO] inserted 42 rows\n",
            ],
            progress_callback=messages.append,
            env={logt_adapter.ENV_PASSWORD: ENV_SECRET},
        )

        self.assertTrue(messages)
        joined = "\n".join(messages)
        self.assertNotIn(ENV_SECRET, joined)
        self.assertTrue(any("***" in message for message in messages))
        self.assertNotIn(ENV_SECRET, result.stdout)
        self.assertNotIn(ENV_SECRET, result.stderr)
        self.assertNotIn(ENV_SECRET, result.error)

    def test_progress_callback_failure_does_not_break_import(self):
        """A raising UI callback is swallowed, as before."""
        def explode(message):
            raise RuntimeError("UI is gone")

        result = _run_scripted_import(
            {"database": "tracelog", "password": OVERRIDE_SECRET},
            stdout_lines=["[INFO] inserted 42 rows\n"],
            progress_callback=explode,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.rows_loaded, 1)


class TestImportEntrypointResolution(unittest.TestCase):
    """MINOR 2: the vendored path is resolved, not hardcoded."""

    @unittest.skipUnless(LOGT_VENDORED, "vendor/logt is not bundled; set LOGT_ROOT to run this test")
    def test_default_entrypoint_is_under_repo_root(self):
        path = logt_adapter._resolve_logt_entrypoint()
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "main.py")
        self.assertIn("logt", str(path).lower())

    @unittest.skipUnless(LOGT_VENDORED, "vendor/logt is not bundled; set LOGT_ROOT to run this test")
    def test_environment_can_override_logt_root(self):
        with mock.patch.dict(os.environ, {"LOGT_ROOT": str(Path(logt_adapter.REPO_ROOT) / "vendor" / "logt")}):
            path = logt_adapter._resolve_logt_entrypoint()
        self.assertTrue(path.exists())

    def test_missing_entrypoint_raises_actionable_error(self):
        with mock.patch.object(logt_adapter, "REPO_ROOT", Path(logt_adapter.REPO_ROOT) / "no-such-dir"):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LOGT_ROOT", None)
                with self.assertRaises(logt_adapter.AdapterError) as caught:
                    logt_adapter._resolve_logt_entrypoint()
        self.assertIn("LOGT_ROOT", str(caught.exception))

    @unittest.skipUnless(LOGT_VENDORED, "vendor/logt is not bundled; set LOGT_ROOT to run this test")
    def test_override_falls_back_to_vendored_copy(self):
        """A wrong LOGT_ROOT should not be fatal if the vendored copy exists."""
        path = logt_adapter._resolve_logt_entrypoint(
            Path(logt_adapter.REPO_ROOT) / "no-such-dir"
        )
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
