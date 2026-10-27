"""Command-line interface: `transit-pulse analyze ...`."""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import duckdb

from . import __version__
from .analysis import Thresholds, compute_metrics, infer_service_date
from .gtfs_static import GTFSError, agency_timezone, build_scheduled_events, load_gtfs
from .realtime import RealtimeError, load_observation_files
from .report import write_report


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0 (seconds)")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transit-pulse",
        description="Measure transit reliability from GTFS static + GTFS-Realtime data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="compute metrics and write a report")
    analyze.add_argument("--static", required=True, help="GTFS static .zip")
    analyze.add_argument(
        "--realtime", required=True, nargs="+",
        help="GTFS-Realtime .pb file(s) and/or normalized .json observation file(s)",
    )
    analyze.add_argument("--out-dir", required=True, help="directory for metrics.json + report.html")
    analyze.add_argument(
        "--date", type=_date,
        help="service date YYYY-MM-DD (default: inferred from the realtime data)",
    )
    analyze.add_argument("--early-secs", type=_non_negative_int, default=60,
                         help="seconds early still counted on time (default 60)")
    analyze.add_argument("--late-secs", type=_non_negative_int, default=300,
                         help="seconds late still counted on time (default 300)")
    return parser


def run_analyze(args) -> int:
    con = duckdb.connect()
    load_gtfs(con, args.static)
    load_observation_files(con, args.realtime)
    service_date = args.date or infer_service_date(con, agency_timezone(con))
    if service_date is None:
        print("error: no observations to infer a service date from; pass --date",
              file=sys.stderr)
        return 2
    n_events = build_scheduled_events(con, service_date)
    if n_events == 0:
        print(f"warning: no scheduled service on {service_date}", file=sys.stderr)
    metrics = compute_metrics(con, service_date, Thresholds(args.early_secs, args.late_secs))
    json_path, html_path = write_report(metrics, args.out_dir)

    o = metrics["overall"]
    otp = "n/a" if o["otp_pct"] is None else f"{o['otp_pct']:.1f}%"
    print(f"service date {service_date}: OTP {otp} over {o['observed_events']} observed "
          f"of {o['scheduled_events']} scheduled stop events; "
          f"{o['cancelled_trips']} cancelled trips; {o['bunching_events']} bunching events")
    print(f"wrote {json_path}")
    print(f"wrote {html_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "analyze":
            return run_analyze(args)
    except (GTFSError, RealtimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
