import json
import re

from google.transit import gtfs_realtime_pb2 as rt

from conftest import obs, sched_epoch
from transit_pulse.cli import main


def _write_pb(path):
    feed = rt.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = sched_epoch("10:30:00")
    for i, (trip, seq, t, delay) in enumerate([
        ("T1a", 1, "08:00:00", 30), ("T1a", 2, "08:05:00", 400), ("T1b", 1, "08:10:00", -90),
    ]):
        e = feed.entity.add(id=f"tu{i}")
        e.trip_update.trip.trip_id = trip
        e.trip_update.trip.start_date = "20240306"
        stu = e.trip_update.stop_time_update.add(stop_sequence=seq)
        stu.arrival.time = sched_epoch(t) + delay
    e = feed.entity.add(id="cx")
    e.trip_update.trip.trip_id = "T2b"
    e.trip_update.trip.schedule_relationship = rt.TripDescriptor.CANCELED
    path.write_bytes(feed.SerializeToString())
    return str(path)


def test_cli_end_to_end_protobuf(tmp_path, gtfs_zip, capsys):
    pb = _write_pb(tmp_path / "feed.pb")
    out = tmp_path / "out"
    code = main(["analyze", "--static", gtfs_zip, "--realtime", pb, "--out-dir", str(out),
                 "--date", "2024-03-06", "--early-secs", "60", "--late-secs", "300"])
    assert code == 0
    metrics = json.loads((out / "metrics.json").read_text())
    o = metrics["overall"]
    assert metrics["service_date"] == "2024-03-06"
    assert (o["observed_events"], o["on_time_events"], o["late_events"], o["early_events"]) \
        == (3, 1, 1, 1)
    assert o["cancelled_trips"] == 1

    html = (out / "report.html").read_text()
    assert html.startswith("<!DOCTYPE html>")
    assert html.count("<svg") == 2
    assert "<td>1</td>" in html and "<td>2</td>" in html  # per-route table rows
    # Self-contained: no external scripts, stylesheets, images or fonts.
    assert not re.search(r"<(script|link|img)\b", html)
    assert "url(" not in html and "@import" not in html
    assert "OTP 33.3%" in capsys.readouterr().out


def test_cli_json_input_and_inferred_date(tmp_path, gtfs_zip):
    js = tmp_path / "obs.json"
    js.write_text(json.dumps([obs("T1a", 1, "08:00:00", delay=20)]))
    out = tmp_path / "out"
    assert main(["analyze", "--static", gtfs_zip, "--realtime", str(js),
                 "--out-dir", str(out)]) == 0
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["service_date"] == "2024-03-06"
    assert metrics["overall"]["otp_pct"] == 100.0


def test_cli_reports_missing_gtfs_file(tmp_path, capsys):
    from conftest import write_gtfs_zip

    bad = write_gtfs_zip(tmp_path / "bad.zip", omit=["stops.txt"])
    js = tmp_path / "obs.json"
    js.write_text("[]")
    code = main(["analyze", "--static", bad, "--realtime", str(js),
                 "--out-dir", str(tmp_path / "out"), "--date", "2024-03-06"])
    assert code == 2
    assert "missing required file: stops.txt" in capsys.readouterr().err


def test_console_script_installed():
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    exe = Path(sys.executable).parent / "transit-pulse"
    assert exe.exists() or shutil.which("transit-pulse")
    result = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
    assert result.returncode == 0 and "transit-pulse" in result.stdout


def test_cli_v2_outputs_added_trips_events_csv_and_untimed_warning(tmp_path, capsys):
    import csv

    from conftest import feed_with, stop_time, write_gtfs_zip

    feed = feed_with(
        [("R1", "I1"), ("R2", "U1")],
        [stop_time("I1", 1, "10:00:00"), stop_time("I1", 2, ""), stop_time("I1", 3, "10:10:00"),
         stop_time("U1", 1, ""), stop_time("U1", 2, "")],
    )
    gtfs = write_gtfs_zip(tmp_path / "v2.zip", feed)
    js = tmp_path / "obs.json"
    js.write_text(json.dumps([
        obs("I1", 2, "10:05:00", delay=20),
        {"trip_id": "X1", "route_id": "R2", "stop_id": "S1", "stop_sequence": 1,
         "observed_epoch": sched_epoch("09:00:00"), "schedule_relationship": "ADDED"},
    ]))
    out = tmp_path / "out"
    assert main(["analyze", "--static", gtfs, "--realtime", str(js), "--out-dir", str(out),
                 "--date", "2024-03-06"]) == 0
    captured = capsys.readouterr()
    assert "1 added trips" in captured.out
    assert "warning: 1 trips have no timed stops" in captured.err and "U1" in captured.err

    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["added_trips"]["count"] == 1
    assert metrics["added_trips"]["routes"][0]["route_id"] == "R2"
    assert metrics["schedule"]["interpolated_events"] == 1

    with open(out / "events.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == metrics["overall"]["scheduled_events"] == 18
    interp = [r for r in rows if r["interpolated"] == "true"]
    assert [(r["trip_id"], r["stop_sequence"], r["delay_secs"], r["status"]) for r in interp] \
        == [("I1", "2", "20", "on_time")]
    html = (out / "report.html").read_text()
    assert "estimated by linear interpolation" in html and "events.csv" in html
