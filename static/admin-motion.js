'use strict';

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


// ── Self-Heal section moved to static/admin-selfheal.js ─────────────────────

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

'use strict';

(() => {
  function bindMotionEvents() {
    document.getElementById('fa-search-input')?.addEventListener('keydown', e => {
      if (e.key === 'Enter') window.faSearch?.();
    });
    document.getElementById('fa-search-input')?.addEventListener('input', () => window.faSearchDebounced?.());

    document.querySelectorAll('.fa-filter-header').forEach(el => {
      el.addEventListener('click', () => el.parentElement.classList.toggle('collapsed'));
    });

    document.querySelectorAll('#fa-filter-cameras input[type=checkbox], #fa-filter-events input[type=checkbox]').forEach(cb => {
      cb.addEventListener('change', () => window.faApplyFilters?.());
    });
    document.getElementById('fa-flagged-only')?.addEventListener('change', () => window.faApplyFilters?.());
    document.getElementById('fa-date-input')?.addEventListener('change', () => window.faApplyFilters?.(true));
    document.getElementById('fa-date-from')?.addEventListener('change', () => window.faApplyFilters?.(true));
    document.getElementById('fa-date-to')?.addEventListener('change', () => window.faApplyFilters?.(true));

    document.getElementById('fa-bulk-toggle')?.addEventListener('change', function() {
      window.faToggleBulkMode?.(this.checked);
    });
    document.getElementById('btn-fa-bulk-select-all')?.addEventListener('click', () => window.faBulkSelectAll?.());
    document.getElementById('btn-fa-bulk-delete')?.addEventListener('click', () => window.faBulkDelete?.());
    document.getElementById('btn-fa-bulk-clear')?.addEventListener('click', () => window.faBulkClearSelection?.());
    document.getElementById('btn-fa-bulk-delete-all')?.addEventListener('click', () => window.faBulkDeleteAll?.());

    document.getElementById('btn-fa-mobile-toggle')?.addEventListener('click', () => {
      document.getElementById('fa-sidebar')?.classList.toggle('open');
    });
    document.getElementById('fa-sidebar-overlay')?.addEventListener('click', () => {
      document.getElementById('fa-sidebar')?.classList.remove('open');
    });

    document.getElementById('fa-player-timeline-link')?.addEventListener('click', () => window.faGoToTimeline?.());
    document.getElementById('fa-player-flag-btn')?.addEventListener('click', () => window.faToggleFlag?.());
    document.getElementById('btn-fa-download')?.addEventListener('click', () => window.faDownloadClip?.());
    document.getElementById('btn-fa-delete')?.addEventListener('click', () => window.faDeleteClip?.());
    document.getElementById('btn-fa-collapse')?.addEventListener('click', () => window.faCollapsePlayer?.());
  }

  bindMotionEvents();

  window.registerAdminSection?.('motion', {
    onEnter() {
      return window.faInit?.();
    },
    onLeave() {
      document.getElementById('fa-sidebar')?.classList.remove('open');
    },
  });
})();
