#!/usr/bin/env python3
"""Download, verify and normalize Binance futures data dumps.

Examples:
  .venv/bin/python scripts/download_data.py --symbols SOLUSDT,DOGEUSDT --start 2024-10 --end 2026-09
  .venv/bin/python scripts/download_data.py --symbols-file symbols.txt --kinds klines,funding --workers 10
  .venv/bin/python scripts/download_data.py --plan-file data/plan_1m.csv --workers 12

Phases:
  1. Parallel download of monthly (or daily) ZIPs with SHA256 verification
     (strict by default).
  2. Safe extraction to data/raw/<group>/<symbol>/.
  3. Per-symbol parquet build to data/parquet/<group>/<symbol>.parquet with gap
     report. Monthly and daily dumps of the same series share one storage group
     and are concatenated + de-duplicated (current-month tails).

Plan-file mode takes a CSV with header `kind,symbol,period`
(period YYYY-MM or YYYY-MM-DD depending on kind) and downloads exactly that
task list — used by the G0 funnel to touch only event-adjacent months.

Any download/verify/parse failure is reported loudly; the build phase refuses to
produce parquet from corrupt inputs.
"""

from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from signalx.datafeed import (  # noqa: E402
    KINDS,
    DataFeedError,
    download,
    dump_stem,
    dump_url,
    raw_dir,
    read_funding_csv,
    read_kline_csv,
    read_plan,
    safe_extract_zip,
    storage_group,
    validate_month,
    verify_sha256,
    write_parquet,
)
from signalx.validation import ValidationError, validate_symbol  # noqa: E402

_tls = threading.local()


def _session():
    import requests

    if not hasattr(_tls, "sess"):
        _tls.sess = requests.Session()
    return _tls.sess


def months_between(start: str, end: str) -> list[str]:
    validate_month(start)
    validate_month(end)
    y0, m0 = (int(x) for x in start.split("-"))
    y1, m1 = (int(x) for x in end.split("-"))
    if (y0, m0) > (y1, m1):
        raise ValidationError("start must be <= end")
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def fetch_one(kind: str, symbol: str, period: str, data_root: Path, *, strict_checksum: bool) -> dict:
    stem = dump_stem(kind, symbol, period)
    dest_dir = raw_dir(data_root, kind, symbol)
    zip_path = dest_dir / f"{stem}.zip"
    ok_marker = dest_dir / f"{stem}.ok"
    if ok_marker.exists():
        return {"kind": kind, "symbol": symbol, "period": period, "status": "cached"}

    url = dump_url(kind, symbol, period)
    sess = _session()
    if not download(url, zip_path, session=sess):
        return {"kind": kind, "symbol": symbol, "period": period, "status": "absent"}

    cs = sess.get(url + ".CHECKSUM", timeout=60)
    if cs.status_code == 200:
        verify_sha256(zip_path, cs.text, expected_name=f"{stem}.zip")
    elif strict_checksum:
        raise DataFeedError(f"missing checksum for {stem} (strict mode)")

    safe_extract_zip(zip_path, dest_dir)
    ok_marker.write_text("ok\n")
    return {"kind": kind, "symbol": symbol, "period": period, "status": "downloaded"}


def build_klines_parquet(symbol: str, data_root: Path, kind: str = "klines") -> dict:
    csvs = sorted(raw_dir(data_root, kind, symbol).glob("*.csv"))
    if not csvs:
        return {"symbol": symbol, "rows": 0, "files": 0, "gap_minutes": 0}
    frames = []
    for f in csvs:
        try:
            frames.append(read_kline_csv(f))
        except DataFeedError as exc:
            raise SystemExit(f"FATAL: corrupt input, refusing to build {symbol}: {exc}") from exc
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates("open_time", keep="last").sort_values("open_time").reset_index(drop=True)
    step = df["open_time"].diff().dropna() // 60_000
    gap_minutes = int((step - 1).clip(lower=0).sum())
    write_parquet(df, data_root / "parquet" / storage_group(kind) / f"{symbol}.parquet")
    return {"symbol": symbol, "rows": len(df), "files": len(csvs), "gap_minutes": gap_minutes}


