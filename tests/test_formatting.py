import unittest

from i18n import configure_language
from telethon.tl.types import MessageEntityBold, MessageEntitySpoiler
from telerixa_core import formatting


class FakeReplyTo:
    def __init__(
        self,
        quote_text=None,
        quote_entities=None,
        reply_to_peer_id=None,
        reply_to_msg_id=None,
    ):
        self.quote_text = quote_text
        self.quote_entities = quote_entities
        self.reply_to_peer_id = reply_to_peer_id
        self.reply_to_msg_id = reply_to_msg_id


class FakeChat:
    def __init__(self, title=None, username=None):
        self.title = title
        self.username = username


class FakeMessage:
    def __init__(
        self,
        message_id,
        text="",
        reply_to=None,
        reply_message=None,
        chat=None,
        forward=None,
        entities=None,
        rich_message=None,
    ):
        self.id = message_id
        self.text = text
        self.reply_to = reply_to
        self._reply_message = reply_message
        self.chat = chat
        self.forward = forward
        self.entities = entities
        self.rich_message = rich_message

    async def get_reply_message(self):
        return self._reply_message


class FakeTelegramClient:
    def __init__(self, messages):
        self.messages = messages

    async def get_messages(self, peer_id, ids):
        return self.messages.get((peer_id, ids))


class FormattingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        configure_language("en")

    def test_split_text_keeps_words_when_possible(self):
        self.assertEqual(
            formatting.split_text("alpha beta gamma", 10),
            ["alpha beta", "gamma"],
        )

    def test_split_text_splits_long_words(self):
        self.assertEqual(
            formatting.split_text("abcdefghij", 4),
            ["abcd", "efgh", "ij"],
        )

    def test_split_text_keeps_long_spoilers_balanced(self):
        text = "intro ||" + ("hidden " * 20) + "ending|| outro"

        chunks = formatting.split_text(text, 40)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 40 for chunk in chunks))
        self.assertTrue(all(chunk.count("||") % 2 == 0 for chunk in chunks))
        self.assertTrue(any("ending" in chunk for chunk in chunks))

    def test_select_album_text_message_prefers_longest_caption(self):
        fallback = FakeMessage(1, "fallback")
        chosen = formatting.select_album_text_message(
            [
                FakeMessage(2, ""),
                FakeMessage(3, "short"),
                FakeMessage(4, "longer caption"),
            ],
            fallback,
        )
        self.assertEqual(chosen.id, 4)

    def test_select_album_text_message_falls_back_without_caption(self):
        fallback = FakeMessage(1, "fallback")
        self.assertIs(
            formatting.select_album_text_message([FakeMessage(2, "")], fallback),
            fallback,
        )

    async def test_build_message_text_includes_reply_quote(self):
        message = FakeMessage(
            123,
            text="main text",
            reply_to=FakeReplyTo(quote_text="quoted line"),
        )

        text = await formatting.build_message_text(message, "demo_channel")

        self.assertIn("https://t.me/demo_channel/123", text)
        self.assertIn("Reply to another Telegram post", text)
        self.assertIn("> quoted line", text)
        self.assertIn("main text", text)

    async def test_build_message_text_fetches_cross_reply_with_client(self):
        reply_message = FakeMessage(
            99,
            text="reply body",
            chat=FakeChat(title="Source Channel", username="source_channel"),
        )
        telegram_client = FakeTelegramClient({("peer", 99): reply_message})
        message = FakeMessage(
            123,
            text="main text",
            reply_to=FakeReplyTo(reply_to_peer_id="peer", reply_to_msg_id=99),
        )

        text = await formatting.build_message_text(
            message,
            "demo_channel",
            telegram_client=telegram_client,
        )

        self.assertIn("Reply to [Source Channel](https://t.me/source_channel/99)", text)
        self.assertIn("> reply body", text)

    async def test_long_reply_context_is_preserved_and_chunked(self):
        tail_marker = "END-OF-REPLY-CONTEXT"
        reply_body = ("Long forwarded reply paragraph. " * 80) + tail_marker
        reply_message = FakeMessage(
            99,
            text=reply_body,
            chat=FakeChat(title="Source Channel", username="source_channel"),
        )
        message = FakeMessage(
            123,
            text="main text",
            reply_to=FakeReplyTo(),
            reply_message=reply_message,
        )

        text = await formatting.build_message_text(message, "demo_channel")
        chunks = formatting.split_text(text, 500)

        self.assertIn(tail_marker, text)
        self.assertNotIn("END-OF-REPLY-CON...", text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(any(tail_marker in chunk for chunk in chunks))

    def test_repair_mojibake_leaves_normal_text_unchanged(self):
        self.assertEqual(formatting.repair_mojibake("normal text"), "normal text")

    def test_format_spoilers_uses_telegram_utf16_offsets(self):
        text = "A😀secret Z"
        entities = [MessageEntitySpoiler(offset=3, length=6)]

        self.assertEqual(
            formatting.format_spoilers(text, entities),
            "A😀||secret|| Z",
        )

    def test_format_spoilers_ignores_other_entities_and_merges_ranges(self):
        text = "one two three"
        entities = [
            MessageEntityBold(offset=0, length=3),
            MessageEntitySpoiler(offset=4, length=3),
            MessageEntitySpoiler(offset=7, length=6),
        ]

        self.assertEqual(
            formatting.format_spoilers(text, entities),
            "one ||two three||",
        )

    async def test_build_message_text_preserves_post_and_reply_spoilers(self):
        reply_message = FakeMessage(
            99,
            text="reply hidden",
            entities=[MessageEntitySpoiler(offset=6, length=6)],
        )
        message = FakeMessage(
            123,
            text="main secret",
            entities=[MessageEntitySpoiler(offset=5, length=6)],
            reply_to=FakeReplyTo(),
            reply_message=reply_message,
        )

        text = await formatting.build_message_text(message, "demo_channel")

        self.assertIn("> reply ||hidden||", text)
        self.assertIn("main ||secret||", text)

    async def test_build_message_text_preserves_quote_spoilers(self):
        message = FakeMessage(
            123,
            text="main text",
            reply_to=FakeReplyTo(
                quote_text="quote secret",
                quote_entities=[MessageEntitySpoiler(offset=6, length=6)],
            ),
        )

        text = await formatting.build_message_text(message, "demo_channel")

        self.assertIn("> quote ||secret||", text)

    async def test_build_message_text_uses_native_rich_content_without_fallback_text(self):
        from telethon.tl import types

        message = FakeMessage(
            321,
            rich_message=types.RichMessage(
                blocks=[
                    types.PageBlockHeading1(types.TextPlain("Native heading")),
                    types.PageBlockParagraph(
                        types.TextSpoiler(types.TextPlain("native secret"))
                    ),
                ],
                photos=[],
                documents=[],
            ),
        )

        text = await formatting.build_message_text(message, "rich_channel")

        self.assertIn("https://t.me/rich_channel/321", text)
        self.assertIn("# Native heading", text)
        self.assertIn("||native secret||", text)


if __name__ == "__main__":
    unittest.main()
