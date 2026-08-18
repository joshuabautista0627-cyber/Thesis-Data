"""Cautious opt-in Ender 3 homing and small-jog hardware smoke test.

This is intentionally separate from the automated test suite. It requires an
explicit command-line clearance acknowledgement, keeps COM3 reserved for the
Nano/HX711, and saves the exact COM4 command/response history. The motion is:
G28, Z +1.0 mm, X +1.0 mm, Y +1.0 mm, Z -0.1 mm, and Z +0.1 mm.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
import platform
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.models import PrinterConfig
from data.exporter import atomic_write_json
from services.printer_service import PrinterSerialService, enumerate_printer_ports


def run_motion_smoke(
    *,
    printer_port: str,
    loadcell_port: str,
    baud_rate: int,
) -> dict[str, object]:
    if printer_port.strip().casefold() == loadcell_port.strip().casefold():
        raise ValueError("printer and load-cell ports must be different")
    ports = enumerate_printer_ports()
    devices = {str(item["device"]).casefold(): item for item in ports}
    if printer_port.casefold() not in devices:
        raise RuntimeError(f"printer port {printer_port} was not enumerated")
    if loadcell_port.casefold() not in devices:
        raise RuntimeError(f"load-cell port {loadcell_port} was not enumerated")

    service = PrinterSerialService()
    started = datetime.now(UTC).isoformat()
    stages: list[dict[str, object]] = []
    try:
        info = service.connect(
            PrinterConfig(port=printer_port, baud_rate=int(baud_rate)),
            port=printer_port,
        )
        stages.append({"stage": "connected_m115", "connection": asdict(info)})

        before = service.query_position()
        stages.append({"stage": "position_before_home", "position": asdict(before)})

        homed = service.home_all()
        stages.append({"stage": "home_all_complete", "position": asdict(homed)})

        planned_jogs = (
            ("Z", 1.0, 120.0, "positive Z away from sensor"),
            ("X", 1.0, 600.0, "small positive X"),
            ("Y", 1.0, 600.0, "small positive Y"),
            ("Z", -0.1, 60.0, "very small negative Z direction check"),
            ("Z", 0.1, 60.0, "return Z to pre-direction-check height"),
        )
        for axis, delta_mm, feed, purpose in planned_jogs:
            service.jog(axis, delta_mm, feed_rate_mm_min=feed)
            stages.append(
                {
                    "stage": "jog_complete",
                    "axis": axis,
                    "delta_mm": delta_mm,
                    "feed_rate_mm_min": feed,
                    "purpose": purpose,
                    "position": asdict(service.position),
                }
            )

        diagnostics = service.diagnostics()
        commands = [str(item.get("gcode", "")) for item in diagnostics["command_history"]]
        if any(command.upper().startswith("G92") for command in commands):
            raise RuntimeError("prohibited G92 was emitted")
        return {
            "test_type": "physical_cautious_printer_motion_smoke",
            "started_utc": started,
            "finished_utc": datetime.now(UTC).isoformat(),
            "operating_system": platform.platform(),
            "printer_port": printer_port,
            "loadcell_port_reserved_not_opened": loadcell_port,
            "ports_are_separate": True,
            "enumerated_ports": list(ports),
            "stages": stages,
            "commands_actually_sent": commands,
            "command_history": diagnostics["command_history"],
            "motion_interlock_reason": diagnostics["motion_interlock_reason"],
            "press_zero_set": False,
            "camera_or_loadcell_acquisition_tested": False,
            "operator_observation_required": (
                "Confirm physical axis directions and absence of collision; serial success alone "
                "does not prove the observed direction."
            ),
        }
    finally:
        service.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--printer-port", default="COM4")
    parser.add_argument("--loadcell-port", default="COM3")
    parser.add_argument("--baud-rate", type=int, default=115200)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--confirm-clear",
        action="store_true",
        help="Acknowledge main PSU on, clear build volume, and operator at the printer.",
    )
    args = parser.parse_args(argv)
    if not args.confirm_clear:
        parser.error("--confirm-clear is required before physical motion")
    result = run_motion_smoke(
        printer_port=args.printer_port,
        loadcell_port=args.loadcell_port,
        baud_rate=args.baud_rate,
    )
    atomic_write_json(args.output, result)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
