#!/usr/bin/env python3
"""
Nova Admin Panel Redesign — Transform Script
Applies Phase 2-4 changes to admin.html in a single pass.

Usage:
    python3 scripts/transform_admin.py

Reads:  static/admin.html
Writes: static/admin.html (backup created at static/admin.html.bak_transform)
"""
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
INPUT = ROOT / "static" / "admin.html"
BACKUP = ROOT / "static" / "admin.html.bak_transform"

def main():
    if not INPUT.exists():
        print(f"ERROR: {INPUT} not found")
        sys.exit(1)

    html = INPUT.read_text(encoding="utf-8")
    original_len = len(html)

    # Backup
    BACKUP.write_text(html, encoding="utf-8")
    print(f"Backup: {BACKUP}")

    # ── Phase 2: Component Redesign ──────────────────────────────────────

    # 2a. Inject new CSS components before the closing </style>
    new_css = NEW_CSS_COMPONENTS()
    html = html.replace("</style>", new_css + "\n</style>", 1)

    # 2b. Inject _CONFIG_CATEGORIES and toggleCollapsible into JS
    new_js = NEW_JS_ADDITIONS()
    # Insert before the closing </script>
    html = html.replace("</script>\n</body>", new_js + "\n</script>\n</body>", 1)

    # 2c. Remaining inline style cleanup (component-specific)
    html = cleanup_remaining_inline_styles(html)

    # 2d. Replace nav-item divs with buttons
    html = re.sub(
        r'<div class="nav-item([^"]*)" data-section="([^"]*)" onclick="navigate\(this\)"',
        r'<button class="nav-item\1" data-section="\2" onclick="navigate(this)"',
        html,
    )
    # Close the button tags (replace the corresponding </div> after nav-item content)
    # This is tricky — nav items have SVG + text inside. Use a targeted approach:
    # Find each <button class="nav-item ... and replace the next </div> with </button>
    html = fix_nav_item_closing_tags(html)

    # ── Phase 3: UX Enhancements ─────────────────────────────────────────

    # 3a. Toast container (replace single #toast with stacking container)
    html = upgrade_toast_system(html)

    # ── Phase 4: Accessibility ───────────────────────────────────────────

    # 4a. Add keyboard shortcut handler
    # (Already injected via NEW_JS_ADDITIONS)

    # 4b. Add focus-visible styles
    # (Already injected via NEW_CSS_COMPONENTS)

    # ── Write output ─────────────────────────────────────────────────────
    INPUT.write_text(html, encoding="utf-8")
    new_len = len(html)
    remaining_styles = html.count('style="')
    print(f"Done. {original_len} → {new_len} chars")
    print(f"Remaining inline styles: {remaining_styles}")
    print(f"Output: {INPUT}")


# ═══════════════════════════════════════════════════════════════════════════════
# CSS COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════════

