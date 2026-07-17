from dataclasses import dataclass
from typing import Any
import re

from i18n import tr
from telethon.tl import types


_MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_~|>\[\]])")


@dataclass(frozen=True)
class RichMediaAttachment:
    media: Any
    spoiler: bool = False


def has_rich_message(message):
    return bool(message and getattr(message, "rich_message", None))


def _as_dict(value):
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, dict):
            return result
    return {}


def _escape_markdown(value):
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", str(value or ""))


def render_plain_text(node):
    node = _as_dict(node)
    if not node:
        return ""

    kind = node.get("_", "")
    if kind == "TextPlain":
        return str(node.get("text", ""))
    if kind == "TextEmpty":
        return ""
    if kind == "TextConcat":
        return "".join(render_plain_text(item) for item in node.get("texts", []))
    if kind in {"TextCustomEmoji", "TextImage", "TextMath"}:
        return str(
            node.get("alt")
            or node.get("source")
            or tr("rich.inline_image")
        )

    nested_text = node.get("text")
    if isinstance(nested_text, dict):
        return render_plain_text(nested_text)
    return str(nested_text or "")


def render_rich_text(node):
    node = _as_dict(node)
    if not node:
        return ""

    kind = node.get("_", "")
    if kind == "TextEmpty":
        return ""
    if kind == "TextPlain":
        return _escape_markdown(node.get("text", ""))
    if kind == "TextConcat":
        return "".join(render_rich_text(item) for item in node.get("texts", []))

    wrappers = {
        "TextBold": ("**", "**"),
        "TextItalic": ("*", "*"),
        "TextMarked": ("**", "**"),
        "TextSpoiler": ("||", "||"),
        "TextStrike": ("~~", "~~"),
        "TextUnderline": ("__", "__"),
    }
    if kind in wrappers:
        prefix, suffix = wrappers[kind]
        return f"{prefix}{render_rich_text(node.get('text'))}{suffix}"

    if kind == "TextFixed":
        content = render_plain_text(node.get("text")).replace("`", "'")
        return f"`{content}`"
    if kind == "TextCustomEmoji":
        return _escape_markdown(node.get("alt", ""))
    if kind == "TextImage":
        return f"[{tr('rich.inline_image')}]"
    if kind == "TextMath":
        return f"`{str(node.get('source', '')).replace('`', "'")}`"
    if kind == "TextUrl":
        label = render_rich_text(node.get("text"))
        url = str(node.get("url", ""))
        return label if not url or url.startswith("#") else f"[{label}]({url})"
    if kind == "TextEmail":
        label = render_rich_text(node.get("text"))
        email = str(node.get("email", ""))
        return f"[{label}](mailto:{email})" if email else label
    if kind == "TextPhone":
        label = render_rich_text(node.get("text"))
        phone = str(node.get("phone", ""))
        return f"[{label}](tel:{phone})" if phone else label

    nested_text = node.get("text")
    if isinstance(nested_text, dict):
        return render_rich_text(nested_text)
    return _escape_markdown(nested_text or "")


def _quote(value):
    return "\n".join(
        f"> {line}" for line in str(value or "").splitlines() if line.strip()
    )


def _render_caption(caption):
    caption = _as_dict(caption)
    if not caption:
        return ""
    text = render_rich_text(caption.get("text"))
    credit = render_rich_text(caption.get("credit"))
    if text and credit:
        return f"{text}\n*{credit}*"
    return text or (f"*{credit}*" if credit else "")


def _indent_continuation(value, indentation="  "):
    lines = str(value or "").splitlines()
    if not lines:
        return ""
    return "\n".join([lines[0], *(f"{indentation}{line}" for line in lines[1:])])


def _render_list_item(item, marker):
    item = _as_dict(item)
    checkbox = ""
    if item.get("checkbox"):
        checkbox = "[x] " if item.get("checked") else "[ ] "

    if isinstance(item.get("text"), dict):
        content = render_rich_text(item["text"])
    else:
        content = "\n".join(
            value
            for value in (render_block(block) for block in item.get("blocks", []))
            if value
        )
    return f"{marker} {checkbox}{_indent_continuation(content)}".rstrip()


def _render_table(block):
    rows = []
    for row in block.get("rows", []):
        row = _as_dict(row)
        rows.append(
            [
                render_plain_text(_as_dict(cell).get("text"))
                .replace("\n", " ")
                .replace("|", "¦")
                for cell in row.get("cells", [])
            ]
        )
    if not rows or not any(rows):
        return ""

    column_count = max(len(row) for row in rows)
    widths = [
        max(len(row[index]) if index < len(row) else 0 for row in rows)
        for index in range(column_count)
    ]
    lines = []
    for row in rows:
        cells = [
            (row[index] if index < len(row) else "").ljust(width)
            for index, width in enumerate(widths)
        ]
        lines.append(" | ".join(cells).rstrip())

    title = render_rich_text(block.get("title"))
    table = "```text\n" + "\n".join(lines) + "\n```"
    return f"**{title}**\n{table}" if title else table


