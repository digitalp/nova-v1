#!/usr/bin/env python3
"""
Comprehensive Find Anything light mode fix + UniFi Protect-inspired styling.

Fixes ALL remaining hardcoded colors in the motion/FA section and adds
complete light mode overrides for every FA/motion component.
"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # 1. Replace remaining hardcoded colors in motion/FA CSS with tokens
    html = fix_hardcoded_colors(html)

    # 2. Add comprehensive light mode overrides for ALL motion/FA components
    light_css = FA_LIGHT_MODE_CSS()
    html = html.replace("</style>", light_css + "\n</style>", 1)

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Output: {INPUT}")


def fix_hardcoded_colors(html):
    """Replace hardcoded hex/rgba colors in motion section with theme tokens."""

    # Motion card colors
    html = html.replace(
        ".motion-insight-chip strong { color: #7dd3fc; font-weight: 700; }",
        ".motion-insight-chip strong { color: var(--cyan); font-weight: 700; }",
    )
    html = html.replace(
        ".motion-mark { padding: 0 3px; border-radius: 3px; background: rgba(10,132,255,.22); color: #70b8ff; }",
        ".motion-mark { padding: 0 3px; border-radius: 3px; background: var(--accent-dim); color: var(--accent-text); }",
    )

    # Fix any remaining #91a3ba type colors
    html = html.replace("color:#91a3ba;", "color:var(--text2);")

    return html


def FA_LIGHT_MODE_CSS():
    return """
/* ═══════════════════════════════════════════════════════════════════════════ */
/* FIND ANYTHING — COMPLETE LIGHT MODE (UniFi Protect-inspired)               */
/* ═══════════════════════════════════════════════════════════════════════════ */

/* Timeline */
[data-theme="light"] .fa-timeline-head { color: var(--text); }
[data-theme="light"] .fa-timeline-note { color: var(--text3); }
[data-theme="light"] .fa-timeline-bar { background: var(--surface2); }

/* Motion cards */
[data-theme="light"] .motion-card {
  background: var(--surface);
  border-color: var(--border);
  box-shadow: var(--shadow-card);
}
[data-theme="light"] .motion-card:hover {
  border-color: var(--border2);
  box-shadow: var(--shadow);
}
[data-theme="light"] .motion-card-desc { color: var(--text); }
[data-theme="light"] .motion-card-time { color: var(--text3); }
[data-theme="light"] .motion-card-id { color: var(--text3); }
[data-theme="light"] .motion-card-camera { color: var(--text3); }
[data-theme="light"] .motion-card-note { color: var(--text2); }
[data-theme="light"] .motion-card-badge { color: var(--text); }

/* Motion activity bar */
[data-theme="light"] .motion-activity-bar { background: var(--surface2); }
[data-theme="light"] .motion-activity-label { color: var(--text3); }

/* Motion groups */
[data-theme="light"] .motion-group-label { color: var(--text); }
[data-theme="light"] .motion-group-count { color: var(--text3); }
[data-theme="light"] .motion-group-line { background: var(--border); }

/* Motion history items */
[data-theme="light"] .motion-history-item {
  background: var(--surface);
  border-color: var(--border);
}
[data-theme="light"] .motion-history-item:hover {
  border-color: var(--accent);
  background: var(--accent-dim);
}
[data-theme="light"] .motion-history-title { color: var(--text); }
[data-theme="light"] .motion-history-time { color: var(--text3); }
[data-theme="light"] .motion-history-desc { color: var(--text2); }
[data-theme="light"] .motion-history-note-inline { color: var(--text2); border-left-color: var(--accent); }
[data-theme="light"] .motion-history-note-inline strong { color: var(--text); }

/* Motion modal */
[data-theme="light"] .motion-modal-overlay { background: rgba(0,0,0,.4); }
[data-theme="light"] .motion-modal {
  background: var(--surface);
  border-color: var(--border);
  box-shadow: var(--shadow-lg);
}
[data-theme="light"] .motion-modal-title { color: var(--text); }
[data-theme="light"] .motion-modal-close { color: var(--text3); }
[data-theme="light"] .motion-modal-close:hover { background: var(--surface2); color: var(--text); }
[data-theme="light"] .motion-modal-fallback-label { color: var(--text3); }
[data-theme="light"] .motion-modal-fallback-sub { color: var(--text2); }

/* Motion meta blocks */
[data-theme="light"] .motion-meta-block { border-bottom-color: var(--border); }
[data-theme="light"] .motion-meta-label { color: var(--text3); }
[data-theme="light"] .motion-meta-value { color: var(--text); }

/* Motion sidebar */
[data-theme="light"] .motion-modal-sidebar { border-right-color: var(--border); }
[data-theme="light"] .motion-side-note { border-top-color: var(--border); color: var(--text2); }

/* Motion insight chips */
[data-theme="light"] .motion-insight-chip { background: var(--surface2); color: var(--text2); }
[data-theme="light"] .motion-insight-chip strong { color: var(--accent-text); }

/* Motion mark (search highlight) */
[data-theme="light"] .motion-mark { background: rgba(108,92,231,.12); color: var(--accent-text); }

/* Event history action buttons in FA */
[data-theme="light"] .motion-action-btn {
  background: var(--surface2);
  color: var(--text2);
  border-color: var(--border);
}
[data-theme="light"] .motion-action-btn:hover {
  background: var(--accent-dim);
  color: var(--accent-text);
  border-color: var(--accent);
}

/* Open loop badges */
[data-theme="light"] .motion-card-badge.active { background: var(--accent-dim); color: var(--accent-text); }
[data-theme="light"] .motion-card-badge.acknowledged { background: var(--yellow-dim); color: #8b6914; }
[data-theme="light"] .motion-card-badge.resolved { background: var(--green-dim); color: #0a7558; }
[data-theme="light"] .motion-card-badge.dismissed { background: var(--surface2); color: var(--text3); }
[data-theme="light"] .motion-card-badge.review { background: var(--red-dim); color: #b83232; }
[data-theme="light"] .motion-card-badge.review::before { background: var(--text3); }

/* Empty state in FA */
[data-theme="light"] .fa-empty-state { color: var(--text3); }
[data-theme="light"] .fa-empty-icon { color: var(--text3); }

/* Chip colors */
[data-theme="light"] .motion-chip { background: var(--surface2); color: var(--text2); border-color: var(--border); }
[data-theme="light"] .motion-chip:hover { background: var(--surface3); color: var(--text); }
[data-theme="light"] .motion-chip.active { background: var(--accent-dim); color: var(--accent-text); border-color: var(--accent); }

/* Live indicator */
[data-theme="light"] .live-indicator { background: var(--green-dim); color: var(--green); }
[data-theme="light"] .live-dot { background: var(--green); }

/* Workflow summary */
[data-theme="light"] .fa-workflow-card { background: var(--surface); border-color: var(--border); }
[data-theme="light"] .fa-workflow-card:hover { border-color: var(--border2); }
[data-theme="light"] .fa-workflow-title { color: var(--text); }
[data-theme="light"] .fa-workflow-sub { color: var(--text2); }
"""


if __name__ == "__main__":
    main()
