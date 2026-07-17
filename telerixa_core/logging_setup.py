import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .constants import BOT_LOG_FILE, LOG_DIR


LOG_FORMAT = "%(asctime)s %(levelname)s:%(name)s:%(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
SUCCESS_LEVEL = 25
ANSI_RESET = "\x1b[0m"
ANSI_LEVEL_COLORS = {
    logging.DEBUG: "\x1b[90m",
    logging.INFO: "\x1b[36m",
    SUCCESS_LEVEL: "\x1b[32m",
    logging.WARNING: "\x1b[33m",
    logging.ERROR: "\x1b[31m",
    logging.CRITICAL: "\x1b[1;31m",
}

logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


class ConsoleColorFormatter(logging.Formatter):
    def __init__(self, *args, use_color=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_color = use_color

    def format(self, record):
        rendered = super().format(record)
        if not self.use_color:
            return rendered
        color = ANSI_LEVEL_COLORS.get(record.levelno)
        if not color:
            return rendered
        return f"{color}{rendered}{ANSI_RESET}"


def _enable_windows_ansi(stream):
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint()
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


def stream_supports_color(stream):
    if os.environ.get("NO_COLOR") is not None:
        return False
    if not getattr(stream, "isatty", lambda: False)():
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    return _enable_windows_ansi(stream)


def build_console_formatter(stream):
    return ConsoleColorFormatter(
        LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        use_color=stream_supports_color(stream),
    )


def log_success(logger, message, *args, **kwargs):
    logger.log(SUCCESS_LEVEL, message, *args, **kwargs)


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)

    file_formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(build_console_formatter(console_handler.stream))

    file_handler = RotatingFileHandler(
        BOT_LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(file_formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
