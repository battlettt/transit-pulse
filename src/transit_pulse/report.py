"""Write metrics.json, events.csv and a self-contained HTML dashboard (inline CSS + SVG,
no network)."""

from __future__ import annotations

import json
import os
from html import escape

CSS = """
:root { color-scheme: light; --surface: #fcfcfb; --panel: #ffffff; --text: #0b0b0b;
  --text-2: #52514e; --muted: #8a8984; --grid: #e4e3df; --series: #2a78d6; }
@media (prefers-color-scheme: dark) {
  :root { color-scheme: dark; --surface: #1a1a19; --panel: #232322; --text: #f0efec;
    --text-2: #c3c2b7; --muted: #8f8e86; --grid: #3a3a37; --series: #3987e5; }
}
body { margin: 0; padding: 24px; background: var(--surface); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1040px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 0 0 12px; }
.sub { color: var(--text-2); margin: 0 0 20px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px; margin-bottom: 20px; }
.tile, .panel { background: var(--panel); border: 1px solid var(--grid); border-radius: 8px;
  padding: 14px 16px; }
.tile .label { color: var(--text-2); font-size: 12px; }
.tile .value { font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 12px; margin-bottom: 20px; }
svg text { fill: var(--text-2); font-size: 11px; }
svg .axis { stroke: var(--grid); }
svg .bar { fill: var(--series); }
svg .bar:hover { opacity: 0.8; }
svg .threshold { stroke: var(--muted); stroke-dasharray: 4 3; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 6px 8px; border-bottom: 1px solid var(--grid); text-align: right; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--text-2); font-weight: 600; font-size: 12px; }
.note { color: var(--text-2); font-size: 12px; margin-top: 16px; }
"""


def _fmt(value, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _bar_path(x: float, y: float, w: float, h: float, r: float, horizontal: bool) -> str:
    """Bar with rounded corners on the data end only, square at the baseline."""
    r = max(0.0, min(r, w / 2 if not horizontal else h / 2, h if not horizontal else w))
    if horizontal:  # baseline on the left
        return (f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
                f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} "
                f"H{x:.1f} Z")
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def delay_histogram_svg(bins: list[dict], early_secs: int, late_secs: int) -> str:
    width, height = 480, 240
    left, right, top, bottom = 40, 10, 24, 42
    plot_w, plot_h = width - left - right, height - top - bottom
    max_count = max((b["count"] for b in bins), default=0) or 1
    slot = plot_w / max(len(bins), 1)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Histogram of arrival delay in one-minute bins">']
    for frac in (0, 0.5, 1):
        y = top + plot_h * (1 - frac)
        parts.append(f'<line class="axis" x1="{left}" x2="{width - right}" y1="{y:.1f}" '
                     f'y2="{y:.1f}"/>')
        if (max_count * frac).is_integer():  # label only whole-number gridlines
            parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">'
                         f'{int(max_count * frac)}</text>')
    for i, b in enumerate(bins):
        h = plot_h * b["count"] / max_count
        x = left + i * slot + 1  # 2px gap between adjacent bars
        label = _bin_label(b)
        if h > 0:
            parts.append(f'<path class="bar" d="{_bar_path(x, top + plot_h - h, slot - 2, h, 4, False)}">'
                         f'<title>{escape(label)}: {b["count"]} events</title></path>')
        if b["bin_start_secs"] is not None and b["bin_start_secs"] % 300 == 0:
            parts.append(f'<text x="{left + i * slot:.1f}" y="{top + plot_h + 14}" '
                         f'text-anchor="middle">{_minutes(b["bin_start_secs"])}</text>')
    for secs, name in ((-early_secs, "early"), (late_secs, "late")):
        x = _secs_to_x(secs, bins, left, slot)
        if x is not None:
            parts.append(f'<line class="threshold" x1="{x:.1f}" x2="{x:.1f}" y1="{top}" '
                         f'y2="{top + plot_h}"/>')
            parts.append(f'<text x="{x:.1f}" y="{top - 8}" text-anchor="middle">{name}</text>')
    parts.append(f'<text x="{left + plot_w / 2:.1f}" y="{height - 6}" text-anchor="middle">'
                 f'delay (minutes; outer bars collect everything beyond)</text>')
    parts.append("</svg>")
    return "".join(parts)


def _minutes(secs: int) -> str:
    return "0" if secs == 0 else f"{secs // 60:+d}"


def _bin_label(b: dict) -> str:
    if b["bin_start_secs"] is None:
        return f"earlier than {b['bin_end_secs'] // 60:+d} min"
    if b["bin_end_secs"] is None:
        return f"{b['bin_start_secs'] // 60:+d} min or later"
    return f"{b['bin_start_secs'] // 60:+d} to {b['bin_end_secs'] // 60:+d} min"


def _secs_to_x(secs: int, bins: list[dict], left: float, slot: float):
    for i, b in enumerate(bins):
        start, end = b["bin_start_secs"], b["bin_end_secs"]
        if start is not None and end is not None and start <= secs <= end:
            return left + (i + (secs - start) / (end - start)) * slot
    return None


