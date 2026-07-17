from io import BytesIO
import hashlib
import json
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import zipfile

from telerixa_core import ffmpeg_tools


class FFmpegToolsTests(unittest.TestCase):
    def setUp(self):
        ffmpeg_tools.reset_ffmpeg_tools_cache()

    def tearDown(self):
        ffmpeg_tools.reset_ffmpeg_tools_cache()

    def test_pinned_build_metadata_is_immutable(self):
        linux = ffmpeg_tools.MANAGED_BUILDS["linux-x86_64"]
        windows = ffmpeg_tools.MANAGED_BUILDS["windows-x86_64"]

        self.assertIn("autobuild-2026-06-30-13-34", linux.url)
        self.assertNotIn("/latest/", linux.url)
        self.assertEqual(
            linux.sha256,
            "0ba73bbd93472c7622f6dec26d334c5e62e64d858d072490b2844320970456cd",
        )
        self.assertEqual(linux.size, 124756048)
        self.assertEqual(
            windows.sha256,
            "682361e32c9631caec09e5d9f09077101c9ed90c14e275f62014fefa6d397990",
        )
        self.assertEqual(windows.size, 166372072)

    def test_system_tools_have_priority(self):
        with patch.object(
            ffmpeg_tools.shutil,
            "which",
            side_effect=["system-ffmpeg", "system-ffprobe"],
        ):
            result = ffmpeg_tools.find_ffmpeg_tools()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.source, "system")
        self.assertEqual(result.ffmpeg, "system-ffmpeg")

    def test_finds_verified_managed_tools_without_fetching(self):
        build = ffmpeg_tools.get_managed_build()
        with TemporaryDirectory() as temp_dir:
            with (
                patch.object(ffmpeg_tools, "_tools_root", return_value=Path(temp_dir)),
                patch.object(ffmpeg_tools.shutil, "which", return_value=None),
            ):
                directory = ffmpeg_tools._managed_directory(build)
                directory.mkdir(parents=True)
                ffmpeg_path, ffprobe_path = ffmpeg_tools._managed_executable_paths(build)
                ffmpeg_path.touch()
                ffprobe_path.touch()
                ffmpeg_tools._marker_path(build).write_text(
                    json.dumps(
                        {
                            "build_id": ffmpeg_tools.BUILD_ID,
                            "archive": build.archive_name,
                            "archive_sha256": build.sha256,
                        }
                    ),
                    encoding="utf-8",
                )

                result = ffmpeg_tools.find_ffmpeg_tools()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.source, "managed-btbn")

    def test_download_accepts_only_expected_size_and_sha256(self):
        payload = b"verified archive bytes"
        build = ffmpeg_tools.ManagedBuild(
            platform_key="test-x86_64",
            archive_name="test.zip",
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
            archive_type="zip",
        )
        open_url = Mock(return_value=BytesIO(payload))

        with TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir, "download.zip")
            ffmpeg_tools._download_archive(build, archive_path, open_url=open_url)
            self.assertEqual(archive_path.read_bytes(), payload)

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, build.url)

    def test_download_rejects_integrity_mismatches(self):
        payload = b"archive bytes"
        cases = (
            (len(payload) + 1, hashlib.sha256(payload).hexdigest(), "size mismatch"),
            (len(payload) - 1, hashlib.sha256(payload).hexdigest(), "exceeded expected size"),
            (len(payload), "0" * 64, "SHA-256 mismatch"),
        )
        for size, sha256, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                build = ffmpeg_tools.ManagedBuild(
                    platform_key="test-x86_64",
                    archive_name="test.zip",
                    sha256=sha256,
                    size=size,
                    archive_type="zip",
                )
                with TemporaryDirectory() as temp_dir:
                    with self.assertRaisesRegex(
                        ffmpeg_tools.FFmpegSetupError,
                        expected_error,
                    ):
                        ffmpeg_tools._download_archive(
                            build,
                            Path(temp_dir, "download.zip"),
                            open_url=Mock(return_value=BytesIO(payload)),
                        )

    def test_zip_extraction_keeps_only_required_tools(self):
        build = ffmpeg_tools.ManagedBuild(
            "windows-x86_64",
            "test.zip",
            "0" * 64,
            0,
            "zip",
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "tools.zip"
            destination = root / "destination"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("bundle/bin/ffmpeg.exe", b"ffmpeg")
                archive.writestr("bundle/bin/ffprobe.exe", b"ffprobe")
                archive.writestr("bundle/bin/extra.exe", b"extra")
                archive.writestr("../evil.exe", b"evil")

            ffmpeg_tools._extract_tools(build, archive_path, destination)

            self.assertEqual((destination / "ffmpeg.exe").read_bytes(), b"ffmpeg")
            self.assertEqual((destination / "ffprobe.exe").read_bytes(), b"ffprobe")
            self.assertFalse((destination / "extra.exe").exists())
            self.assertFalse((root / "evil.exe").exists())

    def test_tar_extraction_keeps_only_required_tools(self):
        build = ffmpeg_tools.ManagedBuild(
            "linux-x86_64",
            "test.tar.xz",
            "0" * 64,
            0,
            "tar.xz",
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "tools.tar.xz"
            destination = root / "destination"
            with tarfile.open(archive_path, "w:xz") as archive:
                for name, data in (
                    ("bundle/bin/ffmpeg", b"ffmpeg"),
                    ("bundle/bin/ffprobe", b"ffprobe"),
                    ("bundle/bin/extra", b"extra"),
                    ("../evil", b"evil"),
                ):
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, BytesIO(data))

            ffmpeg_tools._extract_tools(build, archive_path, destination)

            self.assertEqual((destination / "ffmpeg").read_bytes(), b"ffmpeg")
            self.assertEqual((destination / "ffprobe").read_bytes(), b"ffprobe")
            self.assertFalse((destination / "extra").exists())
            self.assertFalse((root / "evil").exists())

    def test_ensure_installs_managed_tools_once(self):
        tools = ffmpeg_tools.FFmpegTools("ffmpeg", "ffprobe", "managed-btbn")
        installer = Mock(return_value=tools)
        with (
            patch.object(ffmpeg_tools, "find_ffmpeg_tools", side_effect=[None, None, tools]),
            patch.object(ffmpeg_tools, "_install_managed_tools", installer),
        ):
            first = ffmpeg_tools.ensure_ffmpeg_tools()
            second = ffmpeg_tools.ensure_ffmpeg_tools()

        self.assertEqual(first, tools)
        self.assertEqual(second, tools)
        installer.assert_called_once_with(ffmpeg_tools.get_managed_build())

    def test_failed_install_is_not_repeated_during_cooldown(self):
        installer = Mock(side_effect=OSError("network unavailable"))
        with (
            patch.object(ffmpeg_tools, "find_ffmpeg_tools", return_value=None),
            patch.object(ffmpeg_tools, "_install_managed_tools", installer),
            patch.object(ffmpeg_tools.time, "monotonic", return_value=100.0),
        ):
            with self.assertRaises(ffmpeg_tools.FFmpegSetupError):
                ffmpeg_tools.ensure_ffmpeg_tools()
            with self.assertRaises(ffmpeg_tools.FFmpegSetupError):
                ffmpeg_tools.ensure_ffmpeg_tools()

        installer.assert_called_once_with(ffmpeg_tools.get_managed_build())

    def test_failed_install_can_retry_after_cooldown(self):
        tools = ffmpeg_tools.FFmpegTools("ffmpeg", "ffprobe", "managed-btbn")
        installer = Mock(side_effect=[OSError("network unavailable"), tools])
        with (
            patch.object(ffmpeg_tools, "find_ffmpeg_tools", return_value=None),
            patch.object(ffmpeg_tools, "_install_managed_tools", installer),
            patch.object(
                ffmpeg_tools.time,
                "monotonic",
                side_effect=[100.0, 401.0, 401.0],
            ),
        ):
            with self.assertRaises(ffmpeg_tools.FFmpegSetupError):
                ffmpeg_tools.ensure_ffmpeg_tools()
            result = ffmpeg_tools.ensure_ffmpeg_tools()

        self.assertEqual(result, tools)
        self.assertEqual(installer.call_count, 2)


if __name__ == "__main__":
    unittest.main()
