'use strict';


let _currentRole   = 'viewer';
let _changePwTarget = null;

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
  parental:'Parental Control',
  dashboard:'Dashboard', config:'Configuration', speakers:'Speakers', music:'Music', energy:'Energy', prompt:'System Prompt',
  'prompts-tuning':'Prompts & Tuning',
  acl:'ACL Rules', sessions:'Sessions', memory:'Memory',
  avatar:'Avatar', tools:'Tools', users:'Users', faces:'Face Recognition',
  decisions:'AI Decisions', selfheal:'Self-Heal', motion:'AI Vision',
  costs:'LLM Cost', metrics:'System Metrics', pylog:'Server Logs',
};

const _sectionControllers = new Map();
let _activeSection = document.querySelector('.nav-item.active')?.dataset.section || 'dashboard';
const SIDEBAR_PREF_KEY = 'novaAdminSidebarCollapsed';

function registerAdminSection(section, controller) {
  if (!section || !controller) return;
  _sectionControllers.set(section, controller);
}

function _runSectionHook(section, hook, payload) {
  const controller = _sectionControllers.get(section);
  const fn = controller?.[hook];
  if (typeof fn === 'function') return fn(payload);
  return undefined;
}

window.registerAdminSection = registerAdminSection;

// ── Avatar section moved to static/admin-avatar.js ──────────────────────────

function dashOpenSection(section) {
  const btn = document.querySelector(`.nav-item[data-section="${section}"]`);
  if (btn) btn.click();
}

function _isMobileNav() {
  return window.matchMedia('(max-width: 900px)').matches;
}

function applySidebarPreference(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', !!collapsed && !_isMobileNav());
}

function loadSidebarPreference() {
  const collapsed = localStorage.getItem(SIDEBAR_PREF_KEY) === '1';
  applySidebarPreference(collapsed);
}

function setSidebarCollapsed(collapsed) {
  localStorage.setItem(SIDEBAR_PREF_KEY, collapsed ? '1' : '0');
  applySidebarPreference(collapsed);
}

function initSidebarMode() {
  document.querySelectorAll('.nav-item[data-section]').forEach(btn => {
    if (!btn.title) btn.title = btn.textContent.trim();
  });
  loadSidebarPreference();
  window.addEventListener('resize', () => {
    if (_isMobileNav()) {
      document.body.classList.remove('sidebar-collapsed');
    } else {
      loadSidebarPreference();
      closeSidebar();
    }
  });
}

function toggleSidebar() {
  if (_isMobileNav()) {
    const open = document.getElementById('sidebar').classList.toggle('open');
    document.getElementById('sidebar-overlay').classList.toggle('show', open);
    return;
  }
  setSidebarCollapsed(!document.body.classList.contains('sidebar-collapsed'));
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-overlay').classList.remove('show');
}

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

initSidebarMode();

