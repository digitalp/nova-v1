#!/usr/bin/env python3
"""
Nova Admin Panel — Visual Overhaul Transform (v2)

Applies Dribbble-inspired aesthetic + light/dark mode toggle.
Design references:
  - Task Management Dashboard (soft cards, warm accents, generous whitespace)
  - Minecloud Cloud Storage (clean sidebar, airy layout, rounded elements)

Usage:
    python3 scripts/transform_v2_visual.py

Reads:  static/admin.html
Writes: static/admin.html (backup at static/admin.html.bak_v2)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "static" / "admin.html"
BACKUP = ROOT / "static" / "admin.html.bak_v2"


def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found"); sys.exit(1)
    html = INPUT.read_text(encoding="utf-8")
    BACKUP.write_text(html, encoding="utf-8")
    print(f"Backup: {BACKUP}")

    # 1. Replace the entire :root block with dual-theme token system
    html = replace_root_tokens(html)

    # 2. Inject theme toggle button in topbar
    html = inject_theme_toggle(html)

    # 3. Inject theme toggle JS
    html = inject_theme_js(html)

    # 4. Update sidebar styles for the new aesthetic
    html = update_sidebar_styles(html)

    # 5. Update card styles (softer, more rounded, subtle shadows)
    html = update_card_styles(html)

    # 6. Update stat card styles
    html = update_stat_card_styles(html)

    # 7. Update button styles
    html = update_button_styles(html)

    # 8. Redesign dashboard section HTML
    html = redesign_dashboard_html(html)

    # 9. Update topbar styles
    html = update_topbar_styles(html)

    # 10. Update content area
    html = update_content_styles(html)

    # 11. Update page header styles
    html = update_page_header_styles(html)

    # 12. Update table styles
    html = update_table_styles(html)

    # 13. Update form styles
    html = update_form_styles(html)

    # 14. Update scrollbar for light mode
    html = update_scrollbar_styles(html)

    INPUT.write_text(html, encoding="utf-8")
    print(f"Done. Output: {INPUT}")
    print(f"Remaining inline styles: {html.count('style=')}")


# ═══════════════════════════════════════════════════════════════════════════════

def replace_root_tokens(html):
    """Replace the :root block with a dual-theme system using data-theme attribute."""
    old_root = re.search(r':root \{[^}]+\}', html, re.DOTALL)
    if not old_root:
        print("WARNING: Could not find :root block"); return html

    new_tokens = """/* ── Theme: Dark (default) ────────────────────────────────────────────────── */
:root, [data-theme="dark"] {
  --bg:          #0f1117;
  --bg2:         #161822;
  --surface:     #1c1e2e;
  --surface2:    #252840;
  --surface3:    #2e3150;
  --border:      rgba(255,255,255,.06);
  --border2:     rgba(255,255,255,.1);
  --accent:      #6c5ce7;
  --accent-dim:  rgba(108,92,231,.12);
  --accent-glow: rgba(108,92,231,.25);
  --accent-text: #a29bfe;
  --cyan:        #00cec9;
  --cyan-dim:    rgba(0,206,201,.12);
  --green:       #00b894;
  --green-dim:   rgba(0,184,148,.12);
  --red:         #ff6b6b;
  --red-dim:     rgba(255,107,107,.12);
  --yellow:      #fdcb6e;
  --yellow-dim:  rgba(253,203,110,.12);
  --purple:      #a29bfe;
  --text:        #f0f0f5;
  --text2:       #a0a3bd;
  --text3:       #5a5d7a;
  --nav-w:       260px;
  --radius:      16px;
  --radius-sm:   10px;
  --radius-xs:   6px;
  --shadow:      0 2px 8px rgba(0,0,0,.2), 0 0 1px rgba(0,0,0,.1);
  --shadow-lg:   0 8px 32px rgba(0,0,0,.3), 0 0 1px rgba(0,0,0,.15);
  --shadow-card: 0 1px 3px rgba(0,0,0,.12), 0 0 1px rgba(0,0,0,.08);

  /* ── Spacing scale ── */
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px;
  --sp-5: 20px; --sp-6: 24px; --sp-8: 32px; --sp-10: 40px; --sp-12: 48px;

  /* ── Typography scale ── */
  --text-xs: 11px; --text-sm: 12px; --text-md: 13px; --text-base: 14px;
  --text-lg: 16px; --text-xl: 20px; --text-2xl: 24px; --text-3xl: 32px;

  /* ── Font weight scale ── */
  --fw-normal: 400; --fw-medium: 500; --fw-semibold: 600; --fw-bold: 700;

  /* ── Transition tokens ── */
  --ease-default: cubic-bezier(.4, 0, .2, 1);
  --duration-fast: .15s; --duration-normal: .2s; --duration-slow: .3s;
}

