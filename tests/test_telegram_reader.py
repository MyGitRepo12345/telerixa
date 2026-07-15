import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from i18n import configure_language
from telerixa_core import telegram_reader


class FakeMessage:
    def __init__(
        self,
        message_id,
        text="",
        media=None,
        grouped_id=None,
        date=None,
    ):
        self.id = message_id
        self.text = text
        self.media = media
        self.grouped_id = grouped_id
        self.date = date or datetime(2026, 7, 12, tzinfo=timezone.utc)


class FakeTelegramClient:
    def __init__(self, messages=None, iter_messages=None, error=None):
        self.messages = messages or []
        self.iter_message_items = iter_messages or []
        self.error = error
        self.get_messages_calls = []
        self.iter_messages_calls = []

    async def get_messages(self, entity, **kwargs):
        self.get_messages_calls.append((entity, kwargs))
        if self.error:
            raise self.error
        return self.messages

    def iter_messages(self, entity, **kwargs):
        self.iter_messages_calls.append((entity, kwargs))

        async def iterator():
            for message in self.iter_message_items:
                yield message

        return iterator()


class ConcurrentTelegramClient:
    def __init__(self):
        self.active_requests = 0
        self.max_active_requests = 0

    async def get_entity(self, channel):
        self.active_requests += 1
        self.max_active_requests = max(
            self.max_active_requests,
            self.active_requests,
        )
        await asyncio.sleep(0.02)
        self.active_requests -= 1
        return channel

    async def get_messages(self, entity, **kwargs):
        channel_index = int(entity.rsplit("-", 1)[-1])
        return [FakeMessage(channel_index + 1)]


class TelegramReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        configure_language("en")

    def test_forwardable_message_requires_text_or_media(self):
        self.assertFalse(telegram_reader.is_forwardable_message(None))
        self.assertFalse(telegram_reader.is_forwardable_message(FakeMessage(1)))
        self.assertTrue(
            telegram_reader.is_forwardable_message(FakeMessage(2, text="post"))
        )
        self.assertTrue(
            telegram_reader.is_forwardable_message(FakeMessage(3, media=object()))
        )

    def test_message_datetime_uses_configured_timezone(self):
        message = FakeMessage(1)
        local_timezone = timezone(timedelta(hours=2))

        converted = telegram_reader.get_message_datetime(message, local_timezone)

        self.assertEqual(converted.utcoffset(), timedelta(hours=2))
        self.assertEqual(converted.hour, 2)

    async def test_latest_message_id_handles_empty_and_nonempty_channels(self):
        client = FakeTelegramClient([FakeMessage(42)])
        self.assertEqual(
            await telegram_reader.get_latest_message_id(client, "entity"),
            42,
        )

        empty_client = FakeTelegramClient()
        self.assertEqual(
            await telegram_reader.get_latest_message_id(empty_client, "entity"),
            0,
        )

    async def test_startup_tail_deduplicates_albums_and_detects_older_posts(self):
        client = FakeTelegramClient(
            iter_messages=[
                FakeMessage(10, media=object(), grouped_id=100),
                FakeMessage(9, media=object(), grouped_id=100),
                FakeMessage(8, text="second post"),
                FakeMessage(7),
                FakeMessage(6, text="older post"),
            ]
        )

        messages, has_older = await telegram_reader.collect_startup_tail(
            client,
            "entity",
            last_seen_id=1,
            limit=2,
        )

        self.assertEqual([message.id for message in messages], [8, 10])
        self.assertTrue(has_older)
        self.assertEqual(client.iter_messages_calls, [("entity", {"min_id": 1})])

    async def test_album_lookup_filters_sorts_and_caches_messages(self):
        anchor = FakeMessage(100, media=object(), grouped_id=500)
        client = FakeTelegramClient(
            [
                FakeMessage(102, media=object(), grouped_id=999),
                None,
                FakeMessage(101, media=object(), grouped_id=500),
                FakeMessage(99, media=object(), grouped_id=500),
            ]
        )

        first_result = await telegram_reader.get_album_messages(
            client,
            "demo",
            anchor,
        )
        second_result = await telegram_reader.get_album_messages(
            client,
            "demo",
            anchor,
        )

        self.assertEqual([message.id for message in first_result], [99, 101])
        self.assertIs(second_result, first_result)
        self.assertEqual(len(client.get_messages_calls), 1)
        entity, kwargs = client.get_messages_calls[0]
        self.assertEqual(entity, "@demo")
        self.assertEqual(kwargs["ids"][0], 80)
        self.assertEqual(kwargs["ids"][-1], 120)

    async def test_album_lookup_failure_falls_back_and_caches_anchor(self):
        anchor = FakeMessage(5, media=object(), grouped_id=500)
        client = FakeTelegramClient(error=ConnectionError("offline"))

        first_result = await telegram_reader.get_album_messages(
            client,
            "demo",
            anchor,
        )
        second_result = await telegram_reader.get_album_messages(
            client,
            "demo",
            anchor,
        )

        self.assertEqual(first_result, [anchor])
        self.assertIs(second_result, first_result)
        self.assertEqual(len(client.get_messages_calls), 1)

    async def test_non_album_message_does_not_query_telegram(self):
        message = FakeMessage(1, text="single post")
        client = FakeTelegramClient()

        result = await telegram_reader.get_album_messages(client, "demo", message)

        self.assertEqual(result, [message])
        self.assertEqual(client.get_messages_calls, [])

    async def test_channel_collection_runs_concurrently(self):
        client = ConcurrentTelegramClient()
        channels = ["channel-0", "channel-1", "channel-2"]

        results = await telegram_reader.collect_channels(
            client,
            channels,
            {channel: None for channel in channels},
            concurrency_limit=3,
        )

        self.assertEqual([channel for channel, _result in results], channels)
        self.assertTrue(
            all(
                isinstance(result, telegram_reader.ChannelCollection)
                for _channel, result in results
            )
        )
        self.assertEqual(client.max_active_requests, 3)

    def test_merge_posts_is_global_chronological_and_deduplicates_album(self):
        base_time = datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)
        channel_one = telegram_reader.ChannelCollection(
            channel="one",
            channel_order=0,
            last_seen_id=0,
            latest_message_id=3,
            messages=[
                FakeMessage(1, text="10:00", date=base_time),
                FakeMessage(
                    2,
                    media=object(),
                    grouped_id=500,
                    date=base_time + timedelta(minutes=2),
                ),
                FakeMessage(
                    3,
                    media=object(),
                    grouped_id=500,
                    date=base_time + timedelta(minutes=2, seconds=1),
                ),
            ],
        )
        channel_two = telegram_reader.ChannelCollection(
            channel="two",
            channel_order=1,
            last_seen_id=0,
            latest_message_id=8,
            messages=[
                FakeMessage(
                    8,
                    text="10:01",
                    date=base_time + timedelta(minutes=1),
                )
            ],
        )

        posts = telegram_reader.merge_chronological_posts(
            [channel_one, channel_two]
        )

        self.assertEqual(
            [(post.channel, post.message.id) for post in posts],
            [("one", 1), ("two", 8), ("one", 2)],
        )


if __name__ == "__main__":
    unittest.main()
