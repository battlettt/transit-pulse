"""GTFS-RT trip schedule_relationship REPLACEMENT / DUPLICATED / NEW and unknown values.

Before 0.3.0 any of these aborted the run with `unknown schedule_relationship`.
"""

import json

import duckdb
import pytest
from google.transit import gtfs_realtime_pb2 as rt

from conftest import SERVICE_DATE, obs, route, sched_epoch
from transit_pulse.analysis import compute_metrics
from transit_pulse.cli import main
from transit_pulse.gtfs_static import build_scheduled_events, load_gtfs
from transit_pulse.realtime import (
    UnknownRelationshipWarning,
    load_observations,
    parse_feed_message,
    parse_json_observations,
    read_observation_file,
)

HEADER_TS = sched_epoch("12:00:00")
TD = rt.TripDescriptor


def _feed() -> rt.FeedMessage:
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = HEADER_TS
    return feed


def _trip(feed, trip_id, relationship, stops, route_id="R1", start_time=None):
    """Add a TripUpdate; stops = [(stop_sequence, gtfs_time, delay) or (seq, "SKIPPED")]."""
    e = feed.entity.add(id=f"e{len(feed.entity)}")
    tu = e.trip_update
    tu.trip.trip_id = trip_id
    tu.trip.route_id = route_id
    tu.trip.start_date = "20240306"
    if start_time:
        tu.trip.start_time = start_time
    tu.trip.schedule_relationship = relationship
    for stop in stops:
        stu = tu.stop_time_update.add(stop_sequence=stop[0], stop_id=f"S{stop[0]}")
        if stop[1] == "SKIPPED":
            stu.schedule_relationship = rt.TripUpdate.StopTimeUpdate.SKIPPED
        else:
            stu.arrival.time = sched_epoch(stop[1]) + stop[2]
    return tu


def _roundtrip(feed) -> list[dict]:
    """Serialize and parse again, as the real CLI does with a .pb file."""
    parsed = rt.FeedMessage()
    parsed.ParseFromString(feed.SerializeToString())
    return parse_feed_message(parsed)


def _analyze(gtfs_zip, rows):
    con = duckdb.connect()
    load_gtfs(con, gtfs_zip)
    load_observations(con, rows)
    build_scheduled_events(con, SERVICE_DATE)
    return compute_metrics(con, SERVICE_DATE), con


def test_protobuf_replacement_matches_scheduled_trip_and_is_counted(gtfs_zip):
    feed = _feed()
    _trip(feed, "T1a", TD.REPLACEMENT, [(1, "08:00:00", 30), (2, "08:05:00", 420),
                                         (3, "SKIPPED")])
    _trip(feed, "T1b", TD.SCHEDULED, [(1, "08:10:00", 0)])
    rows = _roundtrip(feed)
    assert [r["schedule_relationship"] for r in rows] == \
        ["REPLACEMENT", "REPLACEMENT", "SKIPPED", "SCHEDULED"]

    metrics, con = _analyze(gtfs_zip, rows)
    o = metrics["overall"]
    assert o["replaced_trips"] == 1
    assert route(metrics, "R1")["replaced_trips"] == 1
    assert route(metrics, "R2")["replaced_trips"] == 0
    assert (o["observed_events"], o["on_time_events"], o["late_events"]) == (3, 2, 1)
    assert o["skipped_events"] == 1
    assert o["scheduled_trips"] == 5 and o["cancelled_trips"] == 0
    assert metrics["added_trips"]["count"] == 0
    assert metrics["inputs"]["observations_unmatched"] == 0
    delays = con.execute("SELECT stop_sequence, delay_secs FROM events "
                         "WHERE trip_id = 'T1a' ORDER BY 1").fetchall()
    assert delays == [(1, 30), (2, 420), (3, None)]


