"""Opt-in Ender 3 Home All hardware check with no subsequent jog motion."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-clear", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_clear:
        parser.error("--confirm-clear is required before G28")
    if args.printer_port.casefold() == args.loadcell_port.casefold():
        parser.error("printer and load-cell ports must be different")

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
        after = service.home_all()
        diagnostics = service.diagnostics()
        commands = [str(row.get("gcode", "")) for row in diagnostics["command_history"]]
        expected = ["M115", "M114", "G28", "M400", "M114"]
        if commands != expected:
            raise RuntimeError(f"unexpected Home All command list: {commands}")
        result = {
            "test_type": "physical_explicit_home_all",
            "started_utc": started,
            "finished_utc": datetime.now(UTC).isoformat(),
            "printer_port": args.printer_port,
            "loadcell_port_reserved_not_opened": args.loadcell_port,
            "ports_are_separate": True,
            "enumerated_ports": list(ports),
            "connection": asdict(info),
            "position_before_home": asdict(before),
            "position_after_home": asdict(after),
            "commands_actually_sent": commands,
            "command_history": diagnostics["command_history"],
            "jog_commands_sent": False,
            "press_zero_set": False,
            "emergency_commands_sent": False,
        }
        atomic_write_json(args.output, result)
        print(args.output.resolve())
        return 0
    finally:
        service.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
