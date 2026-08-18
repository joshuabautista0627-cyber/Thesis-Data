"""Cautious, read-only Ender 3 smoke test for the independent COM4 path.

This script never homes or moves an axis. It enumerates COM3/COM4, verifies
Marlin using M115, queries M114, saves attributable evidence, and disconnects.
Physical motion must be initiated separately through the GUI after an operator
checks clearance and confirms machine direction.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import platform
import sys

# Keep the documented direct invocation (`python scripts/...py`) working as
# well as module execution from the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.models import PrinterConfig
from data.exporter import atomic_write_json
from services.printer_service import PrinterSerialService, enumerate_printer_ports


def run_read_only_printer_smoke(
    *,
    printer_port: str = "COM4",
    loadcell_port: str = "COM3",
    baud_rate: int = 115200,
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
    try:
        info = service.connect(
            PrinterConfig(port=printer_port, baud_rate=int(baud_rate)),
            port=printer_port,
        )
        position = service.query_position()
        diagnostics = service.diagnostics()
        commands = [
            item["gcode"]
            for item in diagnostics["command_history"]
        ]
        if commands != ["M115", "M114"]:
            raise RuntimeError(
                f"read-only smoke emitted unexpected commands: {commands}"
            )
        return {
            "test_type": "physical_read_only_printer_smoke",
            "started_utc": started,
            "finished_utc": datetime.now(UTC).isoformat(),
            "operating_system": platform.platform(),
            "printer_port": printer_port,
            "loadcell_port_reserved_not_opened": loadcell_port,
            "ports_are_separate": True,
            "enumerated_ports": list(ports),
            "connection": {
                "port": info.port,
                "baud_rate": info.baud_rate,
                "firmware_name": info.firmware_name,
                "firmware_version": info.firmware_version,
                "marlin_verified": info.marlin_verified,
                "connected_monotonic_ns": info.connected_monotonic_ns,
            },
            "position": {
                "reported_x_mm": position.reported_x_mm,
                "reported_y_mm": position.reported_y_mm,
                "reported_z_mm": position.reported_z_mm,
                "reported_valid": position.reported_valid,
                "homed_claimed": position.homed,
                "press_zero_valid": position.press_zero_valid,
            },
            "commands_actually_sent": commands,
            "motion_commands_sent": False,
            "homing_performed": False,
            "hardware_claim_scope": (
                "COM4 transport, Marlin M115 identity, and M114 parsing only; "
                "no camera/load-cell acquisition and no printer motion"
            ),
        }
    finally:
        service.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--printer-port", default="COM4")
    parser.add_argument("--loadcell-port", default="COM3")
    parser.add_argument("--baud-rate", type=int, default=115200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = run_read_only_printer_smoke(
        printer_port=args.printer_port,
        loadcell_port=args.loadcell_port,
        baud_rate=args.baud_rate,
    )
    if args.output is not None:
        atomic_write_json(args.output, result)
        print(args.output.resolve())
    else:
        import json

        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
