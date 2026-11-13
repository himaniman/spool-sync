"""Sync logic against a fake cloud: python -m unittest discover -s tests"""
import tempfile
import unittest
from pathlib import Path

from spool_sync.store import Store
from spool_sync.sync import Syncer

DEV = "PRINTER1"


def task(i, status, weight=34.62, start="2030-01-01T10:00:00Z", end="2030-01-01T11:00:00Z", cost=3600, ftype="PLA"):
    return {"id": i, "status": status, "title": f"print {i}", "weight": weight, "startTime": start, "endTime": end,
            "costTime": cost, "deviceId": DEV, "amsDetailMapping": [{"filamentType": ftype, "weight": weight}]}


def spool(i, net, in_printer=False, ftype="PLA", depleted=False):
    return {"id": i, "filamentName": f"{ftype} Basic", "filamentType": ftype, "color": "#00AE42FF",
            "netWeight": net, "totalNetWeight": 1000, "inPrinter": in_printer,
            "devId": DEV if in_printer else "", "slotId": "255" if in_printer else "", "depleted": depleted}


class FakeCloud:
    def __init__(self, tasks, spools):
        self._tasks, self._spools, self.puts = tasks, spools, []

    def tasks(self, limit=20):
        return list(self._tasks)

    def spools(self):
        return [dict(s) for s in self._spools]

    def put_spool(self, s):
        self.puts.append((s["id"], s["netWeight"]))
        for x in self._spools:
            if x["id"] == s["id"]:
                x.update(s, depleted=s["netWeight"] == 0 and x["depleted"])
                return dict(x)


class FakeTg:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class SyncTest(unittest.TestCase):
    def make(self, tasks, spools, since="2029-12-31T00:00:00+00:00"):
        self.cloud, self.tg = FakeCloud(tasks, spools), FakeTg()
        self.store = Store(Path(tempfile.mkdtemp()) / "t.db")
        self.store.set_since(since)
        return Syncer(self.cloud, self.store, self.tg, low_grams=100, remind_hours=3)

    def test_finished_print_is_deducted_once(self):
        s = self.make([task(1, 2)], [spool(10, 685, True), spool(11, 1000)])
        s.once()
        s.once()
        self.assertEqual(self.cloud.puts, [(10, 650)])
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("685 → 650", self.tg.sent[0])
        self.assertIn("зелёная", self.tg.sent[0])

    def test_history_before_cursor_is_ignored(self):
        s = self.make([task(1, 2, start="2029-01-01T00:00:00Z")], [spool(10, 685, True)])
        s.once()
        self.assertEqual(self.cloud.puts, [])

    def test_aborted_print_deducts_by_time(self):
        s = self.make([task(1, 3, weight=100, end="2030-01-01T10:24:00Z", cost=3600)], [spool(10, 500, True)])
        s.once()
        self.assertEqual(self.cloud.puts, [(10, 460)])
        self.assertIn("40 %", self.tg.sent[0])

    def test_no_spool_in_printer_waits_and_retries(self):
        spools = [spool(10, 500)]
        s = self.make([task(1, 2)], spools)
        s.once()
        s.once()  # within REMIND_HOURS: no second message
        self.assertEqual(self.cloud.puts, [])
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("не отмечена катушка", self.tg.sent[0])
        spools[0].update(inPrinter=True, devId=DEV, slotId="255")  # user fixed it in Handy
        s.once()
        self.assertEqual(self.cloud.puts, [(10, 465)])

    def test_type_mismatch_is_not_deducted(self):
        s = self.make([task(1, 2, ftype="PETG")], [spool(10, 500, True)])
        s.once()
        self.assertEqual(self.cloud.puts, [])
        self.assertIn("PETG", self.tg.sent[0])

    def test_running_print_warns_when_not_enough(self):
        s = self.make([task(1, 4, weight=60)], [spool(10, 48, True)])
        s.once()
        s.once()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Может не хватить", self.tg.sent[0])

    def test_running_print_is_quiet_when_enough(self):
        s = self.make([task(1, 4, weight=60)], [spool(10, 600, True)])
        s.once()
        self.assertEqual(self.tg.sent, [])

    def test_low_line_once_per_spool(self):
        s = self.make([task(1, 2, weight=30), task(2, 2, weight=10, start="2030-01-02T10:00:00Z")],
                      [spool(10, 120, True)])
        s.once()
        self.assertEqual(self.cloud.puts, [(10, 90), (10, 80)])
        self.assertIn("Осталось меньше 100 г", self.tg.sent[0])
        self.assertNotIn("Осталось меньше", self.tg.sent[1])

    def test_never_below_zero(self):
        s = self.make([task(1, 2, weight=80)], [spool(10, 50, True)])
        s.once()
        self.assertEqual(self.cloud.puts, [(10, 0)])

    def test_spool_zeroed_elsewhere_is_only_recorded(self):
        spools = [spool(10, 600, True)]
        s = self.make([], spools)
        s.once()
        spools[0].update(netWeight=0, depleted=True, updatedAt=111)
        s.once()
        self.assertEqual(self.cloud.puts, [])
        self.assertEqual([r[2] for r in self.store.history(10)], [0, 600])

    def test_history_records_changes(self):
        spools = [spool(10, 600)]
        s = self.make([], spools)
        s.once()
        s.once()  # unchanged: no new row
        spools[0]["netWeight"] = 550
        s.once()
        self.assertEqual([r[2] for r in self.store.history(10)], [550, 600])

    def test_dry_run_writes_nothing(self):
        s = self.make([task(1, 2)], [spool(10, 685, True)])
        s.dry = True
        s.once()
        self.assertEqual(self.cloud.puts, [])
        self.assertFalse(self.store.is_handled(1))


if __name__ == "__main__":
    unittest.main()
