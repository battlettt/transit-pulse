"""Join scheduled stop events to realtime observations and compute reliability metrics.

Expects tables `scheduled_events` (gtfs_static.build_scheduled_events) and
`observations` (realtime.load_observations) in the same DuckDB connection.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .gtfs_static import schedule_summary

# An observation more than this far from its scheduled time is treated as unmatched,
# so the same trip_id on an adjacent service day cannot produce a ~24h "delay".
MAX_MATCH_WINDOW_SECS = 4 * 3600
# A stop counts as high-frequency when its median scheduled headway is at most this.
HIGH_FREQUENCY_HEADWAY_SECS = 15 * 60
BUNCHING_OBSERVED_GAP_SECS = 120
BUNCHING_MIN_SCHEDULED_GAP_SECS = 300
HISTOGRAM_BIN_SECS = 60
HISTOGRAM_MIN_SECS = -300
HISTOGRAM_MAX_SECS = 900


@dataclass
class Thresholds:
    """On time means -early_secs <= delay <= late_secs (both bounds inclusive)."""

    early_secs: int = 60
    late_secs: int = 300


def build_events(con, service_date: dt.date, thresholds: Thresholds) -> None:
    """Create table `events`: one row per scheduled stop event with its match status.

    status is one of: early, on_time, late, unobserved, skipped, cancelled.
    delay_secs is NULL unless the event was observed (never a fake zero).
    """
    start_date = service_date.strftime("%Y%m%d")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE obs AS
        SELECT row_number() OVER () AS obs_id, *
        FROM observations
        WHERE (start_date IS NULL OR start_date = $start_date)
        """,
        {"start_date": start_date},
    )
    # Candidate matches: trip_id + stop_sequence first; trip_id + stop_id as fallback
    # when the observation has no sequence or its sequence is not in the schedule.
    # Frequency-based trips share one trip_id across instances: a realtime start_time
    # selects the instance exactly, otherwise the nearest instance in time wins.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE obs_match AS
        WITH candidates AS (
            SELECT o.obs_id, e.event_id, 1 AS match_rank,
                   abs(o.observed_epoch - e.sched_epoch) AS distance
            FROM obs o
            JOIN scheduled_events e
              ON o.trip_id = e.trip_id AND o.stop_sequence = e.stop_sequence
             AND (o.start_time IS NULL OR e.frequency_start_time IS NULL
                   OR o.start_time = e.frequency_start_time)
            UNION ALL
            SELECT o.obs_id, e.event_id, 2 AS match_rank,
                   abs(o.observed_epoch - e.sched_epoch) AS distance
            FROM obs o
            JOIN scheduled_events e ON o.trip_id = e.trip_id AND o.stop_id = e.stop_id
             AND (o.start_time IS NULL OR e.frequency_start_time IS NULL
                   OR o.start_time = e.frequency_start_time)
            WHERE (o.stop_sequence IS NULL
               OR NOT EXISTS (SELECT 1 FROM scheduled_events s
                              WHERE s.trip_id = o.trip_id
                                AND s.stop_sequence = o.stop_sequence))
        )
        SELECT obs_id, event_id, match_rank FROM candidates
        QUALIFY row_number() OVER (PARTITION BY obs_id
                                   ORDER BY match_rank, distance NULLS LAST) = 1
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE event_obs AS
        SELECT m.event_id, o.observed_epoch, o.schedule_relationship
        FROM obs_match m JOIN obs o USING (obs_id)
        WHERE o.schedule_relationship IN ('SCHEDULED', 'ADDED')
          AND abs(o.observed_epoch - (SELECT sched_epoch FROM scheduled_events s
                                     WHERE s.event_id = m.event_id)) <= $window
        -- Several reports for one event: trust the newest feed, then the earliest time.
        QUALIFY row_number() OVER (PARTITION BY m.event_id
                                   ORDER BY o.feed_timestamp DESC NULLS LAST,
                                            o.observed_epoch) = 1
        """,
        {"window": MAX_MATCH_WINDOW_SECS},
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE event_skipped AS
        SELECT DISTINCT m.event_id FROM obs_match m JOIN obs o USING (obs_id)
        WHERE o.schedule_relationship = 'SKIPPED'
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE cancelled_trips AS
        SELECT DISTINCT trip_id, start_time FROM obs WHERE schedule_relationship = 'CANCELED'
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE events AS
        WITH cancelled_events AS (
            -- A start_time on the cancellation narrows it to one frequency instance.
            SELECT DISTINCT e.event_id FROM scheduled_events e
            JOIN cancelled_trips c ON c.trip_id = e.trip_id
             AND (c.start_time IS NULL OR e.frequency_start_time IS NULL
                  OR c.start_time = e.frequency_start_time)
        )
        SELECT e.*,
               eo.observed_epoch,
               eo.observed_epoch - e.sched_epoch AS delay_secs,
               e.event_id IN (SELECT event_id FROM cancelled_events) AS trip_cancelled,
               CASE
                   -- An actual observation wins: the vehicle demonstrably served the stop.
                   WHEN eo.observed_epoch IS NOT NULL THEN
                       CASE WHEN eo.observed_epoch - e.sched_epoch < -$early THEN 'early'
                            WHEN eo.observed_epoch - e.sched_epoch > $late THEN 'late'
                            ELSE 'on_time' END
                   WHEN e.event_id IN (SELECT event_id FROM cancelled_events) THEN 'cancelled'
                   WHEN e.event_id IN (SELECT event_id FROM event_skipped) THEN 'skipped'
                   ELSE 'unobserved'
               END AS status
        FROM scheduled_events e
        LEFT JOIN event_obs eo USING (event_id)
        """,
        {"early": thresholds.early_secs, "late": thresholds.late_secs},
    )