def NEW_CSS_COMPONENTS():
    return """
/* ── Empty State ─────────────────────────────────────────────────────────────── */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--sp-12) var(--sp-6);
  text-align: center;
  color: var(--text3);
}
.empty-state-icon {
  width: 48px; height: 48px;
  margin-bottom: var(--sp-4);
  opacity: 0.4;
  font-size: 32px;
}
.empty-state-title {
  font-size: var(--text-lg);
  font-weight: var(--fw-semibold);
  color: var(--text2);
  margin-bottom: var(--sp-2);
}
.empty-state-desc {
  font-size: var(--text-md);
  max-width: 320px;
  line-height: 1.5;
}

/* ── Loading Skeleton ────────────────────────────────────────────────────────── */
.loading-skeleton {
  background: linear-gradient(90deg, var(--surface) 25%, var(--surface2) 50%, var(--surface) 75%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.5s infinite;
  border-radius: var(--radius-sm);
  min-height: 20px;
}
@keyframes skeleton-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
.loading-spinner {
  width: 24px; height: 24px;
  border: 2px solid var(--border2);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin .7s linear infinite;
}

/* ── Collapsible Group ───────────────────────────────────────────────────────── */
.collapsible-group {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: var(--sp-3);
  overflow: hidden;
}
.collapsible-header {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  width: 100%;
  padding: var(--sp-4) var(--sp-5);
  background: var(--surface);
  border: none;
  color: var(--text);
  font-size: var(--text-base);
  font-weight: var(--fw-semibold);
  font-family: inherit;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease-default);
  text-align: left;
}
.collapsible-header:hover { background: var(--surface2); }
.collapsible-chevron {
  margin-left: auto;
  width: 16px; height: 16px;
  transition: transform var(--duration-normal) var(--ease-default);
  color: var(--text3);
  flex-shrink: 0;
}
.collapsible-group[data-expanded="false"] .collapsible-chevron {
  transform: rotate(-90deg);
}
.collapsible-body {
  padding: var(--sp-4) var(--sp-5);
  border-top: 1px solid var(--border);
}
.collapsible-group[data-expanded="false"] .collapsible-body {
  display: none;
}

/* ── Segmented Control ───────────────────────────────────────────────────────── */
.segmented-control {
  display: inline-flex;
  background: rgba(255,255,255,.06);
  border-radius: var(--radius);
  padding: 3px;
  position: relative;
}
.segmented-control .segment-btn {
  padding: var(--sp-1) var(--sp-4);
  border-radius: calc(var(--radius) - 2px);
  border: none;
  background: transparent;
  color: var(--text3);
  font-size: var(--text-sm);
  font-weight: var(--fw-semibold);
  font-family: inherit;
  cursor: pointer;
  transition: color var(--duration-fast), background var(--duration-fast);
  position: relative;
  z-index: 1;
}
.segmented-control .segment-btn:hover { color: var(--text2); }
.segmented-control .segment-btn.active {
  color: var(--accent);
  background: var(--accent-dim);
}

/* ── Toast Stacking ──────────────────────────────────────────────────────────── */
.toast-container {
  position: fixed;
  top: var(--sp-6);
  right: var(--sp-6);
  z-index: 500;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  pointer-events: none;
}
.toast-item {
  background: var(--surface2);
  border: 1px solid var(--border2);
  border-radius: var(--radius);
  padding: var(--sp-3) var(--sp-5);
  font-size: var(--text-md);
  color: var(--text);
  box-shadow: var(--shadow-lg);
  pointer-events: auto;
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  max-width: 380px;
  font-weight: var(--fw-medium);
  animation: toast-in var(--duration-normal) var(--ease-default);
}
.toast-item .toast-close {
  background: none; border: none;
  color: var(--text3); cursor: pointer;
  padding: var(--sp-1); margin-left: auto;
  font-size: var(--text-lg); line-height: 1;
}
.toast-item .toast-close:hover { color: var(--text); }
.toast-item.ok   { border-left: 3px solid var(--green); }
.toast-item.err  { border-left: 3px solid var(--red); }
.toast-item.warn { border-left: 3px solid var(--yellow); }
@keyframes toast-in {
  from { opacity: 0; transform: translateX(20px); }
  to   { opacity: 1; transform: translateX(0); }
}

/* ── Focus Visible (Accessibility) ───────────────────────────────────────────── */
.nav-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
button:focus-visible,
a:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* ── Log Search ──────────────────────────────────────────────────────────────── */
.log-search-wrap {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 1;
  min-width: 0;
}
.log-search-input {
  flex: 1;
  min-width: 120px;
  background: var(--bg);
  border: 1px solid var(--border2);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: 5px 10px;
  font-size: var(--text-sm);
  font-family: inherit;
  outline: none;
  transition: border-color var(--duration-fast);
}
.log-search-input:focus { border-color: var(--accent); }
.log-counter {
  font-size: var(--text-xs);
  color: var(--text3);
  font-weight: var(--fw-medium);
  padding: var(--sp-1) var(--sp-3);
  background: rgba(255,255,255,.04);
  border-radius: 20px;
  white-space: nowrap;
}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# JS ADDITIONS
# ═══════════════════════════════════════════════════════════════════════════════

def NEW_JS_ADDITIONS():
    return """
