#!/usr/bin/env python3
"""
Fix light mode visibility issues in admin.html.

The problem: many CSS rules use hardcoded rgba(255,255,255,...) which is
invisible on white/light backgrounds. This script adds comprehensive
[data-theme="light"] overrides for all affected components.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # Insert comprehensive light mode overrides before </style>
    light_overrides = LIGHT_MODE_CSS()
    html = html.replace("</style>", light_overrides + "\n</style>", 1)

    # Fix the nav-item hover which uses rgba(255,255,255,.05)
    html = html.replace(
        ".nav-item:hover { background: rgba(255,255,255,.05); color: var(--text); }",
        ".nav-item:hover { background: var(--accent-dim); color: var(--text); }",
        1
    )

    # Fix sidebar footer status background
    html = html.replace(
        "background: rgba(255,255,255,.03);",
        "background: var(--surface2);",
    )

    # Fix the sidebar glow to use theme token
    html = html.replace(
        "background: radial-gradient(circle, rgba(129,140,248,.18) 0%, transparent 70%);",
        "background: radial-gradient(circle, var(--accent-dim) 0%, transparent 70%);",
    )

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Light mode fixes applied to {INPUT}")


def LIGHT_MODE_CSS():
    return """
/* ═══════════════════════════════════════════════════════════════════════════ */
/* LIGHT MODE COMPREHENSIVE OVERRIDES                                         */
/* ═══════════════════════════════════════════════════════════════════════════ */

[data-theme="light"] body {
  background: var(--bg);
  color: var(--text);
}

/* Sidebar */
[data-theme="light"] #sidebar {
  background: var(--bg2);
  border-right-color: var(--border);
}
[data-theme="light"] #sidebar::before { opacity: 0.3; }
[data-theme="light"] #sidebar-header { border-bottom-color: var(--border); }
[data-theme="light"] #sidebar-footer { border-top-color: var(--border); }
[data-theme="light"] .nav-item { color: var(--text2); }
[data-theme="light"] .nav-item:hover { background: var(--surface2); color: var(--text); }
[data-theme="light"] .nav-item.active { background: var(--accent-dim); color: var(--accent); }
[data-theme="light"] .nav-group-label { color: var(--text3); }
[data-theme="light"] .nova-title { color: var(--text); }
[data-theme="light"] .nova-sub { color: var(--text3); }
[data-theme="light"] .user-name-display { color: var(--text); }
[data-theme="light"] .user-role-display { color: var(--text3); }

/* Topbar */
[data-theme="light"] #topbar {
  background: var(--bg2);
  border-bottom-color: var(--border);
}
[data-theme="light"] #section-breadcrumb strong { color: var(--text); }

/* Content */
[data-theme="light"] #content { background: var(--bg); }

/* Cards */
[data-theme="light"] .card {
  background: var(--surface);
  border-color: var(--border);
  box-shadow: var(--shadow-card);
}
[data-theme="light"] .card:hover { border-color: var(--border2); box-shadow: var(--shadow); }
[data-theme="light"] .card-title { color: var(--text2); }

/* Stat cards */
[data-theme="light"] .stat-card {
  background: var(--surface);
  border-color: var(--border);
  box-shadow: var(--shadow-card);
}
[data-theme="light"] .stat-card:hover { border-color: var(--border2); box-shadow: var(--shadow); }
[data-theme="light"] .stat-label { color: var(--text3); }
[data-theme="light"] .stat-value { color: var(--text); }

/* Stat icon backgrounds — need solid colors in light mode */
[data-theme="light"] .stat-icon.green  { background: var(--green-dim); color: var(--green); }
[data-theme="light"] .stat-icon.red    { background: var(--red-dim); color: var(--red); }
[data-theme="light"] .stat-icon.yellow { background: var(--yellow-dim); color: var(--yellow); }
[data-theme="light"] .stat-icon.accent { background: var(--accent-dim); color: var(--accent); }
[data-theme="light"] .stat-icon.cyan   { background: var(--cyan-dim); color: var(--cyan); }

