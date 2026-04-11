#!/usr/bin/env python3
"""
Fix motion card text hierarchy — use proper text2/text3 tokens for
secondary content, and ensure full light mode visibility.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # Fix the base motion card text colors — use proper hierarchy
    replacements = [
        # Time should be muted
        (".motion-card-time { font-size: 11px; color: var(--text); margin-top: 2px; }",
         ".motion-card-time { font-size: 11px; color: var(--text3); margin-top: 2px; }"),

        # ID should be very muted
        (".motion-card-id { font-size: 10px; color: var(--text); letter-spacing: .04em; text-transform: uppercase; white-space: nowrap; padding-top: 2px; }",
         ".motion-card-id { font-size: 10px; color: var(--text3); letter-spacing: .04em; text-transform: uppercase; white-space: nowrap; padding-top: 2px; }"),

        # Camera name should be muted
        (".motion-card-camera { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--text); margin-top: 2px; }",
         ".motion-card-camera { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--text3); margin-top: 2px; }"),

        # Description should be secondary
        (".motion-card-desc {\n  font-size: 12px;\n  color: var(--text);",
         ".motion-card-desc {\n  font-size: 12px;\n  color: var(--text2);"),

        # Note should be muted
        (".motion-card-note { font-size: 11px; color: var(--text); }",
         ".motion-card-note { font-size: 11px; color: var(--text3); }"),

        # Empty state
        (".motion-empty {\n  padding: 40px 24px;\n  text-align: center;\n  color: var(--text);",
         ".motion-empty {\n  padding: 40px 24px;\n  text-align: center;\n  color: var(--text3);"),

        # Chip default color
        (".motion-chip {\n  display: inline-flex;\n  align-items: center;\n  padding: 3px 8px;\n  border-radius: 6px;\n  border: 1px solid var(--border2);\n  background: var(--surface2);\n  color: var(--text);",
         ".motion-chip {\n  display: inline-flex;\n  align-items: center;\n  padding: 3px 8px;\n  border-radius: 6px;\n  border: 1px solid var(--border2);\n  background: var(--surface2);\n  color: var(--text2);"),

        # Fallback text
        (".motion-card-fallback {\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  color: var(--text);",
         ".motion-card-fallback {\n  display: flex;\n  align-items: center;\n  justify-content: center;\n  color: var(--text3);"),
    ]

    for old, new in replacements:
        if old in html:
            html = html.replace(old, new, 1)
            print(f"Fixed: {old[:50]}...")
        else:
            print(f"SKIP (not found): {old[:50]}...")

    # Add light mode overrides for motion card badge overlays
    # (these sit on top of video thumbnails so they need dark bg in both modes)
    extra_light = """
/* Motion card badge overlays — keep dark in light mode (on top of video) */
[data-theme="light"] .motion-card-badge,
[data-theme="light"] .motion-card-age {
  background: rgba(0,0,0,.6);
  color: #fff;
  border-color: rgba(255,255,255,.15);
}
[data-theme="light"] .motion-card-overlay {
  background: linear-gradient(180deg, rgba(0,0,0,.5) 0%, transparent 100%);
}

/* Motion card body in light mode */
[data-theme="light"] .motion-card-body { background: var(--surface); }
[data-theme="light"] .motion-card-title { color: var(--text); }
[data-theme="light"] .motion-card-time { color: var(--text3); }
[data-theme="light"] .motion-card-id { color: var(--text3); }
[data-theme="light"] .motion-card-camera { color: var(--text3); }
[data-theme="light"] .motion-card-desc { color: var(--text2); }
[data-theme="light"] .motion-card-note { color: var(--text3); }
[data-theme="light"] .motion-card-media { border-bottom-color: var(--border); }

/* Motion chips in light mode */
[data-theme="light"] .motion-chip { background: var(--surface2); color: var(--text2); border-color: var(--border); }
[data-theme="light"] .motion-chip.search { background: rgba(108,92,231,.08); color: #5a4bd1; border-color: rgba(108,92,231,.2); }
[data-theme="light"] .motion-chip.good { background: rgba(0,168,132,.08); color: #0a7558; border-color: rgba(0,168,132,.18); }
[data-theme="light"] .motion-chip.info { background: rgba(108,92,231,.08); color: #5a4bd1; border-color: rgba(108,92,231,.15); }

/* Motion empty state in light mode */
[data-theme="light"] .motion-empty { color: var(--text3); border-color: var(--border); }

/* Motion card actions */
[data-theme="light"] .motion-card-actions .btn { color: var(--text2); }
[data-theme="light"] .motion-card-actions .btn:hover { color: var(--accent-text); }
"""
    html = html.replace("</style>", extra_light + "\n</style>", 1)

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Output: {INPUT}")


if __name__ == "__main__":
    main()
