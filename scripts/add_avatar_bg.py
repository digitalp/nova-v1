#!/usr/bin/env python3
"""
Add avatar background customization — color or image.
Changes: admin.py (backend model), admin.html (UI), avatar.html (apply bg).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN_PY = ROOT / "avatar_backend" / "routers" / "admin.py"
ADMIN_HTML = ROOT / "static" / "admin.html"
AVATAR_HTML = ROOT / "static" / "avatar.html"

def main():
    # 1. Update backend model
    fix_backend()
    # 2. Add UI to admin panel
    fix_admin_html()
    # 3. Update avatar.html to apply background
    fix_avatar_html()
    print("Done. All 3 files updated.")


def fix_backend():
    html = ADMIN_PY.read_text(encoding="utf-8")

    # Add new fields to AvatarSettings model
    html = html.replace(
        """class AvatarSettings(BaseModel):
    skin_tone: int = 0
    avatar_url: str = \"\"""",
        """class AvatarSettings(BaseModel):
    skin_tone: int = 0
    avatar_url: str = \"\"
    bg_type: str = "color"  # "color" or "image"
    bg_color: str = ""  # hex color e.g. "#080d16"
    bg_image_url: str = ""  # URL to background image""",
        1
    )

    # Update the default return to include new fields
    html = html.replace(
        'return {"skin_tone": 0, "avatar_url": ""}',
        'return {"skin_tone": 0, "avatar_url": "", "bg_type": "color", "bg_color": "", "bg_image_url": ""}',
        1
    )

    ADMIN_PY.write_text(html, encoding="utf-8")
    print(f"Updated: {ADMIN_PY}")


