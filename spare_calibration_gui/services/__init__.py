"""Hardware-boundary service contracts and implementations."""

from .interfaces import (
    CameraConnectionInfo,
    CameraPropertyReadback,
    CameraServiceInterface,
    CapturedFrame,
    SerialConnectionInfo,
    SerialServiceInterface,
)
from .serial_service import (
    Hx711SerialService,
    SerialPlaybackSource,
    SerialPortDescriptor,
    SerialService,
    enumerate_serial_ports,
    parse_data_line,
    parse_hello_line,
)

__all__ = [
    "CameraConnectionInfo",
    "CameraPropertyReadback",
    "CameraServiceInterface",
    "CapturedFrame",
    "SerialConnectionInfo",
    "Hx711SerialService",
    "SerialPlaybackSource",
    "SerialPortDescriptor",
    "SerialService",
    "SerialServiceInterface",
    "enumerate_serial_ports",
    "parse_data_line",
    "parse_hello_line",
]