/* ── Theme: Light ────────────────────────────────────────────────────────── */
[data-theme="light"] {
  --bg:          #f5f6fa;
  --bg2:         #ffffff;
  --surface:     #ffffff;
  --surface2:    #f0f1f5;
  --surface3:    #e8e9f0;
  --border:      rgba(0,0,0,.06);
  --border2:     rgba(0,0,0,.1);
  --accent:      #6c5ce7;
  --accent-dim:  rgba(108,92,231,.08);
  --accent-glow: rgba(108,92,231,.15);
  --accent-text: #5a4bd1;
  --cyan:        #00b4a6;
  --cyan-dim:    rgba(0,180,166,.08);
  --green:       #00a884;
  --green-dim:   rgba(0,168,132,.08);
  --red:         #e55050;
  --red-dim:     rgba(229,80,80,.08);
  --yellow:      #e5a800;
  --yellow-dim:  rgba(229,168,0,.08);
  --purple:      #7c6dd8;
  --text:        #1a1c2e;
  --text2:       #5a5d7a;
  --text3:       #9a9cb8;
  --shadow:      0 2px 8px rgba(0,0,0,.06), 0 0 1px rgba(0,0,0,.04);
  --shadow-lg:   0 8px 32px rgba(0,0,0,.08), 0 0 1px rgba(0,0,0,.06);
  --shadow-card: 0 1px 3px rgba(0,0,0,.04), 0 0 1px rgba(0,0,0,.03);
}
[data-theme="light"] .badge-muted { background: rgba(0,0,0,.05); color: var(--text3); }
[data-theme="light"] .log-line.lvl-debug { color: #b0b3c8; }
[data-theme="light"] ::-webkit-scrollbar-thumb { background: rgba(0,0,0,.12); }
[data-theme="light"] ::-webkit-scrollbar-thumb:hover { background: rgba(0,0,0,.2); }"""

    html = html[:old_root.start()] + new_tokens + html[old_root.end():]
    return html


def inject_theme_toggle(html):
    """Add a theme toggle button in the topbar actions area."""
    toggle_html = '''<button class="btn btn-ghost btn-sm" id="theme-toggle" onclick="toggleTheme()" aria-label="Toggle light/dark mode">
        <svg id="theme-icon-dark" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
        <svg id="theme-icon-light" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
      </button>'''
    # Insert before the refresh button in topbar-actions
    html = html.replace(
        '<button class="btn btn-ghost btn-sm" onclick="refreshSection()">',
        toggle_html + '\n      <button class="btn btn-ghost btn-sm" onclick="refreshSection()">',
        1
    )
    return html


def inject_theme_js(html):
    """Add theme toggle JavaScript before closing </script>."""
    theme_js = """
// ── Theme Toggle ────────────────────────────────────────────────────────────
function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('nova-theme', next);
  updateThemeIcon(next);
}
function updateThemeIcon(theme) {
  const dark = document.getElementById('theme-icon-dark');
  const light = document.getElementById('theme-icon-light');
  if (dark) dark.style.display = theme === 'dark' ? '' : 'none';
  if (light) light.style.display = theme === 'light' ? '' : 'none';
}
(function initTheme() {
  const saved = localStorage.getItem('nova-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  // Defer icon update until DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => updateThemeIcon(saved));
  } else {
    updateThemeIcon(saved);
  }
})();
"""
    html = html.replace("</script>\n</body>", theme_js + "\n</script>\n</body>", 1)
    return html


def update_sidebar_styles(html):
    """Update sidebar to match the Dribbble aesthetic — cleaner, softer."""
    old = """#sidebar {
  width: var(--nav-w);
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
  position: relative;
  z-index: 10;
}"""
    new = """#sidebar {
  width: var(--nav-w);
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  overflow: hidden;
  position: relative;
  z-index: 10;
  transition: background var(--duration-slow), border-color var(--duration-slow);
}"""
    html = html.replace(old, new, 1)

    # Remove the sidebar glow effect (too dark-theme specific)
    html = html.replace(
        """/* subtle top glow */
#sidebar::before {
  content: '';
  position: absolute;
  top: -60px; left: 50%;
  transform: translateX(-50%);
  width: 200px; height: 200px;
  background: radial-gradient(circle, rgba(129,140,248,.18) 0%, transparent 70%);
  pointer-events: none;
}""",
        """/* subtle top glow — theme-aware */
