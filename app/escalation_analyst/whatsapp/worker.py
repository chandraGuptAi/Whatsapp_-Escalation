"""Own the native library in one Python process, isolated from the HTTP backend."""
from queue import Empty
import threading
from multiprocessing import parent_process
import os


def run_native(settings, states, commands, replies, stop):
    from ..runtime import Runtime
    from .native import NativeWhatsApp

    runtime = Runtime(settings)
    transport = NativeWhatsApp(runtime, publish=states.put)
    runtime.whatsapp = transport

    def watch_parent():
        parent = parent_process()
        while not stop.wait(1):
            if parent and not parent.is_alive():
                os._exit(0)  # Release the session if Windows force-stops the API process.

    threading.Thread(target=watch_parent, name="whatsapp-parent", daemon=True).start()

    def control():
        while not stop.is_set():
            try:
                request = commands.get(timeout=0.2)
            except Empty:
                continue
            try:
                if request["method"] == "send":
                    value = transport.send(**request["body"])
                else:
                    value = transport.refresh_names()
                replies.put({"id": request["id"], "value": value})
            except Exception as exc:
                replies.put({"id": request["id"], "error": getattr(exc, "message", "WhatsApp operation failed")})

    threading.Thread(target=control, name="whatsapp-control", daemon=True).start()
    try:
        transport._run()
    finally:
        stop.set()
        runtime.close()
