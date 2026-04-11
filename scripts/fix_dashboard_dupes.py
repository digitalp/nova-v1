#!/usr/bin/env python3
"""
Fix: Remove duplicate gauge row + chart grid at bottom of dashboard,
and fix the system resource gauge values not showing.
"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # 1. Remove the old "System Resources" label + dash-sys-grid + old chart-grid
    #    These are the duplicates at the bottom of the dashboard section.
    #    The new charts are in the "Performance Overview" section above.

    # Find and remove: "System Resources" label
    html = html.replace(
        '      <div style="font-size:var(--text-sm);font-weight:var(--fw-semibold);color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:var(--sp-3);">System Resources</div>\n',
        '',
        1
    )

    # Find and remove the entire dash-sys-grid block (the gauge cards with CPU/RAM/Disk/GPU)
    dash_sys_start = html.find('<div class="dash-sys-grid">')
    if dash_sys_start != -1:
        # Find the closing </div> of dash-sys-grid
        # Count nested divs to find the right closing tag
        depth = 0
        pos = dash_sys_start
        end_pos = -1
        while pos < len(html):
            if html[pos:pos+4] == '<div':
                depth += 1
            elif html[pos:pos+6] == '</div>':
                depth -= 1
                if depth == 0:
                    end_pos = pos + 6
                    break
            pos += 1
        if end_pos != -1:
            # Also remove any trailing whitespace/newlines
            while end_pos < len(html) and html[end_pos] in '\n\r ':
                end_pos += 1
            html = html[:dash_sys_start] + html[end_pos:]
            print(f"Removed dash-sys-grid block ({end_pos - dash_sys_start} chars)")

    # 2. Remove the old chart-grid that was part of the metrics section
    #    (it may have been left behind when we removed section-metrics)
    #    Look for chart-grid inside section-dashboard that has chart-cpu-ram / chart-gpu canvases
    old_chart_block = html.find('<canvas id="chart-cpu-ram">')
    if old_chart_block != -1:
        # Find the enclosing chart-grid div
        search_back = html.rfind('<div class="chart-grid">', 0, old_chart_block)
        if search_back != -1:
            # Find the closing </div> of chart-grid
            depth = 0
            pos = search_back
            end_pos = -1
            while pos < len(html):
                if html[pos:pos+4] == '<div':
                    depth += 1
                elif html[pos:pos+6] == '</div>':
                    depth -= 1
                    if depth == 0:
                        end_pos = pos + 6
                        break
                pos += 1
            if end_pos != -1:
                while end_pos < len(html) and html[end_pos] in '\n\r ':
                    end_pos += 1
                html = html[:search_back] + html[end_pos:]
                print(f"Removed old chart-grid block ({end_pos - search_back} chars)")

    # 3. Fix the _applyGaugeDash function — it references dash-cpu, dash-ram etc.
    #    but those elements are in the gauge cards we just removed.
    #    The values should show in the stat-card gauge bars instead.
    #    Actually, the stat-card gauge bars (dash-cpu-bar etc.) are ALSO in the
    #    removed dash-sys-grid. We need to keep the gauge display somewhere.
    #    
    #    Solution: The "Performance Overview" charts already show CPU/RAM/GPU.
    #    But we still want the numeric values visible. Let's add them as
    #    small stat values inside the chart card headers.

    # Update the chart card headers to include live values
    html = html.replace(
        """<div class="card-title-lg">CPU & RAM History</div>
            <div class="card-sub">Last 24 hours (hourly avg)</div>""",
        """<div class="card-title-lg">CPU & RAM History</div>
            <div class="card-sub">Last 24 hours (hourly avg)</div>
          </div>
          <div style="display:flex;gap:var(--sp-4);align-items:center;">
            <div style="text-align:right;"><div class="text-xs text-muted">CPU</div><div id="dash-cpu" class="text-lg font-bold" style="color:var(--cyan);">—</div></div>
            <div style="text-align:right;"><div class="text-xs text-muted">RAM</div><div id="dash-ram" class="text-lg font-bold" style="color:var(--accent);">—</div></div>
            <div style="text-align:right;"><div class="text-xs text-muted">Disk</div><div id="dash-disk" class="text-lg font-bold" style="color:var(--yellow);">—</div></div>""",
        1
    )

    html = html.replace(
        """<div class="card-title-lg">GPU Utilisation</div>
            <div class="card-sub">Last 24 hours (hourly avg)</div>""",
        """<div class="card-title-lg">GPU Utilisation</div>
            <div class="card-sub">Last 24 hours (hourly avg)</div>
          </div>
          <div style="text-align:right;"><div class="text-xs text-muted">GPU</div><div id="dash-gpu" class="text-lg font-bold" style="color:var(--green);">—</div>""",
        1
    )

    # 4. Remove references to dash-cpu-bar, dash-ram-bar etc. from _applyGaugeDash
    #    since those elements no longer exist
    html = html.replace(
        """  function _setBar(barId, pct) {
    const bar = _b(barId);
    if (!bar) return;
    const clamped = Math.max(0, Math.min(100, pct));
    bar.style.width = clamped.toFixed(1) + '%';
    bar.className = 'gauge-bar-fill ' + getGaugeColorClass(clamped);
  }""",
        """  function _setBar(barId, pct) {
    const bar = _b(barId);
    if (!bar) return;
    const clamped = Math.max(0, Math.min(100, pct));
    bar.style.width = clamped.toFixed(1) + '%';
    bar.className = 'gauge-bar-fill ' + getGaugeColorClass(clamped);
  }
  // Note: dash-*-bar elements may not exist if gauges are removed from dashboard""",
        1
    )

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Output: {INPUT}")


if __name__ == "__main__":
    main()
