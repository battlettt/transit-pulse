"""frequencies.txt: template trips expanded into one instance per headway."""

import duckdb

from conftest import (
    SERVICE_DATE, feed_with, obs, route, run_analysis, sched_epoch, stop_time, write_gtfs_zip,
)
from transit_pulse.gtfs_static import build_scheduled_events, load_gtfs

# F1 (route R1): template times 06:00 / 06:04 / 06:10 -> offsets 0, 240, 600 s.
#   exact_times=1, 07:00-08:00 every 15 min -> 07:00, 07:15, 07:30, 07:45 (08:00 excluded)
#   plus a second window 16:00-17:00 every 30 min -> 16:00, 16:30.
# F2 (route R2): headway-based (exact_times blank), 09:00-09:30 every 10 min -> 3 instances.
FREQ_TRIPS = [("R1", "F1"), ("R2", "F2")]
FREQ_STOP_TIMES = [
    stop_time("F1", 1, "06:00:00"), stop_time("F1", 2, "06:04:00"),
    stop_time("F1", 3, "06:10:00"),
    stop_time("F2", 1, "00:00:00"), stop_time("F2", 2, "00:05:00"),
]
FREQUENCIES = [
    {"trip_id": "F1", "start_time": "07:00:00", "end_time": "08:00:00",
     "headway_secs": "900", "exact_times": "1"},
    {"trip_id": "F1", "start_time": "16:00:00", "end_time": "17:00:00",
     "headway_secs": "1800", "exact_times": "1"},
    {"trip_id": "F2", "start_time": "09:00:00", "end_time": "09:30:00",
     "headway_secs": "600", "exact_times": ""},
]


def _zip(tmp_path):
    return write_gtfs_zip(tmp_path / "freq.zip",
                          feed_with(FREQ_TRIPS, FREQ_STOP_TIMES, FREQUENCIES))


def _hms(secs):
    return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def test_frequency_trip_expands_at_headway_intervals(tmp_path):
    con = duckdb.connect()
    load_gtfs(con, _zip(tmp_path))
    n = build_scheduled_events(con, SERVICE_DATE)
    assert n == 15 + 6 * 3 + 3 * 2  # timetable trips + F1 instances + F2 instances

    rows = con.execute(
        "SELECT trip_instance_id, frequency_start_time, stop_sequence, sched_epoch, "
        "schedule_type FROM scheduled_events WHERE trip_id = 'F1' "
        "ORDER BY sched_epoch, stop_sequence"
    ).fetchall()
    starts = [7 * 3600, 7 * 3600 + 900, 7 * 3600 + 1800, 7 * 3600 + 2700,
              16 * 3600, 16 * 3600 + 1800]
    expected = [
        (f"F1@{_hms(s)}", _hms(s), seq, sched_epoch(_hms(s + offset)), "frequency_exact")
        for s in starts
        for seq, offset in ((1, 0), (2, 240), (3, 600))
    ]
    assert rows == expected
    # The 06:00 template run itself is not a scheduled trip.
    assert sched_epoch("06:00:00") not in [r[3] for r in rows]


def test_headway_based_trip_is_flagged(tmp_path):
    con = duckdb.connect()
    load_gtfs(con, _zip(tmp_path))
    build_scheduled_events(con, SERVICE_DATE)
    rows = con.execute(
        "SELECT DISTINCT frequency_start_time, schedule_type FROM scheduled_events "
        "WHERE trip_id = 'F2' ORDER BY 1"
    ).fetchall()
    assert rows == [("09:00:00", "frequency_headway"), ("09:10:00", "frequency_headway"),
                    ("09:20:00", "frequency_headway")]


def test_exact_times_trip_matched_against_stated_times(tmp_path):
    records = [
        # No start_time: nearest instance (07:15, so stop 2 at 07:19) -> 30 s late.
        obs("F1", 2, "07:19:00", delay=30),
        # start_time picks the 07:30 instance (stop 2 at 07:34) even though 07:22
        # would be nearer to the 07:15 one -> 12 min early.
        obs("F1", 2, "07:34:00", delay=-720, start_time="07:30:00"),
    ]
    metrics, con = run_analysis(_zip(tmp_path), records)
    rows = con.execute(
        "SELECT trip_instance_id, delay_secs, status FROM events "
        "WHERE observed_epoch IS NOT NULL ORDER BY trip_instance_id"
    ).fetchall()
    assert rows == [("F1@07:15:00", 30, "on_time"), ("F1@07:30:00", -720, "early")]
    r1 = route(metrics, "R1")
    assert r1["scheduled_trips"] == 3 + 6  # each frequency instance is its own trip
    assert metrics["schedule"]["frequency_exact_events"] == 18
    assert metrics["schedule"]["frequency_headway_events"] == 6
    assert metrics["schedule"]["frequency_trip_instances"] == 9
    assert metrics["inputs"]["observations_unmatched"] == 0


def test_cancellation_with_start_time_cancels_one_instance(tmp_path):
    records = [{"trip_id": "F1", "start_time": "7:45:00", "schedule_relationship": "CANCELED"}]
    metrics, con = run_analysis(_zip(tmp_path), records)
    r1 = route(metrics, "R1")
    assert r1["cancelled_trips"] == 1 and r1["cancelled_events"] == 3
    cancelled = con.execute(
        "SELECT DISTINCT trip_instance_id FROM events WHERE status = 'cancelled'"
    ).fetchall()
    assert cancelled == [("F1@07:45:00",)]


def test_feed_without_frequencies_is_unchanged(gtfs_zip):
    con = duckdb.connect()
    load_gtfs(con, gtfs_zip)
    build_scheduled_events(con, SERVICE_DATE)
    rows = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE trip_instance_id = trip_id "
        "AND schedule_type = 'timetable' AND frequency_start_time IS NULL) "
        "FROM scheduled_events"
    ).fetchone()
    assert rows == (15, 15)