/* Buttons */
[data-theme="light"] .btn-outline {
  border-color: var(--border2);
  color: var(--text2);
}
[data-theme="light"] .btn-outline:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-dim);
}
[data-theme="light"] .btn-ghost { color: var(--text2); }
[data-theme="light"] .btn-ghost:hover:not(:disabled) {
  color: var(--text);
  background: var(--surface2);
}

/* Forms */
[data-theme="light"] input[type=text],
[data-theme="light"] input[type=password],
[data-theme="light"] input[type=url],
[data-theme="light"] select {
  background: var(--surface2);
  border-color: var(--border2);
  color: var(--text);
}
[data-theme="light"] input[type=text]:focus,
[data-theme="light"] input[type=password]:focus,
[data-theme="light"] input[type=url]:focus,
[data-theme="light"] select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-dim);
}
[data-theme="light"] textarea {
  background: var(--surface2);
  border-color: var(--border2);
  color: var(--text);
}
[data-theme="light"] textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-dim);
}
[data-theme="light"] label { color: var(--text2); }
[data-theme="light"] .input-reveal button { color: var(--text3); }
[data-theme="light"] .input-reveal button:hover { color: var(--text); }

/* Tables */
[data-theme="light"] thead tr { border-bottom-color: var(--border); }
[data-theme="light"] th { color: var(--text3); }
[data-theme="light"] td { color: var(--text2); border-bottom-color: var(--border); }
[data-theme="light"] tr:hover td { background: var(--accent-dim); color: var(--text); }

/* Badges */
[data-theme="light"] .badge-accent { background: var(--accent-dim); color: var(--accent-text); }
[data-theme="light"] .badge-green { background: var(--green-dim); color: var(--green); }
[data-theme="light"] .badge-red { background: var(--red-dim); color: var(--red); }
[data-theme="light"] .badge-muted { background: var(--surface2); color: var(--text3); }
[data-theme="light"] .role-badge.admin { background: var(--accent-dim); color: var(--accent-text); }
[data-theme="light"] .role-badge.viewer { background: var(--surface2); color: var(--text3); }

/* Collapsible groups */
[data-theme="light"] .collapsible-group { border-color: var(--border); }
[data-theme="light"] .collapsible-header {
  background: var(--surface);
  color: var(--text);
}
[data-theme="light"] .collapsible-header:hover { background: var(--surface2); }
[data-theme="light"] .collapsible-body { border-top-color: var(--border); }
[data-theme="light"] .collapsible-chevron { color: var(--text3); }

/* Log viewer */
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
[data-theme="light"] .log-line.lvl-debug { color: var(--text3); }

/* Stream toolbar */
[data-theme="light"] .stream-toolbar { border-bottom-color: var(--border); }
[data-theme="light"] .stream-title { color: var(--text); }
[data-theme="light"] .stream-sub { color: var(--text3); }

/* Filter chips */
[data-theme="light"] .filter-chip {
  border-color: var(--border2);
  color: var(--text3);
}
[data-theme="light"] .filter-chip:hover { border-color: var(--accent); color: var(--accent); }
[data-theme="light"] .filter-chip.active { background: var(--accent-dim); border-color: var(--accent); color: var(--accent); }
[data-theme="light"] .filter-bar { border-bottom-color: var(--border); }

/* Toast */
[data-theme="light"] .toast-item {
  background: var(--surface);
  border-color: var(--border);
  color: var(--text);
  box-shadow: var(--shadow-lg);
}
[data-theme="light"] .toast-item .toast-close { color: var(--text3); }

/* Gauge bars */
[data-theme="light"] .gauge-bar-track { background: var(--surface2); }
[data-theme="light"] .gauge-card { background: var(--surface); border-color: var(--border); }
[data-theme="light"] .gauge-label { color: var(--text3); }
[data-theme="light"] .gauge-val { color: var(--text); }
[data-theme="light"] .gauge-sub { color: var(--text3); }

/* Divider */
[data-theme="light"] .divider { border-top-color: var(--border); }

