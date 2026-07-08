import logging

from i18n import tr


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


def trim_context_text(text, limit=700):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit - 3].rstrip() + "..."


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

    reply_text = (
        getattr(reply_to, "quote_text", None)
        or (reply_message.text if reply_message else "")
    )
    reply_text = trim_context_text(repair_mojibake(reply_text))
    if reply_text:
        return f"{header}\n\n{format_blockquote(reply_text)}"

    return header


def split_text(text, limit):
    """Split long text without cutting through paragraphs when possible."""
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


def select_album_text_message(album_messages, fallback_message):
    text_messages = [message for message in album_messages if message and message.text]
    if not text_messages:
        return fallback_message

    return max(text_messages, key=lambda message: len(message.text or ""))


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

    if source_message.text:
        lines.append(source_message.text)

    return "\n\n".join(lines)

