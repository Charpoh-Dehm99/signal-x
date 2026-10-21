"""CLI-level offline tests for scripts/*.py.

Scripts are loaded via importlib (no packaging yet); every test stays
offline — only argument-validation and pure helpers are exercised, never
network, never the WS loops.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT_NAMES = (
    "sentinel",
    "recorder",
    "stream",
    "book",
    "cvd",
    "watchdog",
    "cost_probe",
    "run_h1_study",
)


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"script_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def scripts():
    return {name: load_script(name) for name in SCRIPT_NAMES}


# --- watchdog ---------------------------------------------------------------


def test_watchdog_specs_wellformed(scripts):
    wd = scripts["watchdog"]
    names = [n for n, _ in wd.DAEMONS]
    assert len(names) == len(set(names))
    for name, argv in wd.DAEMONS:
        assert argv[0] == f"scripts/{name}.py"
        assert all(isinstance(a, str) for a in argv)


def test_watchdog_rejects_short_interval(scripts):
    assert scripts["watchdog"].main(["--interval", "5"]) == 2


def test_watchdog_find_running_absent(scripts):
    assert scripts["watchdog"].find_running("definitely-no-such-daemon-xyz") is None


# --- argument validation (all offline; return 2) -----------------------------


@pytest.mark.parametrize("name", ["sentinel", "recorder", "stream", "book", "cvd"])
def test_scripts_reject_top_zero(scripts, name):
    assert scripts[name].main(["--top", "0"]) == 2


def test_stream_rejects_negative_reconnects(scripts):
    assert scripts["stream"].main(["--top", "1", "--max-reconnects", "-1"]) == 2


def test_stream_rejects_negative_min_notional(scripts):
    assert scripts["stream"].main(["--top", "1", "--min-notional", "-1"]) == 2


def test_sentinel_accepts_flow_flag(scripts):
    args = scripts["sentinel"].parse_args(["--flow", "--flow-top", "10"])
    assert args.flow is True
    assert args.flow_top == 10


def test_sentinel_rejects_bad_flow_top(scripts):
    assert scripts["sentinel"].main(["--top", "1", "--flow-top", "0"]) == 2


def test_book_rejects_short_flush(scripts):
    assert scripts["book"].main(["--top", "1", "--flush", "1"]) == 2


def test_cvd_rejects_short_flush(scripts):
    assert scripts["cvd"].main(["--top", "1", "--flush", "1"]) == 2


def test_stream_rejects_bad_symbol(scripts):
    assert scripts["stream"].main(["--top", "1", "--symbols", "bad symbol"]) == 2


# --- cost probe --------------------------------------------------------------


def test_cost_probe_bad_fee(scripts):
    assert scripts["cost_probe"].main(["--fee-bps", "-1"]) == 2


def test_cost_probe_bad_notionals(scripts):
    assert scripts["cost_probe"].main(["--notionals", "abc"]) == 2


def test_cost_probe_empty_book_root(scripts, tmp_path):
    assert scripts["cost_probe"].main(["--book-root", str(tmp_path)]) == 2


# --- h1 runner (argparse only; never runs the study) -------------------------


def test_h1_parse_args(scripts):
    args = scripts["run_h1_study"].parse_args(["--limit", "3"])
    assert args.limit == 3
    with pytest.raises(SystemExit):
        scripts["run_h1_study"].parse_args(["--limit", "nope"])
