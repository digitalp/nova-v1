'use strict';


let _currentRole   = 'viewer';
let _changePwTarget = null;
let _motionClips = [];
let _motionAllClips = [];
let _motionSearchMode = 'recent';
let _motionQuickQuery = '';
let _motionActiveCamera = '';
let _motionActiveEventType = '';
let _motionGroupMode = 'day';
let _motionModalIndex = -1;
let _motionModalHistoryIndex = -1;
let _eventHistoryItems = [];
let _eventHistoryKind = '';
let _eventHistoryEventType = '';
let _eventHistorySource = '';
let _eventHistoryStatus = '';
let _eventHistoryWindow = '24h';
let _eventHistoryBeforeTs = '';
let _eventHistoryGroupMode = 'time';
let _eventHistoryPreset = '';
let _eventHistoryQuery = '';
const _MOTION_CAMERA_LABELS = {};
// Camera labels auto-populated from clip data — no hardcoded entity IDs.

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  try {
    const me = await api('GET', '/admin/me');
    _currentRole = me.role;
    const displayName = me.username || '?';
    document.getElementById('user-info').textContent = me.username || '—';
    document.getElementById('user-role-display').textContent = me.role || '—';
    document.getElementById('user-avatar-initials').textContent = displayName.charAt(0).toUpperCase();
    if (me.role === 'admin') {
      document.querySelectorAll('.admin-only').forEach(el => el.style.display = '');
      document.getElementById('restart-btn').style.display = '';
    } else {
      document.getElementById('restart-btn').style.display = 'none';
      ['config','prompt','acl'].forEach(sec => {
        const el = document.querySelector(`.nav-item[data-section="${sec}"]`);
        if (el) el.style.display = 'none';
      });
    }
  } catch {
    window.location.href = '/admin/login';
    return;
  }
  loadDashboard();
  pollHealth();
  setInterval(pollHealth, 15000);
}

async function logout() {
  await fetch('/admin/logout', { method: 'POST' }).catch(() => {});
  window.location.href = '/admin/login';
}

init();

// ── Navigation ────────────────────────────────────────────────────────────────

const TITLES = {
  dashboard:'Dashboard', config:'Configuration', speakers:'Speakers', music:'Music', energy:'Energy', prompt:'System Prompt',
  'prompts-tuning':'Prompts & Tuning',
  acl:'ACL Rules', sessions:'Sessions', memory:'Memory',
  avatar:'Avatar', tools:'Tools', users:'Users',
  decisions:'AI Decisions', selfheal:'Self-Heal', motion:'Find Anything',
  costs:'LLM Cost', metrics:'System Metrics', pylog:'Server Logs',
};

const SKIN_LABELS = ['Porcelain','Light','Medium','Dark','Deep'];
let _currentSkinTone = -1;

function selectSkin(index) {
  _currentSkinTone = index;
  document.querySelectorAll('#skin-swatches .skin-swatch').forEach(s => s.classList.remove('selected'));
  const el = document.querySelector(`#skin-swatches .skin-swatch[data-index="${index}"]`);
  if (el) el.classList.add('selected');
  document.getElementById('skin-label').textContent = index < 0 ? 'GLB Default' : (SKIN_LABELS[index] || '—');
}

let _activeAvatarUrl = '';

async function loadAvatarSettings() {
  // Also load background settings after main settings
  try {
    const d = await api('GET', '/admin/avatar-settings');
    selectSkin(d.skin_tone ?? -1);
    selectHair(d.hair_color ?? -1);
    _activeAvatarUrl = d.avatar_url || '';
    document.getElementById('avatar-url').value = _activeAvatarUrl.startsWith('/static/') ? '' : _activeAvatarUrl;
    // Restore background settings into the form
    if (d.bg_type) {
      const radio = document.querySelector('input[name="bg-type"][value="' + d.bg_type + '"]');
      if (radio) { radio.checked = true; if (typeof onBgTypeChange === 'function') onBgTypeChange(); }
    }
    if (d.bg_color) {
      const ci = document.getElementById('bg-color-input');
      const cp = document.getElementById('bg-color-picker');
      if (ci) ci.value = d.bg_color;
      if (cp) cp.value = d.bg_color;
    }
    if (d.bg_image_url) {
      const ii = document.getElementById('bg-image-input');
      if (ii) ii.value = d.bg_image_url;
      if (typeof updateBgImagePreview === 'function') updateBgImagePreview();
    }
  } catch(e) { selectSkin(0); }
  await loadAvatarLibrary();
}

async function loadAvatarLibrary() {
  const grid = document.getElementById('avatar-grid');
  if (!grid) return;
  try {
    const [settings, lib] = await Promise.all([
      api('GET', '/admin/avatar-settings'),
      api('GET', '/admin/avatars'),
    ]);
    _activeAvatarUrl = settings.avatar_url || '';
    grid.innerHTML = '';
    if (!lib.avatars.length) {
      grid.innerHTML = '<div class="text-md text-muted">No avatars found in static/avatars/</div>';
      return;
    }
    for (const filename of lib.avatars) {
      const url = '/static/avatars/' + filename;
      const isDefault = filename === 'brunette.glb';
      const isActive = _activeAvatarUrl === url || (!_activeAvatarUrl && isDefault);
      const label = filename.replace(/\.glb$/i,'').replace(/[-_]/g,' ')
                            .replace(/\b\w/g, c => c.toUpperCase());
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
  } catch(e) {
    grid.innerHTML = '<div class="text-md text-muted">Failed to load library.</div>';
  }
}

async function selectAvatarFile(url) {
  _activeAvatarUrl = url;
  const _bgT = document.querySelector('input[name="bg-type"]:checked')?.value || 'color';
  const _bgC = document.getElementById('bg-color-input')?.value || '';
  const _bgI = document.getElementById('bg-image-input')?.value || '';
  await api('POST', '/admin/avatar-settings', { skin_tone: _currentSkinTone, avatar_url: url, bg_type: _bgT, bg_color: _bgC, bg_image_url: _bgI });
  document.getElementById('avatar-url').value = '';
  await loadAvatarLibrary();
  toast('Avatar selected — reload the avatar page to apply');
}

async function saveSkinTone() {
  const bgType = document.querySelector('input[name="bg-type"]:checked')?.value || 'color';
  const bgColor = document.getElementById('bg-color-input')?.value || '';
  const bgImageUrl = document.getElementById('bg-image-input')?.value || '';
  await api('POST', '/admin/avatar-settings', { skin_tone: _currentSkinTone, avatar_url: _activeAvatarUrl, bg_type: bgType, bg_color: bgColor, bg_image_url: bgImageUrl });
  toast('Skin tone saved');
}

let _currentHairColor = -1;
const HAIR_LABELS = ['Black','Dark Brown','Brown','Auburn','Red','Ginger','Blonde','Platinum','Grey','White'];

function selectHair(index) {
  _currentHairColor = index;
  document.querySelectorAll('#hair-swatches .skin-swatch').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.hair) === index);
  });
  document.getElementById('hair-label').textContent = index < 0 ? 'GLB Default' : (HAIR_LABELS[index] || '—');
}

async function saveHairColor() {
  const bgType = document.querySelector('input[name="bg-type"]:checked')?.value || 'color';
  const bgColor = document.getElementById('bg-color-input')?.value || '';
  const bgImageUrl = document.getElementById('bg-image-input')?.value || '';
  await api('POST', '/admin/avatar-settings', { skin_tone: _currentSkinTone, hair_color: _currentHairColor, avatar_url: _activeAvatarUrl, bg_type: bgType, bg_color: bgColor, bg_image_url: bgImageUrl });
  toast('Hair colour saved');
}

async function saveExternalUrl() {
  const url = document.getElementById('avatar-url').value.trim();
  if (!url) return toast('Enter a URL first');
  _activeAvatarUrl = url;
  await api('POST', '/admin/avatar-settings', { skin_tone: _currentSkinTone, avatar_url: url });
  await loadAvatarLibrary();
  toast('External URL saved — reload the avatar page to apply');
}

async function clearExternalUrl() {
  document.getElementById('avatar-url').value = '';
  const defaultUrl = '/static/avatars/brunette.glb';
  _activeAvatarUrl = defaultUrl;
  const ___bgT = document.querySelector('input[name="bg-type"]:checked')?.value || 'color';
  const ___bgC = document.getElementById('bg-color-input')?.value || '';
  const ___bgI = document.getElementById('bg-image-input')?.value || '';
  await api('POST', '/admin/avatar-settings', { skin_tone: _currentSkinTone, avatar_url: defaultUrl, bg_type: ___bgT, bg_color: ___bgC, bg_image_url: ___bgI });
  await loadAvatarLibrary();
  toast('Reverted to library selection');
}

async function uploadAvatar(input) {
  const file = input.files[0];
  if (!file) return;
  const status = document.getElementById('avatar-upload-status');
  const sizeMB = (file.size / 1024 / 1024).toFixed(1);
  status.style.color = '';
  status.textContent = `Uploading ${file.name} (${sizeMB} MB)… 0%`;
  const fd = new FormData();
  fd.append('file', file);
  try {
    // Use XHR for upload progress
    const d = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/admin/avatars/upload');
      xhr.withCredentials = true;
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const pct = Math.round(e.loaded / e.total * 100);
          status.textContent = pct < 100
            ? `⬆ Uploading ${sizeMB} MB… ${pct}%`
            : '🔧 Optimizing avatar — fixing skeleton, transferring blendshapes, compressing textures…';
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText));
        } else {
          reject(new Error(xhr.responseText || xhr.statusText));
        }
      };
      xhr.onerror = () => reject(new Error('Network error'));
      xhr.send(fd);
    });
    input.value = '';
    const fix = d.fix || {};
    if (fix.actions && fix.actions.length) {
      status.style.color = 'var(--green, #10b981)';
      status.innerHTML = '✅ Avatar optimized:<br>' + fix.actions.map(a => '  • ' + a).join('<br>');
      toast('Avatar optimized: ' + fix.actions.length + ' fixes applied');
    } else if (fix.error) {
      status.style.color = 'var(--warning, #f59e0b)';
      status.textContent = '⚠ Uploaded but fix failed: ' + fix.error;
    } else {
      status.textContent = '✅ Uploaded — no fixes needed';
      toast('Uploaded: ' + d.uploaded);
    }
    await loadAvatarLibrary();
  } catch(e) {
    status.style.color = 'var(--danger, #ef4444)';
    status.textContent = '❌ Upload failed: ' + e.message;
  }
}

async function deleteAvatar(filename) {
  if (!confirm('Delete ' + filename + '?')) return;
  try {
    await api('DELETE', '/admin/avatars/' + filename);
    if (_activeAvatarUrl === '/static/avatars/' + filename) {
      _activeAvatarUrl = '/static/avatars/brunette.glb';
      const __bgT = document.querySelector('input[name="bg-type"]:checked')?.value || 'color';
  const __bgC = document.getElementById('bg-color-input')?.value || '';
  const __bgI = document.getElementById('bg-image-input')?.value || '';
  await api('POST', '/admin/avatar-settings', { skin_tone: _currentSkinTone, avatar_url: _activeAvatarUrl, bg_type: __bgT, bg_color: __bgC, bg_image_url: __bgI });
    }
    toast('Deleted ' + filename);
    await loadAvatarLibrary();
  } catch(e) { toast('Delete failed: ' + e.message); }
}

async function saveAvatarSettings() { await saveSkinTone(); }
async function resetAvatarSettings() { await clearExternalUrl(); }

function toggleSidebar() {
  const open = document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebar-overlay').classList.toggle('show', open);
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');

// ── Intron Afro TTS toggle ───────────────────────────────────────────────────
async function checkIntronStatus() {
  try {
    const r = await api('GET', '/admin/intron-tts/status');
    const el = document.getElementById('intron-status');
    const btn = document.getElementById('intron-toggle');
    if (el) el.textContent = r.running ? '● Running' : '○ Stopped';
    if (el) el.style.color = r.running ? '#4ade80' : '#64748b';
    if (btn) { btn.textContent = r.running ? 'Stop' : 'Start'; btn.dataset.running = r.running; }
  } catch {}
}
async function toggleIntronTTS() {
  const btn = document.getElementById('intron-toggle');
  const running = btn?.dataset.running === 'true';
  btn.textContent = running ? 'Stopping...' : 'Starting...';
  try {
    await api('POST', '/admin/intron-tts/toggle', { enable: !running });
    toast(running ? 'Intron TTS stopped' : 'Intron TTS started', 'ok');
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
  setTimeout(checkIntronStatus, 2000);
}
  document.getElementById('sidebar-overlay').classList.remove('show');
}

function navigate(el) {
  closeSidebar();
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  el.classList.add('active');
  const sec = el.dataset.section;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.getElementById('section-' + sec).classList.add('active');
  document.getElementById('section-title').textContent = TITLES[sec] || sec;
  if (sec === 'config')    { loadConfig(); loadGeminiPool(); loadVisionCameras(); loadRooms(); }
  if (sec === 'prompt')    loadPrompt();
  if (sec === 'prompts-tuning') loadPromptsTuning();
  if (sec === 'acl')       loadAcl();
  if (sec === 'avatar')    loadAvatarSettings();
  if (sec === 'sessions')  loadSessions();
  if (sec === 'memory')    loadMemory();
  if (sec === 'dashboard') loadDashboard();
  if (sec === 'users')     loadUsers();
  if (sec === 'speakers')  loadSpeakerConfig();
  if (sec === 'music')     loadMusicPlayers();
  if (sec === 'energy')    { loadEnergy(); if (!window._energyInterval) window._energyInterval = setInterval(loadEnergy, 15000); }
  if (sec !== 'energy' && window._energyInterval) { clearInterval(window._energyInterval); window._energyInterval = null; }
  if (sec === 'pylog')     initPylog();
  if (sec === 'motion')    faInit();
  if (sec === 'tools')   { loadWakeStatus(); loadAnnouncementLog(); loadHeatingShadow(); }
  if (sec === 'selfheal') loadSelfHeal();
  if (sec === 'faces')    loadFaces();
  if (sec === 'scoreboard') loadScoreboard();
}

function refreshSection() {
  const active = document.querySelector('.nav-item.active');
  if (active) navigate(active);
}

// ── Toast ─────────────────────────────────────────────────────────────────────

function toast(msg, type='info', duration=4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 0.3s'; setTimeout(() => el.remove(), 300); }, duration);
}

// ── API helpers ───────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: 'include' };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (r.status === 401) { window.location.href = '/admin/login'; throw new Error('Session expired'); }
  if (!r.ok) { const t = await r.text(); throw new Error(t || r.statusText); }
  return r.json();
}

// ── Motion archive / Find Anything ───────────────────────────────────────────

function _motionFilters() {
  const cameraField = document.getElementById('motion-camera');
  const cameraValue = cameraField?.value.trim() || _motionActiveCamera || '';
  const eventTypeField = document.getElementById('motion-event-type');
  const eventTypeValue = eventTypeField?.value.trim() || _motionActiveEventType || '';
  return {
    query: document.getElementById('motion-query')?.value.trim() || '',
    date: document.getElementById('motion-date')?.value || undefined,
    start_time: document.getElementById('motion-start')?.value || undefined,
    end_time: document.getElementById('motion-end')?.value || undefined,
    camera_entity_id: cameraValue || undefined,
    canonical_event_type: eventTypeValue || undefined,
  };
}

function _fmtMotionTs(ts) {
  if (!ts) return '—';
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleString();
}

function _motionModeLabel(mode) {
  return mode === 'ai' ? 'AI-ranked results'
    : mode === 'keyword' ? 'Keyword fallback results'
    : 'Recent Nova-indexed motion clips';
}

function _motionHumanMode(mode) {
  return mode === 'ai' ? 'AI Ranked'
    : mode === 'keyword' ? 'Keyword'
    : 'Recent';
}

