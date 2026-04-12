#!/usr/bin/env python3
"""
Transform: Move system metric charts to dashboard + compact health cards.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # 1. Make health stat cards smaller and fit on one line
    html = compact_health_cards(html)

    # 2. Find the metrics charts HTML and duplicate into dashboard
    html = move_charts_to_dashboard(html)

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Output: {INPUT}")


def compact_health_cards(html):
    """Make the health stat grid use smaller cards that fit 5 on one line."""

    # Replace the stat-grid to use a fixed 5-column layout for health
    # Add a new CSS class for compact stat cards
    compact_css = """
/* ── Compact health row (5 items on one line) ────────────────────────────── */
.health-row {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: var(--sp-3);
  margin-bottom: var(--sp-6);
}
.health-row .stat-card {
  padding: var(--sp-3) var(--sp-4);
  gap: var(--sp-3);
}
.health-row .stat-icon {
  width: 34px; height: 34px;
  border-radius: var(--radius-xs);
}
.health-row .stat-icon svg { width: 16px; height: 16px; }
.health-row .stat-label { font-size: 10px; }
.health-row .stat-value { font-size: var(--text-base); margin-top: 1px; }
@media (max-width: 900px) { .health-row { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 480px) { .health-row { grid-template-columns: repeat(2, 1fr); } }

/* ── Dashboard charts section ────────────────────────────────────────────── */
.dash-charts-section {
  margin-bottom: var(--sp-6);
}
.dash-charts-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--sp-4);
}
@media (max-width: 720px) { .dash-charts-grid { grid-template-columns: 1fr; } }
"""
    html = html.replace("</style>", compact_css + "\n</style>", 1)

    # Change the health stats grid class from stat-grid to health-row
    html = html.replace(
        '<div class="stat-grid" id="health-stats">',
        '<div class="health-row" id="health-stats">',
        1
    )

    return html


def move_charts_to_dashboard(html):
    """Add chart containers to the dashboard that mirror the metrics section charts."""

    # Find the end of the dashboard section (before the system gauges summary cards)
    # We'll insert chart placeholders after the system gauges grid
    
    # Find the closing </div> of the dash-sys-grid and insert charts after it
    marker = '      <div style="font-size:var(--text-sm);font-weight:var(--fw-semibold);color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:var(--sp-3);">System Resources</div>'
    
    # Find the end of the dashboard section to insert before it
    # Look for the closing of section-dashboard
    dashboard_end = '    </div>\n\n    <!-- '
    
    charts_html = """
      <!-- Dashboard Charts -->
      <div style="font-size:var(--text-sm);font-weight:var(--fw-semibold);color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:var(--sp-3);margin-top:var(--sp-6);">Performance Overview</div>
      <div class="dash-charts-grid">
        <div class="card mb-0">
          <div class="card-header">
            <div class="card-title">CPU & GPU Usage</div>
          </div>
          <div class="chart-wrap" id="dash-chart-cpu-wrap">
            <canvas id="dash-chart-cpu"></canvas>
          </div>
        </div>
        <div class="card mb-0">
          <div class="card-header">
            <div class="card-title">Memory & Disk</div>
          </div>
          <div class="chart-wrap" id="dash-chart-mem-wrap">
            <canvas id="dash-chart-mem"></canvas>
          </div>
        </div>
      </div>
"""

    # Insert the charts HTML right before the closing of section-dashboard
    # Find "</div>" that closes section-dashboard
    # The dashboard section ends with </div> followed by CONFIG section comment
    config_marker = '    <!-- '
    
    # Find the section-dashboard div and its content
    dash_start = html.find('<div id="section-dashboard"')
    if dash_start == -1:
        print("WARNING: Could not find section-dashboard")
        return html
    
    # Find the next section after dashboard
    config_start = html.find('<div id="section-config"', dash_start)
    if config_start == -1:
        print("WARNING: Could not find section-config")
        return html
    
    # Find the closing </div> of section-dashboard (it's right before section-config)
    # Walk backwards from config_start to find the </div>\n pattern
    search_area = html[dash_start:config_start]
    last_close = search_area.rfind('</div>')
    if last_close == -1:
        print("WARNING: Could not find dashboard closing div")
        return html
    
    insert_pos = dash_start + last_close
    html = html[:insert_pos] + charts_html + "\n    " + html[insert_pos:]

    # Now add JS to populate the dashboard charts when dashboard loads
    chart_js = """
