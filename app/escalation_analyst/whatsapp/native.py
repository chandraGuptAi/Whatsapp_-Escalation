"""In-process Python WhatsApp client. No Node process, browser or HTTP bridge."""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
import threading
import time
from uuid import uuid4
from filelock import FileLock, Timeout
from ..errors import EscalationError
from .messages import MessageReceiver, jid

logger = logging.getLogger("escalation_analyst.whatsapp")


class NativeWhatsApp:
    def __init__(self, runtime, publish=None):
        self.r = runtime
        self.publish = publish
        self.client = None
        self.worker = None
        self.receiver = MessageReceiver(runtime)
        self.path = Path(runtime.settings.whatsapp_session_path).resolve()
        self._state_lock = threading.RLock()
        self._lifecycle = threading.Lock()
        self._closing = False
        self._qr_number = 0
        self._status = dict(state="disconnected", qrImage=None, expiresAt=None,
                            message="Connect WhatsApp to pair this Python service.", revision=0)

    def set_status(self, state: str, message: str | None = None):
        with self._state_lock:
            self._status.update(state=state, message=message, qrImage=None, expiresAt=None)
            if self.publish: self.publish(dict(self._status))

    def status(self) -> dict:
        with self._state_lock:
            result = dict(self._status)
        if result["expiresAt"] and result["expiresAt"] <= time.time() * 1000:
            result.update(qrImage=None, message="This QR expired. Retry to request a fresh code.")
        return result

    def qr(self, client, value: bytes):
        # Neonize 0.5.2 passes raw QR data to event.qr, not an image.
        import segno
        output = io.BytesIO()
        segno.make_qr(value).save(output, kind="svg", scale=5)
        with self._state_lock:
            if self._closing:
                return
            self._qr_number += 1
            self._status.update(state="qr_required", message="Scan using WhatsApp > Linked Devices > Link a device.",
                                qrImage="data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode(),
                                expiresAt=int(time.time() * 1000) + (60000 if self._qr_number == 1 else 20000),
                                revision=self._status["revision"] + 1)
            if self.publish: self.publish(dict(self._status))

    def _connected(self, client, event):
        if not self._closing:
            self.set_status("ready")
            self.r.executor.submit(self._refresh_safe)

    def _refresh_safe(self):
        try:
            self.refresh_names()
        except Exception:
            logger.exception("Python WhatsApp group-name refresh failed")

    def _register(self, client):
        from neonize.events import (ConnectedEv, DisconnectedEv, LoggedOutEv, MessageEv, HistorySyncEv,
                                    StreamReplacedEv, ConnectFailureEv, ClientOutdatedEv, PairStatusEv,
                                    JoinedGroupEv, GroupInfoEv)
        client.event.qr(self.qr)
        client.event(ConnectedEv)(self._connected)
        client.event(MessageEv)(self.receiver.live)
        client.event(HistorySyncEv)(lambda c, e: self.r.executor.submit(self.receiver.history, c, e))
        client.event(DisconnectedEv)(lambda c, e: self.set_status("disconnected", "Connection lost. WhatsApp will try to reconnect; saved messages remain available."))
        client.event(LoggedOutEv)(lambda c, e: self.set_status("disconnected", "WhatsApp was logged out. Connect again to scan a fresh QR."))
        client.event(StreamReplacedEv)(lambda c, e: self.set_status("error", "Another service opened this WhatsApp session. Run one Python WhatsApp service."))
        client.event(ConnectFailureEv)(lambda c, e: self.set_status("error", "WhatsApp connection failed. Retry the connection."))
        client.event(ClientOutdatedEv)(lambda c, e: self.set_status("error", "The Python WhatsApp client needs an update."))
        client.event(PairStatusEv)(lambda c, e: self.set_status("error", "Pairing failed. Request a new QR and scan again.") if e.Error else None)
        for event in (JoinedGroupEv, GroupInfoEv):
            client.event(event)(lambda c, e: self.r.executor.submit(self._refresh_safe))

    def _run(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Only one backend worker may own a WhatsApp session.
            with FileLock(str(self.path) + ".lock", timeout=0):
                from neonize.client import NewClient
                client = NewClient(str(self.path), uuid="erp-whatsapp-" + uuid4().hex)
                self.client = client
                self._register(client)
                client.connect()
        except Timeout:
            self.set_status("error", "Another Python worker owns this WhatsApp session. Run one backend worker.")
        except Exception:
            logger.exception("Python WhatsApp connection failed")
            self.set_status("error", "WhatsApp could not start. Check the Python service logs and retry.")
        finally:
            if not self._closing and self.status()["state"] not in ("error", "disconnected"):
                self.set_status("disconnected", "WhatsApp stopped. Connect again to continue.")

    def reconnect(self) -> dict:
        with self._lifecycle:
            state = self.status()
            if self.worker and self.worker.is_alive():
                if state["state"] in ("ready", "initializing") or state["qrImage"]:
                    return {"ok": True, "state": state["state"]}
                self.client.stop()
                self.worker.join(timeout=5)
                if self.worker.is_alive():
                    raise EscalationError(503, "Previous WhatsApp connection is still closing. Retry shortly.")
            self._closing = False
            self._qr_number = 0
            self.set_status("initializing", "Starting the Python WhatsApp client...")
            self.worker = threading.Thread(target=self._run, name="python-whatsapp", daemon=True)
            self.worker.start()
            return {"ok": True, "state": "initializing"}

    def close(self):
        with self._lifecycle:
            self._closing = True
            if self.client:
                self.client.stop()
            if self.worker:
                self.worker.join(timeout=5)
            self.set_status("disconnected", "Python WhatsApp service stopped.")

    def refresh_names(self) -> dict:
        if self.status()["state"] != "ready":
            raise EscalationError(503, "Connect WhatsApp before refreshing groups.")
        groups = self.client.get_joined_groups()
        for group in groups:
            self.r.groups.learn(jid(group.JID), group.GroupName.Name)
        return {"ok": True, "updated": len(groups), "authors": self.receiver.rename_authors(self.client)}

    def send(self, recipient: str, text: str) -> dict:
        if self.status()["state"] != "ready":
            raise EscalationError(503, "WhatsApp is not connected")
        from neonize.utils.jid import build_jid
        address = jid(recipient)
        user, server = address.split("@", 1) if "@" in address else (address, "s.whatsapp.net")
        self.client.send_message(build_jid(user, server), text)
        return {"ok": True}