/* Page headers */
[data-theme="light"] .page-title { color: var(--text); }
[data-theme="light"] .page-sub { color: var(--text3); }

/* User rows */
[data-theme="light"] .user-row { border-bottom-color: var(--border); }
[data-theme="light"] .user-name { color: var(--text); }

/* Metric cards */
[data-theme="light"] .metric-card { background: var(--surface); border-color: var(--border); }
[data-theme="light"] .metric-label { color: var(--text3); }
[data-theme="light"] .metric-value { color: var(--text); }

/* Period buttons */
[data-theme="light"] .period-btn { border-color: var(--border2); color: var(--text3); }
[data-theme="light"] .period-btn:hover { border-color: var(--accent); color: var(--accent); }
[data-theme="light"] .period-btn.active { background: var(--accent-dim); border-color: var(--accent); color: var(--accent); }

/* Segmented control */
[data-theme="light"] .segmented-control { background: var(--surface2); }
[data-theme="light"] .segmented-control .segment-btn { color: var(--text3); }
[data-theme="light"] .segmented-control .segment-btn.active { color: var(--accent); background: var(--surface); box-shadow: var(--shadow-card); }

/* Empty state */
[data-theme="light"] .empty-state { color: var(--text3); }
[data-theme="light"] .empty-state-title { color: var(--text2); }

/* Announce preview */
[data-theme="light"] .announce-preview { background: var(--surface2); border-color: var(--border); color: var(--text2); }

/* Avatar cards */
[data-theme="light"] .avatar-card { background: var(--surface2); }
[data-theme="light"] .avatar-card:hover { border-color: var(--border2); background: var(--surface3); }
[data-theme="light"] .avatar-card.active { border-color: var(--accent); }
[data-theme="light"] .avatar-card-name { color: var(--text); }

/* Skin swatches */
[data-theme="light"] .skin-swatch.selected { border-color: var(--accent); }

/* Find Anything / Motion section */
[data-theme="light"] .fa-search-input {
  background: var(--surface2);
  border-color: var(--border2);
  color: var(--text);
}
[data-theme="light"] .fa-search-input::placeholder { color: var(--text3); }
[data-theme="light"] .fa-search-input:focus { border-color: var(--accent); background: var(--surface); }
[data-theme="light"] .fa-reset-btn { border-color: var(--border2); background: var(--surface2); color: var(--text2); }
[data-theme="light"] .fa-reset-btn:hover { background: var(--surface3); color: var(--text); }
[data-theme="light"] .motion-camera-item { border-color: var(--border2); background: var(--surface2); color: var(--text2); }
[data-theme="light"] .motion-camera-item:hover { border-color: var(--border2); background: var(--surface3); color: var(--text); }
[data-theme="light"] .motion-camera-item.active { border-color: var(--accent); background: var(--accent-dim); color: var(--accent); }
[data-theme="light"] .motion-camera-count { color: var(--text3); }
[data-theme="light"] .fa-tabs { background: var(--surface2); }
[data-theme="light"] .fa-tab { color: var(--text3); }
[data-theme="light"] .fa-tab.active { background: var(--surface); color: var(--text); box-shadow: var(--shadow-card); }
[data-theme="light"] .fa-count-badge { background: var(--surface2); border-color: var(--border); color: var(--text2); }
[data-theme="light"] .fa-results-sub { color: var(--text3); }
[data-theme="light"] .fa-section-label { color: var(--text3); }

/* Responsive sidebar overlay */
[data-theme="light"] #sidebar-overlay.show { background: rgba(0,0,0,.3); }

/* Loading skeleton */
[data-theme="light"] .loading-skeleton {
  background: linear-gradient(90deg, var(--surface2) 25%, var(--surface3) 50%, var(--surface2) 75%);
  background-size: 200% 100%;
}

/* Log search */
[data-theme="light"] .log-search-input {
  background: var(--surface2);
  border-color: var(--border2);
  color: var(--text);
}
[data-theme="light"] .log-counter { background: var(--surface2); color: var(--text3); }
"""


if __name__ == "__main__":
    main()