// ── Config Categories (Phase 2) ─────────────────────────────────────────────
const _CONFIG_CATEGORIES = {
  'LLM Provider':       ['LLM_PROVIDER', 'OLLAMA_URL', 'OLLAMA_MODEL', 'CLOUD_MODEL'],
  'API Keys':           ['API_KEY', 'OPENAI_API_KEY', 'GOOGLE_API_KEY', 'ANTHROPIC_API_KEY', 'ELEVENLABS_API_KEY'],
  'Speech (TTS/STT)':   ['WHISPER_MODEL', 'TTS_PROVIDER', 'PIPER_VOICE', 'ELEVENLABS_VOICE_ID', 'ELEVENLABS_MODEL',
                          'AFROTTS_VOICE', 'AFROTTS_SPEED', 'INTRON_AFRO_TTS_URL', 'INTRON_AFRO_TTS_TIMEOUT_S',
                          'INTRON_AFRO_TTS_REFERENCE_WAV', 'INTRON_AFRO_TTS_LANGUAGE', 'TTS_ENGINE'],
  'Home Assistant':     ['HA_URL', 'HA_TOKEN'],
  'Speakers & Audio':   ['SPEAKERS', 'SPEAKER_AUDIO_OFFSET_MS'],
  'Motion & Camera':    ['MOTION_CLIP_DURATION_S', 'MOTION_CLIP_SEARCH_CANDIDATES', 'MOTION_CLIP_SEARCH_RESULTS'],
  'Server & Network':   ['PUBLIC_URL', 'CORS_ORIGINS', 'LOG_LEVEL', 'HOST', 'PORT'],
};

function toggleCollapsible(header) {
  const group = header.closest('.collapsible-group');
  const expanded = group.dataset.expanded !== 'false';
  group.dataset.expanded = expanded ? 'false' : 'true';
  header.setAttribute('aria-expanded', !expanded);
}

// ── Gauge Color Classification (Phase 2) ────────────────────────────────────
function getGaugeColorClass(pct) {
  if (pct > 95) return 'red';
  if (pct > 80) return 'yellow';
  return 'green';
}

// ── Keyboard Shortcuts (Phase 4) ────────────────────────────────────────────
document.addEventListener('keydown', (e) => {
  if (!e.ctrlKey || e.altKey || e.metaKey) return;
  const num = parseInt(e.key);
  if (num >= 1 && num <= 9) {
    e.preventDefault();
    const items = [...document.querySelectorAll('.nav-item')].filter(el => el.offsetParent !== null);
    if (items[num - 1]) navigate(items[num - 1]);
  }
});

// ── Log Search/Filter (Phase 3) ─────────────────────────────────────────────
let _logSearchTerm = '';
let _logLevelFilter = 'all';
const _LOG_LEVELS = ['debug', 'info', 'warning', 'error', 'critical'];

function filterPylogLines() {
  const output = document.getElementById('pylog-output') || document.getElementById('log-output');
  if (!output) return;
  const lines = output.querySelectorAll('.log-line');
  const term = _logSearchTerm.toLowerCase();
  const levelIdx = _logLevelFilter === 'all' ? -1 : _LOG_LEVELS.indexOf(_logLevelFilter);
  let shown = 0, total = lines.length;
  lines.forEach(line => {
    const text = line.textContent.toLowerCase();
    const matchesTerm = !term || text.includes(term);
    let matchesLevel = true;
    if (levelIdx >= 0) {
      const lineLevel = _LOG_LEVELS.findIndex(l => line.classList.contains('lvl-' + l));
      matchesLevel = lineLevel >= levelIdx;
    }
    const visible = matchesTerm && matchesLevel;
    line.style.display = visible ? '' : 'none';
    if (visible) shown++;
  });
  const counter = document.getElementById('log-line-counter');
  if (counter) {
    counter.textContent = (term || levelIdx >= 0) ? `${shown} of ${total} lines` : `${total} lines`;
  }
}

let _logSearchDebounce = null;
function onLogSearchInput(e) {
  clearTimeout(_logSearchDebounce);
  _logSearchDebounce = setTimeout(() => {
    _logSearchTerm = e.target.value;
    filterPylogLines();
  }, 150);
}

function setLogLevelFilter(level) {
  _logLevelFilter = level;
  document.querySelectorAll('.log-level-chip').forEach(c => {
    c.classList.toggle('active', c.dataset.level === level);
  });
  filterPylogLines();
}

