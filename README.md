# spool-sync

Keeps the remaining grams of Bambu Lab **Filament Manager** spools (Bambu Studio / Bambu Handy) up to date
for a printer **without AMS**: Bambu itself does not deduct prints from manually added, non-RFID spools on
the external holder. Messages go to Telegram.

## How it works

Every 5 minutes, through the Bambu cloud only (the printer can be anywhere):

1. `GET /v1/user-service/my/tasks` — print jobs sent **through the cloud** (Studio "Print", Handy). Each has
   the slicer's filament estimate `weight` (grams) and `status` (2 finished, 3 failed/aborted; anything else
   is still running).
2. `GET /v1/design-user-service/my/filament/v2` — Filament Manager spools. The loaded one is
   `inPrinter: true` with the printer's serial in `devId` (`slotId: "255"` = external spool).
3. A finished print deducts its full `weight`; an aborted one deducts `weight × real duration / estimated
   duration` (rough: heating counts as printing). A running print only triggers a warning when the spool
   has less than it needs.
4. `PUT /v1/design-user-service/my/filament/v2` with the whole spool object stores the new `netWeight`,
   then the spool is read back: the reply only echoes the body, and a spool flagged `depleted` silently
   ignores a new weight unless the flag is cleared too.
5. SQLite (`/data/spool-sync.db`) remembers handled jobs and sent alerts, so nothing is deducted twice.
   Prints that started before the first run are never touched.

Prints started from the SD card / printer screen never reach the cloud task list, and the printer does
not report their weight anywhere (it is only inside the file on the printer). A second thread listens to
the cloud MQTT broker (`us.mqtt.bambulab.com:8883`, user `u_{uid}`, password = the access token,
`device/{serial}/report`; one `pushall` per connect because P1 printers send deltas) and, when a print
with `print_type: local` / `task_id: 0` ends, only reminds to fix the spool by hand. Cloud prints report
`print_type: cloud` with `task_id` = the `my/tasks` id and are left to the sync pass. LAN-only mode is
not covered at all.

The service also keeps every change of every spool it sees (`history`), with the cloud's own
`updatedAt`, and marks its own writes `deduct` / `restore` — to tell later who changed a spool.

**Bambu zeroes the loaded spool.** A few minutes before a cloud print, Studio / Handy copy the printer's
external slot (`vt_tray.remain: 0`, `tray_weight: "0"` — it never knows the rest of a non-RFID spool) into
Filament Manager: the spool becomes 0 g and `depleted`. When a loaded spool turns 0 g + depleted and the
service last saw it with at least `LOW_GRAMS`, it writes the last known weight back (every time; the
message at most every `REMIND_HOURS`). A spool that really ran out is below `LOW_GRAMS` by then, or is taken
out of the printer in Handy — unloaded spools are never touched.

### Messages

| When | Text |
|---|---|
| print finished | ✅ «title» — списал 34.6 г / PLA Basic зелёная: 685 → 650 г |
| print aborted | ⏹ «title» остановлена примерно на 40 % — списал ≈14 г (оценка по времени) |
| below `LOW_GRAMS` | extra line 🔻 Осталось меньше 100 г. (once per spool until it is refilled) |
| running, not enough | ⚠️ «title» требует 60 г, а на … осталось 48 г. Может не хватить. |
| Bambu zeroed the loaded spool | 🛟 Bambu обнулил PLA Lite серая — вернул 840 г. |
| no / several spools marked in the printer, type mismatch | ❓ … — nothing deducted; retried every pass, reminded every `REMIND_HOURS` |
| SD card print ended | 🗂 «name» с флешки закончилась — вес не знаю, ничего не списал. Поправь остаток … в Handy. |
| login expires in `TOKEN_WARN_DAYS` | 🔑 Вход в Bambu истекает через N дн. |
| HTTP 401 | 🔑 Bambu не пускает (401). |
| cloud failing for `OUTAGE_ALERT_SEC` | 🛑 … and ✅ when it recovers |

## Run

```powershell
copy .env.example .env      # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
pip install -r requirements.txt
python -m spool_sync login            # email + password + the code Bambu emails; password is not stored
python -m spool_sync test-message
python -m spool_sync spools
python -m spool_sync once --dry-run   # first call only sets the cursor
python -m unittest discover -s tests
```

### Docker host

`docker compose up -d --build` (the image is built on the docker host; state lives in the `spool-data`
volume). Log in inside the container, or copy a token made locally:

```powershell
docker compose run --rm spool-sync login
Get-Content -Raw data\token.json | docker compose run --rm -T spool-sync import-token
docker compose run --rm spool-sync log 20    # what was deducted
docker logs -f spool-sync
```

The Bambu login lives ~90 days and cannot be refreshed (the refresh endpoint returns 401); the bot warns a
week before, then run `login` again.

## Caveats

- The whole API is **unofficial** and undocumented; Bambu can change or close it. Login may hit Cloudflare,
  hence `curl_cffi` with a browser fingerprint.
- `weight` is the slicer estimate, not a measurement.
- Telegram is send-only: no webhook, no commands.

## Sources

- [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) — cloud HTTP (login, `my/tasks` fields) and MQTT.
- The Filament Manager endpoint and its `PUT` were found here: the path is in OpenBambuAPI's undocumented
  list, the allowed methods come from the server's `405 Allow: GET, POST, PUT`, the field names from Bambu
  Studio's own `device_page` web bundle (`resources/web/device_page`).
