"""Normalize GTFS-Realtime protobuf feeds and JSON observation files into one table.

Every input becomes rows of the `observations` table:

    trip_id, route_id, stop_id, stop_sequence, observed_epoch,
    schedule_relationship, start_date, feed_timestamp, source, start_time

start_time is the TripDescriptor start_time ("HH:MM:SS", may exceed 24:00:00). It is
only used to pick the right instance of a frequency-based trip.

schedule_relationship is one of SCHEDULED, ADDED, SKIPPED (one stop not served) or
CANCELED (whole trip; stop_id/stop_sequence/observed_epoch are NULL).
"""

from __future__ import annotations

import json
import os

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as rt

COLUMNS = [
    "trip_id",
    "route_id",
    "stop_id",
    "stop_sequence",
    "observed_epoch",
    "schedule_relationship",
    "start_date",
    "feed_timestamp",
    "source",
    "start_time",
]

# Spellings seen in the wild, mapped to one canonical value.
RELATIONSHIP_ALIASES = {
    "SCHEDULED": "SCHEDULED",
    "ADDED": "ADDED",
    "UNSCHEDULED": "ADDED",
    "SKIPPED": "SKIPPED",
    "CANCELED": "CANCELED",
    "CANCELLED": "CANCELED",
    "DELETED": "CANCELED",
}

_TRIP_REL = rt.TripDescriptor.ScheduleRelationship
_STOP_REL = rt.TripUpdate.StopTimeUpdate.ScheduleRelationship
_STOPPED_AT = rt.VehiclePosition.VehicleStopStatus.Value("STOPPED_AT")


class RealtimeError(Exception):
    """Raised when a realtime input cannot be parsed."""


def _row(**values) -> dict:
    row = dict.fromkeys(COLUMNS)
    row.update(values)
    return row


def normalize_relationship(value) -> str:
    key = str(value or "SCHEDULED").strip().upper()
    if key not in RELATIONSHIP_ALIASES:
        raise RealtimeError(f"unknown schedule_relationship: {value!r}")
    return RELATIONSHIP_ALIASES[key]


def parse_json_observations(records, source: str = "json") -> list[dict]:
    """Accept a list of {trip_id, route_id, stop_id, stop_sequence, observed_epoch,
    schedule_relationship[, start_date, start_time]} dicts (or {"observations": [...]})."""
    if isinstance(records, dict):
        records = records.get("observations")
    if not isinstance(records, list):
        raise RealtimeError("JSON observations must be a list (or {'observations': [...]})")
    rows = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict) or not rec.get("trip_id"):
            raise RealtimeError(f"observation #{i} has no trip_id")
        rel = normalize_relationship(rec.get("schedule_relationship"))
        epoch = rec.get("observed_epoch")
        if rel in ("SCHEDULED", "ADDED") and epoch is None:
            raise RealtimeError(f"observation #{i} ({rec['trip_id']}) has no observed_epoch")
        seq = rec.get("stop_sequence")
        rows.append(
            _row(
                trip_id=str(rec["trip_id"]),
                route_id=_str_or_none(rec.get("route_id")),
                stop_id=_str_or_none(rec.get("stop_id")),
                stop_sequence=int(seq) if seq is not None else None,
                observed_epoch=int(epoch) if epoch is not None else None,
                schedule_relationship=rel,
                start_date=_str_or_none(rec.get("start_date")),
                feed_timestamp=rec.get("feed_timestamp"),
                source=source,
                start_time=normalize_start_time(rec.get("start_time")),
            )
        )
    return rows


def normalize_start_time(value) -> str | None:
    """'8:00:00' -> '08:00:00'; blank -> None. Raises RealtimeError if malformed."""
    if value in (None, ""):
        return None
    parts = str(value).strip().split(":")
    try:
        h, m, sec = (int(x) for x in parts)
    except ValueError:
        raise RealtimeError(f"invalid start_time: {value!r} (expected HH:MM:SS)") from None
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _str_or_none(value):
    return None if value in (None, "") else str(value)


def parse_feed_message(feed: rt.FeedMessage) -> list[dict]:
    """Turn one FeedMessage into observation rows.

    StopTimeUpdates timed after the feed header timestamp are *predictions*, not
    observations, and are dropped. Only VehiclePositions with status STOPPED_AT count
    as a vehicle serving a stop.
    """
    header_ts = feed.header.timestamp or None
    rows = []
    for entity in feed.entity:
        if entity.is_deleted:
            continue
        if entity.HasField("trip_update"):
            rows.extend(_trip_update_rows(entity.trip_update, header_ts))
        if entity.HasField("vehicle"):
            row = _vehicle_row(entity.vehicle, header_ts)
            if row:
                rows.append(row)
    return rows


