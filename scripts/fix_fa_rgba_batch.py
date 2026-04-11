#!/usr/bin/env python3
"""
Batch-replace all remaining rgba(255,255,255,...) in FA/motion CSS with theme tokens.
"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"

# Map rgba(255,255,255,X) opacity ranges to theme tokens
OPACITY_MAP = [
    # (min_opacity, max_opacity, replacement)
    (0.01, 0.09, 'var(--border)'),      # very faint borders/dividers
    (0.10, 0.19, 'var(--border2)'),      # borders, hover borders
    (0.20, 0.34, 'var(--text3)'),        # muted text, labels
    (0.35, 0.54, 'var(--text3)'),        # secondary muted text
    (0.55, 0.74, 'var(--text2)'),        # secondary text
    (0.75, 0.89, 'var(--text2)'),        # near-primary text
    (0.90, 1.00, 'var(--text)'),         # primary text
]

def get_replacement(opacity):
    for lo, hi, token in OPACITY_MAP:
        if lo <= opacity <= hi:
            return token
    return 'var(--text)'

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")
    
    # Find all rgba(255,255,255,...) used as color values (not backgrounds)
    # We need to be selective — only replace color properties, not backgrounds
    
    count = 0
    
    # Replace color: rgba(255,255,255,X) patterns
    def replace_color(m):
        nonlocal count
        opacity = float(m.group(1))
        token = get_replacement(opacity)
        count += 1
        return f'color: {token};'
    
    html = re.sub(r'color:\s*rgba\(255,\s*255,\s*255,\s*\.?(\d*\.?\d+)\);', replace_color, html)
    
    # Replace color: #fff with var(--text)
    # But NOT in gradient backgrounds or specific places where white is intentional
    # Only replace standalone color: #fff; declarations
    def replace_fff(m):
        nonlocal count
        count += 1
        return 'color: var(--text);'
    
    # Be careful: only replace "color: #fff;" not "color: #fff" inside other contexts
    html = re.sub(r'(?<![background-])color:\s*#fff;', replace_fff, html)
    
    # Replace background: rgba(255,255,255,X) with low opacity (used as subtle bg)
    def replace_bg(m):
        nonlocal count
        opacity = float(m.group(1))
        if opacity <= 0.08:
            count += 1
            return f'background: var(--surface2);'
        elif opacity <= 0.15:
            count += 1
            return f'background: var(--surface3);'
        return m.group(0)  # leave higher opacity backgrounds alone
    
    html = re.sub(r'background:\s*rgba\(255,\s*255,\s*255,\s*\.?(\d*\.?\d+)\);', replace_bg, html)
    
    # Replace border-color: rgba(255,255,255,X)
    def replace_border(m):
        nonlocal count
        opacity = float(m.group(1))
        if opacity <= 0.12:
            count += 1
            return f'border-color: var(--border);'
        else:
            count += 1
            return f'border-color: var(--border2);'
    
    html = re.sub(r'border-color:\s*rgba\(255,\s*255,\s*255,\s*\.?(\d*\.?\d+)\);', replace_border, html)
    
    # Replace border: 1px solid rgba(255,255,255,X)
    def replace_border_shorthand(m):
        nonlocal count
        opacity = float(m.group(1))
        token = 'var(--border)' if opacity <= 0.12 else 'var(--border2)'
        count += 1
        return f'border: 1px solid {token};'
    
    html = re.sub(r'border:\s*1px\s+solid\s+rgba\(255,\s*255,\s*255,\s*\.?(\d*\.?\d+)\);', replace_border_shorthand, html)
    
    # Replace border: 2px solid rgba(255,255,255,X)
    def replace_border2(m):
        nonlocal count
        opacity = float(m.group(1))
        token = 'var(--border)' if opacity <= 0.12 else 'var(--border2)'
        count += 1
        return f'border: 2px solid {token};'
    
    html = re.sub(r'border:\s*2px\s+solid\s+rgba\(255,\s*255,\s*255,\s*\.?(\d*\.?\d+)\);', replace_border2, html)
    
    # Replace border-bottom: 1px solid rgba(255,255,255,X)
    def replace_border_bottom(m):
        nonlocal count
        opacity = float(m.group(1))
        token = 'var(--border)' if opacity <= 0.12 else 'var(--border2)'
        count += 1
        return f'border-bottom: 1px solid {token};'
    
    html = re.sub(r'border-bottom:\s*1px\s+solid\s+rgba\(255,\s*255,\s*255,\s*\.?(\d*\.?\d+)\);', replace_border_bottom, html)
    
    # Replace border-left: 2px solid rgba(...)
    def replace_border_left(m):
        nonlocal count
        count += 1
        return f'border-left: 2px solid var(--accent);'
    
    html = re.sub(r'border-left:\s*2px\s+solid\s+rgba\(10,\s*132,\s*255,\s*\.?\d+\);', replace_border_left, html)
    
    INPUT.write_text(html, encoding="utf-8")
    remaining = html.count('rgba(255,255,255')
    print(f"Done. {count} replacements. Remaining rgba(255,255,255,...): {remaining}")


if __name__ == "__main__":
    main()
