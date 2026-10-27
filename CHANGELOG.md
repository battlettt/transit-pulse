# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.3.0 - 2026-10-27

### Fixed
- A GTFS-Realtime trip with `schedule_relationship` `REPLACEMENT`, `DUPLICATED` or `NEW`
  no longer aborts the run with `unknown schedule_relationship` (exit 2). This bug had
  existed since 0.1.0.
  - `REPLACEMENT` trips are matched to the scheduled trip with the same `trip_id` (and
    `start_time`) and scored like `SCHEDULED` ones. The scheduled trip is counted in the
    new `replaced_trips` metric.
  - `DUPLICATED` and `NEW` trips go to the `added_trips` accounting and are never
    matched to a scheduled trip. A `DUPLICATED` trip uses `trip_properties.trip_id` /
    `start_date` / `start_time` when they are present. Their `SKIPPED` stop updates are
    ignored, so a copy cannot mark the original trip's stops as skipped.
- Observation loading is about 200× faster: 16,530 rows took 17.69 s and now take
  0.08 s. `load_observations` inserted rows one at a time with `executemany` and now
  bulk-loads through a temporary CSV. Found during the real-feed validation. Results are
  unchanged (byte-identical `metrics.json`).

### Changed
- **Behaviour change:** an unknown `schedule_relationship` value (JSON, or a protobuf
  enum number the bindings do not define) now prints `warning: unknown
  schedule_relationship: ...` and the row is counted as unmatched. Before, JSON input
  raised an error (exit 2). The v0.1 test asserting that error now asserts the warning.
  Unknown protobuf enum numbers used to be read silently as `SCHEDULED`; they are now
  detected through the message's unknown fields (needs protobuf ≥ 4.25; with older
  protobuf they are still read as `SCHEDULED`).
- The CLI summary line also prints the replaced-trip count. `report.html` has a
  "Replaced trips" tile.

### Added
- Metrics `replaced_trips`, `duplicated_trips` and `new_trips` (overall and per route;
  the last two are subsets of `added_trips`). Inputs
  `observations_unknown_relationship` and `replacements_for_unscheduled_trips`.
- JSON observations accept `REPLACEMENT`, `DUPLICATED` and `NEW`.
- `realtime.UnknownRelationshipWarning`.
- `examples/REAL_FEED_VALIDATION.md`: an end-to-end run on the Big Blue Bus (Santa
  Monica) GTFS feed, with measured timings and counts, and a check of its live
  GTFS-Realtime snapshot.
  `examples/big-blue-bus/`: the observation generator, `metrics.json` and `report.html`.
- 8 tests in `tests/test_trip_relationships.py` (protobuf round-trip for each
  relationship, unknown enum on the wire, JSON, and a CLI run fed only such trips) and a
  bulk-load round-trip test in `tests/test_realtime.py`.

## 0.2.0 - 2026-10-27

### Added
- `frequencies.txt` support. Each frequency-based trip is expanded into one trip instance
  every `headway_secs`, from `start_time` (inclusive) to `end_time` (exclusive), using the
  template trip's stop-time offsets. Instances get `trip_instance_id = trip_id@HH:MM:SS`.
  Events are flagged `schedule_type = frequency_exact` (`exact_times=1`) or
  `frequency_headway` (`exact_times` 0 or blank).
- Realtime `start_time` (TripDescriptor, or the JSON `start_time` key) is captured and
  used to pick the exact frequency instance for observations and cancellations.
- Linear interpolation of blank arrival/departure times between two timed stops,
  weighted by `stop_sequence` distance and rounded to whole seconds. Interpolated events
  are flagged `interpolated = true`.
- Trips with no timed stops are left out of the schedule and reported under
  `schedule.trips_without_timed_stops`, plus a CLI warning. Before 0.2.0 they were dropped
  silently.
- `added_trips` section in `metrics.json`: realtime trips marked `ADDED` that have no
  scheduled trip, with a count, an observation count and a per-route breakdown
  (`route_id`, `route_name`, `trips`, `observations`, `trip_ids`). `overall.added_trips`
  and `routes[].added_trips` hold the same counts. `inputs.observations_added_trips`.
- `schedule` section in `metrics.json` (`interpolated_events`, `frequency_exact_events`,
  `frequency_headway_events`, `frequency_trip_instances`, `untimed_stop_times_dropped`,
  `trips_without_timed_stops`). `interpolated_events` per route and overall.
- `events.csv` output: one row per scheduled stop event, including `interpolated`,
  `schedule_type`, `trip_instance_id`, delay and status.
- Report tile for added trips, and a schedule note about interpolation, frequency trips
  and untimed trips.
- GitHub Actions CI (`.github/workflows/ci.yml`): pytest on push and pull request,
  Python 3.11 and 3.12.
- This changelog.

### Changed
- `scheduled_trips` and `cancelled_trips` count trip instances, so each run of a
  frequency-based trip is counted. Nothing changes for timetable-only feeds.
- Observations from unscheduled `ADDED` trips are no longer included in
  `inputs.observations_unmatched`. They are counted under `added_trips` instead.
- Bunching `leading_trip_id`/`following_trip_id` hold the trip instance ID. This is the
  same as `trip_id` for timetable trips.

## 0.1.0 - 2026-10-27

### Added
- Initial release: GTFS static loading into DuckDB (calendar and calendar_dates,
  times past 24:00, DST-correct service-day origin), GTFS-Realtime protobuf and JSON
  observation ingest (predictions dropped, `STOPPED_AT` vehicle positions,
  cancellations, skipped stops), matching on trip + stop_sequence with stop_id fallback
  and a 4 h guard, OTP/coverage/delay statistics, TCQSM headway adherence, bunching
  detection, delay histogram, `metrics.json` and a self-contained `report.html`, and the
  `transit-pulse analyze` CLI.
