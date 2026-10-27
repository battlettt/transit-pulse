"""Synthetic GTFS feed shared by the tests.

Agency timezone America/New_York; 2024-03-06 is a Wednesday in EST (UTC-5).

Route R1 ("1"): trips T1a/T1b/T1c leave S1 at 08:00/08:10/08:20 (10-minute headway),
    reaching S2 +5 min and S3 +10 min.
Route R2 ("2"): T2a runs across midnight (23:50 -> 24:10 -> 24:30);
    T2b runs 10:00 -> 10:05 -> 10:10.
Service WK runs Mon-Fri all of 2024 except 2024-07-04 (calendar_dates removes it);
    service HOL (trip T3 on R2) is added only on 2024-07-04.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from zoneinfo import ZoneInfo

import duckdb
import pytest

from transit_pulse.analysis import Thresholds, compute_metrics
from transit_pulse.gtfs_static import build_scheduled_events, load_gtfs
from transit_pulse.realtime import load_observations, parse_json_observations

TZ = ZoneInfo("America/New_York")
SERVICE_DATE = dt.date(2024, 3, 6)


def _trip_stop_times(trip_id, times):
    return [
        {"trip_id": trip_id, "arrival_time": t, "departure_time": t,
         "stop_id": f"S{i}", "stop_sequence": str(i)}
        for i, t in enumerate(times, start=1)
    ]


def default_feed() -> dict[str, list[dict]]:
    stop_times = (
        _trip_stop_times("T1a", ["08:00:00", "08:05:00", "08:10:00"])
        + _trip_stop_times("T1b", ["08:10:00", "08:15:00", "08:20:00"])
        + _trip_stop_times("T1c", ["08:20:00", "08:25:00", "08:30:00"])
        + _trip_stop_times("T2a", ["23:50:00", "24:10:00", "24:30:00"])
        + _trip_stop_times("T2b", ["10:00:00", "10:05:00", "10:10:00"])
        + _trip_stop_times("T3", ["12:00:00", "12:05:00", "12:10:00"])
    )
    return {
        "agency.txt": [{"agency_id": "A", "agency_name": "Test Transit",
                        "agency_url": "https://example.org",
                        "agency_timezone": "America/New_York"}],
        "routes.txt": [
            {"route_id": "R1", "agency_id": "A", "route_short_name": "1",
             "route_long_name": "Crosstown", "route_type": "3"},
            {"route_id": "R2", "agency_id": "A", "route_short_name": "2",
             "route_long_name": "Night Owl", "route_type": "3"},
        ],
        "trips.txt": [
            {"route_id": "R1", "service_id": "WK", "trip_id": t, "direction_id": "0"}
            for t in ("T1a", "T1b", "T1c")
        ] + [
            {"route_id": "R2", "service_id": "WK", "trip_id": "T2a", "direction_id": "0"},
            {"route_id": "R2", "service_id": "WK", "trip_id": "T2b", "direction_id": "0"},
            {"route_id": "R2", "service_id": "HOL", "trip_id": "T3", "direction_id": "0"},
        ],
        "stops.txt": [
            {"stop_id": f"S{i}", "stop_name": f"Stop {i}", "stop_lat": "40.0",
             "stop_lon": f"-73.{i}"}
            for i in (1, 2, 3)
        ],
        "stop_times.txt": stop_times,
        "calendar.txt": [{
            "service_id": "WK", "monday": "1", "tuesday": "1", "wednesday": "1",
            "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
            "start_date": "20240101", "end_date": "20241231",
        }],
        "calendar_dates.txt": [
            {"service_id": "WK", "date": "20240704", "exception_type": "2"},
            {"service_id": "HOL", "date": "20240704", "exception_type": "1"},
        ],
    }


def write_gtfs_zip(path, feed: dict[str, list[dict]] | None = None, omit=()) -> str:
    feed = feed or default_feed()
    with zipfile.ZipFile(path, "w") as zf:
        for name, rows in feed.items():
            if name in omit:
                continue
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            zf.writestr(name, buf.getvalue())
    return str(path)


def sched_epoch(gtfs_time: str, service_date: dt.date = SERVICE_DATE) -> int:
    """Independent re-implementation: local midnight of the service date + H:M:S
    (valid away from DST-change days)."""
    h, m, s = (int(x) for x in gtfs_time.split(":"))
    midnight = dt.datetime(service_date.year, service_date.month, service_date.day, tzinfo=TZ)
    return int(midnight.timestamp()) + h * 3600 + m * 60 + s


def obs(trip_id, seq, gtfs_time, delay=0, route_id=None, stop_id=None, **extra) -> dict:
    record = {
        "trip_id": trip_id,
        "route_id": route_id,
        "stop_id": stop_id if stop_id is not None else f"S{seq}",
        "stop_sequence": seq,
        "observed_epoch": sched_epoch(gtfs_time) + delay,
        "schedule_relationship": "SCHEDULED",
    }
    record.update(extra)
    return record


@pytest.fixture
def gtfs_zip(tmp_path):
    return write_gtfs_zip(tmp_path / "gtfs.zip")


def run_analysis(zip_path, records, service_date=SERVICE_DATE, thresholds=None):
    """Full pipeline on one GTFS zip and a list of observation dicts -> (metrics, con)."""
    con = duckdb.connect()
    load_gtfs(con, zip_path)
    load_observations(con, parse_json_observations(records))
    build_scheduled_events(con, service_date)
    metrics = compute_metrics(con, service_date, thresholds or Thresholds())
    return metrics, con


@pytest.fixture
def analyze(gtfs_zip):
    """analyze(observation_dicts, date=SERVICE_DATE, thresholds=None) -> (metrics, con)"""

    def _run(records, service_date=SERVICE_DATE, thresholds=None):
        return run_analysis(gtfs_zip, records, service_date, thresholds)

    return _run


def stop_time(trip_id, seq, t, stop_id=None, departure=None) -> dict:
    """One stop_times.txt row; t may be "" for a blank (non-timepoint) time."""
    return {"trip_id": trip_id, "arrival_time": t,
            "departure_time": t if departure is None else departure,
            "stop_id": stop_id or f"S{(seq - 1) % 3 + 1}", "stop_sequence": str(seq)}


def feed_with(extra_trips=(), extra_stop_times=(), frequencies=None) -> dict:
    """default_feed() plus extra R1/R2 trips (route_id, trip_id), stop times and an
    optional frequencies.txt (list of row dicts)."""
    feed = default_feed()
    feed["trips.txt"] += [
        {"route_id": r, "service_id": "WK", "trip_id": t, "direction_id": "0"}
        for r, t in extra_trips
    ]
    feed["stop_times.txt"] += list(extra_stop_times)
    if frequencies:
        feed["frequencies.txt"] = frequencies
    return feed


def route(metrics: dict, route_id: str) -> dict:
    return next(r for r in metrics["routes"] if r["route_id"] == route_id)
