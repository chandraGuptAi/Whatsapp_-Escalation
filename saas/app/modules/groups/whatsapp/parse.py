"""Turn neonize (WhatsApp) events into plain messages. Pure: no database, testable without a phone.
Ported from app/escalation_analyst/whatsapp/messages.py of the old service."""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("app.ingest")

# Old WhatsApp kinds -> app.messages.kind
KIND = {"chat": "text", "image": "image", "video": "video", "audio": "audio", "ptt": "audio",
        "document": "document", "sticker": "sticker"}
MEDIA_FIELDS = [("imageMessage", "image"), ("videoMessage", "video"), ("audioMessage", "audio"),
                ("documentMessage", "document"), ("stickerMessage", "sticker")]
WRAPPERS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension",
            "documentWithCaptionMessage")
QUOTE_FIELDS = ("extendedTextMessage", "imageMessage", "videoMessage", "audioMessage", "documentMessage")


@dataclass
class ParsedMessage:
    chat: str                 # group jid, e.g. 1203...@g.us
    group_name: str           # known only from history sync; "" for live messages
    key: str                  # "<from_me>_<chat>_<id>": same key as the old service, so imports never duplicate
    sender: str
    sender_name: str
    wa_kind: str              # WhatsApp kind (chat, ptt, image, ...)
    body: str
    sent_at: int              # epoch seconds
    reply_to: str | None = None
    media: object | None = None       # neonize media sub-message (size, mimetype, file name)
    raw: object | None = None         # unwrapped message, what client.download_any() needs

    @property
    def kind(self) -> str:
        return KIND.get(self.wa_kind, "other")


def jid(value) -> str:
    if isinstance(value, str):
        return value.replace("@c.us", "@s.whatsapp.net")
    return f"{value.User}@{value.Server}" if value.User and value.Server else ""


def display_name(client, sender: str, alt: str = "", pushname: str = "") -> str:
    """Name the linked phone shows: saved contact, then WhatsApp profile name, then phone number."""
    addresses = [a for a in (sender, alt) if "@" in a]
    saved = profile = ""
    if client is not None and addresses:
        from neonize.utils.jid import build_jid
        try:
            # Group senders arrive as hidden @lid ids; the contact store is keyed by phone number.
            if not any(a.endswith("@s.whatsapp.net") for a in addresses):
                addresses += [a for a in (jid(client.get_pn_from_lid(build_jid(*sender.split("@", 1)))),) if a]
            for address in addresses:
                info = client.contact.get_contact(build_jid(*address.split("@", 1)))
                if info.Found:
                    saved = saved or info.FullName or info.FirstName
                    profile = profile or info.PushName or info.BusinessName
        except Exception:
            logger.debug("WhatsApp contact lookup failed for %s", sender, exc_info=True)
    phone = next(("+" + a.split("@")[0] for a in addresses if a.endswith("@s.whatsapp.net")), "")
    return saved or pushname or profile or phone or sender


def content(message):
    """Returns (text, WhatsApp kind, media sub-message or None, unwrapped message)."""
    for _ in range(5):
        wrapper = next((name for name in WRAPPERS if message.HasField(name)), None)
        if not wrapper:
            break
        message = getattr(message, wrapper).message
    if message.conversation:
        return message.conversation, "chat", None, message
    if message.HasField("extendedTextMessage"):
        return message.extendedTextMessage.text, "chat", None, message
    for field, kind in MEDIA_FIELDS:
        if message.HasField(field):
            media = getattr(message, field)
            return getattr(media, "caption", ""), "ptt" if kind == "audio" and media.PTT else kind, media, message
    return "", "unsupported", None, message


def build(client, chat: str, token: str, sender: str, stamp: int, message, *, name: str = "", pushname: str = "",
          from_me: bool = False, alt: str = "") -> ParsedMessage | None:
    # Live events carry epoch milliseconds, history sync epoch seconds.
    if stamp > 10**11:
        stamp //= 1000
    if not chat.endswith("@g.us") or not token or stamp <= 0:
        return None
    body, wa_kind, media, unwrapped = content(message)
    if wa_kind == "unsupported":
        return None
    reply_to = None
    for field in QUOTE_FIELDS:
        if unwrapped.HasField(field):
            reply_to = getattr(unwrapped, field).contextInfo.stanzaID or None
            break
    return ParsedMessage(chat=chat, group_name=name, key=f"{str(from_me).lower()}_{chat}_{token}", sender=sender,
                         sender_name=display_name(client, sender, alt, pushname), wa_kind=wa_kind, body=body,
                         sent_at=stamp, reply_to=reply_to, media=media, raw=unwrapped if media is not None else None)


def parse_live(client, event) -> ParsedMessage | None:
    info = event.Info
    source = info.MessageSource
    return build(client, jid(source.Chat), info.ID, jid(source.Sender), int(info.Timestamp), event.Message,
                 pushname=info.Pushname, from_me=source.IsFromMe, alt=jid(source.SenderAlt))


def parse_history(client, event, limit: int) -> tuple[list[tuple[str, str]], list[ParsedMessage]]:
    """Returns ([(group jid, group name)], messages). Keeps only the newest `limit` messages per group."""
    groups, messages = [], []
    for conversation in event.Data.conversations:
        if not conversation.ID.endswith("@g.us"):
            continue
        name = conversation.name or conversation.displayName or conversation.ID
        groups.append((conversation.ID, name))
        rows = sorted(conversation.messages, key=lambda row: row.message.messageTimestamp)
        for row in rows[-limit:] if limit else []:
            item = row.message
            parsed = build(client, conversation.ID, item.key.ID, item.key.participant or item.participant,
                           int(item.messageTimestamp), item.message, name=name, pushname=item.pushName,
                           from_me=item.key.fromMe)
            if parsed:
                messages.append(parsed)
    return groups, messages