#sidebar::before {
  content: '';
  position: absolute;
  top: -60px; left: 50%;
  transform: translateX(-50%);
  width: 200px; height: 200px;
  background: radial-gradient(circle, var(--accent-dim) 0%, transparent 70%);
  pointer-events: none;
  opacity: 0.6;
}""",
        1
    )
    return html


def update_card_styles(html):
    """Softer cards with more rounded corners and subtle shadows."""
    old = """.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-5);
  margin-bottom: var(--sp-4);
  transition: border-color .2s;
}
.card:hover { border-color: var(--border2); }"""
    new = """.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-6);
  margin-bottom: var(--sp-4);
  box-shadow: var(--shadow-card);
  transition: border-color var(--duration-normal), box-shadow var(--duration-normal), background var(--duration-slow);
}
.card:hover { border-color: var(--border2); box-shadow: var(--shadow); }"""
    html = html.replace(old, new, 1)
    return html


def update_stat_card_styles(html):
    """Redesign stat cards — larger, more breathing room, softer feel."""
    old = """.stat-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: var(--sp-3); margin-bottom: var(--sp-4); }
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px 20px;
  display: flex;
  align-items: center;
  gap: 14px;
  transition: border-color .2s, transform .15s;
}
.stat-card:hover { border-color: var(--border2); transform: translateY(-1px); }"""
    new = """.stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: var(--sp-4); margin-bottom: var(--sp-6); }
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-5) var(--sp-6);
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  box-shadow: var(--shadow-card);
  transition: border-color var(--duration-normal), box-shadow var(--duration-normal), transform var(--duration-fast), background var(--duration-slow);
}
.stat-card:hover { border-color: var(--border2); box-shadow: var(--shadow); transform: translateY(-2px); }"""
    html = html.replace(old, new, 1)

    # Update stat icon to be larger and softer
    old_icon = """.stat-icon {
  width: 40px; height: 40px;
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.stat-icon svg { width: 20px; height: 20px; }"""
    new_icon = """.stat-icon {
  width: 44px; height: 44px;
  border-radius: var(--radius-sm);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.stat-icon svg { width: 20px; height: 20px; }"""
    html = html.replace(old_icon, new_icon, 1)

    # Update stat value to be slightly smaller and cleaner
    old_val = """.stat-value { font-size: 22px; font-weight: 700; margin-top: 2px; color: var(--text); }"""
    new_val = """.stat-value { font-size: var(--text-xl); font-weight: var(--fw-bold); margin-top: var(--sp-1); color: var(--text); letter-spacing: -.02em; }"""
    html = html.replace(old_val, new_val, 1)

    return html


def update_button_styles(html):
    """Softer buttons with more rounded corners."""
    old = """.btn-primary {
  background: var(--accent);
  color: #fff;
  box-shadow: 0 2px 12px rgba(129,140,248,.3);
}
.btn-primary:hover:not(:disabled) {
  background: #6d77f0;
  box-shadow: 0 4px 16px rgba(129,140,248,.4);
}"""
    new = """.btn-primary {
  background: var(--accent);
  color: #fff;
  box-shadow: 0 2px 12px var(--accent-glow);
}
.btn-primary:hover:not(:disabled) {
  filter: brightness(1.1);
  box-shadow: 0 4px 20px var(--accent-glow);
}"""
    html = html.replace(old, new, 1)
    return html


def redesign_dashboard_html(html):
    """Redesign the dashboard section with a greeting, better layout."""
    old_header = """      <div class="page-header">
        <div class="page-title">Dashboard</div>
        <div class="page-sub">System health and real-time status</div>
      </div>"""
    new_header = """      <div class="page-header">
        <div class="page-title" style="font-size:var(--text-2xl);font-weight:var(--fw-bold);letter-spacing:-.03em;">Welcome back 👋</div>
        <div class="page-sub">Here's what's happening with your Nova system</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:var(--sp-4);margin-bottom:var(--sp-6);">
        <div class="card mb-0" style="background:linear-gradient(135deg, var(--accent), #a29bfe);border:none;color:#fff;">
          <div style="font-size:var(--text-sm);opacity:.8;font-weight:var(--fw-medium);">Active Sessions</div>
          <div id="dash-sessions" style="font-size:var(--text-3xl);font-weight:var(--fw-bold);margin-top:var(--sp-2);">—</div>
        </div>
        <div class="card mb-0" style="background:linear-gradient(135deg, var(--green), #55efc4);border:none;color:#fff;">
          <div style="font-size:var(--text-sm);opacity:.8;font-weight:var(--fw-medium);">Month LLM Cost</div>
          <div id="dash-month-cost" style="font-size:var(--text-2xl);font-weight:var(--fw-bold);margin-top:var(--sp-2);">—</div>
          <div id="dash-month-calls" style="font-size:var(--text-xs);opacity:.7;margin-top:var(--sp-1);">— calls this month</div>
        </div>
        <div class="card mb-0" style="background:linear-gradient(135deg, #636e72, #b2bec3);border:none;color:#fff;">
          <div style="font-size:var(--text-sm);opacity:.8;font-weight:var(--fw-medium);">Server Version</div>
          <div id="dash-version" style="font-size:var(--text-2xl);font-weight:var(--fw-semibold);margin-top:var(--sp-2);">—</div>
        </div>
      </div>"""
    html = html.replace(old_header, new_header, 1)

    # Remove the old summary cards at the bottom of dashboard (they're now at the top)
    old_summary = """      <div class="grid-3 gap-3">
        <div class="card mb-0">
          <div class="card-title">Active Sessions</div>
          <div id="dash-sessions" class="text-3xl font-bold text-accent mt-2">—</div>
        </div>
        <div class="card mb-0">
          <div class="card-title">Month LLM Cost</div>
          <div id="dash-month-cost" class="text-2xl font-bold text-green mt-2">—</div>
          <div class="text-xs text-muted mt-1" id="dash-month-calls">— calls this month</div>
        </div>
        <div class="card mb-0">
          <div class="card-title">Server Version</div>
          <div id="dash-version" class="text-2xl font-semi mt-2">—</div>
        </div>
      </div>"""
    html = html.replace(old_summary, "", 1)

    # Add section labels for health and system gauges
    html = html.replace(
        '      <div class="stat-grid" id="health-stats">',
        '      <div style="font-size:var(--text-sm);font-weight:var(--fw-semibold);color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:var(--sp-3);">Service Health</div>\n      <div class="stat-grid" id="health-stats">',
        1
    )
    html = html.replace(
        '      <!-- System live gauges -->\n      <div class="dash-sys-grid">',
        '      <div style="font-size:var(--text-sm);font-weight:var(--fw-semibold);color:var(--text3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:var(--sp-3);">System Resources</div>\n      <div class="dash-sys-grid">',
        1
    )

    return html


def update_topbar_styles(html):
    """Cleaner topbar."""
    old = """#topbar {
  height: 56px;
  padding: 0 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
  background: rgba(8,12,22,.8);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  position: relative;
  z-index: 5;
}"""
    new = """#topbar {
  height: 60px;
  padding: 0 var(--sp-6);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
  background: var(--bg2);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  position: relative;
  z-index: 5;
  transition: background var(--duration-slow), border-color var(--duration-slow);
}"""
    html = html.replace(old, new, 1)
    return html


def update_content_styles(html):
    """More generous content padding."""
    html = html.replace(
        "#content { flex: 1; overflow-y: auto; padding: var(--sp-6); }",
        "#content { flex: 1; overflow-y: auto; padding: var(--sp-8); transition: background var(--duration-slow); }",
        1
    )
    return html


def update_page_header_styles(html):
    """Better page headers."""
    old = """.page-header { margin-bottom: var(--sp-5); }
