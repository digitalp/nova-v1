#!/usr/bin/env python3
"""
Transform: Fix log visibility in light mode, redesign dashboard charts
with historical data (Dribbble style), remove system metrics section,
and update font to match Dribbble designs.
"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # 1. Update font to Plus Jakarta Sans (Dribbble-style)
    html = update_font(html)

    # 2. Fix log viewer and decision log light mode visibility
    html = fix_log_light_mode(html)

    # 3. Redesign dashboard charts to match Dribbble screenshot
    html = redesign_dashboard_charts(html)

    # 4. Remove the system metrics section and its nav item
    html = remove_metrics_section(html)

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Output: {INPUT}")


def update_font(html):
    """Replace Inter with Plus Jakarta Sans — the Dribbble dashboard font."""
    # Update the Google Fonts import
    html = html.replace(
        "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap",
        "https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap",
        1
    )
    # Update the font-family declaration
    html = html.replace(
        "font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;",
        "font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;",
    )
    # Also update any -apple-system references in FA section
    html = html.replace(
        "font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif;",
        "font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;",
    )
    html = html.replace(
        "font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;",
        "font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;",
    )
    return html


def fix_log_light_mode(html):
    """Make log streams much more visible in light mode."""
    # Replace the existing light mode log overrides with better ones
    old_log_overrides = """/* Log viewer */
