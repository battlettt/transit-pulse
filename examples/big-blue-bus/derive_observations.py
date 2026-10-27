"""Derive a realistic, synthetic realtime observation set from a real GTFS schedule.

Usage:
    python derive_observations.py GTFS.zip YYYY-MM-DD OUT_PREFIX [--seed 42]

Writes OUT_PREFIX.json (normalized observations) and OUT_PREFIX.pb (the same data as a
GTFS-Realtime FeedMessage whose header timestamp is after every event, so nothing is
dropped as a prediction). Deterministic for a given seed.

Model (documented in examples/REAL_FEED_VALIDATION.md):
- 30% of the day's scheduled trip instances are sampled; the rest stay unobserved,
  imitating realtime coverage gaps.
- Each sampled trip gets a starting delay ~ N(45 s, 90 s); 8% of trips are "early
  runners" starting ~ N(-150 s, 45 s). Delay then drifts along the trip by
  N(+6 s, 20 s) per stop (buses tend to lose time), plus N(0, 15 s) noise per stop.
- 2% of sampled trips are CANCELED (whole trip). In the remaining trips, 1% of stops are
  SKIPPED and 5% of stops have no report (AVL gaps).
- A handful of GTFS-RT 2.x trip types exercise the v0.3.0 fix on real ids: 3 trips are
  REPLACEMENT (observed like scheduled trips), 2 are DUPLICATED (copies running 20 min
  later, new id in trip_properties) and 1 is NEW (an unscheduled trip).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random

import duckdb
from google.transit import gtfs_realtime_pb2 as rt

from transit_pulse.gtfs_static import build_scheduled_events, load_gtfs

SAMPLE_TRIPS = 0.30
EARLY_RUNNER = 0.08
CANCEL = 0.02
SKIP_STOP = 0.01
GAP_STOP = 0.05
N_REPLACEMENT, N_DUPLICATED = 3, 2


def scheduled_trips(zip_path: str, service_date: dt.date) -> dict[str, list[tuple]]:
    con = duckdb.connect()
    load_gtfs(con, zip_path)
    build_scheduled_events(con, service_date)
    rows = con.execute(
        "SELECT trip_instance_id, trip_id, route_id, stop_id, stop_sequence, sched_epoch "
        "FROM scheduled_events ORDER BY trip_instance_id, stop_sequence"
    ).fetchall()
    trips: dict[str, list[tuple]] = {}
    for inst, *rest in rows:
        trips.setdefault(inst, []).append(tuple(rest))
    return trips


def derive(trips: dict[str, list[tuple]], start_date: str, seed: int) -> list[dict]:
    rng = random.Random(seed)
    sampled = sorted(rng.sample(sorted(trips), round(len(trips) * SAMPLE_TRIPS)))
    special = rng.sample(sampled, N_REPLACEMENT + N_DUPLICATED)
    replacement, duplicated = set(special[:N_REPLACEMENT]), special[N_REPLACEMENT:]
    records = []
    for inst in sampled:
        stops = trips[inst]
        trip_id, route_id = stops[0][0], stops[0][1]
        base = {"trip_id": trip_id, "route_id": route_id, "start_date": start_date}
        if inst not in replacement and rng.random() < CANCEL:
            records.append(dict(base, schedule_relationship="CANCELED"))
            continue
        relationship = "REPLACEMENT" if inst in replacement else "SCHEDULED"
        delay = (rng.gauss(-150, 45) if rng.random() < EARLY_RUNNER
                 else rng.gauss(45, 90))
        for _, _, stop_id, seq, sched in stops:
            delay += rng.gauss(6, 20)
            roll = rng.random()
            if roll < SKIP_STOP:
                records.append(dict(base, stop_id=stop_id, stop_sequence=seq,
                                    schedule_relationship="SKIPPED"))
            elif roll < SKIP_STOP + GAP_STOP:
                continue
            else:
                records.append(dict(base, stop_id=stop_id, stop_sequence=seq,
                                    observed_epoch=int(sched + delay + rng.gauss(0, 15)),
                                    schedule_relationship=relationship))
    for n, inst in enumerate(duplicated):
        for _, route_id, stop_id, seq, sched in trips[inst]:
            records.append({"trip_id": f"{trips[inst][0][0]}-dup{n}", "route_id": route_id,
                            "start_date": start_date, "stop_id": stop_id,
                            "stop_sequence": seq, "observed_epoch": int(sched + 1200),
                            "schedule_relationship": "DUPLICATED",
                            "duplicated_from": trips[inst][0][0]})
    _, route_id, stop_id, _, sched = trips[sampled[0]][0]
    records.append({"trip_id": "NEW-1", "route_id": route_id, "start_date": start_date,
                    "stop_id": stop_id, "stop_sequence": 1, "observed_epoch": int(sched + 600),
                    "schedule_relationship": "NEW"})
    return records


def to_feed_message(records: list[dict]) -> rt.FeedMessage:
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = max(r.get("observed_epoch") or 0 for r in records) + 60
    by_trip: dict[tuple, list[dict]] = {}
    for r in records:
        by_trip.setdefault((r["trip_id"], r["schedule_relationship"] == "CANCELED"), []).append(r)
    rel_enum = rt.TripDescriptor.ScheduleRelationship
    for i, ((trip_id, cancelled), rows) in enumerate(by_trip.items()):
        tu = feed.entity.add(id=f"tu{i}").trip_update
        first = rows[0]
        trip_rel = "CANCELED" if cancelled else next(
            (r["schedule_relationship"] for r in rows if r["schedule_relationship"] != "SKIPPED"),
            "SCHEDULED")
        tu.trip.trip_id = first.get("duplicated_from", trip_id)
        tu.trip.route_id = first["route_id"]
        tu.trip.start_date = first["start_date"]
        tu.trip.schedule_relationship = rel_enum.Value(trip_rel)
        if trip_rel == "DUPLICATED":
            tu.trip_properties.trip_id = trip_id
            tu.trip_properties.start_date = first["start_date"]
        if cancelled:
            continue
        for r in rows:
            stu = tu.stop_time_update.add(stop_sequence=r["stop_sequence"], stop_id=r["stop_id"])
            if r["schedule_relationship"] == "SKIPPED":
                stu.schedule_relationship = rt.TripUpdate.StopTimeUpdate.SKIPPED
            else:
                stu.arrival.time = r["observed_epoch"]
    return feed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gtfs_zip")
    ap.add_argument("date", type=dt.date.fromisoformat)
    ap.add_argument("out_prefix")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    trips = scheduled_trips(args.gtfs_zip, args.date)
    records = derive(trips, args.date.strftime("%Y%m%d"), args.seed)
    with open(f"{args.out_prefix}.json", "w") as fh:
        json.dump([{k: v for k, v in r.items() if k != "duplicated_from"} for r in records], fh)
    with open(f"{args.out_prefix}.pb", "wb") as fh:
        fh.write(to_feed_message(records).SerializeToString())
    counts: dict[str, int] = {}
    for r in records:
        counts[r["schedule_relationship"]] = counts.get(r["schedule_relationship"], 0) + 1
    print(f"scheduled trip instances {len(trips)}; records {len(records)}; by relationship "
          f"{dict(sorted(counts.items()))}")


if __name__ == "__main__":
    main()
