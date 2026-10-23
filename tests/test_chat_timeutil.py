"""System-local time formatting tests."""

from __future__ import annotations

from datetime import datetime

from signalx.chat.timeutil import local_iso, local_label, local_zone


def test_local_fields_are_system_local_and_explicit():
    ms = 1_700_000_300_000
    iso = local_iso(ms)
    label = local_label(ms)
    zone = local_zone()
    assert iso is not None and "T" in iso
    assert datetime.fromisoformat(iso).tzinfo is not None
    assert label is not None and label.startswith("2023-11-")
    assert zone and "UTC" in zone


def test_invalid_time_values_are_none():
    for value in (None, 0, -1, "nope", True, float("nan"), float("inf")):
        assert local_iso(value) is None
        assert local_label(value) is None
