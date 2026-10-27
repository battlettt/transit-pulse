# transit-pulse

Measure how reliable public transit service **actually was**, using only public GTFS data.

transit-pulse joins a GTFS static schedule with GTFS-Realtime observations. It reports
on-time performance, headway adherence and bus bunching per route. The numbers go to
`metrics.json`, per-event rows go to `events.csv`, and it builds a self-contained HTML
dashboard.

**New in 0.2.0** (see [CHANGELOG.md](CHANGELOG.md)): `frequencies.txt` trips, interpolation
of blank stop times, counting of unscheduled `ADDED` trips, `events.csv`, and GitHub
Actions CI.

## Why it matters

Agencies publish schedules, and most also publish live GTFS-Realtime feeds, but very few
publish how the service they ran compared with the schedule. Riders, journalists and
advocates usually can't answer "how often was the 7 on time last Tuesday?" or "does the
bus really come every 10 minutes, or do two show up together every 20?" transit-pulse
answers those questions with a small, auditable pipeline. The work happens in DuckDB SQL,
and every metric is defined below.

## Install

Requires Python 3.10+. Runtime dependencies are `duckdb` and `gtfs-realtime-bindings`.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'     # dev extra adds pytest
.venv/bin/pytest                      # run the test suite
```

## Usage

```sh
transit-pulse analyze --static GTFS.zip --realtime FEED.pb --out-dir OUT \
    [--date YYYY-MM-DD] [--early-secs 60] [--late-secs 300]