def test_replacement_for_trip_not_in_schedule_is_unmatched(gtfs_zip):
    feed = _feed()
    _trip(feed, "GHOST", TD.REPLACEMENT, [(1, "08:00:00", 0)])
    metrics, _ = _analyze(gtfs_zip, _roundtrip(feed))
    assert metrics["overall"]["replaced_trips"] == 0
    assert metrics["inputs"]["observations_unmatched"] == 1
    assert metrics["inputs"]["replacements_for_unscheduled_trips"] == 1


def test_protobuf_duplicated_trip_goes_to_added_accounting(gtfs_zip):
    feed = _feed()
    # Copy of T1a at 09:00 with its own id in trip_properties.
    tu = _trip(feed, "T1a", TD.DUPLICATED, [(1, "09:00:00", 0), (2, "09:05:00", 0)])
    tu.trip_properties.trip_id = "T1a-dup"
    tu.trip_properties.start_date = "20240306"
    tu.trip_properties.start_time = "09:00:00"
    # Copy without trip_properties.trip_id: keyed by the copied trip's id + start_time.
    # Its SKIPPED stop must not mark scheduled T1a stop 3 as skipped.
    _trip(feed, "T1a", TD.DUPLICATED, [(1, "09:30:00", 0), (3, "SKIPPED")],
          start_time="09:30:00")
    rows = _roundtrip(feed)
    assert {r["schedule_relationship"] for r in rows} == {"DUPLICATED"}
    assert rows[0]["trip_id"] == "T1a-dup" and rows[0]["start_time"] == "09:00:00"
    assert len(rows) == 3  # the SKIPPED stop of the duplicate is dropped

    metrics, con = _analyze(gtfs_zip, rows)
    added = metrics["added_trips"]
    assert added["count"] == 2 and added["observations"] == 3
    assert added["routes"][0]["trip_ids"] == ["T1a-dup", "T1a@09:30:00"]
    o = metrics["overall"]
    assert (o["added_trips"], o["duplicated_trips"], o["new_trips"]) == (2, 2, 0)
    assert route(metrics, "R1")["duplicated_trips"] == 2
    # The scheduled T1a is untouched: no observation, delay or skip from its copies.
    assert o["observed_events"] == 0 and o["skipped_events"] == 0
    assert metrics["inputs"]["observations_unmatched"] == 0
    assert metrics["inputs"]["observations_added_trips"] == 3
    assert con.execute("SELECT count(*) FROM events WHERE trip_id = 'T1a' "
                       "AND status <> 'unobserved'").fetchone() == (0,)


def test_protobuf_new_trip_goes_to_added_accounting(gtfs_zip):
    feed = _feed()
    _trip(feed, "N1", TD.NEW, [(1, "09:00:00", 0), (2, "09:05:00", 0)], route_id="R2")
    _trip(feed, "N2", TD.NEW, [(1, "09:10:00", 0)], route_id="R2")
    rows = _roundtrip(feed)
    assert {r["schedule_relationship"] for r in rows} == {"NEW"}
    metrics, _ = _analyze(gtfs_zip, rows)
    assert metrics["added_trips"]["count"] == 2
    assert route(metrics, "R2")["new_trips"] == 2
    assert route(metrics, "R2")["added_trips"] == 2
    assert metrics["overall"]["new_trips"] == 2
    assert metrics["inputs"]["observations_unmatched"] == 0


def test_vehicle_position_on_duplicated_trip_is_added():
    feed = _feed()
    e = feed.entity.add(id="vp")
    e.vehicle.trip.trip_id = "T1a"
    e.vehicle.trip.schedule_relationship = TD.DUPLICATED
    e.vehicle.current_stop_sequence = 1
    e.vehicle.current_status = rt.VehiclePosition.STOPPED_AT
    e.vehicle.timestamp = HEADER_TS - 5
    (row,) = _roundtrip(feed)
    assert row["schedule_relationship"] == "DUPLICATED"


