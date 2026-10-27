import json

import pytest
from google.transit import gtfs_realtime_pb2 as rt

from transit_pulse.realtime import (
    RealtimeError,
    UnknownRelationshipWarning,
    parse_feed_message,
    load_observations,
    parse_json_observations,
    read_observation_file,
)

HEADER_TS = 1_709_730_000


def _feed() -> rt.FeedMessage:
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = HEADER_TS

    e = feed.entity.add(id="tu1")
    e.trip_update.trip.trip_id = "T1a"
    e.trip_update.trip.route_id = "R1"
    e.trip_update.trip.start_date = "20240306"
    past = e.trip_update.stop_time_update.add(stop_sequence=1, stop_id="S1")
    past.arrival.time = HEADER_TS - 100
    dep_only = e.trip_update.stop_time_update.add(stop_sequence=2, stop_id="S2")
    dep_only.departure.time = HEADER_TS - 10
    future = e.trip_update.stop_time_update.add(stop_sequence=3, stop_id="S3")
    future.arrival.time = HEADER_TS + 300  # a prediction, must be dropped

    e = feed.entity.add(id="tu2")
    e.trip_update.trip.trip_id = "T1b"
    skipped = e.trip_update.stop_time_update.add(stop_sequence=2, stop_id="S2")
    skipped.schedule_relationship = rt.TripUpdate.StopTimeUpdate.SKIPPED

    e = feed.entity.add(id="cx")
    e.trip_update.trip.trip_id = "T2b"
    e.trip_update.trip.schedule_relationship = rt.TripDescriptor.CANCELED

    e = feed.entity.add(id="vp1")
    e.vehicle.trip.trip_id = "T1c"
    e.vehicle.stop_id = "S1"
    e.vehicle.current_stop_sequence = 1
    e.vehicle.current_status = rt.VehiclePosition.STOPPED_AT
    e.vehicle.timestamp = HEADER_TS - 5

    e = feed.entity.add(id="vp2")
    e.vehicle.trip.trip_id = "T1c"
    e.vehicle.current_stop_sequence = 2
    e.vehicle.current_status = rt.VehiclePosition.IN_TRANSIT_TO
    e.vehicle.timestamp = HEADER_TS
    return feed


def test_protobuf_feed_is_normalized():
    rows = parse_feed_message(_feed())
    by_key = {(r["trip_id"], r["stop_sequence"], r["source"]): r for r in rows}

    assert by_key[("T1a", 1, "trip_update")]["observed_epoch"] == HEADER_TS - 100
    assert by_key[("T1a", 1, "trip_update")]["start_date"] == "20240306"
    assert by_key[("T1a", 2, "trip_update")]["observed_epoch"] == HEADER_TS - 10
    assert ("T1a", 3, "trip_update") not in by_key  # future prediction dropped
    assert by_key[("T1b", 2, "trip_update")]["schedule_relationship"] == "SKIPPED"
    assert by_key[("T2b", None, "trip_update")]["schedule_relationship"] == "CANCELED"
    assert by_key[("T1c", 1, "vehicle_position")]["observed_epoch"] == HEADER_TS - 5
    assert ("T1c", 2, "vehicle_position") not in by_key  # IN_TRANSIT_TO is not a stop event
    assert len(rows) == 5


def test_read_protobuf_and_json_files(tmp_path):
    pb = tmp_path / "feed.pb"
    pb.write_bytes(_feed().SerializeToString())
    assert len(read_observation_file(str(pb))) == 5

    js = tmp_path / "obs.json"
    js.write_text(json.dumps({"observations": [
        {"trip_id": "T1a", "stop_sequence": 1, "observed_epoch": 1, "stop_id": "S1"},
        {"trip_id": "T2b", "schedule_relationship": "cancelled"},
    ]}))
    rows = read_observation_file(str(js))
    assert [r["schedule_relationship"] for r in rows] == ["SCHEDULED", "CANCELED"]


def test_json_validation_errors():
    with pytest.raises(RealtimeError, match="no trip_id"):
        parse_json_observations([{"stop_id": "S1", "observed_epoch": 1}])
    with pytest.raises(RealtimeError, match="no observed_epoch"):
        parse_json_observations([{"trip_id": "T1a", "stop_sequence": 1}])
    # Since 0.3.0 an unknown value warns and is kept as UNKNOWN (counted unmatched).
    with pytest.warns(UnknownRelationshipWarning, match="unknown schedule_relationship"):
        rows = parse_json_observations([{"trip_id": "T1a", "schedule_relationship": "LATE"}])
    assert rows[0]["schedule_relationship"] == "UNKNOWN"


def test_garbage_file_rejected(tmp_path):
    bad = tmp_path / "feed.pb"
    bad.write_bytes(b"\xff\xff\xff not protobuf")
    with pytest.raises(RealtimeError):
        read_observation_file(str(bad))


def test_protobuf_added_trip_and_start_time():
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = HEADER_TS
    e = feed.entity.add(id="added")
    e.trip_update.trip.trip_id = "X1"
    e.trip_update.trip.route_id = "R1"
    e.trip_update.trip.start_time = "7:15:00"
    e.trip_update.trip.schedule_relationship = rt.TripDescriptor.ADDED
    stu = e.trip_update.stop_time_update.add(stop_sequence=1, stop_id="S1")
    stu.arrival.time = HEADER_TS - 60
    e = feed.entity.add(id="bad_start")
    e.trip_update.trip.trip_id = "T1a"
    e.trip_update.trip.start_time = "soon"  # malformed: ignored, not fatal
    stu = e.trip_update.stop_time_update.add(stop_sequence=1, stop_id="S1")
    stu.arrival.time = HEADER_TS - 60
    added, other = parse_feed_message(feed)
    assert added["schedule_relationship"] == "ADDED"
    assert (added["route_id"], added["start_time"]) == ("R1", "07:15:00")
    assert other["start_time"] is None


def test_json_start_time_validation():
    rows = parse_json_observations([{"trip_id": "F1", "stop_sequence": 1, "observed_epoch": 1,
                                     "start_time": "25:05:00"}])
    assert rows[0]["start_time"] == "25:05:00"
    with pytest.raises(RealtimeError, match="invalid start_time"):
        parse_json_observations([{"trip_id": "F1", "observed_epoch": 1, "start_time": "7am"}])


def test_load_observations_bulk_round_trip():
    """The CSV bulk load keeps awkward strings intact and NULL distinct from ''."""
    import duckdb

    con = duckdb.connect()
    rows = parse_json_observations([
        {"trip_id": 'a,"b"\nc', "stop_id": "S1", "stop_sequence": 3, "observed_epoch": 7,
         "feed_timestamp": 5, "start_time": "8:00:00"},
        {"trip_id": "ü", "schedule_relationship": "CANCELED"},
    ])
    rows[0]["route_id"] = ""
    assert load_observations(con, rows) == 2
    got = con.execute("SELECT * FROM observations ORDER BY trip_id").fetchall()
    assert got == [
        ('a,"b"\nc', "", "S1", 3, 7, "SCHEDULED", None, 5, "json", "08:00:00"),
        ("ü", None, None, None, None, "CANCELED", None, None, "json", None),
    ]
    assert load_observations(con, []) == 0
    assert con.execute("SELECT count(*) FROM observations").fetchone() == (0,)
