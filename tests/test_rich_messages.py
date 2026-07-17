import unittest

from i18n import configure_language
from telethon.tl import types
from telerixa_core import rich_messages


def empty_caption():
    return types.PageCaption(types.TextEmpty(), types.TextEmpty())


def photo(media_id):
    return types.Photo(
        id=media_id,
        access_hash=1,
        file_reference=b"reference",
        date=None,
        sizes=[],
        dc_id=1,
    )


def document(media_id):
    return types.Document(
        id=media_id,
        access_hash=1,
        file_reference=b"reference",
        date=None,
        mime_type="application/octet-stream",
        size=1,
        dc_id=1,
        attributes=[],
    )


class FakeMessage:
    def __init__(self, media=None, rich_message=None):
        self.media = media
        self.rich_message = rich_message


class RichMessageTests(unittest.TestCase):
    def setUp(self):
        configure_language("en")

    def test_renderer_preserves_supported_structure_and_inline_styles(self):
        rich_message = types.RichMessage(
            blocks=[
                types.PageBlockHeading1(types.TextPlain("Release status")),
                types.PageBlockParagraph(
                    types.TextConcat(
                        [
                            types.TextBold(types.TextPlain("Ready")),
                            types.TextPlain(" with "),
                            types.TextSpoiler(types.TextPlain("one caveat")),
                            types.TextPlain(" and "),
                            types.TextMath("x^2"),
                        ]
                    )
                ),
                types.PageBlockBlockquoteBlocks(
                    blocks=[
                        types.PageBlockParagraph(types.TextPlain("first line")),
                        types.PageBlockParagraph(types.TextPlain("second line")),
                    ],
                    caption=types.TextPlain("source"),
                ),
                types.PageBlockList(
                    [
                        types.PageListItemText(
                            types.TextPlain("verified"),
                            checkbox=True,
                            checked=True,
                        )
                    ]
                ),
                types.PageBlockDetails(
                    blocks=[
                        types.PageBlockParagraph(types.TextPlain("full context"))
                    ],
                    title=types.TextPlain("Read more"),
                ),
                types.PageBlockTable(
                    title=types.TextPlain("Matrix"),
                    rows=[
                        types.PageTableRow(
                            [
                                types.PageTableCell(
                                    header=True,
                                    text=types.TextPlain("Type"),
                                ),
                                types.PageTableCell(
                                    header=True,
                                    text=types.TextPlain("Status"),
                                ),
                            ]
                        ),
                        types.PageTableRow(
                            [
                                types.PageTableCell(text=types.TextPlain("Rich")),
                                types.PageTableCell(text=types.TextPlain("Yes")),
                            ]
                        ),
                    ],
                    bordered=True,
                    striped=True,
                ),
                types.PageBlockMath(r"E = mc^2"),
            ],
            photos=[],
            documents=[],
        )

        rendered = rich_messages.render_rich_message(rich_message)

        self.assertIn("# Release status", rendered)
        self.assertIn("**Ready**", rendered)
        self.assertIn("||one caveat||", rendered)
        self.assertIn("`x^2`", rendered)
        self.assertIn("> first line\n> second line\n> - source", rendered)
        self.assertNotIn("\n>\n", rendered)
        self.assertIn("- [x] verified", rendered)
        self.assertIn("**Details: Read more**", rendered)
        self.assertIn("```text\nType | Status", rendered)
        self.assertIn("```latex\nE = mc^2\n```", rendered)

    def test_renderer_marks_unknown_blocks_instead_of_dropping_them(self):
        rendered = rich_messages.render_block({"_": "PageBlockFutureFeature"})

        self.assertIn("Unsupported Telegram rich block", rendered)
        self.assertIn("PageBlockFutureFeature", rendered)

    def test_embedded_media_follow_block_order_and_preserve_spoilers(self):
        first_photo = photo(1)
        collage_photo = photo(4)
        video = document(2)
        audio = document(3)
        rich_message = types.RichMessage(
            blocks=[
                types.PageBlockPhoto(
                    1,
                    empty_caption(),
                    spoiler=True,
                ),
                types.PageBlockVideo(
                    2,
                    empty_caption(),
                    spoiler=True,
                ),
                types.PageBlockAudio(3, empty_caption()),
                types.PageBlockCollage(
                    [types.PageBlockPhoto(4, empty_caption())],
                    empty_caption(),
                ),
            ],
            photos=[collage_photo, first_photo],
            documents=[audio, video],
        )

        attachments = rich_messages.get_rich_media_attachments(rich_message)

        self.assertEqual(
            [attachment.media.id for attachment in attachments],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            [attachment.spoiler for attachment in attachments],
            [True, True, False, False],
        )

    def test_regular_and_rich_media_are_deduplicated(self):
        regular_photo = photo(10)
        duplicate_photo = photo(10)
        rich_message = types.RichMessage(
            blocks=[types.PageBlockPhoto(10, empty_caption())],
            photos=[duplicate_photo],
            documents=[],
        )

        attachments = rich_messages.get_message_media_attachments(
            FakeMessage(media=regular_photo, rich_message=rich_message)
        )

        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].media.id, 10)

    def test_non_file_message_media_is_not_treated_as_attachment(self):
        webpage = types.MessageMediaWebPage(types.WebPageEmpty(1))
        giveaway = types.MessageMediaGiveaway(
            channels=[1],
            quantity=1,
            until_date=None,
        )

        self.assertEqual(
            rich_messages.get_message_media_attachments(FakeMessage(media=webpage)),
            [],
        )
        self.assertEqual(
            rich_messages.get_message_media_attachments(FakeMessage(media=giveaway)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