def _rows_as_dicts(cursor) -> list[dict]:
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _clean(value):
    """Round floats and convert DuckDB/Decimal values into plain JSON types."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return round(float(value), 2)


def performance_metrics(con) -> tuple[dict, list[dict]]:
    """OTP and delay statistics overall and per route, via GROUPING SETS."""
    cursor = con.execute(
        """
        SELECT
            route_id,
            GROUPING(route_id) = 1 AS is_overall,
            count(*) AS scheduled_events,
            count(DISTINCT trip_instance_id) AS scheduled_trips,
            count(DISTINCT trip_instance_id) FILTER (WHERE trip_cancelled) AS cancelled_trips,
            count(*) FILTER (WHERE status = 'cancelled') AS cancelled_events,
            count(*) FILTER (WHERE status = 'skipped') AS skipped_events,
            count(*) FILTER (WHERE status = 'unobserved') AS unobserved_events,
            count(*) FILTER (WHERE delay_secs IS NOT NULL) AS observed_events,
            count(*) FILTER (WHERE status = 'early') AS early_events,
            count(*) FILTER (WHERE status = 'on_time') AS on_time_events,
            count(*) FILTER (WHERE status = 'late') AS late_events,
            count(*) FILTER (WHERE interpolated) AS interpolated_events,
            100.0 * count(*) FILTER (WHERE status = 'on_time')
                / nullif(count(*) FILTER (WHERE delay_secs IS NOT NULL), 0) AS otp_pct,
            100.0 * count(*) FILTER (WHERE delay_secs IS NOT NULL)
                / nullif(count(*) FILTER (WHERE status NOT IN ('cancelled', 'skipped')), 0)
                AS coverage_pct,
            avg(delay_secs) AS mean_delay_secs,
            avg(abs(delay_secs)) AS mean_abs_delay_secs,
            median(abs(delay_secs)) AS median_abs_delay_secs,
            quantile_cont(abs(delay_secs), 0.9) AS p90_abs_delay_secs
        FROM events
        GROUP BY GROUPING SETS ((route_id), ())
        ORDER BY is_overall DESC, route_id
        """
    )
    overall, routes = {}, []
    for row in _rows_as_dicts(cursor):
        is_overall = row.pop("is_overall")
        clean = {k: _clean(v) for k, v in row.items()}
        if is_overall:
            clean.pop("route_id")
            overall = clean
        else:
            routes.append(clean)
    return overall, routes


def _observed_pairs_sql() -> str:
    """Consecutive observed arrivals at the same route/direction/stop, ordered by the
    observed time, with the scheduled gap between those same two trips."""
    return """
        SELECT route_id, direction_id, stop_id,
               lag(trip_instance_id) OVER w AS prev_trip_id, trip_instance_id AS trip_id,
               observed_epoch - lag(observed_epoch) OVER w AS observed_headway,
               sched_epoch - lag(sched_epoch) OVER w AS scheduled_headway
        FROM events
        WHERE observed_epoch IS NOT NULL
        WINDOW w AS (PARTITION BY route_id, direction_id, stop_id ORDER BY observed_epoch)
    """


def headway_metrics(con) -> dict[str, dict]:
    """Headway adherence at high-frequency stops, per route (key None = overall).

    headway_deviation_cv follows the TCQSM definition: the standard deviation of
    (observed - scheduled headway) divided by the mean scheduled headway.
    """
    cursor = con.execute(
        f"""
        WITH sched AS (
            SELECT route_id, direction_id, stop_id,
                   sched_epoch - lag(sched_epoch) OVER (
                       PARTITION BY route_id, direction_id, stop_id ORDER BY sched_epoch
                   ) AS headway
            FROM events
        ),
        frequent_stops AS (
            SELECT route_id, direction_id, stop_id FROM sched
            WHERE headway IS NOT NULL
            GROUP BY ALL
            HAVING median(headway) <= $max_headway
        ),
        pairs AS (
            SELECT p.* FROM ({_observed_pairs_sql()}) p
            JOIN frequent_stops USING (route_id, direction_id, stop_id)
            WHERE p.observed_headway IS NOT NULL
        )
        SELECT route_id,
               GROUPING(route_id) = 1 AS is_overall,
               count(*) AS headway_pairs,
               avg(scheduled_headway) AS mean_scheduled_headway_secs,
               avg(observed_headway) AS mean_observed_headway_secs,
               avg(abs(observed_headway - scheduled_headway)) AS mean_abs_headway_deviation_secs,
               stddev_pop(observed_headway - scheduled_headway)
                   / nullif(avg(scheduled_headway), 0) AS headway_deviation_cv
        FROM pairs
        GROUP BY GROUPING SETS ((route_id), ())
        """,
        {"max_headway": HIGH_FREQUENCY_HEADWAY_SECS},
    )
    result = {}
    for row in _rows_as_dicts(cursor):
        key = None if row.pop("is_overall") else row["route_id"]
        row.pop("route_id")
        result[key] = {k: _clean(v) for k, v in row.items()}
    return result


def bunching_events(con) -> list[dict]:
    """Two trips of one route serving the same stop within 120 s of each other when
    their scheduled times were at least 300 s apart."""
    cursor = con.execute(
        f"""
        SELECT route_id, direction_id, stop_id, prev_trip_id AS leading_trip_id,
               trip_id AS following_trip_id, observed_headway AS observed_gap_secs,
               abs(scheduled_headway) AS scheduled_gap_secs
        FROM ({_observed_pairs_sql()})
        WHERE observed_headway <= $max_gap AND abs(scheduled_headway) >= $min_sched
        ORDER BY route_id, stop_id, following_trip_id
        """,
        {
            "max_gap": BUNCHING_OBSERVED_GAP_SECS,
            "min_sched": BUNCHING_MIN_SCHEDULED_GAP_SECS,
        },
    )
    return [{k: _clean(v) for k, v in row.items()} for row in _rows_as_dicts(cursor)]


def delay_histogram(con) -> list[dict]:
    """Signed delay counts in 1-minute bins; the outer bins collect everything beyond."""
    rows = con.execute(
        """
        SELECT greatest(least(floor(delay_secs / $w) * $w, $hi), $lo - $w) AS bin, count(*)
        FROM events WHERE delay_secs IS NOT NULL GROUP BY 1
        """,
        {"w": HISTOGRAM_BIN_SECS, "lo": HISTOGRAM_MIN_SECS, "hi": HISTOGRAM_MAX_SECS},
    ).fetchall()
    counts = {int(b): n for b, n in rows}
    bins = []
    for start in range(HISTOGRAM_MIN_SECS - HISTOGRAM_BIN_SECS, HISTOGRAM_MAX_SECS + 1,
                       HISTOGRAM_BIN_SECS):
        bins.append(
            {
                "bin_start_secs": None if start < HISTOGRAM_MIN_SECS else start,
                "bin_end_secs": None if start >= HISTOGRAM_MAX_SECS else start
                + HISTOGRAM_BIN_SECS,
                "count": counts.get(start, 0),
            }
        )
    return bins


def input_summary(con) -> dict:
    row = con.execute(
        """
        SELECT
            (SELECT count(*) FROM observations) AS observations_total,
            (SELECT count(*) FROM obs) AS observations_for_service_date,
            (SELECT count(*) FROM obs
             WHERE schedule_relationship IN ('SCHEDULED', 'ADDED')
               AND obs_id NOT IN (SELECT obs_id FROM obs_match)
               AND obs_id NOT IN (SELECT obs_id FROM added_obs)) AS observations_unmatched,
            (SELECT count(*) FROM added_obs) AS observations_added_trips,
            (SELECT count(*) FROM cancelled_trips
             WHERE trip_id NOT IN (SELECT trip_id FROM scheduled_events))
                AS cancellations_for_unscheduled_trips
        """
    ).fetchone()
    names = ["observations_total", "observations_for_service_date",
             "observations_unmatched", "observations_added_trips",
             "cancellations_for_unscheduled_trips"]
    return dict(zip(names, row))


def added_trips(con, names: dict[str, str]) -> dict:
    """Realtime trips marked ADDED whose trip_id is not in the schedule for this date.

    They have no scheduled times, so they cannot have a delay or count toward OTP; they
    are counted here instead of being dropped. Route comes from the realtime
    TripDescriptor (null when the feed omits it).
    """
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE added_obs AS
        SELECT * FROM obs
        WHERE schedule_relationship = 'ADDED'
          AND trip_id NOT IN (SELECT trip_id FROM scheduled_events)
        """
    )
    rows = con.execute(
        """
        WITH trips AS (
            SELECT trip_id, max(route_id) AS route_id,
                   count(*) FILTER (WHERE observed_epoch IS NOT NULL) AS observations
            FROM added_obs GROUP BY trip_id
        )
        SELECT route_id, count(*) AS trips, sum(observations)::BIGINT AS observations,
               list(trip_id ORDER BY trip_id) AS trip_ids
        FROM trips GROUP BY route_id ORDER BY route_id NULLS LAST
        """
    ).fetchall()
    routes = [
        {"route_id": r, "route_name": names.get(r, r) if r else None, "trips": n,
         "observations": o, "trip_ids": list(ids)}
        for r, n, o, ids in rows
    ]
    return {
        "count": sum(r["trips"] for r in routes),
        "observations": sum(r["observations"] for r in routes),
        "routes": routes,
    }