[data-theme="light"] #log-output,
[data-theme="light"] #pylog-output {
  background: var(--surface2);
  border-color: var(--border);
  color: var(--text);
}
[data-theme="light"] .log-line.lvl-info { color: var(--text2); }
[data-theme="light"] .log-line.lvl-warning { color: #b8860b; }
[data-theme="light"] .log-line.lvl-error { color: #c0392b; }
[data-theme="light"] .log-line.lvl-critical { color: #e74c3c; }
[data-theme="light"] .log-line.lvl-debug { color: var(--text3); }"""

    new_log_overrides = """/* Log viewer — high contrast in light mode */
[data-theme="light"] #log-output,
[data-theme="light"] #pylog-output {
  background: #1e1e2e;
  border-color: var(--border);
  color: #cdd6f4;
}
[data-theme="light"] .log-line { color: #cdd6f4; }
[data-theme="light"] .log-line.lvl-info { color: #a6adc8; }
[data-theme="light"] .log-line.lvl-warning { color: #f9e2af; }
[data-theme="light"] .log-line.lvl-error { color: #f38ba8; }
[data-theme="light"] .log-line.lvl-critical { color: #eba0ac; font-weight: 600; }
[data-theme="light"] .log-line.lvl-debug { color: #585b70; }

/* Decision log — keep dark terminal look in light mode too */
[data-theme="light"] #dec-log {
  background: #1e1e2e !important;
  color: #cdd6f4;
}"""

    html = html.replace(old_log_overrides, new_log_overrides, 1)
    return html


def redesign_dashboard_charts(html):
    """Replace the current dashboard charts with Dribbble-style historical charts."""

    # Find and replace the existing dashboard charts JS
    old_chart_init = """// ── Dashboard Charts ────────────────────────────────────────────────────────
let _dashCpuChart = null;
let _dashMemChart = null;
const _dashCpuData = { labels: [], cpu: [], gpu: [] };
const _dashMemData = { labels: [], ram: [], disk: [] };
const _DASH_CHART_MAX_POINTS = 30;"""

    new_chart_init = """// ── Dashboard Charts (Historical — Dribbble style) ──────────────────────────
let _dashCpuChart = null;
let _dashGpuChart = null;
let _dashChartsInitialized = false;
const _DASH_CHART_MAX_POINTS = 60;"""

    html = html.replace(old_chart_init, new_chart_init, 1)

    # Replace the _initDashCharts function
    old_init = """function _initDashCharts() {
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
}"""

    new_init = """async function _initDashCharts() {
  if (_dashChartsInitialized) return;
  const cpuCanvas = document.getElementById('dash-chart-cpu');
  const gpuCanvas = document.getElementById('dash-chart-mem');
  if (!cpuCanvas || !gpuCanvas) return;
  _dashChartsInitialized = true;

  const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  const gridColor = isDark ? 'rgba(255,255,255,.06)' : 'rgba(0,0,0,.06)';
  const tickColor = isDark ? '#5a5d7a' : '#9a9cb8';
  const legendColor = isDark ? '#a0a3bd' : '#5a5d7a';

  const baseOpts = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 400 },
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: true, position: 'top', align: 'start',
        labels: { boxWidth: 10, boxHeight: 10, padding: 16, usePointStyle: true, pointStyle: 'rectRounded',
          font: { size: 12, family: "'Plus Jakarta Sans', sans-serif", weight: '500' }, color: legendColor } },
      tooltip: { backgroundColor: isDark ? '#1c1e2e' : '#fff', titleColor: isDark ? '#f0f0f5' : '#1a1c2e',
        bodyColor: isDark ? '#a0a3bd' : '#5a5d7a', borderColor: isDark ? 'rgba(255,255,255,.1)' : 'rgba(0,0,0,.1)',
        borderWidth: 1, padding: 12, cornerRadius: 10,
        titleFont: { size: 13, weight: '600', family: "'Plus Jakarta Sans', sans-serif" },
        bodyFont: { size: 12, family: "'Plus Jakarta Sans', sans-serif" },
        callbacks: { label: ctx => ' ' + ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + '%' } }
    },
    scales: {
      x: { display: true, grid: { display: false },
        ticks: { font: { size: 10, family: "'Plus Jakarta Sans', sans-serif" }, color: tickColor, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
      y: { min: 0, max: 100, grid: { color: gridColor, drawBorder: false },
        ticks: { font: { size: 10, family: "'Plus Jakarta Sans', sans-serif" }, color: tickColor, callback: v => v + '%', stepSize: 20 },
        border: { display: false } }
    }
  };

  // Load historical data from the metrics API
  let histLabels = [], histCpu = [], histRam = [], histGpu = [];
  try {
    const d = await api('GET', '/admin/metrics');
    const history = d.history || [];
    history.forEach(h => {
      const t = new Date(h.ts);
      histLabels.push(t.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}));
      histCpu.push(h.cpu_pct ?? 0);
      histRam.push(h.ram_total ? (h.ram_used / h.ram_total * 100) : 0);
      histGpu.push(h.gpu_util ?? 0);
    });
  } catch(e) { /* no history available */ }

  _dashCpuChart = new Chart(cpuCanvas, {
    type: 'line',
    data: {
      labels: histLabels.length ? histLabels : [],
      datasets: [
        { label: 'CPU %', data: histCpu, borderColor: '#00cec9', backgroundColor: 'rgba(0,206,201,.15)',
          borderWidth: 2.5, tension: .4, fill: true, pointRadius: 0, pointHoverRadius: 4 },
        { label: 'RAM %', data: histRam, borderColor: '#6c5ce7', backgroundColor: 'transparent',
          borderWidth: 2, tension: .4, fill: false, pointRadius: 2, pointBackgroundColor: '#6c5ce7',
          pointBorderColor: '#6c5ce7', pointHoverRadius: 5, borderDash: [] },
      ]
    },
    options: { ...baseOpts }
  });

  _dashGpuChart = new Chart(gpuCanvas, {
    type: 'line',
    data: {
      labels: histLabels.length ? histLabels : [],
      datasets: [
        { label: 'GPU %', data: histGpu, borderColor: '#00b894', backgroundColor: 'rgba(0,184,148,.15)',
          borderWidth: 2.5, tension: .4, fill: true, pointRadius: 0, pointHoverRadius: 4 },
      ]
    },
    options: { ...baseOpts }
  });
}"""

    html = html.replace(old_init, new_init, 1)

    # Replace the _updateDashCharts function
    old_update = """function _updateDashCharts(s) {
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
}"""

    new_update = """function _updateDashCharts(s) {
  if (!s || !_dashCpuChart) return;
  const now = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

  // CPU & RAM chart
  const cpuData = _dashCpuChart.data;
  cpuData.labels.push(now);
  cpuData.datasets[0].data.push(s.cpu_pct ?? 0);
  const ramPct = (s.ram_total && s.ram_used) ? (s.ram_used / s.ram_total * 100) : 0;
  cpuData.datasets[1].data.push(ramPct);
  if (cpuData.labels.length > _DASH_CHART_MAX_POINTS) {
    cpuData.labels.shift();
    cpuData.datasets.forEach(ds => ds.data.shift());
  }
  _dashCpuChart.update('none');

  // GPU chart
  if (_dashGpuChart) {
    const gpuData = _dashGpuChart.data;
    gpuData.labels.push(now);
    gpuData.datasets[0].data.push(s.gpu_util ?? 0);
    if (gpuData.labels.length > _DASH_CHART_MAX_POINTS) {
      gpuData.labels.shift();
      gpuData.datasets.forEach(ds => ds.data.shift());
    }
    _dashGpuChart.update('none');
  }
}"""

    html = html.replace(old_update, new_update, 1)

    # Update the dashboard chart HTML titles to match Dribbble
    html = html.replace(
        '<div class="card-title">CPU & GPU Usage</div>',
        '<div class="card-title-lg">CPU & RAM History</div>\n            <div class="card-sub">Last 24 hours (hourly avg)</div>',
        1
    )
    html = html.replace(
        '<div class="card-title">Memory & Disk</div>',
        '<div class="card-title-lg">GPU Utilisation</div>\n            <div class="card-sub">Last 24 hours (hourly avg)</div>',
        1
    )

    return html


def remove_metrics_section(html):
    """Remove the System Metrics section and its nav item."""

    # Remove the nav item for metrics
    # Find the metrics nav item and remove it
    html = re.sub(
        r'<button class="nav-item[^"]*" data-section="metrics"[^>]*>.*?</button>\s*',
        '',
        html,
        flags=re.DOTALL,
        count=1
    )

    # Remove the entire section-metrics div
    # Find <div id="section-metrics" and remove until the next section
    metrics_start = html.find('<div id="section-metrics"')
    if metrics_start != -1:
        # Find the next section or closing comment
        next_section = html.find('<!-- ', metrics_start + 10)
        if next_section == -1:
            next_section = html.find('<div id="section-tools"', metrics_start + 10)
        if next_section != -1:
            # Find the </div> that closes section-metrics just before next_section
            close_pos = html.rfind('</div>', metrics_start, next_section)
            if close_pos != -1:
                html = html[:metrics_start] + html[next_section:]

    # Remove the loadMetrics call from navigate
    html = html.replace("  if (sec === 'metrics')   loadMetrics();\n", "", 1)

    return html


if __name__ == "__main__":
    main()