function navigate(el) {
  closeSidebar();
  const previousSection = _activeSection;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  el.classList.add('active');
  const sec = el.dataset.section;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.getElementById('section-' + sec).classList.add('active');
  document.getElementById('section-title').textContent = TITLES[sec] || sec;
  if (previousSection && previousSection !== sec) _runSectionHook(previousSection, 'onLeave', { from: previousSection, to: sec });
  _activeSection = sec;
  if (sec === 'prompt')    loadPrompt();
  if (sec === 'prompts-tuning') loadPromptsTuning();
  if (sec === 'acl')       loadAcl();
  if (sec === 'sessions')  loadSessions();
  if (sec === 'memory')    { loadMemory(); loadStaleMemories(); }
  if (sec === 'dashboard') loadDashboard();
  if (sec === 'users')     loadUsers();
  if (sec === 'speakers')  loadSpeakerConfig();
  if (sec === 'pylog')     initPylog();
  _runSectionHook(sec, 'onEnter', { from: previousSection, to: sec });
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

async function selfhealRequest(path, opts={}) {
  const r = await fetch('/admin/selfheal' + path, opts);
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error(data?.error || `HTTP ${r.status}`);
  return data;
}

const adminApi = {
  config: {
    getConfig: () => api('GET', '/admin/config'),
    saveConfig: (payload) => api('POST', '/admin/config', payload),
    getGeminiPool: () => api('GET', '/admin/gemini-pool'),
    addGeminiKey: (payload) => api('POST', '/admin/gemini-pool/add', payload),
    removeGeminiKey: (index) => api('DELETE', '/admin/gemini-pool/' + encodeURIComponent(index)),
    toggleGeminiKey: (payload) => api('POST', '/admin/gemini-pool/toggle', payload),
    getVisionCameras: () => api('GET', '/admin/vision-cameras'),
    saveVisionCameras: (payload) => api('POST', '/admin/vision-cameras', payload),
    getRooms: () => api('GET', '/admin/rooms'),
    createRoom: (payload) => api('POST', '/admin/rooms', payload),
    deleteRoom: (roomId) => api('DELETE', '/admin/rooms/' + encodeURIComponent(roomId)),
    patchRoom: (roomId, payload) => api('PATCH', '/admin/rooms/' + encodeURIComponent(roomId), payload),
    getAvatars: () => api('GET', '/admin/avatars'),
  },
  avatar: {
    getSettings: () => api('GET', '/admin/avatar-settings'),
    saveSettings: (payload) => api('POST', '/admin/avatar-settings', payload),
    getLibrary: () => api('GET', '/admin/avatars'),
    deleteAvatar: (filename) => api('DELETE', '/admin/avatars/' + filename),
  },
  energy: {
    getSummary: () => api('GET', '/admin/energy/summary'),
    getDevices: () => api('GET', '/admin/energy/devices'),
  },
  chat: {
    getApiKey: () => api('GET', '/admin/api-key'),
  },
  faces: {
    getUnknown: () => api('GET', '/admin/faces/unknown'),
    getKnown: () => api('GET', '/admin/faces/known'),
    register: (payload) => api('POST', '/admin/faces/register', payload),
    dismissUnknown: (faceId) => api('DELETE', '/admin/faces/unknown/' + encodeURIComponent(faceId)),
    deleteKnown: (name) => api('DELETE', '/admin/faces/known/' + encodeURIComponent(name)),
  },
  selfheal: {
    getStatus: () => selfhealRequest('/status'),
    getPending: () => selfhealRequest('/pending'),
    getEvents: (limit=100) => selfhealRequest('/events?limit=' + encodeURIComponent(limit)),
    getConfig: () => selfhealRequest('/config'),
    clearEvents: () => selfhealRequest('/clear-events', { method: 'POST' }),
    approve: (fixId) => selfhealRequest('/pending/' + encodeURIComponent(fixId) + '/approve', { method: 'POST' }),
    reject: (fixId) => selfhealRequest('/pending/' + encodeURIComponent(fixId) + '/reject', { method: 'POST' }),
    bulkReject: (fixIds) => selfhealRequest('/pending/bulk-reject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fix_ids: fixIds }),
    }),
    saveConfig: (payload) => selfhealRequest('/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
    injectTest: () => api('POST', '/admin/selfheal-test'),
    restart: () => fetch('/admin/selfheal-restart', { method: 'POST' }),
  },
  music: {
    getPlayers: () => api('GET', '/admin/music/players'),
    getStatus: () => api('GET', '/admin/music/status'),
    search: (q) => api('GET', '/admin/music/search?q=' + encodeURIComponent(q)),
    control: (payload) => api('POST', '/admin/music/control', payload),
  },
  parental: {
    getStatus: () => api('GET', '/admin/parental/status'),
    getConfigurations: () => api('GET', '/admin/parental/configurations'),
    getDevices: () => api('GET', '/admin/parental/devices'),
    getDeviceInfo: (number) => api('GET', `/admin/parental/devices/${encodeURIComponent(number)}/info`),
    getApps: (query='') => {
      const q = String(query || '').trim();
      const endpoint = q ? `/admin/parental/apps?query=${encodeURIComponent(q)}` : '/admin/parental/apps';
      return api('GET', endpoint);
    },
    sendAlert: (payload) => api('POST', '/admin/parental/alert', payload),
    blockApp: (payload) => api('POST', '/admin/parental/apps/block', payload),
    deployApp: (payload) => api('POST', '/admin/parental/apps/deploy', payload),
    getEnrollQr: (configId) => api('GET', `/admin/parental/enroll/${encodeURIComponent(configId)}`),
    getProvisioningQr: (configId) => api('GET', `/admin/parental/provisioning-qr?config_id=${encodeURIComponent(configId)}`),
    getOverrides: (status='') => api('GET', `/admin/parental/overrides${status ? '?status=' + encodeURIComponent(status) : ''}`),
    approveOverride: (id) => api('POST', `/admin/parental/overrides/${encodeURIComponent(id)}/approve`),
    denyOverride: (id) => api('POST', `/admin/parental/overrides/${encodeURIComponent(id)}/deny`),
  },
  scoreboard: {
    getOverview: () => api('GET', '/admin/scoreboard'),
    setWidgetVisibility: (showWidget) => api('POST', '/admin/scoreboard/widget-visibility', { show_widget: showWidget }),
    patchTaskAssignment: (taskId, assignedTo) => api('PATCH', '/admin/scoreboard/tasks/' + encodeURIComponent(taskId), { assigned_to: assignedTo }),
    deleteTask: (taskId) => api('DELETE', '/admin/scoreboard/tasks/' + encodeURIComponent(taskId)),
    createTask: (payload) => api('POST', '/admin/scoreboard/tasks', payload),
    deleteLog: (id) => api('DELETE', '/admin/scoreboard/logs/' + encodeURIComponent(id)),
    awardTask: (person, taskId) => api('POST', '/admin/scoreboard/log', { person, task_id: taskId }),
    getLogs: (days) => api('GET', '/admin/scoreboard/logs?days=' + encodeURIComponent(days)),
    getNotifications: () => api('GET', '/admin/scoreboard/notifications'),
    saveNotifications: (names) => api('PATCH', '/admin/scoreboard/notifications', { blind_reminder_names: names }),
    getPenalties: () => api('GET', '/admin/scoreboard/penalties'),
    issuePenalty: (person, penaltyId) => api('POST', '/admin/scoreboard/penalty', { person, penalty_id: penaltyId }),
    createPenalty: (payload) => api('POST', '/admin/scoreboard/penalties', payload),
    deletePenalty: (penaltyId) => api('DELETE', '/admin/scoreboard/penalties/' + encodeURIComponent(penaltyId)),
  },
  tools: {
    sendAnnouncementTest: (payload) => api('POST', '/admin/announce/test', payload),
    getAnnouncements: (limit=200) => api('GET', '/admin/announcements?limit=' + encodeURIComponent(limit)),
    clearAnnouncements: () => api('DELETE', '/admin/announcements'),
    getHeatingShadowHistory: (limit=80) => api('GET', '/admin/heating-shadow/history?limit=' + encodeURIComponent(limit)),
    forceHeatingShadow: (scenario) => api('POST', `/admin/heating-shadow/force?scenario=${encodeURIComponent(scenario)}`),
    getConversationAudit: (sessionId='') => {
      const sid = String(sessionId || '').trim();
      return api('GET', sid ? `/admin/conversations/${encodeURIComponent(sid)}` : '/admin/conversations?limit=100');
    },
    getWakeStatus: () => api('GET', '/admin/coral/wake-status'),
    installEdgeTpuCompiler: () => api('POST', '/admin/coral/install-edgetpu-compiler'),
  },
};

window.adminApi = adminApi;


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
  const dashNeedsFallback = (el) => !el || /^[-—\s]*$/.test(el.textContent || '');
  try {
    const [health, sessions, monthCost, metricsNow, decisions, selfhealStatus, memory, parentalDevices] = await Promise.all([
      fetch('/health').then(r => r.json()),
      api('GET', '/admin/sessions'),
      api('GET', '/admin/costs/history?period=month').catch(() => null),
      api('GET', '/admin/metrics').catch(() => null),
      fetch('/admin/decisions', { credentials:'include' }).then(r => r.ok ? r.json() : null).catch(() => null),
      selfhealRequest('/status').catch(() => null),
      api('GET', '/admin/memory?n=200').catch(() => null),
      api('GET', '/admin/parental/devices').catch(() => null),
    ]);
    renderHealth(health);
    document.getElementById('dash-sessions').textContent = sessions.active_sessions ?? '—';
    if (monthCost && monthCost.summary) {
      const s = monthCost.summary;
      const cost = s.cost_usd || 0;
      document.getElementById('dash-month-cost').textContent = '$' + cost.toFixed(cost < 0.01 ? 6 : 4);
      document.getElementById('dash-month-calls').textContent = (s.calls || 0) + ' calls this month';
    }
    if (decisions && Array.isArray(decisions.decisions)) {
      const items = decisions.decisions;
      const announces = items.filter(e => e.kind === 'triage_announce' || e.kind === 'motion_announce' || e.kind === 'weather_announce').length;
      const tools = items.filter(e => e.kind === 'tool_call').length;
      const el = document.getElementById('dash-decisions');
      const sub = document.getElementById('dash-decisions-sub');
      if (el) el.textContent = `${items.length} entries`;
      if (sub) sub.textContent = `${announces} announces · ${tools} tools`;
    } else {
      const el = document.getElementById('dash-decisions');
      const sub = document.getElementById('dash-decisions-sub');
      if (dashNeedsFallback(el)) el.textContent = 'Unavailable';
      if (dashNeedsFallback(sub)) sub.textContent = 'No data';
    }
    if (selfhealStatus) {
      const pending = Number(selfhealStatus.pending_count || 0);
      const applied = Number(selfhealStatus.patches_applied || 0);
      const errors = Number(selfhealStatus.errors_detected || 0);
      const el = document.getElementById('dash-selfheal');
      const sub = document.getElementById('dash-selfheal-sub');
      if (el) el.textContent = pending ? `${pending} pending` : 'Running';
      if (sub) sub.textContent = `${applied} applied · ${errors} errors`;
    } else {
      const el = document.getElementById('dash-selfheal');
      const sub = document.getElementById('dash-selfheal-sub');
      if (dashNeedsFallback(el)) el.textContent = 'Unavailable';
      if (dashNeedsFallback(sub)) sub.textContent = 'No data';
    }
    if (memory && Array.isArray(memory.memories)) {
      const items = memory.memories;
      const pinned = items.filter(m => m.pinned).length;
      const categories = new Set(items.map(m => (m.category || 'general').trim()).filter(Boolean)).size;
      const el = document.getElementById('dash-memory');
      const sub = document.getElementById('dash-memory-sub');
      if (el) el.textContent = `${items.length} stored`;
      if (sub) sub.textContent = `${pinned} pinned · ${categories} categories`;
    } else {
      const el = document.getElementById('dash-memory');
      const sub = document.getElementById('dash-memory-sub');
      if (dashNeedsFallback(el)) el.textContent = 'Unavailable';
      if (dashNeedsFallback(sub)) sub.textContent = 'No data';
    }
    if (parentalDevices && Array.isArray(parentalDevices.devices)) {
      const devices = parentalDevices.devices;
      const online = devices.filter(d => d.statusCode === 'green').length;
      const el = document.getElementById('dash-parental');
      const sub = document.getElementById('dash-parental-sub');
      if (el) el.textContent = `${devices.length} devices`;
      if (sub) sub.textContent = `${online} online`;
    } else {
      const el = document.getElementById('dash-parental');
      const sub = document.getElementById('dash-parental-sub');
      if (dashNeedsFallback(el)) el.textContent = 'Unavailable';
      if (dashNeedsFallback(sub)) sub.textContent = 'No data';
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

// ── Configuration section moved to static/admin-config.js ───────────────────

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

// ── Music section moved to static/admin-music.js ─────────────────────────────

// ── Energy section moved to static/admin-energy.js ──────────────────────────

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

function renderSessionsSummary(sessions) {
  const items = sessions || [];
  const totalEl = document.getElementById('sessions-summary-total');
  const roomsEl = document.getElementById('sessions-summary-rooms');
  const messagesEl = document.getElementById('sessions-summary-messages');
  const hostsEl = document.getElementById('sessions-summary-hosts');
  const roomCount = new Set(items.map(s => s.room_id).filter(Boolean)).size;
  const hostCount = new Set(items.map(s => {
    const meta = s.metadata || {};
    return meta.host_label || meta.host || '';
  }).filter(Boolean)).size;
  const messageCount = items.reduce((sum, s) => sum + Number(s.message_count || 0), 0);
  if (totalEl) totalEl.textContent = `${items.length}`;
  if (roomsEl) roomsEl.textContent = `${roomCount}`;
  if (messagesEl) messagesEl.textContent = `${messageCount}`;
  if (hostsEl) hostsEl.textContent = `${hostCount}`;
}

async function loadSessions() {
  try {
    const d = await api('GET', '/admin/sessions');
    const tbody = document.getElementById('sessions-tbody');
    tbody.innerHTML = '';
    const count = d.active_sessions || 0;
    const sessions = d.sessions || [];
    renderSessionsSummary(sessions);
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
  } catch(e) {
    renderSessionsSummary([]);
    toast('Failed to load sessions: ' + e.message, 'err');
  }
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

function renderMemorySummary(items) {
  const totalEl = document.getElementById('memory-summary-total');
  const pinnedEl = document.getElementById('memory-summary-pinned');
  const categoriesEl = document.getElementById('memory-summary-categories');
  const updatedEl = document.getElementById('memory-summary-updated');
  const memories = items || [];
  const pinnedCount = memories.filter(m => m.pinned).length;
  const categories = new Set(memories.map(m => (m.category || 'general').trim()).filter(Boolean));
  const lastUpdated = memories
    .map(m => m.updated_ts || m.created_ts)
    .filter(Boolean)
    .sort((a, b) => new Date(b).getTime() - new Date(a).getTime())[0];
  if (totalEl) totalEl.textContent = `${memories.length}`;
  if (pinnedEl) pinnedEl.textContent = `${pinnedCount}`;
  if (categoriesEl) categoriesEl.textContent = `${categories.size}`;
  if (updatedEl) updatedEl.textContent = memories.length ? _fmtMemoryTs(lastUpdated) : 'No memories yet';
}

async function loadMemory() {
  const tbody = document.getElementById('memory-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="7" class="text-muted">Loading…</td></tr>';
  try {
    const d = await api('GET', '/admin/memory?n=200');
    const items = d.memories || [];
    renderMemorySummary(items);
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
          <button class="btn btn-outline" onclick="markMemoryStale(${m.id})">Stale</button>
          <button class="btn btn-outline" onclick="deleteMemory(${m.id})">Delete</button>
        </div></td>
      </tr>
    `).join('');
  } catch(e) {
    renderMemorySummary([]);
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


async function loadStaleMemories() {
  const el = document.getElementById('memory-stale-list');
  const countEl = document.getElementById('memory-summary-stale');
  if (!el) return;
  el.innerHTML = '<tr><td colspan="5" class="text-muted">Loading…</td></tr>';
  try {
    const d = await api('GET', '/admin/memory/stale');
    const items = d.memories || [];
    if (countEl) countEl.textContent = String(items.length);
    if (!items.length) {
      el.innerHTML = '<tr><td colspan="5" class="text-muted" style="padding:12px 0;">No stale memories — everything is current.</td></tr>';
      return;
    }
    el.innerHTML = items.map(m => `
      <tr>
        <td><div style="font-weight:500;color:var(--text1);white-space:pre-wrap;">${_escapeHtml(m.summary)}</div></td>
        <td><span class="badge">${_escapeHtml(m.category || 'general')}</span></td>
        <td style="color:var(--text2);font-size:12px;">${_fmtMemoryTs(m.updated_ts || m.created_ts)}</td>
        <td style="font-size:12px;color:var(--text3);">${m.expires_ts ? _fmtMemoryTs(m.expires_ts) : '—'}</td>
        <td><div style="display:flex;gap:6px;white-space:nowrap;">
          <button class="btn btn-outline" onclick="restoreMemory(${m.id})">Restore</button>
          <button class="btn btn-outline" onclick="deleteMemory(${m.id})">Delete</button>
        </div></td>
      </tr>
    `).join('');
  } catch(e) {
    el.innerHTML = '<tr><td colspan="5" style="color:#fca5a5;">Failed to load stale memories.</td></tr>';
  }
}

async function markMemoryStale(id) {
  if (!confirm('Mark this memory as stale? It will be hidden from Nova but kept for review.')) return;
  try {
    await api('POST', '/admin/memory/' + id + '/mark-stale');
    await loadMemory();
    await loadStaleMemories();
    toast('Memory marked stale', 'ok');
  } catch(e) {
    toast('Failed: ' + e.message, 'err');
  }
}

async function restoreMemory(id) {
  try {
    await api('POST', '/admin/memory/' + id + '/restore');
    await loadMemory();
    await loadStaleMemories();
    toast('Memory restored', 'ok');
  } catch(e) {
    toast('Failed: ' + e.message, 'err');
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

// ── Test announce moved to static/admin-tools.js ─────────────────────────────

// ── Users ─────────────────────────────────────────────────────────────────────

async function loadUsers() {
  try {
    const d = await api('GET', '/admin/users');
    const container = document.getElementById('users-list');
    const users = d.users || [];
    const adminCount = users.filter(u => u.role === 'admin').length;
    const viewerCount = users.filter(u => u.role === 'viewer').length;
    const totalEl = document.getElementById('users-summary-total');
    const adminEl = document.getElementById('users-summary-admins');
    const viewerEl = document.getElementById('users-summary-viewers');
    if (totalEl) totalEl.textContent = `${users.length}`;
    if (adminEl) adminEl.textContent = `${adminCount}`;
    if (viewerEl) viewerEl.textContent = `${viewerCount}`;
    if (!users.length) {
      container.innerHTML = '<p style="padding:16px 0;color:var(--text3);font-size:13px;">No users yet.</p>';
      return;
    }
    container.innerHTML = users.map(u => `
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

function renderDecisionSummary(statusText = null) {
  const totalEl = document.getElementById('dec-summary-total');
  const announcesEl = document.getElementById('dec-summary-announces');
  const toolsEl = document.getElementById('dec-summary-tools');
  const streamEl = document.getElementById('dec-summary-stream');
  const entries = _decEntries || [];
  const announceCount = entries.filter(e => e.kind === 'triage_announce' || e.kind === 'motion_announce' || e.kind === 'weather_announce').length;
  const toolCount = entries.filter(e => e.kind === 'tool_call').length;
  const liveText = statusText ?? document.getElementById('dec-status')?.textContent ?? 'Idle';
  if (totalEl) totalEl.textContent = `${entries.length}`;
  if (announcesEl) announcesEl.textContent = `${announceCount}`;
  if (toolsEl) toolsEl.textContent = `${toolCount}`;
  if (streamEl) streamEl.textContent = liveText || 'Idle';
}

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
  renderDecisionSummary();
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
  renderDecisionSummary('Log cleared');
  const log = document.getElementById('dec-log');
  if (log) log.innerHTML = '<div style="padding:12px 18px;color:var(--text3);">Log cleared.</div>';
}

function startDecisionStream() {
  if (_decES) { _decES.close(); _decES = null; }
  _decEntries = [];
  renderDecisionSummary('Connecting…');
  const decLog = document.getElementById('dec-log');
  if (decLog) decLog.innerHTML = '<div style="padding:12px 18px;color:var(--text3);">Loading…</div>';
  const status = document.getElementById('dec-status');
  if (status) {
    status.textContent = 'Connecting…';
    renderDecisionSummary(status.textContent);
  }
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
      renderDecisionSummary(status?.textContent || `${_decEntries.length} loaded`);
    })
    .catch(() => {});
  let _sseDecBacklogDone = false;
  _decES = new EventSource('/admin/decisions/stream');
  _decES.onopen = () => {
    if (status) status.textContent = '● Live';
    renderDecisionSummary(status?.textContent || '● Live');
    setTimeout(() => { _sseDecBacklogDone = true; }, 1500);
  };
  _decES.onmessage = (ev) => {
    if (!_sseDecBacklogDone) return;
    try { const e = JSON.parse(ev.data); if (e && e.kind) _appendDecEntry(e); } catch {}
  };
  _decES.onerror = () => {
    if (status) status.textContent = '⚠ Retrying…';
    renderDecisionSummary(status?.textContent || '⚠ Retrying…');
  };
}


// ── Parental section moved to static/admin-parental.js ───────────────────────

window.registerAdminSection?.('decisions', {
  onEnter() {
    startDecisionStream();
  },
  onLeave() {
    if (_decES) {
      _decES.close();
      _decES = null;
    }
  },
});

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

window.registerAdminSection?.('costs', {
  onEnter() {
    startCostStream();
    loadCostHistory(_costPeriod);
  },
  onLeave() {
    if (_costES) {
      _costES.close();
      _costES = null;
    }
  },
});


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

  // ── Server Logs (pylog) ───────────────────────────────────────────
  let _pylogES = null, _pylogAll = [], _pylogLevel = '', _pylogSearch = '';
  const _LEVEL_COL = {debug:'#475569',info:'#94a3b8',warning:'#fbbf24',error:'#f87171',critical:'#f97316'};

  function renderPylogSummary(statusText = null) {
    const totalEl = document.getElementById('pylog-summary-total');
    const warningsEl = document.getElementById('pylog-summary-warnings');
    const errorsEl = document.getElementById('pylog-summary-errors');
    const streamEl = document.getElementById('pylog-summary-stream');
    const entries = _pylogAll || [];
    const warnings = entries.filter(e => e.level === 'warning').length;
    const errors = entries.filter(e => e.level === 'error' || e.level === 'critical').length;
    const liveText = statusText ?? document.getElementById('pylog-status')?.textContent ?? 'idle';
    if (totalEl) totalEl.textContent = `${entries.length}`;
    if (warningsEl) warningsEl.textContent = `${warnings}`;
    if (errorsEl) errorsEl.textContent = `${errors}`;
    if (streamEl) streamEl.textContent = liveText || 'idle';
  }

  function initPylog() {
    if (_pylogES) return;
    fetch('/admin/pylog?n=500').then(r=>r.json()).then(d=>{
      _pylogAll = d.entries||[];
      renderPylogSummary();
      _renderPylog();
    }).catch(()=>{});
    _pylogES = new EventSource('/admin/pylog/stream');
    _pylogES.onopen  = ()=>{
      document.getElementById('pylog-status').textContent='live';
      renderPylogSummary('live');
    };
    _pylogES.onerror = ()=>{
      document.getElementById('pylog-status').textContent='disconnected';
      renderPylogSummary('disconnected');
    };
    _pylogES.onmessage = e => {
      try {
        const entry = JSON.parse(e.data);
        _pylogAll.push(entry);
        if (_pylogAll.length > 2000) _pylogAll.shift();
        renderPylogSummary();
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
  function clearPylog() {
    _pylogAll=[];
    renderPylogSummary('cleared');
    document.getElementById('pylog-output').innerHTML='<div style="padding:12px 18px;color:var(--text3);">Cleared.</div>';
  }
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


// ── Avatar section moved to static/admin-avatar.js ──────────────────────────

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

// ── Tools section moved to static/admin-tools.js ─────────────────────────────

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




// ── Chat section moved to static/admin-chat.js ──────────────────────────────

// ── Faces section moved to static/admin-faces.js ────────────────────────────

// ── Event Delegation (replaces inline onclick handlers) ──────────────────────

document.getElementById('nav')?.addEventListener('click', e => {
  const btn = e.target.closest('.nav-item[data-section]');
  if (btn) navigate(btn);
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

// -- Server Logs --
document.getElementById('pylog-search')?.addEventListener('input', () => filterPylog());
document.getElementById('btn-clear-pylog')?.addEventListener('click', () => clearPylog());

// -- Memory --
document.getElementById('memory-save-btn')?.addEventListener('click', () => saveMemory());
document.getElementById('btn-load-memory')?.addEventListener('click', () => { loadMemory(); loadStaleMemories(); });
document.getElementById('btn-clear-memory-form')?.addEventListener('click', () => clearMemoryForm());
document.getElementById('btn-clear-all-memory')?.addEventListener('click', () => clearAllMemory());

// -- AI Decisions --
document.getElementById('btn-clear-decisions')?.addEventListener('click', () => clearDecisions());

// -- LLM Cost --
document.getElementById('btn-clear-costs')?.addEventListener('click', () => clearCosts());

// -- Tools moved to static/admin-tools.js --

// ══════════════════════════════════════════════════════════════════
// Scoreboard moved to static/admin-scoreboard.js
// ══════════════════════════════════════════════════════════════════

// -- Users --
document.getElementById('btn-create-user')?.addEventListener('click', () => createUser());
document.getElementById('btn-submit-pw-change')?.addEventListener('click', () => submitPasswordChange());
document.getElementById('btn-cancel-pw-change')?.addEventListener('click', () => cancelPasswordChange());
