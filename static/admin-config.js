'use strict';

(() => {
  const configState = {
    lastConfigValues: {},
  };

  const configApi = window.adminApi?.config;
  if (!configApi) return;

  async function loadVisionCameras() {
    const el = document.getElementById('vision-cameras-list');
    if (!el) return;
    try {
      const d = await configApi.getVisionCameras();
      const _cameras = d.cameras || [];
      el.innerHTML = _cameras.map(c =>
        `<label style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--surface2);border-radius:8px;cursor:pointer;">
          <input type="checkbox" class="vision-cam-check" value="${_escapeHtml(c.entity_id)}" ${c.vision_enabled ? 'checked' : ''}>
          <span style="font-size:13px;">${_escapeHtml(c.label)}</span>
          <span class="text-sm text-muted" style="margin-left:auto;">${_escapeHtml(c.entity_id)}</span>
        </label>`
      ).join('');
      const _visionEnabled = _cameras.filter(c => c.vision_enabled).length;
      const _visionEl = document.getElementById('cfg-summary-vision');
      if (_visionEl) _visionEl.textContent = _visionEnabled + ' / ' + _cameras.length;
    } catch (e) {
      el.innerHTML = '<div class="text-sm" style="color:var(--danger);">Failed to load cameras: ' + (e.message || e) + '</div>';
    }
  }

  async function saveVisionCameras() {
    const checks = document.querySelectorAll('.vision-cam-check:checked');
    const enabled = Array.from(checks).map(c => c.value);
    try {
      await configApi.saveVisionCameras({ enabled });
      toast(`Vision enabled for ${enabled.length} cameras. Restart to apply.`);
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function loadRooms() {
    const el = document.getElementById('rooms-list');
    if (!el) return;
    try {
      const [rd, ad] = await Promise.all([
        configApi.getRooms(),
        configApi.getAvatars().catch(() => ({ avatars: [] })),
      ]);
      const rooms = rd.rooms || [];
      const avatars = ad.avatars || [];
      const avatarOptions = ['<option value="">Default (global setting)</option>',
        ...avatars.map(a => `<option value="${_escapeHtml(a)}">${_escapeHtml(a)}</option>`),
      ].join('');
      const newGlbSelEarly = document.getElementById('room-new-glb');
      if (newGlbSelEarly && avatars.length) {
        const cur = newGlbSelEarly.value;
        newGlbSelEarly.innerHTML = '<option value="">Default</option>' +
          avatars.map(a => `<option value="${_escapeHtml(a)}">${_escapeHtml(a)}</option>`).join('');
        if (cur) newGlbSelEarly.value = cur;
      }
      if (!rooms.length) {
        el.innerHTML = '<div class="text-sm text-muted">No rooms configured yet. Add one below.</div>';
        return;
      }
      el.innerHTML = rooms.map(r => `
        <div style="display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--surface2);border-radius:10px;flex-wrap:wrap;">
          <span style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:${r.connected ? '#10b981' : 'var(--text3)'};"
                title="${r.connected ? 'Connected' : 'Offline'}"></span>
          <div style="flex:1;min-width:120px;">
            <div style="font-weight:600;font-size:13px;">${_escapeHtml(r.label)}</div>
            <div style="font-size:11px;color:var(--text3);font-family:monospace;">${_escapeHtml(r.id)}</div>
          </div>
          <select class="room-glb-select" data-room-id="${_escapeHtml(r.id)}"
            style="padding:4px 8px;font-size:12px;background:var(--surface3);border:1px solid var(--border);border-radius:6px;color:var(--text1);max-width:180px;"
            onchange="updateRoomGlb(this)">
            ${avatarOptions}
          </select>
          <a href="${_escapeHtml(r.avatar_url)}" target="_blank"
             style="font-size:11px;color:var(--accent);text-decoration:none;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex-shrink:0;"
             title="${_escapeHtml(r.avatar_url)}">${_escapeHtml(r.avatar_url)}</a>
          <button class="btn btn-outline btn-xs" onclick='copyRoomUrl(${JSON.stringify(r.avatar_url)})' style="flex-shrink:0;" title="Copy public URL">Public</button>
          <button class="btn btn-outline btn-xs" onclick='copyRoomUrl(${JSON.stringify(r.local_url || "")})' style="flex-shrink:0;" title="Copy local URL">Local</button>
          <button class="btn btn-outline btn-xs" style="color:var(--danger);flex-shrink:0;" onclick="deleteRoom(${JSON.stringify(r.id)})">Remove</button>
        </div>`
      ).join('');
      rooms.forEach(r => {
        const sel = el.querySelector(`.room-glb-select[data-room-id="${CSS.escape(r.id)}"]`);
        if (sel && r.glb) sel.value = r.glb;
      });
    } catch (e) {
      el.innerHTML = '<div class="text-sm" style="color:var(--danger);">Failed to load rooms: ' + (e.message || e) + '</div>';
    }
  }

  async function updateRoomGlb(selectEl) {
    const roomId = selectEl.dataset.roomId;
    const glb = selectEl.value;
    try {
      await configApi.patchRoom(roomId, { glb: glb || null });
      toast(glb ? `Avatar set to ${glb}` : 'Using default avatar');
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  function copyRoomUrl(url) {
    if (!url) {
      toast('No URL', 'err');
      return;
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(url).then(() => toast('URL copied'));
      return;
    }
    const textarea = document.createElement('textarea');
    textarea.value = url;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    document.body.removeChild(textarea);
    toast('URL copied');
  }

  async function addRoom() {
    const label = document.getElementById('room-new-label')?.value.trim();
    const id = document.getElementById('room-new-id')?.value.trim().toLowerCase().replace(/\s+/g, '_');
    const glb = document.getElementById('room-new-glb')?.value.trim() || null;
    if (!label || !id) {
      toast('Enter room name and slug', 'err');
      return;
    }
    try {
      await configApi.createRoom({ label, id, ...(glb ? { glb } : {}) });
      document.getElementById('room-new-label').value = '';
      document.getElementById('room-new-id').value = '';
      if (document.getElementById('room-new-glb')) document.getElementById('room-new-glb').value = '';
      await loadRooms();
      toast('Room added');
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function deleteRoom(roomId) {
    if (!confirm(`Remove room "${roomId}"?`)) return;
    try {
      await configApi.deleteRoom(roomId);
      await loadRooms();
      toast('Room removed');
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function loadGeminiPool() {
    const keysEl = document.getElementById('gemini-pool-keys');
    const statsEl = document.getElementById('gemini-pool-stats');
    if (!keysEl) return;
    try {
      const d = await configApi.getGeminiPool();
      const keys = d.keys || [];
      const stats = d.stats || {};
      statsEl.innerHTML = `${stats.pool_size || 0} keys · ${stats.available || 0} available · ${stats.total_calls || 0} calls · ${stats.total_429s || 0} rate limits` + (stats.rpm ? ` · <strong>${stats.rpm}</strong> req/min` : "") + (stats.keys_in_cooldown ? ` · <span style="color:var(--danger)">${stats.keys_in_cooldown} in cooldown</span>` : "") + (stats.total_tokens ? ` · ${stats.total_tokens} tokens` : "");
      if (!keys.length) {
        keysEl.innerHTML = '<div class="text-sm text-muted">No keys configured. Add your Gemini API keys below.</div>';
        return;
      }
      keysEl.innerHTML = keys.map((k, i) => {
        const status = k.available
          ? '<span style="color:var(--green,#10b981);font-weight:600;">● Active</span>'
          : '<span style="color:var(--danger,#ef4444);font-weight:600;">● Cooldown ' + k.cooldown_remaining_s + 's</span>';
        const pins = k.pinned_cameras.length ? '<span class="text-sm text-muted"> · 📷 ' + k.pinned_cameras.join(', ') + '</span>' : '';
        const chk = k.enabled ? 'checked' : '';
        return '<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--surface2);border-radius:8px;border:1px solid var(--border);">'
          + '<label style="display:flex;align-items:center;gap:6px;cursor:pointer;" title="Enable / disable this key">'
          + '<input type="checkbox" ' + chk + ' onchange="toggleGeminiKey(' + i + ', this.checked)" style="width:15px;height:15px;cursor:pointer;">'
          + '</label>'
          + '<div style="flex:1;">'
          + '<div style="font-weight:600;font-size:13px;' + (!k.enabled ? 'opacity:.45;' : '') + '">' + _esc(k.label) + ' <span class="text-muted" style="font-weight:400;">' + _esc(k.masked_key) + '</span></div>'
          + '<div class="text-sm text-muted">' + (k.enabled ? status : '<span style="color:var(--text3);">Disabled</span>') + ' · ' + k.total_calls + ' calls · ' + k.total_429s + ' 429s' + (k.rpm ? ' \xb7 ' + k.rpm + ' rpm' : '') + (k.avg_latency_ms ? ' \xb7 ' + k.avg_latency_ms + 'ms' : '') + (k.consecutive_429s > 0 ? ' \xb7 <span style="color:var(--danger)">backoff x' + k.consecutive_429s + '</span>' : '') + (k.tokens_used ? ' \xb7 ' + k.tokens_used + ' tok' : '') + pins + '</div>'
          + '</div>'
          + '<button class="btn btn-outline btn-sm" style="font-size:11px;" onclick="removeGeminiKey(' + i + ')">Remove</button>'
          + '</div>';
      }).join('');
    } catch (e) {
      keysEl.innerHTML = '<div class="text-sm" style="color:var(--danger);">Failed to load pool</div>';
    }
  }

  async function addGeminiKey() {
    const keyEl = document.getElementById('gemini-new-key');
    const labelEl = document.getElementById('gemini-new-label');
    const key = (keyEl?.value || '').trim();
    const label = (labelEl?.value || '').trim();
    if (!key) {
      toast('Enter an API key', 'err');
      return;
    }
    try {
      await configApi.addGeminiKey({ key, label });
      keyEl.value = '';
      labelEl.value = '';
      toast('Key added');
      loadGeminiPool();
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function removeGeminiKey(index) {
    if (!confirm('Remove this API key from the pool?')) return;
    try {
      await configApi.removeGeminiKey(index);
      toast('Key removed');
      loadGeminiPool();
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function toggleGeminiKey(index, enabled) {
    try {
      await configApi.toggleGeminiKey({ index, enabled });
      loadGeminiPool();
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function loadConfig() {
    try {
      const d = await configApi.getConfig();
      configState.lastConfigValues = d.values || {};
      _configMeta = d.fields || {};
      const grid = document.getElementById('config-fields');
      if (!grid) return;
      grid.innerHTML = '';
      const _vals = d.values || {};
      const _llmProv = _vals.LLM_PROVIDER || 'ollama';
      const _llmModel = _llmProv === 'ollama' ? (_vals.OLLAMA_MODEL || '').split(':')[0] : (_vals.CLOUD_MODEL || '');
      const _llmEl = document.getElementById('cfg-summary-llm');
      if (_llmEl) _llmEl.textContent = _llmProv + (_llmModel ? ' / ' + _llmModel : '');
      const _ttsEl = document.getElementById('cfg-summary-tts');
      if (_ttsEl) _ttsEl.textContent = _vals.TTS_PROVIDER || 'piper';

      const fieldToCategory = {};
      for (const [cat, keys] of Object.entries(_CONFIG_CATEGORIES)) {
        for (const key of keys) fieldToCategory[key] = cat;
      }

      const categoryFields = {};
      for (const cat of Object.keys(_CONFIG_CATEGORIES)) categoryFields[cat] = [];
      const uncategorized = [];

      for (const [key, [label, sensitive]] of Object.entries(_configMeta)) {
        const val = (d.values || {})[key] || '';
        const cat = fieldToCategory[key];
        const entry = { key, label, sensitive, val };
        if (cat) categoryFields[cat].push(entry);
        else uncategorized.push(entry);
      }

      function buildField(entry) {
        const { key, label, sensitive, val } = entry;
        const defaults = {
          HOST: '0.0.0.0', PORT: '8000', LOG_LEVEL: 'INFO',
          HA_URL: 'http://homeassistant.local:8123',
          LLM_PROVIDER: 'ollama', OLLAMA_URL: 'http://localhost:11434',
          OLLAMA_MODEL: 'llama3.1:8b-instruct-q4_K_M',
          OLLAMA_VISION_MODEL: 'llama3.2-vision:11b-instruct-q4_K_M',
          CLOUD_MODEL: 'gemini-2.5-flash',
          WHISPER_MODEL: 'small', TTS_PROVIDER: 'piper',
          PIPER_VOICE: 'en_US-lessac-medium', AFROTTS_VOICE: 'af_heart', AFROTTS_SPEED: '1.0',
          INTRON_AFRO_TTS_URL: 'http://127.0.0.1:8021', INTRON_AFRO_TTS_TIMEOUT_S: '90',
          INTRON_AFRO_TTS_LANGUAGE: 'en',
          TTS_ENGINE: 'tts.google_translate_en_com', SPEAKER_AUDIO_OFFSET_MS: '0',
          MOTION_CLIP_DURATION_S: '8', MOTION_CLIP_SEARCH_CANDIDATES: '120',
          MOTION_CLIP_SEARCH_RESULTS: '24', MOTION_VISION_PROVIDER: 'gemini',
          HEATING_LLM_PROVIDER: 'gemini', HEATING_SHADOW_ENABLED: 'true',
          PROACTIVE_ENTITY_COOLDOWN_S: '600', PROACTIVE_CAMERA_COOLDOWN_S: '600',
          PROACTIVE_GLOBAL_MOTION_COOLDOWN_S: '600', PROACTIVE_GLOBAL_ANNOUNCE_COOLDOWN_S: '300',
          PROACTIVE_QUEUE_DEDUP_COOLDOWN_S: '120', PROACTIVE_BATCH_WINDOW_S: '60',
          PROACTIVE_MAX_BATCH_CHANGES: '20', PROACTIVE_WEATHER_COOLDOWN_S: '3600',
          PROACTIVE_FORECAST_HOUR: '7', HA_POWER_ALERT_COOLDOWN_S: '1800',
          MOTION_CLIP_RETENTION_DAYS: '30',
          SESSION_RATE_LIMIT_MAX: '30', SESSION_RATE_LIMIT_WINDOW_S: '60',
          MUSIC_ASSISTANT_URL: 'http://localhost:8095',
          BLUEIRIS_URL: '',
          CODEPROJECT_AI_URL: '',
        };
        const ph = defaults[key] ? ` placeholder="default: ${defaults[key]}"` : '';
        const div = document.createElement('div');
        div.className = 'field';
        let input;
        if (_FIELD_OPTIONS[key]) {
          input = _buildSelect(key, val, _FIELD_OPTIONS[key]);
        } else if (key === 'OLLAMA_MODEL') {
          input = '<div id="ollama-model-wrapper"><input type="text" id="cfg-OLLAMA_MODEL" data-key="OLLAMA_MODEL" value="' + esc(val) + '" placeholder="e.g. qwen2.5:7b"></div>';
        } else if (key === 'CLOUD_MODEL') {
          const provider = (d.values || {}).LLM_PROVIDER || 'ollama';
          const models = _CLOUD_MODELS[provider] || [];
          let inner;
          if (models.length) {
            const selected = models.includes(val) ? val : models[0];
            inner = _buildSelect(key, selected, models);
          } else {
            inner = `<input type="text" id="cfg-${key}" data-key="${key}" value="${esc(val)}" placeholder="e.g. llama3.1:8b-instruct-q4_K_M">`;
          }
          input = `<div id="cloud-model-wrapper">${inner}</div>`;
        } else if (sensitive) {
          input = `<div class="input-reveal"><input type="password" id="cfg-${key}" data-key="${key}" value="${esc(val)}"${ph} autocomplete="off"><button onclick="toggleReveal('cfg-${key}',this)" tabindex="-1">&#128065;</button></div>`;
        } else {
          input = `<input type="text" id="cfg-${key}" data-key="${key}" value="${esc(val)}"${ph}>`;
        }
        div.innerHTML = `<label for="cfg-${key}">${label}</label>${input}`;
        return div;
      }

      for (const [cat, fields] of Object.entries(categoryFields)) {
        if (!fields.length) continue;
        const group = document.createElement('div');
        group.className = 'collapsible-group';
        group.dataset.expanded = 'false';
        group.innerHTML = `
          <button class="collapsible-header" onclick="toggleCollapsible(this)" aria-expanded="false">
            <span>${cat}</span>
            <span class="badge badge-muted">${fields.length}</span>
            <svg class="collapsible-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="collapsible-body"><div class="config-grid"></div></div>
        `;
        const innerGrid = group.querySelector('.config-grid');
        for (const entry of fields) innerGrid.appendChild(buildField(entry));
        grid.appendChild(group);
      }

      if (uncategorized.length) {
        const group = document.createElement('div');
        group.className = 'collapsible-group';
        group.dataset.expanded = 'false';
        group.innerHTML = `
          <button class="collapsible-header" onclick="toggleCollapsible(this)" aria-expanded="false">
            <span>Other</span>
            <span class="badge badge-muted">${uncategorized.length}</span>
            <svg class="collapsible-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="collapsible-body"><div class="config-grid"></div></div>
        `;
        const innerGrid = group.querySelector('.config-grid');
        for (const entry of uncategorized) innerGrid.appendChild(buildField(entry));
        grid.appendChild(group);
      }

      const providerEl = document.getElementById('cfg-LLM_PROVIDER');
      if (providerEl) {
        providerEl.addEventListener('change', () => { _updateCloudModelDropdown(providerEl.value); });
      }
      if (providerEl) _updateCloudModelDropdown(providerEl.value, (d.values || {}).CLOUD_MODEL || '');

      await _fetchOllamaModels();
      _updateOllamaModelDropdown((d.values || {}).OLLAMA_MODEL || '');

      if (providerEl) {
        providerEl.addEventListener('change', async () => {
          if (providerEl.value === 'ollama') {
            await _fetchOllamaModels();
            _updateOllamaModelDropdown();
          }
        });
      }

      const ttsProviderEl = document.getElementById('cfg-TTS_PROVIDER');
      if (ttsProviderEl) {
        ttsProviderEl.addEventListener('change', async () => {
          _updateTTSFields(ttsProviderEl.value);
          if (ttsProviderEl.value === 'intron_afro_tts') {
            await _fetchIntronVoices();
            const refVal = (d.values || {}).INTRON_AFRO_TTS_REFERENCE_WAV || '';
            _updateIntronVoiceDropdown(refVal);
          }
        });
        _updateTTSFields(ttsProviderEl.value);
        if (ttsProviderEl.value === 'intron_afro_tts') {
          await _fetchIntronVoices();
          const refVal = (d.values || {}).INTRON_AFRO_TTS_REFERENCE_WAV || '';
          _updateIntronVoiceDropdown(refVal);
        }
      }
    } catch (e) {
      toast('Failed to load config: ' + e.message, 'err');
    }
  }

  async function saveConfig() {
    const values = {};
    document.querySelectorAll('#config-fields [data-key]').forEach(el => {
      values[el.dataset.key] = el.value;
    });
    try {
      await configApi.saveConfig({ values });
      toast('Configuration saved', 'ok');
    } catch (e) {
      toast('Save failed: ' + e.message, 'err');
    }
  }

  function syncRoomSlugFromLabel() {
    const label = document.getElementById('room-new-label');
    const idEl = document.getElementById('room-new-id');
    if (!label || !idEl) return;
    const slug = label.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    if (!idEl.dataset.manualEdit) idEl.value = slug;
  }

  function bindConfigEvents() {
    document.getElementById('btn-save-config')?.addEventListener('click', () => saveConfig());
    document.getElementById('btn-load-config')?.addEventListener('click', () => loadConfig());
    document.getElementById('btn-gemini-add-key')?.addEventListener('click', () => addGeminiKey());
    document.getElementById('btn-gemini-refresh')?.addEventListener('click', () => loadGeminiPool());
    document.getElementById('btn-save-vision-cameras')?.addEventListener('click', () => saveVisionCameras());
    document.getElementById('btn-refresh-vision-cameras')?.addEventListener('click', () => loadVisionCameras());
    document.getElementById('btn-add-room')?.addEventListener('click', () => addRoom());
    document.getElementById('room-new-label')?.addEventListener('input', syncRoomSlugFromLabel);
    document.getElementById('room-new-id')?.addEventListener('input', function onRoomIdInput() {
      this.dataset.manualEdit = this.value ? '1' : '';
    });
    document.getElementById('room-new-label')?.addEventListener('keydown', e => {
      if (e.key === 'Enter') addRoom();
    });
    document.getElementById('room-new-id')?.addEventListener('keydown', e => {
      if (e.key === 'Enter') addRoom();
    });
  }

  async function enterConfigSection() {
    await Promise.allSettled([
      loadConfig(),
      loadGeminiPool(),
      loadVisionCameras(),
      loadRooms(),
    ]);
  }

  bindConfigEvents();

  window.registerAdminSection?.('config', {
    onEnter: enterConfigSection,
  });

  Object.assign(window, {
    loadVisionCameras,
    saveVisionCameras,
    loadRooms,
    updateRoomGlb,
    copyRoomUrl,
    addRoom,
    deleteRoom,
    loadGeminiPool,
    addGeminiKey,
    removeGeminiKey,
    toggleGeminiKey,
    loadConfig,
    saveConfig,
  });
})();
