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
