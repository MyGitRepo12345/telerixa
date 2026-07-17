from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.request import Request, urlopen
import zipfile


SETUP_RETRY_SECONDS = 300
DOWNLOAD_TIMEOUT_SECONDS = 600
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
BUILD_ID = "btbn-ffmpeg-8.1.2-2026-06-30"
BUILD_BASE_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
    "autobuild-2026-06-30-13-34"
)


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: str
    ffprobe: str
    source: str


@dataclass(frozen=True)
class ManagedBuild:
    platform_key: str
    archive_name: str
    sha256: str
    size: int
    archive_type: str

    @property
    def url(self):
        return f"{BUILD_BASE_URL}/{self.archive_name}"


MANAGED_BUILDS = {
    "linux-x86_64": ManagedBuild(
        platform_key="linux-x86_64",
        archive_name=(
            "ffmpeg-n8.1.2-21-gce3c09c101-linux64-gpl-8.1.tar.xz"
        ),
        sha256=(
            "0ba73bbd93472c7622f6dec26d334c5e"
            "62e64d858d072490b2844320970456cd"
        ),
        size=124756048,
        archive_type="tar.xz",
    ),
    "windows-x86_64": ManagedBuild(
        platform_key="windows-x86_64",
        archive_name=(
            "ffmpeg-n8.1.2-21-gce3c09c101-win64-gpl-8.1.zip"
        ),
        sha256=(
            "682361e32c9631caec09e5d9f0907710"
            "1c9ed90c14e275f62014fefa6d397990"
        ),
        size=166372072,
        archive_type="zip",
    ),
}


class FFmpegSetupError(RuntimeError):
    pass


_setup_lock = threading.Lock()
_cached_tools = None
_cached_error = None
_cached_error_at = 0.0


def _platform_key():
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64"}:
        raise FFmpegSetupError(f"unsupported CPU architecture: {machine}")
    if sys.platform == "win32":
        return "windows-x86_64"
    if sys.platform.startswith("linux"):
        return "linux-x86_64"
    raise FFmpegSetupError(f"unsupported operating system: {sys.platform}")


def get_managed_build():
    return MANAGED_BUILDS[_platform_key()]


