import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telerixa_core import transcoding
from telerixa_core.ffmpeg_tools import FFmpegSetupError, FFmpegTools


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    async def communicate(self):
        await asyncio.Event().wait()

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.terminated:
            self.returncode = -15
        return self.returncode


class TranscodingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory(ignore_cleanup_errors=True)
        self.input_path = Path(self.temp_dir.name, "source.mp4")
        self.input_path.write_bytes(b"source video")

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def ffmpeg_tools():
        return FFmpegTools("ffmpeg", "ffprobe", "system")

    async def test_successful_transcode_returns_created_output(self):
        async def run_process(arguments, timeout_seconds):
            Path(arguments[-1]).write_bytes(b"converted")
            return transcoding.ProcessResult(returncode=0)

        with (
            patch.object(
                transcoding,
                "ensure_ffmpeg_tools",
                return_value=self.ffmpeg_tools(),
            ),
            patch.object(
                transcoding,
                "_probe_duration",
                AsyncMock(return_value=(30.0, "")),
            ),
            patch.object(transcoding, "_run_process", side_effect=run_process),
        ):
            result = await transcoding.transcode_video(
                str(self.input_path),
                self.temp_dir.name,
                target_limit_mb=1,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertTrue(Path(result.output_path).exists())
        self.assertEqual(result.output_size, len(b"converted"))

    def test_video_detection_uses_telegram_document_mime_type(self):
        wrapped_document = SimpleNamespace(
            document=SimpleNamespace(mime_type="video/mp4")
        )

        self.assertTrue(transcoding.is_video_media(wrapped_document))
        self.assertFalse(
            transcoding.is_video_media(
                SimpleNamespace(mime_type="application/pdf")
            )
        )

    async def test_oversized_first_output_retries_with_lower_bitrate(self):
        bitrates = []

        async def run_process(arguments, timeout_seconds):
            bitrate_index = arguments.index("-b:v") + 1
            bitrates.append(int(arguments[bitrate_index].removesuffix("k")))
            size = 2 * 1024 * 1024 if len(bitrates) == 1 else 800 * 1024
            Path(arguments[-1]).write_bytes(b"x" * size)
            return transcoding.ProcessResult(returncode=0)

        with (
            patch.object(
                transcoding,
                "ensure_ffmpeg_tools",
                return_value=self.ffmpeg_tools(),
            ),
            patch.object(
                transcoding,
                "_probe_duration",
                AsyncMock(return_value=(30.0, "")),
            ),
            patch.object(transcoding, "_run_process", side_effect=run_process),
        ):
            result = await transcoding.transcode_video(
                str(self.input_path),
                self.temp_dir.name,
                target_limit_mb=1,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(bitrates), 2)
        self.assertLess(bitrates[1], bitrates[0])

    async def test_missing_ffmpeg_returns_fallback_result(self):
        with patch.object(
            transcoding,
            "ensure_ffmpeg_tools",
            side_effect=FFmpegSetupError("could not prepare FFmpeg"),
        ):
            result = await transcoding.transcode_video(
                str(self.input_path),
                self.temp_dir.name,
                target_limit_mb=1,
            )

        self.assertFalse(result.success)
        self.assertIn("could not prepare", result.error)

    async def test_ffmpeg_error_removes_partial_output(self):
        output_paths = []

        async def run_process(arguments, timeout_seconds):
            output_path = Path(arguments[-1])
            output_paths.append(output_path)
            output_path.write_bytes(b"partial")
            return transcoding.ProcessResult(returncode=1, stderr="codec failed")

        with (
            patch.object(
                transcoding,
                "ensure_ffmpeg_tools",
                return_value=self.ffmpeg_tools(),
            ),
            patch.object(
                transcoding,
                "_probe_duration",
                AsyncMock(return_value=(30.0, "")),
            ),
            patch.object(transcoding, "_run_process", side_effect=run_process),
        ):
            result = await transcoding.transcode_video(
                str(self.input_path),
                self.temp_dir.name,
                target_limit_mb=1,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "codec failed")
        self.assertFalse(output_paths[0].exists())

    async def test_timeout_removes_partial_output(self):
        output_paths = []

        async def run_process(arguments, timeout_seconds):
            output_path = Path(arguments[-1])
            output_paths.append(output_path)
            output_path.write_bytes(b"partial")
            return transcoding.ProcessResult(returncode=-1, timed_out=True)

        with (
            patch.object(
                transcoding,
                "ensure_ffmpeg_tools",
                return_value=self.ffmpeg_tools(),
            ),
            patch.object(
                transcoding,
                "_probe_duration",
                AsyncMock(return_value=(30.0, "")),
            ),
            patch.object(transcoding, "_run_process", side_effect=run_process),
        ):
            result = await transcoding.transcode_video(
                str(self.input_path),
                self.temp_dir.name,
                target_limit_mb=1,
                timeout_seconds=5,
            )

        self.assertFalse(result.success)
        self.assertIn("timed out", result.error)
        self.assertFalse(output_paths[0].exists())

    async def test_spoiler_video_keeps_discord_filename_prefix(self):
        spoiler_input = Path(self.temp_dir.name, "SPOILER_source.mp4")
        self.input_path.replace(spoiler_input)

        async def run_process(arguments, timeout_seconds):
            Path(arguments[-1]).write_bytes(b"converted")
            return transcoding.ProcessResult(returncode=0)

        with (
            patch.object(
                transcoding,
                "ensure_ffmpeg_tools",
                return_value=self.ffmpeg_tools(),
            ),
            patch.object(
                transcoding,
                "_probe_duration",
                AsyncMock(return_value=(30.0, "")),
            ),
            patch.object(transcoding, "_run_process", side_effect=run_process),
        ):
            result = await transcoding.transcode_video(
                str(spoiler_input),
                self.temp_dir.name,
                target_limit_mb=1,
            )

        self.assertTrue(result.success)
        self.assertTrue(Path(result.output_path).name.startswith("SPOILER_"))

    async def test_run_process_terminates_child_on_cancellation(self):
        process = FakeProcess()

        with patch.object(
            transcoding.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            task = asyncio.create_task(
                transcoding._run_process(["ffmpeg"], timeout_seconds=60)
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(process.terminated)
        self.assertEqual(process.returncode, -15)


if __name__ == "__main__":
    unittest.main()
