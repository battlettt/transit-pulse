"""Blank (non-timepoint) stop times: linear interpolation between timed stops."""

import duckdb

from conftest import (
    SERVICE_DATE, feed_with, obs, route, run_analysis, sched_epoch, stop_time, write_gtfs_zip,
)
from transit_pulse.gtfs_static import build_scheduled_events, load_gtfs

# I1 (route R1): seq 1 blank (before first timed stop), 2 = 10:00, 3 and 5 blank,
# 11 = 10:09, 12 blank (after last timed stop). Interpolation runs over stop_sequence:
# 540 s across 9 sequence steps = 60 s per step -> seq 3 at 10:01, seq 5 at 10:03.
# P1: seq 2 has only a departure time, which is used as-is (not interpolated).
# U1 (route R2): no timed stops at all.
INTERP_TRIPS = [("R1", "I1"), ("R1", "P1"), ("R2", "U1")]
INTERP_STOP_TIMES = [
    stop_time("I1", 1, ""), stop_time("I1", 2, "10:00:00"), stop_time("I1", 3, ""),
    stop_time("I1", 5, ""), stop_time("I1", 11, "10:09:00"), stop_time("I1", 12, ""),
    stop_time("P1", 1, "11:00:00"), stop_time("P1", 2, "", departure="11:07:00"),
    stop_time("P1", 3, "11:10:00"),
    stop_time("U1", 1, ""), stop_time("U1", 2, ""), stop_time("U1", 3, ""),
]


def _zip(tmp_path, frequencies=None, trips=INTERP_TRIPS, stop_times=INTERP_STOP_TIMES):
    return write_gtfs_zip(tmp_path / "interp.zip", feed_with(trips, stop_times, frequencies))


def _events(con, trip_id):
    return con.execute(
        "SELECT stop_sequence, sched_epoch, interpolated FROM scheduled_events "
        "WHERE trip_id = ? ORDER BY stop_sequence", [trip_id]
    ).fetchall()


def test_blank_times_interpolated_between_timed_stops(tmp_path):
    con = duckdb.connect()
    load_gtfs(con, _zip(tmp_path))
    build_scheduled_events(con, SERVICE_DATE)
    assert _events(con, "I1") == [
        (2, sched_epoch("10:00:00"), False),
        (3, sched_epoch("10:01:00"), True),
        (5, sched_epoch("10:03:00"), True),
        (11, sched_epoch("10:09:00"), False),
    ]
    # Departure-only stop keeps its stated time and is not flagged.
    assert _events(con, "P1")[1] == (2, sched_epoch("11:07:00"), False)


def test_no_extrapolation_before_first_or_after_last_timed_stop(tmp_path):
    metrics, con = run_analysis(_zip(tmp_path), [])
    seqs = [r[0] for r in _events(con, "I1")]
    assert 1 not in seqs and 12 not in seqs
    assert metrics["schedule"]["untimed_stop_times_dropped"] == 2
    assert metrics["schedule"]["interpolated_events"] == 2


def test_interpolation_rounds_to_whole_seconds(tmp_path):
    stop_times = [stop_time("I2", 1, "10:00:00"), stop_time("I2", 2, ""),
                  stop_time("I2", 3, ""), stop_time("I2", 4, "10:00:10")]
    con = duckdb.connect()
    load_gtfs(con, _zip(tmp_path, trips=[("R1", "I2")], stop_times=stop_times))
    build_scheduled_events(con, SERVICE_DATE)
    base = sched_epoch("10:00:00")
    assert [r[1] - base for r in _events(con, "I2")] == [0, 3, 7, 10]  # 3.33, 6.67


def test_interpolated_event_is_matched_and_flagged_in_metrics(tmp_path):
    metrics, con = run_analysis(_zip(tmp_path), [obs("I1", 3, "10:01:00", delay=45)])
    row = con.execute(
        "SELECT delay_secs, status, interpolated FROM events "
        "WHERE trip_id = 'I1' AND stop_sequence = 3"
    ).fetchone()
    assert row == (45, "on_time", True)
    assert metrics["overall"]["interpolated_events"] == 2
    assert route(metrics, "R1")["interpolated_events"] == 2
    assert route(metrics, "R2")["interpolated_events"] == 0


def test_trip_without_timed_stops_is_reported_not_crashed(tmp_path):
    records = [obs("U1", 1, "12:00:00"), obs("T1a", 1, "08:00:00")]
    metrics, con = run_analysis(_zip(tmp_path), records)
    assert con.execute(
        "SELECT count(*) FROM scheduled_events WHERE trip_id = 'U1'").fetchone()[0] == 0
    assert metrics["schedule"]["trips_without_timed_stops"] == {"count": 1, "trip_ids": ["U1"]}
    assert metrics["inputs"]["observations_unmatched"] == 1  # the U1 observation
    assert metrics["overall"]["observed_events"] == 1
    # U1's blank stops are reported under the trip, not double-counted as dropped stops.
    assert metrics["schedule"]["untimed_stop_times_dropped"] == 2


def test_interpolated_template_of_frequency_trip(tmp_path):
    stop_times = [stop_time("F3", 1, "00:00:00"), stop_time("F3", 2, ""),
                  stop_time("F3", 3, "00:10:00")]
    frequencies = [{"trip_id": "F3", "start_time": "07:00:00", "end_time": "07:20:00",
                    "headway_secs": "600", "exact_times": "1"}]
    con = duckdb.connect()
    load_gtfs(con, _zip(tmp_path, frequencies, [("R1", "F3")], stop_times))
    build_scheduled_events(con, SERVICE_DATE)
    rows = con.execute(
        "SELECT frequency_start_time, sched_epoch FROM scheduled_events "
        "WHERE trip_id = 'F3' AND interpolated ORDER BY 1"
    ).fetchall()
    assert rows == [("07:00:00", sched_epoch("07:05:00")),
                    ("07:10:00", sched_epoch("07:15:00"))]