// ── Dashboard Charts ────────────────────────────────────────────────────────
let _dashCpuChart = null;
let _dashMemChart = null;
const _dashCpuData = { labels: [], cpu: [], gpu: [] };
const _dashMemData = { labels: [], ram: [], disk: [] };
const _DASH_CHART_MAX_POINTS = 30;

function _initDashCharts() {
  const cpuCanvas = document.getElementById('dash-chart-cpu');
  const memCanvas = document.getElementById('dash-chart-mem');
  if (!cpuCanvas || !memCanvas) return;
  if (_dashCpuChart) return; // already initialized

  const chartOpts = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 300 },
    plugins: { legend: { display: true, position: 'bottom', labels: { boxWidth: 8, padding: 12, font: { size: 11 }, color: getComputedStyle(document.documentElement).getPropertyValue('--text3').trim() || '#5a5d7a' } } },
    scales: {
      x: { display: false },
      y: { min: 0, max: 100, ticks: { font: { size: 10 }, color: getComputedStyle(document.documentElement).getPropertyValue('--text3').trim() || '#5a5d7a', callback: v => v + '%' }, grid: { color: 'rgba(128,128,128,.1)' } }
    }
  };

  _dashCpuChart = new Chart(cpuCanvas, {
    type: 'line',
    data: {
      labels: _dashCpuData.labels,
      datasets: [
        { label: 'CPU', data: _dashCpuData.cpu, borderColor: '#00cec9', backgroundColor: 'rgba(0,206,201,.1)', borderWidth: 2, tension: .4, fill: true, pointRadius: 0 },
        { label: 'GPU', data: _dashCpuData.gpu, borderColor: '#6c5ce7', backgroundColor: 'rgba(108,92,231,.1)', borderWidth: 2, tension: .4, fill: true, pointRadius: 0 },
      ]
    },
    options: chartOpts
  });

  _dashMemChart = new Chart(memCanvas, {
    type: 'line',
    data: {
      labels: _dashMemData.labels,
      datasets: [
        { label: 'RAM %', data: _dashMemData.ram, borderColor: '#00b894', backgroundColor: 'rgba(0,184,148,.1)', borderWidth: 2, tension: .4, fill: true, pointRadius: 0 },
        { label: 'Disk %', data: _dashMemData.disk, borderColor: '#fdcb6e', backgroundColor: 'rgba(253,203,110,.1)', borderWidth: 2, tension: .4, fill: true, pointRadius: 0 },
      ]
    },
    options: chartOpts
  });
}

function _updateDashCharts(s) {
  if (!s || !_dashCpuChart) return;
  const now = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});

  _dashCpuData.labels.push(now);
  _dashCpuData.cpu.push(s.cpu_pct ?? 0);
  _dashCpuData.gpu.push(s.gpu_util ?? 0);
  if (_dashCpuData.labels.length > _DASH_CHART_MAX_POINTS) {
    _dashCpuData.labels.shift(); _dashCpuData.cpu.shift(); _dashCpuData.gpu.shift();
  }

  const ramPct = (s.ram_total && s.ram_used) ? (s.ram_used / s.ram_total * 100) : 0;
  const diskPct = (s.disk_total && s.disk_used) ? (s.disk_used / s.disk_total * 100) : 0;
  _dashMemData.labels.push(now);
  _dashMemData.ram.push(ramPct);
  _dashMemData.disk.push(diskPct);
  if (_dashMemData.labels.length > _DASH_CHART_MAX_POINTS) {
    _dashMemData.labels.shift(); _dashMemData.ram.shift(); _dashMemData.disk.shift();
  }

  _dashCpuChart.update('none');
  _dashMemChart.update('none');
}
"""

    # Inject the chart JS before the closing </script>
    html = html.replace("</script>\n</body>", chart_js + "\n</script>\n</body>", 1)

    # Hook into the existing _applyGaugeDash to also update dashboard charts
    html = html.replace(
        "function _applyGaugeDash(s) {",
        "function _applyGaugeDash(s) {\n  _updateDashCharts(s);",
        1
    )

    # Hook into loadDashboard to initialize charts
    html = html.replace(
        "async function loadDashboard() {",
        "async function loadDashboard() {\n  _initDashCharts();",
        1
    )

    return html


if __name__ == "__main__":
    main()
