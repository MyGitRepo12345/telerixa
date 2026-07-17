import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from i18n import configure_language
from telethon.tl import types
from telerixa_core import media_delivery, transcoding
from telerixa_core.models import SendResult


class FakeClientSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeResponse:
    def __init__(self, status=204):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class RecordingClientSession(FakeClientSession):
    def __init__(self, status=204):
        self.status = status
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        return FakeResponse(self.status)


class FakeTelegramClient:
    def __init__(self):
        self.download_media = AsyncMock()


class FakeMessage:
    id = 10
    grouped_id = None
    media = object()
    text = "unused live text"
    date = datetime(2026, 7, 12, tzinfo=timezone.utc)


class MediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        configure_language("en")
        self.settings = media_delivery.MediaDeliverySettings(
            webhook_url="https://example.invalid/webhook",
            max_message_length=3,
            file_limit_mb=25,
            large_file_action="send_text_link",
        )

    async def test_media_resume_skips_download_and_sends_remaining_chunk(self):
        telegram_client = FakeTelegramClient()
        send_chunks = AsyncMock(return_value=SendResult.success())

        with (
            patch.object(
                media_delivery.aiohttp,
                "ClientSession",
                return_value=FakeClientSession(),
            ),
            patch.object(media_delivery, "send_text_chunks", send_chunks),
        ):
            result = await media_delivery.send_to_discord(
                telegram_client,
                FakeMessage(),
                "demo",
                self.settings,
                get_album_messages=AsyncMock(),
                get_message_datetime=lambda message: message.date,
                next_chunk_index=1,
                media_sent=True,
                rendered_text="one two",
            )

        self.assertTrue(result)
        telegram_client.download_media.assert_not_awaited()
        send_chunks.assert_awaited_once()
        call = send_chunks.await_args
        assert call is not None
        self.assertEqual(call.args[2], ["two"])
        self.assertEqual(call.kwargs["start_part"], 2)
        self.assertEqual(call.kwargs["total_parts"], 2)
        self.assertTrue(call.kwargs["media_sent"])

    async def test_completed_cursor_returns_success_without_network(self):
        telegram_client = FakeTelegramClient()

        with patch.object(media_delivery.aiohttp, "ClientSession") as session:
            result = await media_delivery.send_to_discord(
                telegram_client,
                FakeMessage(),
                "demo",
                self.settings,
                get_album_messages=AsyncMock(),
                get_message_datetime=lambda message: message.date,
                next_chunk_index=2,
                media_sent=True,
                rendered_text="one two",
            )

        self.assertTrue(result)
        telegram_client.download_media.assert_not_awaited()
        session.assert_not_called()

    async def test_native_rich_media_uses_existing_multipart_delivery(self):
        photo = types.Photo(
            id=100,
            access_hash=1,
            file_reference=b"reference",
            date=None,
            sizes=[],
            dc_id=1,
        )
        rich_message = types.RichMessage(
            blocks=[
                types.PageBlockHeading1(types.TextPlain("Native rich post")),
                types.PageBlockPhoto(
                    100,
                    types.PageCaption(types.TextEmpty(), types.TextEmpty()),
                    spoiler=True,
                ),
            ],
            photos=[photo],
            documents=[],
        )
        message = SimpleNamespace(
            id=10,
            grouped_id=None,
            media=None,
            text="",
            entities=None,
            reply_to=None,
            forward=None,
            rich_message=rich_message,
            date=datetime(2026, 7, 12, tzinfo=timezone.utc),
        )
        telegram_client = FakeTelegramClient()

        async def download_media(media, directory):
            path = Path(directory, "native.jpg")
            path.write_bytes(b"rich media")
            return str(path)

        telegram_client.download_media.side_effect = download_media
        session = RecordingClientSession()
        progress = []
        settings = media_delivery.MediaDeliverySettings(
            webhook_url="https://example.invalid/webhook",
            max_message_length=2000,
            file_limit_mb=25,
            large_file_action="send_text_link",
        )

        with patch.object(
            media_delivery.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            result = await media_delivery.send_to_discord(
                telegram_client,
                message,
                "rich_channel",
                settings,
                get_album_messages=AsyncMock(),
                get_message_datetime=lambda item: item.date,
                progress_callback=lambda **values: progress.append(values),
            )

        self.assertTrue(result)
        telegram_client.download_media.assert_awaited_once()
        download_call = telegram_client.download_media.await_args
        assert download_call is not None
        self.assertIs(download_call.args[0], photo)
        self.assertEqual(len(session.calls), 1)
        self.assertIsNotNone(session.calls[0]["data"])
        self.assertTrue(progress[-1]["media_sent"])
        self.assertEqual(progress[-1]["next_chunk_index"], 1)
        self.assertIn("# Native rich post", progress[0]["rendered_text"])

    async def test_non_file_media_sends_text_without_download_retry(self):
        telegram_client = FakeTelegramClient()
        send_chunks = AsyncMock(return_value=SendResult.success())
        settings = media_delivery.MediaDeliverySettings(
            webhook_url="https://example.invalid/webhook",
            max_message_length=2000,
            file_limit_mb=25,
            large_file_action="send_text_link",
        )
        message = SimpleNamespace(
            id=118891,
            grouped_id=None,
            media=types.MessageMediaWebPage(types.WebPageEmpty(1)),
            text="A post with a link preview",
            entities=None,
            reply_to=None,
            forward=None,
            rich_message=None,
            date=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )

        with (
            patch.object(
                media_delivery.aiohttp,
                "ClientSession",
                return_value=FakeClientSession(),
            ),
            patch.object(media_delivery, "send_text_chunks", send_chunks),
        ):
            result = await media_delivery.send_to_discord(
                telegram_client,
                message,
                "astrapress",
                settings,
                get_album_messages=AsyncMock(),
                get_message_datetime=lambda item: item.date,
            )

        self.assertTrue(result)
        telegram_client.download_media.assert_not_awaited()
        send_chunks.assert_awaited_once()
        call = send_chunks.await_args
        assert call is not None
        self.assertIn("A post with a link preview", call.args[2][0])

    def test_spoiler_media_gets_discord_spoiler_filename(self):
        with TemporaryDirectory() as directory:
            source_path = Path(directory, "photo.jpg")
            source_path.write_bytes(b"image")

            spoiler_path = Path(
                media_delivery._apply_spoiler_filename(str(source_path))
            )

            self.assertEqual(spoiler_path.name, "SPOILER_photo.jpg")
            self.assertTrue(spoiler_path.exists())
            self.assertFalse(source_path.exists())

    async def test_oversized_video_is_transcoded_and_uploaded(self):
        telegram_client = FakeTelegramClient()
        created_paths = []

        async def download_media(media, directory):
            source_path = Path(directory, "source.mp4")
            source_path.write_bytes(b"x" * (1024 * 1024 + 1))
            created_paths.append(source_path)
            return str(source_path)

        async def transcode_video(
            input_path,
            output_dir,
            target_limit_mb,
            preset,
            timeout_seconds,
        ):
            output_path = Path(output_dir, "converted.mp4")
            output_path.write_bytes(b"converted")
            created_paths.append(output_path)
            return transcoding.TranscodeResult(
                success=True,
                output_path=str(output_path),
                source_size=1024 * 1024 + 1,
                output_size=len(b"converted"),
                duration_seconds=12.5,
                attempts=1,
            )

        telegram_client.download_media.side_effect = download_media
        settings = media_delivery.MediaDeliverySettings(
            webhook_url="https://example.invalid/webhook",
            max_message_length=2000,
            file_limit_mb=1,
            large_file_action="compress_then_text",
            transcode_preset="fast",
            transcode_timeout_seconds=300,
        )
        session = RecordingClientSession()
        attachment = SimpleNamespace(media=object(), spoiler=False)

        with (
            patch.object(
                media_delivery,
                "_collect_media_attachments",
                return_value=[attachment],
            ),
            patch.object(
                media_delivery,
                "transcode_video",
                side_effect=transcode_video,
            ) as transcode_mock,
            patch.object(
                media_delivery.aiohttp,
                "ClientSession",
                return_value=session,
            ),
        ):
            result = await media_delivery.send_to_discord(
                telegram_client,
                FakeMessage(),
                "demo",
                settings,
                get_album_messages=AsyncMock(),
                get_message_datetime=lambda message: message.date,
                rendered_text="Telegram post: https://t.me/demo/10",
            )

        self.assertTrue(result)
        transcode_mock.assert_awaited_once()
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(all(not path.exists() for path in created_paths))

    async def test_transcode_failure_sends_text_link_without_queue_retry(self):
        telegram_client = FakeTelegramClient()

        async def download_media(media, directory):
            source_path = Path(directory, "source.mp4")
            source_path.write_bytes(b"x" * (1024 * 1024 + 1))
            return str(source_path)

        telegram_client.download_media.side_effect = download_media
        settings = media_delivery.MediaDeliverySettings(
            webhook_url="https://example.invalid/webhook",
            max_message_length=2000,
            file_limit_mb=1,
            large_file_action="compress_then_text",
        )
        send_chunks = AsyncMock(return_value=SendResult.success())
        attachment = SimpleNamespace(media=object(), spoiler=False)

        with (
            patch.object(
                media_delivery,
                "_collect_media_attachments",
                return_value=[attachment],
            ),
            patch.object(
                media_delivery,
                "transcode_video",
                AsyncMock(
                    return_value=transcoding.TranscodeResult(
                        success=False,
                        error="ffmpeg missing",
                    )
                ),
            ),
            patch.object(media_delivery, "send_text_chunks", send_chunks),
            patch.object(
                media_delivery.aiohttp,
                "ClientSession",
                return_value=FakeClientSession(),
            ),
        ):
            result = await media_delivery.send_to_discord(
                telegram_client,
                FakeMessage(),
                "demo",
                settings,
                get_album_messages=AsyncMock(),
                get_message_datetime=lambda message: message.date,
                rendered_text="Telegram post: https://t.me/demo/10",
            )

        self.assertTrue(result)
        send_chunks.assert_awaited_once()

    async def test_discord_failure_after_transcode_remains_transient_retry(self):
        telegram_client = FakeTelegramClient()

        async def download_media(media, directory):
            source_path = Path(directory, "source.mp4")
            source_path.write_bytes(b"x" * (1024 * 1024 + 1))
            return str(source_path)

        async def transcode_video(
            input_path,
            output_dir,
            target_limit_mb,
            preset,
            timeout_seconds,
        ):
            output_path = Path(output_dir, "converted.mp4")
            output_path.write_bytes(b"converted")
            return transcoding.TranscodeResult(
                success=True,
                output_path=str(output_path),
                source_size=1024 * 1024 + 1,
                output_size=len(b"converted"),
                duration_seconds=12.5,
                attempts=1,
            )

        telegram_client.download_media.side_effect = download_media
        settings = media_delivery.MediaDeliverySettings(
            webhook_url="https://example.invalid/webhook",
            max_message_length=2000,
            file_limit_mb=1,
            large_file_action="compress_then_text",
        )
        session = RecordingClientSession(status=503)
        attachment = SimpleNamespace(media=object(), spoiler=False)

        with (
            patch.object(
                media_delivery,
                "_collect_media_attachments",
                return_value=[attachment],
            ),
            patch.object(
                media_delivery,
                "transcode_video",
                side_effect=transcode_video,
            ),
            patch.object(
                media_delivery.aiohttp,
                "ClientSession",
                return_value=session,
            ),
        ):
            result = await media_delivery.send_to_discord(
                telegram_client,
                FakeMessage(),
                "demo",
                settings,
                get_album_messages=AsyncMock(),
                get_message_datetime=lambda message: message.date,
                rendered_text="Telegram post: https://t.me/demo/10",
            )

        self.assertFalse(result)
        self.assertFalse(result.terminal)
        self.assertFalse(result.count_attempt)
        self.assertIn("503", result.error)

    async def test_only_oversized_video_is_transcoded_in_mixed_media(self):
        telegram_client = FakeTelegramClient()
        download_count = 0

        async def download_media(media, directory):
            nonlocal download_count
            download_count += 1
            if download_count == 1:
                path = Path(directory, "photo.jpg")
                path.write_bytes(b"photo")
            else:
                path = Path(directory, "video.mp4")
                path.write_bytes(b"x" * (1024 * 1024 + 1))
            return str(path)

        async def transcode_video(
            input_path,
            output_dir,
            target_limit_mb,
            preset,
            timeout_seconds,
        ):
            output_path = Path(output_dir, "converted.mp4")
            output_path.write_bytes(b"converted")
            return transcoding.TranscodeResult(
                success=True,
                output_path=str(output_path),
                source_size=1024 * 1024 + 1,
                output_size=len(b"converted"),
                duration_seconds=10,
                attempts=1,
            )

        telegram_client.download_media.side_effect = download_media
        settings = media_delivery.MediaDeliverySettings(
            webhook_url="https://example.invalid/webhook",
            max_message_length=2000,
            file_limit_mb=1,
            large_file_action="compress_then_text",
        )
        session = RecordingClientSession()
        attachments = [
            SimpleNamespace(media=object(), spoiler=False),
            SimpleNamespace(media=object(), spoiler=False),
        ]

        with (
            patch.object(
                media_delivery,
                "_collect_media_attachments",
                return_value=attachments,
            ),
            patch.object(
                media_delivery,
                "transcode_video",
                side_effect=transcode_video,
            ) as transcode_mock,
            patch.object(
                media_delivery.aiohttp,
                "ClientSession",
                return_value=session,
            ),
        ):
            result = await media_delivery.send_to_discord(
                telegram_client,
                FakeMessage(),
                "demo",
                settings,
                get_album_messages=AsyncMock(),
                get_message_datetime=lambda message: message.date,
                rendered_text="Telegram post: https://t.me/demo/10",
            )

        self.assertTrue(result)
        self.assertEqual(telegram_client.download_media.await_count, 2)
        transcode_mock.assert_awaited_once()
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