function _motionRelative(ts) {
  if (!ts) return 'Unknown';
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  const delta = Math.max(0, Date.now() - date.getTime());
  const mins = Math.floor(delta / 60000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function _motionGroupLabel(ts) {
  if (!ts) return 'Unknown';
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return 'Unknown';
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startClip = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const diffDays = Math.round((startToday - startClip) / 86400000);
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  return date.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
}

function _motionCameraLabel(cameraId) {
  if (!cameraId) return 'Unknown Camera';
  return _MOTION_CAMERA_LABELS[cameraId] || cameraId.replace(/^camera\./, '').replace(/_/g, ' ');
}

function _motionEventTypeLabel(eventType) {
  if (!eventType) return 'All Event Types';
  return String(eventType).replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function _motionSourceLabel(source) {
  if (!source) return 'Unknown Source';
  return String(source).replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function _motionStatusLabel(status) {
  if (!status) return 'Unknown Status';
  return String(status).replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function _eventHistoryKindLabel(kind) {
  if (!kind) return 'All Kinds';
  return String(kind).replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function _motionGroupHeading(clip) {
  if (_motionGroupMode === 'event_type') {
    return _motionEventTypeLabel(clip.canonical_event_type || 'uncategorized');
  }
  if (_motionGroupMode === 'source') {
    return _motionSourceLabel(clip.event_source || 'unknown_source');
  }
  if (_motionGroupMode === 'status') {
    return _motionStatusLabel(clip.status || 'unknown_status');
  }
  return _motionGroupLabel(clip.ts);
}

function _motionGroupCountLabel(clips) {
  if (_motionGroupMode === 'event_type' || _motionGroupMode === 'source' || _motionGroupMode === 'status') {
    const latest = clips[0]?.ts ? _motionGroupLabel(clips[0].ts) : 'Unknown';
    return `${clips.length} event${clips.length === 1 ? '' : 's'} · latest ${latest}`;
  }
  return `${clips.length} event${clips.length === 1 ? '' : 's'}`;
}

function _motionHighlight(text) {
  const raw = String(text || '');
  const escaped = _esc(raw);
  const query = (_motionQuickQuery || '').trim();
  if (!query) return escaped;
  const terms = Array.from(new Set(query.split(/\s+/).map(t => t.trim()).filter(t => t.length >= 3)));
  if (!terms.length) return escaped;
  let html = escaped;
  terms.forEach(term => {
    const safe = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    html = html.replace(new RegExp(`(${safe})`, 'gi'), '<span class="motion-mark">$1</span>');
  });
  return html;
}

function setMotionPreset(query) {
  const input = document.getElementById('motion-query');
  if (input) input.value = query || '';
  _motionQuickQuery = query || '';
  searchMotionClips();
}

async function loadEventHistory() {
  try {
    const params = new URLSearchParams({ limit: '20' });
    if (_eventHistoryQuery) params.set('query', _eventHistoryQuery);
    if (_eventHistoryKind) params.set('kind', _eventHistoryKind);
    if (_eventHistoryEventType) params.set('event_type', _eventHistoryEventType);
    if (_eventHistorySource) params.set('event_source', _eventHistorySource);
    if (_eventHistoryStatus) params.set('status', _eventHistoryStatus);
    if (_eventHistoryWindow) params.set('window', _eventHistoryWindow);
    if (_eventHistoryBeforeTs) params.set('before_ts', _eventHistoryBeforeTs);
    const d = await api('GET', `/admin/event-history?${params.toString()}`);
    _eventHistoryItems = d.events || [];
    _eventHistoryBeforeTs = d.next_before_ts || _eventHistoryBeforeTs;
    await loadEventHistoryWorkflowStatus();
    await loadEventHistoryWorkflowSummary();
    renderEventHistoryFilters();
    renderMotionHistory();
  } catch (e) {
    const root = document.getElementById('motion-history-list');
    if (root) root.innerHTML = `<div class="motion-empty">${_esc(e.message || 'Failed to load event history.')}</div>`;
  }
}

async function loadEventHistoryWorkflowSummary() {
  try {
    const summary = await api('GET', '/admin/event-history/workflow-summary?limit=6');
    renderEventHistoryWorkflowSummary(summary || {});
  } catch (e) {
    renderEventHistoryWorkflowSummary({ error: e.message || 'Failed to load queue.' });
  }
}

function renderEventHistoryWorkflowSummary(summary) {
  const root = document.getElementById('motion-history-open-loop');
  if (!root) return;
  const counts = summary?.counts || {};
  const automation = window._eventHistoryWorkflowStatus || {};
  const reminderDue = Number(counts.reminder_due || 0);
  const escalationDue = Number(counts.escalation_due || 0);
  const stale = Number(counts.stale || 0);
  const topReminder = summary?.next_actions?.reminder_due?.[0];
  const topEscalation = summary?.next_actions?.escalation_due?.[0];
  if (summary?.error) {
    root.innerHTML = `<div class="motion-empty" class="card-flush">${_esc(summary.error)}</div>`;
    return;
  }
  const chips = [
    `<div class="motion-stat"><strong>${reminderDue}</strong> reminders due</div>`,
    `<div class="motion-stat"><strong>${escalationDue}</strong> escalations due</div>`,
    `<div class="motion-stat"><strong>${stale}</strong> stale incidents</div>`,
  ];
  if (automation.last_run_summary) {
    chips.push(`<div class="motion-stat">Auto run: <strong>${Number(automation.last_run_summary.applied || 0)}</strong> applied</div>`);
  }
  if (topEscalation?.title) {
    chips.push(`<div class="motion-stat">Top escalation: <strong>${_esc(topEscalation.title)}</strong></div>`);
  } else if (topReminder?.title) {
    chips.push(`<div class="motion-stat">Next reminder: <strong>${_esc(topReminder.title)}</strong></div>`);
  }
  root.innerHTML = chips.join('');
}

async function loadEventHistoryWorkflowStatus() {
  try {
    window._eventHistoryWorkflowStatus = await api('GET', '/admin/event-history/workflow-status');
  } catch {
    window._eventHistoryWorkflowStatus = null;
  }
}

async function runEventHistoryWorkflow({ includeReminders = true, includeEscalations = true } = {}) {
  try {
    const result = await api('POST', '/admin/event-history/workflow-run', {
      include_reminders: !!includeReminders,
      include_escalations: !!includeEscalations,
      limit: 6,
      dry_run: false,
    });
    const applied = Array.isArray(result.applied) ? result.applied.length : 0;
    toast(applied ? `Applied ${applied} workflow action${applied === 1 ? '' : 's'}` : 'No due workflow actions');
    await loadEventHistory();
  } catch (e) {
    toast('Workflow run failed: ' + e.message, 'err');
  }
}

function setEventHistoryWindow(value) {
  _eventHistoryWindow = value || '24h';
  _eventHistoryBeforeTs = '';
  loadEventHistory();
}

function handleEventHistorySearchKey(evt) {
  if (evt.key !== 'Enter') return;
  evt.preventDefault();
  applyEventHistorySearch();
}

function applyEventHistorySearch() {
  const field = document.getElementById('event-history-query');
  _eventHistoryQuery = field?.value.trim() || '';
  _eventHistoryPreset = '';
  const preset = document.getElementById('event-history-preset');
  if (preset) preset.value = '';
  _eventHistoryBeforeTs = '';
  loadEventHistory();
}

function applyEventHistoryPreset(value) {
  _eventHistoryPreset = value || '';
  const query = document.getElementById('event-history-query');
  const windowField = document.getElementById('event-history-window');
  const kind = document.getElementById('event-history-kind');
  const type = document.getElementById('event-history-type');
  const source = document.getElementById('event-history-source');
  const status = document.getElementById('event-history-status');
  const group = document.getElementById('event-history-group');

  _eventHistoryQuery = '';
  _eventHistoryWindow = '24h';
  _eventHistoryKind = '';
  _eventHistoryEventType = '';
  _eventHistorySource = '';
  _eventHistoryStatus = '';
  _eventHistoryGroupMode = 'time';

  if (_eventHistoryPreset === 'needs_attention') {
    _eventHistoryStatus = 'active';
    _eventHistoryGroupMode = 'status';
  } else if (_eventHistoryPreset === 'deliveries') {
    _eventHistoryQuery = 'delivery package parcel';
    _eventHistoryGroupMode = 'event_type';
  } else if (_eventHistoryPreset === 'door_events') {
    _eventHistoryQuery = 'doorbell visitor door';
    _eventHistoryGroupMode = 'source';
  } else if (_eventHistoryPreset === 'motion_review') {
    _eventHistoryKind = 'motion_clip';
    _eventHistoryGroupMode = 'time';
  }

  if (query) query.value = _eventHistoryQuery;
  if (windowField) windowField.value = _eventHistoryWindow;
  if (kind) kind.value = _eventHistoryKind;
  if (type) type.value = _eventHistoryEventType;
  if (source) source.value = _eventHistorySource;
  if (status) status.value = _eventHistoryStatus;
  if (group) group.value = _eventHistoryGroupMode;
  _eventHistoryBeforeTs = '';
  loadEventHistory();
}

function renderEventHistoryFilters() {
  const kinds = new Map();
  const types = new Map();
  const sources = new Map();
  const statuses = new Map();
  (_eventHistoryItems || []).forEach(item => {
    const kind = String(item.kind || '').trim();
    const type = String(item.event_type || '').trim();
    const source = String(item.event_source || '').trim();
    const status = String(item.status || '').trim();
    if (kind) kinds.set(kind, (kinds.get(kind) || 0) + 1);
    if (type) types.set(type, (types.get(type) || 0) + 1);
    if (source) sources.set(source, (sources.get(source) || 0) + 1);
    if (status) statuses.set(status, (statuses.get(status) || 0) + 1);
  });
  const fill = (id, current, entries, labelFn, emptyLabel) => {
    const select = document.getElementById(id);
    if (!select) return;
    select.innerHTML = `<option value="">${emptyLabel}</option>`;
    Array.from(entries.entries()).sort((a, b) => b[1] - a[1]).forEach(([value, count]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = `${labelFn(value)} (${count})`;
      if (value === current) option.selected = true;
      select.appendChild(option);
    });
  };
  fill('event-history-kind', _eventHistoryKind, kinds, _eventHistoryKindLabel, 'All Kinds');
  fill('event-history-type', _eventHistoryEventType, types, _motionEventTypeLabel, 'All Event Types');
  fill('event-history-source', _eventHistorySource, sources, _motionSourceLabel, 'All Sources');
  fill('event-history-status', _eventHistoryStatus, statuses, _motionStatusLabel, 'All Statuses');
}

function setEventHistoryKind(value) {
  _eventHistoryKind = value || '';
  _eventHistoryPreset = '';
  const preset = document.getElementById('event-history-preset');
  if (preset) preset.value = '';
  _eventHistoryBeforeTs = '';
  loadEventHistory();
}

function setEventHistoryEventType(value) {
  _eventHistoryEventType = value || '';
  _eventHistoryPreset = '';
  const preset = document.getElementById('event-history-preset');
  if (preset) preset.value = '';
  _eventHistoryBeforeTs = '';
  loadEventHistory();
}

function setEventHistorySource(value) {
  _eventHistorySource = value || '';
  _eventHistoryPreset = '';
  const preset = document.getElementById('event-history-preset');
  if (preset) preset.value = '';
  _eventHistoryBeforeTs = '';
  loadEventHistory();
}

function setEventHistoryStatus(value) {
  _eventHistoryStatus = value || '';
  _eventHistoryPreset = '';
  const preset = document.getElementById('event-history-preset');
  if (preset) preset.value = '';
  _eventHistoryBeforeTs = '';
  loadEventHistory();
}

function setEventHistoryGroup(value) {
  _eventHistoryGroupMode = ['status', 'event_type', 'source'].includes(value) ? value : 'time';
  _eventHistoryPreset = '';
  const preset = document.getElementById('event-history-preset');
  if (preset) preset.value = '';
  const group = document.getElementById('event-history-group');
  if (group) group.value = _eventHistoryGroupMode;
  renderMotionHistory();
}

function resetEventHistoryFilters() {
  _eventHistoryPreset = '';
  _eventHistoryQuery = '';
  _eventHistoryWindow = '24h';
  _eventHistoryKind = '';
  _eventHistoryEventType = '';
  _eventHistorySource = '';
  _eventHistoryStatus = '';
  _eventHistoryBeforeTs = '';
  const win = document.getElementById('event-history-window');
  const query = document.getElementById('event-history-query');
  const preset = document.getElementById('event-history-preset');
  const kind = document.getElementById('event-history-kind');
  const type = document.getElementById('event-history-type');
  const source = document.getElementById('event-history-source');
  const status = document.getElementById('event-history-status');
  const group = document.getElementById('event-history-group');
  if (preset) preset.value = '';
  if (query) query.value = '';
  if (win) win.value = '24h';
  if (kind) kind.value = '';
  if (type) type.value = '';
  if (source) source.value = '';
  if (status) status.value = '';
  _eventHistoryGroupMode = 'time';
  if (group) group.value = 'time';
  loadEventHistory();
}

function loadOlderEventHistory() {
  if (!_eventHistoryBeforeTs) return;
  loadEventHistory();
}

function resetEventHistoryPaging() {
  _eventHistoryBeforeTs = '';
  loadEventHistory();
}

async function loadMotionClips() {
  try {
    const d = await api('GET', '/admin/motion-clips?limit=60');
    _motionAllClips = d.clips || [];
    _motionClips = _motionFilterByCamera(_motionAllClips);
    _motionSearchMode = 'recent';
    renderMotionCameraRail();
    renderMotionEventTypeSelect();
    renderMotionClips();
    loadEventHistory();
  } catch (e) {
    document.getElementById('motion-grid').innerHTML = `<div class="motion-empty">${_esc(e.message || 'Failed to load motion clips.')}</div>`;
  }
}

async function searchMotionClips() {
  const filters = _motionFilters();
  _motionQuickQuery = filters.query || '';
  const preset = document.getElementById('motion-preset');
  if (preset && preset.value !== _motionQuickQuery) preset.value = '';
  try {
    const d = await api('POST', '/admin/motion-clips/search', filters);
    _motionAllClips = d.clips || [];
    _motionClips = _motionFilterByCamera(_motionAllClips);
    _motionSearchMode = d.mode || 'recent';
    renderMotionCameraRail();
    renderMotionEventTypeSelect();
    renderMotionClips();
    loadEventHistory();
  } catch (e) {
    toast('Motion search failed: ' + e.message, 'err');
  }
}

function resetMotionFilters() {
  document.getElementById('motion-query').value = '';
  document.getElementById('motion-date').value = '';
  document.getElementById('motion-start').value = '';
  document.getElementById('motion-end').value = '';
  document.getElementById('motion-camera').value = '';
  document.getElementById('motion-event-type').value = '';
  document.getElementById('motion-group-mode').value = 'day';
  document.getElementById('motion-preset').value = '';
  _motionQuickQuery = '';
  _motionActiveCamera = '';
  _motionActiveEventType = '';
  _motionGroupMode = 'day';
  loadMotionClips();
}

function _motionFilterByCamera(clips) {
  let filtered = clips || [];
  if (_motionActiveCamera) {
    filtered = filtered.filter(clip => String(clip.camera_entity_id || '') === _motionActiveCamera);
  }
  if (_motionActiveEventType) {
    filtered = filtered.filter(clip => String(clip.canonical_event_type || '') === _motionActiveEventType);
  }
  return filtered;
}

function setMotionCamera(cameraId) {
  _motionActiveCamera = cameraId || '';
  const cameraField = document.getElementById('motion-camera');
  if (cameraField) cameraField.value = _motionActiveCamera;
  _motionClips = _motionFilterByCamera(_motionAllClips);
  renderMotionCameraRail();
  renderMotionEventTypeSelect();
  renderMotionClips();
}

function setMotionEventType(eventType) {
  _motionActiveEventType = eventType || '';
  const eventTypeField = document.getElementById('motion-event-type');
  if (eventTypeField) eventTypeField.value = _motionActiveEventType;
  _motionClips = _motionFilterByCamera(_motionAllClips);
  renderMotionEventTypeSelect();
  renderMotionClips();
}

function setMotionGroupMode(mode) {
  _motionGroupMode = ['event_type', 'source', 'status'].includes(mode) ? mode : 'day';
  const field = document.getElementById('motion-group-mode');
  if (field) field.value = _motionGroupMode;
  renderMotionClips();
}

function renderMotionCameraRail() {
  const root = document.getElementById('motion-camera-list');
  if (!root) return;
  const counts = new Map();
  (_motionAllClips || []).forEach(clip => {
    const key = String(clip.camera_entity_id || 'unknown');
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const items = [['', _motionAllClips.length], ...Array.from(counts.entries()).sort((a, b) => b[1] - a[1])];
  root.innerHTML = '';
  if (!items.length) {
    root.innerHTML = '<div class="motion-empty" style="padding:18px;">No cameras indexed yet.</div>';
    return;
  }
  items.forEach(([cameraId, count]) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'motion-camera-item' + ((_motionActiveCamera || '') === cameraId ? ' active' : '');
    item.innerHTML = `
      <div class="motion-camera-name">${_esc(cameraId ? _motionCameraLabel(cameraId) : 'All Cameras')}</div>
      <div class="motion-camera-count">${_esc(String(count || 0))}</div>
    `;
    item.onclick = () => setMotionCamera(cameraId);
    root.appendChild(item);
  });
  renderMotionCameraSelect(items);
}

function renderMotionCameraSelect(items) {
  const select = document.getElementById('motion-camera');
  if (!select) return;
  const current = _motionActiveCamera || '';
  select.innerHTML = '<option value="">All Cameras</option>';
  items.forEach(([cameraId, count]) => {
    if (!cameraId) return;
    const option = document.createElement('option');
    option.value = cameraId;
    option.textContent = `${_motionCameraLabel(cameraId)} (${count})`;
    if (cameraId === current) option.selected = true;
    select.appendChild(option);
  });
}

function renderMotionEventTypeSelect() {
  const select = document.getElementById('motion-event-type');
  if (!select) return;
  const current = _motionActiveEventType || '';
  const counts = new Map();
  (_motionAllClips || []).forEach(clip => {
    const key = String(clip.canonical_event_type || '').trim();
    if (!key) return;
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  select.innerHTML = '<option value="">All Event Types</option>';
  Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).forEach(([eventType, count]) => {
    const option = document.createElement('option');
    option.value = eventType;
    option.textContent = `${_motionEventTypeLabel(eventType)} (${count})`;
    if (eventType === current) option.selected = true;
    select.appendChild(option);
  });
}

function renderMotionActivity() {
  const root = document.getElementById('motion-activity-bars');
  const note = document.getElementById('motion-activity-note');
  if (!root) return;
  const now = Date.now();
  const buckets = Array.from({ length: 12 }, (_, idx) => {
    const start = now - ((11 - idx) * 2 * 60 * 60 * 1000);
    return { label: new Date(start).getHours(), start, end: start + (2 * 60 * 60 * 1000), count: 0 };
  });
  (_motionClips || []).forEach(clip => {
    const ts = new Date(clip.ts || '').getTime();
    if (!Number.isFinite(ts)) return;
    buckets.forEach(bucket => {
      if (ts >= bucket.start && ts < bucket.end) bucket.count += 1;
    });
  });
  const max = Math.max(1, ...buckets.map(b => b.count));
  root.innerHTML = '';
  buckets.forEach(bucket => {
    const slot = document.createElement('div');
    slot.className = 'motion-activity-slot';
    const height = Math.max(4, Math.round((bucket.count / max) * 42));
    const label = `${String(bucket.label).padStart(2, '0')}:00`;
    slot.innerHTML = `
      <div class="motion-activity-bar" title="${_esc(label)} · ${_esc(String(bucket.count))} events">
        <div class="motion-activity-fill" style="height:${height}px"></div>
      </div>
      <div class="motion-activity-label">${_esc(label)}</div>
    `;
    root.appendChild(slot);
  });
  if (note) note.textContent = _motionActiveCamera ? `Last 24h · ${_motionCameraLabel(_motionActiveCamera)}` : 'Last 24h across visible results';
}

function _motionInsightCounts(items, valueFn, labelFn) {
  const counts = new Map();
  (items || []).forEach(item => {
    const key = String(valueFn(item) || '').trim();
    if (!key) return;
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
    .map(([key, count]) => ({ key, label: labelFn(key), count }));
}

function _renderMotionInsightChips(rootId, rows) {
  const root = document.getElementById(rootId);
  if (!root) return;
  if (!rows.length) {
    root.innerHTML = '<div class="motion-empty" class="card-flush">No data</div>';
    return;
  }
  root.innerHTML = rows.map(row => (
    `<div class="motion-insight-chip"><strong>${_esc(String(row.count))}</strong>${_esc(row.label)}</div>`
  )).join('');
}

function renderMotionInsights() {
  const note = document.getElementById('motion-insights-note');
  const clips = _motionClips || [];
  _renderMotionInsightChips(
    'motion-insight-types',
    _motionInsightCounts(clips, clip => clip.canonical_event_type, value => _motionEventTypeLabel(value))
  );
  _renderMotionInsightChips(
    'motion-insight-sources',
    _motionInsightCounts(clips, clip => clip.event_source, value => String(value).replaceAll('_', ' '))
  );
  _renderMotionInsightChips(
    'motion-insight-statuses',
    _motionInsightCounts(clips, clip => clip.status || 'ready', value => String(value))
  );
  if (note) {
    note.textContent = clips.length
      ? `${clips.length} visible event${clips.length === 1 ? '' : 's'}`
      : 'No visible events';
  }
}

function renderMotionHistory() {
  const root = document.getElementById('motion-history-list');
  const note = document.getElementById('motion-history-note');
  if (!root) return;
  const events = (_eventHistoryItems || []).slice();
  _renderMotionInsightChips(
    'motion-history-status-mix',
    _motionInsightCounts(events, item => item.status || 'unknown_status', value => _motionStatusLabel(value))
  );
  _renderMotionInsightChips(
    'motion-history-type-mix',
    _motionInsightCounts(events, item => item.event_type || 'event', value => _motionEventTypeLabel(value))
  );
  _renderMotionInsightChips(
    'motion-history-source-mix',
    _motionInsightCounts(events, item => item.event_source || 'unknown_source', value => _motionSourceLabel(value))
  );
  if (!events.length) {
    root.innerHTML = '<div class="motion-empty">No visible events to show.</div>';
    if (note) note.textContent = 'No recent events';
    return;
  }
  root.innerHTML = '';
  const groups = new Map();
  const groupLabel = (item) => {
    if (_eventHistoryGroupMode === 'status') return _motionStatusLabel(item.status || 'unknown_status');
    if (_eventHistoryGroupMode === 'event_type') return _motionEventTypeLabel(item.event_type || 'event');
    if (_eventHistoryGroupMode === 'source') return _motionSourceLabel(item.event_source || 'unknown_source');
    return _motionGroupLabel(item.ts);
  };
  events.forEach(item => {
    const label = groupLabel(item);
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(item);
  });
  groups.forEach((groupItems, label) => {
    const section = document.createElement('div');
    section.className = 'motion-group';
    section.innerHTML = `
      <div class="motion-group-head">
        <div class="motion-group-title">${_esc(label)}</div>
        <div class="motion-group-count">${_esc(String(groupItems.length))} event${groupItems.length === 1 ? '' : 's'}</div>
      </div>
      <div class="motion-history-list"></div>
    `;
    const sectionRoot = section.querySelector('.motion-history-list');
    groupItems.forEach(item => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'motion-history-item';
    const adminNote = String(item.data?.admin_note || '').trim();
    const openLoopNote = String(item.open_loop_note || item.data?.open_loop_note || '').trim();
    row.innerHTML = `
      <div class="motion-history-head">
        <div class="motion-history-title">${_esc(item.title || _motionEventTypeLabel(item.event_type || 'event'))}</div>
        <div class="motion-history-time">${_esc(_motionRelative(item.ts))} · ${_esc(_fmtMotionTs(item.ts))}</div>
      </div>
      <div class="motion-history-meta">
        <div class="motion-chip info">${_esc(_motionEventTypeLabel(item.event_type || 'event'))}</div>
        <div class="motion-chip">${_esc(_motionSourceLabel(item.event_source || 'unknown_source'))}</div>
        <div class="motion-chip">${_esc(_motionStatusLabel(item.status || 'ready'))}</div>
        ${item.camera_entity_id ? `<div class="motion-chip good">${_esc(_motionCameraLabel(item.camera_entity_id))}</div>` : ''}
      </div>
      <div class="motion-history-desc">${_motionHighlight(item.summary || 'No event summary available.')}</div>
      ${adminNote ? `<div class="motion-history-note-inline"><strong>Admin note:</strong> ${_esc(adminNote)}</div>` : ''}
      ${!adminNote && openLoopNote ? `<div class="motion-history-note-inline"><strong>Context:</strong> ${_esc(openLoopNote)}</div>` : ''}
    `;
    row.onclick = () => {
      if (item.clip_id) {
        const clip = (_motionAllClips || []).find(entry => String(entry.id) === String(item.clip_id));
        if (clip) openMotionModal(clip);
        else openEventHistoryModal(item);
      } else {
        openEventHistoryModal(item);
      }
    };
    sectionRoot.appendChild(row);
  });
    root.appendChild(section);
  });
  if (note) note.textContent = `${events.length} most recent cross-event item${events.length === 1 ? '' : 's'}`;
}

function renderMotionClips() {
  const grid = document.getElementById('motion-grid');
  const sub = document.getElementById('motion-results-sub');
  const countChip = document.getElementById('motion-count-chip');
  const countChipBody = document.getElementById('motion-count-chip-body');
  const countHero = document.getElementById('motion-count-hero');
  const modeChip = document.getElementById('motion-mode-chip');
  const modeChipBody = document.getElementById('motion-mode-chip-body');
  const modeHero = document.getElementById('motion-mode-hero');
  const resultsCount = document.getElementById('motion-results-count');
  if (sub) {
    const base = _motionModeLabel(_motionSearchMode);
    const scopes = [];
    if (_motionActiveCamera) scopes.push(_motionCameraLabel(_motionActiveCamera));
    if (_motionActiveEventType) scopes.push(_motionEventTypeLabel(_motionActiveEventType));
    sub.textContent = scopes.length ? `${base} · ${scopes.join(' · ')}` : base;
  }
  if (countChip) countChip.textContent = String(_motionClips.length);
  if (countChipBody) countChipBody.textContent = String(_motionClips.length);
  if (countHero) countHero.textContent = String(_motionClips.length);
  if (modeChip) modeChip.textContent = _motionHumanMode(_motionSearchMode);
  if (modeChipBody) modeChipBody.textContent = _motionHumanMode(_motionSearchMode);
  if (modeHero) modeHero.textContent = _motionHumanMode(_motionSearchMode);
  if (resultsCount) resultsCount.textContent = `${_motionClips.length} result${_motionClips.length === 1 ? '' : 's'}`;
  if (!grid) return;
  grid.innerHTML = '';
  renderMotionActivity();
  renderMotionInsights();
  renderMotionHistory();
  if (!_motionClips.length) {
    const scopes = [];
    if (_motionActiveCamera) scopes.push(`on <strong>${_esc(_motionCameraLabel(_motionActiveCamera))}</strong>`);
    if (_motionActiveEventType) scopes.push(`for <strong>${_esc(_motionEventTypeLabel(_motionActiveEventType))}</strong>`);
    const scope = scopes.length ? ` ${scopes.join(' ')}` : '';
    grid.innerHTML = `<div class="motion-empty">No motion clips matched the current search${_motionQuickQuery ? ` for <strong>${_esc(_motionQuickQuery)}</strong>` : ''}${scope}.</div>`;
    return;
  }
  const groups = new Map();
  _motionClips.forEach((clip) => {
    const key = _motionGroupHeading(clip);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(clip);
  });
  groups.forEach((clips, label) => {
    const group = document.createElement('div');
    group.className = 'motion-group';
    group.innerHTML = `
      <div class="motion-group-head">
        <div class="motion-group-title">${_esc(label)}</div>
        <div class="motion-group-count">${_esc(_motionGroupCountLabel(clips))}</div>
      </div>
      <div class="motion-group-grid"></div>
    `;
    const groupGrid = group.querySelector('.motion-group-grid');
    clips.forEach(clip => {
    const card = document.createElement('div');
    card.className = 'motion-card';
    const status = String(clip.status || 'ready');
    const note = String(clip.extra?.open_loop_note || clip.open_loop_note || '');
    const hasVideo = Boolean(clip.video_url);
    const badgeClass = _motionSearchMode === 'ai' ? 'ai' : 'review';
    const badgeLabel = _motionSearchMode === 'ai' ? 'AI Match' : 'Event';
    const duration = clip.duration_s ? `${clip.duration_s}s` : '—';
    const canonicalType = String(clip.canonical_event_type || '');
    const source = String(clip.event_source || '');
    const coralDetections = typeof _faDedupCoral === 'function' ? _faDedupCoral(clip.extra?.coral_detections) : (Array.isArray(clip.extra?.coral_detections) ? clip.extra.coral_detections : []);
    const coralHasPlate = Boolean(clip.extra?.coral_has_plate);
    const plateNumber = String(clip.extra?.plate_number || '');
    card.innerHTML = `
      <div class="motion-card-media">
        ${hasVideo ? `<video data-src="${clip.video_url}" preload="none" muted playsinline ></video>` : '<div class="motion-card-fallback">Clip unavailable</div>'}
        <div class="motion-card-overlay">
          <div class="motion-card-badge ${badgeClass}">${badgeLabel}</div>
          <div class="motion-card-age">${_esc(_motionRelative(clip.ts))}</div>
        </div>
      </div>
      <div class="motion-card-body">
        <div class="motion-card-head">
          <div>
            <div class="motion-card-title">${_esc(clip.location || clip.camera_entity_id || 'Motion event')}</div>
            <div class="motion-card-camera">${_esc(_motionCameraLabel(clip.camera_entity_id))}</div>
            <div class="motion-card-time">${_esc(_fmtMotionTs(clip.ts))}</div>
          </div>
          <div class="motion-card-id">Clip ${_esc(String(clip.id || '—'))}</div>
        </div>
        <div class="motion-chip-row">
          <div class="motion-chip">${_esc(status)}</div>
          <div class="motion-chip good">${_esc(duration)}</div>
          ${canonicalType ? `<div class="motion-chip info">${_esc(canonicalType.replaceAll('_', ' '))}</div>` : ''}
          ${source ? `<div class="motion-chip">${_esc(source.replaceAll('_', ' '))}</div>` : ''}
          ${_motionSearchMode === 'ai' ? '<div class="motion-chip search">AI match</div>' : ''}
          ${coralDetections.map(d => {
            const parsed = d.match(/^(\w+)\((\d+)%\)$/);
            if (parsed) {
              const score = parseInt(parsed[2], 10);
              const dotColor = score >= 80 ? '#00b894' : score >= 50 ? '#f0a030' : '#ff6b6b';
              return `<div class="motion-chip coral" title="Coral TPU · ${parsed[2]}% confidence"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${dotColor};margin-right:2px;"></span>${_esc(parsed[1])} <span style="color:var(--text3);font-weight:400;">${parsed[2]}%</span></div>`;
            }
            return `<div class="motion-chip coral" title="Coral TPU detection">${_esc(d)}</div>`;
          }).join('')}
          ${plateNumber ? `<div class="motion-chip coral-plate" title="Number plate read by Gemini"><svg viewBox="0 0 24 24" style="width:12px;height:12px;stroke:currentColor;stroke-width:1.8;fill:none;vertical-align:-2px;margin-right:2px;"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 12h12"/></svg>${_esc(plateNumber)}</div>` : (coralHasPlate ? '<div class="motion-chip coral-plate" title="Plate-bearing vehicle detected"><svg viewBox="0 0 24 24" style="width:12px;height:12px;stroke:currentColor;stroke-width:1.8;fill:none;vertical-align:-2px;margin-right:2px;"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 12h12"/></svg>plate</div>' : '')}
        </div>
        <div class="motion-card-desc">${_motionHighlight(clip.description || 'No AI description available.')}</div>
        ${note ? `<div class="motion-card-note">${_esc(note)}</div>` : ''}
        <div class="motion-card-actions">
          <button class="btn btn-primary btn-sm mc-review-btn">Review</button>
          ${hasVideo ? `<a class="btn btn-outline btn-sm" href="${clip.video_url}" target="_blank" rel="noopener">Open File</a>` : ''}
          <button class="btn btn-danger btn-sm mc-delete-btn" title="Delete this clip">Delete</button>
        </div>
      </div>
    `;
    const reviewBtn = card.querySelector('.mc-review-btn');
    if (reviewBtn) reviewBtn.onclick = () => openMotionModal(clip);
    const deleteBtn = card.querySelector('.mc-delete-btn');
    if (deleteBtn) deleteBtn.onclick = (e) => { e.stopPropagation(); _deleteMotionClip(clip.id, card); };
    const video = card.querySelector('video');
    if (video) {
      video.onclick = () => openMotionModal(clip);
      video.addEventListener('mouseenter', () => {
        // Lazy-load on hover if not yet loaded
        if (video.dataset.src && !video.src) {
          video.src = video.dataset.src;
          video.preload = 'metadata';
        }
        video.muted = true;
        video.play().catch(() => {});
      });
      video.addEventListener('mouseleave', () => {
        video.pause();
        video.currentTime = 0;
      });
    }
      groupGrid.appendChild(card);
    });
    grid.appendChild(group);
  });
  _observeVideos(grid);
}

// ── Motion clip deletion ──────────────────────────────────────────────────────

let _motionSelectedIds = new Set();

function _motionSelectAll() {
  _motionSelectedIds = new Set(_motionClips.map(c => String(c.id)));
  document.querySelectorAll('#motion-grid .motion-card').forEach(card => card.classList.add('selected'));
  _updateMotionSelectionUI();
}

function _motionDeselectAll() {
  _motionSelectedIds.clear();
  document.querySelectorAll('#motion-grid .motion-card').forEach(card => card.classList.remove('selected'));
  _updateMotionSelectionUI();
}

function _updateMotionSelectionUI() {
  const count = _motionSelectedIds.size;
  const deselectBtn = document.getElementById('mc-deselect-btn');
  const deleteSelectedBtn = document.getElementById('mc-delete-selected-btn');
  if (deselectBtn) deselectBtn.style.display = count > 0 ? '' : 'none';
  if (deleteSelectedBtn) {
    deleteSelectedBtn.style.display = count > 0 ? '' : 'none';
    deleteSelectedBtn.textContent = `Delete Selected (${count})`;
  }
}

async function _deleteMotionClip(clipId, cardEl) {
  if (!confirm('Delete this clip?')) return;
  try {
    await api('DELETE', `/admin/motion-clips/${clipId}`);
    _motionAllClips = _motionAllClips.filter(c => String(c.id) !== String(clipId));
    _motionClips = _motionClips.filter(c => String(c.id) !== String(clipId));
    _motionSelectedIds.delete(String(clipId));
    cardEl?.remove();
    _updateMotionSelectionUI();
  } catch(e) {
    alert('Delete failed: ' + (e.message || e));
  }
}

async function _deleteSelectedMotionClips() {
  const ids = Array.from(_motionSelectedIds).map(Number);
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} selected clip${ids.length !== 1 ? 's' : ''}? This cannot be undone.`)) return;
  try {
    const r = await api('POST', '/admin/motion-clips/delete', { ids });
    _motionAllClips = _motionAllClips.filter(c => !ids.includes(Number(c.id)));
    _motionClips = _motionClips.filter(c => !ids.includes(Number(c.id)));
    _motionSelectedIds.clear();
    renderMotionClips();
    _updateMotionSelectionUI();
    toast(`Deleted ${r.deleted} clip${r.deleted !== 1 ? 's' : ''}`);
  } catch(e) {
    alert('Delete failed: ' + (e.message || e));
  }
}

async function _deleteAllMotionClips() {
  if (!confirm('Delete ALL motion clips? This permanently removes every clip from the archive and cannot be undone.')) return;
  try {
    const r = await api('POST', '/admin/motion-clips/delete', { delete_all: true });
    _motionAllClips = [];
    _motionClips = [];
    _motionSelectedIds.clear();
    renderMotionClips();
    _updateMotionSelectionUI();
    toast(`Deleted ${r.deleted} clip${r.deleted !== 1 ? 's' : ''}`);
  } catch(e) {
    alert('Delete failed: ' + (e.message || e));
  }
}

// ─────────────────────────────────────────────────────────────────────────────

function openMotionModal(clip) {
  _motionModalIndex = _motionClips.findIndex(item => String(item.id) === String(clip.id));
  _motionModalHistoryIndex = -1;
  _renderMotionModal(clip);
}

function _renderMotionModalActions(item) {
  const root = document.getElementById('motion-modal-actions');
  if (!root) return;
  root.innerHTML = '';
  const addButton = (label, fn) => {
    const button = document.createElement('button');
    button.className = 'btn btn-outline btn-sm';
    button.textContent = label;
    button.onclick = fn;
    root.appendChild(button);
  };
  if (_motionModalHistoryIndex >= 0 && item.event_id) {
    const actions = Array.isArray(item.available_actions) ? item.available_actions : [];
    actions.forEach(action => {
      const label = String(action.label || '').trim();
      if (!label) return;
      addButton(label, () => updateEventHistoryAction(item, action));
    });
  }
  if (item.event_type) {
    addButton('Filter by Type', () => applyMotionArchiveFocus({ eventType: item.event_type }));
  }
  if (item.camera_entity_id) {
    addButton('Filter by Camera', () => applyMotionArchiveFocus({ camera: item.camera_entity_id }));
  }
  if (item.event_source) {
    addButton('History by Source', () => {
      _eventHistorySource = item.event_source || '';
      _eventHistoryBeforeTs = '';
      closeMotionModal();
      loadEventHistory();
    });
  }
  if (!root.childElementCount) {
    root.innerHTML = '<div class="motion-empty" class="card-flush">No drill-down actions available.</div>';
  }
}

function _eventHistoryActionPayload(item, action) {
  const actionId = String(action?.action || '').trim();
  const statusMap = {
    acknowledge: 'acknowledged',
    resolve: 'resolved',
    reopen: 'active',
  };
  const status = statusMap[actionId] || String(item.status || 'active');
  return {
    event_id: item.event_id || '',
    status,
    workflow_action: actionId === 'send_reminder' || actionId === 'escalate_medium' || actionId === 'escalate_high'
      ? actionId
      : null,
    title: item.title || '',
    summary: item.summary || '',
    event_type: item.event_type || '',
    event_source: item.event_source || '',
    camera_entity_id: item.camera_entity_id || '',
  };
}

function _eventHistoryDomainActionPayload(item, action) {
  return {
    session_id: 'admin_event_history',
    event_id: item.event_id || '',
    action: String(action?.action || '').trim(),
    title: item.title || '',
    summary: item.summary || '',
    event_type: item.event_type || '',
    event_source: item.event_source || '',
    camera_entity_id: item.camera_entity_id || '',
    followup_prompt: action?.followup_prompt || null,
    target_camera_entity_id: action?.target_camera_entity_id || null,
    target_event: action?.target_event || null,
    target_title: action?.target_title || null,
    target_message: action?.target_message || null,
  };
}

async function updateEventHistoryAction(item, action) {
  try {
    const actionId = String(action?.action || '').trim();
    if (actionId === 'ask_about_event' || actionId === 'show_related_camera') {
      const followup = document.getElementById('motion-modal-followup');
      if (followup) followup.textContent = actionId === 'ask_about_event' ? 'Thinking…' : 'Opening related camera…';
      const result = await api('POST', '/admin/event-history/domain-action', _eventHistoryDomainActionPayload(item, action));
      if (actionId === 'ask_about_event') {
        if (followup) followup.textContent = result.text || 'No follow-up answer returned.';
        return;
      }
      toast(String(action?.label || 'Domain action'));
      closeMotionModal();
      await loadEventHistory();
      return;
    }
    const noteField = document.getElementById('motion-modal-note');
    const payload = _eventHistoryActionPayload(item, action);
    payload.admin_note = noteField?.value.trim() || '';
    await api('POST', '/admin/event-history/action', payload);
    const actionLabel = String(action?.label || '').trim();
    toast(actionLabel || `Event ${_motionStatusLabel(payload.status).toLowerCase()}`);
    closeMotionModal();
    await loadEventHistory();
  } catch (e) {
    toast('Event update failed: ' + e.message, 'err');
  }
}

function applyMotionArchiveFocus({ camera = '', eventType = '' }) {
  if (camera) {
    _motionActiveCamera = camera;
    const cameraField = document.getElementById('motion-camera');
    if (cameraField) cameraField.value = camera;
  }
  if (eventType) {
    _motionActiveEventType = eventType;
    const eventTypeField = document.getElementById('motion-event-type');
    if (eventTypeField) eventTypeField.value = eventType;
  }
  _motionClips = _motionFilterByCamera(_motionAllClips);
  closeMotionModal();
  renderMotionCameraRail();
  renderMotionEventTypeSelect();
  renderMotionClips();
}

function _renderMotionModal(clip) {
  const modal = document.getElementById('motion-modal');
  const strip = document.getElementById('motion-modal-strip');
  const video = document.getElementById('motion-modal-video');
  const fallback = document.getElementById('motion-modal-fallback');
  document.getElementById('motion-modal-title').textContent = clip.location || clip.camera_entity_id || 'Motion Clip';
  document.getElementById('motion-modal-time').textContent = _fmtMotionTs(clip.ts);
  document.getElementById('motion-modal-desc').innerHTML = _motionHighlight(clip.description || 'No AI description available.');
  document.getElementById('motion-modal-followup').textContent = '—';
  if (strip) {
    strip.innerHTML =
      `<div class="motion-card-badge ${_motionSearchMode === 'ai' ? 'ai' : 'review'}">${_motionSearchMode === 'ai' ? 'AI Match' : 'Event Review'}</div>` +
      `<div class="motion-card-age">${_esc(_motionRelative(clip.ts))}</div>` +
      `<div class="motion-chip">${_esc(clip.status || 'ready')}</div>` +
      `${clip.canonical_event_type ? `<div class="motion-chip info">${_esc(String(clip.canonical_event_type).replaceAll('_', ' '))}</div>` : ''}`;
  }
  document.getElementById('motion-modal-meta').innerHTML =
    `<div class="motion-meta-row"><div class="motion-meta-key">Camera</div><div class="motion-meta-value">${_esc(_motionCameraLabel(clip.camera_entity_id))}</div></div>` +
    `<div class="motion-meta-row"><div class="motion-meta-key">Camera ID</div><div class="motion-meta-value">${_esc(clip.camera_entity_id || '—')}</div></div>` +
    `<div class="motion-meta-row"><div class="motion-meta-key">Trigger</div><div class="motion-meta-value">${_esc(clip.trigger_entity_id || '—')}</div></div>` +
    `<div class="motion-meta-row"><div class="motion-meta-key">Event Type</div><div class="motion-meta-value">${_esc(clip.canonical_event_type || '—')}</div></div>` +
    `<div class="motion-meta-row"><div class="motion-meta-key">Event ID</div><div class="motion-meta-value">${_esc(clip.canonical_event_id || '—')}</div></div>` +
    `<div class="motion-meta-row"><div class="motion-meta-key">Event Source</div><div class="motion-meta-value">${_esc(clip.event_source || '—')}</div></div>` +
    `<div class="motion-meta-row"><div class="motion-meta-key">Status</div><div class="motion-meta-value">${_esc(clip.status || '—')}</div></div>` +
    `<div class="motion-meta-row"><div class="motion-meta-key">Duration</div><div class="motion-meta-value">${_esc(String(clip.duration_s || 0))}s</div></div>` +
    `<div class="motion-meta-row"><div class="motion-meta-key">Clip ID</div><div class="motion-meta-value">${_esc(String(clip.id || '—'))}</div></div>`;
  _renderMotionModalActions({
    event_type: clip.canonical_event_type || '',
    event_source: clip.event_source || '',
    camera_entity_id: clip.camera_entity_id || '',
  });
  if (fallback) fallback.classList.remove('show');
  if (video) video.style.display = '';
  video.src = clip.video_url || '';
  video.currentTime = 0;
  video.playbackRate = 1.0;
  // Add speed controls if not already present
  let speedBar = document.getElementById('fa-speed-controls');
  if (!speedBar) {
    speedBar = document.createElement('div');
    speedBar.id = 'fa-speed-controls';
    speedBar.style.cssText = 'display:flex;gap:4px;padding:6px 0;justify-content:center;';
    [0.25, 0.5, 1, 1.5, 2].forEach(s => {
      const btn = document.createElement('button');
      btn.className = 'btn btn-outline';
      btn.style.cssText = 'font-size:10px;padding:2px 8px;min-width:36px;' + (s === 1 ? 'background:var(--accent);color:#fff;' : '');
      btn.textContent = s + 'x';
      btn.onclick = () => {
        video.playbackRate = s;
        speedBar.querySelectorAll('button').forEach(b => { b.style.background = ''; b.style.color = ''; });
        btn.style.background = 'var(--accent)'; btn.style.color = '#fff';
      };
      speedBar.appendChild(btn);
    });
    video.parentElement.appendChild(speedBar);
  }
  modal.classList.add('show');
}

function _eventDataRows(item) {
  const rows = [];
  rows.push(['Kind', _eventHistoryKindLabel(item.kind || 'event')]);
  rows.push(['Event Type', _motionEventTypeLabel(item.event_type || 'event')]);
  rows.push(['Source', _motionSourceLabel(item.event_source || 'unknown_source')]);
  rows.push(['Status', _motionStatusLabel(item.status || 'unknown_status')]);
  if (item.camera_entity_id) rows.push(['Camera', _motionCameraLabel(item.camera_entity_id)]);
  if (item.event_id) rows.push(['Event ID', item.event_id]);
  if (item.open_loop_note) rows.push(['Open Loop', item.open_loop_note]);
  const data = item.data || {};
  if (data.location) rows.push(['Location', data.location]);
  if (data.trigger_entity_id) rows.push(['Trigger', data.trigger_entity_id]);
  if (data.related_camera) rows.push(['Related Camera', _motionCameraLabel(data.related_camera)]);
  if (data.duration_s) rows.push(['Duration', `${data.duration_s}s`]);
  if (data.admin_note) rows.push(['Admin Note', data.admin_note]);
  const extra = data.extra || {};
  if (extra.open_loop_note && !item.open_loop_note) rows.push(['Open Loop', extra.open_loop_note]);
  if (Array.isArray(extra.coral_detections) && extra.coral_detections.length > 0) {
    rows.push(['Coral TPU', _faDedupCoral(extra.coral_detections).join(', ')]);
  }
  if (extra.plate_number) rows.push(['Number Plate', extra.plate_number]);
  else if (extra.coral_has_plate) rows.push(['Plate-bearing', 'vehicle detected — plate not legible']);
  return rows;
}

function openEventHistoryModal(item) {
  _motionModalIndex = -1;
  _motionModalHistoryIndex = (_eventHistoryItems || []).findIndex(entry => String(entry.id) === String(item.id));
  const modal = document.getElementById('motion-modal');
  const strip = document.getElementById('motion-modal-strip');
  const video = document.getElementById('motion-modal-video');
  const fallback = document.getElementById('motion-modal-fallback');
  const noteField = document.getElementById('motion-modal-note');
  document.getElementById('motion-modal-title').textContent = item.title || _motionEventTypeLabel(item.event_type || 'event');
  document.getElementById('motion-modal-time').textContent = _fmtMotionTs(item.ts);
  document.getElementById('motion-modal-desc').innerHTML = _motionHighlight(item.summary || 'No event summary available.');
  document.getElementById('motion-modal-followup').textContent = '—';
  if (strip) {
    strip.innerHTML =
      `<div class="motion-card-badge review">Event Review</div>` +
      `<div class="motion-card-age">${_esc(_motionRelative(item.ts))}</div>` +
      `<div class="motion-chip">${_esc(_motionStatusLabel(item.status || 'unknown_status'))}</div>` +
      `<div class="motion-chip info">${_esc(_motionEventTypeLabel(item.event_type || 'event'))}</div>`;
  }
  const rows = _eventDataRows(item);
  document.getElementById('motion-modal-meta').innerHTML = rows.map(([key, value]) => (
    `<div class="motion-meta-row"><div class="motion-meta-key">${_esc(key)}</div><div class="motion-meta-value">${_esc(String(value || '—'))}</div></div>`
  )).join('');
  if (noteField) noteField.value = String(item.data?.admin_note || '');
  _renderMotionModalActions(item);
  if (video) {
    video.pause();
    video.removeAttribute('src');
    video.load();
    video.style.display = 'none';
  }
  if (fallback) {
    document.getElementById('motion-modal-fallback-title').textContent = item.title || _motionEventTypeLabel(item.event_type || 'event');
    document.getElementById('motion-modal-fallback-sub').textContent = item.summary || 'No linked clip is available for this event.';
    fallback.classList.add('show');
  }
  modal.classList.add('show');
}

function stepMotionModal(delta) {
  if (_motionModalIndex >= 0) {
    if (!_motionClips.length) return;
    const next = _motionModalIndex + delta;
    if (next < 0 || next >= _motionClips.length) return;
    _motionModalIndex = next;
    _renderMotionModal(_motionClips[_motionModalIndex]);
    return;
  }
  if (_motionModalHistoryIndex >= 0) {
    const next = _motionModalHistoryIndex + delta;
    if (next < 0 || next >= _eventHistoryItems.length) return;
    _motionModalHistoryIndex = next;
    openEventHistoryModal(_eventHistoryItems[_motionModalHistoryIndex]);
  }
}

function closeMotionModal(evt) {
  if (evt && evt.target && evt.target !== evt.currentTarget && !evt.target.classList.contains('motion-modal-close')) return;
  const modal = document.getElementById('motion-modal');
  modal.classList.remove('show');
  const video = document.getElementById('motion-modal-video');
  video.pause();
  video.removeAttribute('src');
  video.load();
  video.style.display = '';
  const fallback = document.getElementById('motion-modal-fallback');
  if (fallback) fallback.classList.remove('show');
  const noteField = document.getElementById('motion-modal-note');
  if (noteField) noteField.value = '';
  const followup = document.getElementById('motion-modal-followup');
  if (followup) followup.textContent = '—';
  _motionModalHistoryIndex = -1;
}

document.addEventListener('DOMContentLoaded', () => {
  const query = document.getElementById('motion-query');
  const camera = document.getElementById('motion-camera');
  [query, camera].forEach(el => {
    if (!el) return;
    el.addEventListener('keydown', (evt) => {
      if (evt.key === 'Enter') {
        evt.preventDefault();
        searchMotionClips();
      }
    });
  });
  if (query) {
    query.addEventListener('input', () => {
      const preset = document.getElementById('motion-preset');
      if (preset && preset.value && preset.value !== query.value.trim()) preset.value = '';
    });
  }
  document.addEventListener('keydown', (evt) => {
    const modal = document.getElementById('motion-modal');
    if (!modal || !modal.classList.contains('show')) return;
    if (evt.key === 'Escape') {
      closeMotionModal();
    } else if (evt.key === 'ArrowLeft') {
      evt.preventDefault();
      stepMotionModal(-1);
    } else if (evt.key === 'ArrowRight') {
      evt.preventDefault();
      stepMotionModal(1);
    }
  });
});

// ── Health / Dashboard ────────────────────────────────────────────────────────

async function pollHealth() {
  try {
    const d = await fetch('/health').then(r => r.json());
    const dot = document.getElementById('status-dot-inner');
    const statusText = document.getElementById('status-text');
    if (d.status === 'ok') {
      dot.className = 'status-dot ok';
      statusText.textContent = 'All systems operational';
    } else {
      dot.className = 'status-dot err';
      statusText.textContent = 'Degraded';
    }
    if (document.querySelector('#section-dashboard.active')) renderHealth(d);
  } catch {
    document.getElementById('status-dot-inner').className = 'status-dot err';
    document.getElementById('status-text').textContent = 'Server unreachable';
  }
}

function renderHealth(d) {
  const comp = d.components || {};
  const map = { ollama:'h-ollama', whisper:'h-whisper', piper:'h-piper', home_assistant:'h-ha' };
  for (const [k, eid] of Object.entries(map)) {
    const el = document.getElementById(eid);
    if (!el) continue;
    const v = comp[k] || '—';
    el.textContent = v;
    el.className = 'stat-value ' + (v === 'reachable' || v === 'ready' ? 'ok' : v === 'loading' ? 'warn' : v === 'not_configured' ? '' : 'err');
  }
  // CodeProject.AI status — check via face service
  api('GET', '/admin/faces/known').then(fr => {
    const cpEl = document.getElementById('h-cpai');
    if (!cpEl) return;
    if (!fr.available) { cpEl.textContent = 'not configured'; cpEl.className = 'stat-value'; }
    else { cpEl.textContent = `reachable (${fr.faces.length} faces)`; cpEl.className = 'stat-value ok'; }
  }).catch(() => { const cpEl = document.getElementById('h-cpai'); if (cpEl) { cpEl.textContent = 'error'; cpEl.className = 'stat-value err'; } });
  document.getElementById('dash-version').textContent = d.version || '—';
}

async function loadDashboard() {
  _initDashCharts();
  try {
    const [health, sessions, monthCost, metricsNow] = await Promise.all([
      fetch('/health').then(r => r.json()),
      api('GET', '/admin/sessions'),
      api('GET', '/admin/costs/history?period=month').catch(() => null),
      api('GET', '/admin/metrics').catch(() => null),
    ]);
    renderHealth(health);
    document.getElementById('dash-sessions').textContent = sessions.active_sessions ?? '—';
    if (monthCost && monthCost.summary) {
      const s = monthCost.summary;
      const cost = s.cost_usd || 0;
      document.getElementById('dash-month-cost').textContent = '$' + cost.toFixed(cost < 0.01 ? 6 : 4);
      document.getElementById('dash-month-calls').textContent = (s.calls || 0) + ' calls this month';
    }
    if (metricsNow && metricsNow.latest) _applyGaugeDash(metricsNow.latest);
  } catch(e) { console.warn(e); }
}

function _applyGaugeDash(s) {
  _updateDashCharts(s);
  if (!s) return;
  const _b = id => document.getElementById(id);
  function _setBar(barId, pct) {
    const bar = _b(barId);
    if (!bar) return;
    const clamped = Math.max(0, Math.min(100, pct));
    bar.style.width = clamped.toFixed(1) + '%';
    bar.className = 'gauge-bar-fill ' + getGaugeColorClass(clamped);
  }
  // Note: dash-*-bar elements may not exist if gauges are removed from dashboard
  if (s.cpu_pct != null) {
    _b('dash-cpu') && (_b('dash-cpu').textContent = s.cpu_pct.toFixed(1) + '%');
    _setBar('dash-cpu-bar', s.cpu_pct);
  }
  if (s.ram_used != null && s.ram_total) {
    _b('dash-ram') && (_b('dash-ram').textContent = _fmtBytes(s.ram_used));
    _setBar('dash-ram-bar', (s.ram_used / s.ram_total) * 100);
  }
  if (s.disk_used != null && s.disk_total) {
    _b('dash-disk') && (_b('dash-disk').textContent = _fmtBytes(s.disk_used));
    _setBar('dash-disk-bar', (s.disk_used / s.disk_total) * 100);
  }
  if (s.gpu_util != null) {
    _b('dash-gpu') && (_b('dash-gpu').textContent = s.gpu_util.toFixed(0) + '%');
    _setBar('dash-gpu-bar', s.gpu_util);
  }
  if (s.gpu_mem_used != null && s.gpu_mem_total) {
    const vramMB = Math.round(s.gpu_mem_used / 1048576);
    const totalMB = Math.round(s.gpu_mem_total / 1048576);
    _b('dash-vram') && (_b('dash-vram').textContent = vramMB + ' / ' + totalMB + ' MB');
  }
}

// ── Config ────────────────────────────────────────────────────────────────────

const _CLOUD_MODELS = {
  ollama:    [],
  openai:    ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo'],
  google:    ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-2.0-flash', 'gemini-2.0-flash-001', 'gemini-2.0-flash-lite-001'],
  anthropic: ['claude-opus-4-6', 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001'],
};

const _FIELD_OPTIONS = {
  LLM_PROVIDER:  ['ollama', 'openai', 'google', 'anthropic'],
  TTS_PROVIDER:  ['piper', 'elevenlabs', 'afrotts', 'intron_afro_tts'],
  AFROTTS_VOICE: ['af_heart', 'af_nicole', 'af_sarah', 'af_sky', 'am_adam', 'am_michael', 'bf_emma', 'bf_isabella', 'bm_george', 'bm_lewis'],
  WHISPER_MODEL: ['tiny', 'base', 'small', 'medium', 'large-v2', 'large-v3'],
  LOG_LEVEL:     ['INFO', 'DEBUG', 'WARNING', 'ERROR'],
};

const _TTS_FIELDS = {
  piper:      ['PIPER_VOICE'],
  elevenlabs: ['ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID', 'ELEVENLABS_MODEL'],
  afrotts:    ['AFROTTS_VOICE', 'AFROTTS_SPEED'],
  intron_afro_tts: ['INTRON_AFRO_TTS_URL', 'INTRON_AFRO_TTS_REFERENCE_WAV', 'INTRON_AFRO_TTS_LANGUAGE', 'INTRON_AFRO_TTS_TIMEOUT_S'],
};

let _intronVoices = [];

async function _fetchIntronVoices() {
  try {
    const d = await api('GET', '/admin/intron-voices');
    _intronVoices = d.voices || [];
  } catch(_) { _intronVoices = []; }
  // If sidecar is down, provide a sensible default so the field is still usable
  if (_intronVoices.length === 0) {
    _intronVoices = [
      { id: 'reference_accent', name: 'Reference Accent', path: '/models/intron_afro_tts/audios/reference_accent.wav' },
    ];
  }
}

function _updateIntronVoiceDropdown(currentVal) {
  const el = document.getElementById('cfg-INTRON_AFRO_TTS_REFERENCE_WAV');
  if (!el) return;
  if (_intronVoices.length === 0) return;

  // Build a select dropdown to replace the current element
  const select = document.createElement('select');
  select.id = 'cfg-INTRON_AFRO_TTS_REFERENCE_WAV';
  select.dataset.key = 'INTRON_AFRO_TTS_REFERENCE_WAV';

  // Add a default/empty option
  const defaultOpt = document.createElement('option');
  defaultOpt.value = '';
  defaultOpt.textContent = '— Default reference voice —';
  select.appendChild(defaultOpt);

  for (const voice of _intronVoices) {
    const opt = document.createElement('option');
    opt.value = voice.path;
    opt.textContent = voice.name + ' (' + voice.id + ')';
    if (currentVal && currentVal === voice.path) opt.selected = true;
    select.appendChild(opt);
  }

  el.replaceWith(select);
}

function _updateTTSFields(provider) {
  const all = Object.values(_TTS_FIELDS).flat();
  all.forEach(key => {
    const el = document.getElementById('cfg-' + key);
    if (el) el.closest('.field').style.display = 'none';
  });
  const show = _TTS_FIELDS[provider] || [];
  show.forEach(key => {
    const el = document.getElementById('cfg-' + key);
    if (el) el.closest('.field').style.display = '';
  });
}

let _ollamaModels = [];

async function _fetchOllamaModels() {
  try {
    const d = await api('GET', '/admin/ollama-models');
    _ollamaModels = d.models || [];
  } catch(_) { _ollamaModels = []; }
}

function _updateOllamaModelDropdown(currentVal) {
  const wrapper = document.getElementById('ollama-model-wrapper');
  if (!wrapper) return;
  const val = currentVal !== undefined ? currentVal : (document.getElementById('cfg-OLLAMA_MODEL')?.value || '');
  if (_ollamaModels.length) {
    const selected = _ollamaModels.includes(val) ? val : (_ollamaModels[0] || val);
    wrapper.innerHTML = _buildSelect('OLLAMA_MODEL', selected, _ollamaModels);
  } else {
    wrapper.innerHTML = '<input type="text" id="cfg-OLLAMA_MODEL" data-key="OLLAMA_MODEL" value="' + esc(val) + '" placeholder="e.g. qwen2.5:7b">';
  }
}

let _configMeta = {};

function _buildSelect(key, val, options) {
  const opts = options.map(o =>
    `<option value="${esc(o)}"${o === val ? ' selected' : ''}>${esc(o)}</option>`
  ).join('');
  return `<select id="cfg-${key}" data-key="${key}">${opts}</select>`;
}

function _updateCloudModelDropdown(provider, currentVal) {
  const wrapper = document.getElementById('cloud-model-wrapper');
  if (!wrapper) return;
  const models = _CLOUD_MODELS[provider] || [];
  const val = currentVal !== undefined ? currentVal : (document.getElementById('cfg-CLOUD_MODEL')?.value || '');
  if (models.length) {
    const selected = models.includes(val) ? val : models[0];
    wrapper.innerHTML = _buildSelect('CLOUD_MODEL', selected, models);
  } else {
    wrapper.innerHTML = `<input type="text" id="cfg-CLOUD_MODEL" data-key="CLOUD_MODEL" value="${esc(val)}" placeholder="e.g. llama3.1:8b-instruct-q4_K_M">`;
  }
}

// ── Gemini Key Pool ──────────────────────────────────────────────────────────

// ── Vision Camera Selection ─────────────────────────────────────────────────

async function loadVisionCameras() {
  const el = document.getElementById('vision-cameras-list');
  if (!el) return;
  try {
    const d = await api('GET', '/admin/vision-cameras');
    el.innerHTML = (d.cameras || []).map(c =>
      `<label style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--surface2);border-radius:8px;cursor:pointer;">
        <input type="checkbox" class="vision-cam-check" value="${_escapeHtml(c.entity_id)}" ${c.vision_enabled ? 'checked' : ''}>
        <span style="font-size:13px;">${_escapeHtml(c.label)}</span>
        <span class="text-sm text-muted" style="margin-left:auto;">${_escapeHtml(c.entity_id)}</span>
      </label>`
    ).join('');
  } catch (e) {
    el.innerHTML = '<div class="text-sm" style="color:var(--danger);">Failed to load cameras: ' + (e.message || e) + '</div>';
  }
}

async function saveVisionCameras() {
  const checks = document.querySelectorAll('.vision-cam-check:checked');
  const enabled = Array.from(checks).map(c => c.value);
  try {
    await api('POST', '/admin/vision-cameras', { enabled });
    toast(`Vision enabled for ${enabled.length} cameras. Restart to apply.`);
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
}

async function loadRooms() {
  const el = document.getElementById('rooms-list');
  if (!el) return;
  try {
    const [rd, ad] = await Promise.all([api('GET', '/admin/rooms'), api('GET', '/admin/avatars').catch(() => ({avatars:[]}))]);
    const rooms = rd.rooms || [];
    const avatars = ad.avatars || [];
    const avatarOptions = ['<option value="">Default (global setting)</option>',
      ...avatars.map(a => `<option value="${_escapeHtml(a)}">${_escapeHtml(a)}</option>`)
    ].join('');
    // Populate the add-form GLB dropdown regardless of room count
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
        <button class="btn btn-outline btn-xs" onclick="copyRoomUrl(${JSON.stringify(r.avatar_url)})" style="flex-shrink:0;">Copy</button>
        <button class="btn btn-outline btn-xs" style="color:var(--danger);flex-shrink:0;" onclick="deleteRoom(${JSON.stringify(r.id)})">Remove</button>
      </div>`
    ).join('');
    // Set current GLB selections after rendering
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
    await api('PATCH', '/admin/rooms/' + encodeURIComponent(roomId), { glb: glb || null });
    toast(glb ? `Avatar set to ${glb}` : 'Using default avatar');
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
}

function copyRoomUrl(url) {
  navigator.clipboard?.writeText(url).then(() => toast('URL copied')).catch(() => toast('Copy failed', 'err'));
}

async function addRoom() {
  const label = document.getElementById('room-new-label')?.value.trim();
  const id = document.getElementById('room-new-id')?.value.trim().toLowerCase().replace(/\s+/g,'_');
  const glb = document.getElementById('room-new-glb')?.value.trim() || null;
  if (!label || !id) { toast('Enter room name and slug', 'err'); return; }
  try {
    await api('POST', '/admin/rooms', { label, id, ...(glb ? { glb } : {}) });
    document.getElementById('room-new-label').value = '';
    document.getElementById('room-new-id').value = '';
    if (document.getElementById('room-new-glb')) document.getElementById('room-new-glb').value = '';
    await loadRooms();
    toast('Room added');
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
}

async function deleteRoom(roomId) {
  if (!confirm(`Remove room "${roomId}"?`)) return;
  try {
    await api('DELETE', '/admin/rooms/' + encodeURIComponent(roomId));
    await loadRooms();
    toast('Room removed');
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
}

async function loadGeminiPool() {
  const keysEl = document.getElementById('gemini-pool-keys');
  const statsEl = document.getElementById('gemini-pool-stats');
  if (!keysEl) return;
  try {
    const d = await api('GET', '/admin/gemini-pool');
    const keys = d.keys || [];
    const stats = d.stats || {};
    statsEl.textContent = `${stats.pool_size || 0} keys · ${stats.available || 0} available · ${stats.total_calls || 0} calls · ${stats.total_429s || 0} rate limits`;
    if (!keys.length) {
      keysEl.innerHTML = '<div class="text-sm text-muted">No keys configured. Add your Gemini API keys below.</div>';
      return;
    }
    keysEl.innerHTML = keys.map((k, i) => {
      const status = k.available
        ? '<span style="color:var(--green,#10b981);font-weight:600;">● Active</span>'
        : '<span style="color:var(--danger,#ef4444);font-weight:600;">● Cooldown ' + k.cooldown_remaining_s + 's</span>';
      const pins = k.pinned_cameras.length ? '<span class="text-sm text-muted"> · 📷 ' + k.pinned_cameras.join(', ') + '</span>' : '';
      return '<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--surface2);border-radius:8px;border:1px solid var(--border);">'
        + '<div style="flex:1;">'
        + '<div style="font-weight:600;font-size:13px;">' + _esc(k.label) + ' <span class="text-muted" style="font-weight:400;">' + _esc(k.masked_key) + '</span></div>'
        + '<div class="text-sm text-muted">' + status + ' · ' + k.total_calls + ' calls · ' + k.total_429s + ' 429s' + pins + '</div>'
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
  if (!key) { toast('Enter an API key', 'err'); return; }
  try {
    await api('POST', '/admin/gemini-pool/add', { key, label });
    keyEl.value = '';
    labelEl.value = '';
    toast('Key added');
    loadGeminiPool();
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
}

async function removeGeminiKey(index) {
  if (!confirm('Remove this API key from the pool?')) return;
  try {
    await api('DELETE', '/admin/gemini-pool/' + index);
    toast('Key removed');
    loadGeminiPool();
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
}

async function loadConfig() {
  try {
    const d = await api('GET', '/admin/config');
    _configMeta = d.fields || {};
    const grid = document.getElementById('config-fields');
    grid.innerHTML = '';

    // Build a lookup: field key → category name
    const _fieldToCategory = {};
    for (const [cat, keys] of Object.entries(_CONFIG_CATEGORIES)) {
      for (const k of keys) _fieldToCategory[k] = cat;
    }

    // Group fields by category, preserving order
    const categoryFields = {};
    for (const cat of Object.keys(_CONFIG_CATEGORIES)) categoryFields[cat] = [];
    const uncategorized = [];

    for (const [key, [label, sensitive]] of Object.entries(_configMeta)) {
      const val = (d.values || {})[key] || '';
      const cat = _fieldToCategory[key];
      const entry = { key, label, sensitive, val };
      if (cat) categoryFields[cat].push(entry);
      else uncategorized.push(entry);
    }

    // Helper to build a field element
    function buildField(entry) {
      const { key, label, sensitive, val } = entry;
      const defaults = {
        HOST:'0.0.0.0', PORT:'8000', LOG_LEVEL:'INFO',
        HA_URL:'http://homeassistant.local:8123',
        LLM_PROVIDER:'ollama', OLLAMA_URL:'http://localhost:11434',
        OLLAMA_MODEL:'llama3.1:8b-instruct-q4_K_M',
        OLLAMA_VISION_MODEL:'llama3.2-vision:11b-instruct-q4_K_M',
        CLOUD_MODEL:'gemini-2.5-flash',
        WHISPER_MODEL:'small', TTS_PROVIDER:'piper',
        PIPER_VOICE:'en_US-lessac-medium', AFROTTS_VOICE:'af_heart', AFROTTS_SPEED:'1.0',
        INTRON_AFRO_TTS_URL:'http://127.0.0.1:8021', INTRON_AFRO_TTS_TIMEOUT_S:'90',
        INTRON_AFRO_TTS_LANGUAGE:'en',
        TTS_ENGINE:'tts.google_translate_en_com', SPEAKER_AUDIO_OFFSET_MS:'0',
        MOTION_CLIP_DURATION_S:'8', MOTION_CLIP_SEARCH_CANDIDATES:'120',
        MOTION_CLIP_SEARCH_RESULTS:'24', MOTION_VISION_PROVIDER:'gemini',
        HEATING_LLM_PROVIDER:'gemini', HEATING_SHADOW_ENABLED:'true',
        PROACTIVE_ENTITY_COOLDOWN_S:'600', PROACTIVE_CAMERA_COOLDOWN_S:'600',
        PROACTIVE_GLOBAL_MOTION_COOLDOWN_S:'600', PROACTIVE_GLOBAL_ANNOUNCE_COOLDOWN_S:'300',
        PROACTIVE_QUEUE_DEDUP_COOLDOWN_S:'120', PROACTIVE_BATCH_WINDOW_S:'60',
        PROACTIVE_MAX_BATCH_CHANGES:'20', PROACTIVE_WEATHER_COOLDOWN_S:'3600',
        PROACTIVE_FORECAST_HOUR:'7', HA_POWER_ALERT_COOLDOWN_S:'1800',
        MOTION_CLIP_RETENTION_DAYS:'30',
        SESSION_RATE_LIMIT_MAX:'30', SESSION_RATE_LIMIT_WINDOW_S:'60',
        MUSIC_ASSISTANT_URL:'http://localhost:8095',
        BLUEIRIS_URL:'',
        CODEPROJECT_AI_URL:'',
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
        const provider = (d.values || {})['LLM_PROVIDER'] || 'ollama';
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

    // Render each category as a collapsible group
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

    // Render uncategorized fields (if any)
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
    if (providerEl) _updateCloudModelDropdown(providerEl.value, (d.values || {})['CLOUD_MODEL'] || '');

    await _fetchOllamaModels();
    _updateOllamaModelDropdown((d.values || {})['OLLAMA_MODEL'] || '');

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
          const refVal = (d.values || {})['INTRON_AFRO_TTS_REFERENCE_WAV'] || '';
          _updateIntronVoiceDropdown(refVal);
        }
      });
      _updateTTSFields(ttsProviderEl.value);
      if (ttsProviderEl.value === 'intron_afro_tts') {
        await _fetchIntronVoices();
        const refVal = (d.values || {})['INTRON_AFRO_TTS_REFERENCE_WAV'] || '';
        _updateIntronVoiceDropdown(refVal);
      }
    }
  } catch(e) { toast('Failed to load config: ' + e.message, 'err'); }
}

async function saveConfig() {
  const values = {};
  document.querySelectorAll('#config-fields [data-key]').forEach(el => {
    values[el.dataset.key] = el.value;
  });
  try {
    await api('POST', '/admin/config', { values });
    toast('Configuration saved', 'ok');
  } catch(e) { toast('Save failed: ' + e.message, 'err'); }
}

async function loadSpeakerConfig() {
  const container = document.getElementById('speaker-config');
  const occupied = document.getElementById('speaker-occupied-areas');
  const targetArea = document.getElementById('announce-target-area');
  if (!container || !occupied) return;
  try {
    const data = await api('GET', '/admin/speakers');
    const areas = data.areas || [];
    const occupiedAreas = data.occupied_areas || [];
    occupied.textContent = occupiedAreas.length
      ? `Occupied areas right now: ${occupiedAreas.join(', ')}`
      : 'Occupied areas right now: none detected';
    if (targetArea) {
      targetArea.innerHTML = '<option value="">Auto (occupied areas)</option>' +
        areas.map(area => `<option value="${esc(area.area_name)}">${esc(area.area_name)}</option>`).join('');
    }
    if (!areas.length) {
      container.innerHTML = '<div class="text-md text-muted">No Home Assistant media players were discovered.</div>';
      return;
    }
    container.innerHTML = areas.map(area => `
      <div style="border:1px solid var(--border);border-radius:12px;padding:14px 16px;background:var(--surface);">
        <div onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'grid':'none';this.querySelector('.chevron').textContent=this.nextElementSibling.style.display==='none'?'▸':'▾'" style="font-weight:600;color:var(--text1);display:flex;justify-content:space-between;gap:12px;cursor:pointer;user-select:none;">
          <span><span class="chevron">▸</span> ${esc(area.area_name)}</span>
          <span class="text-xs text-muted">${area.speakers.length} speaker${area.speakers.length === 1 ? '' : 's'}</span>
        </div>
        <div style="display:none;gap:8px;margin-top:10px;">
          ${area.speakers.map(sp => `
            <label style="display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 10px;border-radius:10px;background:var(--surface2);">
              <span>
                <span style="display:block;color:var(--text1);font-weight:500;">${esc(sp.friendly_name || sp.entity_id)}</span>
                <span style="display:block;font-size:11px;color:var(--text3);">${esc(sp.entity_id)} · ${sp.use_alexa ? 'Alexa notify' : 'HA TTS/media'}</span>
              </span>
              <input type="checkbox" data-speaker-entity="${esc(sp.entity_id)}" ${sp.enabled ? 'checked' : ''} />
            </label>
          `).join('')}
        </div>
      </div>
    `).join('');
  } catch (e) {
    container.innerHTML = '<div style="color:#fca5a5;font-size:13px;">Failed to load speaker routing.</div>';
    occupied.textContent = 'Occupied areas right now: unavailable';
    toast('Failed to load speakers: ' + e.message, 'err');
  }
}

async function saveSpeakerConfig() {
  const entries = [...document.querySelectorAll('[data-speaker-entity]')].map(el => ({
    entity_id: el.dataset.speakerEntity,
    enabled: !!el.checked,
  }));
  try {
    await api('POST', '/admin/speakers', { speakers: entries });
    toast('Speaker routing saved', 'ok');
    await loadSpeakerConfig();
  } catch (e) {
    toast('Failed to save speaker routing: ' + e.message, 'err');
  }
}

// ── Music ─────────────────────────────────────────────────────────────────────

async function loadMusicPlayers() {
  // Check Music Assistant status
  const maEl = document.getElementById('music-ma-status');
  if (maEl) {
    try {
      const st = await api('GET', '/admin/music/status');
      if (!st.configured) {
        maEl.innerHTML = '⚪ Not configured — add <code>MUSIC_ASSISTANT_URL</code> to .env';
      } else if (st.available) {
        maEl.innerHTML = '<span style="color:var(--green);">● Connected</span> — search and play available';
      } else {
        maEl.innerHTML = '<span style="color:var(--red);">● Offline</span> — start with: <code>docker compose up -d music-assistant</code>';
      }
    } catch { maEl.innerHTML = '⚪ Status unknown'; }
  }

  const npEl = document.getElementById('music-now-playing');
  const allEl = document.getElementById('music-players-list');
  if (!npEl || !allEl) return;
  try {
    const d = await api('GET', '/admin/music/players');
    const players = d.players || [];
    const active = players.filter(p => ['playing','paused','buffering','idle'].includes(p.state) && (p.media_title || p.state === 'paused'));

    // Populate speaker checkboxes grouped by brand
    const sel = document.getElementById('music-target-players');
    if (sel) {
      const available = players.filter(p => p.state !== 'unavailable');
      // Remember previously checked
      const wasChecked = new Set([...document.querySelectorAll('.music-speaker-chk:checked')].map(c => c.value));
      const _B = {
        sonos: { match: id => id.includes('sonos'), label: 'SONOS', color: '#000' },
        denon: { match: id => id.includes('denon'), label: 'DENON', color: '#0a2463' },
        alexa: { match: id => id.includes('echo') || id.includes('alexa'), label: '🔵 Alexa', color: '#232f3e' },
      };
      let html = '';
      const used = new Set();
      for (const [, b] of Object.entries(_B)) {
        const group = available.filter(p => b.match(p.entity_id));
        group.forEach(p => used.add(p.entity_id));
        if (!group.length) continue;
        html += `<div style="margin-bottom:8px;"><div style="font-size:10px;font-weight:700;color:var(--text3);letter-spacing:1px;margin-bottom:4px;">${b.label}</div><div style="display:flex;flex-wrap:wrap;gap:6px;">`;
        html += group.map(p => {
          const eid = _esc(p.entity_id);
          const chk = wasChecked.has(p.entity_id) ? 'checked' : '';
          return `<label style="display:flex;align-items:center;gap:5px;font-size:12px;padding:6px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:all .15s;" onmouseover="this.style.borderColor='${b.color}'" onmouseout="this.style.borderColor='var(--border)'">
            <input type="checkbox" class="music-speaker-chk" value="${eid}" ${chk} style="accent-color:${b.color};">
            ${_esc(p.friendly_name)}
          </label>`;
        }).join('');
        html += '</div></div>';
      }
      const other = available.filter(p => !used.has(p.entity_id));
      if (other.length) {
        html += `<div style="margin-bottom:8px;"><div style="font-size:10px;font-weight:700;color:var(--text3);letter-spacing:1px;margin-bottom:4px;">OTHER</div><div style="display:flex;flex-wrap:wrap;gap:6px;">`;
        html += other.map(p => {
          const chk = wasChecked.has(p.entity_id) ? 'checked' : '';
          return `<label style="display:flex;align-items:center;gap:5px;font-size:12px;padding:6px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;cursor:pointer;"><input type="checkbox" class="music-speaker-chk" value="${_esc(p.entity_id)}" ${chk} style="accent-color:var(--accent);">${_esc(p.friendly_name)}</label>`;
        }).join('');
        html += '</div></div>';
      }
      sel.innerHTML = html || '<span class="text-sm text-muted">No speakers available</span>';
    }
    if (!active.length) {
      npEl.innerHTML = '<div class="text-sm text-muted">Nothing playing.</div>';
    } else {
      _renderMusicPlayersList(active, npEl);
    }
    if (!players.length) {
      allEl.innerHTML = '<div class="text-sm text-muted">No media players found.</div>';
    } else {
      _renderMusicPlayersList(players, allEl);
    }
  } catch(e) {
    npEl.innerHTML = `<div class="text-sm" style="color:var(--danger);">Failed: ${_esc(e.message)}</div>`;
  }
}

function _renderMusicPlayer(p, showControls) {
  const eid = _esc(p.entity_id);
  const isSonos = eid.includes('sonos');
  const isDenon = eid.includes('denon');
  const accent = isSonos ? '#000' : isDenon ? '#0a2463' : '#232f3e';
  const accentLight = isSonos ? 'rgba(0,0,0,.06)' : isDenon ? 'rgba(10,36,99,.06)' : 'rgba(35,47,62,.06)';
  const isActive = p.state === 'playing' || p.state === 'paused';
  const vol = p.volume_level != null ? Math.round(p.volume_level * 100) + '%' : '';
  const track = p.media_title || '';
  const artist = p.media_artist || '';
  const album = p.media_album_name || '';
  const art = p.entity_picture || '';
  const stateIcon = p.state === 'playing' ? '▶' : p.state === 'paused' ? '⏸' : '○';
  const stateColor = p.state === 'playing' ? '#34c759' : p.state === 'paused' ? '#ffcc00' : 'var(--text3)';

  const artHtml = art
    ? `<div style="width:56px;height:56px;border-radius:8px;overflow:hidden;flex-shrink:0;background:${accentLight};"><img src="${_esc(art)}" style="width:100%;height:100%;object-fit:cover;" onerror="this.parentElement.innerHTML='🎵'"></div>`
    : isActive ? `<div style="width:56px;height:56px;border-radius:8px;background:${accentLight};display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0;">🎵</div>` : '';

  const controls = isActive || showControls ? `
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px;">
      <button class="btn btn-outline btn-sm" onclick="musicCtrl('${eid}','previous')" style="font-size:13px;padding:4px 8px;">⏮</button>
      <button style="width:36px;height:36px;border-radius:50%;border:none;background:${accent};color:#fff;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;" onclick="musicCtrl('${eid}','${p.state==='playing'?'pause':'play'}')">${p.state==='playing'?'⏸':'▶'}</button>
      <button class="btn btn-outline btn-sm" onclick="musicCtrl('${eid}','next')" style="font-size:13px;padding:4px 8px;">⏭</button>
      <button class="btn btn-outline btn-sm" onclick="musicCtrl('${eid}','stop')" style="font-size:11px;padding:4px 8px;">⏹</button>
      <div style="flex:1;display:flex;align-items:center;gap:6px;margin-left:8px;">
        <span style="font-size:10px;color:var(--text3);">🔊</span>
        <input type="range" min="0" max="100" value="${p.volume_level!=null?Math.round(p.volume_level*100):50}" style="flex:1;max-width:100px;accent-color:${accent};" onchange="musicCtrl('${eid}','volume',this.value/100)">
        <span style="font-size:10px;color:var(--text3);min-width:28px;">${vol}</span>
      </div>
    </div>` : '';

  return `<div style="padding:12px;margin-bottom:8px;border-radius:12px;background:var(--surface2);border-left:3px solid ${isActive ? accent : 'transparent'};">
    <div style="display:flex;gap:12px;align-items:center;">
      ${artHtml}
      <div style="flex:1;min-width:0;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span style="font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${_esc(p.friendly_name)}</span>
          <span style="font-size:11px;color:${stateColor};flex-shrink:0;margin-left:8px;">${stateIcon} ${_esc(p.state)}</span>
        </div>
        ${track ? `<div style="font-size:12px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"><strong>${_esc(track)}</strong></div>` : ''}
        ${artist ? `<div style="font-size:11px;color:var(--text3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${_esc(artist)}${album ? ' · ' + _esc(album) : ''}</div>` : ''}
      </div>
    </div>
    ${controls}
  </div>`;
}

function _renderMusicPlayersList(players, container) {
  const _BRANDS = {
    sonos:  { match: p => p.entity_id.includes('sonos'), logo: '<span style="font-size:16px;font-weight:800;letter-spacing:2px;text-transform:uppercase;">Sonos</span>' },
    denon:  { match: p => p.entity_id.includes('denon'), logo: '<span style="font-size:16px;font-weight:800;letter-spacing:1px;text-transform:uppercase;">DENON</span>' },
    alexa:  { match: p => p.entity_id.includes('echo') || p.entity_id.includes('alexa'), logo: '<span style="font-size:16px;font-weight:800;letter-spacing:1px;">🔵 Alexa</span>' },
  };
  let html = '';
  const used = new Set();
  for (const [brand, cfg] of Object.entries(_BRANDS)) {
    const group = players.filter(p => cfg.match(p));
    group.forEach(p => used.add(p.entity_id));
    if (!group.length) continue;
    html += `<div style="margin-bottom:16px;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding-bottom:8px;border-bottom:2px solid var(--border);">
        ${cfg.logo}
      </div>
      ${group.map(p => _renderMusicPlayer(p, false)).join('')}
    </div>`;
  }
  const other = players.filter(p => !used.has(p.entity_id));
  if (other.length) {
    html += `<div style="margin-bottom:16px;">
      <div style="font-weight:600;font-size:13px;margin-bottom:8px;padding-bottom:8px;border-bottom:2px solid var(--border);">Other</div>
      ${other.map(p => _renderMusicPlayer(p, false)).join('')}
    </div>`;
  }
  if (!html) html = '<div class="text-sm text-muted">No speakers found.</div>';
  container.innerHTML = html;
}

async function musicCtrl(entityId, action, value) {
  try {
    await api('POST', '/admin/music/control', { entity_id: entityId, action, value: value ?? null });
    setTimeout(loadMusicPlayers, 500);
  } catch(e) { toast('Music control failed: ' + e.message); }
}

async function musicSearch() {
  const q = (document.getElementById('music-search-input')?.value || '').trim();
  const el = document.getElementById('music-search-results');
  if (!q || !el) return;
  el.innerHTML = '<div class="text-sm text-muted">Searching…</div>';
  try {
    const d = await api('GET', '/admin/music/search?q=' + encodeURIComponent(q));
    const results = d.results || [];
    if (!results.length) {
      el.innerHTML = '<div class="text-sm text-muted">No results found. Is Music Assistant running?</div>';
      return;
    }
    window._musicResults = results;
    el.innerHTML = results.map((r, i) => {
      const name = _esc(r.name || r.title || r.media_title || '?');
      const artist = _esc(r.artist || r.artists?.[0]?.name || '');
      const hasUri = r.uri || r.media_content_id;
      return `<div style="padding:6px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">
        <div><span style="font-weight:500;">${name}</span>${artist ? ` <span class="text-xs text-muted">— ${artist}</span>` : ''}</div>
        ${hasUri ? `<button class="btn btn-outline btn-sm" onclick="musicPlayUri(window._musicResults[${i}].uri||window._musicResults[${i}].media_content_id)" style="font-size:11px;">▶ Play</button>` : ''}
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = `<div class="text-sm" style="color:var(--danger);">Search failed: ${_esc(e.message)}</div>`;
  }
}

async function musicPlayUri(uri) {
  const checked = [...document.querySelectorAll('.music-speaker-chk:checked')].map(c => c.value);
  if (!checked.length) { toast('Select at least one speaker'); return; }
  try {
    for (const eid of checked) {
      await api('POST', '/admin/music/control', { entity_id: eid, action: 'play', value: uri });
    }
    toast('Playing on ' + checked.length + ' speaker' + (checked.length > 1 ? 's' : ''));
    setTimeout(loadMusicPlayers, 1000);
  } catch(e) { toast('Play failed: ' + e.message); }
}

// ── Energy Dashboard ──────────────────────────────────────────────────────────

async function loadEnergy() {
  try {
    const [sumR, devR] = await Promise.all([
      api('GET', '/admin/energy/summary'),
      api('GET', '/admin/energy/devices'),
    ]);
    const s = sumR.summary || {};
    const devices = devR.devices || [];

    // Summary cards
    const cards = document.getElementById('energy-summary-cards');
    const _card = (icon, label, val, unit, color) =>
      `<div style="background:var(--surface2);border-radius:12px;padding:14px 16px;border-left:3px solid ${color};">
        <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">${icon} ${label}</div>
        <div style="font-size:22px;font-weight:700;">${val}<span style="font-size:12px;font-weight:400;color:var(--text3);margin-left:4px;">${unit}</span></div>
      </div>`;
    const _v = (k) => s[k]?.value != null ? s[k].value : '—';
    const _u = (k) => s[k]?.unit || '';
    cards.innerHTML = [
      _card('⚡', 'Live Power', _v('total_power'), 'W', '#ff9500'),
      _card('💰', 'Cost/Hour', _v('total_cost_hourly'), '£/h', '#34c759'),
      _card('📅', 'Today', _v('daily_cost'), '£', '#007aff'),
      _card('📆', 'This Month', _v('monthly_cost'), '£', '#5856d6'),
      _card('🔌', 'Today Usage', _v('smart_elec_today'), 'kWh', '#ff3b30'),
      _card('🔥', 'Gas Today', _v('smart_gas_cost_today'), '£', '#ff6b35'),
    ].join('');

    // Glow Smart Meter
    const glow = document.getElementById('energy-glow');
    if (glow) {
      glow.innerHTML = `
        <div style="background:var(--surface2);border-radius:10px;padding:14px;border-left:3px solid #007aff;">
          <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">⚡ Electricity Today</div>
          <div style="font-size:20px;font-weight:700;">${_v('smart_elec_today')} <span style="font-size:12px;color:var(--text3);">kWh</span></div>
          <div style="font-size:14px;font-weight:600;color:#007aff;margin-top:4px;">£${_v('smart_elec_cost_today')}</div>
        </div>
        <div style="background:var(--surface2);border-radius:10px;padding:14px;border-left:3px solid #ff6b35;">
          <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">🔥 Gas Today</div>
          <div style="font-size:20px;font-weight:700;">${_v('smart_gas_today')} <span style="font-size:12px;color:var(--text3);">kWh</span></div>
          <div style="font-size:14px;font-weight:600;color:#ff6b35;margin-top:4px;">£${_v('smart_gas_cost_today')}</div>
        </div>
        <div style="background:var(--surface2);border-radius:10px;padding:14px;border-left:3px solid #5856d6;">
          <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">⚡ Electricity This Month</div>
          <div style="font-size:20px;font-weight:700;">£${_v('monthly_cost')}</div>
        </div>
        <div style="background:var(--surface2);border-radius:10px;padding:14px;border-left:3px solid #af52de;">
          <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">🔥 Gas Yesterday</div>
          <div style="font-size:20px;font-weight:700;">£${_v('gas_prev_cost')}</div>
          <div style="font-size:11px;color:var(--text3);margin-top:2px;">${_v('gas_prev_kwh')} kWh</div>
        </div>
      `;
    }

    // Tariff
    const tariff = document.getElementById('energy-tariff');
    tariff.innerHTML = `
      <div><span class="text-sm text-muted">Electricity Rate</span><div style="font-size:16px;font-weight:600;">${_v('elec_rate')} <span class="text-xs text-muted">${_u('elec_rate')}</span></div></div>
      <div><span class="text-sm text-muted">Standing Charge</span><div style="font-size:16px;font-weight:600;">${_v('elec_standing')} <span class="text-xs text-muted">${_u('elec_standing')}</span></div></div>
      <div><span class="text-sm text-muted">Gas Rate</span><div style="font-size:16px;font-weight:600;">${_v('gas_rate')} <span class="text-xs text-muted">${_u('gas_rate')}</span></div></div>
      <div><span class="text-sm text-muted">Gas Standing</span><div style="font-size:16px;font-weight:600;">${_v('gas_standing')} <span class="text-xs text-muted">${_u('gas_standing')}</span></div></div>
    `;

    // Device breakdown
    const devEl = document.getElementById('energy-devices');
    const maxW = Math.max(...devices.map(d => d.watts), 1);
    devEl.innerHTML = devices.map(d => {
      const pct = Math.max((d.watts / maxW) * 100, 2);
      const color = d.watts > 100 ? '#ff3b30' : d.watts > 20 ? '#ff9500' : '#34c759';
      return `<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
        <span style="min-width:120px;font-size:12px;font-weight:500;">${_esc(d.name)}</span>
        <div style="flex:1;height:20px;background:var(--surface);border-radius:4px;overflow:hidden;">
          <div style="height:100%;width:${pct}%;background:${color};border-radius:4px;transition:width .3s;"></div>
        </div>
        <span style="min-width:60px;text-align:right;font-size:12px;font-weight:600;">${d.watts} W</span>
      </div>`;
    }).join('') || '<div class="text-sm text-muted">No device data</div>';

    // Yesterday
    const yest = document.getElementById('energy-yesterday');
    yest.innerHTML = `
      <div><span class="text-sm text-muted">Electricity</span><div style="font-size:16px;font-weight:600;">${_v('elec_prev_kwh')} kWh — £${_v('elec_prev_cost')}</div></div>
      <div><span class="text-sm text-muted">Gas</span><div style="font-size:16px;font-weight:600;">${_v('gas_prev_kwh')} kWh — £${_v('gas_prev_cost')}</div></div>
    `;
  } catch(e) {
    document.getElementById('energy-summary-cards').innerHTML = `<div class="text-sm" style="color:var(--danger);">Failed: ${_esc(e.message)}</div>`;
  }
}

// ── System Prompt ─────────────────────────────────────────────────────────────

async function loadPrompt() {
  try {
    const d = await api('GET', '/admin/prompt');
    document.getElementById('prompt-text').value = d.text || '';
  } catch(e) { toast('Failed to load prompt: ' + e.message, 'err'); }
}

async function savePrompt() {
  try {
    await api('POST', '/admin/prompt', { text: document.getElementById('prompt-text').value });
    toast('System prompt saved', 'ok');
  } catch(e) { toast('Save failed: ' + e.message, 'err'); }
}


// ── Sync Devices to Prompt ────────────────────────────────────────────────────

let _syncEntities = [];   // full list from preview
let _syncAreas    = [];   // available area names

async function syncPreview() {
  document.getElementById('sync-preview-info').textContent = 'Scanning HA for new entities...';
  document.getElementById('sync-entity-list').innerHTML = '';
  document.getElementById('sync-apply-status').textContent = '';
  document.getElementById('sync-preview-panel').style.display = 'block';
  try {
    const d = await api('GET', '/admin/sync-prompt/preview');
    _syncEntities = d.entities || [];
    _syncAreas    = d.available_areas || [];
    if (_syncEntities.length === 0) {
      document.getElementById('sync-preview-info').textContent = 'No new entities found — system prompt is up to date.';
      document.getElementById('sync-entity-list').innerHTML = '<div style="padding:16px;color:var(--text2);text-align:center;">All discovered entities are already in the prompt.</div>';
      return;
    }
    document.getElementById('sync-preview-info').textContent = `${_syncEntities.length} new entities found. Select which to add to the system prompt.`;
    syncRenderEntities(_syncEntities);
  } catch(e) { toast('Scan failed: ' + e.message, 'err'); }
}

function syncRenderEntities(entities) {
  const list = document.getElementById('sync-entity-list');
  list.innerHTML = '';
  // group by logical group
  const groups = {};
  for (const e of entities) {
    const g = e.group || e.domain;
    if (!groups[g]) groups[g] = [];
    groups[g].push(e);
  }
  for (const [group, items] of Object.entries(groups).sort()) {
    const hdr = document.createElement('div');
    hdr.className = 'sync-group-hdr';
    hdr.textContent = `${group} (${items.length})`;
    list.appendChild(hdr);
    for (const e of items) {
      const row = document.createElement('div');
      row.className = 'sync-row';
      row.dataset.entityId = e.entity_id;
      // area dropdown
      const areaOpts = ['', ..._syncAreas].map(a =>
        `<option value="${esc(a)}" ${a === (e.area||'') ? 'selected' : ''}>${a || '— no area —'}</option>`
      ).join('');
      row.innerHTML = `
        <input type="checkbox" class="sync-chk" data-id="${esc(e.entity_id)}" checked>
        <span class="sync-name" title="${esc(e.friendly_name || e.entity_id)}">${esc(e.friendly_name || e.entity_id)}</span>
        <span class="sync-eid">${esc(e.entity_id)}</span>
        <span class="sync-state">${esc(e.state)}${e.unit ? ' ' + esc(e.unit) : ''}${e.device_class ? ' [' + esc(e.device_class) + ']' : ''}</span>
        <select class="sync-area-sel" data-id="${esc(e.entity_id)}">${areaOpts}</select>
      `;
      list.appendChild(row);
    }
  }
}

function syncFilterEntities() {
  const q = (document.getElementById('sync-filter').value || '').toLowerCase();
  if (!q) { syncRenderEntities(_syncEntities); return; }
  syncRenderEntities(_syncEntities.filter(e =>
    e.entity_id.toLowerCase().includes(q) ||
    (e.friendly_name||'').toLowerCase().includes(q) ||
    (e.area||'').toLowerCase().includes(q) ||
    (e.group||'').toLowerCase().includes(q)
  ));
}

function syncSelectAll(checked) {
  document.querySelectorAll('.sync-chk').forEach(cb => cb.checked = checked);
}

async function syncApply() {
  const selected = [...document.querySelectorAll('.sync-chk:checked')].map(cb => cb.dataset.id);
  if (selected.length === 0) { toast('No entities selected', 'warn'); return; }
  const area_overrides = {};
  document.querySelectorAll('.sync-area-sel').forEach(sel => {
    if (sel.value) area_overrides[sel.dataset.id] = sel.value;
  });
  document.getElementById('sync-apply-status').textContent = `Integrating ${selected.length} entities via LLM — this may take up to a minute...`;
  try {
    const d = await api('POST', '/admin/sync-prompt/apply', { entity_ids: selected, area_overrides });
    document.getElementById('sync-apply-status').textContent = d.summary || 'Done.';
    toast(d.summary || 'Prompt updated', 'ok');
    document.getElementById('sync-preview-panel').style.display = 'none';
    await loadPrompt();  // refresh the prompt textarea
  } catch(e) {
    document.getElementById('sync-apply-status').textContent = 'Failed: ' + e.message;
    toast('Apply failed: ' + e.message, 'err');
  }
}

// ── ACL ───────────────────────────────────────────────────────────────────────

async function loadAcl() {
  try {
    const d = await api('GET', '/admin/acl');
    document.getElementById('acl-text').value = d.text || '';
  } catch(e) { toast('Failed to load ACL: ' + e.message, 'err'); }
}

async function saveAcl() {
  try {
    await api('POST', '/admin/acl', { text: document.getElementById('acl-text').value });
    toast('ACL saved', 'ok');
  } catch(e) { toast('Save failed: ' + e.message, 'err'); }
}

// ── Sessions ──────────────────────────────────────────────────────────────────

async function loadSessions() {
  try {
    const d = await api('GET', '/admin/sessions');
    const tbody = document.getElementById('sessions-tbody');
    tbody.innerHTML = '';
    const count = d.active_sessions || 0;
    const sessions = d.sessions || [];
    if (count === 0) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><div class="empty-state-icon">💬</div><div class="empty-state-title">No active sessions</div><div class="empty-state-desc">Conversations will appear here when users connect to the avatar.</div></div></td></tr>';
      return;
    }
    sessions.forEach(sess => {
      const meta = sess.metadata || {};
      const tr = document.createElement('tr');
      const host = meta.host_label || meta.host || '—';
      const device = [meta.platform, meta.screen].filter(Boolean).join(' · ') || '—';
      const cs = sess.connected_seconds; const idle = typeof cs === 'number' ? (cs < 60 ? cs + 's' : cs < 3600 ? Math.floor(cs/60) + 'm ' + (cs%60) + 's' : Math.floor(cs/3600) + 'h ' + Math.floor((cs%3600)/60) + 'm') : '—';
      const msgCount = Number(sess.message_count || 0);
      tr.innerHTML = `
        <td>
          <div style="font-weight:600;color:var(--text1);">${esc(sess.session_id || 'unknown')}</div>
          ${meta.page_url ? `<div style="font-size:11px;color:var(--text3);word-break:break-all;">${esc(meta.page_url)}</div>` : ''}
        </td>
        <td>${sess.room_id ? `<span style="padding:2px 8px;border-radius:10px;background:var(--accent-soft,rgba(99,102,241,0.15));color:var(--accent,#818cf8);font-size:12px;font-weight:500;">${esc(sess.room_id.replace(/_/g,' '))}</span>` : '<span class="text-muted">—</span>'}</td>
        <td>${esc(host)}</td>
        <td>
          <div>${esc(device)}</div>
          ${meta.user_agent ? `<div style="font-size:11px;color:var(--text3);max-width:340px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(meta.user_agent)}</div>` : ''}
        </td>
        <td>${esc(idle)}</td>
        <td>${msgCount}</td>
        <td><button class="btn btn-outline btn-xs" onclick="clearSession('${esc(sess.session_id || '')}')">Clear</button></td>
      `;
      tbody.appendChild(tr);
    });
  } catch(e) { toast('Failed to load sessions: ' + e.message, 'err'); }
}

async function clearSession(sessionId) {
  if (!sessionId) return;
  try {
    await api('DELETE', '/admin/sessions/' + encodeURIComponent(sessionId));
    await loadSessions();
    toast('Session cleared', 'ok');
  } catch (e) {
    toast('Failed to clear session: ' + e.message, 'err');
  }
}

// ── Persistent memory ────────────────────────────────────────────────────────

function clearMemoryForm() {
  window._editingMemoryId = null;
  document.getElementById('memory-summary').value = '';
  document.getElementById('memory-category').value = 'general';
  document.getElementById('memory-confidence').value = '0.90';
  document.getElementById('memory-pinned').checked = false;
  const saveBtn = document.getElementById('memory-save-btn');
  if (saveBtn) saveBtn.textContent = 'Save Memory';
  const state = document.getElementById('memory-edit-state');
  if (state) {
    state.textContent = '';
    state.style.display = 'none';
  }
}

function _escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function _fmtMemoryTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString();
}

async function loadMemory() {
  const tbody = document.getElementById('memory-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="7" class="text-muted">Loading…</td></tr>';
  try {
    const d = await api('GET', '/admin/memory?n=200');
    const items = d.memories || [];
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><div class="empty-state-icon">🧠</div><div class="empty-state-title">No memories stored yet</div><div class="empty-state-desc">Nova learns from conversations, or you can add memories manually using the form above.</div></div></td></tr>';
      return;
    }
    tbody.innerHTML = items.map(m => `
      <tr>
        <td>
          <div style="font-weight:500;color:var(--text1);white-space:pre-wrap;">${_escapeHtml(m.summary)}</div>
          ${m.last_referenced_ts ? `<div class="text-xs text-muted mt-1">Last used ${_fmtMemoryTs(m.last_referenced_ts)}</div>` : ''}
        </td>
        <td><span class="badge">${_escapeHtml(m.category || 'general')}</span></td>
        <td>${Number(m.confidence || 0).toFixed(2)}</td>
        <td>${m.pinned ? '<span class="badge" style="background:rgba(34,197,94,.16);color:#86efac;">Pinned</span>' : '<span class="text-muted">No</span>'}</td>
        <td>${m.times_seen ?? 0}</td>
        <td style="color:var(--text2);font-size:12px;">${_fmtMemoryTs(m.updated_ts || m.created_ts)}</td>
        <td><div style="display:flex;gap:6px;white-space:nowrap;">
          <button class="btn btn-outline" onclick="editMemory(${m.id})">Edit</button>
          <button class="btn btn-outline" onclick="deleteMemory(${m.id})">Delete</button>
        </div></td>
      </tr>
    `).join('');
  } catch(e) {
    tbody.innerHTML = '<tr><td colspan="7" style="color:#fca5a5;">Failed to load memory.</td></tr>';
    toast('Failed to load memory: ' + e.message, 'err');
  }
}

async function saveMemory() {
  const summary = document.getElementById('memory-summary').value.trim();
  const category = document.getElementById('memory-category').value.trim() || 'general';
  const confidence = Number(document.getElementById('memory-confidence').value);
  const pinned = document.getElementById('memory-pinned').checked;
  if (!summary) return toast('Enter a memory summary first', 'warn');
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    return toast('Confidence must be between 0 and 1', 'warn');
  }
  try {
    if (window._editingMemoryId) {
      await api('PUT', '/admin/memory/' + encodeURIComponent(window._editingMemoryId), { summary, category, confidence, pinned });
      toast('Memory updated', 'ok');
    } else {
      await api('POST', '/admin/memory', { summary, category, confidence, pinned });
      toast('Memory saved', 'ok');
    }
    clearMemoryForm();
    await loadMemory();
  } catch(e) {
    toast('Failed to save memory: ' + e.message, 'err');
  }
}

async function editMemory(id) {
  try {
    const d = await api('GET', '/admin/memory?n=200');
    const memory = (d.memories || []).find(m => Number(m.id) === Number(id));
    if (!memory) return toast('Memory not found', 'err');
    window._editingMemoryId = Number(id);
    document.getElementById('memory-summary').value = memory.summary || '';
    document.getElementById('memory-category').value = memory.category || 'general';
    document.getElementById('memory-confidence').value = Number(memory.confidence || 0.9).toFixed(2);
    document.getElementById('memory-pinned').checked = !!memory.pinned;
    const saveBtn = document.getElementById('memory-save-btn');
    if (saveBtn) saveBtn.textContent = 'Update Memory';
    const state = document.getElementById('memory-edit-state');
    if (state) {
      state.textContent = `Editing memory #${id}`;
      state.style.display = '';
    }
    document.getElementById('memory-summary').focus();
  } catch(e) {
    toast('Failed to load memory for editing: ' + e.message, 'err');
  }
}

async function deleteMemory(id) {
  if (!confirm('Delete this memory?')) return;
  try {
    await api('DELETE', '/admin/memory/' + id);
    await loadMemory();
    toast('Memory deleted', 'ok');
  } catch(e) {
    toast('Delete failed: ' + e.message, 'err');
  }
}

async function clearAllMemory() {
  if (!confirm('Delete all stored memories? This cannot be undone.')) return;
  try {
    await api('DELETE', '/admin/memory');
    await loadMemory();
    toast('All memories cleared', 'ok');
  } catch(e) {
    toast('Clear failed: ' + e.message, 'err');
  }
}

// ── Server restart ────────────────────────────────────────────────────────────

async function restartServer() {
  if (!confirm('Restart the avatar-backend service? The server will be briefly unavailable.')) return;
  const btn = document.getElementById('restart-btn');
  btn.disabled = true; btn.textContent = 'Restarting…';
  try {
    await api('POST', '/admin/restart');
    toast('Server is restarting…', 'warn');
    setTimeout(() => {
      btn.disabled = false;
      btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-.49-4.72"/></svg> Restart';
      pollHealth();
    }, 8000);
  } catch(e) {
    toast('Restart failed: ' + e.message, 'err');
    btn.disabled = false;
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-.49-4.72"/></svg> Restart';
  }
}

// ── Test announce ─────────────────────────────────────────────────────────────

async function testAnnounce() {
  const message  = document.getElementById('announce-msg').value.trim();
  const priority = document.getElementById('announce-priority').value;
  const targetArea = document.getElementById('announce-target-area')?.value || '';
  if (!message) { toast('Enter a message', 'warn'); return; }
  try {
    await api('POST', '/admin/announce/test', { message, priority, target_area: targetArea });
    toast('Announcement sent', 'ok');
  } catch(e) { toast('Announce failed: ' + e.message, 'err'); }
}

// ── Users ─────────────────────────────────────────────────────────────────────

async function loadUsers() {
  try {
    const d = await api('GET', '/admin/users');
    const container = document.getElementById('users-list');
    if (!d.users.length) {
      container.innerHTML = '<p style="padding:16px 0;color:var(--text3);font-size:13px;">No users yet.</p>';
      return;
    }
    container.innerHTML = d.users.map(u => `
      <div class="user-row">
        <div class="user-row-icon">${esc(u.username.charAt(0).toUpperCase())}</div>
        <span class="user-name">${esc(u.username)}</span>
        <span class="role-badge ${u.role}">${u.role}</span>
        <div class="user-actions">
          <select id="role-sel-${esc(u.username)}" class="btn btn-outline btn-xs" style="padding:3px 8px;">
            <option value="viewer"${u.role==='viewer'?' selected':''}>Viewer</option>
            <option value="admin"${u.role==='admin'?' selected':''}>Admin</option>
          </select>
          <button class="btn btn-outline btn-xs" onclick="changeRole('${esc(u.username)}')">Set Role</button>
          <button class="btn btn-outline btn-xs" onclick="showPasswordChange('${esc(u.username)}')">Password</button>
          <button class="btn btn-danger btn-xs" onclick="deleteUser('${esc(u.username)}')">Delete</button>
        </div>
      </div>`).join('');
  } catch(e) { toast('Failed to load users: ' + e.message, 'err'); }
}

async function createUser() {
  const username = document.getElementById('new-username').value.trim();
  const password = document.getElementById('new-password').value;
  const role     = document.getElementById('new-role').value;
  if (!username || !password) { toast('Username and password required', 'warn'); return; }
  try {
    await api('POST', '/admin/users', { username, password, role });
    document.getElementById('new-username').value = '';
    document.getElementById('new-password').value = '';
    toast(`User '${username}' created`, 'ok');
    loadUsers();
  } catch(e) { toast('Failed: ' + e.message, 'err'); }
}

async function deleteUser(username) {
  if (!confirm(`Delete user '${username}'? This cannot be undone.`)) return;
  try {
    await api('DELETE', `/admin/users/${encodeURIComponent(username)}`);
    toast(`User '${username}' deleted`, 'ok');
    loadUsers();
  } catch(e) { toast('Failed: ' + e.message, 'err'); }
}

async function changeRole(username) {
  const role = document.getElementById(`role-sel-${username}`).value;
  try {
    await api('POST', `/admin/users/${encodeURIComponent(username)}/role`, { role });
    toast(`Role updated to '${role}'`, 'ok');
    loadUsers();
  } catch(e) { toast('Failed: ' + e.message, 'err'); }
}

function showPasswordChange(username) {
  _changePwTarget = username;
  document.getElementById('change-pw-name').textContent = username;
  document.getElementById('change-pw-input').value = '';
  document.getElementById('change-pw-card').style.display = '';
  document.getElementById('change-pw-input').focus();
}
function cancelPasswordChange() {
  _changePwTarget = null;
  document.getElementById('change-pw-card').style.display = 'none';
}
async function submitPasswordChange() {
  const pw = document.getElementById('change-pw-input').value;
  if (!pw) { toast('Enter a new password', 'warn'); return; }
  try {
    await api('POST', `/admin/users/${encodeURIComponent(_changePwTarget)}/password`, { new_password: pw });
    toast('Password updated', 'ok');
    cancelPasswordChange();
  } catch(e) { toast('Failed: ' + e.message, 'err'); }
}

// ── Util ──────────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function toggleReveal(id, btn) {
  const el = document.getElementById(id);
  if (!el) return;
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── AI Decision Log ───────────────────────────────────────────────────────────
let _decES     = null;
let _decFilter = 'all';
let _decEntries = [];
const _kindMeta = {
  triage_announce: { label:'▲ ANNOUNCE', bg:'rgba(16,185,129,.15)', color:'#4ade80' },
  triage_silence:  { label:'— SILENT',   bg:'rgba(71,85,105,.2)',   color:'#64748b' },
  tool_call:       { label:'⚙ TOOL',     bg:'rgba(129,140,248,.15)',color:'#818cf8' },
  chat_response:   { label:'💬 CHAT',    bg:'rgba(34,211,238,.12)', color:'#22d3ee' },
  motion_announce: { label:'📷 MOTION',  bg:'rgba(245,158,11,.15)', color:'#f59e0b' },
  weather_announce:{ label:'🌤 WEATHER', bg:'rgba(56,189,248,.12)', color:'#38bdf8' },
  heating_shadow_eval_start:    { label:'♨ SHADOW', bg:'rgba(250,204,21,.12)', color:'#facc15' },
  heating_shadow_action:        { label:'♨ SHADOW', bg:'rgba(250,204,21,.12)', color:'#facc15' },
  heating_shadow_eval_silent:   { label:'♨ SHADOW', bg:'rgba(250,204,21,.12)', color:'#facc15' },
  heating_shadow_eval_error:    { label:'♨ SHADOW', bg:'rgba(250,204,21,.12)', color:'#facc15' },
  heating_shadow_tool_call:     { label:'♨ SHADOW', bg:'rgba(250,204,21,.12)', color:'#facc15' },
  heating_shadow_round_silent:  { label:'♨ SHADOW', bg:'rgba(250,204,21,.12)', color:'#facc15' },
  heating_shadow_max_rounds:    { label:'♨ SHADOW', bg:'rgba(250,204,21,.12)', color:'#facc15' },
  heating_shadow_comparison:    { label:'♨ SHADOW ⚖', bg:'rgba(250,204,21,.18)', color:'#facc15' },
  auto_fix_issue_attempt: { label:'🛠 AUTO-FIX', bg:'rgba(250,204,21,.12)', color:'#facc15' },
  auto_fix_issue_resolved:{ label:'✓ FIXED',    bg:'rgba(74,222,128,.12)', color:'#4ade80' },
  coral_detection:        { label:'🪸 CORAL',    bg:'rgba(245,158,11,.15)', color:'#f59e0b' },
  motion_coral_filtered:  { label:'🪸 FILTERED', bg:'rgba(71,85,105,.2)',   color:'#64748b' },
};

function _kindBadge(kind) {
  const m = _kindMeta[kind] || { label: kind.toUpperCase(), bg:'rgba(255,255,255,.08)', color:'#e2e8f0' };
  return `<span style="background:${m.bg};color:${m.color};padding:1px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.3px;flex-shrink:0;">${m.label}</span>`;
}

function _heatingShadowOutcome(idx, entries) {
  for (let i = idx + 1; i < entries.length; i += 1) {
    const row = entries[i];
    if (row.kind === 'heating_shadow_eval_start') return '';
    if (row.kind === 'heating_shadow_eval_error') {
      return '<span style="background:rgba(244,63,94,.14);color:#fda4af;padding:1px 8px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.2px;flex-shrink:0;">FAILED</span>';
    }
    if (row.kind === 'heating_shadow_action' || row.kind === 'heating_shadow_eval_silent') {
      return '<span style="background:rgba(74,222,128,.14);color:#86efac;padding:1px 8px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.2px;flex-shrink:0;">SUCCEEDED</span>';
    }
  }
  return '';
}

function _decEntryHTML(e, idx = -1, entries = _decEntries) {
  let detail = '';
  const llmBadge = e.llm_tag
    ? `<span style="background:rgba(34,211,238,.12);color:#67e8f9;padding:1px 8px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.2px;flex-shrink:0;">${e.llm_tag}</span>`
    : '';
  let outcomeBadge = '';
  if (e.kind === 'triage_announce' || e.kind === 'triage_silence') {
    const ents = (e.entities || []).join(', ');
    detail = `<span class="text-muted">${ents}</span>`;
    if (e.message) detail += ` <span style="color:#86efac;">→ "${e.message}"</span>`;
    if (e.reason)  detail += ` <span class="text-muted">(${e.reason})</span>`;
  } else if (e.kind === 'tool_call') {
    const args = Object.entries(e.args || {}).map(([k,v])=>`${k}=${v}`).join(', ');
    const ok   = e.success ? '<span style="color:#4ade80;">✓</span>' : '<span style="color:#f43f5e;">✗</span>';
    detail = `${ok} <span style="color:#818cf8;">${e.tool || ''}</span>(<span class="">${args}</span>)`;
    if (e.result) detail += ` <span class="text-muted">→ ${e.result}</span>`;
  } else if (e.kind === 'chat_response') {
    detail = `<span style="color:#22d3ee;">"${e.query || ''}"</span>`;
    if (e.tool_count > 0) detail += ` <span class="text-muted">[${e.tool_count} tool${e.tool_count>1?'s':''}]</span>`;
    if (e.response) detail += ` <span class="text-muted">→ "${e.response}"</span>`;
    if (e.ms) detail += ` <span class="text-muted">(${e.ms}ms)</span>`;
  } else if (e.kind === 'motion_announce' || e.kind === 'weather_announce') {
    detail = `<span style="color:#f59e0b;">${e.camera || (e.old + ' → ' + e.new)}</span>`;
    if (e.message) detail += ` <span style="color:#86efac;">→ "${e.message}"</span>`;
  } else if (e.kind === 'auto_fix_issue_attempt') {
    const issue = e.issue_kind || 'unknown_issue';
    const action = e.action || 'noop';
    const ok = e.success ? '<span style="color:#4ade80;">✓</span>' : '<span style="color:#f43f5e;">✗</span>';
    detail = `${ok} <span style="color:#facc15;">${issue}</span>`;
    detail += ` <span class="text-muted">→ ${action}</span>`;
    if (e.detail) detail += ` <span class="text-muted">(${e.detail})</span>`;
    if (e.source) detail += ` <span class="text-muted">[${e.source}]</span>`;
  } else if (e.kind === 'auto_fix_issue_resolved') {
    const issue = e.issue_kind || 'unknown_issue';
    detail = `<span style="color:#4ade80;">${issue}</span>`;
    if (e.source) detail += ` <span class="text-muted">resolved via ${e.source}</span>`;
  } else if (e.kind === 'heating_shadow_eval_start') {
    detail = `<span style="color:#facc15;">Local heating shadow evaluation</span>`;
    if (e.season) detail += ` <span class="text-muted">(${e.season})</span>`;
    outcomeBadge = idx >= 0 ? _heatingShadowOutcome(idx, entries) : '';
  } else if (e.kind === 'heating_shadow_action') {
    detail = `<span style="color:#86efac;">${e.message || 'Shadow proposed an action'}</span>`;
  } else if (e.kind === 'heating_shadow_eval_silent') {
    detail = `<span class="text-muted">${e.reason || 'No local action suggested'}</span>`;
  } else if (e.kind === 'heating_shadow_eval_error') {
    detail = `<span style="color:#fda4af;">${e.reason || 'Shadow evaluation failed'}</span>`;
  } else if (e.kind === 'coral_detection') {
    const dets = (e.detections || []).join(', ') || 'unknown';
    const plate = e.has_plate_bearing ? ' 🚗' : '';
    detail = `<span style="color:#f59e0b;">${e.camera || ''}</span> <span style="color:#e2e8f0;">${dets}${plate}</span>`;
    if (e.inference_ms) detail += ` <span class="text-muted">${e.inference_ms}ms</span>`;
  } else if (e.kind === 'motion_coral_filtered') {
    detail = `<span style="color:#64748b;">${e.camera || ''}</span> <span class="text-muted">${e.reason || 'no detection'} (${e.inference_ms || '?'}ms)</span>`;
  }
  return `<div class="dec-entry" data-kind="${e.kind}"
               style="padding:5px 18px;border-bottom: 1px solid var(--border2);
                      display:flex;align-items:baseline;gap:8px;">
    <span style="color:var(--text3);min-width:58px;flex-shrink:0;">${e.ts}</span>
    ${_kindBadge(e.kind)}
    ${llmBadge}
    ${outcomeBadge}
    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${detail}</span>
  </div>`;
}

function _appendDecEntry(e) {
  _decEntries.push(e);
  if (_decEntries.length > 500) _decEntries.shift();
  const log = document.getElementById('dec-log');
  if (!log) return;
  // Skip full re-render: just append the new row to the DOM
  if (_decFilter !== 'all' && e.kind !== _decFilter) return;
  const placeholder = log.querySelector('.dec-placeholder');
  if (placeholder) placeholder.remove();
  const idx = _decEntries.length - 1;
  const tmp = document.createElement('div');
  tmp.innerHTML = _decEntryHTML(e, idx, _decEntries);
  const node = tmp.firstElementChild;
  if (node) log.appendChild(node);
  if (document.getElementById('dec-autoscroll')?.checked) log.scrollTop = log.scrollHeight;
}

function filterDecisions(btn) {
  _decFilter = btn.dataset.kind;
  document.querySelectorAll('.dec-filter').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const log = document.getElementById('dec-log');
  if (!log) return;
  const entries = _decFilter === 'all' ? _decEntries : _decEntries.filter(e => e.kind === _decFilter);
  if (entries.length === 0) {
    log.innerHTML = '<div style="padding:12px 18px;color:var(--text3);">No entries for this filter.</div>';
  } else {
    log.innerHTML = entries.map((row, index) => _decEntryHTML(row, index, entries)).join('');
    log.scrollTop = log.scrollHeight;
  }
}

function clearDecisions() {
  _decEntries = [];
  const log = document.getElementById('dec-log');
  if (log) log.innerHTML = '<div style="padding:12px 18px;color:var(--text3);">Log cleared.</div>';
}

function startDecisionStream() {
  if (_decES) { _decES.close(); _decES = null; }
  _decEntries = [];
  const decLog = document.getElementById('dec-log');
  if (decLog) decLog.innerHTML = '<div style="padding:12px 18px;color:var(--text3);">Loading…</div>';
  const status = document.getElementById('dec-status');
  if (status) status.textContent = 'Connecting…';
  fetch('/admin/decisions', { credentials:'include' })
    .then(r => r.json())
    .then(data => {
      _decEntries = (data.decisions || []).slice(-500);
      const entries = _decFilter === 'all' ? _decEntries : _decEntries.filter(r => r.kind === _decFilter);
      if (decLog) {
        if (entries.length === 0) {
          decLog.innerHTML = '<div class="dec-placeholder" style="padding:12px 18px;color:var(--text3);">No entries.</div>';
        } else {
          decLog.innerHTML = entries.map((row, i) => _decEntryHTML(row, i, entries)).join('');
          if (document.getElementById('dec-autoscroll')?.checked) decLog.scrollTop = decLog.scrollHeight;
        }
      }
      if (status) status.textContent = `${_decEntries.length} loaded`;
    })
    .catch(() => {});
  let _sseDecBacklogDone = false;
  _decES = new EventSource('/admin/decisions/stream');
  _decES.onopen = () => {
    if (status) status.textContent = '● Live';
    setTimeout(() => { _sseDecBacklogDone = true; }, 1500);
  };
  _decES.onmessage = (ev) => {
    if (!_sseDecBacklogDone) return;
    try { const e = JSON.parse(ev.data); if (e && e.kind) _appendDecEntry(e); } catch {}
  };
  _decES.onerror = () => { if (status) status.textContent = '⚠ Retrying…'; };
}

const _origNavigate = window.navigate;
window.navigate = function(el) {
  _origNavigate(el);
  if (el.dataset.section === 'decisions') startDecisionStream();
};

// ── LLM Cost Log ─────────────────────────────────────────────────────────────
let _costES = null;
let _costEntries = [];
let _costTotals = {};

const _purposeColor = {
  chat: '#22d3ee', proactive: '#f59e0b', triage: '#f59e0b', vision: '#a78bfa',
};

function _fmtTokens(n) {
  if (n >= 1000000) return (n/1000000).toFixed(2) + 'M';
  if (n >= 1000)    return (n/1000).toFixed(1) + 'K';
  return String(n);
}

function _fmtCost(usd) {
  if (usd === 0) return '<span class="text-muted">$0 local</span>';
  if (usd < 0.001) return `<span class="text-green">$${usd.toFixed(6)}</span>`;
  return `<span class="text-green">$${usd.toFixed(4)}</span>`;
}

function _costEntryHTML(e) {
  const col = _purposeColor[e.purpose] || '#e2e8f0';
  const rate = e.price_in > 0
    ? `<span class="text-xs text-muted">$${e.price_in}/$${e.price_out}/M</span>`
    : '<span class="text-xs text-muted">local</span>';
  return `<div style="padding:4px 18px;border-bottom: 1px solid var(--border2);
                      display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;">
    <span style="color:var(--text3);min-width:58px;flex-shrink:0;">${e.ts}</span>
    <span style="background: var(--surface2);color:${col};padding:1px 8px;border-radius:4px;font-size:10px;font-weight:700;flex-shrink:0;">${(e.purpose||'chat').toUpperCase()}</span>
    <span style="color:#818cf8;flex-shrink:0;">${e.model}</span>
    <span class="text-muted">↑${_fmtTokens(e.input_tokens)} ↓${_fmtTokens(e.output_tokens)}</span>
    ${_fmtCost(e.cost_usd)}
    <span class="text-muted">${e.elapsed_ms}ms</span>
    ${rate}
  </div>`;
}

function _updateCostSummary(totals) {
  _costTotals = totals;
  const el = id => document.getElementById(id);
  if (el('cost-calls'))      el('cost-calls').textContent = _fmtTokens(totals.session_calls || 0);
  if (el('cost-input-tok'))  el('cost-input-tok').textContent = _fmtTokens(totals.session_input_tokens || 0);
  if (el('cost-output-tok')) el('cost-output-tok').textContent = _fmtTokens(totals.session_output_tokens || 0);
  if (el('cost-total'))      el('cost-total').textContent = '$' + (totals.session_cost_usd || 0).toFixed(6);

  const bm = document.getElementById('cost-by-model');
  if (!bm) return;
  const models = Object.entries(totals.by_model || {});
  if (models.length === 0) {
    bm.innerHTML = '<div style="padding:12px 18px;color:var(--text3);font-size:13px;">No invocations yet.</div>';
    return;
  }
  bm.innerHTML = models.map(([key, m]) => `
    <div style="display:flex;align-items:center;gap:14px;padding:8px 18px;border-bottom: 1px solid var(--border2);flex-wrap:wrap;">
      <span style="color:#818cf8;min-width:180px;font-weight:500;">${key}</span>
      <span class="text-sm text-muted">${m.calls} calls</span>
      <span class="text-sm text-muted">↑${_fmtTokens(m.input_tokens)} ↓${_fmtTokens(m.output_tokens)}</span>
      <span style="font-size:13px;">${_fmtCost(m.cost_usd)}</span>
      <span style="color:var(--text3);font-size:11px;margin-left:auto;">$${m.price_in}/$${m.price_out} per M</span>
    </div>
  `).join('');
}

function _appendCostEntry(e) {
  _costEntries.push(e);
  if (_costEntries.length > 500) _costEntries.shift();
  const log = document.getElementById('cost-log');
  if (!log) return;
  if (log.querySelector('div[style*="Waiting"]')) log.innerHTML = '';
  log.insertAdjacentHTML('beforeend', _costEntryHTML(e));
  if (document.getElementById('cost-autoscroll')?.checked) log.scrollTop = log.scrollHeight;
  let si=0, so=0, sc=0;
  _costEntries.forEach(x => { si += x.input_tokens; so += x.output_tokens; sc += x.cost_usd; });
  _updateCostSummary({ session_calls: _costEntries.length, session_input_tokens: si, session_output_tokens: so, session_cost_usd: sc, by_model: _costTotals.by_model || {} });
}

function clearCosts() {
  _costEntries = [];
  const log = document.getElementById('cost-log');
  if (log) log.innerHTML = '<div style="padding:12px 18px;color:var(--text3);">Log cleared.</div>';
}

function startCostStream() {
  if (_costES) { _costES.close(); _costES = null; }
  // Reset state each time we (re-)open this section
  _costEntries = [];
  _costTotals  = {};
  const log = document.getElementById('cost-log');
  if (log) log.innerHTML = '<div style="padding:12px 18px;color:var(--text3);">Loading…</div>';
  const status = document.getElementById('cost-status');
  if (status) status.textContent = 'Connecting…';

  // Load snapshot (authoritative source), then open SSE only for NEW events
  fetch('/admin/costs', { credentials: 'include' })
    .then(r => r.json())
    .then(data => {
      if (data.totals) _updateCostSummary(data.totals);
      // Rebuild log from snapshot
      if (log) log.innerHTML = '';
      (data.entries || []).forEach(e => {
        _costEntries.push(e);
        if (log) log.insertAdjacentHTML('beforeend', _costEntryHTML(e));
      });
      if (document.getElementById('cost-autoscroll')?.checked && log) log.scrollTop = log.scrollHeight;
      if (status) status.textContent = `${_costEntries.length} loaded`;
    })
    .catch(() => {});

  // SSE stream — the server sends a 50-entry backlog on connect followed by live events.
  // We skip the backlog by only accepting entries with a ts strictly after stream open.
  const _streamOpenedAt = Date.now();
  let _sseBacklogDone = false;

  _costES = new EventSource('/admin/costs/stream');
  _costES.onopen = () => {
    if (status) status.textContent = '● Live';
    // Mark backlog as done after a short buffer so initial SSE backlog is ignored
    setTimeout(() => { _sseBacklogDone = true; }, 1500);
  };
  _costES.onmessage = (ev) => {
    if (!_sseBacklogDone) return;  // skip SSE backlog — already loaded via JSON fetch
    try { const e = JSON.parse(ev.data); if (e && e.model) _appendCostEntry(e); } catch {}
  };
  _costES.onerror = () => { if (status) status.textContent = '⚠ Retrying…'; };
}

const _origNavigateCost = window.navigate;
window.navigate = function(el) {
  _origNavigateCost(el);
  if (el.dataset.section === 'costs') startCostStream();
};


// ══════════════════════════════════════════════════════════════════

// ── Format helpers ────────────────────────────────────────────────────────────
function _fmtBytes(b) {
  if (b == null) return '—';
  if (b >= 1e12) return (b/1e12).toFixed(2) + ' TB';
  if (b >= 1e9)  return (b/1e9).toFixed(2)  + ' GB';
  if (b >= 1e6)  return (b/1e6).toFixed(1)  + ' MB';
  return (b/1e3).toFixed(0) + ' KB';
}

function _pct(used, total) { return total ? Math.round(used / total * 100) : 0; }

function _barColor(pct) {
  if (pct > 85) return 'red';
  if (pct > 65) return 'yellow';
  return 'green';
}

// ── Chart defaults ────────────────────────────────────────────────────────────
Chart.defaults.color           = '#64748b';
Chart.defaults.borderColor     = 'rgba(255,255,255,.05)';
Chart.defaults.font.family     = 'Inter, sans-serif';
Chart.defaults.font.size       = 11;

function _lineDataset(label, data, color, fill=false) {
  return {
    label, data,
    borderColor: color,
    backgroundColor: fill ? color.replace(')',', 0.12)').replace('rgb','rgba') : 'transparent',
    borderWidth: 2,
    pointRadius: 2,
    pointHoverRadius: 4,
    tension: 0.35,
    fill,
  };
}

function _barDataset(label, data, color) {
  return {
    label, data,
    backgroundColor: color,
    borderRadius: 4,
    borderWidth: 0,
  };
}

const _chartRegistry = {};

function _getOrCreate(id, cfg) {
  if (_chartRegistry[id]) { _chartRegistry[id].destroy(); }
  const ctx = document.getElementById(id);
  if (!ctx) return null;
  const c = new Chart(ctx, cfg);
  _chartRegistry[id] = c;
  return c;
}

// ── System Metrics ────────────────────────────────────────────────────────────
let _metricsES = null;

function _applyGauges(s) {
  if (!s) return;
  const _b = id => document.getElementById(id);

  // CPU
  const cpu = s.cpu_pct ?? 0;
  _b('m-cpu-val') && (_b('m-cpu-val').textContent = cpu.toFixed(1) + '%');
  _b('m-cpu-pct') && (_b('m-cpu-pct').textContent = '');
  const cpuBar = _b('m-cpu-bar');
  if (cpuBar) { cpuBar.style.width = Math.min(cpu,100) + '%'; cpuBar.className = 'gauge-bar-fill ' + _barColor(cpu); }

  // RAM
  const rp = _pct(s.ram_used, s.ram_total);
  _b('m-ram-val') && (_b('m-ram-val').textContent = _fmtBytes(s.ram_used));
  _b('m-ram-pct') && (_b('m-ram-pct').textContent = rp + '%');
  _b('m-ram-sub') && (_b('m-ram-sub').textContent = 'of ' + _fmtBytes(s.ram_total));
  const ramBar = _b('m-ram-bar');
  if (ramBar) { ramBar.style.width = rp + '%'; ramBar.className = 'gauge-bar-fill ' + _barColor(rp); }

  // Disk
  const dp = _pct(s.disk_used, s.disk_total);
  _b('m-disk-val') && (_b('m-disk-val').textContent = _fmtBytes(s.disk_used));
  _b('m-disk-pct') && (_b('m-disk-pct').textContent = dp + '%');
  _b('m-disk-sub') && (_b('m-disk-sub').textContent = 'of ' + _fmtBytes(s.disk_total));
  const diskBar = _b('m-disk-bar');
  if (diskBar) { diskBar.style.width = dp + '%'; diskBar.className = 'gauge-bar-fill ' + _barColor(dp); }

  // GPU
  const gpu = s.gpu_util ?? null;
  const gmp = s.gpu_mem_used != null ? _pct(s.gpu_mem_used, s.gpu_mem_total) : null;
  _b('m-gpu-val') && (_b('m-gpu-val').textContent = gpu != null ? gpu.toFixed(0) + '%' : 'N/A');
  _b('m-gpu-pct') && (_b('m-gpu-pct').textContent = gpu != null ? gpu.toFixed(0) + '%' : '');
  _b('m-gpu-sub') && (_b('m-gpu-sub').textContent = s.gpu_mem_used != null ? _fmtBytes(s.gpu_mem_used) + ' / ' + _fmtBytes(s.gpu_mem_total) : '');
  const gpuBar = _b('m-gpu-bar');
  if (gpuBar && gpu != null) { gpuBar.style.width = gpu + '%'; gpuBar.className = 'gauge-bar-fill ' + _barColor(gpu); }
}

function _buildMetricsCharts(history) {
  const labels = history.map(h => h.hour ? h.hour.slice(11,16) : '');
  const cpus   = history.map(h => h.cpu_pct != null ? +h.cpu_pct.toFixed(1) : null);
  const rams   = history.map(h => (h.ram_used != null && h.ram_total) ? +(h.ram_used / h.ram_total * 100).toFixed(1) : null);
  const gpus   = history.map(h => h.gpu_util != null ? +h.gpu_util.toFixed(1) : null);

  _getOrCreate('chart-cpu-ram', {
    type: 'line',
    data: {
      labels,
      datasets: [
        _lineDataset('CPU %', cpus, '#22d3ee', true),
        _lineDataset('RAM %', rams, '#818cf8', false),
      ],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: true, position: 'top' } },
      scales: { y: { min: 0, max: 100, ticks: { callback: v => v + '%' } }, x: { ticks: { maxTicksLimit: 12 } } },
    },
  });

  _getOrCreate('chart-gpu', {
    type: 'line',
    data: {
      labels,
      datasets: [_lineDataset('GPU %', gpus, '#10b981', true)],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { y: { min: 0, max: 100, ticks: { callback: v => v + '%' } }, x: { ticks: { maxTicksLimit: 12 } } },
    },
  });
}

async function loadMetrics() {
  try {
    const d = await api('GET', '/admin/metrics');
    _applyGauges(d.latest);
    if (d.history && d.history.length) _buildMetricsCharts(d.history);
  } catch(e) { console.warn('metrics load err', e); }

  // SSE live updates
  if (_metricsES) { _metricsES.close(); _metricsES = null; }
  _metricsES = new EventSource('/admin/metrics/stream');
  _metricsES.onmessage = (ev) => {
    try { const s = JSON.parse(ev.data); if (s.cpu_pct != null) { _applyGauges(s); _applyGaugeDash(s); } } catch {}
  };
}

// Stop metrics SSE when leaving section
const _origNavigateMetrics = window.navigate;
window.navigate = function(el) {
  _origNavigateMetrics(el);
  if (el.dataset.section !== 'metrics' && _metricsES) {
    _metricsES.close(); _metricsES = null;
  }
  if (el.dataset.section === 'metrics') loadMetrics();
};

// ── Persistent LLM Cost Chart ─────────────────────────────────────────────────
let _costPeriod = 'month';

function selectCostPeriod(btn) {
  _costPeriod = btn.dataset.period;
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadCostHistory(_costPeriod);
}

function _buildCostChart(byDay) {
  const labels = byDay.map(r => r.day || r.month || '');
  const costs  = byDay.map(r => +(r.cost_usd || 0).toFixed(6));
  _getOrCreate('chart-cost', {
    type: 'bar',
    data: {
      labels,
      datasets: [_barDataset('Cost (USD)', costs, 'rgba(16,185,129,.7)')],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { ticks: { callback: v => '$' + (v < 0.001 ? v.toFixed(6) : v.toFixed(4)) } },
        x: { ticks: { maxTicksLimit: 14, maxRotation: 45 } },
      },
    },
  });
}

async function loadCostHistory(period='month') {
  try {
    const d = await api('GET', '/admin/costs/history?period=' + period);
    const s = d.summary || {};
    const cost = s.cost_usd || 0;
    document.getElementById('cost-db-calls') && (document.getElementById('cost-db-calls').textContent = _fmtTokens(s.calls || 0));
    document.getElementById('cost-db-in')    && (document.getElementById('cost-db-in').textContent    = _fmtTokens(s.input_tokens || 0));
    document.getElementById('cost-db-out')   && (document.getElementById('cost-db-out').textContent   = _fmtTokens(s.output_tokens || 0));
    document.getElementById('cost-db-total') && (document.getElementById('cost-db-total').textContent = '$' + (cost < 0.01 ? cost.toFixed(6) : cost.toFixed(4)));

    const chartData = period === 'year' ? (d.monthly || []) : (d.by_day || []);
    if (chartData.length) _buildCostChart(chartData);
  } catch(e) { console.warn('cost history err', e); }
}

// Hook into cost section navigation to also load DB history
const _origNavigateCostHist = window.navigate;
window.navigate = function(el) {
  _origNavigateCostHist(el);
  if (el.dataset.section === 'costs') loadCostHistory(_costPeriod);
};



  // ── Server Logs (pylog) ───────────────────────────────────────────
  let _pylogES = null, _pylogAll = [], _pylogLevel = '', _pylogSearch = '';
  const _LEVEL_COL = {debug:'#475569',info:'#94a3b8',warning:'#fbbf24',error:'#f87171',critical:'#f97316'};

  function initPylog() {
    if (_pylogES) return;
    fetch('/admin/pylog?n=500').then(r=>r.json()).then(d=>{
      _pylogAll = d.entries||[];
      _renderPylog();
    }).catch(()=>{});
    _pylogES = new EventSource('/admin/pylog/stream');
    _pylogES.onopen  = ()=>{ document.getElementById('pylog-status').textContent='live'; };
    _pylogES.onerror = ()=>{ document.getElementById('pylog-status').textContent='disconnected'; };
    _pylogES.onmessage = e => {
      try {
        const entry = JSON.parse(e.data);
        _pylogAll.push(entry);
        if (_pylogAll.length > 2000) _pylogAll.shift();
        if (_pylogMatch(entry)) {
          _appendPylogLine(entry);
          if (document.getElementById('pylog-autoscroll').checked) {
            const o = document.getElementById('pylog-output'); o.scrollTop = o.scrollHeight;
          }
        }
      } catch(_){}
    };
  }

  function _pylogMatch(e) {
    if (_pylogLevel && e.level !== _pylogLevel) return false;
    if (_pylogSearch) {
      const q = _pylogSearch.toLowerCase();
      if (!(e.event||'').toLowerCase().includes(q) &&
          !(e.logger||'').toLowerCase().includes(q) &&
          !JSON.stringify(e).toLowerCase().includes(q)) return false;
    }
    return true;
  }

  function _renderPylog() {
    const out = document.getElementById('pylog-output');
    out.innerHTML = '';
    const filtered = _pylogAll.filter(_pylogMatch);
    if (!filtered.length) {
      out.innerHTML = '<div style="padding:12px 18px;color:var(--text3);">No entries match filter.</div>';
      return;
    }
    filtered.forEach(_appendPylogLine);
    if (document.getElementById('pylog-autoscroll').checked) out.scrollTop = out.scrollHeight;
  }

  function _appendPylogLine(entry) {
    const out = document.getElementById('pylog-output');
    const placeholder = out.querySelector('div[style*="color:var(--text3)"]');
    if (placeholder) placeholder.remove();
    const col = _LEVEL_COL[entry.level]||'#94a3b8';
    const extras = Object.entries(entry)
      .filter(([k])=>!['ts','level','event','logger'].includes(k))
      .map(([k,v])=>`<span style="color:#64748b;">${k}=</span><span style="color:#7dd3fc;">${_esc(JSON.stringify(v))}</span>`)
      .join(' ');
    const div = document.createElement('div');
    div.style.cssText='padding:1px 18px;white-space:pre-wrap;word-break:break-all;border-bottom: 1px solid var(--border);';
    div.innerHTML =
      `<span style="color:#475569;">${entry.ts||''}</span> ` +
      `<span style="color:${col};font-weight:600;display:inline-block;min-width:54px;">${(entry.level||'').toUpperCase()}</span> ` +
      `<span style="color:#64748b;">[${_esc(entry.logger||'')}]</span> ` +
      `<span class="">${_esc(entry.event||'')}</span>` +
      (extras ? ` <span style="margin-left:8px;">${extras}</span>` : '');
    out.appendChild(div);
  }

  function setPylogLevel(btn) {
    document.querySelectorAll('.pylog-level').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active'); _pylogLevel=btn.dataset.level; _renderPylog();
  }
  function filterPylog() { _pylogSearch=document.getElementById('pylog-search').value; _renderPylog(); }
  function clearPylog() { _pylogAll=[]; document.getElementById('pylog-output').innerHTML='<div style="padding:12px 18px;color:var(--text3);">Cleared.</div>'; }
  function _esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }


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

// ── Toast System (uses global toast() defined at top) ───────────────────────


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


// ── Dashboard Charts (Historical — Dribbble style) ──────────────────────────
let _dashCpuChart = null;
let _dashGpuChart = null;
let _dashChartsInitialized = false;
const _DASH_CHART_MAX_POINTS = 60;

async function _initDashCharts() {
  if (_dashChartsInitialized) return;
  const cpuCanvas = document.getElementById('dash-chart-cpu');
  const gpuCanvas = document.getElementById('dash-chart-mem');
  if (!cpuCanvas || !gpuCanvas) return;
  _dashChartsInitialized = true;

  const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  const gridColor = isDark ? 'rgba(255,255,255,.06)' : 'rgba(0,0,0,.06)';
  const tickColor = isDark ? '#5a5d7a' : '#9a9cb8';
  const legendColor = isDark ? '#a0a3bd' : '#5a5d7a';

  const baseOpts = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 400 },
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: true, position: 'top', align: 'start',
        labels: { boxWidth: 10, boxHeight: 10, padding: 16, usePointStyle: true, pointStyle: 'rectRounded',
          font: { size: 12, family: "'Plus Jakarta Sans', sans-serif", weight: '500' }, color: legendColor } },
      tooltip: { backgroundColor: isDark ? '#1c1e2e' : '#fff', titleColor: isDark ? '#f0f0f5' : '#1a1c2e',
        bodyColor: isDark ? '#a0a3bd' : '#5a5d7a', borderColor: isDark ? 'rgba(255,255,255,.1)' : 'rgba(0,0,0,.1)',
        borderWidth: 1, padding: 12, cornerRadius: 10,
        titleFont: { size: 13, weight: '600', family: "'Plus Jakarta Sans', sans-serif" },
        bodyFont: { size: 12, family: "'Plus Jakarta Sans', sans-serif" },
        callbacks: { label: ctx => ' ' + ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(1) + '%' } }
    },
    scales: {
      x: { display: true, grid: { display: false },
        ticks: { font: { size: 10, family: "'Plus Jakarta Sans', sans-serif" }, color: tickColor, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
      y: { min: 0, max: 100, grid: { color: gridColor, drawBorder: false },
        ticks: { font: { size: 10, family: "'Plus Jakarta Sans', sans-serif" }, color: tickColor, callback: v => v + '%', stepSize: 20 },
        border: { display: false } }
    }
  };

  // Load historical data from the metrics API
  let histLabels = [], histCpu = [], histRam = [], histGpu = [];
  try {
    const d = await api('GET', '/admin/metrics');
    const history = d.history || [];
    history.forEach(h => {
      const t = new Date(h.hour || h.ts);
      histLabels.push(t.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}));
      histCpu.push(h.cpu_pct ?? 0);
      histRam.push(h.ram_total ? (h.ram_used / h.ram_total * 100) : 0);
      histGpu.push(h.gpu_util ?? 0);
    });
  } catch(e) { /* no history available */ }

  _dashCpuChart = new Chart(cpuCanvas, {
    type: 'line',
    data: {
      labels: histLabels.length ? histLabels : [],
      datasets: [
        { label: 'CPU %', data: histCpu, borderColor: '#00cec9', backgroundColor: 'rgba(0,206,201,.15)',
          borderWidth: 2.5, tension: .4, fill: true, pointRadius: 0, pointHoverRadius: 4 },
        { label: 'RAM %', data: histRam, borderColor: '#6c5ce7', backgroundColor: 'transparent',
          borderWidth: 2, tension: .4, fill: false, pointRadius: 2, pointBackgroundColor: '#6c5ce7',
          pointBorderColor: '#6c5ce7', pointHoverRadius: 5, borderDash: [] },
      ]
    },
    options: { ...baseOpts }
  });

  _dashGpuChart = new Chart(gpuCanvas, {
    type: 'line',
    data: {
      labels: histLabels.length ? histLabels : [],
      datasets: [
        { label: 'GPU %', data: histGpu, borderColor: '#00b894', backgroundColor: 'rgba(0,184,148,.15)',
          borderWidth: 2.5, tension: .4, fill: true, pointRadius: 0, pointHoverRadius: 4 },
      ]
    },
    options: { ...baseOpts }
  });
}

function _updateDashCharts(s) {
  if (!s || !_dashCpuChart) return;
  const now = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

  // CPU & RAM chart
  const cpuData = _dashCpuChart.data;
  cpuData.labels.push(now);
  cpuData.datasets[0].data.push(s.cpu_pct ?? 0);
  const ramPct = (s.ram_total && s.ram_used) ? (s.ram_used / s.ram_total * 100) : 0;
  cpuData.datasets[1].data.push(ramPct);
  if (cpuData.labels.length > _DASH_CHART_MAX_POINTS) {
    cpuData.labels.shift();
    cpuData.datasets.forEach(ds => ds.data.shift());
  }
  _dashCpuChart.update('none');

  // GPU chart
  if (_dashGpuChart) {
    const gpuData = _dashGpuChart.data;
    gpuData.labels.push(now);
    gpuData.datasets[0].data.push(s.gpu_util ?? 0);
    if (gpuData.labels.length > _DASH_CHART_MAX_POINTS) {
      gpuData.labels.shift();
      gpuData.datasets.forEach(ds => ds.data.shift());
    }
    _dashGpuChart.update('none');
  }
}


// ── Lazy Video Loading (IntersectionObserver) ───────────────────────────────
const _videoObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      const video = entry.target;
      if (video.dataset.src && !video.src) {
        video.src = video.dataset.src;
        video.preload = 'metadata';
      }
      _videoObserver.unobserve(video);
    }
  });
}, { rootMargin: '200px 0px', threshold: 0.01 });

