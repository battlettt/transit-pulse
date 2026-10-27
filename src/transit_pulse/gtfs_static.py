"""Load a GTFS static feed (.zip) into DuckDB and expand it into scheduled stop events.

GTFS times are "seconds since noon minus 12h" on the service date, in the agency
timezone, and may exceed 24:00:00 for trips that run past midnight. We convert every
scheduled stop time to an absolute UTC epoch so it can be compared with realtime data.
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

# Columns we create empty when an optional calendar file is absent.
EMPTY_CALENDAR_SQL = {
    "calendar": """CREATE TABLE calendar (service_id VARCHAR, monday VARCHAR, tuesday VARCHAR,
        wednesday VARCHAR, thursday VARCHAR, friday VARCHAR, saturday VARCHAR,
        sunday VARCHAR, start_date VARCHAR, end_date VARCHAR)""",
    "calendar_dates": """CREATE TABLE calendar_dates (service_id VARCHAR, date VARCHAR,
        exception_type VARCHAR)""",
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

        for name in REQUIRED_FILES + CALENDAR_FILES:
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
    """
    tz_name = agency_timezone(con)
    origin = service_day_origin_epoch(service_date, tz_name)
    services = active_service_ids(con, service_date)
    arrival = _gtfs_time_to_seconds_sql("st.arrival_time")
    departure = _gtfs_time_to_seconds_sql("st.departure_time")
    has_direction = _has_column(con, "trips", "direction_id")
    direction = "coalesce(nullif(trim(t.direction_id), ''), '0')" if has_direction else "'0'"

    con.execute("DROP TABLE IF EXISTS scheduled_events")
    con.execute(
        f"""
        CREATE TABLE scheduled_events AS
        SELECT
            row_number() OVER (ORDER BY t.trip_id, CAST(st.stop_sequence AS INTEGER)) AS event_id,
            $service_date::DATE AS service_date,
            trim(t.trip_id) AS trip_id,
            trim(t.route_id) AS route_id,
            {direction} AS direction_id,
            trim(st.stop_id) AS stop_id,
            CAST(st.stop_sequence AS INTEGER) AS stop_sequence,
            $origin + coalesce({arrival}, {departure}) AS sched_epoch
        FROM stop_times st
        JOIN trips t ON trim(t.trip_id) = trim(st.trip_id)
        WHERE trim(t.service_id) IN (SELECT unnest($services::VARCHAR[]))
          -- Non-timepoint stops with blank times are skipped (no interpolation in v1).
          AND coalesce({arrival}, {departure}) IS NOT NULL
        """,
        {"service_date": service_date.isoformat(), "origin": origin, "services": services},
    )
    return con.execute("SELECT count(*) FROM scheduled_events").fetchone()[0]


def _has_column(con, table: str, column: str) -> bool:
    return (
        con.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()[0]
        > 0
    )
