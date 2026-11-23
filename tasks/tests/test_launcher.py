import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from flamethrower.launcher import prepare_storage, runtime_record


class RuntimeRecordTests(SimpleTestCase):
    def test_records_port_replaces_stale_file_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            prepare_storage(project)
            path = project / ".flamethrower" / "server.json"
            path.write_text(json.dumps({"application": "flamethrower", "pid": 123, "port": 1234}))
            with runtime_record(path.parent, 4321):
                self.assertEqual(json.loads(path.read_text()), {
                    "application": "flamethrower", "pid": os.getpid(), "port": 4321,
                })
            self.assertFalse(path.exists())

    def test_does_not_remove_a_replaced_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            path = storage / "server.json"
            replacement = {"application": "flamethrower", "pid": 123, "port": 1234}
            with runtime_record(storage, 4321):
                path.write_text(json.dumps(replacement))
            self.assertEqual(json.loads(path.read_text()), replacement)

    def test_invalid_or_symlinked_files_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            path = storage / "server.json"
            path.write_text("unrelated data")
            with self.assertRaises(ValueError):
                with runtime_record(storage, 4321):
                    self.fail("Invalid file was accepted")
            self.assertEqual(path.read_text(), "unrelated data")
            path.unlink()
            target = storage / "target"
            target.write_text("preserve")
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                with runtime_record(storage, 4321):
                    self.fail("Symlink was accepted")
            self.assertEqual(target.read_text(), "preserve")
            self.assertTrue(path.is_symlink())

    def test_exception_cleans_up_and_failed_write_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "failure"):
                with runtime_record(storage, 4321):
                    raise RuntimeError("failure")
            self.assertFalse((storage / "server.json").exists())
            with patch("flamethrower.launcher.os.replace", side_effect=OSError("cannot write")):
                with self.assertRaises(OSError):
                    with runtime_record(storage, 4321):
                        self.fail("Write failure was ignored")
            self.assertEqual(list(storage.iterdir()), [])

    def test_launcher_publishes_actual_pid_and_port_and_cleans_up_on_signals(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            path = project / ".flamethrower" / "server.json"
            for signum in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(signal=signum):
                    process = subprocess.Popen(
                        [sys.executable, "-c", "from flamethrower.launcher import main; main()", "--no-browser", str(project)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    try:
                        deadline = time.monotonic() + 30
                        record = None
                        while time.monotonic() < deadline:
                            if process.poll() is not None:
                                self.fail(f"Launcher exited early: {process.communicate()}")
                            if path.exists():
                                record = json.loads(path.read_text())
                                try:
                                    with urllib.request.urlopen(f"http://127.0.0.1:{record['port']}/", timeout=1) as response:
                                        if response.status == 200:
                                            break
                                except (OSError, urllib.error.URLError):
                                    pass
                            time.sleep(0.1)
                        else:
                            self.fail("Launcher did not publish a responsive server.")
                        self.assertEqual(record["pid"], process.pid)
                        process.send_signal(signum)
                        process.wait(timeout=15)
                        self.assertFalse(path.exists())
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                        process.communicate()
