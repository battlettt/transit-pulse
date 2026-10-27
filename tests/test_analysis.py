import datetime as dt
import json

from conftest import SERVICE_DATE, obs, route, sched_epoch
from transit_pulse.analysis import Thresholds, infer_service_date


def _status(con, trip_id, seq):
    return con.execute(
        "SELECT status, delay_secs FROM events WHERE trip_id = ? AND stop_sequence = ?",
        [trip_id, seq],
    ).fetchone()


def test_otp_threshold_boundaries_are_inclusive(analyze):
    records = [
        obs("T1a", 1, "08:00:00", delay=-60),   # exactly 60 s early -> on time
        obs("T1a", 2, "08:05:00", delay=-61),   # 61 s early -> early
        obs("T1a", 3, "08:10:00", delay=300),   # exactly 300 s late -> on time
        obs("T1b", 1, "08:10:00", delay=301),   # 301 s late -> late
    ]
    metrics, con = analyze(records)
    o = metrics["overall"]
    assert (o["early_events"], o["on_time_events"], o["late_events"]) == (1, 2, 1)
    assert o["observed_events"] == 4
    assert o["otp_pct"] == 50.0
    assert _status(con, "T1a", 1) == ("on_time", -60)
    assert _status(con, "T1a", 2) == ("early", -61)
    assert _status(con, "T1a", 3) == ("on_time", 300)
    assert _status(con, "T1b", 1) == ("late", 301)
    assert o["mean_abs_delay_secs"] == (60 + 61 + 300 + 301) / 4
    assert o["median_abs_delay_secs"] == 180.5
    assert metrics["thresholds"] == {"early_secs": 60, "late_secs": 300}


def test_custom_thresholds(analyze):
    records = [
        obs("T1a", 2, "08:05:00", delay=-61),
        obs("T1b", 1, "08:10:00", delay=301),
    ]
    metrics, _ = analyze(records, thresholds=Thresholds(early_secs=61, late_secs=301))
    assert metrics["overall"]["otp_pct"] == 100.0
    metrics, _ = analyze(records, thresholds=Thresholds(early_secs=0, late_secs=0))
    assert metrics["overall"]["otp_pct"] == 0.0


def test_missing_observation_is_unmatched_not_zero_delay(analyze):
    metrics, con = analyze([obs("T1a", 1, "08:00:00", delay=120)])
    o = metrics["overall"]
    assert o["scheduled_events"] == 15
    assert o["observed_events"] == 1
    assert o["unobserved_events"] == 14
    assert o["mean_abs_delay_secs"] == 120.0   # not diluted by fake zeros
    assert o["otp_pct"] == 100.0
    assert _status(con, "T1a", 2) == ("unobserved", None)
    assert round(o["coverage_pct"], 2) == round(100 / 15, 2)


def test_midnight_crossing_trip_matches_after_midnight_observation(analyze):
    # 24:10 on 2024-03-06 is 00:10 on 2024-03-07; observed at 00:12.
    after_midnight = int(dt.datetime(2024, 3, 7, 5, 12, tzinfo=dt.timezone.utc).timestamp())
    record = {"trip_id": "T2a", "stop_id": "S2", "stop_sequence": 2,
              "observed_epoch": after_midnight, "schedule_relationship": "SCHEDULED"}
    metrics, con = analyze([record])
    assert _status(con, "T2a", 2) == ("on_time", 120)
    assert route(metrics, "R2")["observed_events"] == 1


def test_cancelled_trip_counted_not_dropped(analyze):
    records = [
        {"trip_id": "T2b", "route_id": "R2", "schedule_relationship": "CANCELED"},
        obs("T2a", 1, "23:50:00"),
    ]
    metrics, con = analyze(records)
    o, r2 = metrics["overall"], route(metrics, "R2")
    assert o["cancelled_trips"] == 1 and r2["cancelled_trips"] == 1
    assert o["cancelled_events"] == 3 and r2["cancelled_events"] == 3
    assert o["scheduled_trips"] == 5 and o["scheduled_events"] == 15  # still in the schedule
    assert _status(con, "T2b", 1) == ("cancelled", None)
    assert route(metrics, "R1")["cancelled_trips"] == 0
    # Coverage excludes cancelled events from its denominator: 1 observed of 12.
    assert round(o["coverage_pct"], 2) == round(100 / 12, 2)


def test_cancellation_for_unknown_trip_is_reported(analyze):
    metrics, _ = analyze([{"trip_id": "NOPE", "schedule_relationship": "CANCELED"}])
    assert metrics["overall"]["cancelled_trips"] == 0
    assert metrics["inputs"]["cancellations_for_unscheduled_trips"] == 1


def test_skipped_stop(analyze):
    record = {"trip_id": "T1a", "stop_sequence": 2, "stop_id": "S2",
              "schedule_relationship": "SKIPPED"}
    metrics, con = analyze([record])
    assert metrics["overall"]["skipped_events"] == 1
    assert _status(con, "T1a", 2) == ("skipped", None)


