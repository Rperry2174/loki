#!/usr/bin/env python3
"""Turn `go test -bench` output into the before/after comparison used in the PR.

Reads two `go test -bench -benchmem -count=N` runs and emits a JSON summary plus
a self-contained HTML report (rendered to PNG with headless Chrome).

    python3 artifacts/bench_report.py before.txt after.txt artifacts/
"""

import json
import re
import statistics
import sys
from pathlib import Path

# e.g. "BenchmarkResponseMerge/foo-4   50   1291549 ns/op   2659448 B/op   10474 allocs/op"
LINE = re.compile(
    r"^(?P<name>Benchmark\S+?)(?:-\d+)?\s+\d+\s+"
    r"(?P<ns>[\d.]+) ns/op\s+"
    r"(?P<bytes>\d+) B/op\s+"
    r"(?P<allocs>\d+) allocs/op"
)

METRICS = [
    ("allocs", "Allocations", "allocs/op"),
    ("bytes", "Bytes allocated", "B/op"),
    ("ns", "Wall time", "ns/op"),
]


def parse(path):
    runs = {}
    for line in Path(path).read_text().splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        name = m.group("name").removeprefix("Benchmark")
        rec = runs.setdefault(name, {"ns": [], "bytes": [], "allocs": []})
        rec["ns"].append(float(m.group("ns")))
        rec["bytes"].append(float(m.group("bytes")))
        rec["allocs"].append(float(m.group("allocs")))
    return {n: {k: statistics.median(v) for k, v in r.items()} for n, r in runs.items()}


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def fmt(metric, value):
    if metric == "bytes":
        return human_bytes(value)
    if metric == "ns":
        return f"{value / 1e6:.2f} ms" if value >= 1e6 else f"{value / 1e3:.1f} \u00b5s"
    return f"{value:,.0f}"


def short_name(name):
    """Compact the Go benchmark path into something that fits a table cell."""
    name = name.replace("ResponseMergeSplitHeavy/", "splitHeavy ")
    name = name.replace("ResponseMerge/", "")
    for suffix in ("_limited", "_unlimited"):
        if name.endswith(suffix):
            return f"{name[: -len(suffix)]} ({suffix.strip('_')})"
    return name


# Benchmarks that never touch the priority queue; they should not move.
CONTROLS = {
    "ResponseMerge/mergeStreams_limited",
    "ResponseMerge/mergeStreams_unlimited",
    "ResponseMerge/mergeOrderedNonOverlappingStreams_unlimited",
}


def bar(pct, color):
    return (
        f'<div class="bar-track"><div class="bar" style="width:{max(pct, 0.6):.2f}%;'
        f'background:{color}"></div></div>'
    )