.page-title  { font-size: 18px; font-weight: 700; color: var(--text); letter-spacing: -.3px; }
.page-sub    { font-size: 13px; color: var(--text3); margin-top: 4px; }"""
    new = """.page-header { margin-bottom: var(--sp-6); }
.page-title  { font-size: var(--text-xl); font-weight: var(--fw-bold); color: var(--text); letter-spacing: -.03em; }
.page-sub    { font-size: var(--text-md); color: var(--text3); margin-top: var(--sp-1); line-height: 1.5; }"""
    html = html.replace(old, new, 1)
    return html


def update_table_styles(html):
    """Softer table styles."""
    old = """table { width: 100%; border-collapse: collapse; }
thead tr { border-bottom: 1px solid var(--border); }
th { text-align: left; padding: 10px 14px; font-size: 11px; font-weight: 600; color: var(--text3); text-transform: uppercase; letter-spacing: .5px; }
td { padding: 12px 14px; border-bottom: 1px solid var(--border); font-size: 13px; color: var(--text2); }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255,255,255,.02); color: var(--text); }"""
    new = """table { width: 100%; border-collapse: collapse; }
thead tr { border-bottom: 1px solid var(--border); }
th { text-align: left; padding: var(--sp-3) var(--sp-4); font-size: var(--text-xs); font-weight: var(--fw-semibold); color: var(--text3); text-transform: uppercase; letter-spacing: .5px; }
td { padding: var(--sp-3) var(--sp-4); border-bottom: 1px solid var(--border); font-size: var(--text-md); color: var(--text2); transition: background var(--duration-fast); }
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--accent-dim); color: var(--text); }"""
    html = html.replace(old, new, 1)
    return html


def update_form_styles(html):
    """Softer form inputs."""
    old = """input[type=text]:focus, input[type=password]:focus, input[type=url]:focus, select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-dim);
}"""
    new = """input[type=text]:focus, input[type=password]:focus, input[type=url]:focus, select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-dim);
}"""
    html = html.replace(old, new, 1)
    return html


def update_scrollbar_styles(html):
    """Theme-aware scrollbar."""
    old = """::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,.1); border-radius: 10px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.2); }"""
    new = """::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,.1); border-radius: 10px; }
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.18); }"""
    html = html.replace(old, new, 1)
    return html


if __name__ == "__main__":
    main()