function _observeVideos(container) {
  if (!container) return;
  container.querySelectorAll('video[data-src]').forEach(v => {
    if (!v.src) _videoObserver.observe(v);
  });
}


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

// ── Announcement Log ─────────────────────────────────────────────────────────

const _SOURCE_LABELS = {
  announce: 'Manual',
  proactive: 'Proactive',
  doorbell: 'Doorbell',
  media_fun_fact: 'Media Fun Fact',
};

function _fmtAnnounceTs(ts) {
  try {
    return new Date(ts).toLocaleString(undefined, {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch { return ts || '—'; }
}

async function loadAnnouncementLog() {
  const el = document.getElementById('announce-log-list');
  if (!el) return;
  try {
    const d = await api('GET', '/admin/announcements?limit=200');
    const items = d.announcements || [];
    if (!items.length) {
      el.innerHTML = '<div class="text-sm text-muted" style="padding:8px 0;">No announcements logged yet.</div>';
      return;
    }
    el.innerHTML = items.map(a => {
      const label = _SOURCE_LABELS[a.source] || a.source || 'Unknown';
      const areas = Array.isArray(a.target_areas) && a.target_areas.length ? a.target_areas.join(', ') : 'All';
      const priClass = a.priority === 'alert' ? 'coral-plate' : 'good';
      const queryHtml = a.query ? `<div class="announce-log-query">${_esc(a.query)}</div>` : '';
      return `<div class="announce-log-row">
        <div class="announce-log-meta">
          <span class="motion-chip ${priClass}" style="font-size:10px;padding:2px 7px;">${_esc(a.priority)}</span>
          <span class="motion-chip" style="font-size:10px;padding:2px 7px;">${_esc(label)}</span>
          <span class="announce-log-areas">${_esc(areas)}</span>
          <span class="announce-log-time">${_esc(_fmtAnnounceTs(a.ts))}</span>
        </div>
        ${queryHtml}<div class="announce-log-text">${_esc(a.text)}</div>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = `<div class="text-sm" style="color:var(--danger,#ff453a);padding:8px 0;">Failed to load announcement log: ${_esc(e.message || String(e))}</div>`;
  }
}

async function clearAnnouncementLog() {
  if (!confirm('Clear all announcement history?')) return;
  try {
    await api('DELETE', '/admin/announcements');
    await loadAnnouncementLog();
    toast('Announcement log cleared');
  } catch(e) {
    alert('Failed to clear: ' + (e.message || e));
  }
}

// ── Heating Shadow Monitor ────────────────────────────────────────────────────

async function loadHeatingShadow() {
  const el = document.getElementById('heating-shadow-list');
  if (!el) return;
  try {
    const d = await api('GET', '/admin/heating-shadow/history?limit=80');
    const entries = d.entries || [];
    if (!entries.length) {
      el.innerHTML = '<div class="text-sm text-muted" style="padding:8px 0;">No shadow evaluations yet. Shadow runs automatically every 30 min alongside the primary heating eval.</div>';
      return;
    }

    // Group entries into evaluation runs by pairing eval_start → comparison
    const runs = [];
    let current = null;
    for (const e of entries) {
      if (e.kind === 'heating_shadow_eval_start') {
        current = { start: e, tools: [], comparison: null };
        runs.push(current);
      } else if (current) {
        if (e.kind === 'heating_shadow_tool_call') current.tools.push(e);
        else if (e.kind === 'heating_shadow_comparison') { current.comparison = e; current = null; }
        else if (['heating_shadow_round_silent','heating_shadow_eval_error','heating_shadow_max_rounds'].includes(e.kind)) {
          current.endEvent = e;
        }
      }
    }

    // Update model label from first start entry
    const firstStart = entries.find(e => e.kind === 'heating_shadow_eval_start');
    if (firstStart?.llm_model) {
      const lbl = document.getElementById('shadow-model-label');
      if (lbl) lbl.textContent = firstStart.llm_model;
    }

    el.innerHTML = runs.slice().reverse().map(run => {
      const cmp = run.comparison;
      const agreement = cmp?.agreement || (run.endEvent?.kind === 'heating_shadow_eval_error' ? 'error' : 'pending');
      const agreeLabel = {
        both_silent:   '<span class="shadow-agree">✓ Both silent</span>',
        both_acted:    '<span class="shadow-agree">✓ Both acted</span>',
        shadow_only:   '<span class="shadow-diverge">⚠ Shadow acted, primary silent</span>',
        primary_only:  '<span class="shadow-diverge">⚠ Primary acted, shadow silent</span>',
        error:         '<span style="color:var(--danger)">✗ Error</span>',
        pending:       '<span class="shadow-silent">… pending</span>',
      }[agreement] || `<span>${_esc(agreement)}</span>`;

      const writes = run.tools.filter(t => t.is_write);
      const reads  = run.tools.filter(t => !t.is_write);
      const season = run.start?.season || '—';
      const ts     = run.start?.ts || '';
      const shadowOnly = run.start?.shadow_only ? ' <span style="color:#60a5fa;font-size:10px;">[manual]</span>' : '';

      const toolRows = run.tools.map(t => {
        const cls = t.is_write ? 'shadow-tool-write' : 'shadow-tool-read';
        const icon = t.is_write ? '✎' : '↳';
        const entity = t.args?.entity_id || '';
        const argsStr = Object.entries(t.args||{}).map(([k,v])=>`${k}=${v}`).join(', ');
        return `<div class="shadow-tool-row ${cls}">${icon} r${t.round} ${_esc(t.tool)}(${_esc(argsStr)})${entity ? ` <em>${_esc(entity)}</em>` : ''}${t.is_write ? ' <span style="opacity:.5">[intercepted]</span>' : ''}</div>`;
      }).join('');

      const entityDiff = cmp ? (() => {
        const parts = [];
        if (cmp.entity_overlap?.length) parts.push(`<span style="color:#4ade80">match: ${cmp.entity_overlap.join(', ')}</span>`);
        if (cmp.entity_shadow_only?.length) parts.push(`<span style="color:#f97316">shadow-only: ${cmp.entity_shadow_only.join(', ')}</span>`);
        if (cmp.entity_primary_only?.length) parts.push(`<span style="color:#60a5fa">primary-only: ${cmp.entity_primary_only.join(', ')}</span>`);
        return parts.length ? `<div style="font-size:11px;margin-top:4px;">${parts.join(' · ')}</div>` : '';
      })() : '';

      return `<div class="shadow-eval-row">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:4px;margin-bottom:4px;">
          <div>${agreeLabel}${shadowOnly} <span class="text-xs text-muted" style="margin-left:6px;">${_esc(season)}</span></div>
          <span class="text-xs text-muted">${_esc(ts)}</span>
        </div>
        <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">
          ${writes.length} write${writes.length!==1?'s':''} intercepted · ${reads.length} read${reads.length!==1?'s':''} executed · ${run.tools.length} total calls
        </div>
        ${entityDiff}
        ${toolRows ? `<details style="margin-top:4px;"><summary style="font-size:11px;cursor:pointer;color:var(--text3);">Show tool calls (${run.tools.length})</summary>${toolRows}</details>` : ''}
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = `<div class="text-sm" style="color:var(--danger);padding:8px 0;">Failed to load: ${_esc(e.message||String(e))}</div>`;
  }
}

async function forceHeatingShadow(scenario) {
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = 'Running…'; }
  try {
    const d = await api('POST', `/admin/heating-shadow/force?scenario=${encodeURIComponent(scenario)}`);
    if (d.ok) {
      toast(`Shadow ${scenario} test done — ${d.write_calls_intercepted} writes intercepted, ${d.read_calls_executed} reads executed`);
      await loadHeatingShadow();
    } else {
      alert('Shadow test failed: ' + (d.message || 'unknown error'));
    }
  } catch(e) {
    alert('Error: ' + (e.message || e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = scenario === 'winter' ? '▶ Run Winter Test' : '▶ Run Spring Test'; }
  }
}

// ── Conversation Audit Trail ──────────────────────────────────────────────────

async function loadConversationAudit() {
  const el = document.getElementById('audit-list');
  if (!el) return;
  el.innerHTML = '<div class="text-sm text-muted" style="padding:8px 0;">Loading…</div>';
  const sid = (document.getElementById('audit-session-filter')?.value || '').trim();
  const url = sid ? `/admin/conversations/${encodeURIComponent(sid)}` : '/admin/conversations?limit=100';
  try {
    const d = await api('GET', url);
    const items = d.conversations || [];
    if (!items.length) {
      el.innerHTML = '<div class="text-sm text-muted" style="padding:8px 0;">No conversations found.</div>';
      return;
    }
    el.innerHTML = items.map(a => {
      const tc = Array.isArray(a.tool_calls) ? a.tool_calls : [];
      const toolBadges = tc.map(t =>
        `<span class="motion-chip ${t.status==='allowed'?'good':'coral-plate'}" style="font-size:10px;padding:2px 7px;">${_esc(t.name)}</span>`
      ).join(' ');
      const ts = (a.ts||'').replace('T',' ').slice(0,19);
      return `<div style="padding:10px 0;border-bottom:1px solid var(--border);">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px;">
          <span class="motion-chip" style="font-size:10px;padding:2px 7px;">${_esc(a.model||'?')}</span>
          <span class="text-xs text-muted">${_esc(ts)}</span>
          <span class="text-xs text-muted">${a.processing_ms||0}ms</span>
          ${toolBadges}
        </div>
        <div class="text-sm" style="margin-bottom:2px;"><strong>User:</strong> ${_esc((a.user_text||'').slice(0,200))}</div>
        <div class="text-sm text-muted">${_esc((a.final_reply||'').slice(0,300))}</div>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = `<div class="text-sm" style="color:var(--danger,#ff453a);padding:8px 0;">Failed: ${_esc(e.message||String(e))}</div>`;
  }
}

// ── Wake Word Training ────────────────────────────────────────────────────────

async function loadWakeStatus() {
  const el = document.getElementById('wake-status');
  if (!el) return;
  try {
    const d = await api('GET', '/admin/coral/wake-status');
    const stages = (d.pipeline_stages || []).join(' → ');
    const lines = [
      `<strong>Pipeline:</strong> ${stages || 'not initialized'}`,
      `Coral TPU: ${d.coral_available ? '✅ Active (~1-3ms)' : '❌ Not available'}`,
    ];
    // Show the best available classifier — only one should be active
    if (d.cpu_tflite_available) {
      lines.push(`CPU TFLite: ✅ Active (~3-8ms)`);
    }
    if (d.numpy_model_available) {
      lines.push(`Numpy Classifier: ✅ Active (~3-5ms)`);
    }
    lines.push(`Verifier: ${d.verifier_available ? '✅ Active' : (d.verifier_model_exists ? '✅ Trained' : '⚠ Not trained')}`);
    lines.push(`VAD Gate: ${d.vad_available ? '✅ Active' : '❌ Not available'}`);
    lines.push(`Whisper Fallback: ✅ Ready`);
    lines.push(`Edge TPU Model: ${d.coral_model_exists ? '✅ Present' : '⚠ Not present'}`);
    lines.push(`Edge TPU Compiler: ${d.edgetpu_compiler_available ? '✅ Installed' : '⚠ Not installed (optional)'}`);
    const compilerBtn = document.getElementById('wake-install-compiler-btn');
    if (compilerBtn) compilerBtn.style.display = d.edgetpu_compiler_available ? 'none' : '';
    el.innerHTML = lines.join('<br>');
  } catch (e) {
    if (!el._retries) el._retries = 0;
    if (++el._retries < 3) setTimeout(loadWakeStatus, 2000);
    else el.textContent = 'Could not load wake word status.';
  }
}

async function installEdgeTPUCompiler() {
  const btn = document.getElementById('wake-install-compiler-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Installing...';
  try {
    const d = await api('POST', '/admin/coral/install-edgetpu-compiler');
    if (d.ok) {
      toast('Edge TPU compiler installed! Re-train to compile for TPU.', 'ok');
    } else {
      toast(d.message || 'Installation failed', 'err');
    }
    await loadWakeStatus();
  } catch (e) {
    toast('Installation failed: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Install Edge TPU Compiler';
  }
}

async function trainWakeWord() {
  const btn = document.getElementById('wake-train-btn');
  const progress = document.getElementById('wake-progress');
  const bar = document.getElementById('wake-progress-bar');
  const text = document.getElementById('wake-progress-text');
  const wakeWord = (document.getElementById('wake-word-input')?.value || 'Nova').trim();
  if (!wakeWord) { toast('Enter a wake word first', 'err'); return; }
  btn.disabled = true;
  progress.style.display = '';
  bar.style.width = '0%';
  text.textContent = `Training "${wakeWord}"...`;

  try {
    const resp = await fetch('/admin/coral/train-wakeword', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wake_word: wakeWord }),
    });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const d = JSON.parse(line.slice(6));
          bar.style.width = d.progress + '%';
          text.textContent = d.message || '';
          if (d.stage === 'done') {
            toast(d.message, 'ok');
            await loadWakeStatus();
          } else if (d.stage === 'error') {
            toast(d.message, 'err');
          }
        } catch (_) {}
      }
    }
  } catch (e) {
    toast('Training failed: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

// Load wake status when Tools section is shown
// Use MutationObserver to detect when tools section becomes visible
setTimeout(() => {
  const toolsSection = document.getElementById('section-tools');
  if (toolsSection) {
    const observer = new MutationObserver(() => {
      if (toolsSection.classList.contains('active')) {
        loadWakeStatus();
        loadAnnouncementLog();
      }
    });
    observer.observe(toolsSection, { attributes: true, attributeFilter: ['class'] });
  }
}, 500);

// ── Prompts & Tuning ──────────────────────────────────────────────────────────

function toggleTuningCard(headerEl) {
  const card = headerEl.parentElement;
  const body = card.querySelector('.collapse-body');
  const chev = headerEl.querySelector('.chev');
  if (!body) return;
  const isHidden = body.style.display === 'none';
  body.style.display = isHidden ? '' : 'none';
  if (chev) chev.style.transform = isHidden ? '' : 'rotate(-90deg)';
}

const _COOLDOWN_KEYS = [
  'PROACTIVE_ENTITY_COOLDOWN_S',
  'PROACTIVE_CAMERA_COOLDOWN_S',
  'PROACTIVE_GLOBAL_MOTION_COOLDOWN_S',
  'PROACTIVE_GLOBAL_ANNOUNCE_COOLDOWN_S',
  'PROACTIVE_QUEUE_DEDUP_COOLDOWN_S',
  'PROACTIVE_BATCH_WINDOW_S',
  'PROACTIVE_MAX_BATCH_CHANGES',
  'PROACTIVE_WEATHER_COOLDOWN_S',
  'PROACTIVE_FORECAST_HOUR',
  'HA_POWER_ALERT_COOLDOWN_S',
];

const _COOLDOWN_DEFAULTS = {
  'PROACTIVE_ENTITY_COOLDOWN_S': 600,
  'PROACTIVE_CAMERA_COOLDOWN_S': 600,
  'PROACTIVE_GLOBAL_MOTION_COOLDOWN_S': 600,
  'PROACTIVE_GLOBAL_ANNOUNCE_COOLDOWN_S': 300,
  'PROACTIVE_QUEUE_DEDUP_COOLDOWN_S': 120,
  'PROACTIVE_BATCH_WINDOW_S': 60,
  'PROACTIVE_MAX_BATCH_CHANGES': 20,
  'PROACTIVE_WEATHER_COOLDOWN_S': 3600,
  'PROACTIVE_FORECAST_HOUR': 7,
  'HA_POWER_ALERT_COOLDOWN_S': 1800,
};

let _promptsData = [];
let _currentPromptSlug = null;

function _humanTime(seconds) {
  const s = Number(seconds);
  if (isNaN(s)) return seconds;
  if (s >= 3600) return (s / 3600).toFixed(1).replace(/\.0$/, '') + 'h';
  if (s >= 60) return (s / 60).toFixed(0) + 'm';
  return s + 's';
}

async function loadPromptsTuning() {
  try {
    const cfg = await api('GET', '/admin/config');
    const container = document.getElementById('cooldown-fields');
    container.innerHTML = '';
    const fields = cfg.fields || {};
    const values = cfg.values || {};
    for (const key of _COOLDOWN_KEYS) {
      const meta = fields[key];
      if (!meta) continue;
      const [label] = meta;
      const val = values[key] || '';
      const row = document.createElement('div');
      row.className = 'config-row';
      row.innerHTML = `
        <label class="config-label">
          <span>${label}</span>
          ${val ? `<span style="font-size:.7rem;color:var(--text3);margin-left:.4rem;">(current: ${_humanTime(val)})</span>` : ''}
        </label>
        <input class="config-input" data-key="${key}" value="${val}" placeholder="${_COOLDOWN_DEFAULTS[key] ?? ''} (${_humanTime(_COOLDOWN_DEFAULTS[key] ?? '')})"
               style="max-width:140px;" type="number" min="0">
      `;
      container.appendChild(row);
    }
    const saveRow = document.createElement('div');
    saveRow.style.cssText = 'margin-top:.8rem;display:flex;gap:.6rem;';
    saveRow.innerHTML = `
      <button class="btn btn-primary" onclick="saveCooldowns()">Save Cooldowns</button>
      <span id="cooldown-save-status" style="font-size:.8rem;color:rgba(48,209,88,.8);align-self:center;"></span>
    `;
    container.appendChild(saveRow);
  } catch (e) { console.warn('Failed to load cooldowns', e); }
  try {
    const data = await api('GET', '/admin/prompts');
    _promptsData = data.prompts || [];
    renderPromptList();
  } catch (e) { console.warn('Failed to load prompts', e); }
}

function renderPromptList() {
  const container = document.getElementById('prompt-list');
  container.innerHTML = '';
  const categories = {
    'Vision Prompts': _promptsData.filter(p => p.slug.startsWith('vision_')),
    'Behaviour Prompts': _promptsData.filter(p => !p.slug.startsWith('vision_')),
  };
  for (const [catName, prompts] of Object.entries(categories)) {
    if (!prompts.length) continue;
    const catLabel = document.createElement('div');
    catLabel.style.cssText = 'font-size:.82rem;font-weight:600;color:var(--text2);margin:1rem 0 .5rem;text-transform:uppercase;letter-spacing:.04em;';
    catLabel.textContent = catName;
    container.appendChild(catLabel);
    for (const p of prompts) {
      const card = document.createElement('div');
      card.style.cssText = 'background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem 1rem;margin-bottom:.5rem;cursor:pointer;transition:all .15s;';
      card.onmouseenter = () => card.style.borderColor = 'rgba(10,132,255,.4)';
      card.onmouseleave = () => card.style.borderColor = 'var(--border)';
      card.onclick = () => openPromptEditor(p.slug);
      const preview = (p.text || '').trim().substring(0, 120).replace(/\n/g, ' ');
      card.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <div>
            <div style="font-size:.9rem;font-weight:500;color:var(--text);">${p.label}</div>
            <div style="font-size:.75rem;color:var(--text3);margin-top:.15rem;">${p.description}</div>
          </div>
          <div style="display:flex;align-items:center;gap:.5rem;">
            <span style="font-size:.7rem;color:var(--text3);">${p.chars} chars</span>
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
          </div>
        </div>
        ${preview ? `<div style="font-size:.75rem;color:var(--text3);margin-top:.4rem;font-family:'SF Mono',Monaco,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${preview}…</div>` : ''}
      `;
      container.appendChild(card);
    }
  }
}

function openPromptEditor(slug) {
  const p = _promptsData.find(x => x.slug === slug);
  if (!p) return;
  _currentPromptSlug = slug;
  document.getElementById('prompt-editor-title').textContent = p.label;
  document.getElementById('prompt-editor-desc').textContent = p.description;
  const textarea = document.getElementById('prompt-editor-text');
  textarea.value = p.text || '';
  _updatePromptCharCount();
  textarea.oninput = _updatePromptCharCount;
  document.getElementById('prompt-editor-overlay').style.display = 'flex';
}

function closePromptEditor() {
  document.getElementById('prompt-editor-overlay').style.display = 'none';
  _currentPromptSlug = null;
}

function _updatePromptCharCount() {
  const text = document.getElementById('prompt-editor-text').value;
  document.getElementById('prompt-editor-chars').textContent = text.length + ' characters';
}

async function saveCurrentPrompt() {
  if (!_currentPromptSlug) return;
  const text = document.getElementById('prompt-editor-text').value;
  try {
    await api('POST', `/admin/prompts/${_currentPromptSlug}`, { text });
    toast('Prompt saved');
    const p = _promptsData.find(x => x.slug === _currentPromptSlug);
    if (p) { p.text = text; p.chars = text.length; }
    renderPromptList();
    closePromptEditor();
  } catch (e) { toast('Save failed: ' + e.message); }
}

async function saveCooldowns() {
  const inputs = document.querySelectorAll('#cooldown-fields .config-input');
  const values = {};
  try {
    const cfg = await api('GET', '/admin/config');
    Object.assign(values, cfg.values || {});
  } catch (e) {}
  for (const input of inputs) {
    const key = input.dataset.key;
    const val = input.value.trim();
    if (val) values[key] = val;
  }
  try {
    await api('POST', '/admin/config', { values });
    document.getElementById('cooldown-save-status').textContent = 'Saved — restart to apply';
    setTimeout(() => { const el = document.getElementById('cooldown-save-status'); if (el) el.textContent = ''; }, 4000);
    toast('Cooldowns saved — restart server to apply');
  } catch (e) { toast('Save failed: ' + e.message); }
}

// ── Find Anything — UniFi Protect-style ──────────────────────────────────────

let _faClips = [];
let _faActiveClipId = null;
let _faTimeRange = '1w';
let _faInitialized = false;
let _faLoading = false;
let _faHasMore = true;
let _faPage = 0;
const _FA_PAGE_SIZE = 50;
let _faSearchTimer = null;
let _faBulkMode = false;
let _faBulkSelected = new Set();

const _FA_CAMERA_LABELS = {};
// Labels auto-derived from entity IDs — override in home_runtime.json if needed.

function _faCameraLabel(id) {
  return _FA_CAMERA_LABELS[id] || id.replace('camera.', '').replace(/_/g, ' ').replace(/fluent|mainstream|profile000/gi, '').trim() || id;
}

function _faFormatTs(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  const mon = d.toLocaleString('en-GB', { month: 'short' });
  const day = String(d.getDate()).padStart(2, '0');
  const h = String(d.getHours()).padStart(2, '0');
  const m = String(d.getMinutes()).padStart(2, '0');
  return `${mon} ${day}, ${h}:${m}`;
}

function _faEventIcon(clip) {
  const desc = ((clip.description || '') + ' ' + (clip.location || '')).toLowerCase();
  const extra = clip.extra || {};
  const coralDets = (Array.isArray(extra.coral_detections) ? extra.coral_detections : []).join(' ').toLowerCase();
  // Delivery / package — box with arrow
  if (extra.delivery || desc.includes('delivery') || desc.includes('package') || desc.includes('parcel'))
    return '<svg viewBox="0 0 24 24"><path d="M12 3l9 4.5v9L12 21l-9-4.5v-9L12 3z"/><path d="M12 12l9-4.5"/><path d="M12 12v9"/><path d="M12 12L3 7.5"/><path d="M16.5 5.25L7.5 10"/></svg>';
  // Doorbell — bell
  if (desc.includes('doorbell') || desc.includes('door bell'))
    return '<svg viewBox="0 0 24 24"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>';
  // Person
  if (coralDets.includes('person') || desc.includes('person') || desc.includes('someone') || desc.includes('walking'))
    return '<svg viewBox="0 0 24 24"><circle cx="12" cy="5" r="3"/><path d="M12 8c-3.5 0-6 2.5-6 6v4h12v-4c0-3.5-2.5-6-6-6z"/></svg>';
  // Vehicle
  if (coralDets.includes('car') || coralDets.includes('truck') || coralDets.includes('bus') || desc.includes('vehicle') || desc.includes('car') || desc.includes('van'))
    return '<svg viewBox="0 0 24 24"><path d="M5 17h14v-5l-2-5H7L5 12v5z"/><circle cx="7.5" cy="17" r="1.5"/><circle cx="16.5" cy="17" r="1.5"/><path d="M5 12h14"/></svg>';
  // Animal
  if (coralDets.includes('dog') || coralDets.includes('cat') || coralDets.includes('bird'))
    return '<svg viewBox="0 0 24 24"><path d="M10 5.172C10 3.782 8.423 2.679 6.5 3c-2.823.47-4.113 6.006-4 7 .137 1.217 1.5 2 2.5 2s2-.5 3-1.5c.52-.52 1-1.328 1-2.328z"/><path d="M14.267 5.172c0-1.39 1.577-2.493 3.5-2.172 2.823.47 4.113 6.006 4 7-.137 1.217-1.5 2-2.5 2s-2-.5-3-1.5c-.52-.52-1-1.328-1-2.328z"/><path d="M8 14v.5"/><path d="M16 14v.5"/><path d="M11.25 16.25h1.5L12 17l-.75-.75z"/><path d="M4.42 11.247A13.152 13.152 0 0 0 4 14.556C4 18.728 7.582 21 12 21s8-2.272 8-6.444a11.702 11.702 0 0 0-.493-3.309"/></svg>';
  // Motion (generic) — no icon
  return '';
}

async function faInit() {
  if (!_faInitialized) {
    _faInitialized = true;
    faLoadStats();
    // Infinite scroll — load more when near bottom
    const main = document.getElementById('fa-main');
    if (main) {
      main.addEventListener('scroll', () => {
        if (_faLoading || !_faHasMore) return;
        if (main.scrollTop + main.clientHeight >= main.scrollHeight - 200) {
          faLoadMore();
        }
      });
    }
  }
  _faPage = 0;
  _faHasMore = true;
  _faClips = [];
  await faLoadClips();
}

async function faLoadStats() {
  try {
    const s = await api('GET', '/admin/motion-clips/stats');
    // Populate camera labels from server config
    if (s.camera_labels) {
      Object.assign(_MOTION_CAMERA_LABELS, s.camera_labels);
      Object.assign(_FA_CAMERA_LABELS, s.camera_labels);
    }
    const el = (id) => document.getElementById(id);
    if (el('fa-stat-total')) el('fa-stat-total').textContent = s.total_clips || 0;
    if (el('fa-stat-flagged')) el('fa-stat-flagged').textContent = s.flagged_clips || 0;
    if (el('fa-stat-disk')) el('fa-stat-disk').textContent = (s.disk_usage_mb || 0) + ' MB';
  } catch (e) {}
}

function _faDateFilter() {
  const dateInput = document.getElementById('fa-date-input');
  if (dateInput && dateInput.value) return dateInput.value;
  const now = new Date();
  if (_faTimeRange === '1h') { now.setHours(now.getHours() - 1); return now.toISOString().slice(0, 10); }
  if (_faTimeRange === '1d') { return now.toISOString().slice(0, 10); }
  if (_faTimeRange === '1w') { now.setDate(now.getDate() - 7); return now.toISOString().slice(0, 10); }
  if (_faTimeRange === '1m') { now.setMonth(now.getMonth() - 1); return now.toISOString().slice(0, 10); }
  return '';
}

async function faLoadClips() {
  const grid = document.getElementById('fa-grid');
  if (!grid) return;
  _faLoading = true;
  grid.innerHTML = Array.from({length: 12}, () => '<div class="fa-card-skeleton"></div>').join('');

  const query = (document.getElementById('fa-search-input') || {}).value || '';
  const flagged = (document.getElementById('fa-flagged-only') || {}).checked;
  const params = new URLSearchParams();
  if (query) params.set('query', query);
  if (flagged) params.set('flagged_only', '1');
  params.set('limit', String(_FA_PAGE_SIZE));

  // Camera filter
  const camChecks = document.querySelectorAll('#fa-camera-list input[type=checkbox]:checked');
  let selectedCam = '';
  camChecks.forEach(cb => { if (cb.value) selectedCam = cb.value; });
  if (selectedCam) params.set('camera', selectedCam);

  // Date range filter
  const dateFrom = document.getElementById('fa-date-from')?.value || _faDateFilter();
  const dateTo = document.getElementById('fa-date-to')?.value || '';
  if (dateFrom) params.set('since', dateFrom);
  if (dateTo) params.set('before', dateTo + 'T23:59:59');

  try {
    let clips;
    if (query) {
      const body = { query, camera_entity_id: selectedCam || undefined };
      const res = await api('POST', '/admin/motion-clips/search', body);
      clips = (res.clips || []).map(c => ({ ...c, ..._faSerialize(c) }));
      _faHasMore = false; // search returns all results
    } else {
      const res = await api('GET', '/admin/motion-clips?' + params.toString());
      clips = (res.clips || []).map(c => ({ ...c, ..._faSerialize(c) }));
      _faHasMore = clips.length >= _FA_PAGE_SIZE;
    }
    _faClips = clips;
    _faPage = 1;
    faRenderGrid(clips);
    faPopulateCameras(clips);
    // Prefetch thumbnails for instant display on filter changes
    _faPrefetchThumbs(clips);
  } catch (e) {
    grid.innerHTML = '<div class="fa-empty"><div class="fa-empty-icon">📹</div><div class="fa-empty-title">Failed to load clips</div><div class="fa-empty-desc">' + _escapeHtml(e.message || '') + '</div></div>';
  } finally {
    _faLoading = false;
  }
}

async function faLoadMore() {
  if (_faLoading || !_faHasMore) return;
  _faLoading = true;
  const params = new URLSearchParams();
  params.set('limit', String(_FA_PAGE_SIZE));
  params.set('offset', String(_faPage * _FA_PAGE_SIZE));
  const flagged = (document.getElementById('fa-flagged-only') || {}).checked;
  if (flagged) params.set('flagged_only', '1');
  const camChecks = document.querySelectorAll('#fa-camera-list input[type=checkbox]:checked');
  let selectedCam = '';
  camChecks.forEach(cb => { if (cb.value) selectedCam = cb.value; });
  if (selectedCam) params.set('camera', selectedCam);
  try {
    const res = await api('GET', '/admin/motion-clips?' + params.toString());
    const newClips = (res.clips || []).map(c => ({ ...c, ..._faSerialize(c) }));
    if (newClips.length < _FA_PAGE_SIZE) _faHasMore = false;
    _faClips = _faClips.concat(newClips);
    _faPage++;
    faRenderGrid(_faClips);
  } catch (e) {}
  _faLoading = false;
}

const _faPrefetchedUrls = new Set();
function _faPrefetchThumbs(clips) {
  for (const c of clips) {
    if (c.thumb_url && !_faPrefetchedUrls.has(c.thumb_url)) {
      _faPrefetchedUrls.add(c.thumb_url);
      const img = new Image();
      img.src = c.thumb_url;
    }
  }
}

function _faSerialize(c) {
  return {
    thumb_url: c.thumb_url || '',
    video_url: c.video_url || '',
    flagged: !!c.flagged,
  };
}

function faRenderGrid(clips) {
  const grid = document.getElementById('fa-grid');
  if (!grid) return;

  // Apply client-side event type filter
  const eventTypes = _faGetEventTypeFilters();
  let filtered = eventTypes.length ? clips.filter(c => _faMatchesEventType(c, eventTypes)) : [...clips];

  // Apply client-side camera filter
  const camChecks = document.querySelectorAll('#fa-camera-list input[type=checkbox]:checked');
  let selectedCam = '';
  camChecks.forEach(cb => { if (cb.value) selectedCam = cb.value; });
  if (selectedCam) filtered = filtered.filter(c => c.camera_entity_id === selectedCam);

  // Apply flagged filter
  if (document.getElementById('fa-flagged-only')?.checked) filtered = filtered.filter(c => c.flagged);

  if (!filtered.length) {
    grid.innerHTML = '<div class="fa-empty"><div class="fa-empty-icon">📹</div><div class="fa-empty-title">No clips found</div><div class="fa-empty-desc">Try adjusting your filters or time range</div></div>';
    return;
  }

  // Group clips if group-by is active
  const groups = _faGroupClips(filtered);
  let html = '';
  for (const group of groups) {
    if (group.label) {
      html += `<div style="grid-column:1/-1;padding:8px 4px 4px;font-size:12px;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--border);margin-top:8px;">${_esc(group.label)} <span style="font-weight:400;color:var(--text3);">(${group.clips.length})</span></div>`;
    }
    html += group.clips.map(clip => _faRenderCard(clip)).join('');
  }
  grid.innerHTML = html;
  // Click delegation for clip cards
  grid.onclick = (e) => {
    if (e.target.closest('.fa-card-bulk') || e.target.closest('.fa-card-flag')) return;
    const card = e.target.closest('.fa-card[data-id]');
    if (card) faExpandClip(parseInt(card.dataset.id));
  };
  // Hover video preview — play on mouseenter, pause on mouseleave
  grid.querySelectorAll('.fa-card-media').forEach(el => {
    const vid = el.querySelector('.fa-hover-video');
    if (!vid || !vid.src) return;
    el.addEventListener('mouseenter', () => { vid.currentTime = 0; vid.play().catch(()=>{}); });
    el.addEventListener('mouseleave', () => { vid.pause(); });
  });

  // Render camera timeline
  _faRenderTimeline(clips);
}

function _faRenderTimeline(clips) {
  const el = document.getElementById('fa-timeline');
  if (!el || !clips.length) { if (el) el.style.display = 'none'; return; }

  // Group by camera
  const byCam = {};
  for (const c of clips) {
    const cam = c.camera_entity_id || 'unknown';
    if (!byCam[cam]) byCam[cam] = [];
    byCam[cam].push(c);
  }

  // Find time range
  const times = clips.map(c => new Date(c.ts).getTime()).filter(t => !isNaN(t));
  if (!times.length) { el.style.display = 'none'; return; }
  const minT = Math.min(...times);
  const maxT = Math.max(...times);
  const range = maxT - minT || 1;

  let html = '';
  for (const [cam, events] of Object.entries(byCam)) {
    const label = _faCameraLabel(cam);
    const dots = events.map(c => {
      const t = new Date(c.ts).getTime();
      const pct = ((t - minT) / range * 100).toFixed(1);
      const hasTag = (c.description || '').toLowerCase();
      const color = hasTag.includes('person') ? '#34d399' : hasTag.includes('delivery') ? '#f59e0b' : '#60a5fa';
      return `<div onclick="faExpandClip(${c.id})" style="position:absolute;left:${pct}%;top:50%;transform:translate(-50%,-50%);width:8px;height:8px;border-radius:50%;background:${color};cursor:pointer;border:1px solid rgba(255,255,255,.3);" title="${_esc(c.description || '').slice(0,60)}"></div>`;
    }).join('');
    html += `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;">
      <span style="font-size:10px;color:var(--text3);min-width:100px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_esc(label)}</span>
      <div style="flex:1;height:16px;background:var(--surface2);border-radius:8px;position:relative;overflow:visible;">${dots}</div>
    </div>`;
  }

  // Time axis
  const startLabel = new Date(minT).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  const endLabel = new Date(maxT).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  html += `<div style="display:flex;justify-content:space-between;padding:2px 108px 0;font-size:9px;color:var(--text3);">
    <span>${startLabel}</span><span>${endLabel}</span>
  </div>`;

  el.innerHTML = html;
  el.style.display = 'block';
}

function _faParseCoralScore(detections) {
  // Extract the highest confidence % from coral detections like ["person(85%)", "car(72%)"]
  const deduped = _faDedupCoral(detections);
  if (!deduped.length) return null;
  let best = 0;
  let bestLabel = '';
  for (const d of deduped) {
    const m = d.match(/^(\w+)\((\d+)%\)$/);
    if (m) {
      const pct = parseInt(m[2], 10);
      if (pct > best) { best = pct; bestLabel = m[1]; }
    }
  }
  return best > 0 ? { label: bestLabel, score: best } : null;
}

function _faDedupCoral(detections) {
  // Keep only the highest confidence per label — handles legacy clips with dupes
  if (!Array.isArray(detections) || !detections.length) return [];
  const best = {};
  for (const d of detections) {
    const m = d.match(/^(\w+)\((\d+)%\)$/);
    if (m) {
      const label = m[1];
      const pct = parseInt(m[2], 10);
      if (!best[label] || pct > best[label]) best[label] = pct;
    } else {
      best[d] = best[d] || 0; // non-standard format, keep as-is
    }
  }
  return Object.entries(best)
    .sort((a, b) => b[1] - a[1])
    .map(([label, pct]) => pct > 0 ? `${label}(${pct}%)` : label);
}

function _faCoralBadgeColor(score) {
  if (score >= 80) return 'rgba(0,184,148,.85)';   // green — high confidence
  if (score >= 50) return 'rgba(230,165,0,.85)';    // amber — medium
  return 'rgba(255,107,107,.85)';                    // red — low
}

function _faAutoTags(clip) {
  const tags = [];
  const desc = (clip.description || '').toLowerCase();
  const extra = clip.extra || {};
  if (extra.delivery) tags.push({icon:'📦', label:extra.delivery_company || 'Delivery', color:'#f59e0b', bg:'rgba(245,158,11,.12)'});
  if (extra.plate_number) tags.push({icon:'🔢', label:'Plate', color:'#818cf8', bg:'rgba(129,140,248,.12)'});
  if (desc.includes('person') || desc.includes('someone') || desc.includes('walking') || desc.includes('approaching')) tags.push({icon:'', label:'Person', color:'#34d399', bg:'rgba(52,211,153,.12)', svg:'person'});
  if (desc.includes('vehicle') || desc.includes('car') || desc.includes('van') || desc.includes('truck') || desc.includes('hatchback') || desc.includes('suv')) tags.push({icon:'', label:'Vehicle', color:'#60a5fa', bg:'rgba(96,165,250,.12)', svg:'car'});
  if (desc.includes('parcel') || desc.includes('package')) tags.push({icon:'📦', label:'Parcel', color:'#f59e0b', bg:'rgba(245,158,11,.12)'});
  return tags;
}

function _faTagHtml(tags) {
  if (!tags.length) return '';
  const svgs = {
    person: '<svg viewBox="0 0 24 24" style="width:10px;height:10px;fill:currentColor;"><circle cx="12" cy="7" r="4"/><path d="M5.5 21c0-4.4 2.9-8 6.5-8s6.5 3.6 6.5 8"/></svg>',
    car: '<svg viewBox="0 0 24 24" style="width:11px;height:11px;fill:currentColor;"><path d="M5 11l1.5-4.5A2 2 0 018.4 5h7.2a2 2 0 011.9 1.5L19 11M5 11h14M5 11v6a1 1 0 001 1h1a1 1 0 001-1v-1h8v1a1 1 0 001 1h1a1 1 0 001-1v-6M7.5 15a1 1 0 100-2 1 1 0 000 2zM16.5 15a1 1 0 100-2 1 1 0 000 2z"/></svg>',
  };
  return tags.map(t => {
    const icon = t.svg ? svgs[t.svg] || '' : t.icon;
    return `<span style="display:inline-flex;align-items:center;gap:3px;font-size:9px;font-weight:600;letter-spacing:.3px;padding:2px 7px;border-radius:99px;color:${t.color};background:${t.bg};backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);border:1px solid ${t.color}22;">${icon}${t.label.toUpperCase()}</span>`;
  }).join('');
}

function _faRenderCard(clip) {
  const icon = _faEventIcon(clip);
  const cam = _faCameraLabel(clip.camera_entity_id);
  const time = _faFormatTs(clip.ts);
  const thumb = clip.thumb_url
    ? `<img src="${clip.thumb_url}" loading="lazy" alt=""><video class="fa-hover-video" src="${clip.video_url || ''}" muted loop preload="none"></video>`
    : `<div class="fa-card-placeholder">📷</div>`;
  const flagClass = clip.flagged ? ' flagged' : '';
  const bulkCheck = _faBulkMode
    ? `<input type="checkbox" class="fa-card-bulk" ${_faBulkSelected.has(clip.id) ? 'checked' : ''} onclick="event.stopPropagation();faBulkToggle(${clip.id},this.checked)" style="position:absolute;top:6px;left:6px;z-index:2;width:18px;height:18px;accent-color:var(--accent);">`
    : '';
  const extra = clip.extra || {};
  const coralTop = _faParseCoralScore(extra.coral_detections);
  const badgeHtml = coralTop
    ? `<span class="fa-card-badge" style="background:${_faCoralBadgeColor(coralTop.score)}" title="${_faDedupCoral(extra.coral_detections).join(', ')}">${coralTop.score}%</span>`
    : '';
  const plateNumber = String(extra.plate_number || '');
  const plateHtml = plateNumber
    ? `<span class="fa-card-badge" style="bottom:42px;background:rgba(255,255,255,.08);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid rgba(255,100,100,.2);display:inline-flex;align-items:center;gap:3px;" title="Number plate"><svg viewBox="0 0 24 24" style="width:10px;height:10px;stroke:rgba(255,255,255,.7);stroke-width:1.8;fill:none;"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 12h12"/></svg>${_esc(plateNumber)}</span>`
    : '';
  return `<div class="fa-card${_faActiveClipId === clip.id ? ' selected' : ''}${_faBulkSelected.has(clip.id) ? ' selected' : ''}" data-id="${clip.id}">
    <div class="fa-card-media">
      ${bulkCheck}
      ${thumb}
      <div class="fa-card-overlay">
        <span class="fa-card-camera">${_esc(cam)}</span>
        <span class="fa-card-time">${_esc(time)}</span>
      </div>
      ${!_faBulkMode && icon ? `<span class="fa-card-type">${icon}</span>` : ''}
      ${badgeHtml}
      ${plateHtml}
      <span class="fa-card-flag${flagClass}" onclick="event.stopPropagation();faToggleFlagCard(${clip.id})">${clip.flagged ? '★' : '☆'}</span>
      ${clip.duration_s ? `<span style="position:absolute;bottom:4px;left:4px;background:rgba(0,0,0,.7);color:#fff;font-size:9px;padding:1px 5px;border-radius:3px;">${clip.duration_s}s</span>` : ''}
    </div>
    ${clip.description && clip.description !== 'Motion detected' ? `<div style="padding:4px 8px 6px;font-size:11px;color:var(--text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;" title="${_esc(clip.description)}">${_esc(clip.description.slice(0,80))}</div>` : ''}
    ${_faAutoTags(clip).length ? `<div style="padding:0 8px 6px;display:flex;gap:3px;flex-wrap:wrap;">${_faTagHtml(_faAutoTags(clip))}</div>` : ''}
  </div>`;
}

function faPopulateCameras(clips) {
  const list = document.getElementById('fa-camera-list');
  if (!list) return;
  const cameras = [...new Set(clips.map(c => c.camera_entity_id).filter(Boolean))];
  const current = list.querySelector('input:checked')?.value || '';
  list.innerHTML = '<label class="fa-check-item"><input type="checkbox" ' + (!current ? 'checked' : '') + ' value="" onchange="faSelectCamera(this)"> All Cameras</label>';
  for (const cam of cameras) {
    const label = _faCameraLabel(cam);
    const checked = current === cam ? ' checked' : '';
    list.innerHTML += `<label class="fa-check-item"><input type="checkbox"${checked} value="${_esc(cam)}" onchange="faSelectCamera(this)"> ${_esc(label)}</label>`;
  }
}

function faSelectCamera(el) {
  // Radio-style: uncheck others
  document.querySelectorAll('#fa-camera-list input').forEach(cb => { if (cb !== el) cb.checked = false; });
  if (!el.checked) { el.checked = true; } // at least one must be checked
  if (_faClips.length) faRenderGrid(_faClips);
  else faLoadClips();
}

function faSetTimeRange(range, btn) {
  _faTimeRange = range;
  document.querySelectorAll('.fa-time-btns .fa-time-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.getElementById('fa-date-input').value = '';
  faLoadClips();
}

function faSearch() { faLoadClips(); }
function faApplyFilters(serverRefresh) {
  // Event type and camera filters work client-side on cached clips
  // Date range changes need a server re-fetch
  if (serverRefresh) {
    clearTimeout(_faSearchTimer);
    _faSearchTimer = setTimeout(() => faLoadClips(), 300);
  } else if (_faClips.length) {
    faRenderGrid(_faClips);
  } else {
    clearTimeout(_faSearchTimer);
    _faSearchTimer = setTimeout(() => faLoadClips(), 300);
  }
}
function faSearchDebounced() {
  clearTimeout(_faSearchTimer);
  _faSearchTimer = setTimeout(() => faLoadClips(), 300);
}

function _faGetEventTypeFilters() {
  const checks = document.querySelectorAll('#fa-filter-events input[type=checkbox]:checked');
  return [...checks].map(cb => cb.value).filter(Boolean);
}

function _faMatchesEventType(clip, types) {
  if (!types.length) return true;
  const desc = ((clip.description || '') + ' ' + JSON.stringify(clip.extra || {})).toLowerCase();
  return types.some(t => {
    if (t === 'person') return /person|someone|walking|man|woman/.test(desc);
    if (t === 'vehicle') return /vehicle|car|van|truck|bus/.test(desc);
    if (t === 'delivery') return /delivery|parcel|dhl|royal mail|amazon/.test(desc) || (clip.extra || {}).delivery;
    if (t === 'package') return /package|parcel|box/.test(desc);
    if (t === 'doorbell') return /doorbell|door bell|visitor|rang/.test(desc);
    return false;
  });
}

function faSetView(view) {
  const gridEl = document.getElementById('fa-grid');
  const playerEl = document.getElementById('fa-player');
  const historyEl = document.getElementById('fa-history-pane');
  const gridBtn = document.getElementById('fa-view-grid');
  const histBtn = document.getElementById('fa-view-history');
  if (view === 'history') {
    if (gridEl) gridEl.style.display = 'none';
    if (playerEl) playerEl.style.display = 'none';
    if (historyEl) historyEl.style.display = '';
    if (gridBtn) gridBtn.classList.remove('active');
    if (histBtn) histBtn.classList.add('active');
    if (typeof loadEventHistory === 'function') loadEventHistory();
  } else {
    if (gridEl) gridEl.style.display = '';
    if (historyEl) historyEl.style.display = 'none';
    if (gridBtn) gridBtn.classList.add('active');
    if (histBtn) histBtn.classList.remove('active');
  }
}

// ── Inline player ──

function faExpandClip(clipId) {
  const clip = _faClips.find(c => c.id === clipId);
  if (!clip) return;
  _faActiveClipId = clipId;

  const player = document.getElementById('fa-player');
  const video = document.getElementById('fa-player-video');
  const camEl = document.getElementById('fa-player-camera');
  const timeEl = document.getElementById('fa-player-time');
  const descEl = document.getElementById('fa-player-desc');
  const flagBtn = document.getElementById('fa-player-flag-btn');

  if (camEl) camEl.textContent = _faCameraLabel(clip.camera_entity_id);
  if (timeEl) timeEl.textContent = _faFormatTs(clip.ts);
  if (descEl) {
    let descHtml = _esc(clip.description || '');
    // Append Coral TPU detection chips below description
    const extra = clip.extra || {};
    const coralDets = _faDedupCoral(extra.coral_detections);
    const plateNum = String(extra.plate_number || '');
    if (coralDets.length || plateNum) {
      descHtml += '<div class="fa-detect-row">';
      for (const d of coralDets) {
        const parsed = d.match(/^(\w+)\((\d+)%\)$/);
        if (parsed) {
          const score = parseInt(parsed[2], 10);
          const dotColor = _faCoralBadgeColor(score);
          descHtml += `<span class="fa-detect-chip" title="Coral TPU · ${parsed[2]}% confidence"><span class="fa-detect-dot" style="background:${dotColor}"></span><span class="fa-detect-label">${_esc(parsed[1])}</span><span class="fa-detect-score">${parsed[2]}%</span></span>`;
        } else {
          descHtml += `<span class="fa-detect-chip" title="Coral TPU detection"><span class="fa-detect-dot" style="background:#f0a030"></span>${_esc(d)}</span>`;
        }
      }
      if (plateNum) {
        descHtml += `<span class="fa-detect-chip plate" title="Number plate"><svg viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 12h12"/><circle cx="7" cy="9" r=".5" fill="currentColor"/><circle cx="17" cy="9" r=".5" fill="currentColor"/></svg><span>${_esc(plateNum)}</span></span>`;
      } else if (extra.coral_has_plate) {
        descHtml += `<span class="fa-detect-chip plate" title="Plate-bearing vehicle"><svg viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 12h12"/></svg><span>plate detected</span></span>`;
      }
      descHtml += '</div>';
    }
    descEl.innerHTML = descHtml;
  }
  if (flagBtn) flagBtn.textContent = clip.flagged ? '★' : '⚑';
  if (video && clip.video_url) {
    video.src = clip.video_url;
    video.load();
  }
  if (player) player.classList.add('show');

  // Highlight selected card
  document.querySelectorAll('.fa-card').forEach(c => c.classList.remove('selected'));
  const card = document.querySelector(`.fa-card[data-id="${clipId}"]`);
  if (card) card.classList.add('selected');

  // Load related clips
  faLoadRelated(clip);

  // Scroll player into view
  if (player) player.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function faCollapsePlayer() {
  _faActiveClipId = null;
  const player = document.getElementById('fa-player');
  const video = document.getElementById('fa-player-video');
  if (video) { video.pause(); video.src = ''; }
  if (player) player.classList.remove('show');
  document.querySelectorAll('.fa-card').forEach(c => c.classList.remove('selected'));
}

async function faLoadRelated(clip) {
  const container = document.getElementById('fa-player-related');
  if (!container) return;
  container.innerHTML = '';
  // Show clips from same camera
  const related = _faClips.filter(c => c.camera_entity_id === clip.camera_entity_id && c.id !== clip.id).slice(0, 10);
  for (const r of related) {
    const img = document.createElement('img');
    img.className = 'fa-player-related-thumb' + (r.id === _faActiveClipId ? ' active' : '');
    img.src = r.thumb_url || '';
    img.alt = _faFormatTs(r.ts);
    img.title = _faCameraLabel(r.camera_entity_id) + ' — ' + _faFormatTs(r.ts);
    img.onclick = () => faExpandClip(r.id);
    if (!r.thumb_url) { img.style.background = '#222'; img.style.minWidth = '80px'; }
    container.appendChild(img);
  }
}

async function faToggleFlag() {
  if (!_faActiveClipId) return;
  try {
    const res = await api('POST', `/admin/motion-clips/${_faActiveClipId}/flag`);
    const clip = _faClips.find(c => c.id === _faActiveClipId);
    if (clip) clip.flagged = res.flagged;
    const btn = document.getElementById('fa-player-flag-btn');
    if (btn) btn.textContent = res.flagged ? '★' : '⚑';
    faRenderGrid(_faClips);
    faLoadStats();
  } catch (e) { toast('Flag failed: ' + e.message); }
}

async function faToggleFlagCard(clipId) {
  try {
    const res = await api('POST', `/admin/motion-clips/${clipId}/flag`);
    const clip = _faClips.find(c => c.id === clipId);
    if (clip) clip.flagged = res.flagged;
    faRenderGrid(_faClips);
    faLoadStats();
  } catch (e) { toast('Flag failed: ' + e.message); }
}

function faDownloadClip() {
  if (!_faActiveClipId) return;
  window.open(`/admin/motion-clips/${_faActiveClipId}/video?download=1`, '_blank');
}

async function faDeleteClip() {
  if (!_faActiveClipId) return;
  if (!confirm('Delete this clip?')) return;
  try {
    await api('DELETE', `/admin/motion-clips/${_faActiveClipId}`);
    faCollapsePlayer();
    _faClips = _faClips.filter(c => c.id !== _faActiveClipId);
    faRenderGrid(_faClips);
    faLoadStats();
    toast('Clip deleted');
  } catch (e) { toast('Delete failed: ' + e.message); }
}


// ── Self-Heal ────────────────────────────────────────────────────────────────

const _SH_API = () => '/admin/selfheal';

const _SH_TYPE_LABELS = {
  error_detected:  '🔴 Error detected',
  deduplicated:    '⏭ Deduplicated',
  no_source_file:  '⚠️ No source file',
  claude_failed:   '💔 Claude failed',
  fix_proposed:    '🔧 Fix proposed',
  analysis_only:   '🔍 Analysis only',
  approved:        '✅ Approved',
  rejected:        '❌ Rejected',
  auto_rejected:   '⏱ Auto-rejected',
  apply_ok:        '✅ Patch applied',
  apply_failed:    '💥 Apply failed',
  restart_ok:      '🔄 Restarted',
  restart_failed:  '💥 Restart failed',
};

async function _shFetch(path, opts={}) {
  const r = await fetch(_SH_API() + path, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function _shFmtUptime(s) {
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}

async function shClearAllEvents() {
  if (!confirm("Delete ALL self-heal events? This cannot be undone.")) return;
  try {
    await _shFetch("/clear-events", { method: "POST" });
    toast("All events cleared");
    loadSelfHeal();
  } catch(e) { toast("Failed: " + e.message); }
}

async function loadSelfHeal() {
  // Status
  try {
    const st = await _shFetch('/status');
    document.getElementById('sh-stat-status').innerHTML =
      '<span style="color:var(--green,#34c759)">&#9679; Running</span>';
    document.getElementById('sh-stat-uptime').textContent = _shFmtUptime(st.uptime_seconds);
    document.getElementById('sh-stat-errors').textContent = st.errors_detected ?? '—';
    document.getElementById('sh-stat-applied').textContent = st.patches_applied ?? '—';
    document.getElementById('sh-stat-rejected').textContent = st.fixes_rejected ?? '—';
    document.getElementById('sh-stat-pending').textContent = st.pending_count ?? '—';
  } catch(e) {
    document.getElementById('sh-stat-status').innerHTML =
      '<span style="color:var(--red,#ff3b30)">&#9679; Offline</span>';
    ['sh-stat-uptime','sh-stat-errors','sh-stat-applied','sh-stat-rejected','sh-stat-pending']
      .forEach(function(id){ document.getElementById(id).textContent = '—'; });
  }

  // Pending fixes
  try {
    const pending = await _shFetch('/pending');
    const list = document.getElementById('sh-pending-list');
    const badge = document.getElementById('sh-pending-badge');
    if (badge) badge.textContent = pending.length ? `(${pending.length})` : '(0)';
    if (!pending.length) {
      list.innerHTML = '<div class="text-sm text-muted">No pending fixes.</div>';
    } else {
      list.innerHTML = pending.map(function(f) {
        const diffHtml = f.has_diff
          ? '<pre style="background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:10px;font-size:11px;overflow-x:auto;max-height:200px;white-space:pre;">' + _shEsc(f.diff) + '</pre>'
          : '';
        return '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:12px;">'
          + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
          + '<div style="font-weight:600;font-size:13px;">🔴 ' + _shEsc(f.event) + ' <span style="color:var(--text3);font-weight:400;">— ' + _shEsc(f.exc_type) + '</span></div>'
          + '<div style="font-size:11px;color:var(--text3);">' + Math.floor(f.age_seconds/60) + 'm ago</div>'
          + '</div>'
          + '<div style="font-size:12px;color:var(--text2);margin-bottom:4px;">📄 ' + _shEsc(f.source_file) + '</div>'
          + '<div style="font-size:12px;margin-bottom:10px;">' + _shEsc(f.summary) + '</div>'
          + diffHtml
          + '<div style="display:flex;gap:8px;margin-top:10px;">'
          + '<button class="btn btn-primary" style="font-size:12px;" onclick="shApprove(\'' + f.fix_id + '\')">✅ Approve</button>'
          + '<button class="btn btn-outline" style="font-size:12px;" onclick="shReject(\'' + f.fix_id + '\')">❌ Reject</button>'
          + '</div></div>';
      }).join('');
    }
  } catch(e) {
    document.getElementById('sh-pending-list').innerHTML = '<div class="text-sm text-muted">Could not load pending fixes.</div>';
  }

  // Events
  try {
    const events = await _shFetch('/events?limit=100');
    const tbody = document.getElementById('sh-events-tbody');
    if (!events.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-muted">No events yet.</td></tr>';
    } else {
      tbody.innerHTML = events.map(function(e) {
        const label = _SH_TYPE_LABELS[e.event_type] || e.event_type;
        const time = e.ts_iso ? new Date(e.ts_iso).toLocaleString() : '—';
        const detail = [e.summary, e.message].filter(Boolean).join(' ').slice(0, 120);
        return '<tr>'
          + '<td style="white-space:nowrap;color:var(--text3)">' + time + '</td>'
          + '<td>' + label + '</td>'
          + '<td style="font-size:11px;color:var(--text2)">' + _shEsc(e.log_event || e.service || '') + '</td>'
          + '<td style="font-size:11px;color:var(--text2)">' + _shEsc(detail) + '</td>'
          + '</tr>';
      }).join('');
    }
  } catch(e) {
    document.getElementById('sh-events-tbody').innerHTML =
      '<tr><td colspan="4" class="text-muted">Could not load events.</td></tr>';
  }

  // Config — populate form fields
  try {
    const cfg = await _shFetch('/config');
    const at = document.getElementById('sh-cfg-approval-timeout');
    const dw = document.getElementById('sh-cfg-dedup-window');
    const ct = document.getElementById('sh-cfg-claude-timeout');
    const ll = document.getElementById('sh-cfg-log-level');
    if (at && cfg.approval_timeout_seconds) at.value = cfg.approval_timeout_seconds;
    if (dw && cfg.dedup_window_seconds) dw.value = cfg.dedup_window_seconds;
    if (ct && cfg.claude_timeout_seconds) ct.value = cfg.claude_timeout_seconds;
    if (ll && cfg.log_level) ll.value = cfg.log_level;
    const om = document.getElementById('sh-cfg-openai-model');
    if (om && cfg.openai_model) om.value = cfg.openai_model;
  } catch(e) {
    // non-fatal — form just stays empty
  }
}



async function shTestInject() {
  try {
    const r = await fetch('/admin/selfheal-test', {method:'POST'});
    const d = await r.json();
    toast(d.message || 'Test error injected.');
    setTimeout(loadSelfHeal, 8000);
  } catch(e) { toast('Test failed: ' + e.message, 'err'); }
}

async function shSaveConfig() {
  const apiKey = document.getElementById('sh-cfg-api-key').value.trim();
  const openaiKey = document.getElementById('sh-cfg-openai-key').value.trim();
  const openaiModel = document.getElementById('sh-cfg-openai-model').value;
  const approvalTimeout = document.getElementById('sh-cfg-approval-timeout').value;
  const dedupWindow = document.getElementById('sh-cfg-dedup-window').value;
  const claudeTimeout = document.getElementById('sh-cfg-claude-timeout').value;
  const logLevel = document.getElementById('sh-cfg-log-level').value;

  const payload = {};
  if (apiKey) payload['anthropic_api_key'] = apiKey;
  if (openaiKey) payload['openai_api_key'] = openaiKey;
  if (openaiModel) payload['openai_model'] = openaiModel;
  if (approvalTimeout) payload['approval_timeout_seconds'] = parseInt(approvalTimeout);
  if (dedupWindow) payload['dedup_window_seconds'] = parseInt(dedupWindow);
  if (claudeTimeout) payload['claude_timeout_seconds'] = parseInt(claudeTimeout);
  if (logLevel) payload['log_level'] = logLevel;

  if (!Object.keys(payload).length) {
    toast('No changes to save.', 'err');
    return;
  }

  const statusEl = document.getElementById('sh-cfg-status');
  statusEl.textContent = 'Saving…';
  try {
    const r = await fetch(_SH_API() + '/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.status);
    statusEl.textContent = 'Saved. Restarting service…';
    document.getElementById('sh-cfg-api-key').value = ''; document.getElementById('sh-cfg-openai-key').value = '';
    // Restart nova-selfheal via Nova API
    try {
      await fetch('/admin/selfheal-restart', {method: 'POST'});
    } catch(e) {}
    setTimeout(() => { statusEl.textContent = ''; loadSelfHeal(); }, 3000);
    toast('Configuration saved.');
  } catch(e) {
    statusEl.textContent = 'Error: ' + e.message;
    toast('Save failed: ' + e.message, 'err');
  }
}

function shToggleKeyVisibility(id, btn) {
  if (!id) id = 'sh-cfg-api-key';
  const input = document.getElementById(id);
  if (!btn) btn = input.nextElementSibling;
  if (input.type === 'password') {
    input.type = 'text';
    btn.textContent = 'Hide';
  } else {
    input.type = 'password';
    btn.textContent = 'Show';
  }
}

async function shApprove(fix_id) {
  try {
    await _shFetch('/pending/' + fix_id + '/approve', {method:'POST'});
    toast('Fix approved — applying patch\u2026');
    setTimeout(loadSelfHeal, 1500);
  } catch(e) { toast('Approve failed: ' + e.message, 'err'); }
}

async function shReject(fix_id) {
  try {
    await _shFetch('/pending/' + fix_id + '/reject', {method:'POST'});
    toast('Fix rejected.');
    setTimeout(loadSelfHeal, 800);
  } catch(e) { toast('Reject failed: ' + e.message, 'err'); }
}

async function shBulkRejectAll() {
  if (!confirm('Reject ALL pending fixes?')) return;
  try {
    const pending = await _shFetch('/pending');
    const ids = (pending || []).map(f => f.fix_id).filter(Boolean);
    if (!ids.length) { toast('No pending fixes to reject.'); return; }
    await _shFetch('/pending/bulk-reject', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({fix_ids: ids})});
    toast(ids.length + ' pending fixes rejected.');
    setTimeout(loadSelfHeal, 800);
  } catch(e) { toast('Bulk reject failed: ' + e.message, 'err'); }
}

function _shEsc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Group By ──

let _faGroupBy = 'none';

function faSetGroupBy(mode, btn) {
  _faGroupBy = mode;
  const parent = btn?.parentElement;
  if (parent) parent.querySelectorAll('.fa-time-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  faRenderGrid(_faClips);
}

function _faGroupClips(clips) {
  if (_faGroupBy === 'none') return [{ label: '', clips }];
  const groups = {};
  for (const clip of clips) {
    let key;
    if (_faGroupBy === 'day') {
      key = clip.ts ? clip.ts.slice(0, 10) : 'Unknown';
    } else if (_faGroupBy === 'camera') {
      key = _faCameraLabel(clip.camera_entity_id);
    } else if (_faGroupBy === 'type') {
      key = _faEventIcon(clip) ? _faEventIcon(clip) + ' ' + _faGuessType(clip) : 'Motion';
    } else {
      key = 'All';
    }
    if (!groups[key]) groups[key] = [];
    groups[key].push(clip);
  }
  return Object.entries(groups).map(([label, clips]) => ({ label, clips }));
}

function _faGuessType(clip) {
  const desc = ((clip.description || '') + ' ' + JSON.stringify(clip.extra || {})).toLowerCase();
  if ((clip.extra || {}).delivery || desc.includes('delivery')) return 'Delivery';
  if (desc.includes('doorbell') || desc.includes('visitor')) return 'Doorbell';
  if (desc.includes('person') || desc.includes('someone') || desc.includes('walking')) return 'Person';
  if (desc.includes('vehicle') || desc.includes('car') || desc.includes('van')) return 'Vehicle';
  if (desc.includes('package') || desc.includes('parcel')) return 'Package';
  return 'Motion';
}

// ── Go to Timeline ──

function faGoToTimeline() {
  if (!_faActiveClipId) return;
  const clip = _faClips.find(c => c.id === _faActiveClipId);
  if (!clip) return;
  // Switch to Event History view filtered to this clip's time
  faCollapsePlayer();
  faSetView('history');
}

// ── Bulk select ──

function faToggleBulkMode(on) {
  _faBulkMode = on;
  _faBulkSelected.clear();
  document.getElementById('fa-bulk-actions').style.display = on ? '' : 'none';
  _faUpdateBulkCount();
  faRenderGrid(_faClips);
}

function faBulkToggle(clipId, checked) {
  if (checked) _faBulkSelected.add(clipId);
  else _faBulkSelected.delete(clipId);
  _faUpdateBulkCount();
}

function _faUpdateBulkCount() {
  const el = document.getElementById('fa-bulk-count');
  if (el) el.textContent = _faBulkSelected.size;
}

function faBulkSelectAll() {  _faClips.forEach(c => _faBulkSelected.add(c.id));  _faUpdateBulkCount();  faRenderGrid(_faClips);}
function faBulkClearSelection() {
  _faBulkSelected.clear();
  _faUpdateBulkCount();
  faRenderGrid(_faClips);
}

async function faBulkDelete() {
  if (!_faBulkSelected.size) return;
  if (!confirm(`Delete ${_faBulkSelected.size} selected clips?`)) return;
  try {
    await api('POST', '/admin/motion-clips/delete', { ids: [..._faBulkSelected] });
    toast(`${_faBulkSelected.size} clips deleted`);
    _faClips = _faClips.filter(c => !_faBulkSelected.has(c.id));
    _faBulkSelected.clear();
    _faUpdateBulkCount();
    faRenderGrid(_faClips);
    faLoadStats();
  } catch (e) { toast('Bulk delete failed: ' + e.message); }
}

async function faBulkDeleteAll() {
  if (!confirm("DELETE ALL archived clips? This cannot be undone.")) return;
  try {
    const r = await api("POST", "/admin/motion-clips/delete", { delete_all: true });
    toast((r.deleted || 0) + " clips deleted, " + (r.files_removed || 0) + " files removed");
    _faClips = [];
    _faBulkSelected.clear();
    _faUpdateBulkCount();
    faRenderGrid(_faClips);
    faLoadStats();
  } catch(e) { toast("Failed: " + e.message); }
}

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  if (!document.getElementById('section-motion')?.classList.contains('active')) return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'Escape') faCollapsePlayer();
  if (e.key === ' ' && _faActiveClipId) {
    e.preventDefault();
    const vid = document.getElementById('fa-player-video');
    if (vid) vid.paused ? vid.play() : vid.pause();
  }
  if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
    if (!_faActiveClipId || !_faClips.length) return;
    const idx = _faClips.findIndex(c => c.id === _faActiveClipId);
    if (idx < 0) return;
    const next = e.key === 'ArrowRight' ? idx + 1 : idx - 1;
    if (next >= 0 && next < _faClips.length) {
      e.preventDefault();
      faExpandClip(_faClips[next].id);
    }
  }
});



// ── Chat with Nova ───────────────────────────────────────────────────────────
let _chatSessionId = 'admin_chat_' + Date.now();
let _chatApiKey = '';
let _chatLastText = '';

async function _ensureChatApiKey() {
  if (_chatApiKey) return;
  try { const r = await api('GET', '/admin/api-key'); _chatApiKey = r.api_key || ''; } catch {}
}

function _chatTs() { return new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); }

function _chatBubble(role, text) {
  const isUser = role === 'user';
  const wrap = document.createElement('div');
  wrap.style.cssText = `display:flex;flex-direction:column;${isUser?'align-items:flex-end;':'align-items:flex-start;'}`;
  const bubble = document.createElement('div');
  bubble.style.cssText = `max-width:80%;padding:10px 14px;border-radius:12px;word-wrap:break-word;white-space:pre-wrap;${
    isUser ? 'background:var(--accent);color:#fff;border-bottom-right-radius:4px;'
           : 'background:var(--bg1);color:var(--text1);border:1px solid var(--border);border-bottom-left-radius:4px;'
  }`;
  bubble.textContent = text;
  const ts = document.createElement('div');
  ts.style.cssText = 'font-size:10px;color:var(--text3);margin-top:2px;padding:0 4px;';
  ts.textContent = _chatTs();
  wrap.appendChild(bubble); wrap.appendChild(ts);
  return wrap;
}

function _chatToolBubble(calls) {
  const bubble = document.createElement('div');
  bubble.style.cssText = 'align-self:flex-start;padding:6px 10px;border-radius:8px;background:var(--bg1);border:1px solid var(--border);font-size:11px;color:var(--text3);font-family:monospace;';
  bubble.textContent = '🔧 ' + calls.map(c => c.function_name + '(' + Object.values(c.arguments || {}).join(', ') + ')').join(' → ');
  return bubble;
}

function chatClear() {
  _chatSessionId = 'admin_chat_' + Date.now();
  const c = document.getElementById('chat-messages');
  if (c) c.innerHTML = '<div style="color:var(--text3);text-align:center;padding:40px 0;">Say something to Nova...</div>';
  toast('Chat cleared');
}

async function chatSend(e) {
  e.preventDefault();
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  _chatLastText = text;
  input.value = '';
  const container = document.getElementById('chat-messages');
  if (container.children.length === 1 && container.children[0].style.textAlign === 'center') container.innerHTML = '';
  container.appendChild(_chatBubble('user', text));
  container.scrollTop = container.scrollHeight;
  const thinking = document.createElement('div');
  thinking.style.cssText = 'align-self:flex-start;color:var(--text3);font-size:12px;padding:6px;';
  thinking.textContent = 'Nova is thinking...';
  container.appendChild(thinking);
  container.scrollTop = container.scrollHeight;
  let timedOut = false;
  const slowTimer = setTimeout(() => { thinking.textContent = 'Still thinking... (GPU may be busy)'; }, 15000);
  try {
    await _ensureChatApiKey();
    const ctrl = new AbortController();
    const abortTimer = setTimeout(() => ctrl.abort(), 120000);
    const r = await fetch('/chat', {
      method: 'POST', headers: {'Content-Type':'application/json','X-API-Key':_chatApiKey},
      body: JSON.stringify({text, session_id: _chatSessionId}), signal: ctrl.signal,
    });
    clearTimeout(abortTimer); clearTimeout(slowTimer);
    if (!r.ok) throw new Error(await r.text() || r.statusText);
    const data = await r.json();
    container.removeChild(thinking);
    if (data.tool_calls && data.tool_calls.length) container.appendChild(_chatToolBubble(data.tool_calls));
    container.appendChild(_chatBubble('assistant', data.text || '(no response)'));
  } catch (err) {
    clearTimeout(slowTimer);
    container.removeChild(thinking);
    const msg = err.name === 'AbortError' ? 'Request timed out (2 min). GPU may be overloaded.' : err.message;
    const errWrap = document.createElement('div');
    errWrap.style.cssText = 'align-self:flex-start;display:flex;flex-direction:column;gap:4px;';
    const errBubble = _chatBubble('assistant', '⚠ ' + msg);
    errWrap.appendChild(errBubble);
    const retryBtn = document.createElement('button');
    retryBtn.className = 'btn btn-outline';
    retryBtn.style.cssText = 'font-size:11px;padding:4px 10px;align-self:flex-start;';
    retryBtn.textContent = '↻ Retry';
    retryBtn.onclick = () => { errWrap.remove(); document.getElementById('chat-input').value = _chatLastText; chatSend(new Event('submit')); };
    errWrap.appendChild(retryBtn);
    container.appendChild(errWrap);
  }
  container.scrollTop = container.scrollHeight;
}

// ── Face Recognition ─────────────────────────────────────────────────────────
async function loadFaces() {
  const unknownEl = document.getElementById('faces-unknown');
  const knownEl = document.getElementById('faces-known');
  if (!unknownEl || !knownEl) return;
  try {
    const [unknown, known] = await Promise.all([
      api('GET', '/admin/faces/unknown'),
      api('GET', '/admin/faces/known'),
    ]);
    if (!unknown.available) {
      unknownEl.innerHTML = '<div class="text-sm text-muted">CodeProject.AI not configured</div>';
      knownEl.innerHTML = '';
      return;
    }
    if (!unknown.faces.length) {
      unknownEl.innerHTML = '<div class="text-sm text-muted">No unknown faces pending</div>';
    } else {
      unknownEl.innerHTML = unknown.faces.map(f => `
        <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;text-align:center;">
          <img src="data:image/jpeg;base64,${f.crop_b64}" style="width:100%;aspect-ratio:1;object-fit:cover;">
          <div style="padding:8px;">
            <div style="font-size:10px;color:var(--text3);margin-bottom:4px;">${(f.confidence * 100).toFixed(0)}% conf</div>
            <input type="text" placeholder="Name this person" style="width:100%;font-size:11px;padding:4px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg1);color:var(--text1);margin-bottom:4px;" id="face-name-${f.id}">
            <div style="display:flex;gap:4px;">
              <button class="btn btn-primary" style="flex:1;font-size:10px;padding:3px;" onclick="registerFace('${f.id}')">Save</button>
              <button class="btn btn-outline" style="font-size:10px;padding:3px;" onclick="dismissFace('${f.id}')">✕</button>
            </div>
          </div>
        </div>
      `).join('');
    }
    knownEl.innerHTML = (known.faces || []).map(name =>
      `<span style="display:inline-flex;align-items:center;gap:4px;padding:4px 12px;border-radius:99px;background:var(--surface2);color:var(--text1);font-size:12px;font-weight:500;">${name}<button onclick="deleteFace('${name}')" style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:14px;padding:0 0 0 2px;" title="Delete">×</button></span>`
    ).join('');
  } catch (e) {
    unknownEl.innerHTML = '<div class="text-sm" style="color:var(--danger);">Failed to load faces</div>';
  }
}

async function registerFace(faceId) {
  const input = document.getElementById('face-name-' + faceId);
  const name = input?.value.trim();
  if (!name) { toast('Enter a name first', 'err'); return; }
  try {
    const r = await api('POST', '/admin/faces/register', { face_id: faceId, name });
    if (r.ok) { toast(`Registered ${name}`, 'ok'); loadFaces(); }
    else { toast(r.error || 'Failed', 'err'); }
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
}

async function dismissFace(faceId) {
  try { await api('DELETE', '/admin/faces/unknown/' + faceId); loadFaces(); } catch {}
}

async function deleteFace(name) {
  if (!confirm(`Delete face "${name}"? They will need to be re-registered.`)) return;
  try {
    await api('DELETE', '/admin/faces/known/' + encodeURIComponent(name));
    toast(`Deleted ${name}`, 'ok');
    loadFaces();
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
}

// ── Train New Face ────────────────────────────────────────────────────────────

let _trainFaceBytes = null;
let _trainFaceStream = null;

function _trainFaceSetPreview(blob) {
  const url = URL.createObjectURL(blob);
  const img = document.getElementById('train-face-preview');
  const wrap = document.getElementById('train-face-preview-wrap');
  if (img) img.src = url;
  if (wrap) wrap.style.display = '';
}

function _trainFaceStopCam() {
  if (_trainFaceStream) { _trainFaceStream.getTracks().forEach(t => t.stop()); _trainFaceStream = null; }
  const vid = document.getElementById('train-face-video');
  const btns = document.getElementById('train-face-webcam-btns');
  if (vid) { vid.srcObject = null; vid.style.display = 'none'; }
  if (btns) btns.style.display = 'none';
}

document.getElementById('train-face-file')?.addEventListener('change', function() {
  const file = this.files?.[0];
  if (!file) return;
  const fnEl = document.getElementById('train-face-filename');
  if (fnEl) fnEl.textContent = file.name;
  const reader = new FileReader();
  reader.onload = e => {
    _trainFaceBytes = new Uint8Array(e.target.result);
    _trainFaceStopCam();
    fetch(URL.createObjectURL(file)).then(() => {});  // warm
    _trainFaceSetPreview(file);
  };
  reader.readAsArrayBuffer(file);
});

document.getElementById('btn-train-face-webcam')?.addEventListener('click', async () => {
  const statusEl = document.getElementById('train-face-status');
  const btn = document.getElementById('btn-train-face-webcam');

  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    const msg = 'Camera requires HTTPS. Open the admin via <b>https://</b> instead of http://';
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">${msg}</span>`;
    return;
  }

  if (btn) btn.textContent = 'Starting…';
  if (statusEl) statusEl.textContent = '';
  try {
    _trainFaceStream = await navigator.mediaDevices.getUserMedia({ video: true });
    const vid = document.getElementById('train-face-video');
    const btns = document.getElementById('train-face-webcam-btns');
    if (vid) { vid.srcObject = _trainFaceStream; vid.style.display = 'block'; }
    if (btns) btns.style.display = 'flex';
    if (btn) btn.textContent = 'Use Webcam';
  } catch (e) {
    if (btn) btn.textContent = 'Use Webcam';
    const msg = e.name === 'NotAllowedError'        ? 'Camera permission denied — check browser settings'
              : e.name === 'NotFoundError'           ? 'No camera found on this device'
              : e.name === 'OverconstrainedError'    ? 'Camera constraint error: ' + e.message
              : 'Camera error: ' + e.name + ' — ' + e.message;
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">${msg}</span>`;
  }
});

document.getElementById('btn-train-face-snap')?.addEventListener('click', () => {
  const vid = document.getElementById('train-face-video');
  const canvas = document.getElementById('train-face-canvas');
  if (!vid || !canvas) return;
  canvas.width = vid.videoWidth;
  canvas.height = vid.videoHeight;
  canvas.getContext('2d').drawImage(vid, 0, 0);
  canvas.toBlob(blob => {
    if (!blob) return;
    blob.arrayBuffer().then(buf => {
      _trainFaceBytes = new Uint8Array(buf);
      const fnEl = document.getElementById('train-face-filename');
      if (fnEl) fnEl.textContent = 'webcam snapshot';
      _trainFaceSetPreview(blob);
    });
  }, 'image/jpeg', 0.92);
  _trainFaceStopCam();
});

document.getElementById('btn-train-face-cancel-cam')?.addEventListener('click', () => _trainFaceStopCam());

document.getElementById('btn-train-face-submit')?.addEventListener('click', async () => {
  const name = (document.getElementById('train-face-name')?.value || '').trim().toLowerCase();
  const statusEl = document.getElementById('train-face-status');
  if (!name) { toast('Enter a name first', 'err'); return; }
  if (!_trainFaceBytes) { toast('Choose a photo or snap from webcam', 'err'); return; }
  if (statusEl) statusEl.textContent = 'Registering…';
  try {
    const fd = new FormData();
    fd.append('name', name);
    fd.append('image', new Blob([_trainFaceBytes], { type: 'image/jpeg' }), 'face.jpg');
    const r = await fetch('/admin/faces/train', { method: 'POST', credentials: 'include', body: fd });
    if (r.status === 401) { window.location.href = '/admin/login'; return; }
    const data = await r.json();
    if (data.ok) {
      if (statusEl) statusEl.innerHTML = `<span style="color:var(--success)">Registered "${name}" successfully</span>`;
      toast(`Face registered: ${name}`, 'ok');
      _trainFaceBytes = null;
      document.getElementById('train-face-name').value = '';
      const fn = document.getElementById('train-face-filename'); if (fn) fn.textContent = '';
      const wrap = document.getElementById('train-face-preview-wrap'); if (wrap) wrap.style.display = 'none';
      const fileInput = document.getElementById('train-face-file'); if (fileInput) fileInput.value = '';
      loadFaces();
    } else {
      if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">${data.error || 'Registration failed'}</span>`;
      toast(data.error || 'Registration failed', 'err');
    }
  } catch (e) {
    if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">${e.message}</span>`;
    toast('Failed: ' + e.message, 'err');
  }
});

// ── Event Delegation (replaces inline onclick handlers) ──────────────────────

document.getElementById('nav')?.addEventListener('click', e => {
  const btn = e.target.closest('.nav-item[data-section]');
  if (btn) navigate(btn);
});

document.getElementById('skin-swatches')?.addEventListener('click', e => {
  const sw = e.target.closest('.skin-swatch[data-index]');
  if (sw) selectSkin(parseInt(sw.dataset.index));
});

document.getElementById('hair-swatches')?.addEventListener('click', e => {
  const sw = e.target.closest('.skin-swatch[data-hair]');
  if (sw) selectHair(parseInt(sw.dataset.hair));
});

document.getElementById('bg-swatches')?.addEventListener('click', e => {
  const sw = e.target.closest('.skin-swatch[data-color]');
  if (sw) setBgColor(sw.dataset.color);
});

document.getElementById('pylog-levels')?.addEventListener('click', e => {
  const btn = e.target.closest('.pylog-level[data-level]');
  if (btn) setPylogLevel(btn);
});

document.getElementById('dec-filters')?.addEventListener('click', e => {
  const btn = e.target.closest('.dec-filter[data-kind]');
  if (btn) filterDecisions(btn);
});

document.getElementById('cost-periods')?.addEventListener('click', e => {
  const btn = e.target.closest('.period-btn[data-period]');
  if (btn) selectCostPeriod(btn);
});

document.getElementById('fa-time-ranges')?.addEventListener('click', e => {
  const btn = e.target.closest('.fa-time-btn[data-range]');
  if (btn) faSetTimeRange(btn.dataset.range, btn);
});

document.getElementById('fa-group-btns')?.addEventListener('click', e => {
  const btn = e.target.closest('.fa-time-btn[data-groupby]');
  if (btn) faSetGroupBy(btn.dataset.groupby, btn);
});

document.getElementById('fa-view-btns')?.addEventListener('click', e => {
  const btn = e.target.closest('.fa-time-btn[data-view]');
  if (btn) faSetView(btn.dataset.view);
});

// ── Event bindings (migrated from inline handlers) ──────────────────────────

// -- Helper: toggle collapsible section (body + chevron) --
function _toggleCollapsibleSection(bodyId, chevronId, onExpandFn) {
  const b = document.getElementById(bodyId);
  const a = document.getElementById(chevronId);
  if (!b) return;
  if (b.style.display === 'none') {
    b.style.display = '';
    if (a) a.style.transform = 'rotate(180deg)';
    if (onExpandFn) onExpandFn();
  } else {
    b.style.display = 'none';
    if (a) a.style.transform = '';
  }
}

// -- Sidebar & topbar --
document.getElementById('nav')?.addEventListener('click', e => {
  const btn = e.target.closest('.nav-item[data-section]');
  if (btn) navigate(btn);
});
document.getElementById('sidebar-overlay')?.addEventListener('click', () => closeSidebar());
document.getElementById('btn-logout')?.addEventListener('click', () => logout());
document.getElementById('menu-toggle')?.addEventListener('click', () => toggleSidebar());
document.getElementById('theme-toggle')?.addEventListener('click', () => toggleTheme());
document.getElementById('btn-refresh-section')?.addEventListener('click', () => refreshSection());
document.getElementById('restart-btn')?.addEventListener('click', () => restartServer());

// -- Chat --
document.getElementById('chat-form')?.addEventListener('submit', e => chatSend(e));
document.getElementById('btn-chat-clear')?.addEventListener('click', () => chatClear());

// -- Config --
document.getElementById('btn-save-config')?.addEventListener('click', () => saveConfig());
document.getElementById('btn-load-config')?.addEventListener('click', () => loadConfig());
document.getElementById('btn-gemini-add-key')?.addEventListener('click', () => addGeminiKey());
document.getElementById('btn-gemini-refresh')?.addEventListener('click', () => loadGeminiPool());
document.getElementById('btn-save-vision-cameras')?.addEventListener('click', () => saveVisionCameras());
document.getElementById('btn-refresh-vision-cameras')?.addEventListener('click', () => loadVisionCameras());
document.getElementById('btn-add-room')?.addEventListener('click', () => addRoom());
document.getElementById('room-new-label')?.addEventListener('input', function() {
  const slug = this.value.trim().toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'');
  const idEl = document.getElementById('room-new-id');
  if (idEl && !idEl.dataset.manualEdit) idEl.value = slug;
});
document.getElementById('room-new-id')?.addEventListener('input', function() { this.dataset.manualEdit = this.value ? '1' : ''; });
document.getElementById('room-new-label')?.addEventListener('keydown', e => { if (e.key === 'Enter') addRoom(); });
document.getElementById('room-new-id')?.addEventListener('keydown', e => { if (e.key === 'Enter') addRoom(); });

// -- Prompts & Tuning --
document.querySelectorAll('.tuning-card-header').forEach(el => {
  el.addEventListener('click', () => toggleTuningCard(el));
});
document.getElementById('btn-close-prompt-editor-x')?.addEventListener('click', () => closePromptEditor());
document.getElementById('btn-close-prompt-editor')?.addEventListener('click', () => closePromptEditor());
document.getElementById('btn-save-current-prompt')?.addEventListener('click', () => saveCurrentPrompt());

// -- Speakers --
document.getElementById('btn-save-speaker-config')?.addEventListener('click', () => saveSpeakerConfig());
document.getElementById('btn-load-speaker-config')?.addEventListener('click', () => loadSpeakerConfig());

// -- Music --
document.getElementById('btn-refresh-music')?.addEventListener('click', () => loadMusicPlayers());
document.getElementById('music-search-input')?.addEventListener('keydown', e => { if (e.key === 'Enter') musicSearch(); });
document.getElementById('btn-music-search')?.addEventListener('click', () => musicSearch());
document.getElementById('music-players-toggle')?.addEventListener('click', () => {
  _toggleCollapsibleSection('music-players-body', 'music-players-chevron', loadMusicPlayers);
});

// -- Energy --
document.getElementById('btn-refresh-energy')?.addEventListener('click', () => loadEnergy());

// -- System Prompt --
document.getElementById('btn-save-prompt')?.addEventListener('click', () => savePrompt());
document.getElementById('btn-load-prompt')?.addEventListener('click', () => loadPrompt());

// -- Sync Devices --
document.getElementById('btn-sync-preview')?.addEventListener('click', () => syncPreview());
document.getElementById('btn-sync-select-all')?.addEventListener('click', () => syncSelectAll(true));
document.getElementById('btn-sync-deselect-all')?.addEventListener('click', () => syncSelectAll(false));
document.getElementById('sync-filter')?.addEventListener('input', () => syncFilterEntities());
document.getElementById('btn-sync-apply')?.addEventListener('click', () => syncApply());
document.getElementById('btn-sync-cancel')?.addEventListener('click', () => {
  document.getElementById('sync-preview-panel').style.display = 'none';
});

// -- ACL --
document.getElementById('btn-save-acl')?.addEventListener('click', () => saveAcl());
document.getElementById('btn-load-acl')?.addEventListener('click', () => loadAcl());

// -- Avatar --
document.getElementById('btn-save-skin-tone')?.addEventListener('click', () => saveSkinTone());
document.getElementById('btn-save-hair-color')?.addEventListener('click', () => saveHairColor());
document.querySelectorAll('input[name="bg-type"]').forEach(r => {
  r.addEventListener('change', () => onBgTypeChange());
});
document.getElementById('bg-color-picker')?.addEventListener('change', function() {
  document.getElementById('bg-color-input').value = this.value;
});
document.getElementById('bg-color-input')?.addEventListener('input', function() {
  try { document.getElementById('bg-color-picker').value = this.value; } catch(e) {}
});
document.getElementById('bg-image-input')?.addEventListener('input', () => previewBgImage());
document.getElementById('btn-save-avatar-bg')?.addEventListener('click', () => saveAvatarBg());
document.getElementById('btn-reset-avatar-bg')?.addEventListener('click', () => resetAvatarBg());
document.getElementById('avatar-upload-input')?.addEventListener('change', function() { uploadAvatar(this); });
document.getElementById('btn-upload-avatar')?.addEventListener('click', () => {
  document.getElementById('avatar-upload-input').click();
});
document.getElementById('btn-save-external-url')?.addEventListener('click', () => saveExternalUrl());
document.getElementById('btn-clear-external-url')?.addEventListener('click', () => clearExternalUrl());

// -- Server Logs --
document.getElementById('pylog-search')?.addEventListener('input', () => filterPylog());
document.getElementById('btn-clear-pylog')?.addEventListener('click', () => clearPylog());

// -- Memory --
document.getElementById('memory-save-btn')?.addEventListener('click', () => saveMemory());
document.getElementById('btn-load-memory')?.addEventListener('click', () => loadMemory());
document.getElementById('btn-clear-memory-form')?.addEventListener('click', () => clearMemoryForm());
document.getElementById('btn-clear-all-memory')?.addEventListener('click', () => clearAllMemory());

// -- AI Decisions --
document.getElementById('btn-clear-decisions')?.addEventListener('click', () => clearDecisions());

// -- Find Anything: search --
document.getElementById('fa-search-input')?.addEventListener('keydown', e => { if (e.key === 'Enter') faSearch(); });
document.getElementById('fa-search-input')?.addEventListener('input', () => faSearchDebounced());

// -- Find Anything: filter headers (collapse/expand) --
document.querySelectorAll('.fa-filter-header').forEach(el => {
  el.addEventListener('click', () => el.parentElement.classList.toggle('collapsed'));
});

// -- Find Anything: filter checkboxes & date inputs --
document.querySelectorAll('#fa-filter-cameras input[type=checkbox], #fa-filter-events input[type=checkbox]').forEach(cb => {
  cb.addEventListener('change', () => faApplyFilters());
});
document.getElementById('fa-flagged-only')?.addEventListener('change', () => faApplyFilters());
document.getElementById('fa-date-input')?.addEventListener('change', () => faApplyFilters(true));
document.getElementById('fa-date-from')?.addEventListener('change', () => faApplyFilters(true));
document.getElementById('fa-date-to')?.addEventListener('change', () => faApplyFilters(true));

// -- Find Anything: bulk mode --
document.getElementById('fa-bulk-toggle')?.addEventListener('change', function() { faToggleBulkMode(this.checked); });
document.getElementById('btn-fa-bulk-select-all')?.addEventListener('click', () => faBulkSelectAll());
document.getElementById('btn-fa-bulk-delete')?.addEventListener('click', () => faBulkDelete());
document.getElementById('btn-fa-bulk-clear')?.addEventListener('click', () => faBulkClearSelection());
document.getElementById('btn-fa-bulk-delete-all')?.addEventListener('click', () => faBulkDeleteAll());

// -- Find Anything: mobile sidebar --
document.getElementById('btn-fa-mobile-toggle')?.addEventListener('click', () => {
  document.getElementById('fa-sidebar').classList.toggle('open');
});
document.getElementById('fa-sidebar-overlay')?.addEventListener('click', () => {
  document.getElementById('fa-sidebar').classList.remove('open');
});

// -- Find Anything: player buttons --
document.getElementById('fa-player-timeline-link')?.addEventListener('click', () => faGoToTimeline());
document.getElementById('fa-player-flag-btn')?.addEventListener('click', () => faToggleFlag());
document.getElementById('btn-fa-download')?.addEventListener('click', () => faDownloadClip());
document.getElementById('btn-fa-delete')?.addEventListener('click', () => faDeleteClip());
document.getElementById('btn-fa-collapse')?.addEventListener('click', () => faCollapsePlayer());

// -- LLM Cost --
document.getElementById('btn-clear-costs')?.addEventListener('click', () => clearCosts());

// -- Tools: Announce --
document.getElementById('btn-test-announce')?.addEventListener('click', () => testAnnounce());
document.getElementById('btn-clear-announce-log')?.addEventListener('click', () => clearAnnouncementLog());

// -- Tools: Heating Shadow --
document.getElementById('btn-refresh-heating-shadow')?.addEventListener('click', () => loadHeatingShadow());
document.getElementById('btn-force-heating-winter')?.addEventListener('click', () => forceHeatingShadow('winter'));
document.getElementById('btn-force-heating-spring')?.addEventListener('click', () => forceHeatingShadow('spring'));

// -- Tools: Wake Word --
document.getElementById('wake-train-btn')?.addEventListener('click', () => trainWakeWord());
document.getElementById('wake-install-compiler-btn')?.addEventListener('click', () => installEdgeTPUCompiler());

// -- Tools: Conversation Audit --
document.getElementById('audit-toggle')?.addEventListener('click', () => {
  _toggleCollapsibleSection('audit-body', 'audit-chevron', loadConversationAudit);
});
document.getElementById('btn-audit-search')?.addEventListener('click', () => loadConversationAudit());
document.getElementById('btn-audit-refresh')?.addEventListener('click', () => loadConversationAudit());

// -- Self-Heal --
document.getElementById('btn-refresh-selfheal')?.addEventListener('click', () => loadSelfHeal());
document.getElementById('btn-sh-clear-all')?.addEventListener('click', () => shClearAllEvents());
document.getElementById('btn-sh-test-inject')?.addEventListener('click', () => shTestInject());
document.getElementById('sh-pending-toggle')?.addEventListener('click', () => {
  _toggleCollapsibleSection('sh-pending-body', 'sh-pending-chevron');
});
document.getElementById('btn-sh-bulk-reject')?.addEventListener('click', () => shBulkRejectAll());
document.getElementById('btn-sh-toggle-api-key')?.addEventListener('click', function() {
  shToggleKeyVisibility('sh-cfg-api-key', this);
});
document.getElementById('btn-sh-toggle-openai-key')?.addEventListener('click', function() {
  shToggleKeyVisibility('sh-cfg-openai-key', this);
});
document.getElementById('btn-sh-save-config')?.addEventListener('click', () => shSaveConfig());


// ══════════════════════════════════════════════════════════════════
// Scoreboard
// ══════════════════════════════════════════════════════════════════
let _sbConfig = {};

async function loadScoreboard() {
  loadSbNotifications();
  try {
    const d = await api('GET', '/admin/scoreboard');
    _sbConfig = d.config || {};
    _renderSbLeaderboard(d.weekly || []);
    _renderSbRecent(d.recent || []);
    _populateSbSelects(_sbConfig);
    _renderSbTasks(_sbConfig);
    _setSbToggleState(_sbConfig.show_widget !== false);
  } catch (e) { console.error('loadScoreboard', e); }
}

function _setSbToggleState(on) {
  const track = document.getElementById('sb-widget-toggle');
  const knob = document.getElementById('sb-widget-knob');
  if (!track || !knob) return;
  track.style.background = on ? 'var(--accent)' : 'var(--border)';
  knob.style.transform = on ? 'translateX(16px)' : 'translateX(0)';
}

async function sbToggleWidget() {
  const on = _sbConfig.show_widget !== false;
  const next = !on;
  try {
    await api('POST', '/admin/scoreboard/widget-visibility', { show_widget: next });
    _sbConfig.show_widget = next;
    _setSbToggleState(next);
  } catch(e) { toast('Failed to update', 'err'); }
}

function _renderSbLeaderboard(weekly) {
  const el = document.getElementById('sb-leaderboard');
  if (!el) return;
  if (!weekly.length) { el.innerHTML = '<span style="color:var(--muted)">No scores yet this week.</span>'; return; }
  const medals = ['🥇','🥈','🥉'];
  el.innerHTML = weekly.map((s, i) => {
    const photoUrl = `/admin/faces/photo/${encodeURIComponent(s.person)}`;
    const initials = s.person.charAt(0).toUpperCase();
    return `<div style="background:var(--bg3);border-radius:10px;padding:16px 20px;min-width:120px;text-align:center;">
      <div style="position:relative;width:56px;height:56px;margin:0 auto 8px;">
        <img src="${photoUrl}" style="width:56px;height:56px;border-radius:50%;object-fit:cover;border:2px solid var(--accent);"
          onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
        <div style="display:none;width:56px;height:56px;border-radius:50%;background:var(--surface2);align-items:center;justify-content:center;font-size:22px;font-weight:700;color:var(--accent);border:2px solid var(--accent);">${initials}</div>
        <div style="position:absolute;bottom:-4px;right:-4px;font-size:18px;">${medals[i] || '🏅'}</div>
      </div>
      <div style="font-weight:700;font-size:15px;margin:4px 0;">${s.person.charAt(0).toUpperCase()+s.person.slice(1)}</div>
      <div style="font-size:20px;font-weight:700;color:var(--accent);">${s.points} pts</div>
      <div style="font-size:11px;color:var(--muted);">${s.tasks} tasks</div>
    </div>`;
  }).join('');
}

function _renderSbRecent(recent) {
  const el = document.getElementById('sb-recent');
  if (!el) return;
  if (!recent.length) { el.innerHTML = '<span style="color:var(--muted)">No recent activity.</span>'; return; }
  el.innerHTML = '<table style="width:100%;border-collapse:collapse;">' +
    '<thead><tr style="color:var(--muted);text-align:left;"><th style="padding:4px 8px;">Person</th><th style="padding:4px 8px;">Task</th><th style="padding:4px 8px;">Points</th><th style="padding:4px 8px;">When</th><th></th></tr></thead><tbody>' +
    recent.map(r => {
      const when = new Date(r.ts * 1000).toLocaleString();
      return `<tr style="border-top:1px solid var(--border);">
        <td style="padding:4px 8px;">${r.person}</td>
        <td style="padding:4px 8px;">${r.task_label}</td>
        <td style="padding:4px 8px;color:var(--accent);">+${r.points}</td>
        <td style="padding:4px 8px;color:var(--muted);">${when}</td>
        <td style="padding:4px 8px;"><button class="btn btn-outline" style="font-size:11px;padding:2px 8px;" onclick="sbDeleteLog(${r.id})">Delete</button></td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

function _populateSbSelects(cfg) {
  const taskSel = document.getElementById('sb-award-task');
  if (taskSel) taskSel.innerHTML = (cfg.tasks || []).map(t => `<option value="${t.id}">${t.label} (${t.points}pts)</option>`).join('');

  const members = cfg.members || [];
  const personSel = document.getElementById('sb-award-person');
  if (personSel) personSel.innerHTML = members.map(m => `<option value="${m}">${m.charAt(0).toUpperCase()+m.slice(1)}</option>`).join('');

  // Checkboxes for assign-to in add-task form
  const assignDiv = document.getElementById('sb-new-assigned');
  if (assignDiv) {
    assignDiv.innerHTML = members.map(m => `
      <label style="display:flex;align-items:center;gap:4px;font-size:12px;">
        <input type="checkbox" name="sb-assign" value="${m}"> ${m.charAt(0).toUpperCase()+m.slice(1)}
      </label>`).join('');
  }
}

function _renderSbTasks(cfg) {
  const el = document.getElementById('sb-tasks-list');
  if (!el) return;
  const members = cfg.members || [];
  const tasks = cfg.tasks || [];
  if (!tasks.length) { el.innerHTML = '<span style="color:var(--muted)">No tasks configured.</span>'; return; }
  el.innerHTML = '<table style="width:100%;border-collapse:collapse;">' +
    '<thead><tr style="color:var(--muted);font-size:12px;text-align:left;">' +
    '<th style="padding:4px 8px;">Task</th><th style="padding:4px 8px;">Pts</th>' +
    '<th style="padding:4px 8px;">Verify</th><th style="padding:4px 8px;">Assigned To</th><th style="padding:4px 8px;"></th>' +
    '</tr></thead><tbody>' +
    tasks.map(t => {
      const assigned = (t.assigned_to && t.assigned_to.length) ? t.assigned_to.join(', ') : 'Everyone';
      const assignCheckboxes = members.map(m => `
        <label style="display:flex;align-items:center;gap:3px;font-size:11px;white-space:nowrap;">
          <input type="checkbox" onchange="sbToggleAssign('${t.id}','${m}',this.checked)" ${(t.assigned_to||[]).includes(m)?'checked':''}>
          ${m.charAt(0).toUpperCase()+m.slice(1)}
        </label>`).join('');
      return `<tr style="border-top:1px solid var(--border);">
        <td style="padding:6px 8px;font-weight:600;">${t.label}<div style="font-size:10px;color:var(--muted);">${t.id}</div></td>
        <td style="padding:6px 8px;color:var(--accent);">${t.points}</td>
        <td style="padding:6px 8px;">${t.verification}</td>
        <td style="padding:6px 8px;"><div style="display:flex;gap:8px;flex-wrap:wrap;">${assignCheckboxes}</div></td>
        <td style="padding:6px 8px;"><button class="btn btn-outline" style="font-size:11px;padding:2px 8px;color:#f87171;" onclick="sbDeleteTask('${t.id}')">Delete</button></td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

async function sbToggleAssign(taskId, member, checked) {
  const cfg = _sbConfig;
  const task = (cfg.tasks||[]).find(t => t.id === taskId);
  if (!task) return;
  const assigned = task.assigned_to || [];
  if (checked && !assigned.includes(member)) assigned.push(member);
  if (!checked) task.assigned_to = assigned.filter(m => m !== member);
  else task.assigned_to = assigned;
  try {
    await api('PATCH', '/admin/scoreboard/tasks/' + taskId, { assigned_to: task.assigned_to });
  } catch(e) { toast('Failed to update assignment', 'err'); }
}

async function sbDeleteTask(taskId) {
  if (!confirm(`Delete task "${taskId}"? This cannot be undone.`)) return;
  try {
    const r = await api('DELETE', '/admin/scoreboard/tasks/' + taskId);
    if (r.ok) { toast('Task deleted', 'ok'); loadScoreboard(); }
    else toast(r.error || 'Error', 'err');
  } catch(e) { toast('Error', 'err'); }
}

async function sbAddTask() {
  const id = document.getElementById('sb-new-id')?.value.trim();
  const label = document.getElementById('sb-new-label')?.value.trim();
  const points = parseInt(document.getElementById('sb-new-points')?.value || '5');
  const cooldown = parseInt(document.getElementById('sb-new-cooldown')?.value || '16');
  const verification = document.getElementById('sb-new-verification')?.value || 'honour';
  const keywords = (document.getElementById('sb-new-keywords')?.value || '').split(',').map(s=>s.trim()).filter(Boolean);
  const assigned_to = [...document.querySelectorAll('input[name="sb-assign"]:checked')].map(cb => cb.value);
  const msg = document.getElementById('sb-add-task-msg');
  if (!id || !label) { if (msg) msg.textContent = 'ID and Label are required.'; return; }
  try {
    const r = await api('POST', '/admin/scoreboard/tasks', { id, label, points, cooldown_hours: cooldown, verification, keywords, assigned_to });
    if (r.ok) {
      if (msg) msg.textContent = 'Task added!';
      loadScoreboard();
      ['sb-new-id','sb-new-label','sb-new-keywords'].forEach(id => { const el = document.getElementById(id); if(el) el.value=''; });
    } else { if (msg) msg.textContent = r.error || 'Error'; }
  } catch(e) { if (msg) msg.textContent = 'Error: ' + e.message; }
}

async function sbDeleteLog(id) {
  if (!confirm('Delete this log entry?')) return;
  try {
    await api('DELETE', '/admin/scoreboard/logs/' + id);
    loadScoreboard();
  } catch (e) { toast('Failed to delete', 'err'); }
}

async function sbAward() {
  const person = document.getElementById('sb-award-person')?.value || '';
  const taskId = document.getElementById('sb-award-task')?.value || '';
  const msg = document.getElementById('sb-award-msg');
  if (!person || !taskId) { if (msg) msg.textContent = 'Select person and task.'; return; }
  try {
    const r = await api('POST', '/admin/scoreboard/log', { person, task_id: taskId });
    if (msg) msg.textContent = r.ok ? 'Points awarded!' : (r.error || 'Error');
    loadScoreboard();
  } catch (e) { if (msg) msg.textContent = 'Error: ' + e.message; }
}

async function sbLoadLogs() {
  const days = document.getElementById('sb-log-days')?.value || 7;
  const el = document.getElementById('sb-logs-table');
  try {
    const d = await api('GET', '/admin/scoreboard/logs?days=' + days);
    const logs = d.logs || [];
    if (!logs.length) { if (el) el.innerHTML = '<span style="color:var(--muted)">No logs found.</span>'; return; }
    if (el) el.innerHTML = '<table style="width:100%;border-collapse:collapse;">' +
      '<thead><tr style="color:var(--muted);"><th style="padding:4px 8px;text-align:left;">Date</th><th style="padding:4px 8px;text-align:left;">Person</th><th style="padding:4px 8px;text-align:left;">Task</th><th style="padding:4px 8px;">Pts</th><th style="padding:4px 8px;">Verified</th><th></th></tr></thead><tbody>' +
      logs.map(r => {
        const when = new Date(r.ts * 1000).toLocaleString();
        return `<tr style="border-top:1px solid var(--border);">
          <td style="padding:4px 8px;color:var(--muted);">${when}</td>
          <td style="padding:4px 8px;">${r.person}</td>
          <td style="padding:4px 8px;">${r.task_label}</td>
          <td style="padding:4px 8px;text-align:center;color:var(--accent);">+${r.points}</td>
          <td style="padding:4px 8px;text-align:center;">${r.verified ? '✓' : '—'}</td>
          <td style="padding:4px 8px;"><button class="btn btn-outline" style="font-size:11px;padding:2px 8px;" onclick="sbDeleteLog(${r.id})">Del</button></td>
        </tr>`;
      }).join('') + '</tbody></table>';
  } catch (e) { if (el) el.innerHTML = 'Error loading logs.'; }
}

// -- Faces --
document.getElementById('btn-refresh-faces')?.addEventListener('click', () => loadFaces());

// -- Scoreboard --
document.getElementById('btn-sb-refresh')?.addEventListener('click', () => loadScoreboard());
document.getElementById('btn-sb-award')?.addEventListener('click', () => sbAward());
document.getElementById('btn-sb-logs')?.addEventListener('click', () => sbLoadLogs());
document.getElementById('btn-sb-add-task')?.addEventListener('click', () => sbAddTask());

// -- Users --
document.getElementById('btn-create-user')?.addEventListener('click', () => createUser());
document.getElementById('btn-submit-pw-change')?.addEventListener('click', () => submitPasswordChange());
document.getElementById('btn-cancel-pw-change')?.addEventListener('click', () => cancelPasswordChange());
async function loadSbNotifications() {
  try {
    const d = await apiFetch('/admin/scoreboard/notifications');
    const el = document.getElementById('sb-blind-names');
    if (el) el.value = d.blind_reminder_names || '';
  } catch(e) { console.warn('loadSbNotifications', e); }
}

async function sbSaveNotifications() {
  const names = (document.getElementById('sb-blind-names')?.value || '').trim();
  if (!names) return;
  try {
    await apiFetch('/admin/scoreboard/notifications', {
      method: 'PATCH',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({blind_reminder_names: names}),
    });
    showToast('Blind reminder names saved.');
  } catch(e) { showToast('Save failed: ' + e.message, true); }
}

async function loadPenalties() {
  try {
    const d = await api('GET', '/admin/scoreboard/penalties');
    const penalties = d.penalties || [];
    // Populate dropdown in Deductions card
    const sel = document.getElementById('sb-deduct-penalty');
    if (sel) sel.innerHTML = penalties.map(p => `<option value="${p.id}">${p.label} (-${p.points}pts)</option>`).join('');
    // Render manage list
    const list = document.getElementById('sb-penalties-list');
    if (list) {
      if (!penalties.length) {
        list.innerHTML = '<span style="color:var(--muted);font-size:13px;">No penalty types configured.</span>';
      } else {
        list.innerHTML = '<table style="width:100%;border-collapse:collapse;">' +
          '<thead><tr style="color:var(--muted);font-size:12px;text-align:left;">' +
          '<th style="padding:4px 8px;">Label</th><th style="padding:4px 8px;">Points</th><th style="padding:4px 8px;"></th>' +
          '</tr></thead><tbody>' +
          penalties.map(p => `<tr>
            <td style="padding:4px 8px;font-size:13px;">${p.label}</td>
            <td style="padding:4px 8px;font-size:13px;color:#e53e3e;">-${p.points}</td>
            <td style="padding:4px 8px;">
              <button class="btn" style="padding:2px 8px;font-size:11px;" onclick="sbDeletePenalty('${p.id}')">Remove</button>
            </td>
          </tr>`).join('') +
          '</tbody></table>';
      }
    }
  } catch(e) { console.warn('loadPenalties', e); }
}

async function sbIssueDeduction() {
  const person = document.getElementById('sb-deduct-person')?.value;
  const penalty_id = document.getElementById('sb-deduct-penalty')?.value;
  if (!person || !penalty_id) { toast('Select a member and penalty', 'err'); return; }
  try {
    const r = await api('POST', '/admin/scoreboard/penalty', { person, penalty_id });
    toast(`-${r.deducted}pts deducted from ${person} for ${r.label}`, 'ok');
    loadScoreboard();
  } catch(e) { toast('Deduction failed: ' + e.message, 'err'); }
}

async function sbAddPenalty() {
  const id = (document.getElementById('sb-new-penalty-id')?.value || '').trim();
  const label = (document.getElementById('sb-new-penalty-label')?.value || '').trim();
  const points = parseInt(document.getElementById('sb-new-penalty-points')?.value || '10');
  if (!id || !label) { toast('ID and label required', 'err'); return; }
  try {
    await api('POST', '/admin/scoreboard/penalties', { id, label, points });
    document.getElementById('sb-new-penalty-id').value = '';
    document.getElementById('sb-new-penalty-label').value = '';
    document.getElementById('sb-new-penalty-points').value = '10';
    toast('Penalty type added', 'ok');
    loadPenalties();
  } catch(e) { toast('Failed: ' + e.message, 'err'); }
}

async function sbDeletePenalty(penalty_id) {
  if (!confirm('Remove this penalty type?')) return;
  try {
    await api('DELETE', '/admin/scoreboard/penalties/' + penalty_id);
    toast('Penalty removed', 'ok');
    loadPenalties();
  } catch(e) { toast('Failed: ' + e.message, 'err'); }
}
