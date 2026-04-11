#!/usr/bin/env python3
"""
Fix Find Anything section light mode — hardcoded white/rgba(255,255,255,...) colors.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # Replace all hardcoded white colors in .fa-* classes with theme tokens
    replacements = [
        # Search input
        ('.fa-search-input {\n  width: 100%;\n  height: 52px;', None),  # skip, handled below
        ('color: #fff;', None),  # too broad, handle specifically

        # Search icon
        ('color: rgba(255,255,255,.30);', 'color: var(--text3);'),

        # Search input text color
    ]

    # Targeted replacements in the FA section CSS
    # Search input
    html = html.replace(
        """.fa-search-input {
  width: 100%;
  height: 52px;
  padding: 0 120px 0 52px;
  border-radius: 16px;
  border: 1px solid rgba(255,255,255,.10);
  background: rgba(255,255,255,.065);
  color: #fff;""",
        """.fa-search-input {
  width: 100%;
  height: 52px;
  padding: 0 120px 0 52px;
  border-radius: 16px;
  border: 1px solid var(--border2);
  background: var(--surface2);
  color: var(--text);""",
        1
    )

    html = html.replace(
        ".fa-search-input::placeholder { color: rgba(255,255,255,.25); }",
        ".fa-search-input::placeholder { color: var(--text3); }",
        1
    )

    html = html.replace(
        """.fa-search-input:focus {
  border-color: rgba(10,132,255,.55);
  background: rgba(255,255,255,.09);
}""",
        """.fa-search-input:focus {
  border-color: var(--accent);
  background: var(--surface);
}""",
        1
    )

    # Search icon
    html = html.replace(
        """.fa-search-icon {
  position: absolute;
  left: 18px;
  color: rgba(255,255,255,.30);""",
        """.fa-search-icon {
  position: absolute;
  left: 18px;
  color: var(--text3);""",
        1
    )

    # Reset button
    html = html.replace(
        """.fa-reset-btn {
  flex-shrink: 0;
  padding: 0 16px;
  height: 52px;
  border-radius: 14px;
  border: 1px solid rgba(255,255,255,.10);
  background: rgba(255,255,255,.05);
  color: rgba(255,255,255,.55);""",
        """.fa-reset-btn {
  flex-shrink: 0;
  padding: 0 16px;
  height: 52px;
  border-radius: 14px;
  border: 1px solid var(--border2);
  background: var(--surface2);
  color: var(--text2);""",
        1
    )

    html = html.replace(
        ".fa-reset-btn:hover { background: rgba(255,255,255,.09); color: #fff; }",
        ".fa-reset-btn:hover { background: var(--surface3); color: var(--text); }",
        1
    )

    # Camera filter pills
    html = html.replace(
        """.motion-camera-item {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 14px;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,.11);
  background: rgba(255,255,255,.055);
  color: rgba(255,255,255,.65);""",
        """.motion-camera-item {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 14px;
  border-radius: 999px;
  border: 1px solid var(--border2);
  background: var(--surface2);
  color: var(--text2);""",
        1
    )

    html = html.replace(
        ".motion-camera-item:hover { border-color: rgba(255,255,255,.22); background: rgba(255,255,255,.09); color: #fff; }",
        ".motion-camera-item:hover { border-color: var(--border2); background: var(--surface3); color: var(--text); }",
        1
    )

    html = html.replace(
        ".motion-camera-item.active { border-color: #0A84FF; background: rgba(10,132,255,.18); color: #fff; font-weight: 600; }",
        ".motion-camera-item.active { border-color: var(--accent); background: var(--accent-dim); color: var(--accent-text); font-weight: 600; }",
        1
    )

    html = html.replace(
        ".motion-camera-count { color: rgba(255,255,255,.40); font-size: 12px; font-weight: 600; }",
        ".motion-camera-count { color: var(--text3); font-size: 12px; font-weight: 600; }",
        1
    )

    html = html.replace(
        ".motion-camera-item.active .motion-camera-count { color: rgba(255,255,255,.65); }",
        ".motion-camera-item.active .motion-camera-count { color: var(--accent-text); }",
        1
    )

    # Tabs
    html = html.replace(
        """.fa-tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  background: rgba(255,255,255,.06);""",
        """.fa-tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  background: var(--surface2);""",
        1
    )

    html = html.replace(
        """.fa-tab {
  padding: 7px 20px;
  border-radius: 10px;
  border: none;
  background: transparent;
  color: rgba(255,255,255,.50);""",
        """.fa-tab {
  padding: 7px 20px;
  border-radius: 10px;
  border: none;
  background: transparent;
  color: var(--text3);""",
        1
    )

    html = html.replace(
        ".fa-tab.active { background: rgba(255,255,255,.12); color: #fff; font-weight: 600; }",
        ".fa-tab.active { background: var(--surface); color: var(--text); font-weight: 600; box-shadow: var(--shadow-card); }",
        1
    )

    # Count badge
    html = html.replace(
        """.fa-count-badge {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 12px;
  border-radius: 999px;
  background: rgba(255,255,255,.06);
  border: 1px solid rgba(255,255,255,.10);
  font-size: 12px;
  font-weight: 600;
  color: rgba(255,255,255,.70);""",
        """.fa-count-badge {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 12px;
  border-radius: 999px;
  background: var(--surface2);
  border: 1px solid var(--border);
  font-size: 12px;
  font-weight: 600;
  color: var(--text2);""",
        1
    )

    # Results sub
    html = html.replace(
        """.fa-results-sub {
  font-size: 12px;
  color: rgba(255,255,255,.30);""",
        """.fa-results-sub {
  font-size: 12px;
  color: var(--text3);""",
        1
    )

    # Section label
    html = html.replace(
        """.fa-section-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: rgba(255,255,255,.30);""",
        """.fa-section-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: var(--text3);""",
        1
    )

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Find Anything light mode fixes applied.")


if __name__ == "__main__":
    main()
