"""Shared deterministic, hardware-free test configuration."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONHASHSEED", "0")