def _render_media_placeholder(block, label_key):
    label = tr(label_key)
    caption = _render_caption(block.get("caption"))
    placeholder = f"*[{label}]*"
    return f"{placeholder}\n{caption}" if caption else placeholder


def render_block(block):
    block = _as_dict(block)
    if not block:
        return ""

    kind = block.get("_", "")
    if kind == "PageBlockAnchor":
        return ""
    if kind.startswith("PageBlockHeading"):
        level_text = kind.removeprefix("PageBlockHeading")
        level = min(max(int(level_text or "1"), 1), 3)
        return f"{'#' * level} {render_rich_text(block.get('text'))}"

    heading_levels = {
        "PageBlockTitle": 1,
        "PageBlockHeader": 2,
        "PageBlockSubheader": 3,
        "PageBlockSubtitle": 3,
    }
    if kind in heading_levels:
        return f"{'#' * heading_levels[kind]} {render_rich_text(block.get('text'))}"
    if kind == "PageBlockKicker":
        return f"*{render_rich_text(block.get('text'))}*"
    if kind == "PageBlockDivider":
        return "---"
    if kind == "PageBlockParagraph":
        return render_rich_text(block.get("text"))
    if kind in {"PageBlockBlockquote", "PageBlockPullquote"}:
        rendered = _quote(render_rich_text(block.get("text")))
        caption = render_rich_text(block.get("caption"))
        return f"{rendered}\n> - {caption}" if caption else rendered
    if kind == "PageBlockBlockquoteBlocks":
        content = "\n".join(
            value
            for value in (render_block(item) for item in block.get("blocks", []))
            if value
        )
        rendered = _quote(content)
        caption = render_rich_text(block.get("caption"))
        return f"{rendered}\n> - {caption}" if caption else rendered
    if kind == "PageBlockList":
        return "\n".join(
            _render_list_item(item, "-") for item in block.get("items", [])
        )
    if kind == "PageBlockOrderedList":
        return "\n".join(
            _render_list_item(item, f"{_as_dict(item).get('num') or index}.")
            for index, item in enumerate(block.get("items", []), start=1)
        )
    if kind == "PageBlockPreformatted":
        language = re.sub(
            r"[^a-zA-Z0-9_+-]",
            "",
            str(block.get("language", "")),
        )
        content = render_plain_text(block.get("text")).replace("```", "''' ")
        return f"```{language}\n{content}\n```"
    if kind == "PageBlockDetails":
        content = "\n\n".join(
            value
            for value in (render_block(item) for item in block.get("blocks", []))
            if value
        )
        title = render_rich_text(block.get("title"))
        return f"**{tr('rich.details', title=title)}**\n{content}".rstrip()
    if kind == "PageBlockTable":
        return _render_table(block)
    if kind == "PageBlockMath":
        return f"```latex\n{block.get('source', '')}\n```"
    if kind == "PageBlockThinking":
        return f"*{tr('rich.thinking', text=render_rich_text(block.get('text')))}*"
    if kind == "PageBlockMap":
        geo = _as_dict(block.get("geo"))
        latitude = geo.get("lat")
        longitude = geo.get("long")
        caption = _render_caption(block.get("caption")) or tr("rich.location")
        if latitude is None or longitude is None:
            return f"*[{tr('rich.map', caption=caption)}]*"
        url = (
            "https://www.openstreetmap.org/"
            f"?mlat={latitude}&mlon={longitude}"
        )
        return f"[{tr('rich.map', caption=caption)}]({url})"
    if kind == "PageBlockPhoto":
        return _render_media_placeholder(block, "rich.media_photo")
    if kind == "PageBlockVideo":
        return _render_media_placeholder(block, "rich.media_video")
    if kind == "PageBlockAudio":
        return _render_media_placeholder(block, "rich.media_audio")
    if kind == "PageBlockCollage":
        return _render_media_placeholder(block, "rich.media_collage")
    if kind == "PageBlockSlideshow":
        return _render_media_placeholder(block, "rich.media_slideshow")
    if kind == "PageBlockCover":
        return render_block(block.get("cover"))
    if kind == "PageBlockEmbed":
        caption = _render_caption(block.get("caption"))
        url = str(block.get("url") or "")
        if url:
            label = caption or tr("rich.embedded_content")
            return f"[{label}]({url})"
        return caption or f"*[{tr('rich.embedded_content')}]*"
    if kind == "PageBlockEmbedPost":
        header = str(block.get("author") or tr("rich.embedded_post"))
        url = str(block.get("url") or "")
        if url:
            header = f"[{_escape_markdown(header)}]({url})"
        content = "\n\n".join(
            value
            for value in (render_block(item) for item in block.get("blocks", []))
            if value
        )
        caption = _render_caption(block.get("caption"))
        return "\n\n".join(value for value in (header, content, caption) if value)
    if kind == "PageBlockAuthorDate":
        author = render_rich_text(block.get("author"))
        published_date = block.get("published_date")
        date_suffix = f" - {published_date}" if published_date else ""
        return f"*{author}{date_suffix}*"
    if kind == "PageBlockChannel":
        channel = _as_dict(block.get("channel"))
        title = channel.get("title") or channel.get("username")
        return f"**{_escape_markdown(title or tr('rich.channel'))}**"
    if kind == "PageBlockRelatedArticles":
        title = render_rich_text(block.get("title"))
        articles = []
        for article in block.get("articles", []):
            article = _as_dict(article)
            article_title = _escape_markdown(article.get("title") or article.get("url"))
            article_url = str(article.get("url") or "")
            articles.append(
                f"- [{article_title}]({article_url})"
                if article_url
                else f"- {article_title}"
            )
        return "\n".join([f"**{title}**" if title else "", *articles]).strip()
    if kind == "PageBlockFooter":
        return f"*{render_rich_text(block.get('text'))}*"

    return f"*[{tr('rich.unsupported_block', block_type=kind or 'unknown')}]*"