def _trip_fields(trip: rt.TripDescriptor) -> dict:
    return {
        "trip_id": trip.trip_id,
        "route_id": trip.route_id or None,
        "start_date": trip.start_date or None,
        "start_time": _lenient_start_time(trip.start_time),
    }


def _lenient_start_time(value) -> str | None:
    """Protobuf feeds: a malformed start_time is ignored rather than failing the run."""
    try:
        return normalize_start_time(value)
    except RealtimeError:
        return None


def _trip_update_rows(tu: rt.TripUpdate, header_ts) -> list[dict]:
    trip = tu.trip
    if not trip.trip_id:
        return []
    trip_rel = normalize_relationship(_TRIP_REL.Name(trip.schedule_relationship))
    base = dict(_trip_fields(trip), feed_timestamp=header_ts, source="trip_update")
    if trip_rel == "CANCELED":
        return [_row(schedule_relationship="CANCELED", **base)]

    rows = []
    for stu in tu.stop_time_update:
        stop_rel = _STOP_REL.Name(stu.schedule_relationship)
        if stop_rel == "NO_DATA":
            continue
        seq = stu.stop_sequence if stu.HasField("stop_sequence") else None
        stop_id = stu.stop_id or None
        if stop_rel == "SKIPPED":
            rows.append(
                _row(stop_id=stop_id, stop_sequence=seq, schedule_relationship="SKIPPED", **base)
            )
            continue
        epoch = _event_time(stu)
        if epoch is None or (header_ts and epoch > header_ts):
            continue  # no time, or a future prediction rather than an observation
        rows.append(
            _row(
                stop_id=stop_id,
                stop_sequence=seq,
                observed_epoch=epoch,
                schedule_relationship=trip_rel,
                **base,
            )
        )
    return rows


def _event_time(stu) -> int | None:
    """Prefer arrival time; fall back to departure time (e.g. at the first stop)."""
    if stu.HasField("arrival") and stu.arrival.time:
        return int(stu.arrival.time)
    if stu.HasField("departure") and stu.departure.time:
        return int(stu.departure.time)
    return None


def _vehicle_row(vp: rt.VehiclePosition, header_ts) -> dict | None:
    if not vp.HasField("trip") or not vp.trip.trip_id:
        return None
    if vp.current_status != _STOPPED_AT or not vp.timestamp:
        return None
    seq = vp.current_stop_sequence if vp.HasField("current_stop_sequence") else None
    if seq is None and not vp.stop_id:
        return None
    trip_rel = normalize_relationship(_TRIP_REL.Name(vp.trip.schedule_relationship))
    if trip_rel == "CANCELED":
        trip_rel = "SCHEDULED"  # a vehicle stopped at a stop did serve it
    return _row(
        stop_id=vp.stop_id or None,
        stop_sequence=seq,
        observed_epoch=int(vp.timestamp),
        schedule_relationship=trip_rel,
        feed_timestamp=header_ts,
        source="vehicle_position",
        **_trip_fields(vp.trip),
    )


def read_observation_file(path: str) -> list[dict]:
    """Read a .json observation file or a GTFS-Realtime protobuf file."""
    if not os.path.exists(path):
        raise RealtimeError(f"realtime file not found: {path}")
    with open(path, "rb") as fh:
        data = fh.read()
    if path.lower().endswith(".json") or data.lstrip()[:1] in (b"[", b"{"):
        try:
            records = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RealtimeError(f"{path}: invalid JSON: {exc}") from exc
        return parse_json_observations(records, source="json")
    feed = rt.FeedMessage()
    try:
        feed.ParseFromString(data)
    except DecodeError as exc:
        raise RealtimeError(f"{path}: not a GTFS-Realtime protobuf feed: {exc}") from exc
    return parse_feed_message(feed)


def load_observations(con, rows: list[dict]) -> int:
    """(Re)create the `observations` table from normalized rows. Returns row count."""
    con.execute("DROP TABLE IF EXISTS observations")
    con.execute(
        """
        CREATE TABLE observations (
            trip_id VARCHAR, route_id VARCHAR, stop_id VARCHAR, stop_sequence INTEGER,
            observed_epoch BIGINT, schedule_relationship VARCHAR, start_date VARCHAR,
            feed_timestamp BIGINT, source VARCHAR, start_time VARCHAR
        )
        """
    )
    if rows:
        con.executemany(
            f"INSERT INTO observations VALUES ({', '.join('?' * len(COLUMNS))})",
            [[r[c] for c in COLUMNS] for r in rows],
        )
    return len(rows)


def load_observation_files(con, paths: list[str]) -> int:
    rows = []
    for path in paths:
        rows.extend(read_observation_file(path))
    return load_observations(con, rows)
