#!/usr/bin/env python3
"""Fix orphaned metrics section HTML comments and stray </div>."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")

    # Remove the orphaned metrics section remnants
    old = """    <!-- ══════════════ SYSTEM METRICS ══════════════ -->
    <!-- Live gauges -->
      <!-- History charts -->
      </div>

    <!-- ══════════════ TOOLS ══════════════ -->"""

    new = """    <!-- ══════════════ TOOLS ══════════════ -->"""

    if old in html:
        html = html.replace(old, new, 1)
        print("Removed orphaned metrics section remnants + stray </div>")
    else:
        print("WARNING: Could not find orphaned metrics block")

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Output: {INPUT}")

if __name__ == "__main__":
    main()