def _tools_root():
    configured = os.environ.get("TELERIXA_TOOLS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / ".telerixa-tools"


def _managed_directory(build):
    return _tools_root() / "ffmpeg" / BUILD_ID / build.platform_key


def _managed_executable_paths(build, directory=None):
    directory = Path(directory or _managed_directory(build))
    suffix = ".exe" if build.platform_key.startswith("windows-") else ""
    return directory / f"ffmpeg{suffix}", directory / f"ffprobe{suffix}"


def _marker_path(build):
    return _managed_directory(build) / "installed.json"


def _valid_managed_tools(build):
    ffmpeg_path, ffprobe_path = _managed_executable_paths(build)
    if not ffmpeg_path.is_file() or not ffprobe_path.is_file():
        return False
    try:
        marker = json.loads(_marker_path(build).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        marker.get("build_id") == BUILD_ID
        and marker.get("archive") == build.archive_name
        and marker.get("archive_sha256") == build.sha256
    )


def _valid_system_tools(ffmpeg_path, ffprobe_path):
    return bool(ffmpeg_path and ffprobe_path)


def find_ffmpeg_tools():
    """Find ready-to-use system or previously verified managed tools."""
    global _cached_tools

    if _cached_tools is not None:
        if _cached_tools.source == "system" and _valid_system_tools(
            _cached_tools.ffmpeg,
            _cached_tools.ffprobe,
        ):
            return _cached_tools
        try:
            build = get_managed_build()
        except FFmpegSetupError:
            build = None
        if build is not None and _valid_managed_tools(build):
            return _cached_tools

    system_ffmpeg = shutil.which("ffmpeg")
    system_ffprobe = shutil.which("ffprobe")
    if _valid_system_tools(system_ffmpeg, system_ffprobe):
        _cached_tools = FFmpegTools(
            ffmpeg=str(system_ffmpeg),
            ffprobe=str(system_ffprobe),
            source="system",
        )
        return _cached_tools

    try:
        build = get_managed_build()
    except FFmpegSetupError:
        return None
    if not _valid_managed_tools(build):
        return None

    ffmpeg_path, ffprobe_path = _managed_executable_paths(build)
    _cached_tools = FFmpegTools(
        ffmpeg=str(ffmpeg_path),
        ffprobe=str(ffprobe_path),
        source="managed-btbn",
    )
    return _cached_tools


def _download_archive(build, archive_path, open_url=urlopen):
    request = Request(
        build.url,
        headers={"User-Agent": "Telerixa FFmpeg bootstrap"},
    )
    digest = hashlib.sha256()
    downloaded = 0
    next_progress = 10
    expected_mb = build.size / (1024 * 1024)
    print(
        f"Downloading verified FFmpeg build ({expected_mb:.1f} MB)...",
        flush=True,
    )
    with open_url(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        with open(archive_path, "wb") as archive_file:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                archive_file.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if downloaded > build.size:
                    raise FFmpegSetupError(
                        "FFmpeg archive exceeded expected size: "
                        f"expected {build.size}, received at least {downloaded}"
                    )
                progress = int(downloaded * 100 / max(1, build.size))
                if progress >= next_progress:
                    print(f"FFmpeg download: {min(progress, 100)}%", flush=True)
                    next_progress += 10

    if downloaded != build.size:
        raise FFmpegSetupError(
            f"FFmpeg archive size mismatch: expected {build.size}, got {downloaded}"
        )
    actual_digest = digest.hexdigest()
    if actual_digest != build.sha256:
        raise FFmpegSetupError(
            "FFmpeg archive SHA-256 mismatch: "
            f"expected {build.sha256}, got {actual_digest}"
        )
    print("FFmpeg archive SHA-256 verified.", flush=True)


def _is_tool_member(member_name, executable_name):
    normalized = PurePosixPath(str(member_name).replace("\\", "/"))
    return (
        not normalized.is_absolute()
        and ".." not in normalized.parts
        and len(normalized.parts) >= 2
        and normalized.parts[-2:] == ("bin", executable_name)
    )


def _copy_stream(source, destination):
    with source, open(destination, "wb") as target:
        shutil.copyfileobj(source, target, length=DOWNLOAD_CHUNK_SIZE)


def _extract_zip_tools(archive_path, destination, executable_names):
    found = set()
    with zipfile.ZipFile(archive_path, "r") as archive:
        for member in archive.infolist():
            for executable_name in executable_names:
                if _is_tool_member(member.filename, executable_name):
                    _copy_stream(
                        archive.open(member, "r"),
                        destination / executable_name,
                    )
                    found.add(executable_name)
    return found


def _extract_tar_tools(archive_path, destination, executable_names):
    found = set()
    with tarfile.open(archive_path, "r:xz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            for executable_name in executable_names:
                if _is_tool_member(member.name, executable_name):
                    source = archive.extractfile(member)
                    if source is not None:
                        _copy_stream(source, destination / executable_name)
                        found.add(executable_name)
    return found


def _extract_tools(build, archive_path, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if build.platform_key.startswith("windows-") else ""
    executable_names = {f"ffmpeg{suffix}", f"ffprobe{suffix}"}
    if build.archive_type == "zip":
        found = _extract_zip_tools(
            archive_path,
            destination,
            executable_names,
        )
    elif build.archive_type == "tar.xz":
        found = _extract_tar_tools(
            archive_path,
            destination,
            executable_names,
        )
    else:
        raise FFmpegSetupError(
            f"unsupported FFmpeg archive type: {build.archive_type}"
        )
    missing = sorted(executable_names - found)
    if missing:
        raise FFmpegSetupError(
            f"FFmpeg archive is missing required files: {', '.join(missing)}"
        )


def _validate_executables(build, directory):
    ffmpeg_path, ffprobe_path = _managed_executable_paths(build, directory)
    if not build.platform_key.startswith("windows-"):
        ffmpeg_path.chmod(0o755)
        ffprobe_path.chmod(0o755)

    creation_flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW
    for executable in (ffmpeg_path, ffprobe_path):
        completed = subprocess.run(
            [str(executable), "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=creation_flags,
        )
        if completed.returncode != 0:
            raise FFmpegSetupError(
                f"downloaded executable failed validation: {executable.name}"
            )

    encoders = subprocess.run(
        [str(ffmpeg_path), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        creationflags=creation_flags,
    )
    encoder_output = encoders.stdout or encoders.stderr or ""
    if encoders.returncode != 0 or not all(
        codec in encoder_output for codec in ("libx264", "aac")
    ):
        raise FFmpegSetupError(
            "downloaded FFmpeg is missing required libx264 or aac encoders"
        )


def _install_managed_tools(build):
    target = _managed_directory(build)
    target.parent.mkdir(parents=True, exist_ok=True)
    archive_descriptor, archive_name = tempfile.mkstemp(
        prefix=".ffmpeg-download-",
        suffix=f".{build.archive_type}",
        dir=target.parent,
    )
    os.close(archive_descriptor)
    archive_path = Path(archive_name)
    staging = Path(
        tempfile.mkdtemp(prefix=".ffmpeg-install-", dir=target.parent)
    )
    try:
        _download_archive(build, archive_path)
        _extract_tools(build, archive_path, staging)
        _validate_executables(build, staging)
        marker = {
            "build_id": BUILD_ID,
            "source": "BtbN/FFmpeg-Builds",
            "source_url": build.url,
            "archive": build.archive_name,
            "archive_sha256": build.sha256,
        }
        (staging / "installed.json").write_text(
            json.dumps(marker, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
    finally:
        archive_path.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    ffmpeg_path, ffprobe_path = _managed_executable_paths(build)
    return FFmpegTools(
        ffmpeg=str(ffmpeg_path),
        ffprobe=str(ffprobe_path),
        source="managed-btbn",
    )


def ensure_ffmpeg_tools():
    """Return tools, downloading a pinned and verified build when necessary."""
    global _cached_error, _cached_error_at, _cached_tools

    available = find_ffmpeg_tools()
    if available is not None:
        return available
    if (
        _cached_error is not None
        and time.monotonic() - _cached_error_at < SETUP_RETRY_SECONDS
    ):
        raise FFmpegSetupError(_cached_error)

    with _setup_lock:
        available = find_ffmpeg_tools()
        if available is not None:
            return available
        if (
            _cached_error is not None
            and time.monotonic() - _cached_error_at < SETUP_RETRY_SECONDS
        ):
            raise FFmpegSetupError(_cached_error)

        try:
            _cached_tools = _install_managed_tools(get_managed_build())
        except Exception as error:
            _cached_error = f"could not prepare FFmpeg: {error}"
            _cached_error_at = time.monotonic()
            raise FFmpegSetupError(_cached_error) from error

        _cached_error = None
        _cached_error_at = 0.0
        return _cached_tools


def reset_ffmpeg_tools_cache():
    """Reset process-local resolver state for tests."""
    global _cached_error, _cached_error_at, _cached_tools
    with _setup_lock:
        _cached_tools = None
        _cached_error = None
        _cached_error_at = 0.0
