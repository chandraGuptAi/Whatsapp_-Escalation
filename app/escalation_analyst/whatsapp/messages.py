"""Translate native WhatsApp events into the existing message repository contract."""
from __future__ import annotations

import base64
import logging
import time
from .ingest import ingest
from .media import understand

logger = logging.getLogger("escalation_analyst.whatsapp")


def jid(value) -> str:
    if isinstance(value, str):
        return value.replace("@c.us", "@s.whatsapp.net")
    return f"{value.User}@{value.Server}" if value.User and value.Server else ""


def content(message):
    for _ in range(5):
        wrapper = next((name for name in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension", "documentWithCaptionMessage") if message.HasField(name)), None)
        if not wrapper:
            break
        message = getattr(message, wrapper).message
    if message.conversation:
        return message.conversation, "chat", None, message
    if message.HasField("extendedTextMessage"):
        return message.extendedTextMessage.text, "chat", None, message
    for field, kind in [("imageMessage", "image"), ("videoMessage", "video"), ("audioMessage", "audio"), ("documentMessage", "document"), ("stickerMessage", "sticker")]:
        if message.HasField(field):
            media = getattr(message, field)
            return getattr(media, "caption", ""), "ptt" if kind == "audio" and media.PTT else kind, media, message
    return "", "unsupported", None, message


class MessageReceiver:
    def __init__(self, runtime):
        self.r = runtime

    def save(self, client, chat: str, token: str, sender: str, stamp: int, message, name="", pushname="", from_me=False):
        if not chat.endswith("@g.us") or not token or stamp <= 0:
            return
        body, kind, media, unwrapped = content(message)
        if kind == "unsupported":
            return
        # Keep the old Node key prefix so history sync cannot duplicate old captures
        # when the new protocol supplies a different PN/LID sender representation.
        key = f"{str(from_me).lower()}_{chat}_{token}"
        if self.r.groups.has_message_key(chat, key):
            return
        if self.r.settings.retention_days > 0 and stamp < time.time() - self.r.settings.retention_days * 86400:
            return
        info = self.r.groups.transport_info(chat)
        data = dict(wa_id=key, group_id=chat, group_name=name or info["name"] or chat,
                    author=sender, author_name=pushname or sender, body=body,
                    msg_type=kind, timestamp=stamp, has_media=int(media is not None))
        for field in ("extendedTextMessage", "imageMessage", "videoMessage", "audioMessage", "documentMessage"):
            if unwrapped.HasField(field):
                context = getattr(unwrapped, field).contextInfo
                if context.stanzaID:
                    rows = self.r.db.read("SELECT wa_id FROM messages WHERE group_id=? AND wa_id IN (?,?)",
                                          (chat, f"false_{chat}_{context.stanzaID}", f"true_{chat}_{context.stanzaID}"))
                    if len(rows) == 1:
                        data["quoted_wa_id"] = rows[0]["wa_id"]
                break
        result = ingest(self.r, data)
        if result["inserted"] and media is not None:
            self.r.groups.update_body(result["id"], (body + " [media analysing]").strip())
            self.r.executor.submit(self.enrich, client, unwrapped, media, kind, result["id"], body)

    def enrich(self, client, message, media, kind, message_id, caption):
        try:
            if getattr(media, "fileLength", 0) > 6 * 1024 * 1024:
                text = f"[{kind}: too large to analyse]"
            else:
                raw = client.download_any(message)
                text = understand(dict(data=base64.b64encode(raw).decode(),
                                       mimetype=getattr(media, "mimetype", "application/octet-stream"),
                                       filename=getattr(media, "fileName", "")), kind, self.r.settings)
        except Exception:
            logger.warning("Native WhatsApp media download failed (%s)", kind)
            text = f"[{kind}: media download failed]"
        self.r.groups.update_body(message_id, (caption + " " + text).strip())

    def live(self, client, event):
        try:
            info = event.Info
            source = info.MessageSource
            if not jid(source.Chat).endswith("@g.us"):
                return
            body, _, _, _ = content(event.Message)
            owner = jid(self.r.settings.recipient)
            if body.strip().lower() == "!analyse" and owner and owner in (jid(source.Sender), jid(source.SenderAlt)):
                group_id = jid(source.Chat)
                if self.r.groups.transport_info(group_id)["is_active"]:
                    self.r.start("group", "group:" + group_id, {"hours": 24, "deliver": True, "trigger": "command"},
                                 lambda _: self.r.analysis.group(group_id, {"hours": 24, "deliver": True, "trigger": "command"}))
                return
            self.save(client, jid(source.Chat), info.ID, jid(source.Sender), int(info.Timestamp),
                      event.Message, pushname=info.Pushname, from_me=source.IsFromMe)
        except Exception:
            logger.exception("Native WhatsApp message could not be saved")

    def history(self, client, event):
        try:
            for conversation in event.Data.conversations:
                if not conversation.ID.endswith("@g.us"):
                    continue
                name = conversation.name or conversation.displayName or conversation.ID
                self.r.groups.learn(conversation.ID, name)
                messages = sorted(conversation.messages, key=lambda row: row.message.messageTimestamp)
                limit = self.r.settings.whatsapp_history_limit
                for row in messages[-limit:] if limit else []:
                    item = row.message
                    self.save(client, conversation.ID, item.key.ID, item.key.participant or item.participant,
                              int(item.messageTimestamp), item.message, name, item.pushName, item.key.fromMe)
        except Exception:
            logger.exception("Native WhatsApp history sync failed")
