import datetime as dt

import duckdb
import pytest

from conftest import SERVICE_DATE, default_feed, sched_epoch, write_gtfs_zip
from transit_pulse.gtfs_static import (
    GTFSError,
    active_service_ids,
    build_scheduled_events,
    load_gtfs,
    service_day_origin_epoch,
)


def _loaded(path):
    con = duckdb.connect()
    load_gtfs(con, path)
    return con


@pytest.mark.parametrize("missing", ["stop_times.txt", "agency.txt", "trips.txt"])
def test_missing_required_file_is_named(tmp_path, missing):
    path = write_gtfs_zip(tmp_path / "bad.zip", omit=[missing])
    with pytest.raises(GTFSError, match=missing.replace(".", r"\.")):
        _loaded(path)


def test_missing_both_calendar_files(tmp_path):
    path = write_gtfs_zip(tmp_path / "bad.zip", omit=["calendar.txt", "calendar_dates.txt"])
    with pytest.raises(GTFSError, match="calendar.txt or calendar_dates.txt"):
        _loaded(path)


def test_calendar_dates_only_feed_is_accepted(tmp_path):
    path = write_gtfs_zip(tmp_path / "cd_only.zip", omit=["calendar.txt"])
    con = _loaded(path)
    assert active_service_ids(con, dt.date(2024, 7, 4)) == ["HOL"]
    assert active_service_ids(con, SERVICE_DATE) == []


def test_not_a_zip(tmp_path):
    path = tmp_path / "x.zip"
    path.write_text("hello")
    with pytest.raises(GTFSError, match="not a valid GTFS zip"):
        _loaded(str(path))


def test_active_service_ids_weekday_weekend_and_exceptions(gtfs_zip):
    con = _loaded(gtfs_zip)
    assert active_service_ids(con, SERVICE_DATE) == ["WK"]            # Wednesday
    assert active_service_ids(con, dt.date(2024, 3, 9)) == []         # Saturday
    assert active_service_ids(con, dt.date(2025, 3, 5)) == []         # past end_date
    assert active_service_ids(con, dt.date(2024, 7, 4)) == ["HOL"]    # WK removed, HOL added


def test_calendar_dates_exception_removes_service_from_events(gtfs_zip):
    con = _loaded(gtfs_zip)
    build_scheduled_events(con, dt.date(2024, 7, 4))
    trips = {r[0] for r in con.execute("SELECT DISTINCT trip_id FROM scheduled_events").fetchall()}
    assert trips == {"T3"}

    build_scheduled_events(con, SERVICE_DATE)
    trips = {r[0] for r in con.execute("SELECT DISTINCT trip_id FROM scheduled_events").fetchall()}
    assert trips == {"T1a", "T1b", "T1c", "T2a", "T2b"}


def test_midnight_crossing_stop_times_land_on_next_calendar_day(gtfs_zip):
    con = _loaded(gtfs_zip)
    build_scheduled_events(con, SERVICE_DATE)
    rows = dict(con.execute(
        "SELECT stop_sequence, sched_epoch FROM scheduled_events WHERE trip_id = 'T2a'"
    ).fetchall())
    # 24:10 on service date 2024-03-06 is 00:10 EST on 2024-03-07 = 05:10 UTC.
    expected = int(dt.datetime(2024, 3, 7, 5, 10, tzinfo=dt.timezone.utc).timestamp())
    assert rows[2] == expected
    assert rows[1] == sched_epoch("23:50:00")
    assert rows[3] - rows[2] == 20 * 60


def test_service_day_origin_handles_dst():
    # On the spring-forward day the GTFS origin is noon - 12h, i.e. 23:00 the previous
    # evening in local wall-clock terms, not local midnight.
    origin = service_day_origin_epoch(dt.date(2024, 3, 10), "America/New_York")
    noon_edt = int(dt.datetime(2024, 3, 10, 16, 0, tzinfo=dt.timezone.utc).timestamp())
    assert noon_edt - origin == 12 * 3600


def test_ids_with_leading_zeros_preserved(tmp_path):
    feed = default_feed()
    feed["stops.txt"][0]["stop_id"] = "007"
    for st in feed["stop_times.txt"]:
        if st["stop_id"] == "S1":
            st["stop_id"] = "007"
    con = _loaded(write_gtfs_zip(tmp_path / "z.zip", feed))
    build_scheduled_events(con, SERVICE_DATE)
    assert con.execute(
        "SELECT count(*) FROM scheduled_events WHERE stop_id = '007'"
    ).fetchone()[0] == 5
