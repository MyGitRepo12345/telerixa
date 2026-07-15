import unittest
from datetime import datetime, timezone

from i18n import configure_language
from telerixa_core import discord_delivery


class FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(self.statuses.pop(0))


class DiscordDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        configure_language("en")

    def test_get_alert_mention(self):
        self.assertEqual(discord_delivery.get_alert_mention("123"), "<@123> ")
        self.assertEqual(discord_delivery.get_alert_mention(""), "")

    def test_describe_network_error_recognizes_dns_failure(self):
        description = discord_delivery.describe_network_error(
            OSError("getaddrinfo failed")
        )

        self.assertIn("getaddrinfo failed", description)

    async def test_post_json_classifies_terminal_and_transient_errors(self):
        terminal_session = FakeSession([413])
        terminal_result = await discord_delivery.post_json(
            terminal_session,
            "https://example.invalid/webhook",
            {"content": "test"},
        )

        retry_session = FakeSession([500])
        retry_result = await discord_delivery.post_json(
            retry_session,
            "https://example.invalid/webhook",
            {"content": "test"},
        )

        self.assertFalse(terminal_result)
        self.assertTrue(terminal_result.terminal)
        self.assertTrue(terminal_result.count_attempt)
        self.assertFalse(retry_result)
        self.assertFalse(retry_result.terminal)
        self.assertFalse(retry_result.count_attempt)

    async def test_post_json_treats_missing_webhook_as_terminal(self):
        session = FakeSession([404])

        result = await discord_delivery.post_json(
            session,
            "https://example.invalid/webhook",
            {"content": "test"},
        )

        self.assertFalse(result)
        self.assertTrue(result.terminal)

    def test_network_exception_does_not_consume_queue_attempt(self):
        result = discord_delivery.retry_result_for_exception(
            ConnectionResetError("connection reset by peer")
        )

        self.assertFalse(result)
        self.assertFalse(result.terminal)
        self.assertFalse(result.count_attempt)

    def test_unknown_exception_uses_bounded_retry(self):
        result = discord_delivery.retry_result_for_exception(
            ValueError("unexpected payload")
        )

        self.assertFalse(result)
        self.assertFalse(result.terminal)
        self.assertTrue(result.count_attempt)

    async def test_send_text_chunks_posts_every_chunk(self):
        session = FakeSession([204, 204])

        result = await discord_delivery.send_text_chunks(
            session,
            "https://example.invalid/webhook",
            ["first", "second"],
            "demo_channel",
            datetime(2026, 7, 10, tzinfo=timezone.utc),
        )

        self.assertTrue(result)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            session.calls[0]["json"]["embeds"][0]["description"],
            "first",
        )
        self.assertIn(
            "(1/2)",
            session.calls[0]["json"]["embeds"][0]["footer"]["text"],
        )

    async def test_send_text_chunks_reports_only_confirmed_progress(self):
        session = FakeSession([204, 500])
        progress = []

        result = await discord_delivery.send_text_chunks(
            session,
            "https://example.invalid/webhook",
            ["first", "second"],
            "demo_channel",
            datetime(2026, 7, 10, tzinfo=timezone.utc),
            progress_callback=lambda **values: progress.append(values),
        )

        self.assertFalse(result)
        self.assertEqual(
            progress,
            [{"next_chunk_index": 1, "media_sent": False}],
        )

        retry_session = FakeSession([204])
        retry_result = await discord_delivery.send_text_chunks(
            retry_session,
            "https://example.invalid/webhook",
            ["second"],
            "demo_channel",
            datetime(2026, 7, 10, tzinfo=timezone.utc),
            start_part=2,
            total_parts=2,
            progress_callback=lambda **values: progress.append(values),
        )

        self.assertTrue(retry_result)
        self.assertEqual(
            retry_session.calls[0]["json"]["embeds"][0]["description"],
            "second",
        )
        self.assertEqual(progress[-1]["next_chunk_index"], 2)


if __name__ == "__main__":
    unittest.main()