def render_rich_message(rich_message):
    data = _as_dict(rich_message)
    rendered_blocks = []
    for block in data.get("blocks", []):
        value = render_block(block).strip()
        if value:
            rendered_blocks.append(value)
    return "\n\n".join(rendered_blocks)


def _media_id(media):
    try:
        return int(getattr(media, "id"))
    except (AttributeError, TypeError, ValueError):
        return None


def is_downloadable_media(media):
    """Return whether Telethon exposes an actual downloadable file."""
    if isinstance(media, (types.Photo, types.Document)):
        return True
    if isinstance(media, types.MessageMediaPhoto):
        return isinstance(media.photo, types.Photo) or isinstance(
            media.video,
            types.Document,
        )
    if isinstance(media, types.MessageMediaDocument):
        return isinstance(media.document, types.Document)
    return False


def get_rich_media_attachments(rich_message):
    data = _as_dict(rich_message)
    photos = list(getattr(rich_message, "photos", None) or [])
    documents = list(getattr(rich_message, "documents", None) or [])
    photo_by_id = {_media_id(media): media for media in photos}
    document_by_id = {_media_id(media): media for media in documents}
    attachments = []
    seen_ids = set()

    def add_media(media_id, media_by_id, spoiler=False):
        try:
            media_id = int(media_id)
        except (TypeError, ValueError):
            return
        media = media_by_id.get(media_id)
        if (
            media is None
            or media_id in seen_ids
            or not is_downloadable_media(media)
        ):
            return
        seen_ids.add(media_id)
        attachments.append(
            RichMediaAttachment(media=media, spoiler=bool(spoiler))
        )

    def walk_text(node):
        node = _as_dict(node)
        if not node:
            return
        if node.get("_") == "TextImage":
            add_media(node.get("document_id"), document_by_id)
        for key in ("text", "texts"):
            value = node.get(key)
            if isinstance(value, dict):
                walk_text(value)
            elif isinstance(value, list):
                for item in value:
                    walk_text(item)

    def walk_block(block):
        block = _as_dict(block)
        if not block:
            return
        kind = block.get("_", "")
        if kind == "PageBlockPhoto":
            add_media(
                block.get("photo_id"),
                photo_by_id,
                spoiler=bool(block.get("spoiler")),
            )
        elif kind == "PageBlockVideo":
            add_media(
                block.get("video_id"),
                document_by_id,
                spoiler=bool(block.get("spoiler")),
            )
        elif kind == "PageBlockAudio":
            add_media(block.get("audio_id"), document_by_id)
        elif kind == "PageBlockEmbed":
            add_media(block.get("poster_photo_id"), photo_by_id)

        for key in ("items", "blocks"):
            for item in block.get(key, []) or []:
                item = _as_dict(item)
                if "blocks" in item:
                    for nested_block in item.get("blocks", []):
                        walk_block(nested_block)
                else:
                    walk_block(item)
                walk_text(item.get("text"))
        if isinstance(block.get("cover"), dict):
            walk_block(block["cover"])

        for key in ("text", "title", "caption"):
            value = _as_dict(block.get(key))
            if value.get("_") == "PageCaption":
                walk_text(value.get("text"))
                walk_text(value.get("credit"))
            else:
                walk_text(value)

    for block in data.get("blocks", []):
        walk_block(block)

    for media in [*photos, *documents]:
        media_id = _media_id(media)
        if (
            media_id is not None
            and media_id not in seen_ids
            and is_downloadable_media(media)
        ):
            seen_ids.add(media_id)
            attachments.append(RichMediaAttachment(media=media))

    return attachments


def get_message_media_attachments(message):
    attachments = []
    media = getattr(message, "media", None)
    if is_downloadable_media(media):
        attachments.append(
            RichMediaAttachment(
                media=media,
                spoiler=bool(getattr(media, "spoiler", False)),
            )
        )

    rich_message = getattr(message, "rich_message", None)
    if rich_message is not None:
        attachments.extend(get_rich_media_attachments(rich_message))

    unique_attachments = []
    seen_keys = set()
    for attachment in attachments:
        media_id = _media_id(attachment.media)
        key = (type(attachment.media), media_id if media_id is not None else id(attachment.media))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_attachments.append(attachment)
    return unique_attachments
