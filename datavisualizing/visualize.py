"""Turn the simulation CSV output into an interactive web dashboard.

Run it after ``simulation/live_simulation.py`` has produced at least one run
folder in ``simulation/runs/``::

    .venv\\Scripts\\python.exe datavisualizing\\visualize.py

By default it asks whether to render the **newest** run or to **pick** one from
the list of available runs. It then reads that run's
``csv/{cycles,events,daily,summary,checks}_<stamp>.csv`` set, builds a
**self-contained** HTML dashboard (the data is embedded directly, so there are
no cross-origin ``fetch`` problems), writes it to
``datavisualizing/output/dashboard.html`` and — unless ``--no-serve`` is given —
serves it on a local web server and opens it in the browser.

Options
-------
``--runs-dir``  directory holding the run folders (default ``simulation/runs``).
``--stamp``     render a specific run stamp without asking.
``--latest``    render the newest run without asking.
``--port``      local server port (default ``8000``).
``--no-open``   start the server but do not open a browser.
``--no-serve``  only write the HTML file; do not start a server.
"""
from __future__ import annotations

import argparse
import csv
import functools
import glob
import http.server
import json
import os
import re
import socketserver
import sys
import webbrowser
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
DEFAULT_RUNS_DIR = os.path.join(_REPO_ROOT, "simulation", "runs")
OUTPUT_DIR = os.path.join(_HERE, "output")

_DATASETS = ("cycles", "events", "daily", "summary", "checks")
_STAMP_RE = re.compile(r"summary_(\d{8}_\d{6})\.csv$")

# Columns to coerce to numbers when reading the CSVs (everything else stays str).
_NUMERIC_COLUMNS = {
    "hour", "day", "usdc", "sol", "held", "open_offers", "rate_limited",
    "empty_market", "broadcast_fail", "low_balance", "buys", "offers_opened",
    "bumped", "cancelled", "relisted", "markdowns", "sold", "accepted", "fills",
    "reprice_skipped", "recheck_raised", "risk_blocked", "escalated", "paused",
    "price_usd", "market_usd", "resell_usd", "passed",
}


def _coerce(key: str, value: str) -> Any:
    """Coerce a CSV cell to int/float when the column is numeric."""
    if key not in _NUMERIC_COLUMNS:
        return value
    if value in ("", None):
        return 0
    try:
        num = float(value)
    except ValueError:
        return value
    return int(num) if num.is_integer() else num


