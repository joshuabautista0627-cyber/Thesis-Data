from __future__ import annotations

import json
import logging

from core.logging_utils import configure_structured_logging


def test_structured_logging_contains_required_context(tmp_path) -> None:
    path = tmp_path / "application.log"
    runtime = configure_structured_logging(path, "session-007", logger_name="test-structured")
    runtime.logger.info("capture ready", extra={"component": "camera"})
    runtime.stop()

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["severity"] == "INFO"
    assert row["component"] == "camera"
    assert row["session_id"] == "session-007"
    assert row["message"] == "capture ready"
    assert row["timestamp"].endswith("+00:00")
    runtime.stop()

