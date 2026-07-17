import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import tempfile

from .ffmpeg_tools import FFmpegSetupError, ensure_ffmpeg_tools


DEFAULT_SIZE_RESERVE_RATIO = 0.95
SECOND_ATTEMPT_RESERVE_RATIO = 0.92
MIN_VIDEO_BITRATE_KBPS = 120
DEFAULT_AUDIO_BITRATE_KBPS = 96
BITRATE_OVERHEAD_KBPS = 24

FFMPEG_PRESETS = {
    "fast": "veryfast",
    "balanced": "fast",
    "quality": "medium",
}


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class TranscodeResult:
    success: bool
    output_path: str = ""
    error: str = ""
    source_size: int = 0
    output_size: int = 0
    duration_seconds: float = 0.0
    attempts: int = 0


def is_video_file(file_path):
    return Path(file_path).suffix.lower() in {
        ".avi",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".webm",
    }


def is_video_media(media):
    mime_type = str(getattr(media, "mime_type", "") or "").lower()
    if not mime_type:
        document = getattr(media, "document", None)
        mime_type = str(getattr(document, "mime_type", "") or "").lower()
    return mime_type.startswith("video/")


def _decode_process_output(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


async def _stop_process(process):
    if process.returncode is not None:
        return

    try:
        process.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass

    try:
        process.kill()
    except ProcessLookupError:
        return
    await process.wait()


async def _run_process(arguments, timeout_seconds):
    creation_flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW

    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creation_flags,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=max(1, int(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        await _stop_process(process)
        return ProcessResult(
            returncode=process.returncode if process.returncode is not None else -1,
            timed_out=True,
        )
    except asyncio.CancelledError:
        await _stop_process(process)
        raise

    return ProcessResult(
        returncode=int(process.returncode or 0),
        stdout=_decode_process_output(stdout),
        stderr=_decode_process_output(stderr),
    )


async def _probe_duration(ffprobe_path, input_path, timeout_seconds):
    result = await _run_process(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            input_path,
        ],
        min(timeout_seconds, 30),
    )
    if result.timed_out:
        return 0.0, "ffprobe timed out"
    if result.returncode != 0:
        detail = result.stderr.strip() or f"ffprobe exited with {result.returncode}"
        return 0.0, detail
    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError, OverflowError):
        return 0.0, "ffprobe returned an invalid duration"
    if duration <= 0:
        return 0.0, "ffprobe returned a non-positive duration"
    return duration, ""


def _calculate_video_bitrate_kbps(target_bytes, duration_seconds, reserve_ratio):
    target_bits = max(1, int(target_bytes * reserve_ratio)) * 8
    total_kbps = target_bits / duration_seconds / 1000
    return max(
        MIN_VIDEO_BITRATE_KBPS,
        int(total_kbps - DEFAULT_AUDIO_BITRATE_KBPS - BITRATE_OVERHEAD_KBPS),
    )


def _max_video_height(video_bitrate_kbps):
    if video_bitrate_kbps >= 2500:
        return 1080
    if video_bitrate_kbps >= 1200:
        return 720
    if video_bitrate_kbps >= 700:
        return 540
    if video_bitrate_kbps >= 400:
        return 480
    return 360


def _build_ffmpeg_arguments(
    ffmpeg_path,
    input_path,
    output_path,
    video_bitrate_kbps,
    preset,
):
    max_height = _max_video_height(video_bitrate_kbps)
    max_rate = max(video_bitrate_kbps, int(video_bitrate_kbps * 1.08))
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        f"scale=w=-2:h=min(ih\\,{max_height}):force_original_aspect_ratio=decrease",
        "-c:v",
        "libx264",
        "-preset",
        FFMPEG_PRESETS.get(preset, FFMPEG_PRESETS["balanced"]),
        "-b:v",
        f"{video_bitrate_kbps}k",
        "-maxrate",
        f"{max_rate}k",
        "-bufsize",
        f"{video_bitrate_kbps * 2}k",
        "-c:a",
        "aac",
        "-b:a",
        f"{DEFAULT_AUDIO_BITRATE_KBPS}k",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output_path,
    ]