def build(before, after, out_dir):
    names = [n for n in before if n in after]
    # Affected benchmarks first, then the untouched controls.
    names.sort(key=lambda n: (n in CONTROLS, -before[n]["allocs"]))

    rows = []
    summary = {}
    for name in names:
        summary[name] = {}
        cells = []
        for metric, _, _ in METRICS:
            b, a = before[name][metric], after[name][metric]
            delta = (a - b) / b * 100 if b else 0.0
            summary[name][metric] = {"before": b, "after": a, "delta_pct": delta}
            cls = "win" if delta < -1 else ("loss" if delta > 1 else "flat")
            sign = "+" if delta > 0 else ""
            cells.append(
                f"<td>{fmt(metric, b)}</td>"
                f'<td class="after">{fmt(metric, a)}</td>'
                f'<td class="{cls}">{sign}{delta:.1f}%</td>'
            )
        tag = '<span class="tag">control</span>' if name in CONTROLS else ""
        rows.append(
            f'<tr><td class="name">{short_name(name)}{tag}</td>{"".join(cells)}</tr>'
        )

    # Allocation bar chart: after as a share of before.
    chart = []
    for name in names:
        b, a = before[name]["allocs"], after[name]["allocs"]
        pct = (a / b * 100) if b else 0
        tag = '<span class="tag">control</span>' if name in CONTROLS else ""
        chart.append(
            f'<div class="crow"><div class="clabel">{short_name(name)}{tag}</div>'
            f'<div class="cbars">{bar(100, "#9aa0a6")}{bar(pct, "#f54e00")}</div>'
            f'<div class="cval">{fmt("allocs", b)} &rarr; <b>{fmt("allocs", a)}</b>'
            f' <span class="pill">{pct:.1f}% of baseline</span></div></div>'
        )

    header = "".join(
        f'<th colspan="3">{label}<span class="unit">{unit}</span></th>'
        for _, label, unit in METRICS
    )
    sub = "<th>before</th><th>after</th><th>&Delta;</th>" * len(METRICS)

    html = TEMPLATE.format(
        rows="\n".join(rows),
        chart="\n".join(chart),
        header=header,
        sub=sub,
    )
    (out_dir / "benchmark-report.html").write_text(html)
    (out_dir / "benchmark-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Loki merge benchmark</title>
<style>
  :root {{ --bg:#f7f7f4; --fg:#26251e; --card:#f2f1ed; --line:#e1e0db; --accent:#f54e00; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:40px; background:var(--bg); color:var(--fg);
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:1180px; margin:0 auto; }}
  h1 {{ font-size:26px; margin:0 0 6px; letter-spacing:-0.02em; }}
  .sub {{ color:rgba(38,37,30,.6); margin:0 0 28px; font-size:14px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:22px 24px; margin-bottom:22px; }}
  h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.07em;
        color:rgba(38,37,30,.55); margin:0 0 18px; font-weight:600; }}
  table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
  th,td {{ text-align:right; padding:9px 10px; border-bottom:1px solid var(--line); }}
  th {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em;
        color:rgba(38,37,30,.55); font-weight:600; }}
  th .unit {{ display:block; font-size:10px; text-transform:none; letter-spacing:0; opacity:.7; }}
  thead tr:last-child th {{ font-size:10px; }}
  td.name, th:first-child {{ text-align:left; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
                             font-size:12px; width:330px; white-space:normal; word-break:break-word; }}
  .tag {{ display:inline-block; margin-left:7px; padding:0 6px; border-radius:20px;
          background:#e1e0db; color:rgba(38,37,30,.6); font-size:9.5px; font-weight:600;
          text-transform:uppercase; letter-spacing:.05em; vertical-align:1px;
          font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }}
  td.after {{ font-weight:600; }}
  .win {{ color:#1a7f37; font-weight:700; }}
  .loss {{ color:#b42318; font-weight:700; }}
  .flat {{ color:rgba(38,37,30,.5); }}
  tbody tr:last-child td {{ border-bottom:none; }}
  .crow {{ display:grid; grid-template-columns:330px 1fr 290px; gap:18px;
           align-items:center; margin-bottom:14px; }}
  .clabel {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
             word-break:break-word; }}
  .bar-track {{ background:#e6e5e0; border-radius:4px; height:13px; margin:3px 0; overflow:hidden; }}
  .bar {{ height:100%; border-radius:4px; }}
  .cval {{ font-size:12.5px; font-variant-numeric:tabular-nums; }}
  .pill {{ display:inline-block; background:var(--accent); color:#fff; border-radius:20px;
           padding:1px 9px; font-size:11px; font-weight:600; margin-left:4px; }}
  .legend {{ font-size:12px; color:rgba(38,37,30,.6); margin-bottom:16px; }}
  .sw {{ display:inline-block; width:10px; height:10px; border-radius:2px;
         margin:0 5px 0 14px; vertical-align:middle; }}
  .foot {{ font-size:12px; color:rgba(38,37,30,.55); }}
  code {{ background:#e6e5e0; padding:1px 5px; border-radius:4px; font-size:12px; }}
</style></head>
<body><div class="wrap">
  <h1>Query read path &mdash; response merge allocations</h1>
  <p class="sub"><code>pkg/querier/queryrange</code> &middot; median of 8 runs, <code>-benchtime 50x -cpu 4</code>
     &middot; before = <code>main</code>, after = allocation-free <code>priorityqueue.popEntry</code></p>

  <div class="card">
    <h2>Allocations per merge</h2>
    <div class="legend">
      <span class="sw" style="background:#9aa0a6"></span>before
      <span class="sw" style="background:#f54e00"></span>after
    </div>
    {chart}
  </div>

  <div class="card">
    <h2>Full comparison</h2>
    <table>
      <thead><tr><th>benchmark</th>{header}</tr><tr><th></th>{sub}</tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <p class="foot">The merge previously copied a <code>logproto.Stream</code> back onto the heap for every
     entry it emitted &mdash; one short-lived allocation per log line returned. Draining the queue in place
     removes that cost; output is unchanged and pinned by
     <code>TestMergeOrderedNonOverlappingStreams</code>.</p>
</div></body></html>
"""


def main():
    before_path, after_path, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    summary = build(parse(before_path), parse(after_path), out)

    width = max(len(n) for n in summary)
    print(f"{'benchmark'.ljust(width)}  {'allocs/op':>26}  {'B/op':>26}  {'ns/op':>26}")
    for name, m in summary.items():
        cols = []
        for metric, _, _ in METRICS:
            d = m[metric]
            cols.append(
                f"{fmt(metric, d['before'])} -> {fmt(metric, d['after'])}"
                f" ({d['delta_pct']:+.1f}%)".rjust(26)
            )
        print(f"{name.ljust(width)}  " + "  ".join(cols))


if __name__ == "__main__":
    main()
