#!/usr/bin/env python3
"""
Fix: Remove orphaned gauge-row, chart-grid, and System Resources elements
that appear outside the dashboard section, causing them to show on every page.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")
    original = len(html)

    # Find ALL occurrences of "System Resources" label and remove them
    count = html.count('System Resources</div>')
    html = html.replace(
        '      <div style="font-size:var(--text-sm);font-weight:var(--fw-semibold);color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:var(--sp-3);">System Resources</div>\n',
        ''
    )
    print(f"Removed {count} 'System Resources' labels")

    # Remove ALL dash-sys-grid blocks (the gauge cards)
    while '<div class="dash-sys-grid">' in html:
        start = html.find('<div class="dash-sys-grid">')
        if start == -1:
            break
        depth = 0
        pos = start
        end = -1
        while pos < len(html):
            if html[pos:pos+4] == '<div':
                depth += 1
            elif html[pos:pos+6] == '</div>':
                depth -= 1
                if depth == 0:
                    end = pos + 6
                    break
            pos += 1
        if end == -1:
            break
        # Also eat trailing whitespace
        while end < len(html) and html[end] in '\n\r \t':
            end += 1
        print(f"Removing dash-sys-grid at pos {start}-{end}")
        html = html[:start] + html[end:]

    # Remove any orphaned chart-grid blocks with chart-cpu-ram / chart-gpu canvases
    # (these are from the old metrics section)
    while '<canvas id="chart-cpu-ram">' in html:
        canvas_pos = html.find('<canvas id="chart-cpu-ram">')
        # Find the enclosing chart-grid
        grid_start = html.rfind('<div class="chart-grid">', 0, canvas_pos)
        if grid_start == -1:
            break
        depth = 0
        pos = grid_start
        end = -1
        while pos < len(html):
            if html[pos:pos+4] == '<div':
                depth += 1
            elif html[pos:pos+6] == '</div>':
                depth -= 1
                if depth == 0:
                    end = pos + 6
                    break
            pos += 1
        if end == -1:
            break
        while end < len(html) and html[end] in '\n\r \t':
            end += 1
        print(f"Removing old chart-grid at pos {grid_start}-{end}")
        html = html[:grid_start] + html[end:]

    # Remove any orphaned gauge-row blocks (from old metrics section)
    while True:
        start = html.find('<div class="gauge-row">')
        if start == -1:
            break
        # Check if this is inside section-metrics (which should be gone)
        # or floating free — remove it either way
        depth = 0
        pos = start
        end = -1
        while pos < len(html):
            if html[pos:pos+4] == '<div':
                depth += 1
            elif html[pos:pos+6] == '</div>':
                depth -= 1
                if depth == 0:
                    end = pos + 6
                    break
            pos += 1
        if end == -1:
            break
        while end < len(html) and html[end] in '\n\r \t':
            end += 1
        print(f"Removing gauge-row at pos {start}-{end}")
        html = html[:start] + html[end:]

    # Verify section nesting — count opening/closing divs in dashboard section
    dash_start = html.find('<div id="section-dashboard"')
    config_start = html.find('<div id="section-config"')
    if dash_start != -1 and config_start != -1:
        dash_html = html[dash_start:config_start]
        opens = dash_html.count('<div')
        closes = dash_html.count('</div>')
        print(f"Dashboard section: {opens} opens, {closes} closes (should be equal)")
        if opens != closes:
            print(f"WARNING: Mismatched divs! Difference: {opens - closes}")

    removed = original - len(html)
    print(f"Done. Removed {removed} chars. Output: {INPUT}")
    INPUT.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
