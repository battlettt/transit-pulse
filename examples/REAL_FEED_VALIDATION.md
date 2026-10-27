# Real-feed validation (transit-pulse 0.3.0)

Every number below was produced by a command run on **2026-10-27** (UTC 14:39–14:47) on a
2-CPU x86_64 Linux machine with Python 3.12.3 and DuckDB 2.0.0. Nothing here is estimated.

## Agency and sources

| | |
|---|---|
| Agency | **Big Blue Bus** (Santa Monica, CA; `agency_timezone` America/Los_Angeles) |
| How it was found | The Mobility Database catalog, <https://files.mobilitydatabase.org/feeds_v2.csv> (link from <https://github.com/MobilityData/mobility-database-catalogs>), filtered for US agencies with an active static feed and an active TripUpdates feed that both have `urls.authentication_type = 0` (no key) |
| Static GTFS | Mobility Database `mdb-37`: <http://gtfs.bigbluebus.com/current.zip>, downloaded 2026-10-27 14:39 UTC (1,043,108 bytes, sha256 `715a2c0b…5999adf`). `feed_info.txt`: version `2026-08(AUG)_20260728`, valid 2026-08-09 to 2026-12-12 |
| GTFS-Realtime TripUpdates | `mdb-1438`: <http://gtfs.bigbluebus.com/tripupdates.bin>, fetched 2026-10-27 14:39:54 UTC (28,485 bytes, sha256 `576907e9…6093b0b`) |
| GTFS-Realtime VehiclePositions | `mdb-1436`: <http://gtfs.bigbluebus.com/vehiclepositions.bin>, fetched 2026-10-27 14:40:37 UTC (15 bytes) |
| Service date analysed | 2026-10-27 (a Tuesday, weekday service) |

Downloads were kept outside the repository and are not committed. The two example
outputs in [`big-blue-bus/`](big-blue-bus/) total about 54 KB, so no route sampling was
needed. All 17 routes are included.

## (a) Static load: timing and counts

`load_gtfs` + `build_scheduled_events` for 2026-10-27, run 4 times:

| run | load_gtfs | build_scheduled_events | total |
|---|---|---|---|
| 1 | 0.29 s | 0.22 s | 0.51 s |
| 2 | 0.26 s | 0.19 s | 0.45 s |
| 3 | 0.27 s | 0.23 s | 0.50 s |
| 4 | 0.28 s | 0.19 s | 0.47 s |

| Count | Value |
|---|---|
| routes in feed / with service on the date | 17 / 17 |
| trips in feed | 3,277 |
| stop_times rows in feed | 123,699 |
| stops | 901 |
| scheduled trip instances on 2026-10-27 | 1,577 |
| scheduled stop events on 2026-10-27 | 58,287 |
| interpolated events, frequency events, dropped untimed stop times, trips without timed stops | 0, 0, 0, 0 |

The feed has no `frequencies.txt`, and every stop time is timed, so the 0.2.0
interpolation and frequency code paths ran but found nothing to do on this feed.

## (b) Derived observation set run through the real CLI

[`big-blue-bus/derive_observations.py`](big-blue-bus/derive_observations.py) builds a
**synthetic but schedule-faithful** observation set from this feed's own 2026-10-27
schedule. The model is in the script's docstring: it samples 30% of trip instances,
draws a starting delay from N(45 s, 90 s) with 8% "early runners" at N(−150 s, 45 s), adds
drift of N(+6 s, 20 s) per stop plus N(0, 15 s) noise, cancels 2% of sampled trips, skips
1% of stops, and leaves 5% of stops unreported. It also adds 3 REPLACEMENT trips,
2 DUPLICATED trips (copies running 20 min later, new id in `trip_properties`) and 1 NEW
trip, which exercise the 0.3.0 fix on real ids. The output is deterministic with
`--seed 42`; two runs produced the same sha256 (`cce43313…aa87`).

```sh
python examples/big-blue-bus/derive_observations.py current.zip 2026-10-27 bbb_2026-10-27
# scheduled trip instances 1577; records 16530; by relationship {'CANCELED': 13,
#  'DUPLICATED': 67, 'NEW': 1, 'REPLACEMENT': 84, 'SCHEDULED': 16189, 'SKIPPED': 176}
transit-pulse analyze --static current.zip --realtime bbb_2026-10-27.json \
    --out-dir out --date 2026-10-27
# service date 2026-10-27: OTP 73.2% over 16273 observed of 58287 scheduled stop events;
#  13 cancelled trips; 3 replaced trips; 3 added trips; 87 bunching events
```