def _temporary_output_path(output_dir, input_path):
    prefix = "SPOILER_" if Path(input_path).name.startswith("SPOILER_") else ""
    descriptor, output_path = tempfile.mkstemp(
        prefix=f"{prefix}telerixa_",
        suffix=".mp4",
        dir=output_dir,
    )
    os.close(descriptor)
    return output_path


def _remove_file(file_path):
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass


async def transcode_video(
    input_path,
    output_dir,
    target_limit_mb,
    preset="balanced",
    timeout_seconds=600,
):
    source_size = os.path.getsize(input_path)
    timeout_seconds = max(30, int(timeout_seconds))
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        tools = await asyncio.to_thread(ensure_ffmpeg_tools)
    except FFmpegSetupError as error:
        return TranscodeResult(
            success=False,
            error=str(error),
            source_size=source_size,
        )
    ffmpeg_path = tools.ffmpeg
    ffprobe_path = tools.ffprobe

    try:
        duration, probe_error = await _probe_duration(
            ffprobe_path,
            input_path,
            timeout_seconds,
        )
    except OSError as error:
        return TranscodeResult(
            success=False,
            error=f"could not start ffprobe: {error}",
            source_size=source_size,
        )
    if probe_error:
        return TranscodeResult(
            success=False,
            error=probe_error,
            source_size=source_size,
        )

    target_bytes = max(1, int(target_limit_mb)) * 1024 * 1024
    video_bitrate = _calculate_video_bitrate_kbps(
        target_bytes,
        duration,
        DEFAULT_SIZE_RESERVE_RATIO,
    )
    output_path = _temporary_output_path(output_dir, input_path)

    for attempt in range(1, 3):
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            _remove_file(output_path)
            return TranscodeResult(
                success=False,
                error=f"ffmpeg timed out after {timeout_seconds} seconds",
                source_size=source_size,
                duration_seconds=duration,
                attempts=attempt - 1,
            )
        _remove_file(output_path)
        arguments = _build_ffmpeg_arguments(
            ffmpeg_path,
            input_path,
            output_path,
            video_bitrate,
            preset,
        )
        try:
            process_result = await _run_process(arguments, remaining_seconds)
        except asyncio.CancelledError:
            _remove_file(output_path)
            raise
        except OSError as error:
            _remove_file(output_path)
            return TranscodeResult(
                success=False,
                error=f"could not start ffmpeg: {error}",
                source_size=source_size,
                duration_seconds=duration,
                attempts=attempt,
            )
        if process_result.timed_out:
            _remove_file(output_path)
            return TranscodeResult(
                success=False,
                error=f"ffmpeg timed out after {timeout_seconds} seconds",
                source_size=source_size,
                duration_seconds=duration,
                attempts=attempt,
            )
        if process_result.returncode != 0:
            detail = process_result.stderr.strip()
            _remove_file(output_path)
            return TranscodeResult(
                success=False,
                error=detail or f"ffmpeg exited with {process_result.returncode}",
                source_size=source_size,
                duration_seconds=duration,
                attempts=attempt,
            )
        if not os.path.exists(output_path):
            return TranscodeResult(
                success=False,
                error="ffmpeg did not create an output file",
                source_size=source_size,
                duration_seconds=duration,
                attempts=attempt,
            )

        output_size = os.path.getsize(output_path)
        if output_size <= target_bytes:
            return TranscodeResult(
                success=True,
                output_path=output_path,
                source_size=source_size,
                output_size=output_size,
                duration_seconds=duration,
                attempts=attempt,
            )

        if attempt == 1:
            correction_ratio = target_bytes / max(1, output_size)
            video_bitrate = max(
                MIN_VIDEO_BITRATE_KBPS,
                int(
                    video_bitrate
                    * correction_ratio
                    * SECOND_ATTEMPT_RESERVE_RATIO
                ),
            )

    _remove_file(output_path)
    return TranscodeResult(
        success=False,
        error="converted video still exceeds the Discord file limit",
        source_size=source_size,
        duration_seconds=duration,
        attempts=2,
    )
