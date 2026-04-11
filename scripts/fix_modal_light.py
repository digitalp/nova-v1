#!/usr/bin/env python3
"""
Fix motion modal — proper text hierarchy + light mode visibility.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # Fix base modal text hierarchy (dark mode too)
    fixes = [
        # Meta labels should be muted
        (".motion-meta-label { font-size: 10px; font-weight: 600; color: var(--text); letter-spacing:",
         ".motion-meta-label { font-size: 10px; font-weight: 600; color: var(--text3); letter-spacing:"),

        # Meta keys should be muted
        (".motion-meta-key { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--text); }",
         ".motion-meta-key { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--text3); }"),

        # Meta values should be secondary
        (".motion-meta-value { font-size: 13px; color: var(--text); line-height: 1.55; }",
         ".motion-meta-value { font-size: 13px; color: var(--text2); line-height: 1.55; }"),

        # Fallback sub text
        (".motion-modal-fallback-sub { font-size: 13px; color: var(--text); line-height: 1.5;",
         ".motion-modal-fallback-sub { font-size: 13px; color: var(--text2); line-height: 1.5;"),

        # Fallback label
        (".motion-modal-fallback-label { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--text); font-weight: 600;",
         ".motion-modal-fallback-label { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--text3); font-weight: 600;"),
    ]

    for old, new in fixes:
        if old in html:
            html = html.replace(old, new, 1)
            print(f"Fixed: {old[:60]}...")
        else:
            print(f"SKIP: {old[:60]}...")

    # Add comprehensive light mode overrides for the modal
    light_css = """
/* ═══════════════════════════════════════════════════════════════════════════ */
/* MOTION MODAL — LIGHT MODE                                                  */
/* ═══════════════════════════════════════════════════════════════════════════ */

[data-theme="light"] .motion-modal { background: rgba(0,0,0,.35); }

[data-theme="light"] .motion-modal-card {
  background: var(--surface);
  border-color: var(--border);
  box-shadow: 0 24px 80px rgba(0,0,0,.15);
}

[data-theme="light"] .motion-modal-head {
  border-bottom-color: var(--border);
}
[data-theme="light"] .motion-modal-title { color: var(--text); }
[data-theme="light"] .motion-modal-close {
  color: var(--text3);
  border-color: var(--border);
}
[data-theme="light"] .motion-modal-close:hover {
  background: var(--surface2);
  color: var(--text);
}

/* Modal sidebar */
[data-theme="light"] .motion-modal-side {
  background: var(--surface);
  border-left: 1px solid var(--border);
}

/* Meta blocks */
[data-theme="light"] .motion-meta-block { border-bottom-color: var(--border); }
[data-theme="light"] .motion-meta-label { color: var(--text3); }
[data-theme="light"] .motion-meta-value { color: var(--text); }
[data-theme="light"] .motion-meta-key { color: var(--text3); }
[data-theme="light"] .motion-meta-row { border-bottom-color: var(--border); }
[data-theme="light"] .motion-meta-row .motion-meta-value { color: var(--text2); }

/* Navigation buttons */
[data-theme="light"] .motion-modal-nav .btn {
  background: var(--surface2);
  color: var(--text2);
  border-color: var(--border);
}
[data-theme="light"] .motion-modal-nav .btn:hover {
  background: var(--accent-dim);
  color: var(--accent-text);
  border-color: var(--accent);
}

/* Strip chips in modal */
[data-theme="light"] .motion-modal-strip .motion-chip {
  background: var(--surface2);
  color: var(--text2);
  border-color: var(--border);
}

/* Fallback state */
[data-theme="light"] .motion-modal-fallback { background: var(--surface2); }
[data-theme="light"] .motion-modal-fallback-label { color: var(--text3); }
[data-theme="light"] .motion-modal-fallback-title { color: var(--text); }
[data-theme="light"] .motion-modal-fallback-sub { color: var(--text2); }

/* Side note */
[data-theme="light"] .motion-side-note { border-top-color: var(--border); color: var(--text2); }

/* Modal timestamp */
[data-theme="light"] .motion-modal-head .text-muted,
[data-theme="light"] .motion-modal-head .text-sub { color: var(--text3); }
"""
    html = html.replace("</style>", light_css + "\n</style>", 1)

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Output: {INPUT}")


if __name__ == "__main__":
    main()