The script writes the same records as JSON (2,903,760 bytes) and as a GTFS-RT protobuf
`FeedMessage` (303,531 bytes). Running the CLI on each produced **byte-identical**
`metrics.json`, so the protobuf and JSON input paths agree on a real feed.

Results ([`big-blue-bus/metrics.json`](big-blue-bus/metrics.json),
[`big-blue-bus/report.html`](big-blue-bus/report.html)):

| Metric (overall) | Value |
|---|---|
| scheduled events / trips | 58,287 / 1,577 |
| observed events | 16,273 (= 16,189 SCHEDULED + 84 REPLACEMENT records, all matched) |
| early / on time / late | 1,406 / 11,915 / 2,952 |
| OTP | 73.22% |
| coverage | 28.21% |
| cancelled trips / events | 13 / 424 |
| skipped events | 176 |
| replaced trips | 3 |
| added trips (duplicated / new) | 3 (2 / 1), 68 observations |
| unobserved events | 41,414 |
| mean signed / mean abs / median abs / p90 abs delay | 151.62 / 177.43 / 150.0 / 364.0 s |
| headway pairs / headway deviation CV | 9,289 / 0.09 |
| bunching events | 87 (all on route 7, the only route scheduled 6–8 min apart) |
| `inputs.observations_unmatched` | 0 |

These metrics describe the **synthetic delay model**, not Big Blue Bus's real
performance. Coverage of about 28% comes from sampling 30% of trips. The bunching count
comes from independent random per-trip delays on route 7's 6–8 minute headways. The
point of this run is that the pipeline handles a real schedule end to end with
internally consistent counts.

### Timing, and a performance bug this run found

The first full CLI run took **18.89–19.61 s** (3 runs on JSON input; 19.41–20.14 s on
protobuf). Profiling showed `load_observations` used **17.69 s** of that, because it
inserted rows one at a time with `executemany` (about 1 ms per row). Static load took
0.34 s, analysis 0.27 s, and `events.csv` 0.06 s. 0.3.0 replaces that insert with a bulk
load through a temporary CSV. After the change:

| step | before | after |
|---|---|---|
| `load_observations` (16,530 rows) | 17.69 s | 0.08 s |
| full CLI, JSON input (3 runs) | 18.89–19.61 s | 1.17–1.48 s |
| full CLI, protobuf input (3 runs) | 19.41–20.14 s | 1.18–1.21 s |

`metrics.json` from before and after the change is byte-identical.

## (c) Live GTFS-Realtime snapshot through the real parser

A no-key realtime feed for this agency **is** listed, and it was fetched and parsed. It
is **not usable for real measurement**, however:

| TripUpdates snapshot | Value |
|---|---|
| header `gtfs_realtime_version` | 2.0 |
| header timestamp | 1791436022 = **2026-10-08 05:07:02 UTC**, about 19 days before the fetch, although the HTTP `Last-Modified` header showed the fetch time |
| entities | 50, all `trip_update`, all `SCHEDULED`, all `start_date` 20261007 |
| StopTimeUpdates | 1,291: 35 timed at or before the header timestamp, 1,256 after it (predictions), 0 without a time |
| parser rows (`read_observation_file`) | 35, all `SCHEDULED` from `trip_update`; 1,256 predictions dropped as designed; 0 warnings; no crash |
| trip_ids / route_ids found in the current static feed | **0 of 50 / 0 of 50**. The realtime feed uses another id scheme (for example route_id `7` where the static feed has `4104`) |

The real CLI on this snapshot (date inferred as 2026-10-07 from `start_date`) exited
with code 0: 0 observed of 58,287 scheduled events, and `inputs.observations_unmatched` =
35 of 35. That is the right result for observations whose ids do not exist in the
schedule. The VehiclePositions endpoint returned a 15-byte feed with header timestamp
2025-04-09 15:15:21 UTC and 0 entities (parser: 0 rows, no crash).

**Conclusion for (c):** parser robustness on a real, keyless agency feed is verified. A
real-performance measurement is not possible with this agency's current realtime
feeds: they are stale and their ids do not match the published static feed. Steps (a)
and (b) are therefore the substantive validation.
