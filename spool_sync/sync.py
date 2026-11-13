"""One sync pass: new cloud print tasks -> deduct from the spool Filament Manager marks as loaded -> Telegram."""
import logging
from datetime import datetime, timezone

log = logging.getLogger("spool-sync")

# feminine: "катушка зелёная"
PALETTE = {
    "чёрная": (0, 0, 0), "белая": (255, 255, 255), "серая": (128, 128, 128), "красная": (192, 0, 0),
    "оранжевая": (255, 128, 0), "жёлтая": (240, 224, 64), "зелёная": (0, 160, 64), "голубая": (64, 176, 240),
    "синяя": (0, 64, 192), "фиолетовая": (128, 64, 192), "розовая": (240, 128, 192),
    "коричневая": (128, 80, 32), "бежевая": (224, 200, 160),
}


def color_name(hexcolor):
    h = (hexcolor or "").lstrip("#")[:6]
    if len(h) != 6:
        return ""
    rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return min(PALETTE, key=lambda n: sum((a - b) ** 2 for a, b in zip(PALETTE[n], rgb)))


def spool_label(s):
    return f"{s.get('filamentName') or s.get('filamentType')} {color_name(s.get('color'))}".strip()


def g(x):
    return f"{x:.1f}".rstrip("0").rstrip(".")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


class Syncer:
    def __init__(self, bambu, store, tg, low_grams=100, remind_hours=3, printer=None, dry=False):
        self.bambu, self.store, self.tg = bambu, store, tg
        self.low, self.remind, self.printer, self.dry = low_grams, remind_hours * 3600, printer, dry

    def once(self):
        since = self.store.since()
        if not since:
            since = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if not self.dry:
                self.store.set_since(since)
            log.info("first run: cursor set to %s, only prints started after it are deducted", since)
            return
        since = ts(since)
        tasks = self.bambu.tasks(20)
        spools = self.bambu.spools()
        for t in sorted(tasks, key=lambda t: t.get("startTime") or ""):
            start = ts(t.get("startTime"))
            if not start or start < since or self.store.is_handled(t["id"]):
                continue
            if self.printer and t.get("deviceId") != self.printer:
                continue
            if t.get("status") in (2, 3):
                self._finish(t, spools)
            else:
                self._start(t, spools)
        for s in spools:  # a refilled / new spool re-arms its low-weight alert
            if (s.get("netWeight") or 0) >= self.low and self.store.alert_exists(f"low:{s['id']}"):
                self.store.alert_clear(f"low:{s['id']}")

    # -- spool choice ----------------------------------------------------------------------------------

    def _pick(self, t, spools):
        """Return (spool, None) or (None, (kind, message))."""
        title, w = t.get("title"), float(t.get("weight") or 0)
        loaded = [s for s in spools if s.get("inPrinter") and s.get("devId") == t.get("deviceId")]
        if len(loaded) > 1:
            external = [s for s in loaded if str(s.get("slotId")) == "255"]
            loaded = external if len(external) == 1 else loaded
        if not loaded:
            return None, ("nospool", f"❓ «{title}», {g(w)} г: в Handy не отмечена катушка в принтере. "
                                     f"Отметь её — спишу автоматически.")
        if len(loaded) > 1:
            names = ", ".join(spool_label(s) for s in loaded)
            return None, ("many", f"❓ «{title}», {g(w)} г: в принтере отмечено несколько катушек ({names}). "
                                  f"Оставь одну — спишу автоматически.")
        s = loaded[0]
        types = {m.get("filamentType") for m in t.get("amsDetailMapping") or []} - {None, ""}
        if types and s.get("filamentType") not in types:
            return None, ("type", f"❓ «{title}» печаталась {'/'.join(sorted(types))}, а в принтере числится "
                                  f"{spool_label(s)} ({s.get('filamentType')}). Ничего не списал — поправь катушку.")
        return s, None

    def _problem(self, t, problem):
        kind, text = problem
        key = f"problem:{t['id']}:{kind}"
        log.warning("task %s: %s", t["id"], kind)
        if self.store.alert_due(key, self.remind):
            self.tg.send(text)
            if not self.dry:
                self.store.alert_sent(key)

    # -- running print: early warnings ----------------------------------------------------------------

    def _start(self, t, spools):
        key = f"start:{t['id']}"
        if not self.store.alert_due(key):
            return
        s, problem = self._pick(t, spools)
        if problem:
            self._problem(t, problem)
        else:
            w = float(t.get("weight") or 0)
            if w > (s.get("netWeight") or 0):
                self.tg.send(f"⚠️ «{t.get('title')}» требует {g(w)} г, а на {spool_label(s)} "
                             f"осталось {s.get('netWeight')} г. Может не хватить.")
        if not self.dry:
            self.store.alert_sent(key)

    # -- finished / aborted print: deduct --------------------------------------------------------------

    def _finish(self, t, spools):
        title, status, weight = t.get("title"), t.get("status"), float(t.get("weight") or 0)
        if weight <= 0:
            log.info("task %s %r has no weight, nothing to deduct", t["id"], title)
            if not self.dry:
                self.store.add_handled(t["id"], status, title, None, 0, None, None, "no weight")
            return
        s, problem = self._pick(t, spools)
        if problem:
            return self._problem(t, problem)

        if status == 2:
            grams, head = weight, f"✅ «{title}» — списал {g(weight)} г"
        else:
            start, end, cost = ts(t.get("startTime")), ts(t.get("endTime")), float(t.get("costTime") or 0)
            if not (start and end and cost > 0):
                self.tg.send(f"⏹ «{title}» остановлена — сколько ушло, не знаю (нет расчётного времени). "
                             f"Ничего не списал, поправь остаток {spool_label(s)} в Handy.")
                if not self.dry:
                    self.store.add_handled(t["id"], status, title, s["id"], 0, s.get("netWeight"),
                                           s.get("netWeight"), "aborted, no costTime")
                return
            ratio = min(1.0, max(0.0, (end - start).total_seconds() / cost))
            grams = weight * ratio
            head = (f"⏹ «{title}» остановлена примерно на {round(ratio * 100)} % — "
                    f"списал ≈{g(grams)} г (оценка по времени)")

        before = int(s.get("netWeight") or 0)
        after = max(0, int(round(before - grams)))
        if not self.dry:
            s.update(self.bambu.put_spool({**s, "netWeight": after}))  # keeps the in-memory list current
            self.store.add_handled(t["id"], status, title, s["id"], round(grams, 2), before, after)
        text = f"{head}\n{spool_label(s)}: {before} → {after} г"
        if after < self.low and self.store.alert_due(f"low:{s['id']}"):
            text += f"\n🔻 Осталось меньше {self.low} г."
            if not self.dry:
                self.store.alert_sent(f"low:{s['id']}")
        self.tg.send(text)
