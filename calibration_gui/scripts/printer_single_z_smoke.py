"""Opt-in Ender 3 home plus one absolute positive-Z hardware check."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import math
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.models import PrinterConfig
from data.exporter import atomic_write_json
from services.printer_service import PrinterSerialService, enumerate_printer_ports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--printer-port", default="COM4")
    parser.add_argument("--loadcell-port", default="COM3")
    parser.add_argument("--baud-rate", type=int, default=115200)
    parser.add_argument("--expected-home-z", type=float, required=True)
    parser.add_argument("--target-z", type=float, required=True)
    parser.add_argument("--feed", type=float, default=120.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-clear", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_clear:
        parser.error("--confirm-clear is required before physical motion")
    if args.printer_port.casefold() == args.loadcell_port.casefold():
        parser.error("printer and load-cell ports must be different")
    if not args.target_z > args.expected_home_z:
        parser.error("this smoke test requires a positive Z target above the expected home Z")

    ports = enumerate_printer_ports()
    devices = {str(row["device"]).casefold() for row in ports}
    if args.printer_port.casefold() not in devices:
        parser.error(f"printer port {args.printer_port} was not enumerated")
    if args.loadcell_port.casefold() not in devices:
        parser.error(f"load-cell port {args.loadcell_port} was not enumerated")

    service = PrinterSerialService()
    started = datetime.now(UTC).isoformat()
    try:
        info = service.connect(
            PrinterConfig(port=args.printer_port, baud_rate=args.baud_rate),
            port=args.printer_port,
        )
        before = service.query_position()
        homed = service.home_all()
        if not math.isclose(
            homed.reported_z_mm,
            args.expected_home_z,
            rel_tol=0.0,
            abs_tol=0.05,
        ):
            raise RuntimeError(
                f"home reported Z={homed.reported_z_mm:g}, expected {args.expected_home_z:g}; "
                "the Z jog was not sent"
            )
        service.move_absolute(
            z_mm=args.target_z,
            feed_rate_mm_min=args.feed,
        )
        after = service.position
        if not math.isclose(after.reported_z_mm, args.target_z, rel_tol=0.0, abs_tol=0.05):
            raise RuntimeError(
                f"final M114 reported Z={after.reported_z_mm:g}, expected {args.target_z:g}"
            )
        diagnostics = service.diagnostics()
        commands = [str(row.get("gcode", "")) for row in diagnostics["command_history"]]
        expected = [
            "M115",
            "M114",
            "G28",
            "M400",
            "M114",
            "G90",
            f"G1 Z{args.target_z:.3f} F{args.feed:.3f}",
            "M400",
            "M114",
        ]
        if commands != expected:
            raise RuntimeError(f"unexpected single-Z command list: {commands}")
        atomic_write_json(
            args.output,
            {
                "test_type": "physical_explicit_home_and_single_positive_z",
                "started_utc": started,
                "finished_utc": datetime.now(UTC).isoformat(),
                "printer_port": args.printer_port,
                "loadcell_port_reserved_not_opened": args.loadcell_port,
                "ports_are_separate": True,
                "enumerated_ports": list(ports),
                "connection": asdict(info),
                "position_before_home": asdict(before),
                "position_after_home": asdict(homed),
                "requested_target_z_mm": args.target_z,
                "requested_feed_rate_mm_min": args.feed,
                "position_after_z_move": asdict(after),
                "commands_actually_sent": commands,
                "command_history": diagnostics["command_history"],
                "x_or_y_jog_sent": False,
                "press_zero_set": False,
                "emergency_commands_sent": False,
            },
        )
        print(args.output.resolve())
        return 0
    finally:
        service.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
