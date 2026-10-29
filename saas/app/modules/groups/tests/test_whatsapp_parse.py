"""WhatsApp event parsing with real neonize protobufs, no phone and no database.
Ported from tests/escalation_analyst/test_python_whatsapp.py of the old service."""
from __future__ import annotations

import base64
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("neonize")

from neonize.proto import Neonize_pb2 as n  # noqa: E402
from neonize.proto.waCommon.WACommon_pb2 import MessageKey  # noqa: E402
from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as e  # noqa: E402
from neonize.proto.waHistorySync import WAWebProtobufsHistorySync_pb2 as h  # noqa: E402
from neonize.proto.waWeb import WAWebProtobufsWeb_pb2 as w  # noqa: E402
from neonize.utils.jid import build_jid  # noqa: E402

from app.modules.groups.whatsapp import parse  # noqa: E402
from app.modules.groups.whatsapp.session import device_jid_text, parse_device_jid, qr_data_url  # noqa: E402


def event(token="new-message", text="Delivery pending", chat="test", sender="seller", server="g.us"):
    return n.Message(Info=n.MessageInfo(ID=token, Timestamp=int(time.time()), Pushname="Seller",
                     MessageSource=n.MessageSource(Chat=build_jid(chat, server), Sender=build_jid(sender),
                                                   IsGroup=True, IsFromMe=False)),
                     Message=e.Message(conversation=text))


class Contacts:
    """Linked-phone contact store: LID 111 maps to 919800000001, saved as Ramesh FE."""

    def __init__(self):
        self.contact = SimpleNamespace(get_contact=lambda j: n.ContactInfo(Found=True, FullName="Ramesh FE")
                                       if j.User == "919800000001" else n.ContactInfo(Found=False))

    def get_pn_from_lid(self, j):
        return build_jid("919800000001") if j.User == "111" else n.JID()


def test_group_message_keeps_the_old_dedupe_key_format():
    parsed = parse.parse_live(None, event())
    assert parsed.key == "false_test@g.us_new-message"
    assert parsed.chat == "test@g.us" and parsed.sender == "seller@s.whatsapp.net"
    assert parsed.kind == "text" and parsed.body == "Delivery pending" and parsed.sender_name == "Seller"


def test_private_chats_and_unsupported_messages_are_ignored():
    assert parse.parse_live(None, event(chat="someone", server="s.whatsapp.net")) is None
    empty = event()
    empty.Message.CopyFrom(e.Message())
    assert parse.parse_live(None, empty) is None


def test_live_millisecond_timestamps_become_seconds():
    live = event("millis")
    now = int(time.time())
    live.Info.Timestamp = now * 1000 + 456
    assert parse.parse_live(None, live).sent_at == now


def test_sender_names_come_from_contacts_then_push_name_then_phone():
    hidden = event("lid", sender="111")
    hidden.Info.Pushname = ""
    hidden.Info.MessageSource.Sender.Server = "lid"
    assert parse.parse_live(None, hidden).sender_name == "111@lid"
    assert parse.parse_live(Contacts(), hidden).sender_name == "Ramesh FE"
    stranger = event("stranger", sender="222")
    stranger.Info.Pushname = ""
    assert parse.parse_live(Contacts(), stranger).sender_name == "+222"


def test_quoted_reply_keeps_the_source_message_id():
    reply = event("staff-reply", sender="staff")
    reply.Message.CopyFrom(e.Message(extendedTextMessage=e.ExtendedTextMessage(
        text="Checking with courier", contextInfo=e.ContextInfo(stanzaID="seller-enquiry"))))
    parsed = parse.parse_live(None, reply)
    assert parsed.reply_to == "seller-enquiry" and parsed.body == "Checking with courier"


def test_voice_note_is_audio_with_downloadable_media():
    voice = event("voice")
    voice.Message.CopyFrom(e.Message(audioMessage=e.AudioMessage(PTT=True, mimetype="audio/ogg", fileLength=5)))
    parsed = parse.parse_live(None, voice)
    assert parsed.kind == "audio" and parsed.wa_kind == "ptt"
    assert parsed.media.fileLength == 5 and parsed.raw is not None


def test_history_sync_learns_group_names_and_keeps_only_the_newest_messages():
    now = int(time.time())
    items = [h.HistorySyncMsg(message=w.WebMessageInfo(
        key=MessageKey(ID=f"h{i}", remoteJID="test@g.us", participant="seller@s.whatsapp.net", fromMe=False),
        message=e.Message(conversation=f"old {i}"), messageTimestamp=now - 100 + i, pushName="Seller"))
        for i in range(5)]
    history = n.HistorySync(Data=h.HistorySync(conversations=[
        h.Conversation(ID="test@g.us", name="Actual group", messages=items),
        h.Conversation(ID="someone@s.whatsapp.net", name="Private", messages=items[:1]),
    ]))
    groups, messages = parse.parse_history(None, history, limit=2)
    assert groups == [("test@g.us", "Actual group")]
    assert [m.key for m in messages] == ["false_test@g.us_h3", "false_test@g.us_h4"]
    assert all(m.group_name == "Actual group" for m in messages)
    assert parse.parse_history(None, history, limit=0)[1] == []


def test_qr_is_a_local_svg():
    url = qr_data_url(b"local-test-qr")
    assert url.startswith("data:image/svg+xml;base64,")
    assert b"<svg" in base64.b64decode(url.split(",", 1)[1])


def test_device_jid_round_trip():
    jid = n.JID(User="919800000001", Device=7, Server="s.whatsapp.net")
    text = device_jid_text(jid)
    assert text == "919800000001:7@s.whatsapp.net"
    back = parse_device_jid(text)
    assert (back.User, back.Device, back.Server) == ("919800000001", 7, "s.whatsapp.net")
    assert parse_device_jid(None) is None
