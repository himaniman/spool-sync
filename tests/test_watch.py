"""MQTT watcher state machine: python -m unittest discover -s tests"""
import unittest
from pathlib import Path

from spool_sync.watch import Watcher

SERIAL = "PRINTER1"
GREY = {"id": 1, "filamentName": "PLA Lite", "color": "#8C8B8CFF", "inPrinter": True, "devId": SERIAL}


def w():
    return Watcher(Path("nonexistent"), tg=None)


class WatchTest(unittest.TestCase):
    def test_local_print_finish_reminds_once(self):
        x = w()
        full = {"gcode_state": "RUNNING", "print_type": "local", "task_id": "0",
                "subtask_name": "grin tank hp.gcode.3mf", "mc_percent": 98}
        self.assertIsNone(x.on_report(SERIAL, full, lambda: [GREY]))
        text = x.on_report(SERIAL, {"gcode_state": "FINISH", "mc_percent": 100}, lambda: [GREY])  # delta only
        self.assertIn("«grin tank hp» с флешки закончилась", text)
        self.assertIn("PLA Lite серая", text)
        self.assertIsNone(x.on_report(SERIAL, {"gcode_state": "FINISH"}, lambda: [GREY]))

    def test_local_print_failed_says_percent(self):
        x = w()
        x.on_report(SERIAL, {"gcode_state": "RUNNING", "print_type": "local", "task_id": "0",
                             "subtask_name": "x.gcode.3mf", "mc_percent": 37})
        self.assertIn("остановлена на 37 %", x.on_report(SERIAL, {"gcode_state": "FAILED"}, lambda: []))

    def test_cloud_print_is_left_to_sync(self):
        x = w()
        x.on_report(SERIAL, {"gcode_state": "RUNNING", "print_type": "cloud", "task_id": "1437818303",
                             "subtask_name": "павук"})
        self.assertIsNone(x.on_report(SERIAL, {"gcode_state": "FINISH"}, lambda: [GREY]))

    def test_finish_seen_first_after_connect_is_quiet(self):
        x = w()
        self.assertIsNone(x.on_report(SERIAL, {"gcode_state": "FINISH", "print_type": "local", "task_id": "0"}))


if __name__ == "__main__":
    unittest.main()