def test_bunching_detected(analyze):
    # T1a and T1b are scheduled 10 min apart at S2 (08:05 / 08:15) but arrive 60 s apart.
    records = [
        obs("T1a", 2, "08:05:00", delay=540),  # 08:14
        obs("T1b", 2, "08:15:00", delay=0),    # 08:15
        obs("T1c", 2, "08:25:00", delay=0),    # 08:25, normal 10-min gap
    ]
    metrics, _ = analyze(records)
    assert metrics["overall"]["bunching_events"] == 1
    assert route(metrics, "R1")["bunching_events"] == 1
    assert route(metrics, "R2")["bunching_events"] == 0
    event = metrics["bunching"][0]
    assert event["leading_trip_id"] == "T1a" and event["following_trip_id"] == "T1b"
    assert event["observed_gap_secs"] == 60 and event["scheduled_gap_secs"] == 600
    assert event["stop_id"] == "S2"


def test_gap_above_bunching_threshold_is_not_bunching(analyze):
    records = [obs("T1a", 2, "08:05:00", delay=479), obs("T1b", 2, "08:15:00")]  # 121 s apart
    metrics, _ = analyze(records)
    assert metrics["overall"]["bunching_events"] == 0


def test_headway_adherence(analyze):
    on_schedule = [obs(t, 1, hhmm) for t, hhmm in
                   [("T1a", "08:00:00"), ("T1b", "08:10:00"), ("T1c", "08:20:00")]]
    metrics, _ = analyze(on_schedule)
    r1 = route(metrics, "R1")
    assert r1["headway_pairs"] == 2
    assert r1["mean_scheduled_headway_secs"] == 600
    assert r1["headway_deviation_cv"] == 0
    assert r1["mean_abs_headway_deviation_secs"] == 0

    # T1b 5 min late: observed headways 900 and 300 against 600 scheduled.
    late_middle = [obs("T1a", 1, "08:00:00"), obs("T1b", 1, "08:10:00", delay=300),
                   obs("T1c", 1, "08:20:00")]
    metrics, _ = analyze(late_middle)
    r1 = route(metrics, "R1")
    assert r1["mean_abs_headway_deviation_secs"] == 300
    assert r1["headway_deviation_cv"] == 0.5   # stddev(+300, -300) / 600
    # R2 has no high-frequency stops, so no headway figures.
    assert route(metrics, "R2")["headway_pairs"] == 0


def test_fallback_match_on_stop_id_when_sequence_missing(analyze):
    record = obs("T1b", 3, "08:20:00", delay=90)
    record["stop_sequence"] = None
    metrics, con = analyze([record])
    assert _status(con, "T1b", 3) == ("on_time", 90)
    assert metrics["inputs"]["observations_unmatched"] == 0


def test_observation_for_other_service_date_is_ignored(analyze):
    records = [obs("T1a", 1, "08:00:00", start_date="20240305"),
               obs("T1b", 1, "08:10:00", delay=30, start_date="20240306")]
    metrics, _ = analyze(records)
    assert metrics["overall"]["observed_events"] == 1
    assert metrics["inputs"]["observations_for_service_date"] == 1


def test_observation_far_from_schedule_is_unmatched(analyze):
    # Same trip_id one day later (no start_date) must not become a 24 h delay.
    metrics, _ = analyze([obs("T1a", 1, "08:00:00", delay=24 * 3600)])
    assert metrics["overall"]["observed_events"] == 0
    assert metrics["overall"]["otp_pct"] is None


def test_duplicate_reports_use_newest_feed(analyze):
    records = [obs("T1a", 1, "08:00:00", delay=400, feed_timestamp=100),
               obs("T1a", 1, "08:00:00", delay=30, feed_timestamp=200)]
    _, con = analyze(records)
    assert _status(con, "T1a", 1) == ("on_time", 30)


def test_delay_histogram_and_json_serializable(analyze):
    records = [obs("T1a", 1, "08:00:00", delay=-1000), obs("T1a", 2, "08:05:00", delay=30),
               obs("T1a", 3, "08:10:00", delay=5000)]
    metrics, _ = analyze(records)
    bins = metrics["delay_histogram"]
    assert bins[0] == {"bin_start_secs": None, "bin_end_secs": -300, "count": 1}
    assert bins[-1] == {"bin_start_secs": 900, "bin_end_secs": None, "count": 1}
    assert next(b for b in bins if b["bin_start_secs"] == 0)["count"] == 1
    assert sum(b["count"] for b in bins) == metrics["overall"]["observed_events"]
    json.dumps(metrics)


def test_route_names_attached(analyze):
    metrics, _ = analyze([])
    assert [r["route_name"] for r in metrics["routes"]] == ["1", "2"]
    assert metrics["overall"]["otp_pct"] is None


def test_infer_service_date(analyze):
    _, con = analyze([obs("T1a", 1, "08:00:00")])
    assert infer_service_date(con, "America/New_York") == SERVICE_DATE
    _, con = analyze([obs("T1a", 1, "08:00:00", start_date="20240307")])
    assert infer_service_date(con, "America/New_York") == dt.date(2024, 3, 7)
