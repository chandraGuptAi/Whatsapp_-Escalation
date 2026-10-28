"""Native Python QR/lifecycle and real protobuf message contracts, without connecting a phone."""
import base64
import threading
import time
import pytest
from types import SimpleNamespace
from neonize.proto import Neonize_pb2 as n
from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as e
from neonize.proto.waHistorySync import WAWebProtobufsHistorySync_pb2 as h
from neonize.proto.waWeb import WAWebProtobufsWeb_pb2 as w
from neonize.proto.waCommon.WACommon_pb2 import MessageKey
from neonize.utils.jid import build_jid
from .test_core import client, ingest, message
from app.escalation_analyst.whatsapp.native import NativeWhatsApp


@pytest.fixture(autouse=True)
def use_native_callbacks(client):
    runtime=client.app.state.runtime
    runtime.whatsapp.close()
    runtime.whatsapp=NativeWhatsApp(runtime)


def event(token="new-message", text="Delivery pending", chat="test", sender="seller"):
    return n.Message(Info=n.MessageInfo(ID=token, Timestamp=int(time.time()), Pushname="Seller",
        MessageSource=n.MessageSource(Chat=build_jid(chat,"g.us"),Sender=build_jid(sender),IsGroup=True,IsFromMe=False)),
        Message=e.Message(conversation=text))


def wait_until(test):
    for _ in range(100):
        if test(): return
        time.sleep(0.02)
    raise AssertionError("Native worker did not reach the expected state")


def test_native_message_is_saved_and_legacy_sender_keys_deduplicate(client):
    r=client.app.state.runtime
    old=message();old['wa_id']='false_test@g.us_legacy_sender@lid'
    ingest(client,old)
    receiver=r.whatsapp.receiver
    receiver.live(None,event('legacy'))
    assert len(r.groups.messages('test@g.us'))==1
    receiver.live(None,event())
    receiver.live(None,event())
    rows=r.groups.messages('test@g.us')
    assert len(rows)==2
    assert rows[-1]['body']=='Delivery pending'
    assert rows[-1]['author']=='seller@s.whatsapp.net'
    receiver.live(None,event(chat='private'))
    receiver.live(None,n.Message(Info=n.MessageInfo(ID='private',Timestamp=int(time.time()),
        MessageSource=n.MessageSource(Chat=build_jid('private'),Sender=build_jid('seller'))),
        Message=e.Message(conversation='Private chat must not be saved')))
    assert all(row['group_id'].endswith('@g.us') for row in r.groups.messages(None))


def test_paused_group_and_history_sync(client):
    r=client.app.state.runtime
    ingest(client)
    r.groups.status('test@g.us',False)
    r.whatsapp.receiver.live(None,event())
    assert len(r.groups.messages('test@g.us'))==1
    r.groups.status('test@g.us',True)
    item=w.WebMessageInfo(key=MessageKey(ID='history-id',remoteJID='test@g.us',participant='seller@s.whatsapp.net',fromMe=False),
                         message=e.Message(extendedTextMessage=e.ExtendedTextMessage(text='Historical message')),
                         messageTimestamp=int(time.time()),pushName='Seller')
    history=n.HistorySync(Data=h.HistorySync(conversations=[h.Conversation(ID='test@g.us',name='Actual group',messages=[h.HistorySyncMsg(message=item)])]))
    r.whatsapp.receiver.history(None,history)
    r.whatsapp.receiver.live(None,event('history-id'))
    assert len(r.groups.messages('test@g.us'))==2
    assert r.groups.get('test@g.us')['group_name']=='Actual group'


def test_native_quoted_staff_reply_preserves_source_key(client):
    r = client.app.state.runtime
    receiver = r.whatsapp.receiver
    receiver.live(None, event('seller-enquiry', '12345678 / BGSC0078363792'))
    reply = event('staff-reply', sender='staff')
    reply.Message.CopyFrom(e.Message(extendedTextMessage=e.ExtendedTextMessage(
        text='Checking with courier', contextInfo=e.ContextInfo(stanzaID='seller-enquiry'))))
    receiver.live(None, reply)
    messages = r.groups.messages('test@g.us')
    assert len(messages) == 2
    assert messages[1]['quoted_wa_id'] == messages[0]['wa_id']