def build_funding_parquet(symbol: str, data_root: Path, kind: str = "funding") -> dict:
    csvs = sorted(raw_dir(data_root, kind, symbol).glob("*.csv"))
    if not csvs:
        return {"symbol": symbol, "rows": 0}
    frames = []
    for f in csvs:
        try:
            frames.append(read_funding_csv(f))
        except DataFeedError as exc:
            raise SystemExit(f"FATAL: corrupt input, refusing to build {symbol}: {exc}") from exc
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates("funding_time", keep="last").sort_values("funding_time").reset_index(drop=True)
    write_parquet(df, data_root / "parquet" / storage_group(kind) / f"{symbol}.parquet")
    return {"symbol": symbol, "rows": len(df)}





def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument("--symbols", help="comma-separated symbols, e.g. SOLUSDT,DOGEUSDT")
    src.add_argument("--symbols-file", type=Path, help="file with one symbol per line")
    src.add_argument("--plan-file", type=Path, help="CSV with header kind,symbol,period (exact task list)")
    p.add_argument("--start", help="first month YYYY-MM (required without --plan-file)")
    p.add_argument("--end", help="last month YYYY-MM inclusive (required without --plan-file)")
    p.add_argument("--kinds", default="klines,funding", help="comma-separated kinds (without --plan-file)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--data-root", type=Path, default=ROOT / "data")
    p.add_argument("--no-strict-checksum", action="store_true", help="allow missing .CHECKSUM files")
    p.add_argument("--no-build", action="store_true", help="skip parquet build phase")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.plan_file is not None:
        if args.symbols or args.symbols_file or args.start or args.end:
            raise SystemExit("FATAL: --plan-file cannot be combined with --symbols/--symbols-file/--start/--end")
        try:
            tasks = read_plan(args.plan_file)
        except DataFeedError as exc:
            raise SystemExit(f"FATAL: {exc}") from exc
        if not tasks:
            raise SystemExit("FATAL: plan file has no tasks")
        print(f"[dl] plan mode: {len(tasks)} unique tasks from {args.plan_file}", flush=True)
    else:
        if not args.symbols and not args.symbols_file:
            raise SystemExit("FATAL: need --plan-file or --symbols/--symbols-file")
        if not args.start or not args.end:
            raise SystemExit("FATAL: --start and --end are required with --symbols/--symbols-file")
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        else:
            lines = args.symbols_file.read_text().splitlines()
            symbols = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        symbols = [validate_symbol(s) for s in symbols]
        kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
        for k in kinds:
            if k not in KINDS:
                raise SystemExit(f"FATAL: unknown kind {k!r} (known: {', '.join(KINDS)})")
        months = months_between(args.start, args.end)
        tasks = [(k, s, m) for k in kinds for s in symbols for m in months]
        print(
            f"[dl] {len(tasks)} tasks: {len(symbols)} symbols x {len(months)} months x {len(kinds)} kinds",
            flush=True,
        )

    data_root: Path = args.data_root
    data_root.mkdir(parents=True, exist_ok=True)

    counts = {"downloaded": 0, "cached": 0, "absent": 0, "error": 0}
    errors: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(fetch_one, k, s, p, data_root, strict_checksum=not args.no_strict_checksum): (k, s, p)
            for k, s, p in tasks
        }
        for fut in as_completed(futures):
            k, s, p = futures[fut]
            try:
                res = fut.result()
                counts[res["status"]] += 1
            except Exception as exc:  # noqa: BLE001 - collected and reported loudly below
                counts["error"] += 1
                errors.append(f"{k} {s} {p}: {exc}")
            done += 1
            if done % 50 == 0 or done == len(tasks):
                print(f"[dl] {done}/{len(tasks)} {counts}", flush=True)

    if errors:
        print(f"[dl] {len(errors)} ERRORS (first 10):", flush=True)
        for e in errors[:10]:
            print(f"  ! {e}", flush=True)

    if args.no_build:
        return 2 if errors else 0

    print("[build] building parquet files", flush=True)
    by_group: dict[str, set[str]] = {}
    for kind, sym, _per in tasks:
        by_group.setdefault(storage_group(kind), set()).add(sym)
    for group, syms in sorted(by_group.items()):
        for s in sorted(syms):
            if group.startswith("klines"):
                r = build_klines_parquet(s, data_root, kind=group)
                print(
                    f"[build] {group:12s} {s:14s} klines={r['rows']:>9d} rows/{r['files']:>3d} files "
                    f"gap={r['gap_minutes']:>7d} min",
                    flush=True,
                )
            else:
                r = build_funding_parquet(s, data_root, kind=group)
                print(f"[build] {group:12s} {s:14s} funding={r['rows']:>5d} rows", flush=True)
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
