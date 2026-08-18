"""Windows entry point for the standalone calibration GUI."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from core.models import ApplicationConfig
from gui.main_window import MainWindow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Arducam IMX179, Arduino Nano/HX711, and Ender 3 calibration GUI"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config" / "default_config.json",
        help="validated application JSON configuration",
    )
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="preselect clearly labeled synthetic camera and HX711 sources",
    )
    parser.add_argument(
        "--offscreen-smoke-ms",
        type=int,
        default=0,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.offscreen_smoke_ms < 0:
        raise ValueError("--offscreen-smoke-ms must be nonnegative")
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication(sys.argv[:1])
    application.setApplicationName("SPARE Calibration GUI")
    application.setOrganizationName("DLSU")
    config = ApplicationConfig.load_json(args.config)
    window = MainWindow(config, default_simulation=args.simulation)
    window.show()
    if args.offscreen_smoke_ms:
        QTimer.singleShot(args.offscreen_smoke_ms, window.close)
        QTimer.singleShot(args.offscreen_smoke_ms + 250, application.quit)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