def otp_by_route_svg(routes: list[dict]) -> str:
    rows = [r for r in routes if r.get("otp_pct") is not None]
    if not rows:
        return '<p class="note">No observed events, so no on-time performance to chart.</p>'
    row_h, left, right, top = 26, 110, 64, 8
    width = 480
    height = top + row_h * len(rows) + 24
    plot_w = width - left - right
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="On-time performance by route, percent">']
    for pct in (0, 50, 100):
        x = left + plot_w * pct / 100
        parts.append(f'<line class="axis" x1="{x:.1f}" x2="{x:.1f}" y1="{top}" '
                     f'y2="{height - 20}"/>')
        parts.append(f'<text x="{x:.1f}" y="{height - 6}" text-anchor="middle">{pct}%</text>')
    for i, r in enumerate(rows):
        y = top + i * row_h + 4
        w = plot_w * r["otp_pct"] / 100
        name = escape(str(r.get("route_name") or r["route_id"]))
        parts.append(f'<text x="{left - 8}" y="{y + 13}" text-anchor="end">{name}</text>')
        if w > 0:
            parts.append(f'<path class="bar" d="{_bar_path(left, y, w, row_h - 10, 4, True)}">'
                         f'<title>{name}: {r["otp_pct"]:.1f}% on time '
                         f'({r["on_time_events"]} of {r["observed_events"]} observed)</title></path>')
        parts.append(f'<text x="{left + w + 6:.1f}" y="{y + 13}">{r["otp_pct"]:.1f}%</text>')
    parts.append("</svg>")
    return "".join(parts)


ROUTE_COLUMNS = [
    ("Route", "route_name", ""),
    ("OTP", "otp_pct", "%"),
    ("Observed", "observed_events", ""),
    ("Early", "early_events", ""),
    ("Late", "late_events", ""),
    ("Unobserved", "unobserved_events", ""),
    ("Cancelled trips", "cancelled_trips", ""),
    ("Coverage", "coverage_pct", "%"),
    ("Median |delay| s", "median_abs_delay_secs", ""),
    ("p90 |delay| s", "p90_abs_delay_secs", ""),
    ("Headway CV", "headway_deviation_cv", ""),
    ("Bunching", "bunching_events", ""),
]


def route_table_html(routes: list[dict]) -> str:
    head = "".join(f"<th>{escape(title)}</th>" for title, _, _ in ROUTE_COLUMNS)
    body = []
    for r in routes:
        cells = "".join(
            f"<td>{escape(_fmt(r.get(key), suffix, 2 if key.endswith('cv') else 1))}</td>"
            for _, key, suffix in ROUTE_COLUMNS
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_html(metrics: dict) -> str:
    o, t = metrics["overall"], metrics["thresholds"]
    tiles = [
        ("On-time performance", _fmt(o.get("otp_pct"), "%")),
        ("Observed stop events", _fmt(o.get("observed_events"))),
        ("Realtime coverage", _fmt(o.get("coverage_pct"), "%")),
        ("Median |delay|", _fmt(o.get("median_abs_delay_secs"), " s")),
        ("Cancelled trips", _fmt(o.get("cancelled_trips"))),
        ("Bunching events", _fmt(o.get("bunching_events"))),
        ("Replaced trips", _fmt(o.get("replaced_trips"))),
        ("Added (unscheduled) trips", _fmt(o.get("added_trips"))),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value)}</div></div>'
        for label, value in tiles
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>transit-pulse report – {escape(metrics["service_date"])}</title>
<style>{CSS}</style>
</head>
<body>
<main>
<h1>Transit reliability – service date {escape(metrics["service_date"])}</h1>
<p class="sub">On time = between {t["early_secs"]} s early and {t["late_secs"]} s late.
OTP is measured over observed stop events only; see coverage.</p>
<section class="tiles">{tiles_html}</section>
<section class="charts">
<div class="panel"><h2>Delay distribution</h2>
{delay_histogram_svg(metrics["delay_histogram"], t["early_secs"], t["late_secs"])}</div>
<div class="panel"><h2>On-time performance by route</h2>{otp_by_route_svg(metrics["routes"])}</div>
</section>
<section class="panel"><h2>Routes</h2>{route_table_html(metrics["routes"])}</section>
<p class="note">Generated by transit-pulse. {o.get("unobserved_events", 0)} scheduled stop
events had no realtime observation and are excluded from OTP and delay statistics (not
counted as zero delay). {_schedule_note(metrics.get("schedule") or {})}Full numbers are in
metrics.json; per-event rows (with the interpolated flag) are in events.csv.</p>
</main>
</body>
</html>
"""


def _schedule_note(schedule: dict) -> str:
    parts = []
    if schedule.get("interpolated_events"):
        parts.append(f"{schedule['interpolated_events']} scheduled times were estimated by "
                     "linear interpolation between timed stops.")
    freq = schedule.get("frequency_exact_events", 0) + schedule.get("frequency_headway_events", 0)
    if freq:
        parts.append(f"{freq} stop events come from frequencies.txt "
                     f"({schedule.get('frequency_headway_events', 0)} headway-based, "
                     "whose delays are relative to a nominal start time).")
    untimed = (schedule.get("trips_without_timed_stops") or {}).get("count", 0)
    if untimed:
        parts.append(f"{untimed} trips had no timed stops and were left out of the schedule.")
    return escape(" ".join(parts) + " ") if parts else ""


EVENT_CSV_COLUMNS = [
    "event_id", "service_date", "route_id", "direction_id", "trip_id", "trip_instance_id",
    "frequency_start_time", "schedule_type", "stop_id", "stop_sequence", "sched_epoch",
    "interpolated", "observed_epoch", "delay_secs", "status",
]


def write_events_csv(con, out_dir: str) -> str:
    """Write the `events` table (one row per scheduled stop event) to out_dir/events.csv."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "events.csv")
    con.execute(
        f"COPY (SELECT {', '.join(EVENT_CSV_COLUMNS)} FROM events ORDER BY event_id) "
        f"TO '{path.replace(chr(39), chr(39) * 2)}' (HEADER, DELIMITER ',')"
    )
    return path


def write_report(metrics: dict, out_dir: str) -> tuple[str, str]:
    """Write metrics.json and report.html into out_dir; return both paths."""
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "metrics.json")
    html_path = os.path.join(out_dir, "report.html")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2)
        fh.write("\n")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(metrics))
    return json_path, html_path