def _unknown_relationship_bytes() -> bytes:
    """A FeedMessage whose TripDescriptor.schedule_relationship is enum number 42, which
    the bindings do not define (built by appending the raw varint field to the trip)."""
    trip = TD(trip_id="T1a", route_id="R1", start_date="20240306")
    field = TD.DESCRIPTOR.fields_by_name["schedule_relationship"].number
    trip_bytes = trip.SerializeToString() + bytes([field << 3, 42])
    feed = _feed()
    tu = feed.entity.add(id="u").trip_update
    tu.trip.ParseFromString(trip_bytes)
    stu = tu.stop_time_update.add(stop_sequence=1, stop_id="S1")
    stu.arrival.time = sched_epoch("08:00:00")
    return feed.SerializeToString()


def test_unknown_protobuf_relationship_warns_and_counts_unmatched(tmp_path, gtfs_zip):
    pb = tmp_path / "feed.pb"
    pb.write_bytes(_unknown_relationship_bytes())
    with pytest.warns(UnknownRelationshipWarning, match="enum value 42"):
        rows = read_observation_file(str(pb))
    assert [r["schedule_relationship"] for r in rows] == ["UNKNOWN"]
    metrics, _ = _analyze(gtfs_zip, rows)
    assert metrics["overall"]["observed_events"] == 0  # not silently read as SCHEDULED
    assert metrics["inputs"]["observations_unmatched"] == 1
    assert metrics["inputs"]["observations_unknown_relationship"] == 1


def test_json_relationships(gtfs_zip):
    records = [
        obs("T1a", 1, "08:00:00", 20, schedule_relationship="replacement"),
        obs("T1b-copy", 1, "09:00:00", route_id="R1", schedule_relationship="DUPLICATED"),
        obs("N9", 1, "09:00:00", route_id="R2", schedule_relationship="NEW"),
        obs("T1c", 1, "08:20:00", schedule_relationship="EXTRA_BUS"),
    ]
    with pytest.warns(UnknownRelationshipWarning, match="EXTRA_BUS"):
        rows = parse_json_observations(records)
    assert [r["schedule_relationship"] for r in rows] == \
        ["REPLACEMENT", "DUPLICATED", "NEW", "UNKNOWN"]
    metrics, _ = _analyze(gtfs_zip, rows)
    o = metrics["overall"]
    assert (o["replaced_trips"], o["observed_events"], o["on_time_events"]) == (1, 1, 1)
    assert (o["added_trips"], o["duplicated_trips"], o["new_trips"]) == (2, 1, 1)
    assert metrics["inputs"]["observations_unmatched"] == 1
    json.dumps(metrics)


def test_cli_feed_with_only_new_relationships_exits_zero(tmp_path, gtfs_zip, capsys):
    feed = _feed()
    _trip(feed, "T1a", TD.REPLACEMENT, [(1, "08:00:00", -90), (2, "08:05:00", 60)])
    tu = _trip(feed, "T1b", TD.DUPLICATED, [(1, "10:30:00", 0)])
    tu.trip_properties.trip_id = "T1b-dup"
    _trip(feed, "N1", TD.NEW, [(1, "11:00:00", 0)], route_id="R2")
    pb = tmp_path / "feed.pb"
    pb.write_bytes(feed.SerializeToString())
    unknown = tmp_path / "unknown.pb"
    unknown.write_bytes(_unknown_relationship_bytes())
    out = tmp_path / "out"

    code = main(["analyze", "--static", gtfs_zip, "--realtime", str(pb), str(unknown),
                 "--out-dir", str(out), "--date", "2024-03-06"])
    assert code == 0
    captured = capsys.readouterr()
    assert "warning: unknown schedule_relationship: 'enum value 42'" in captured.err
    assert "1 replaced trips; 2 added trips" in captured.out
    metrics = json.loads((out / "metrics.json").read_text())
    o = metrics["overall"]
    assert (o["observed_events"], o["early_events"], o["on_time_events"]) == (2, 1, 1)
    assert o["otp_pct"] == 50.0
    assert (o["replaced_trips"], o["duplicated_trips"], o["new_trips"]) == (1, 1, 1)
    assert metrics["inputs"]["observations_unmatched"] == 1
    assert "Replaced trips" in (out / "report.html").read_text()