```

Real example using the MBTA's public feeds:

```sh
curl -L -o mbta_gtfs.zip https://cdn.mbta.com/MBTA_GTFS.zip
# Archive a snapshot every 30 s for the period you care about:
while true; do curl -s -o "snap_$(date +%s).pb" https://cdn.mbta.com/realtime/TripUpdates.pb; sleep 30; done
# ...later:
transit-pulse analyze --static mbta_gtfs.zip --realtime snap_*.pb --out-dir out/ --date 2026-10-27
```

Then open `out/report.html` in any browser. It needs no network access. The command also
writes `out/metrics.json` and `out/events.csv`. `events.csv` has one row per scheduled stop
event, with columns `event_id, service_date, route_id, direction_id, trip_id,
trip_instance_id, frequency_start_time, schedule_type, stop_id, stop_sequence,
sched_epoch, interpolated, observed_epoch, delay_secs, status`.

- `--realtime` accepts one or more files. Each file is either a GTFS-Realtime protobuf
  (`FeedMessage` with TripUpdates and/or VehiclePositions) or a JSON file of normalized
  observations:
  ```json
  [{"trip_id": "T1", "route_id": "R1", "stop_id": "S1", "stop_sequence": 1,
    "observed_epoch": 1709730000, "schedule_relationship": "SCHEDULED"}]
  ```
  Optional keys are `start_date` (`YYYYMMDD`), `start_time` (`HH:MM:SS`, used to pick
  the instance of a frequency-based trip) and `feed_timestamp`.
  `schedule_relationship` can be `SCHEDULED`, `ADDED`, `SKIPPED`, or `CANCELED`
  (a whole trip; `CANCELLED` is also accepted).
- `--date` is the GTFS *service date*. If you omit it, the tool uses the most common
  `start_date` in the realtime data. If no `start_date` is present, it uses the local date
  of the median observation.
- `--early-secs` / `--late-secs` set the on-time window (defaults are 60 s early and
  300 s late).
- The command exits with code 2 and a clear message on bad input. For example,
  `error: GTFS feed is missing required file: stop_times.txt`.

## How the data is processed

1. **Schedule** (`gtfs_static.py`): loads agency, routes, trips, stop_times, stops,
   calendar and calendar_dates into DuckDB. Active services for the date come from
   `calendar.txt` weekday ranges plus `calendar_dates.txt` exceptions (type 1 adds a
   service, type 2 removes it). Each stop time becomes an absolute epoch measured from
   "noon minus 12 h" in the agency timezone. Times past `24:00:00` therefore land on the
   next calendar day while still belonging to this service date, and DST days are
   handled correctly.
   - **Blank stop times (interpolation).** When a stop has neither an arrival nor a
     departure time but lies between two timed stops of the same trip, its time is
     estimated by linear interpolation over `stop_sequence` distance and rounded to the
     second. For example, timed stops at sequences 2 (10:00) and 11 (10:09) put sequence 3
     at 10:01. This is an **estimate**, because real stops are rarely evenly spaced in
     time, so those events are flagged `interpolated = true` (in `events.csv`, and counted
     as `interpolated_events`). Blank stops before a trip's first timed stop or after its
     last one are never extrapolated. They are dropped and counted in
     `schedule.untimed_stop_times_dropped`. A trip with no timed stops at all is left out
     of the schedule, listed in `schedule.trips_without_timed_stops`, and triggers a CLI
     warning. Its observations show up as `observations_unmatched`.
   - **frequencies.txt.** A trip listed in `frequencies.txt` is a template. Its stop times
     are used only as offsets from its first stop. For each frequency row, one trip
     instance starts at `start_time`, `start_time + headway_secs`, and so on, while the
     start is before `end_time` (end exclusive, so 07:00–08:00 every 15 min gives 07:00,
     07:15, 07:30 and 07:45). Each instance is its own trip (`trip_instance_id =
     trip_id@HH:MM:SS`) in `scheduled_trips` and headway/bunching analysis. Rows with
     `headway_secs` ≤ 0 or unparseable are ignored.
     - `exact_times=1` (`schedule_type = frequency_exact`): instances are treated as a
       normal timetable at exactly those times.
     - `exact_times=0` or blank (`schedule_type = frequency_headway`): GTFS only promises
       a headway, not start times. **Assumption:** we still place instances on the same
       nominal grid, so delays for these trips are measured against a nominal start, not
       a published time. Without a realtime `start_time`, an observation matches the
       nearest instance, so its delay is never larger than about half a headway. Read
       OTP for headway-based routes with care. Headway adherence and bunching are the
       meaningful metrics for them.
2. **Observations** (`realtime.py`): uses the arrival time of a TripUpdate
   StopTimeUpdate, or the departure time if there is no arrival. StopTimeUpdates timed
   *after* the feed header timestamp are predictions, so they are dropped. A
   VehiclePosition counts only when its status is `STOPPED_AT`. TripDescriptor
   `CANCELED` (or `DELETED`) marks a cancelled trip, and StopTimeUpdate `SKIPPED` marks a
   skipped stop.
3. **Matching** (`analysis.py`): observations are joined to scheduled events on
   `trip_id + stop_sequence`. When the observation has no sequence, or its sequence is
   not in the schedule, the join falls back to `trip_id + stop_id` and picks the nearest
   scheduled time. If several reports exist for one event, the newest feed wins, then
   the earliest time. Observations more than 4 h from the scheduled time are left
   unmatched, which stops the same `trip_id` on an adjacent day from showing up as a
   24 h delay. For frequency-based trips, every instance shares the `trip_id`. A realtime
   `start_time` selects one instance exactly, for observations and for `CANCELED`
   trips; otherwise the nearest instance in time wins (and a cancellation with no
   `start_time` cancels every instance).
4. **ADDED trips.** A realtime trip marked `ADDED` whose `trip_id` is not in the schedule
   for the service date has no scheduled times, so it cannot have a delay and is kept out
   of OTP. Before 0.2.0 such trips were lost among the unmatched observations. They are
   now counted under `added_trips` in `metrics.json`:
   ```json
   "added_trips": {"count": 2, "observations": 3, "routes": [
     {"route_id": "R1", "route_name": "1", "trips": 2, "observations": 3,
      "trip_ids": ["X1", "X2"]}]}
   ```
   The route comes from the realtime TripDescriptor (`null` if the feed omits it).
   `overall.added_trips` and `routes[].added_trips` hold the same counts. An `ADDED` trip
   whose `trip_id` *does* exist in the schedule is matched like a scheduled trip. Stops
   marked `SKIPPED` on scheduled trips are still counted as `skipped_events`.

## Metric definitions

| Metric | Definition |
|---|---|
| `delay_secs` | observed time − scheduled time for one stop event (positive = late) |
| early / on time / late | on time if `-early_secs ≤ delay ≤ late_secs` (both bounds inclusive) |
| `otp_pct` | on-time events ÷ **observed** events × 100 |
| `coverage_pct` | observed events ÷ (scheduled events − cancelled − skipped) × 100 |
| `mean/median/p90_abs_delay_secs` | statistics of \|delay\| over observed events |
| `mean_delay_secs` | signed mean delay (shows a systematic early/late bias) |
| `unobserved_events` | scheduled events with no observation. These have **no** delay (never counted as 0) and are excluded from OTP |
| `cancelled_trips` / `cancelled_events` | scheduled trips marked CANCELED and their stop events. They are reported separately, kept out of OTP, and never dropped |
| `skipped_events` | stops marked SKIPPED |
| `scheduled_trips` | distinct trip instances (each run of a frequency-based trip counts once) |
| `interpolated_events` | scheduled events whose time was estimated by interpolation |
| `added_trips` | realtime `ADDED` trips with no scheduled trip, see above. Not part of OTP |
| `headway_deviation_cv` | at high-frequency stops (median scheduled headway ≤ 15 min): stddev(observed headway − scheduled headway) ÷ mean scheduled headway. This is the TCQSM headway-adherence measure. 0 means perfectly regular service |
| `mean_abs_headway_deviation_secs` | mean \|observed − scheduled headway\| at those stops |
| bunching event | two consecutive trips of the same route and direction observed at the same stop ≤ 120 s apart, when their scheduled times were ≥ 300 s apart |
| `delay_histogram` | count of observed events per 1-minute delay bin from −5 to +15 min. The outer bins collect everything beyond that range |

Metrics are reported for all routes together (`overall`) and for each route (`routes`).
`inputs` reports unmatched observations, observations belonging to added trips, and
cancellations for trips that are not in the schedule. `schedule` describes how the
schedule was built: interpolated events, frequency events by type, frequency trip
instances, dropped untimed stop times, and trips without timed stops.

## Limitations: what this cannot tell you

- **Realtime coverage gaps.** OTP is computed only over observed stop events. If a feed
  drops a vehicle, a trip, or a whole hour, those events show up as `unobserved` and
  lower `coverage_pct`, but the tool cannot know whether the bus ran. Low coverage makes
  OTP unreliable, so always read the two numbers together.
- **One snapshot is not history.** A single `TripUpdates.pb` mostly contains
  predictions, which are discarded. Meaningful results need snapshots archived every
  15–60 s across the whole period.
- **Feed-reported times are not ground truth.** Many agencies set "arrival" times when
  the AVL system detects a stop, and some extrapolate or copy predictions. transit-pulse
  measures what the feed says.
- **Headways are affected by gaps.** A missing observation between two observed trips
  doubles the observed headway at that stop. Cancelled trips do the same, but in that
  case the gap is real.
- **Interpolated times are estimates.** Linear interpolation assumes even spacing per
  `stop_sequence` step. It does not use `shape_dist_traveled` or stop distances, so
  delays at interpolated stops can be off by a minute or more. Filter on
  `interpolated` in `events.csv` if that matters.
- **Headway-based frequency trips have no true schedule.** Their delays are measured
  against a nominal start grid (see above). OTP for those routes means less than for
  timetabled ones.
- **ADDED trips are counted, not evaluated.** They have no schedule, so they contribute
  no delay, OTP or headway figures. Their `SKIPPED` stop updates are not counted, and
  stop-level ADDED/NEW stops on scheduled trips are not modelled.
- **Still not supported in 0.2.0:** interpolation using `shape_dist_traveled`;
  `frequencies.txt` without a realtime `start_time` cannot tell apart two instances that
  are close in time; GTFS-RT `REPLACEMENT`/`DUPLICATED`/`NEW` trips (a protobuf feed that contains
  one currently fails with `unknown schedule_relationship`) and `trip_properties`;
  `block_id`/vehicle-level analysis; passenger-weighted metrics; multi-day runs (one
  service date per run). Performance on very large feeds is still untested.

## Development

```
src/transit_pulse/
  gtfs_static.py   GTFS zip -> DuckDB tables, service dates, scheduled_events
  realtime.py      protobuf / JSON -> observations table
  analysis.py      matching + metrics (DuckDB SQL)
  report.py        metrics.json + report.html (inline SVG) + events.csv
  cli.py           argparse entry point
tests/             pytest suite with an in-test synthetic GTFS builder (tests/conftest.py)
.github/workflows/ci.yml   pytest on push / pull_request, Python 3.11 and 3.12
```

See [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

MIT. See [LICENSE](LICENSE).
