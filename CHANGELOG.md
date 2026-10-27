# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
