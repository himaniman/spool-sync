"""Telegram: send-only, one HTTP POST per message (no webhook, no polling)."""
import json
import logging
import urllib.request

log = logging.getLogger("spool-sync")


class Telegram:
    def __init__(self, token, chat_id, dry=False):
        self.token, self.chat_id, self.dry = token, chat_id, dry

    def send(self, text):
        log.info("message: %s", text.replace("\n", " | "))
        if self.dry or not (self.token and self.chat_id):
            return True
        body = json.dumps({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r).get("ok", False)
        except Exception as e:  # never let a failed notification break a deduction
            log.error("telegram send failed: %s", str(e).replace(self.token, "<token>"))
            return False