def test_qr_is_local_svg_and_cleared_on_connection(client):
    transport=client.app.state.runtime.whatsapp
    transport.qr(None,b'local-test-qr')
    status=client.get('/whatsapp/status').json()
    assert status['state']=='qr_required'
    assert b'<svg' in base64.b64decode(status['qrImage'].split(',',1)[1])
    transport.set_status('ready')
    assert not client.get('/whatsapp/status').json()['qrImage']
    transport.set_status('disconnected')
    assert not transport.status()['qrImage']


def test_python_lifecycle_no_node_or_http_bridge(client,monkeypatch):
    import neonize.client
    import httpx
    monkeypatch.setattr(httpx,'request',lambda *a,**k: (_ for _ in ()).throw(AssertionError('No HTTP bridge allowed')))
    created=[]
    class Events:
        def __init__(self): self.handlers={}
        def __call__(self,kind): return lambda handler:self.handlers.__setitem__(kind,handler)
        def qr(self,handler): self.qr_handler=handler
    class Native:
        def __init__(self,name,**kwargs):
            self.event=Events();self.stopped=threading.Event();created.append(self)
        def connect(self):
            self.event.qr_handler(self,b'test-code')
            self.stopped.wait(5)
        def stop(self): self.stopped.set()
    monkeypatch.setattr(neonize.client,'NewClient',Native)
    response=client.post('/whatsapp/reconnect')
    assert response.status_code==200
    transport=client.app.state.runtime.whatsapp
    wait_until(lambda:transport.status()['state']=='qr_required')
    client.post('/whatsapp/reconnect')
    assert len(created)==1
    transport.close()
    assert not transport.worker.is_alive()
    assert transport.status()['state']=='disconnected'


def test_send_builds_a_serializable_native_jid(client):
    captured=[]
    transport=client.app.state.runtime.whatsapp
    transport.client=SimpleNamespace(send_message=lambda target,text:captured.append((target.SerializeToString(),text)),stop=lambda:None)
    transport.set_status('ready')
    assert transport.send('919999999999@c.us','Saved report')=={'ok':True}
    target=n.JID.FromString(captured[0][0])
    assert target.Server=='s.whatsapp.net'
    assert captured[0][1]=='Saved report'


def test_native_voice_media_uses_python_download_and_existing_extractor(client,monkeypatch):
    from app.escalation_analyst.whatsapp import messages
    r=client.app.state.runtime
    monkeypatch.setattr(messages,'understand',lambda media,kind,settings:'Decoded voice' if kind=='ptt' and base64.b64decode(media['data'])==b'voice' else 'bad input')
    incoming=event('voice')
    incoming.Message.CopyFrom(e.Message(audioMessage=e.AudioMessage(PTT=True,mimetype='audio/ogg',fileLength=5)))
    native=SimpleNamespace(download_any=lambda message:b'voice')
    r.whatsapp.receiver.live(native,incoming)
    wait_until(lambda:r.groups.messages('test@g.us') and r.groups.messages('test@g.us')[0]['body']=='Decoded voice')
    stored=r.groups.messages('test@g.us')[0]
    assert stored['msg_type']=='ptt'
    assert stored['has_media']==1


def test_expired_qr_restarts_only_python_worker_even_if_sdk_close_hangs(client,monkeypatch):
    from queue import Queue
    from app.escalation_analyst.whatsapp import client as controller
    created=[]
    class PrivateQueue(Queue):
        def cancel_join_thread(self): pass
        def close(self): pass
    class Process:
        def __init__(self,**options): self.states=options['args'][1];self.alive=False;self.terminated=False;created.append(self)
        def start(self):
            self.alive=True
            self.states.put(dict(state='qr_required',qrImage='test-only',expiresAt=int(time.time()*1000)+60000))
        def is_alive(self): return self.alive
        def join(self,timeout): pass  # Simulate a stuck native library.
        def terminate(self): self.terminated=True;self.alive=False
        def close(self): pass
    context=SimpleNamespace(Queue=PrivateQueue,Event=threading.Event,Process=Process)
    monkeypatch.setattr(controller,'get_context',lambda method:context)
    manager=controller.WhatsAppClient(client.app.state.runtime)
    try:
        manager.reconnect()
        assert manager.status()['qrImage']
        manager.states.put({'expiresAt':int(time.time()*1000)-1})
        assert manager.status()['qrImage'] is None
        assert manager.reconnect()['ok']
        assert len(created)==2 and created[0].terminated
        assert manager.status()['transport']=='python'
        assert manager.status()['qrImage']
    finally:
        manager.close()