def route_names(con) -> dict[str, str]:
    rows = con.execute(
        "SELECT trim(route_id), coalesce(nullif(trim(route_short_name), ''), "
        "nullif(trim(route_long_name), ''), trim(route_id)) FROM routes"
    ).fetchall()
    return dict(rows)


def infer_service_date(con, tz_name: str) -> dt.date | None:
    """Best guess when --date is not given: the most common TripDescriptor start_date,
    else the local calendar date of the median observation time."""
    row = con.execute(
        """
        SELECT start_date FROM observations WHERE start_date IS NOT NULL
        GROUP BY start_date ORDER BY count(*) DESC, start_date LIMIT 1
        """
    ).fetchone()
    if row:
        return dt.datetime.strptime(row[0], "%Y%m%d").date()
    row = con.execute("SELECT median(observed_epoch)::BIGINT FROM observations").fetchone()
    if row[0] is None:
        return None
    return dt.datetime.fromtimestamp(row[0], ZoneInfo(tz_name)).date()


def compute_metrics(con, service_date: dt.date, thresholds: Thresholds | None = None) -> dict:
    """Run the whole analysis and return a JSON-serializable dict."""
    thresholds = thresholds or Thresholds()
    build_events(con, service_date, thresholds)
    overall, routes = performance_metrics(con)
    headways = headway_metrics(con)
    bunching = bunching_events(con)
    names = route_names(con)
    added = added_trips(con, names)
    added_by_route = {r["route_id"]: r["trips"] for r in added["routes"]}

    empty_headway = {
        "headway_pairs": 0,
        "mean_scheduled_headway_secs": None,
        "mean_observed_headway_secs": None,
        "mean_abs_headway_deviation_secs": None,
        "headway_deviation_cv": None,
    }
    overall.update(headways.get(None, empty_headway))
    overall["bunching_events"] = len(bunching)
    overall["added_trips"] = added["count"]
    for route in routes:
        route["route_name"] = names.get(route["route_id"], route["route_id"])
        route.update(headways.get(route["route_id"], empty_headway))
        route["bunching_events"] = sum(1 for b in bunching if b["route_id"] == route["route_id"])
        route["added_trips"] = added_by_route.get(route["route_id"], 0)

    return {
        "service_date": service_date.isoformat(),
        "thresholds": {"early_secs": thresholds.early_secs, "late_secs": thresholds.late_secs},
        "overall": overall,
        "routes": routes,
        "bunching": bunching,
        "delay_histogram": delay_histogram(con),
        "added_trips": added,
        "schedule": schedule_summary(con),
        "inputs": input_summary(con),
    }