def _read_csv(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [
            {k: _coerce(k, v) for k, v in row.items()}
            for row in csv.DictReader(fh)
        ]


def _summary_value(key: str, value: str) -> Any:
    """The summary CSV is metric/value; coerce its value column to a number."""
    if value in ("", None):
        return value
    try:
        num = float(value)
    except ValueError:
        return value
    return int(num) if num.is_integer() else num


def _read_summary(path: str) -> list[dict[str, Any]]:
    rows = []
    for row in _read_csv(path):
        rows.append({"metric": row.get("metric", ""),
                     "value": _summary_value("value", str(row.get("value", "")))})
    return rows


def find_runs(runs_dir: str) -> list[tuple[str, str]]:
    """Discover every run folder, newest first.

    Returns a list of ``(stamp, csv_dir)`` pairs for each
    ``<runs_dir>/<stamp>/csv/`` that holds a ``summary_*.csv``.
    """
    runs: list[tuple[str, str]] = []
    if not os.path.isdir(runs_dir):
        return runs
    for name in os.listdir(runs_dir):
        csv_dir = os.path.join(runs_dir, name, "csv")
        if glob.glob(os.path.join(csv_dir, "summary_*.csv")):
            runs.append((name, csv_dir))
    runs.sort(key=lambda item: item[0], reverse=True)
    return runs


def select_run(runs: list[tuple[str, str]]) -> tuple[str, str]:
    """Interactively ask whether to render the newest run or pick one."""
    print("Available runs (newest first):")
    for index, (stamp, _) in enumerate(runs, start=1):
        tag = "  (latest)" if index == 1 else ""
        print(f"  {index}) {stamp}{tag}")
    while True:
        choice = input(
            "Render newest [Enter], or type a run number: ").strip()
        if not choice:
            return runs[0]
        if choice.isdigit() and 1 <= int(choice) <= len(runs):
            return runs[int(choice) - 1]
        print(f"Please press Enter or type a number between 1 and {len(runs)}.")


def load_run(csv_dir: str, stamp: str) -> dict[str, Any]:
    """Load every dataset for one run stamp into a single payload dict."""
    data: dict[str, Any] = {"stamp": stamp}
    for name in _DATASETS:
        path = os.path.join(csv_dir, f"{name}_{stamp}.csv")
        data[name] = _read_summary(path) if name == "summary" else _read_csv(path)
    data["settings"] = _load_settings(csv_dir)
    return data


def _load_settings(csv_dir: str) -> dict[str, Any]:
    """Read ``simulation/trade_setting.json`` (sibling of the csv dir) if present."""
    path = os.path.join(os.path.dirname(os.path.abspath(csv_dir)),
                        "trade_setting.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CollectorCrypt Simulation Dashboard — __STAMP__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1419; --panel: #1a2029; --panel2: #222b36; --line: #2c3744;
    --text: #e6edf3; --muted: #8b98a5; --accent: #4cc2ff; --green: #3fb950;
    --red: #f85149; --amber: #d29922; --violet: #bc8cff; --cyan: #56d4dd;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 14px; }
  header { padding: 20px 28px; border-bottom: 1px solid var(--line);
    background: linear-gradient(180deg, #161c24, #0f1419);
    display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  header h1 { margin: 0; font-size: 20px; }
  header .meta { color: var(--muted); font-size: 13px; }
  .wrap { padding: 24px 28px; max-width: 1380px; margin: 0 auto; }
  .cards { display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin-bottom: 24px; }
  .card { background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 12px 14px; }
  .card .k { color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: .05em; white-space: nowrap; }
  .card .v { font-size: 22px; font-weight: 650; margin-top: 5px;
    font-variant-numeric: tabular-nums; }
  .card .s { font-size: 11px; color: var(--muted); margin-top: 3px; }
  .grid { display: grid; gap: 20px; grid-template-columns: 1fr 1fr; }
  .panel { background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 16px 18px; margin-bottom: 20px; min-width: 0; }
  .panel h2 { margin: 0 0 4px; font-size: 15px; font-weight: 600; }
  .panel .sub { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
  .panel.full { grid-column: 1 / -1; }
  canvas { max-height: 330px; }
  #balanceChart { max-height: 380px; }
  .chiprow { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 10px;
    font-size: 12px; color: var(--muted); }
  .chip::before { content: ""; display: inline-block; width: 10px; height: 10px;
    border-radius: 2px; margin-right: 6px; background: var(--c, #888);
    vertical-align: -1px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 600; position: sticky; top: 0;
    background: var(--panel); z-index: 1; cursor: default; }
  tbody tr:hover { background: rgba(76,194,255,.05); }
  .scroll { max-height: 460px; overflow: auto; border: 1px solid var(--line);
    border-radius: 8px; }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 999px;
    font-size: 12px; font-weight: 600; }
  .pass { background: rgba(63,185,80,.15); color: var(--green); }
  .fail { background: rgba(248,81,73,.15); color: var(--red); }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 600; background: var(--b, var(--panel2));
    color: #0d1117; }
  .result { font-size: 15px; font-weight: 700; }
  .filterbar { margin-bottom: 10px; display: flex; gap: 10px; align-items: center;
    flex-wrap: wrap; }
  select, input { background: var(--panel2); color: var(--text);
    border: 1px solid var(--line); border-radius: 6px; padding: 6px 9px;
    font-size: 13px; }
  input[type=search] { width: 240px; }
  .num { font-variant-numeric: tabular-nums; text-align: right; }
  .checkgrid { display: grid; gap: 8px;
    grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); }
  .checkitem { display: flex; justify-content: space-between; align-items: center;
    background: var(--panel2); border: 1px solid var(--line); border-radius: 8px;
    padding: 8px 12px; font-size: 13px; gap: 10px; }
  .heat { display: grid; grid-template-columns: 46px repeat(24, 1fr); gap: 3px;
    align-items: center; }
  .heat .hlbl { color: var(--muted); font-size: 11px; text-align: center; }
  .heat .dlbl { color: var(--muted); font-size: 12px; padding-right: 6px;
    text-align: right; }
  .heat .cell { height: 26px; border-radius: 4px; background: var(--panel2);
    position: relative; cursor: default; }
  .heat .cell.anom { outline: 1px solid var(--red); outline-offset: -1px; }
  .setgrid { display: grid; gap: 8px;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); }
  .setitem { background: var(--panel2); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px 12px; font-size: 12.5px;
    display: flex; justify-content: space-between; gap: 10px; }
  .setitem .sk { color: var(--muted); overflow: hidden; text-overflow: ellipsis; }
  .setitem .sv { font-weight: 600; font-variant-numeric: tabular-nums;
    white-space: nowrap; }
  @media (max-width: 920px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>CollectorCrypt — 7-Day Live Simulation</h1>
  <span id="resultPill"></span>
  <span class="meta">Run <strong>__STAMP__</strong></span>
</header>
<div class="wrap">
  <div class="cards" id="statCards"></div>

  <div class="panel full"><h2>Wallet balance &amp; inventory over time</h2>
    <p class="sub">USDC (left axis) vs cards held / open offers (right axis).
      Shaded bands mark injected anomalies.</p>
    <canvas id="balanceChart"></canvas>
    <div class="chiprow">
      <span class="chip" style="--c:rgba(248,81,73,.55)">rate-limit (429)</span>
      <span class="chip" style="--c:rgba(210,153,34,.55)">empty market page</span>
      <span class="chip" style="--c:rgba(188,140,255,.5)">broadcast 500 window</span>
      <span class="chip" style="--c:rgba(86,212,221,.45)">low-balance window</span>
    </div>
  </div>

  <div class="grid">
    <div class="panel"><h2>Daily cash flow</h2>
      <p class="sub">USDC change per day (bars) and cumulative P/L vs start (line).</p>
      <canvas id="cashflowChart"></canvas></div>
    <div class="panel"><h2>Cumulative trading activity</h2>
      <p class="sub">Running totals across the week.</p>
      <canvas id="cumulativeChart"></canvas></div>
    <div class="panel"><h2>Activity per day</h2>
      <p class="sub">Stacked confirmed actions by day.</p>
      <canvas id="dailyChart"></canvas></div>
    <div class="panel"><h2>Event type distribution</h2>
      <p class="sub">Share of every logged event type.</p>
      <canvas id="eventTypeChart"></canvas></div>
    <div class="panel"><h2>Entry price vs market value</h2>
      <p class="sub">Every buy and resting offer; distance below the dashed
        parity line is the discount captured.</p>
      <canvas id="scatterChart"></canvas></div>
    <div class="panel"><h2>Sanity checks <span id="checkSummary" class="meta"></span></h2>
      <p class="sub">Every lifecycle branch the week must exercise.</p>
      <div class="checkgrid" id="checkGrid"></div></div>
  </div>

  <div class="panel full"><h2>Hourly activity heatmap</h2>
    <p class="sub">Confirmed actions per hour (darker = more). Red outline =
      anomaly active that hour.</p>
    <div class="heat" id="heatmap"></div></div>

  <div class="panel full" id="settingsPanel" style="display:none">
    <h2>Run settings (simulation/trade_setting.json)</h2>
    <p class="sub">The tunables this simulation ran with.</p>
    <div class="setgrid" id="setGrid"></div></div>

  <div class="panel full"><h2>Event log</h2>
    <div class="filterbar">
      <label>Type:
        <select id="eventFilter"><option value="">all</option></select></label>
      <input type="search" id="eventSearch" placeholder="Search card / nft / detail…">
      <span id="eventCount" class="meta"></span>
    </div>
    <div class="scroll"><table id="eventsTable">
      <thead><tr><th>Hour</th><th>Day</th><th>Time (UTC)</th><th>Type</th>
        <th>Card</th><th class="num">Price</th><th class="num">Market</th>
        <th>Detail</th></tr></thead>
      <tbody></tbody></table></div>
  </div>
</div>

<script id="run-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById("run-data").textContent);
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const COLORS = { accent: css("--accent"), green: css("--green"), red: css("--red"),
  amber: css("--amber"), violet: css("--violet"), cyan: css("--cyan"),
  muted: css("--muted") };
Chart.defaults.color = css("--muted");
Chart.defaults.borderColor = css("--line");
Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

const sum = (rows, key) => rows.reduce((a, r) => a + (Number(r[key]) || 0), 0);
const metric = (k) => {
  const row = DATA.summary.find((r) => r.metric === k);
  return row ? Number(row.value) : 0;
};
const fmt = (n) => (typeof n === "number" && !Number.isInteger(n))
  ? n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  : Number(n).toLocaleString();
const money = (n) => (n < 0 ? "-$" : "$") + fmt(Math.abs(n));

const cycles = DATA.cycles.slice().sort((a, b) => a.hour - b.hour);
const labels = cycles.map((c) =>
  "D" + c.day + " " + String(c.hour % 24).padStart(2, "0") + ":00");
const ACTIVITY_KEYS = ["buys", "offers_opened", "bumped", "cancelled", "relisted",
  "markdowns", "sold", "accepted", "fills"];

// ---- result pill --------------------------------------------------------- //
const passed = sum(DATA.checks, "passed");
const total = DATA.checks.length;
const allOk = passed === total;
document.getElementById("resultPill").innerHTML =
  `<span class="result" style="color:${allOk ? COLORS.green : COLORS.red}">`
  + `${allOk ? "ALL CHECKS PASSED" : "SOME CHECKS FAILED"} (${passed}/${total})</span>`;

// ---- stat cards ---------------------------------------------------------- //
const startUsdc = metric("start_usdc") || (cycles[0] ? cycles[0].usdc : 0);
const netPL = metric("final_usdc") - startUsdc;
const CARDS = [
  ["Net USDC \\u0394", money(netPL), `from $${fmt(startUsdc)} start`,
    netPL >= 0 ? COLORS.green : COLORS.red],
  ["Final USDC", "$" + fmt(metric("final_usdc")),
    `low $${fmt(metric("lowest_usdc"))}`],
  ["Cards held", fmt(metric("cards_still_held")),
    `${fmt(metric("open_offers_resting"))} offers resting`],
  ["Buys confirmed", fmt(metric("direct_buys_confirmed")),
    `${fmt(metric("resting_offers_filled"))} via filled offers`],
  ["Offers opened", fmt(metric("offers_opened")),
    `${fmt(metric("offers_bumped"))} bumped / ${fmt(metric("offers_cancelled"))} cancelled`],
  ["Cards relisted", fmt(metric("cards_relisted")),
    `${fmt(metric("listings_marked_down"))} markdowns`],
  ["Exits", fmt(metric("sales_detected") + metric("incoming_offers_accepted")),
    `${fmt(metric("sales_detected"))} sold / ${fmt(metric("incoming_offers_accepted"))} accepted`],
  ["Risk-blocked", fmt(metric("risk_blocked_orders")),
    `${fmt(metric("contested_offers_skipped"))} contested skips`],
  ["Cycles OK", `${fmt(metric("cycles_executed_ok"))}/${fmt(metric("cycles_total"))}`,
    `${fmt(metric("sourcing_aborted_429"))} aborted (429)`],
];
document.getElementById("statCards").innerHTML = CARDS.map(([k, v, s, color]) =>
  `<div class="card"><div class="k">${k}</div>`
  + `<div class="v"${color ? ` style="color:${color}"` : ""}>${v}</div>`
  + `<div class="s">${s || ""}</div></div>`).join("");

// ---- anomaly band plugin -------------------------------------------------- //
const BAND_COLORS = {
  rate_limited: "rgba(248,81,73,.16)",
  empty_market: "rgba(210,153,34,.16)",
  broadcast_fail: "rgba(188,140,255,.13)",
  low_balance: "rgba(86,212,221,.12)",
};
const anomalyBands = {
  id: "anomalyBands",
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea, scales: { x } } = chart;
    if (!chartArea) return;
    ctx.save();
    cycles.forEach((c, i) => {
      for (const key of Object.keys(BAND_COLORS)) {
        if (!Number(c[key])) continue;
        const x0 = x.getPixelForValue(Math.max(i - 0.5, x.min));
        const x1 = x.getPixelForValue(Math.min(i + 0.5, x.max));
        ctx.fillStyle = BAND_COLORS[key];
        ctx.fillRect(x0, chartArea.top, x1 - x0, chartArea.bottom - chartArea.top);
      }
    });
    ctx.restore();
  },
};

// ---- balance & inventory over time --------------------------------------- //
const balCtx = document.getElementById("balanceChart").getContext("2d");
const grad = balCtx.createLinearGradient(0, 0, 0, 360);
grad.addColorStop(0, "rgba(63,185,80,.28)");
grad.addColorStop(1, "rgba(63,185,80,0)");
new Chart(balCtx, {
  type: "line",
  data: { labels, datasets: [
    { label: "USDC", data: cycles.map((c) => c.usdc), borderColor: COLORS.green,
      backgroundColor: grad, fill: true, yAxisID: "y", tension: .25,
      pointRadius: 0, borderWidth: 2 },
    { label: "Cards held", data: cycles.map((c) => c.held),
      borderColor: COLORS.accent, backgroundColor: "transparent", yAxisID: "y1",
      tension: .25, pointRadius: 0 },
    { label: "Open offers", data: cycles.map((c) => c.open_offers),
      borderColor: COLORS.violet, backgroundColor: "transparent", yAxisID: "y1",
      tension: .25, pointRadius: 0 },
  ]},
  options: { responsive: true, interaction: { mode: "index", intersect: false },
    plugins: { tooltip: { callbacks: { footer: (items) => {
      const c = cycles[items[0].dataIndex]; const out = [];
      if (Number(c.rate_limited)) out.push("anomaly: rate-limit (429)");
      if (Number(c.empty_market)) out.push("anomaly: empty market page");
      if (Number(c.broadcast_fail)) out.push("anomaly: broadcast 500");
      if (Number(c.low_balance)) out.push("anomaly: low balance");
      return out.join("\\n");
    } } } },
    scales: {
      y: { position: "left", title: { display: true, text: "USDC" } },
      y1: { position: "right", title: { display: true, text: "count" },
        grid: { drawOnChartArea: false } },
      x: { ticks: { maxTicksLimit: 14, maxRotation: 0 } } } },
  plugins: [anomalyBands],
});

// ---- daily cash flow ------------------------------------------------------ //
const dayEnd = {};
for (const c of cycles) dayEnd[c.day] = c.usdc;
const days = Object.keys(dayEnd).map(Number).sort((a, b) => a - b);
let prev = startUsdc;
const deltas = days.map((d) => { const v = dayEnd[d] - prev; prev = dayEnd[d]; return v; });
const cumPL = []; let acc = 0;
for (const v of deltas) { acc += v; cumPL.push(acc); }
new Chart(document.getElementById("cashflowChart"), {
  data: { labels: days.map((d) => "Day " + d), datasets: [
    { type: "bar", label: "Daily \\u0394 USDC", data: deltas,
      backgroundColor: deltas.map((v) =>
        v >= 0 ? "rgba(63,185,80,.55)" : "rgba(248,81,73,.55)"),
      borderColor: deltas.map((v) => v >= 0 ? COLORS.green : COLORS.red),
      borderWidth: 1 },
    { type: "line", label: "Cumulative P/L", data: cumPL,
      borderColor: COLORS.accent, backgroundColor: "transparent",
      tension: .25, pointRadius: 3 },
  ]},
  options: { responsive: true, interaction: { mode: "index", intersect: false },
    plugins: { tooltip: { callbacks: {
      label: (i) => `${i.dataset.label}: ${money(i.parsed.y)}` } } },
    scales: { y: { title: { display: true, text: "USDC" } } } },
});

// ---- cumulative activity ------------------------------------------------- //
const cum = (key) => { let t = 0; return cycles.map((c) => (t += (Number(c[key]) || 0))); };
new Chart(document.getElementById("cumulativeChart"), {
  type: "line",
  data: { labels, datasets: [
    { label: "Buys", data: cum("buys"), borderColor: COLORS.green,
      backgroundColor: "transparent", tension: .2, pointRadius: 0 },
    { label: "Offers opened", data: cum("offers_opened"), borderColor: COLORS.accent,
      backgroundColor: "transparent", tension: .2, pointRadius: 0 },
    { label: "Relisted", data: cum("relisted"), borderColor: COLORS.violet,
      backgroundColor: "transparent", tension: .2, pointRadius: 0 },
    { label: "Markdowns", data: cum("markdowns"), borderColor: COLORS.amber,
      backgroundColor: "transparent", tension: .2, pointRadius: 0 },
    { label: "Sold", data: cum("sold"), borderColor: COLORS.red,
      backgroundColor: "transparent", tension: .2, pointRadius: 0 },
    { label: "Fills", data: cum("fills"), borderColor: COLORS.cyan,
      backgroundColor: "transparent", tension: .2, pointRadius: 0 },
  ]},
  options: { responsive: true, interaction: { mode: "index", intersect: false },
    scales: { x: { ticks: { maxTicksLimit: 14, maxRotation: 0 } } } },
});

// ---- per-day stacked bar ------------------------------------------------- //
const daily = DATA.daily.slice().sort((a, b) => a.day - b.day);
const DAY_SERIES = [
  ["buys", COLORS.green], ["offers_opened", COLORS.accent],
  ["bumped", COLORS.violet], ["cancelled", COLORS.muted],
  ["relisted", "#5fa8ff"], ["markdowns", COLORS.amber],
  ["accepted", "#7ee787"], ["sold", COLORS.red], ["fills", COLORS.cyan],
];
new Chart(document.getElementById("dailyChart"), {
  type: "bar",
  data: { labels: daily.map((d) => "Day " + d.day),
    datasets: DAY_SERIES.map(([k, color]) => ({
      label: k, data: daily.map((d) => d[k] || 0), backgroundColor: color })) },
  options: { responsive: true, scales: { x: { stacked: true }, y: { stacked: true } } },
});

// ---- event type distribution --------------------------------------------- //
const typeCounts = {};
for (const e of DATA.events) typeCounts[e.type] = (typeCounts[e.type] || 0) + 1;
const typeLabels = Object.keys(typeCounts).sort((a, b) => typeCounts[b] - typeCounts[a]);
const palette = ["#3fb950", "#4cc2ff", "#bc8cff", "#d29922", "#f85149", "#7ee787",
  "#5fa8ff", "#ff9bce", "#8b98a5", "#e3b341", "#56d4dd", "#ff7b72", "#a5d6ff",
  "#ffa657", "#d2a8ff", "#79c0ff"];
const TYPE_COLOR = {};
typeLabels.forEach((t, i) => { TYPE_COLOR[t] = palette[i % palette.length]; });
new Chart(document.getElementById("eventTypeChart"), {
  type: "doughnut",
  data: { labels: typeLabels,
    datasets: [{ data: typeLabels.map((t) => typeCounts[t]),
      backgroundColor: typeLabels.map((t) => TYPE_COLOR[t]),
      borderColor: css("--panel"), borderWidth: 2 }] },
  options: { responsive: true, plugins: { legend: { position: "right" } } },
});

// ---- entry price vs market scatter ---------------------------------------- //
const buyPts = DATA.events.filter((e) => e.type === "buy" && e.market_usd > 0)
  .map((e) => ({ x: e.market_usd, y: e.price_usd, name: e.name }));
const offerPts = DATA.events.filter((e) => e.type === "offer" && e.market_usd > 0)
  .map((e) => ({ x: e.market_usd, y: e.price_usd, name: e.name }));
const maxM = Math.max(1, ...buyPts.map((p) => p.x), ...offerPts.map((p) => p.x));
new Chart(document.getElementById("scatterChart"), {
  type: "scatter",
  data: { datasets: [
    { label: "Buys", data: buyPts, backgroundColor: "rgba(63,185,80,.75)",
      pointRadius: 4 },
    { label: "Offers", data: offerPts, backgroundColor: "rgba(76,194,255,.75)",
      pointRadius: 4 },
    { label: "Market parity", type: "line",
      data: [{ x: 0, y: 0 }, { x: maxM * 1.05, y: maxM * 1.05 }],
      borderColor: COLORS.muted, borderDash: [6, 5], borderWidth: 1,
      pointRadius: 0 },
  ]},
  options: { responsive: true,
    plugins: { tooltip: { callbacks: { label: (i) => {
      const p = i.raw;
      if (p.name === undefined) return "";
      const disc = p.x > 0 ? Math.round((1 - p.y / p.x) * 100) : 0;
      return `${p.name}: $${fmt(p.y)} vs market $${fmt(p.x)} (-${disc}%)`;
    } } } },
    scales: {
      x: { title: { display: true, text: "market value (USD)" },
        beginAtZero: true },
      y: { title: { display: true, text: "our price (USD)" },
        beginAtZero: true } } },
});

// ---- sanity checks -------------------------------------------------------- //
document.getElementById("checkSummary").textContent = ` ${passed}/${total} passed`;
document.getElementById("checkGrid").innerHTML = DATA.checks.map((c) =>
  `<div class="checkitem"><span>${c.check}</span>`
  + `<span class="pill ${c.passed ? "pass" : "fail"}">`
  + `${c.passed ? "PASS" : "FAIL"}</span></div>`).join("");

// ---- hourly heatmap ------------------------------------------------------- //
const byHour = {};
for (const c of cycles) byHour[c.hour] = c;
const heatMax = Math.max(1, ...cycles.map((c) =>
  ACTIVITY_KEYS.reduce((a, k) => a + (Number(c[k]) || 0), 0)));
let heatHtml = "<div></div>";
for (let h = 0; h < 24; h++)
  heatHtml += `<div class="hlbl">${h % 3 === 0 ? h : ""}</div>`;
for (let d = 1; d <= 7; d++) {
  heatHtml += `<div class="dlbl">D${d}</div>`;
  for (let h = 0; h < 24; h++) {
    const c = byHour[(d - 1) * 24 + h];
    if (!c) { heatHtml += `<div class="cell" title="no data"></div>`; continue; }
    const v = ACTIVITY_KEYS.reduce((a, k) => a + (Number(c[k]) || 0), 0);
    const anom = ["rate_limited", "empty_market", "broadcast_fail", "low_balance"]
      .some((k) => Number(c[k]));
    const alpha = v > 0 ? (0.12 + 0.78 * (v / heatMax)) : 0;
    const bg = v > 0 ? `background: rgba(76,194,255,${alpha.toFixed(2)});` : "";
    const tip = `D${d} ${String(h).padStart(2, "0")}:00 — ${v} action(s)`
      + ACTIVITY_KEYS.filter((k) => Number(c[k]))
        .map((k) => `\\n  ${k}: ${c[k]}`).join("")
      + (anom ? "\\n  [anomaly active]" : "");
    heatHtml += `<div class="cell${anom ? " anom" : ""}" style="${bg}" title="${tip}"></div>`;
  }
}
document.getElementById("heatmap").innerHTML = heatHtml;

// ---- run settings ---------------------------------------------------------- //
const settings = DATA.settings || {};
const setKeys = Object.keys(settings);
if (setKeys.length) {
  document.getElementById("settingsPanel").style.display = "";
  document.getElementById("setGrid").innerHTML = setKeys.map((k) =>
    `<div class="setitem"><span class="sk" title="${k}">${k}</span>`
    + `<span class="sv">${Array.isArray(settings[k])
        ? settings[k].join(", ") : settings[k]}</span></div>`).join("");
}

// ---- events table + filter + search --------------------------------------- //
const filterSel = document.getElementById("eventFilter");
for (const t of typeLabels) {
  const opt = document.createElement("option"); opt.value = t; opt.textContent = t;
  filterSel.appendChild(opt);
}
const searchBox = document.getElementById("eventSearch");
const MAX_ROWS = 1500;
function renderEvents() {
  const f = filterSel.value;
  const q = searchBox.value.trim().toLowerCase();
  const rows = DATA.events.filter((e) =>
    (!f || e.type === f)
    && (!q || `${e.name} ${e.nft} ${e.detail} ${e.type}`.toLowerCase().includes(q)));
  const shown = rows.slice(0, MAX_ROWS);
  document.getElementById("eventCount").textContent =
    rows.length > MAX_ROWS
      ? `${rows.length} events (showing first ${MAX_ROWS})`
      : `${rows.length} events`;
  document.querySelector("#eventsTable tbody").innerHTML = shown.map((e) =>
    `<tr><td>${e.hour}</td><td>${e.day}</td><td>${e.iso_time || ""}</td>`
    + `<td><span class="badge" style="--b:${TYPE_COLOR[e.type] || "#8b98a5"}">`
    + `${e.type}</span></td><td>${e.name || ""}</td>`
    + `<td class="num">${e.price_usd ? "$" + fmt(e.price_usd) : ""}</td>`
    + `<td class="num">${e.market_usd ? "$" + fmt(e.market_usd) : ""}</td>`
    + `<td>${e.detail || ""}</td></tr>`).join("");
}
filterSel.addEventListener("change", renderEvents);
searchBox.addEventListener("input", renderEvents);
renderEvents();
</script>
</body>
</html>
"""


def render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (_HTML_TEMPLATE
            .replace("__STAMP__", data.get("stamp", "?"))
            .replace("__DATA__", payload))


def build_dashboard(csv_dir: str, stamp: str) -> str:
    """Build the dashboard HTML for one run; returns the html path."""
    data = load_run(csv_dir, stamp)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    html_path = os.path.join(OUTPUT_DIR, "dashboard.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(data))
    return html_path


def serve(html_path: str, port: int, open_browser: bool) -> None:
    """Serve the output directory and (optionally) open the dashboard."""
    directory = os.path.dirname(html_path)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=directory)
    rel = os.path.basename(html_path)
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/{rel}"
        print(f"Serving dashboard at {url}")
        print("Press Ctrl+C to stop.")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    parser.add_argument("--stamp", default=None)
    parser.add_argument("--latest", action="store_true",
                        help="render the newest run without asking")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--no-serve", action="store_true")
    args = parser.parse_args()

    runs = find_runs(args.runs_dir)
    if not runs:
        raise SystemExit(
            f"No runs found in {args.runs_dir}. "
            "Run simulation/live_simulation.py first.")
    run_map = dict(runs)

    if args.stamp:
        if args.stamp not in run_map:
            raise SystemExit(f"Run {args.stamp} not found in {args.runs_dir}.")
        stamp, csv_dir = args.stamp, run_map[args.stamp]
    elif args.latest or len(runs) == 1 or not sys.stdin.isatty():
        stamp, csv_dir = runs[0]
    else:
        stamp, csv_dir = select_run(runs)

    html_path = build_dashboard(csv_dir, stamp)
    print(f"Dashboard for run {stamp} written to: {html_path}")
    if args.no_serve:
        print(f"Open it directly in a browser: file:///{html_path.replace(os.sep, '/')}")
        return 0
    serve(html_path, args.port, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
