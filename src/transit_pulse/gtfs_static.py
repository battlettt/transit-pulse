"""Load a GTFS static feed (.zip) into DuckDB and expand it into scheduled stop events.

GTFS times are "seconds since noon minus 12h" on the service date, in the agency
timezone, and may exceed 24:00:00 for trips that run past midnight. We convert every
scheduled stop time to an absolute UTC epoch so it can be compared with realtime data.

Two GTFS features change what a "scheduled stop event" is:

* Blank arrival/departure times between two timed stops are estimated by linear
  interpolation over stop_sequence and flagged ``interpolated = true``. Blank times before
  the first or after the last timed stop of a trip are not estimated; those stops are
  dropped and counted in ``schedule_stop_times``.
* Trips listed in frequencies.txt are templates: their stop times give offsets from the
  first stop, and one trip instance starts at every ``headway_secs`` from ``start_time``
  (inclusive) up to ``end_time`` (exclusive).
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import zipfile
from zoneinfo import ZoneInfo

import duckdb

REQUIRED_FILES = ["agency.txt", "routes.txt", "trips.txt", "stop_times.txt", "stops.txt"]
# GTFS requires at least one of these two; the other may be absent.
CALENDAR_FILES = ["calendar.txt", "calendar_dates.txt"]
OPTIONAL_FILES = ["frequencies.txt"]

# Columns we create empty when an optional calendar file is absent.
EMPTY_CALENDAR_SQL = {
    "calendar": """CREATE TABLE calendar (service_id VARCHAR, monday VARCHAR, tuesday VARCHAR,
        wednesday VARCHAR, thursday VARCHAR, friday VARCHAR, saturday VARCHAR,
        sunday VARCHAR, start_date VARCHAR, end_date VARCHAR)""",
    "calendar_dates": """CREATE TABLE calendar_dates (service_id VARCHAR, date VARCHAR,
        exception_type VARCHAR)""",
    "frequencies": """CREATE TABLE frequencies (trip_id VARCHAR, start_time VARCHAR,
        end_time VARCHAR, headway_secs VARCHAR, exact_times VARCHAR)""",
}

WEEKDAY_COLUMNS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class GTFSError(Exception):
    """Raised when a GTFS feed is missing files or is otherwise unusable."""


def _gtfs_time_to_seconds_sql(column: str) -> str:
    """SQL expression turning 'H:MM:SS' (hours may be >= 24) into seconds, NULL if blank."""
    c = f"nullif(trim({column}), '')"
    return (
        f"(CAST(split_part({c}, ':', 1) AS INTEGER) * 3600"
        f" + CAST(split_part({c}, ':', 2) AS INTEGER) * 60"
        f" + CAST(split_part({c}, ':', 3) AS INTEGER))"
    )


def _zip_members_by_name(zf: zipfile.ZipFile) -> dict[str, str]:
    """Map 'stops.txt' -> archive member path; tolerates feeds zipped inside a folder."""
    members = {}
    for name in zf.namelist():
        base = os.path.basename(name)
        if base and base not in members:
            members[base] = name
    return members


def load_gtfs(con: duckdb.DuckDBPyConnection, zip_path: str) -> None:
    """Load the GTFS tables we need into `con`. Raises GTFSError on missing files."""
    if not os.path.exists(zip_path):
        raise GTFSError(f"GTFS file not found: {zip_path}")
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise GTFSError(f"{zip_path} is not a valid GTFS zip: {exc}") from exc

    with zf, tempfile.TemporaryDirectory() as tmp:
        members = _zip_members_by_name(zf)
        for name in REQUIRED_FILES:
            if name not in members:
                raise GTFSError(f"GTFS feed is missing required file: {name}")
        if not any(name in members for name in CALENDAR_FILES):
            raise GTFSError(
                "GTFS feed is missing required file: calendar.txt or calendar_dates.txt "
                "(at least one must be present)"
            )

        for name in REQUIRED_FILES + CALENDAR_FILES + OPTIONAL_FILES:
            table = name[:-4]
            con.execute(f"DROP TABLE IF EXISTS {table}")
            if name not in members:
                con.execute(EMPTY_CALENDAR_SQL[table])
                continue
            path = zf.extract(members[name], tmp)
            # all_varchar keeps IDs like "007" intact; we cast explicitly where needed.
            con.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_csv(?, header=true, "
                f"all_varchar=true, auto_detect=true)",
                [path],
            )

    _require_columns(con, "stop_times", ["trip_id", "stop_id", "stop_sequence"])
    _require_columns(con, "trips", ["trip_id", "route_id", "service_id"])
    _require_columns(con, "agency", ["agency_timezone"])
    _require_columns(con, "frequencies", ["trip_id", "start_time", "end_time", "headway_secs"])
    for table, column in (("stop_times", "arrival_time"), ("stop_times", "departure_time"),
                          ("frequencies", "exact_times")):
        if not _has_column(con, table, column):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR")


def _require_columns(con, table: str, columns: list[str]) -> None:
    have = {
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
        ).fetchall()
    }
    for col in columns:
        if col not in have:
            raise GTFSError(f"{table}.txt is missing required column: {col}")


def agency_timezone(con) -> str:
    row = con.execute(
        "SELECT agency_timezone FROM agency WHERE nullif(trim(agency_timezone), '') IS NOT NULL "
        "LIMIT 1"
    ).fetchone()
    if row is None:
        raise GTFSError("agency.txt has no agency_timezone value")
    return row[0].strip()


def active_service_ids(con, service_date: dt.date) -> list[str]:
    """Service IDs running on `service_date`: calendar.txt weekday ranges, then
    calendar_dates.txt exceptions (1 = added, 2 = removed) applied on top."""
    ymd = service_date.strftime("%Y%m%d")
    weekday_col = WEEKDAY_COLUMNS[service_date.weekday()]
    rows = con.execute(
        f"""
        WITH base AS (
            SELECT trim(service_id) AS service_id FROM calendar
            WHERE trim({weekday_col}) = '1'
              AND trim(start_date) <= $ymd AND trim(end_date) >= $ymd
        ),
        exceptions AS (
            SELECT trim(service_id) AS service_id, trim(exception_type) AS exception_type
            FROM calendar_dates WHERE trim(date) = $ymd
        )
        SELECT service_id FROM base
        WHERE service_id NOT IN (SELECT service_id FROM exceptions WHERE exception_type = '2')
        UNION
        SELECT service_id FROM exceptions WHERE exception_type = '1'
        ORDER BY 1
        """,
        {"ymd": ymd},
    ).fetchall()
    return [r[0] for r in rows]


def service_day_origin_epoch(service_date: dt.date, tz_name: str) -> int:
    """Epoch of 'noon minus 12h' on the service date: the zero point for GTFS times.
    This equals local midnight except on DST-change days, where GTFS defines it this way."""
    noon = dt.datetime(
        service_date.year, service_date.month, service_date.day, 12, tzinfo=ZoneInfo(tz_name)
    )
    return int(noon.timestamp()) - 12 * 3600


def build_scheduled_events(con, service_date: dt.date) -> int:
    """Create table `scheduled_events` for one service date. Returns the row count.

    Stop times past 24:00:00 belong to this service date but land on the next calendar
    day; because we add seconds to the service-day origin, that falls out naturally.

    Also creates `schedule_stop_times`: every stop time of an active trip with its raw and
    (possibly interpolated) seconds. Rows whose `secs` is NULL could not be timed and
    are not in `scheduled_events`.

    Columns added in v0.2: `trip_instance_id` (trip_id, or "trip_id@HH:MM:SS" for an
    instance of a frequency-based trip), `frequency_start_time` (that HH:MM:SS, else
    NULL), `schedule_type` ('timetable', 'frequency_exact' or 'frequency_headway') and
    `interpolated`.
    """
    tz_name = agency_timezone(con)
    origin = service_day_origin_epoch(service_date, tz_name)
    services = active_service_ids(con, service_date)
    arrival = _gtfs_time_to_seconds_sql("st.arrival_time")
    departure = _gtfs_time_to_seconds_sql("st.departure_time")
    has_direction = _has_column(con, "trips", "direction_id")
    direction = "coalesce(nullif(trim(t.direction_id), ''), '0')" if has_direction else "'0'"

    # 1. Stop times of active trips, with blank times interpolated between the nearest
    #    timed stops before and after (linear in stop_sequence). This is an estimate.
    con.execute("DROP TABLE IF EXISTS schedule_stop_times")
    con.execute(
        f"""
        CREATE TABLE schedule_stop_times AS
        WITH raw AS (
            SELECT trim(t.trip_id) AS trip_id,
                   trim(t.route_id) AS route_id,
                   {direction} AS direction_id,
                   trim(st.stop_id) AS stop_id,
                   CAST(st.stop_sequence AS INTEGER) AS stop_sequence,
                   coalesce({arrival}, {departure}) AS raw_secs
            FROM stop_times st
            JOIN trips t ON trim(t.trip_id) = trim(st.trip_id)
            WHERE trim(t.service_id) IN (SELECT unnest($services::VARCHAR[]))
        ),
        bracketed AS (
            SELECT *,
                last_value(CASE WHEN raw_secs IS NOT NULL THEN stop_sequence END IGNORE NULLS)
                    OVER before AS prev_seq,
                last_value(raw_secs IGNORE NULLS) OVER before AS prev_secs,
                first_value(CASE WHEN raw_secs IS NOT NULL THEN stop_sequence END IGNORE NULLS)
                    OVER after AS next_seq,
                first_value(raw_secs IGNORE NULLS) OVER after AS next_secs
            FROM raw
            WINDOW
                before AS (PARTITION BY trip_id ORDER BY stop_sequence
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
                after AS (PARTITION BY trip_id ORDER BY stop_sequence
                          ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING)
        )
        SELECT trip_id, route_id, direction_id, stop_id, stop_sequence, raw_secs,
               CASE
                   WHEN raw_secs IS NOT NULL THEN raw_secs
                   -- Only between two timed stops; never extrapolate past either end.
                   WHEN prev_seq IS NOT NULL AND next_seq IS NOT NULL THEN
                       prev_secs + CAST(round((next_secs - prev_secs)
                           * (stop_sequence - prev_seq) / (next_seq - prev_seq)) AS INTEGER)
               END AS secs,
               raw_secs IS NULL AS interpolated
        FROM bracketed
        """,
        {"services": services},
    )

    # 2. Frequency-based trips: one instance per headway, start inclusive, end exclusive.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE frequency_instances AS
        WITH f AS (
            SELECT trim(trip_id) AS trip_id,
                   {_gtfs_time_to_seconds_sql("start_time")} AS start_secs,
                   {_gtfs_time_to_seconds_sql("end_time")} AS end_secs,
                   TRY_CAST(nullif(trim(headway_secs), '') AS INTEGER) AS headway_secs,
                   coalesce(nullif(trim(exact_times), ''), '0') = '1' AS exact_times
            FROM frequencies
        )
        SELECT trip_id, exact_times,
               unnest(range(start_secs, end_secs, headway_secs)) AS instance_secs
        FROM f
        WHERE headway_secs > 0 AND start_secs IS NOT NULL AND end_secs > start_secs
        """
    )

    hhmmss = ("printf('%02d:%02d:%02d', fi.instance_secs // 3600, "
              "fi.instance_secs % 3600 // 60, fi.instance_secs % 60)")
    con.execute("DROP TABLE IF EXISTS scheduled_events")
    con.execute(
        f"""
        CREATE TABLE scheduled_events AS
        WITH timed AS (
            SELECT *, min_by(secs, stop_sequence) OVER (PARTITION BY trip_id) AS first_secs
            FROM schedule_stop_times WHERE secs IS NOT NULL
        ),
        expanded AS (
            SELECT t.trip_id AS trip_instance_id, NULL::VARCHAR AS frequency_start_time,
                   'timetable' AS schedule_type, t.*, t.secs AS event_secs
            FROM timed t
            WHERE t.trip_id NOT IN (SELECT trip_id FROM frequency_instances)
            UNION ALL
            SELECT t.trip_id || '@' || {hhmmss}, {hhmmss},
                   CASE WHEN fi.exact_times THEN 'frequency_exact'
                        ELSE 'frequency_headway' END,
                   t.*, fi.instance_secs + (t.secs - t.first_secs)
            FROM timed t JOIN frequency_instances fi USING (trip_id)
        )
        SELECT
            row_number() OVER (ORDER BY trip_instance_id, stop_sequence) AS event_id,
            $service_date::DATE AS service_date,
            trip_id, route_id, direction_id, stop_id, stop_sequence,
            $origin + event_secs AS sched_epoch,
            trip_instance_id, frequency_start_time, schedule_type, interpolated
        FROM expanded
        """,
        {"service_date": service_date.isoformat(), "origin": origin},
    )
    return con.execute("SELECT count(*) FROM scheduled_events").fetchone()[0]


