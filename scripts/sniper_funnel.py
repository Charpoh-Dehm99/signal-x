"""Print the SNIPER V3 gate funnel from the append-only evaluation journal.

Usage:
    python scripts/sniper_funnel.py [--dir data/sniper/evals] [--json]

Observation only: this reports which gate rejected each evaluation and what the
forward returns looked like. It is not a signal and not financial advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signalx.sniper_v3.evallog import EvalLog  # noqa: E402


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def main() -> int:
    ap = argparse.ArgumentParser(description="SNIPER V3 gate funnel")
    ap.add_argument("--dir", default=str(ROOT / "data" / "sniper" / "evals"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    log = EvalLog(Path(args.dir))
    funnel = log.funnel()
    outcomes = log.outcomes()
    per_symbol = Counter(row.get("symbol") for row in log.rows())

    scored = [row for row in outcomes if row.get("dir_fwd_bps_12") is not None]
    payload = {
        **funnel,
        "per_symbol": dict(per_symbol),
        "outcomes": {
            "n": len(scored),
            "mean_dir_fwd_bps_15m": _mean(
                [float(r["dir_fwd_bps_3"]) for r in scored if r.get("dir_fwd_bps_3") is not None]
            ),
            "mean_dir_fwd_bps_60m": _mean(
                [float(r["dir_fwd_bps_12"]) for r in scored]
            ),
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"evaluations : {funnel['n']}")
    rate = funnel["setup_rate"]
    print(f"setups      : {funnel['setups']} ({rate:.2%})" if rate is not None else "setups      : 0")
    print("\nfailed gate:")
    for gate, count in (funnel["failed_gate"] or {}).items():
        print(f"  {gate:18} {count:6d}  {count / funnel['n']:6.1%}")
    print("\nreached gate (passed):")
    for gate, count in (funnel["reached_gate"] or {}).items():
        print(f"  {gate:18} {count:6d}")
    print("\nper symbol:", dict(per_symbol))
    print("\noutcomes:", payload["outcomes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
