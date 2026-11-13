"""Cloud MQTT watcher: tells about prints that the cloud task list never sees (SD card / printer screen).

A cloud print reports print_type "cloud" and task_id = the my/tasks id (spool-sync deducts those);
a local one reports print_type "local" and task_id "0" — its weight lives only inside the file on the
printer, so all we can do is remind. P1 printers send deltas, so the state is merged per printer.
"""
import json
import logging
import ssl
import threading
import time
import uuid

import paho.mqtt.client as mqtt

from . import bambu as bb
from .sync import spool_label

log = logging.getLogger("spool-sync")
BROKER = "us.mqtt.bambulab.com"
ACTIVE = {"PREPARE", "RUNNING", "PAUSE", "SLICING"}


def is_local(st):
    return st.get("print_type") == "local" or str(st.get("task_id") or "0") == "0"


def job_name(st):
    name = st.get("subtask_name") or st.get("gcode_file") or "?"
    for ext in (".gcode.3mf", ".3mf", ".gcode"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


class Watcher:
    def __init__(self, token_file, tg, printer=None):
        self.token_file, self.tg, self.printer = token_file, tg, printer
        self.state = {}  # serial -> merged push_status

    # -- message handling (pure, testable) ------------------------------------------------------------

    def on_report(self, serial, p, spools=None):
        """Merge one push_status; return the reminder text when a local print just ended, else None."""
        st = self.state.setdefault(serial, {})
        before = st.get("gcode_state")
        st.update(p)
        after = st.get("gcode_state")
        if before not in ACTIVE or after not in ("FINISH", "FAILED") or not is_local(st):
            return None
        loaded = [s for s in (spools() if spools else []) if s.get("inPrinter") and s.get("devId") == serial]
        target = spool_label(loaded[0]) if len(loaded) == 1 else "катушки"
        if after == "FINISH":
            head = f"🗂 «{job_name(st)}» с флешки закончилась"
        else:
            head = f"🗂 «{job_name(st)}» с флешки остановлена на {st.get('mc_percent', '?')} %"
        return f"{head} — вес не знаю, ничего не списал. Поправь остаток {target} в Handy."

    # -- connection -----------------------------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._forever, name="mqtt", daemon=True).start()

    def _forever(self):
        while True:
            try:
                self._session()
            except Exception as e:
                log.warning("mqtt: %s: %s", type(e).__name__, e)
            time.sleep(60)

    def _session(self):
        """One connection; returns when the token file changes or the link stays down, to rebuild it."""
        tok = bb.load_token(self.token_file)
        if not tok:
            return
        api = bb.Bambu(tok["accessToken"])
        uid = api.uid()
        serials = [self.printer] if self.printer else api.device_serials()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"spool-sync-{uuid.uuid4().hex[:8]}")
        c.username_pw_set(f"u_{uid}", tok["accessToken"])
        c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        c.reconnect_delay_set(5, 300)

        def on_connect(cl, _u, _f, rc, _p=None):
            log.info("mqtt connected (%s), watching %d printer(s)", rc, len(serials))
            for s in serials:
                cl.subscribe(f"device/{s}/report")
                # one full status per connect, so a print already running is known (P1 sends deltas only)
                cl.publish(f"device/{s}/request", json.dumps(
                    {"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}}))

        def on_message(_cl, _u, m):
            try:
                p = json.loads(m.payload).get("print") or {}
                if p.get("command") != "push_status":
                    return
                text = self.on_report(m.topic.split("/")[1], p, api.spools)
                if text:
                    self.tg.send(text)
            except Exception as e:
                log.warning("mqtt message: %s: %s", type(e).__name__, e)

        c.on_connect, c.on_message = on_connect, on_message
        c.connect(BROKER, 8883, keepalive=60)
        c.loop_start()
        down_since = None
        try:
            while True:
                time.sleep(30)
                if bb.load_token(self.token_file) != tok:
                    log.info("mqtt: token changed, reconnecting")
                    return
                down_since = None if c.is_connected() else (down_since or time.time())
                if down_since and time.time() - down_since > 600:
                    log.warning("mqtt: down for 10 min, rebuilding the session")
                    return
        finally:
            c.loop_stop()
            c.disconnect()
