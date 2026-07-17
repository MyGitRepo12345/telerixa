import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telerixa_core import lifecycle


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.lock_file = Path(self.temp_dir.name) / "service.pid"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_process_lock_prevents_duplicate_and_cleans_up(self):
        with lifecycle.ProcessLock(self.lock_file, "test service"):
            payload = json.loads(self.lock_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], os.getpid())
            with self.assertRaises(lifecycle.AlreadyRunningError):
                lifecycle.ProcessLock(self.lock_file, "test service").acquire()

        self.assertFalse(self.lock_file.exists())

    def test_process_state_distinguishes_running_and_terminated_processes(self):
        self.assertTrue(lifecycle.is_process_running(os.getpid()))

        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=5)

        self.assertFalse(lifecycle.is_process_running(process.pid))

    def test_process_lock_recovers_stale_and_malformed_files(self):
        self.lock_file.write_text('{"pid": 99999999}', encoding="utf-8")
        with patch.object(lifecycle, "is_process_running", return_value=False):
            with lifecycle.ProcessLock(self.lock_file, "test service"):
                self.assertTrue(self.lock_file.exists())

        self.lock_file.write_text("broken", encoding="utf-8")
        with lifecycle.ProcessLock(self.lock_file, "test service"):
            self.assertTrue(self.lock_file.exists())

    def test_release_does_not_delete_another_process_lock(self):
        lock = lifecycle.ProcessLock(self.lock_file, "test service").acquire()
        payload = json.loads(self.lock_file.read_text(encoding="utf-8"))
        payload["token"] = "replacement-owner"
        self.lock_file.write_text(json.dumps(payload), encoding="utf-8")

        lock.release()

        self.assertTrue(self.lock_file.exists())

    def test_detached_windows_start_requires_explicit_override(self):
        with (
            patch.object(lifecycle.os, "name", "nt"),
            patch.object(lifecycle, "_windows_console_attached", return_value=False),
            patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaises(lifecycle.DetachedProcessError):
                lifecycle.require_attached_console()

        with (
            patch.object(lifecycle.os, "name", "nt"),
            patch.object(lifecycle, "_windows_console_attached", return_value=False),
            patch.dict(os.environ, {"TELERIXA_ALLOW_DETACHED": "1"}, clear=True),
        ):
            lifecycle.require_attached_console()

    def test_lifetime_monitor_interrupts_when_owner_exits(self):
        monitor = lifecycle.ProcessLifetimeMonitor(owner_pid=4242, poll_interval=0)
        with (
            patch.object(monitor.stop_event, "wait", return_value=False),
            patch.object(lifecycle, "is_process_running", return_value=False),
            patch.object(lifecycle._thread, "interrupt_main") as interrupt_main,
        ):
            monitor._watch()

        self.assertEqual(monitor.reason, "owner process 4242 exited")
        interrupt_main.assert_called_once_with()

    def test_shutdown_handler_raises_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            lifecycle.ShutdownSignalHandlers._handle_signal(None, None)


if __name__ == "__main__":
    unittest.main()
