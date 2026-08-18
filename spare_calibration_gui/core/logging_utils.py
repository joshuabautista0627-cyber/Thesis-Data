"""Structured, nonblocking application logging helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from queue import SimpleQueue
from typing import Any


class StructuredJsonFormatter(logging.Formatter):
    """Format one compact JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "component": getattr(record, "component", record.name),
            "session_id": getattr(record, "session_id", ""),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        error_code = getattr(record, "error_code", None)
        if error_code is not None:
            payload["error_code"] = str(error_code)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class LoggingRuntime:
    """Own the queue listener and make shutdown explicit and idempotent."""

    logger: logging.Logger
    listener: QueueListener
    file_handler: logging.Handler
    _stopped: bool = False

    def stop(self) -> None:
        if self._stopped:
            return
        self.listener.stop()
        self.file_handler.flush()
        self.file_handler.close()
        self._stopped = True


def configure_structured_logging(
    log_path: str | Path,
    session_id: str,
    *,
    logger_name: str = "spare_calibration_gui",
    level: int = logging.INFO,
) -> LoggingRuntime:
    """Configure a queue-backed JSON-lines file logger for one session."""

    destination = Path(log_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(destination, encoding="utf-8")
    file_handler.setFormatter(StructuredJsonFormatter())
    records: SimpleQueue[logging.LogRecord] = SimpleQueue()
    listener = QueueListener(records, file_handler, respect_handler_level=True)

    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.setLevel(level)
    logger.propagate = False
    queue_handler = QueueHandler(records)
    queue_handler.addFilter(_SessionContextFilter(session_id))
    logger.addHandler(queue_handler)
    listener.start()
    return LoggingRuntime(logger=logger, listener=listener, file_handler=file_handler)


class _SessionContextFilter(logging.Filter):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self._session_id = session_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "session_id"):
            record.session_id = self._session_id
        return True