def fix_admin_html():
    html = ADMIN_HTML.read_text(encoding="utf-8")

    # Add Background card after the Skin Tone card in the avatar section
    bg_card = """      <div class="card">
        <div class="card-title">Background</div>
        <p class="text-sm text-muted mb-4">Choose a background for the avatar page — a solid color or an image.</p>
        <div class="flex-row gap-3 mb-4">
          <label class="flex-row gap-2" style="cursor:pointer;">
            <input type="radio" name="bg-type" value="color" checked onchange="onBgTypeChange()"> Solid Color
          </label>
          <label class="flex-row gap-2" style="cursor:pointer;">
            <input type="radio" name="bg-type" value="image" onchange="onBgTypeChange()"> Background Image
          </label>
        </div>
        <div id="bg-color-section">
          <div class="field">
            <label for="bg-color-input">Background Color</label>
            <div class="flex-row gap-3">
              <input type="color" id="bg-color-picker" value="#080d16" style="width:48px;height:36px;border:1px solid var(--border2);border-radius:var(--radius-sm);cursor:pointer;padding:2px;" onchange="document.getElementById('bg-color-input').value=this.value">
              <input type="text" id="bg-color-input" placeholder="#080d16" value="" style="flex:1;" oninput="try{document.getElementById('bg-color-picker').value=this.value}catch(e){}">
            </div>
          </div>
          <div class="flex-row gap-2 flex-wrap mt-3">
            <div class="skin-swatch" title="Default Dark" style="background:#080d16;width:36px;height:36px;" onclick="setBgColor('#080d16')"></div>
            <div class="skin-swatch" title="Deep Navy" style="background:#0a192f;width:36px;height:36px;" onclick="setBgColor('#0a192f')"></div>
            <div class="skin-swatch" title="Charcoal" style="background:#1a1a2e;width:36px;height:36px;" onclick="setBgColor('#1a1a2e')"></div>
            <div class="skin-swatch" title="Midnight Blue" style="background:#0d1b2a;width:36px;height:36px;" onclick="setBgColor('#0d1b2a')"></div>
            <div class="skin-swatch" title="Dark Teal" style="background:#0b3d3d;width:36px;height:36px;" onclick="setBgColor('#0b3d3d')"></div>
            <div class="skin-swatch" title="Warm Grey" style="background:#2d2d2d;width:36px;height:36px;" onclick="setBgColor('#2d2d2d')"></div>
            <div class="skin-swatch" title="Pure Black" style="background:#000000;width:36px;height:36px;" onclick="setBgColor('#000000')"></div>
            <div class="skin-swatch" title="White" style="background:#ffffff;border:1px solid var(--border2);width:36px;height:36px;" onclick="setBgColor('#ffffff')"></div>
          </div>
        </div>
        <div id="bg-image-section" style="display:none;">
          <div class="field">
            <label for="bg-image-input">Image URL</label>
            <input type="text" id="bg-image-input" placeholder="https://example.com/background.jpg">
          </div>
          <div id="bg-image-preview" style="margin-top:var(--sp-3);border-radius:var(--radius-sm);overflow:hidden;display:none;">
            <img id="bg-image-preview-img" style="width:100%;max-height:160px;object-fit:cover;border-radius:var(--radius-sm);" alt="Background preview">
          </div>
        </div>
        <div class="flex-row gap-2 mt-4">
          <button class="btn btn-primary" onclick="saveAvatarBg()">Save Background</button>
          <button class="btn btn-ghost" onclick="resetAvatarBg()">Reset to Default</button>
        </div>
      </div>"""

    # Insert after the skin tone card's closing </div>
    # Find the skin tone save button and its parent card closing
    skin_card_end = '          <button class="btn btn-primary" onclick="saveSkinTone()">Save Skin Tone</button>\n        </div>\n      </div>'
    html = html.replace(
        skin_card_end,
        skin_card_end + "\n" + bg_card,
        1
    )

    # Add the JavaScript for background settings
    bg_js = """
// ── Avatar Background Settings ──────────────────────────────────────────────
function onBgTypeChange() {
  const type = document.querySelector('input[name="bg-type"]:checked')?.value || 'color';
  document.getElementById('bg-color-section').style.display = type === 'color' ? '' : 'none';
  document.getElementById('bg-image-section').style.display = type === 'image' ? '' : 'none';
}

function setBgColor(hex) {
  document.getElementById('bg-color-input').value = hex;
  document.getElementById('bg-color-picker').value = hex;
}

function previewBgImage() {
  const url = document.getElementById('bg-image-input').value.trim();
  const preview = document.getElementById('bg-image-preview');
  const img = document.getElementById('bg-image-preview-img');
  if (url) {
    img.src = url;
    preview.style.display = '';
    img.onerror = () => { preview.style.display = 'none'; };
  } else {
    preview.style.display = 'none';
  }
}

async function saveAvatarBg() {
  const type = document.querySelector('input[name="bg-type"]:checked')?.value || 'color';
  const color = document.getElementById('bg-color-input').value.trim();
  const imageUrl = document.getElementById('bg-image-input').value.trim();
  try {
    await api('POST', '/admin/avatar-settings', {
      skin_tone: _currentSkinTone,
      avatar_url: _activeAvatarUrl || '',
      bg_type: type,
      bg_color: type === 'color' ? color : '',
      bg_image_url: type === 'image' ? imageUrl : '',
    });
    toast('Background saved — reload the avatar page to see changes', 'ok');
  } catch(e) { toast('Failed to save: ' + e.message, 'err'); }
}

async function resetAvatarBg() {
  document.querySelector('input[name="bg-type"][value="color"]').checked = true;
  onBgTypeChange();
  setBgColor('#080d16');
  document.getElementById('bg-image-input').value = '';
  document.getElementById('bg-image-preview').style.display = 'none';
  try {
    await api('POST', '/admin/avatar-settings', {
      skin_tone: _currentSkinTone,
      avatar_url: _activeAvatarUrl || '',
      bg_type: 'color',
      bg_color: '',
      bg_image_url: '',
    });
    toast('Background reset to default', 'ok');
  } catch(e) { toast('Failed to reset: ' + e.message, 'err'); }
}

// Load background settings when avatar section loads
const _origLoadAvatarSettings = typeof loadAvatarSettings === 'function' ? loadAvatarSettings : null;
"""

    html = html.replace("</script>\n</body>", bg_js + "\n</script>\n</body>", 1)

    # Hook into loadAvatarSettings to also load bg settings
    # Find the existing loadAvatarSettings function and add bg loading
    html = html.replace(
        "async function loadAvatarSettings() {",
        "async function loadAvatarSettings() {\n  // Also load background settings after main settings",
        1
    )

    # Find where avatar settings are loaded and add bg field population
    # Look for where skin_tone is read from the response
    html = html.replace(
        "if (typeof s.skin_tone === 'number') _currentSkinTone = s.skin_tone;",
        """if (typeof s.skin_tone === 'number') _currentSkinTone = s.skin_tone;
    // Populate background settings
    const bgType = s.bg_type || 'color';
    const bgRadio = document.querySelector('input[name="bg-type"][value="' + bgType + '"]');
    if (bgRadio) bgRadio.checked = true;
    onBgTypeChange();
    if (s.bg_color) setBgColor(s.bg_color);
    if (s.bg_image_url) {
      document.getElementById('bg-image-input').value = s.bg_image_url;
      previewBgImage();
    }""",
        1
    )

    # Add image preview on input change
    html = html.replace(
        'id="bg-image-input" placeholder="https://example.com/background.jpg"',
        'id="bg-image-input" placeholder="https://example.com/background.jpg" oninput="previewBgImage()"',
        1
    )

    ADMIN_HTML.write_text(html, encoding="utf-8")
    print(f"Updated: {ADMIN_HTML}")


def fix_avatar_html():
    html = AVATAR_HTML.read_text(encoding="utf-8")

    # Find where avatar settings are loaded and add background application
    # Look for the skin_tone loading code
    settings_load = "if (typeof s.skin_tone === 'number') _skinToneIndex = s.skin_tone;"
    if settings_load not in html:
        print("WARNING: Could not find avatar settings load in avatar.html")
        return

    bg_apply = """if (typeof s.skin_tone === 'number') _skinToneIndex = s.skin_tone;
    // Apply background from settings
    if (s.bg_type === 'image' && s.bg_image_url) {
      document.body.style.background = '#080d16';
      document.getElementById('avatar-container').style.background = 'url(' + s.bg_image_url + ') center/cover no-repeat';
    } else if (s.bg_type === 'color' && s.bg_color) {
      const c = s.bg_color;
      document.body.style.background = c;
      // Create a subtle radial gradient from the chosen color
      const r = parseInt(c.slice(1,3),16), g = parseInt(c.slice(3,5),16), b = parseInt(c.slice(5,7),16);
      const lighter = 'rgb(' + Math.min(r+20,255) + ',' + Math.min(g+20,255) + ',' + Math.min(b+20,255) + ')';
      document.getElementById('avatar-container').style.background = 'radial-gradient(ellipse at 50% 40%, ' + lighter + ' 0%, ' + c + ' 70%)';
    }"""

    html = html.replace(settings_load, bg_apply, 1)

    AVATAR_HTML.write_text(html, encoding="utf-8")
    print(f"Updated: {AVATAR_HTML}")


if __name__ == "__main__":
    main()
