import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from i18n import configure_language
from telethon.tl import types
from telerixa_core import media_delivery
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


if __name__ == "__main__":
    unittest.main()
