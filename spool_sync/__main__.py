"""spool-sync CLI.

  login                 interactive Bambu login (email, password, email code); token -> $DATA/token.json
  import-token          read a token.json (or a bare access token) from stdin into $DATA/token.json
  run                   the service: a sync pass every INTERVAL_SEC, health alerts
  once [--dry-run]      one sync pass; --dry-run writes nothing to Bambu, the DB or Telegram
  spools                Filament Manager spools and which one is in the printer
  setw <id> <grams>     set a spool's remaining grams
  log [N]               last N journal entries (what was deducted)
  history [id [N]]      every change of the spools seen by the service (what was set, when)
  test-message          send a test message to Telegram

Config is environment variables (a local .env is read too), see .env.example.
"""
import getpass
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import bambu as bb
from .notify import Telegram
from .store import Store
from .sync import Syncer, spool_label

log = logging.getLogger("spool-sync")


def load_dotenv(path=Path(".env")):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
DATA = Path(os.environ.get("SPOOL_DATA", "data"))
TOKEN_FILE = DATA / "token.json"
INTERVAL = int(os.environ.get("INTERVAL_SEC", "300"))
LOW = int(os.environ.get("LOW_GRAMS", "100"))
REMIND_HOURS = float(os.environ.get("REMIND_HOURS", "3"))
TOKEN_WARN_DAYS = int(os.environ.get("TOKEN_WARN_DAYS", "7"))
OUTAGE_ALERT_SEC = int(os.environ.get("OUTAGE_ALERT_SEC", "3600"))
PRINTER = os.environ.get("PRINTER_SERIAL") or None
TZ = ZoneInfo(os.environ.get("TZ") or "Asia/Ho_Chi_Minh")


def telegram(dry=False):
    return Telegram(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"), dry=dry)


def client():
    tok = bb.load_token(TOKEN_FILE)
    if not tok:
        sys.exit(f"no {TOKEN_FILE} - run login or import-token first")
    return bb.Bambu(tok["accessToken"])


def syncer(dry=False):
    return Syncer(client(), Store(DATA / "spool-sync.db"), telegram(dry), LOW, REMIND_HOURS, PRINTER, dry)


def cmd_login():
    email = input("Bambu account email: ").strip()
    password = getpass.getpass("Password (not stored): ")
    token, expires = bb.login(email, password, lambda p: input(p).strip())
    bb.save_token(TOKEN_FILE, token, expires)
    print(f"token saved to {TOKEN_FILE}, valid ~{bb.token_days_left(bb.load_token(TOKEN_FILE)):.0f} days")


def cmd_import_token():
    raw = sys.stdin.read().strip().lstrip("﻿")  # PowerShell pipes prepend a BOM
    try:
        j = json.loads(raw)
        token, saved = j["accessToken"], j.get("savedAt")
    except (ValueError, KeyError, TypeError):
        token, saved = raw, None
    bb.save_token(TOKEN_FILE, token, j.get("expiresIn") if saved else None)
    if saved:  # keep the original login time so the expiry warning stays honest
        t = bb.load_token(TOKEN_FILE)
        t["savedAt"] = saved
        TOKEN_FILE.write_text(json.dumps(t))
    print(f"token stored, ~{bb.token_days_left(bb.load_token(TOKEN_FILE)):.0f} days left")


def cmd_spools():
    for s in client().spools():
        where = f"IN {s.get('deviceName')} slot {s.get('slotId')}" if s.get("inPrinter") else ""
        print(f"#{s['id']:<10} {spool_label(s):<28} {s.get('netWeight'):>5}/{s.get('totalNetWeight')} g"
              f"  {'depleted' if s.get('depleted') else '':<9} {where}")


def cmd_setw(spool_id, grams):
    c = client()
    s = next((x for x in c.spools() if str(x["id"]) == str(spool_id)), None)
    if not s:
        sys.exit(f"no spool {spool_id}")
    before = s.get("netWeight")
    back = c.put_spool({**s, "netWeight": int(round(float(grams)))})
    print(f"#{s['id']} {spool_label(s)}: {before} -> {back['netWeight']} g")


def cmd_log(n):
    for row in Store(DATA / "spool-sync.db").journal(n):
        at, tid, status, title, sid, grams, before, after, note = row
        print(f"{at}  task {tid} st={status} {title!r}  spool {sid}: {before} -> {after} (-{grams} g) {note or ''}")


def cmd_history(spool_id, n):
    for seen, sid, net, inp, dep, upd, src in Store(DATA / "spool-sync.db").history(spool_id, n):
        changed = datetime.fromtimestamp(upd, TZ).strftime("%m-%d %H:%M:%S") if upd else "?"
        print(f"seen {seen}  spool {sid}: {net:>5} g  {'in printer' if inp else '':<10} "
              f"{'depleted' if dep else '':<8}  changed in cloud {changed}  ({src})")


def check_token(store, tg):
    tok = bb.load_token(TOKEN_FILE)
    if not tok:
        return
    left = bb.token_days_left(tok)
    if left <= TOKEN_WARN_DAYS and store.alert_due(f"tokenexp:{tok['savedAt']}", 86400):
        tg.send(f"🔑 Вход в Bambu истекает через {max(0, int(left))} дн. Запусти spool-sync login.")
        store.alert_sent(f"tokenexp:{tok['savedAt']}")


def cmd_run():
    logging.info("spool-sync started: every %ss, low %s g, printer %s", INTERVAL, LOW, PRINTER or "any")
    store, tg = Store(DATA / "spool-sync.db"), telegram()
    failing_since = None
    while True:
        try:
            check_token(store, tg)
            syncer().once()
            if failing_since and store.alert_exists("outage"):
                tg.send("✅ spool-sync: облако Bambu снова отвечает.")
                store.alert_clear("outage")
            failing_since = None
        except bb.AuthError as e:
            log.error("%s", e)
            tok = bb.load_token(TOKEN_FILE) or {}
            key = "auth:" + hashlib.sha256(tok.get("accessToken", "").encode()).hexdigest()[:12]
            if store.alert_due(key):
                tg.send("🔑 Bambu не пускает (401). Нужен новый вход, списания стоят.")
                store.alert_sent(key)
        except SystemExit as e:  # no token file yet
            log.error("%s", e)
        except Exception as e:  # cloud down, API changed, ...
            log.exception("sync pass failed: %s", e)
            failing_since = failing_since or time.time()
            if time.time() - failing_since >= OUTAGE_ALERT_SEC and store.alert_due("outage"):
                since = datetime.fromtimestamp(failing_since, TZ).strftime("%H:%M")
                tg.send(f"🛑 spool-sync: облако Bambu не отвечает с {since}.\n{type(e).__name__}: {str(e)[:200]}")
                store.alert_sent("outage")
        time.sleep(INTERVAL)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    a = sys.argv[1:] or ["help"]
    cmd = a[0]
    if cmd == "login":
        cmd_login()
    elif cmd == "import-token":
        cmd_import_token()
    elif cmd == "run":
        cmd_run()
    elif cmd == "once":
        syncer(dry="--dry-run" in a).once()
    elif cmd == "spools":
        cmd_spools()
    elif cmd == "setw" and len(a) == 3:
        cmd_setw(a[1], a[2])
    elif cmd == "log":
        cmd_log(int(a[1]) if len(a) > 1 else 20)
    elif cmd == "history":
        cmd_history(a[1] if len(a) > 1 else None, int(a[2]) if len(a) > 2 else 30)
    elif cmd == "test-message":
        print("sent" if telegram().send("🧵 spool-sync: тестовое сообщение, связь есть.") else "FAILED")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
