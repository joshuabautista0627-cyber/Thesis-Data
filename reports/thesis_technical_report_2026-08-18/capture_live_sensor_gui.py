from __future__ import annotations

import os
from pathlib import Path
import sys


os.environ.setdefault("QT_QPA_PLATFORM", "windows")

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "spare_calibration_gui"
sys.path.insert(0, str(PROJECT))

from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui.live_sensor_tab import LiveSensorTab  # noqa: E402


app = QApplication.instance() or QApplication([])
app.setFont(QFont("Arial", 10))
tab = LiveSensorTab(PROJECT / "models" / "live_sensor_experimental_hybrid_v4")
tab.resize(1080, 1380)
tab.show()
app.processEvents()

# Exercise the shipped, hardware-free replay path so the screenshot contains a
# real completed-event presentation rather than a hand-built mock-up.
tab.start_demo()
tab._demo_timer.stop()
while tab._demo_mode:
    tab._advance_demo()
if tab._last_completed_result is not None:
    tab._render(tab._last_completed_result)
app.processEvents()

output = Path(__file__).resolve().parent / "charts" / "chart_live_sensor_gui.png"
output.parent.mkdir(parents=True, exist_ok=True)
if not tab.grab().save(str(output), "PNG"):
    raise RuntimeError(f"could not save {output}")

print(output)
