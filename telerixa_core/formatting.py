import logging
from bisect import bisect_left

from i18n import tr
from telethon.tl.types import MessageEntitySpoiler
from .rich_messages import render_rich_message


logger = logging.getLogger(__name__)


def get_post_url(message, channel_name):
    """Build a public Telegram post URL."""
    return f"https://t.me/{channel_name}/{message.id}"


def get_forward_info(message):
    """Return a human-readable source description for forwarded posts."""
    forward = getattr(message, "forward", None)
    if not forward:
        return None

    from_name = getattr(forward, "from_name", None)
    chat = getattr(forward, "chat", None)

    if chat:
        title = getattr(chat, "title", None) or getattr(chat, "username", None)
        username = getattr(chat, "username", None)
        if username:
            return tr("telegram.forward_from_channel_link", title=title, username=username)
        if title:
            return tr("telegram.forward_from_channel", title=title)

    if from_name:
        return tr("telegram.forward_from_user", name=from_name)

    return tr("telegram.forward_from_unknown")


def repair_mojibake(text):
    if not text:
        return text

    text = str(text)
    if not any(marker in text for marker in ("Ð", "Ñ", "Â", "â")):
        return text

    try:
        fixed_text = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        return text

    if any(marker in fixed_text for marker in ("Ð", "Ñ")):
        return text

    return fixed_text


def format_blockquote(text):
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _utf16_offset_to_index(boundaries, offset):
    offset = max(0, min(int(offset), boundaries[-1]))
    return min(bisect_left(boundaries, offset), len(boundaries) - 1)


def format_spoilers(text, entities=None):
    """Convert Telegram spoiler entities to Discord spoiler markup."""
    if not text or not entities:
        return text

    text = str(text)
    boundaries = [0]
    for character in text:
        boundaries.append(
            boundaries[-1] + (2 if ord(character) > 0xFFFF else 1)
        )

    ranges = []
    for entity in entities:
        if not isinstance(entity, MessageEntitySpoiler):
            continue

        start_offset = max(0, int(entity.offset))
        end_offset = start_offset + max(0, int(entity.length))
        start = _utf16_offset_to_index(boundaries, start_offset)
        end = _utf16_offset_to_index(boundaries, end_offset)
        if end > start:
            ranges.append((start, end))

    if not ranges:
        return text

    merged_ranges = []
    for start, end in sorted(ranges):
        if merged_ranges and start <= merged_ranges[-1][1]:
            previous_start, previous_end = merged_ranges[-1]
            merged_ranges[-1] = (previous_start, max(previous_end, end))
        else:
            merged_ranges.append((start, end))

    parts = []
    cursor = 0
    for start, end in merged_ranges:
        parts.append(text[cursor:start])
        parts.append(f"||{text[start:end]}||")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def get_message_text(message):
    if not message:
        return ""

    rich_message = getattr(message, "rich_message", None)
    if rich_message is not None:
        rich_text = render_rich_message(rich_message)
        if rich_text:
            return rich_text

    return format_spoilers(
        getattr(message, "text", "") or "",
        getattr(message, "entities", None),
    )


async def get_reply_info(message, telegram_client=None):
    """Return Telegram reply/quote context when a post replies to another post."""
    reply_to = getattr(message, "reply_to", None)
    if not reply_to:
        return None

    reply_message = None
    try:
        reply_message = await message.get_reply_message()
    except Exception as e:
        logger.warning(tr("telegram.reply_fetch_failed", message_id=message.id, error=e))

    if not reply_message and telegram_client is not None:
        reply_peer = getattr(reply_to, "reply_to_peer_id", None)
        reply_id = getattr(reply_to, "reply_to_msg_id", None)
        if reply_peer and reply_id:
            try:
                reply_message = await telegram_client.get_messages(reply_peer, ids=reply_id)
            except Exception as e:
                logger.warning(
                    tr("telegram.cross_reply_fetch_failed", message_id=message.id, error=e)
                )

    chat = getattr(reply_message, "chat", None) if reply_message else None
    sender = getattr(reply_message, "sender", None) if reply_message else None
    title = (
        getattr(chat, "title", None)
        or getattr(chat, "username", None)
        or getattr(sender, "first_name", None)
        or tr("telegram.reply_unknown_title")
    )
    title = repair_mojibake(title)
    username = getattr(chat, "username", None)

    if username and reply_message:
        header = tr(
            "telegram.reply_to_link",
            title=title,
            username=username,
            message_id=reply_message.id,
        )
    else:
        header = tr("telegram.reply_to", title=title)

    quote_text = getattr(reply_to, "quote_text", None)
    if quote_text:
        reply_text = format_spoilers(
            quote_text,
            getattr(reply_to, "quote_entities", None),
        )
    else:
        reply_text = get_message_text(reply_message)
    reply_text = (repair_mojibake(reply_text) or "").strip()
    if reply_text:
        return f"{header}\n\n{format_blockquote(reply_text)}"

    return header


def _split_plain_text(text, limit):
    if not text:
        return []

    chunks = []
    current = ""

    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        if len(paragraph) > limit:
            words = paragraph.split()
            for word in words:
                if len(word) > limit:
                    if current:
                        chunks.append(current)
                        current = ""
                    chunks.extend(word[i:i + limit] for i in range(0, len(word), limit))
                    continue

                candidate = f"{current} {word}".strip()
                if len(candidate) <= limit:
                    current = candidate
                else:
                    if current:
                        chunks.append(current)
                    current = word
            continue

        separator = "\n\n" if current else ""
        candidate = f"{current}{separator}{paragraph}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)

    return chunks


def _balance_spoiler_chunks(chunks):
    chunks = list(chunks)
    for index in range(len(chunks) - 1):
        if chunks[index].endswith("|") and chunks[index + 1].startswith("|"):
            chunks[index] = chunks[index][:-1]
            chunks[index + 1] = f"|{chunks[index + 1]}"

    balanced = []
    spoiler_open = False
    for chunk in chunks:
        rendered = f"||{chunk}" if spoiler_open else chunk
        if chunk.count("||") % 2:
            spoiler_open = not spoiler_open
        if spoiler_open:
            rendered = f"{rendered}||"
        if rendered:
            balanced.append(rendered)
    return balanced


def split_text(text, limit):
    """Split text while preserving paragraphs and balanced Discord spoilers."""
    if not text or limit <= 0:
        return []
    if "||" not in text or limit <= 4:
        return _split_plain_text(text, limit)

    chunks = _split_plain_text(text, limit - 4)
    return _balance_spoiler_chunks(chunks)


def select_album_text_message(album_messages, fallback_message):
    text_messages = [
        message
        for message in album_messages
        if message and get_message_text(message)
    ]
    if not text_messages:
        return fallback_message

    return max(text_messages, key=lambda message: len(get_message_text(message)))


async def build_message_text(telegram_message, channel_name, text_message=None, telegram_client=None):
    """Build post text with metadata and Telegram link."""
    source_message = text_message or telegram_message
    lines = [tr("telegram.post_link", url=get_post_url(source_message, channel_name))]
    forward_info = get_forward_info(source_message) or get_forward_info(telegram_message)

    if forward_info:
        lines.append(forward_info)

    reply_info = await get_reply_info(source_message, telegram_client=telegram_client)
    if reply_info:
        lines.append(reply_info)

    source_text = get_message_text(source_message)
    if source_text:
        lines.append(source_text)

    return "\n\n".join(lines)
