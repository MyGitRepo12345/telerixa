import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from i18n import configure_language
from telerixa_core import media_delivery
from telerixa_core.models import SendResult


class FakeClientSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


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


if __name__ == "__main__":
    unittest.main()
