"""Realtime ADDED trips with no schedule entry are counted, not dropped."""

import json

from conftest import obs, route, sched_epoch


def _added(trip_id, seq, gtfs_time, route_id=None):
    return {"trip_id": trip_id, "route_id": route_id, "stop_id": f"S{seq}",
            "stop_sequence": seq, "observed_epoch": sched_epoch(gtfs_time),
            "schedule_relationship": "ADDED"}


def test_unscheduled_added_trips_counted_per_route(analyze):
    records = [
        _added("X1", 1, "09:00:00", "R1"), _added("X1", 2, "09:05:00", "R1"),
        _added("X2", 1, "09:10:00", "R1"),
        _added("X3", 1, "09:20:00", "R2"),
        _added("X4", 1, "09:30:00"),                # feed gave no route
        obs("T1a", 1, "08:00:00"),
    ]
    metrics, _ = analyze(records)
    added = metrics["added_trips"]
    assert added["count"] == 4
    assert added["observations"] == 5
    assert added["routes"] == [
        {"route_id": "R1", "route_name": "1", "trips": 2, "observations": 3,
         "trip_ids": ["X1", "X2"]},
        {"route_id": "R2", "route_name": "2", "trips": 1, "observations": 1,
         "trip_ids": ["X3"]},
        {"route_id": None, "route_name": None, "trips": 1, "observations": 1,
         "trip_ids": ["X4"]},
    ]
    assert metrics["overall"]["added_trips"] == 4
    assert route(metrics, "R1")["added_trips"] == 2
    assert route(metrics, "R2")["added_trips"] == 1
    # Not part of the schedule, OTP or the unmatched count.
    o = metrics["overall"]
    assert o["scheduled_trips"] == 5 and o["scheduled_events"] == 15
    assert o["observed_events"] == 1 and o["otp_pct"] == 100.0
    assert metrics["inputs"]["observations_unmatched"] == 0
    assert metrics["inputs"]["observations_added_trips"] == 5
    json.dumps(metrics)


def test_added_trip_for_route_not_in_schedule(analyze):
    metrics, _ = analyze([_added("X9", 1, "09:00:00", "R99")])
    assert metrics["added_trips"]["count"] == 1
    assert metrics["added_trips"]["routes"][0]["route_id"] == "R99"
    assert metrics["added_trips"]["routes"][0]["route_name"] == "R99"
    assert [r["route_id"] for r in metrics["routes"]] == ["R1", "R2"]


def test_added_relationship_on_scheduled_trip_id_is_matched_normally(analyze):
    record = _added("T1a", 1, "08:01:00", "R1")
    metrics, _ = analyze([record])
    assert metrics["added_trips"]["count"] == 0
    assert metrics["overall"]["observed_events"] == 1
    assert metrics["overall"]["on_time_events"] == 1


def test_no_added_trips_gives_empty_section(analyze):
    metrics, _ = analyze([obs("T1a", 1, "08:00:00")])
    assert metrics["added_trips"] == {"count": 0, "observations": 0, "routes": []}
    assert metrics["overall"]["added_trips"] == 0


def test_skipped_stops_still_counted_alongside_added_trips(analyze):
    records = [
        {"trip_id": "T1a", "stop_sequence": 2, "stop_id": "S2",
         "schedule_relationship": "SKIPPED"},
        obs("T1a", 1, "08:00:00"),
        _added("X1", 1, "08:30:00", "R1"),
    ]
    metrics, con = analyze(records)
    assert metrics["overall"]["skipped_events"] == 1
    assert route(metrics, "R1")["skipped_events"] == 1
    assert metrics["added_trips"]["count"] == 1
    status = con.execute(
        "SELECT status FROM events WHERE trip_id = 'T1a' AND stop_sequence = 2").fetchone()
    assert status == ("skipped",)