def schedule_summary(con) -> dict:
    """How the schedule was built: interpolation, frequency expansion, untimed trips."""
    row = con.execute(
        """
        SELECT
            count(*) FILTER (WHERE interpolated),
            count(*) FILTER (WHERE schedule_type = 'frequency_exact'),
            count(*) FILTER (WHERE schedule_type = 'frequency_headway'),
            count(DISTINCT trip_instance_id) FILTER (WHERE schedule_type <> 'timetable')
        FROM scheduled_events
        """
    ).fetchone()
    untimed_trips = [
        r[0]
        for r in con.execute(
            "SELECT trip_id FROM schedule_stop_times GROUP BY trip_id "
            "HAVING count(raw_secs) = 0 ORDER BY trip_id"
        ).fetchall()
    ]
    dropped = con.execute(
        "SELECT count(*) FROM schedule_stop_times s WHERE secs IS NULL "
        "AND trip_id NOT IN (SELECT unnest($t::VARCHAR[]))",
        {"t": untimed_trips},
    ).fetchone()[0]
    return {
        "interpolated_events": row[0],
        "frequency_exact_events": row[1],
        "frequency_headway_events": row[2],
        "frequency_trip_instances": row[3],
        "untimed_stop_times_dropped": dropped,
        "trips_without_timed_stops": {"count": len(untimed_trips), "trip_ids": untimed_trips},
    }


def _has_column(con, table: str, column: str) -> bool:
    return (
        con.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()[0]
        > 0
    )