// ── Stacking Toast System (Phase 3) ─────────────────────────────────────────
function toast(msg, type) {
  type = type || 'ok';
  let container = document.querySelector('.toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  // Max 5 toasts
  while (container.children.length >= 5) {
    container.removeChild(container.firstChild);
  }
  const item = document.createElement('div');
  item.className = 'toast-item ' + type;
  item.innerHTML = '<span>' + msg + '</span><button class="toast-close" onclick="this.parentElement.remove()">&times;</button>';
  container.appendChild(item);
  setTimeout(() => { if (item.parentElement) item.remove(); }, 4000);
}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# INLINE STYLE CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════

def cleanup_remaining_inline_styles(html):
    """Replace remaining common inline style patterns with utility classes."""
    replacements = [
        # flex patterns
        ('style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;"', 'class="flex-row flex-wrap gap-2"'),
        ('style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;"', 'class="flex-row flex-wrap gap-3"'),
        ('style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;"', 'class="flex-row flex-wrap gap-3"'),
        ('style="display:flex;align-items:flex-start;gap:12px;"', 'class="flex-row gap-3"'),
        ('style="display:flex;align-items:center;gap:16px;"', 'class="flex-row gap-4"'),
        ('style="display:flex;flex-direction:column;gap:8px;"', 'class="flex-col gap-2"'),
        ('style="display:flex;flex-direction:column;gap:16px;"', 'class="flex-col gap-4"'),
        # grid patterns
        ('style="display:grid;grid-template-columns:1fr 1fr;gap:12px;"', 'class="grid-2 gap-3"'),
        ('style="display:grid;grid-template-columns:1fr 1fr;gap:16px;"', 'class="grid-2 gap-4"'),
        ('style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;"', 'class="grid-3 gap-3"'),
        # margin/padding
        ('style="margin-top:4px;"', 'class="mt-1"'),
        ('style="margin-top:6px;"', 'class="mt-2"'),
        ('style="margin-top:10px;"', 'class="mt-3"'),
        ('style="margin-top:14px;"', 'class="mt-3"'),
        ('style="margin-top:20px;"', 'class="mt-5"'),
        ('style="margin-top:24px;"', 'class="mt-6"'),
        ('style="margin-bottom:4px;"', 'class="mb-2"'),
        ('style="margin-bottom:6px;"', 'class="mb-2"'),
        ('style="margin-bottom:8px;"', 'class="mb-2"'),
        ('style="margin-bottom:10px;"', 'class="mb-3"'),
        ('style="margin-bottom:20px;"', 'class="mb-4"'),
        # font/color
        ('style="font-size:12px;color:var(--text3);margin-top:4px;"', 'class="text-sm text-muted mt-1"'),
        ('style="font-size:12px;color:var(--text3);margin-top:8px;"', 'class="text-sm text-muted mt-2"'),
        ('style="font-size:12px;color:var(--text2);"', 'class="text-sm text-sub"'),
        ('style="font-size:11px;color:var(--text2);"', 'class="text-xs text-sub"'),
        ('style="font-size:13px;color:var(--text2);"', 'class="text-md text-sub"'),
        ('style="font-size:10px;color:var(--text3);"', 'class="text-xs text-muted"'),
        ('style="font-size:14px;font-weight:600;"', 'class="text-base font-semi"'),
        ('style="font-size:13px;font-weight:600;"', 'class="text-md font-semi"'),
        ('style="font-size:12px;font-weight:600;"', 'class="text-sm font-semi"'),
        ('style="font-weight:600;"', 'class="font-semi"'),
        ('style="font-weight:700;"', 'class="font-bold"'),
    ]
    for old, new in replacements:
        html = html.replace(old, new)
    return html


# ═══════════════════════════════════════════════════════════════════════════════
# NAV ITEM DIV → BUTTON
# ═══════════════════════════════════════════════════════════════════════════════

def fix_nav_item_closing_tags(html):
    """Replace </div> closing tags for nav-items that were converted to <button>."""
    lines = html.split('\n')
    result = []
    in_nav_button = False
    for line in lines:
        if '<button class="nav-item' in line:
            in_nav_button = True
        if in_nav_button and '</div>' in line and '<div' not in line:
            line = line.replace('</div>', '</button>', 1)
            in_nav_button = False
        result.append(line)
    return '\n'.join(result)


# ═══════════════════════════════════════════════════════════════════════════════
# TOAST UPGRADE
# ═══════════════════════════════════════════════════════════════════════════════

def upgrade_toast_system(html):
    """Replace the old single #toast div with a stacking container."""
    # Remove old toast div if present
    html = re.sub(
        r'<div id="toast"[^>]*>.*?</div>',
        '<div class="toast-container"></div>',
        html,
        flags=re.DOTALL,
    )
    return html


if __name__ == "__main__":
    main()
