'use strict';

(() => {
  const avatarApi = window.adminApi?.avatar;
  if (!avatarApi) return;

  const SKIN_LABELS = ['Porcelain', 'Light', 'Medium', 'Dark', 'Deep'];
  const HAIR_LABELS = ['Black', 'Dark Brown', 'Brown', 'Auburn', 'Red', 'Ginger', 'Blonde', 'Platinum', 'Grey', 'White'];

  let currentSkinTone = -1;
  let currentHairColor = -1;
  let activeAvatarUrl = '';

  function currentBgType() {
    return document.querySelector('input[name="bg-type"]:checked')?.value || 'color';
  }

  function currentBgPayload() {
    const type = currentBgType();
    return {
      bg_type: type,
      bg_color: type === 'color' ? (document.getElementById('bg-color-input')?.value.trim() || '') : '',
      bg_image_url: type === 'image' ? (document.getElementById('bg-image-input')?.value.trim() || '') : '',
    };
  }

  function selectSkin(index) {
    currentSkinTone = index;
    document.querySelectorAll('#skin-swatches .skin-swatch').forEach(s => s.classList.remove('selected'));
    const el = document.querySelector(`#skin-swatches .skin-swatch[data-index="${index}"]`);
    if (el) el.classList.add('selected');
    const label = document.getElementById('skin-label');
    if (label) label.textContent = index < 0 ? 'GLB Default' : (SKIN_LABELS[index] || '—');
  }

  function selectHair(index) {
    currentHairColor = index;
    document.querySelectorAll('#hair-swatches .skin-swatch').forEach(el => {
      el.classList.toggle('active', parseInt(el.dataset.hair, 10) === index);
    });
    const label = document.getElementById('hair-label');
    if (label) label.textContent = index < 0 ? 'GLB Default' : (HAIR_LABELS[index] || '—');
  }

  function onBgTypeChange() {
    const type = currentBgType();
    const colorSection = document.getElementById('bg-color-section');
    const imageSection = document.getElementById('bg-image-section');
    if (colorSection) colorSection.style.display = type === 'color' ? '' : 'none';
    if (imageSection) imageSection.style.display = type === 'image' ? '' : 'none';
  }

  function setBgColor(hex) {
    const input = document.getElementById('bg-color-input');
    const picker = document.getElementById('bg-color-picker');
    if (input) input.value = hex;
    if (picker) picker.value = hex;
  }

  function previewBgImage() {
    const url = document.getElementById('bg-image-input')?.value.trim() || '';
    const preview = document.getElementById('bg-image-preview');
    const img = document.getElementById('bg-image-preview-img');
    if (!preview || !img) return;
    if (!url) {
      preview.style.display = 'none';
      return;
    }
    img.src = url;
    preview.style.display = '';
    img.onerror = () => { preview.style.display = 'none'; };
  }

  async function loadAvatarLibrary() {
    const grid = document.getElementById('avatar-grid');
    if (!grid) return;
    try {
      const [settings, lib] = await Promise.all([
        avatarApi.getSettings(),
        avatarApi.getLibrary(),
      ]);
      activeAvatarUrl = settings.avatar_url || '';
      grid.innerHTML = '';
      if (!lib.avatars.length) {
        grid.innerHTML = '<div class="text-md text-muted">No avatars found in static/avatars/</div>';
        return;
      }
      for (const filename of lib.avatars) {
        const url = '/static/avatars/' + filename;
        const isDefault = filename === 'brunette.glb';
        const isActive = activeAvatarUrl === url || (!activeAvatarUrl && isDefault);
        const label = filename.replace(/\.glb$/i, '').replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
        const card = document.createElement('div');
        card.className = 'avatar-card' + (isActive ? ' active' : '');
        card.title = filename;
        card.onclick = () => selectAvatarFile(url);
        card.innerHTML =
          '<div class="avatar-card-icon">' +
          '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="text-muted">' +
          '<circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/></svg></div>' +
          '<div class="avatar-card-name">' + _escapeHtml(label) + '</div>' +
          (isActive ? '<div class="avatar-card-badge">Active</div>' : '') +
          (!isDefault ? '<button class="avatar-card-del" title="Delete" onclick="event.stopPropagation();deleteAvatar(\'' + _escapeHtml(filename) + '\')">×</button>' : '');
        grid.appendChild(card);
      }
    } catch (e) {
      grid.innerHTML = '<div class="text-md text-muted">Failed to load library.</div>';
    }
  }

  async function loadAvatarSettings() {
    try {
      const d = await avatarApi.getSettings();
      selectSkin(d.skin_tone ?? -1);
      selectHair(d.hair_color ?? -1);
      activeAvatarUrl = d.avatar_url || '';
      const avatarUrlInput = document.getElementById('avatar-url');
      if (avatarUrlInput) avatarUrlInput.value = activeAvatarUrl.startsWith('/static/') ? '' : activeAvatarUrl;
      if (d.bg_type) {
        const radio = document.querySelector('input[name="bg-type"][value="' + d.bg_type + '"]');
        if (radio) {
          radio.checked = true;
          onBgTypeChange();
        }
      }
      if (d.bg_color) setBgColor(d.bg_color);
      if (d.bg_image_url) {
        const imageInput = document.getElementById('bg-image-input');
        if (imageInput) imageInput.value = d.bg_image_url;
        previewBgImage();
      }
    } catch (e) {
      selectSkin(0);
    }
    await loadAvatarLibrary();
  }

  async function selectAvatarFile(url) {
    activeAvatarUrl = url;
    await avatarApi.saveSettings({
      skin_tone: currentSkinTone,
      hair_color: currentHairColor,
      avatar_url: url,
      ...currentBgPayload(),
    });
    const avatarUrlInput = document.getElementById('avatar-url');
    if (avatarUrlInput) avatarUrlInput.value = '';
    await loadAvatarLibrary();
    toast('Avatar selected — reload the avatar page to apply');
  }

  async function saveSkinTone() {
    await avatarApi.saveSettings({
      skin_tone: currentSkinTone,
      hair_color: currentHairColor,
      avatar_url: activeAvatarUrl,
      ...currentBgPayload(),
    });
    toast('Skin tone saved');
  }

  async function saveHairColor() {
    await avatarApi.saveSettings({
      skin_tone: currentSkinTone,
      hair_color: currentHairColor,
      avatar_url: activeAvatarUrl,
      ...currentBgPayload(),
    });
    toast('Hair colour saved');
  }

  async function saveExternalUrl() {
    const input = document.getElementById('avatar-url');
    const url = input?.value.trim() || '';
    if (!url) {
      toast('Enter a URL first');
      return;
    }
    activeAvatarUrl = url;
    await avatarApi.saveSettings({
      skin_tone: currentSkinTone,
      hair_color: currentHairColor,
      avatar_url: url,
      ...currentBgPayload(),
    });
    await loadAvatarLibrary();
    toast('External URL saved — reload the avatar page to apply');
  }

  async function clearExternalUrl() {
    const input = document.getElementById('avatar-url');
    if (input) input.value = '';
    activeAvatarUrl = '/static/avatars/brunette.glb';
    await avatarApi.saveSettings({
      skin_tone: currentSkinTone,
      hair_color: currentHairColor,
      avatar_url: activeAvatarUrl,
      ...currentBgPayload(),
    });
    await loadAvatarLibrary();
    toast('Reverted to library selection');
  }

  async function uploadAvatar(input) {
    const file = input.files?.[0];
    if (!file) return;
    const status = document.getElementById('avatar-upload-status');
    const sizeMB = (file.size / 1024 / 1024).toFixed(1);
    if (status) {
      status.style.color = '';
      status.textContent = `Uploading ${file.name} (${sizeMB} MB)… 0%`;
    }
    const fd = new FormData();
    fd.append('file', file);
    try {
      const d = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/admin/avatars/upload');
        xhr.withCredentials = true;
        xhr.upload.onprogress = event => {
          if (!status || !event.lengthComputable) return;
          const pct = Math.round(event.loaded / event.total * 100);
          status.textContent = pct < 100
            ? `⬆ Uploading ${sizeMB} MB… ${pct}%`
            : '🔧 Optimizing avatar — fixing skeleton, transferring blendshapes, compressing textures…';
        };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
          else reject(new Error(xhr.responseText || xhr.statusText));
        };
        xhr.onerror = () => reject(new Error('Network error'));
        xhr.send(fd);
      });
      input.value = '';
      const fix = d.fix || {};
      if (status) {
        if (fix.actions && fix.actions.length) {
          status.style.color = 'var(--green, #10b981)';
          status.innerHTML = '✅ Avatar optimized:<br>' + fix.actions.map(a => '  • ' + a).join('<br>');
        } else if (fix.error) {
          status.style.color = 'var(--warning, #f59e0b)';
          status.textContent = '⚠ Uploaded but fix failed: ' + fix.error;
        } else {
          status.textContent = '✅ Uploaded — no fixes needed';
        }
      }
      if (fix.actions && fix.actions.length) {
        toast('Avatar optimized: ' + fix.actions.length + ' fixes applied');
      } else if (!fix.error) {
        toast('Uploaded: ' + d.uploaded);
      }
      await loadAvatarLibrary();
    } catch (e) {
      if (status) {
        status.style.color = 'var(--danger, #ef4444)';
        status.textContent = '❌ Upload failed: ' + e.message;
      }
    }
  }

  async function deleteAvatar(filename) {
    if (!confirm('Delete ' + filename + '?')) return;
    try {
      await avatarApi.deleteAvatar(filename);
      if (activeAvatarUrl === '/static/avatars/' + filename) {
        activeAvatarUrl = '/static/avatars/brunette.glb';
        await avatarApi.saveSettings({
          skin_tone: currentSkinTone,
          hair_color: currentHairColor,
          avatar_url: activeAvatarUrl,
          ...currentBgPayload(),
        });
      }
      toast('Deleted ' + filename);
      await loadAvatarLibrary();
    } catch (e) {
      toast('Delete failed: ' + e.message);
    }
  }

  async function saveAvatarBg() {
    try {
      await avatarApi.saveSettings({
        skin_tone: currentSkinTone,
        hair_color: currentHairColor,
        avatar_url: activeAvatarUrl || '',
        ...currentBgPayload(),
      });
      toast('Background saved — reload the avatar page to see changes', 'ok');
    } catch (e) {
      toast('Failed to save: ' + e.message, 'err');
    }
  }

  async function resetAvatarBg() {
    const radio = document.querySelector('input[name="bg-type"][value="color"]');
    if (radio) radio.checked = true;
    onBgTypeChange();
    setBgColor('#080d16');
    const imageInput = document.getElementById('bg-image-input');
    if (imageInput) imageInput.value = '';
    const preview = document.getElementById('bg-image-preview');
    if (preview) preview.style.display = 'none';
    try {
      await avatarApi.saveSettings({
        skin_tone: currentSkinTone,
        hair_color: currentHairColor,
        avatar_url: activeAvatarUrl || '',
        bg_type: 'color',
        bg_color: '',
        bg_image_url: '',
      });
      toast('Background reset to default', 'ok');
    } catch (e) {
      toast('Failed to reset: ' + e.message, 'err');
    }
  }

  function bindAvatarEvents() {
    document.getElementById('skin-swatches')?.addEventListener('click', e => {
      const sw = e.target.closest('.skin-swatch[data-index]');
      if (sw) selectSkin(parseInt(sw.dataset.index, 10));
    });
    document.getElementById('hair-swatches')?.addEventListener('click', e => {
      const sw = e.target.closest('.skin-swatch[data-hair]');
      if (sw) selectHair(parseInt(sw.dataset.hair, 10));
    });
    document.getElementById('bg-swatches')?.addEventListener('click', e => {
      const sw = e.target.closest('.skin-swatch[data-color]');
      if (sw) setBgColor(sw.dataset.color);
    });
    document.getElementById('btn-save-skin-tone')?.addEventListener('click', () => saveSkinTone());
    document.getElementById('btn-save-hair-color')?.addEventListener('click', () => saveHairColor());
    document.querySelectorAll('input[name="bg-type"]').forEach(r => {
      r.addEventListener('change', () => onBgTypeChange());
    });
    document.getElementById('bg-color-picker')?.addEventListener('change', function onBgColorPickerChange() {
      const input = document.getElementById('bg-color-input');
      if (input) input.value = this.value;
    });
    document.getElementById('bg-color-input')?.addEventListener('input', function onBgColorInputChange() {
      try {
        const picker = document.getElementById('bg-color-picker');
        if (picker) picker.value = this.value;
      } catch (_) {}
    });
    document.getElementById('bg-image-input')?.addEventListener('input', () => previewBgImage());
    document.getElementById('btn-save-avatar-bg')?.addEventListener('click', () => saveAvatarBg());
    document.getElementById('btn-reset-avatar-bg')?.addEventListener('click', () => resetAvatarBg());
    document.getElementById('avatar-upload-input')?.addEventListener('change', function onAvatarUploadChange() {
      uploadAvatar(this);
    });
    document.getElementById('btn-upload-avatar')?.addEventListener('click', () => {
      document.getElementById('avatar-upload-input')?.click();
    });
    document.getElementById('btn-save-external-url')?.addEventListener('click', () => saveExternalUrl());
    document.getElementById('btn-clear-external-url')?.addEventListener('click', () => clearExternalUrl());
  }

  bindAvatarEvents();

  window.registerAdminSection?.('avatar', {
    onEnter: () => loadAvatarSettings(),
  });

  Object.assign(window, {
    loadAvatarSettings,
    loadAvatarLibrary,
    selectAvatarFile,
    saveSkinTone,
    saveHairColor,
    saveExternalUrl,
    clearExternalUrl,
    uploadAvatar,
    deleteAvatar,
    onBgTypeChange,
    setBgColor,
    previewBgImage,
    saveAvatarBg,
    resetAvatarBg,
  });
})();
