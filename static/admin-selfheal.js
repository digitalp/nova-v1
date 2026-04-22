'use strict';

(() => {
  const selfhealApi = window.adminApi?.selfheal;
  if (!selfhealApi) return;

  const SH_TYPE_LABELS = {
    error_detected: '🔴 Error detected',
    deduplicated: '⏭ Deduplicated',
    no_source_file: '⚠️ No source file',
    claude_failed: '💔 Claude failed',
    fix_proposed: '🔧 Fix proposed',
    analysis_only: '🔍 Analysis only',
    approved: '✅ Approved',
    rejected: '❌ Rejected',
    auto_rejected: '⏱ Auto-rejected',
    apply_ok: '✅ Patch applied',
    apply_failed: '💥 Apply failed',
    restart_ok: '🔄 Restarted',
    restart_failed: '💥 Restart failed',
  };

  function shEsc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function shFmtUptime(s) {
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  }

  async function shClearAllEvents() {
    if (!confirm('Delete ALL self-heal events? This cannot be undone.')) return;
    try {
      await selfhealApi.clearEvents();
      toast('All events cleared');
      loadSelfHeal();
    } catch (e) {
      toast('Failed: ' + e.message);
    }
  }

  async function loadSelfHeal() {
    try {
      const st = await selfhealApi.getStatus();
      document.getElementById('sh-stat-status').innerHTML = '<span style="color:var(--green,#34c759)">&#9679; Running</span>';
      document.getElementById('sh-stat-uptime').textContent = shFmtUptime(st.uptime_seconds);
      document.getElementById('sh-stat-errors').textContent = st.errors_detected ?? '—';
      document.getElementById('sh-stat-applied').textContent = st.patches_applied ?? '—';
      document.getElementById('sh-stat-rejected').textContent = st.fixes_rejected ?? '—';
      document.getElementById('sh-stat-pending').textContent = st.pending_count ?? '—';
    } catch {
      document.getElementById('sh-stat-status').innerHTML = '<span style="color:var(--red,#ff3b30)">&#9679; Offline</span>';
      ['sh-stat-uptime', 'sh-stat-errors', 'sh-stat-applied', 'sh-stat-rejected', 'sh-stat-pending']
        .forEach(id => { document.getElementById(id).textContent = '—'; });
    }

    try {
      const pending = await selfhealApi.getPending();
      const list = document.getElementById('sh-pending-list');
      const badge = document.getElementById('sh-pending-badge');
      if (badge) badge.textContent = pending.length ? `(${pending.length})` : '(0)';
      if (!pending.length) {
        list.innerHTML = '<div class="text-sm text-muted">No pending fixes.</div>';
      } else {
        list.innerHTML = pending.map(f => {
          const diffHtml = f.has_diff
            ? '<pre style="background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:10px;font-size:11px;overflow-x:auto;max-height:200px;white-space:pre;">' + shEsc(f.diff) + '</pre>'
            : '';
          return '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:12px;">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
            + '<div style="font-weight:600;font-size:13px;">🔴 ' + shEsc(f.event) + ' <span style="color:var(--text3);font-weight:400;">— ' + shEsc(f.exc_type) + '</span></div>'
            + '<div style="font-size:11px;color:var(--text3);">' + Math.floor(f.age_seconds / 60) + 'm ago</div>'
            + '</div>'
            + '<div style="font-size:12px;color:var(--text2);margin-bottom:4px;">📄 ' + shEsc(f.source_file) + '</div>'
            + '<div style="font-size:12px;margin-bottom:10px;">' + shEsc(f.summary) + '</div>'
            + diffHtml
            + '<div style="display:flex;gap:8px;margin-top:10px;">'
            + '<button class="btn btn-primary" style="font-size:12px;" onclick="shApprove(\'' + f.fix_id + '\')">✅ Approve</button>'
            + '<button class="btn btn-outline" style="font-size:12px;" onclick="shReject(\'' + f.fix_id + '\')">❌ Reject</button>'
            + '</div></div>';
        }).join('');
      }
    } catch {
      document.getElementById('sh-pending-list').innerHTML = '<div class="text-sm text-muted">Could not load pending fixes.</div>';
    }

    try {
      const events = await selfhealApi.getEvents(100);
      const tbody = document.getElementById('sh-events-tbody');
      if (!events.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-muted">No events yet.</td></tr>';
      } else {
        tbody.innerHTML = events.map(e => {
          const label = SH_TYPE_LABELS[e.event_type] || e.event_type;
          const time = e.ts_iso ? new Date(e.ts_iso).toLocaleString() : '—';
          const detail = [e.summary, e.message].filter(Boolean).join(' ').slice(0, 120);
          return '<tr>'
            + '<td style="white-space:nowrap;color:var(--text3)">' + time + '</td>'
            + '<td>' + label + '</td>'
            + '<td style="font-size:11px;color:var(--text2)">' + shEsc(e.log_event || e.service || '') + '</td>'
            + '<td style="font-size:11px;color:var(--text2)">' + shEsc(detail) + '</td>'
            + '</tr>';
        }).join('');
      }
    } catch {
      document.getElementById('sh-events-tbody').innerHTML = '<tr><td colspan="4" class="text-muted">Could not load events.</td></tr>';
    }

    try {
      const cfg = await selfhealApi.getConfig();
      const at = document.getElementById('sh-cfg-approval-timeout');
      const dw = document.getElementById('sh-cfg-dedup-window');
      const ct = document.getElementById('sh-cfg-claude-timeout');
      const ll = document.getElementById('sh-cfg-log-level');
      const om = document.getElementById('sh-cfg-openai-model');
      if (at && cfg.approval_timeout_seconds) at.value = cfg.approval_timeout_seconds;
      if (dw && cfg.dedup_window_seconds) dw.value = cfg.dedup_window_seconds;
      if (ct && cfg.claude_timeout_seconds) ct.value = cfg.claude_timeout_seconds;
      if (ll && cfg.log_level) ll.value = cfg.log_level;
      if (om && cfg.openai_model) om.value = cfg.openai_model;
    } catch {}
  }

  async function shTestInject() {
    try {
      const d = await selfhealApi.injectTest();
      toast(d.message || 'Test error injected.');
      setTimeout(loadSelfHeal, 8000);
    } catch (e) {
      toast('Test failed: ' + e.message, 'err');
    }
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
    if (apiKey) payload.anthropic_api_key = apiKey;
    if (openaiKey) payload.openai_api_key = openaiKey;
    if (openaiModel) payload.openai_model = openaiModel;
    if (approvalTimeout) payload.approval_timeout_seconds = parseInt(approvalTimeout, 10);
    if (dedupWindow) payload.dedup_window_seconds = parseInt(dedupWindow, 10);
    if (claudeTimeout) payload.claude_timeout_seconds = parseInt(claudeTimeout, 10);
    if (logLevel) payload.log_level = logLevel;

    if (!Object.keys(payload).length) {
      toast('No changes to save.', 'err');
      return;
    }

    const statusEl = document.getElementById('sh-cfg-status');
    statusEl.textContent = 'Saving…';
    try {
      await selfhealApi.saveConfig(payload);
      statusEl.textContent = 'Saved. Restarting service…';
      document.getElementById('sh-cfg-api-key').value = '';
      document.getElementById('sh-cfg-openai-key').value = '';
      try {
        await selfhealApi.restart();
      } catch {}
      setTimeout(() => {
        statusEl.textContent = '';
        loadSelfHeal();
      }, 3000);
      toast('Configuration saved.');
    } catch (e) {
      statusEl.textContent = 'Error: ' + e.message;
      toast('Save failed: ' + e.message, 'err');
    }
  }

  function shToggleKeyVisibility(id, btn) {
    const input = document.getElementById(id || 'sh-cfg-api-key');
    if (!btn) btn = input?.nextElementSibling;
    if (!input || !btn) return;
    if (input.type === 'password') {
      input.type = 'text';
      btn.textContent = 'Hide';
    } else {
      input.type = 'password';
      btn.textContent = 'Show';
    }
  }

  async function shApprove(fixId) {
    try {
      await selfhealApi.approve(fixId);
      toast('Fix approved — applying patch…');
      setTimeout(loadSelfHeal, 1500);
    } catch (e) {
      toast('Approve failed: ' + e.message, 'err');
    }
  }

  async function shReject(fixId) {
    try {
      await selfhealApi.reject(fixId);
      toast('Fix rejected.');
      setTimeout(loadSelfHeal, 800);
    } catch (e) {
      toast('Reject failed: ' + e.message, 'err');
    }
  }

  async function shBulkRejectAll() {
    if (!confirm('Reject ALL pending fixes?')) return;
    try {
      const pending = await selfhealApi.getPending();
      const ids = (pending || []).map(f => f.fix_id).filter(Boolean);
      if (!ids.length) {
        toast('No pending fixes to reject.');
        return;
      }
      await selfhealApi.bulkReject(ids);
      toast(ids.length + ' pending fixes rejected.');
      setTimeout(loadSelfHeal, 800);
    } catch (e) {
      toast('Bulk reject failed: ' + e.message, 'err');
    }
  }

  document.getElementById('btn-refresh-selfheal')?.addEventListener('click', () => loadSelfHeal());
  document.getElementById('btn-sh-clear-all')?.addEventListener('click', () => shClearAllEvents());
  document.getElementById('btn-sh-test-inject')?.addEventListener('click', () => shTestInject());
  document.getElementById('sh-pending-toggle')?.addEventListener('click', () => {
    window._toggleCollapsibleSection?.('sh-pending-body', 'sh-pending-chevron');
  });
  document.getElementById('btn-sh-bulk-reject')?.addEventListener('click', () => shBulkRejectAll());
  document.getElementById('btn-sh-toggle-api-key')?.addEventListener('click', function() {
    shToggleKeyVisibility('sh-cfg-api-key', this);
  });
  document.getElementById('btn-sh-toggle-openai-key')?.addEventListener('click', function() {
    shToggleKeyVisibility('sh-cfg-openai-key', this);
  });
  document.getElementById('btn-sh-save-config')?.addEventListener('click', () => shSaveConfig());

  window.registerAdminSection?.('selfheal', {
    onEnter() {
      loadSelfHeal();
    },
  });

  Object.assign(window, {
    loadSelfHeal,
    shClearAllEvents,
    shTestInject,
    shSaveConfig,
    shToggleKeyVisibility,
    shApprove,
    shReject,
    shBulkRejectAll,
  });
})();
