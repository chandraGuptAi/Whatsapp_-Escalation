"""Python worker lifecycle and private queues; no Node service or HTTP bridge."""
from multiprocessing import get_context
from queue import Empty
import threading
import time
from uuid import uuid4
from ..errors import EscalationError
from .worker import run_native


class WhatsAppClient:
    def __init__(self, runtime):
        self.settings = runtime.settings
        self.process = None
        self._lifecycle = threading.RLock()
        self._rpc = threading.Lock()
        self._queues = []
        self._status = dict(state="disconnected", transport="python", qrImage=None, expiresAt=None,
                            message="Connect WhatsApp to pair this Python service.", revision=0)

    def status(self) -> dict:
        with self._lifecycle:
            if self._queues:
                while True:
                    try:
                        self._status.update(self.states.get_nowait())
                    except Empty:
                        break
            result = dict(self._status)
            if self.process and not self.process.is_alive() and result["state"] != "error":
                result.update(state="disconnected", qrImage=None, expiresAt=None,
                              message="Python WhatsApp worker stopped. Reconnect to continue.")
            if result["expiresAt"] and result["expiresAt"] <= time.time() * 1000:
                result.update(qrImage=None, message="This QR expired. Retry to request a fresh code.")
            return result

    def reconnect(self) -> dict:
        with self._lifecycle:
            state = self.status()
            if self.process and self.process.is_alive() and (
                state["state"] in ("ready", "initializing") or state["qrImage"]
            ):
                return {"ok": True, "state": state["state"]}
            self._stop()
            context = get_context("spawn")
            self.states, self.commands, self.replies = [context.Queue() for _ in range(3)]
            self._queues = [self.states, self.commands, self.replies]
            self.stop_event = context.Event()
            self._status.update(state="initializing", qrImage=None, expiresAt=None,
                                message="Starting the Python WhatsApp client...")
            self.process = context.Process(target=run_native,
                args=(self.settings, self.states, self.commands, self.replies, self.stop_event),
                name="python-whatsapp", daemon=True)
            self.process.start()
            return {"ok": True, "state": "initializing"}

    def _stop(self):
        if self.process:
            self.stop_event.set()
            self.process.join(timeout=0.5)
            if self.process.is_alive():
                # The native SDK can remain blocked after a QR timeout. Terminate
                # only its Python worker; SQLite and OS session locks recover safely.
                self.process.terminate()
                self.process.join(timeout=3)
            if self.process.is_alive():
                raise EscalationError(503, "Previous WhatsApp worker is still closing. Retry shortly.")
            self.process.close()
            self.process = None
        for queue in self._queues:
            queue.cancel_join_thread()
            queue.close()
        self._queues = []

    def close(self):
        with self._lifecycle:
            self._stop()
            self._status.update(state="disconnected", qrImage=None, expiresAt=None,
                                message="Python WhatsApp service stopped.")

    def _command(self, method: str, body: dict) -> dict:
        with self._rpc, self._lifecycle:
            if self.status()["state"] != "ready":
                raise EscalationError(503, "WhatsApp is not connected")
            request_id = uuid4().hex
            self.commands.put({"id": request_id, "method": method, "body": body})
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    reply = self.replies.get(timeout=min(0.5, max(0.01, deadline - time.monotonic())))
                except Empty:
                    continue
                if reply["id"] == request_id:
                    if reply.get("error"):
                        raise EscalationError(503, reply["error"])
                    return reply["value"]
            raise EscalationError(503, "WhatsApp request timed out. Retry shortly.")

    def refresh_names(self) -> dict:
        return self._command("refresh", {})

    def send(self, recipient: str, text: str) -> dict:
        return self._command("send", {"recipient": recipient, "text": text})
