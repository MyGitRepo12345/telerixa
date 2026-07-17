import _thread
import json
import os
import signal
import threading
import time
import uuid
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    def __init__(self, service_name, pid):
        self.service_name = service_name
        self.pid = pid
        super().__init__(f"{service_name} is already running (PID {pid}).")


class DetachedProcessError(RuntimeError):
    pass


def is_process_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes

            synchronize = 0x00100000
            wait_timeout = 0x00000102
            handle = ctypes.windll.kernel32.OpenProcess(
                synchronize,
                False,
                pid,
            )
            if not handle:
                return ctypes.windll.kernel32.GetLastError() == 5
            try:
                return (
                    ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
                    == wait_timeout
                )
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ProcessLock:
    def __init__(self, path, service_name):
        self.path = Path(path)
        self.service_name = service_name
        self.pid = os.getpid()
        self.token = uuid.uuid4().hex
        self.acquired = False

    def _read_existing(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": self.pid,
            "service": self.service_name,
            "started_ts": time.time(),
            "token": self.token,
        }

        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                existing = self._read_existing()
                existing_pid = existing.get("pid")
                if is_process_running(existing_pid):
                    raise AlreadyRunningError(self.service_name, existing_pid)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue

            with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
                json.dump(payload, lock_file)
                lock_file.flush()
                os.fsync(lock_file.fileno())
            self.acquired = True
            return self

    def release(self):
        if not self.acquired:
            return
        try:
            existing = self._read_existing()
            if existing.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        finally:
            self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


def _windows_console_attached():
    if os.name != "nt":
        return True
    try:
        import ctypes

        process_ids = (ctypes.c_uint * 1)()
        return bool(ctypes.windll.kernel32.GetConsoleProcessList(process_ids, 1))
    except (AttributeError, OSError):
        return False


def require_attached_console():
    if os.name != "nt" or os.environ.get("TELERIXA_ALLOW_DETACHED") == "1":
        return
    if not _windows_console_attached():
        raise DetachedProcessError(
            "Telerixa refused to start without an attached console. "
            "Use run.bat/run_ui.bat or set TELERIXA_ALLOW_DETACHED=1 explicitly."
        )


class ShutdownSignalHandlers:
    def __init__(self):
        self.previous_handlers = {}

    @staticmethod
    def _handle_signal(signum, frame):
        raise KeyboardInterrupt

    def __enter__(self):
        signal_names = ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
        for signal_name in signal_names:
            signal_value = getattr(signal, signal_name, None)
            if signal_value is None or signal_value in self.previous_handlers:
                continue
            try:
                self.previous_handlers[signal_value] = signal.getsignal(signal_value)
                signal.signal(signal_value, self._handle_signal)
            except (OSError, RuntimeError, ValueError):
                self.previous_handlers.pop(signal_value, None)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for signal_value, previous_handler in self.previous_handlers.items():
            try:
                signal.signal(signal_value, previous_handler)
            except (OSError, RuntimeError, ValueError):
                pass
        self.previous_handlers.clear()


class ProcessLifetimeMonitor:
    def __init__(self, owner_pid=None, poll_interval=0.5):
        if owner_pid is None:
            owner_pid = os.environ.get("TELERIXA_OWNER_PID") or os.getppid()
        try:
            owner_pid = int(owner_pid)
        except (TypeError, ValueError):
            owner_pid = 0

        self.owner_pid = owner_pid
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self.thread = None
        self.reason = ""
        self.monitor_console = (
            os.name == "nt"
            and os.environ.get("TELERIXA_ALLOW_DETACHED") != "1"
            and _windows_console_attached()
        )

    def _watch(self):
        while not self.stop_event.wait(self.poll_interval):
            if self.owner_pid > 0 and not is_process_running(self.owner_pid):
                self.reason = f"owner process {self.owner_pid} exited"
                _thread.interrupt_main()
                return
            if self.monitor_console and not _windows_console_attached():
                self.reason = "console was closed"
                _thread.interrupt_main()
                return

    def __enter__(self):
        if self.owner_pid > 0 or self.monitor_console:
            self.thread = threading.Thread(
                target=self._watch,
                name="telerixa-lifetime-monitor",
                daemon=True,
            )
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=max(1.0, self.poll_interval * 2))
