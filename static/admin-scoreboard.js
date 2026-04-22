(() => {
  'use strict';

  let _sbConfig = {};

  function _setSbToggleState(on) {
    const track = document.getElementById('sb-widget-toggle');
    const knob = document.getElementById('sb-widget-knob');
    if (!track || !knob) return;
    track.style.background = on ? 'var(--accent)' : 'var(--border)';
    knob.style.transform = on ? 'translateX(16px)' : 'translateX(0)';
  }

  function _renderSbLeaderboard(weekly) {
    const el = document.getElementById('sb-leaderboard');
    if (!el) return;
    if (!weekly.length) {
      el.innerHTML = '<span style="color:var(--muted)">No scores yet this week.</span>';
      return;
    }
    const medals = ['🥇', '🥈', '🥉'];
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
        <div style="font-weight:700;font-size:15px;margin:4px 0;">${s.person.charAt(0).toUpperCase() + s.person.slice(1)}</div>
        <div style="font-size:20px;font-weight:700;color:var(--accent);">${s.points} pts</div>
        <div style="font-size:11px;color:var(--muted);">${s.tasks} tasks</div>
      </div>`;
    }).join('');
  }

  function _renderSbRecent(recent) {
    const el = document.getElementById('sb-recent');
    if (!el) return;
    if (!recent.length) {
      el.innerHTML = '<span style="color:var(--muted)">No recent activity.</span>';
      return;
    }
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
    const memberOptions = members.map(m => `<option value="${m}">${m.charAt(0).toUpperCase() + m.slice(1)}</option>`).join('');
    const personSel = document.getElementById('sb-award-person');
    if (personSel) personSel.innerHTML = memberOptions;
    const deductPersonSel = document.getElementById('sb-deduct-person');
    if (deductPersonSel) deductPersonSel.innerHTML = memberOptions;

    const assignDiv = document.getElementById('sb-new-assigned');
    if (assignDiv) {
      assignDiv.innerHTML = members.map(m => `
        <label style="display:flex;align-items:center;gap:4px;font-size:12px;">
          <input type="checkbox" name="sb-assign" value="${m}"> ${m.charAt(0).toUpperCase() + m.slice(1)}
        </label>`).join('');
    }
  }

  function _renderSbTasks(cfg) {
    const el = document.getElementById('sb-tasks-list');
    if (!el) return;
    const members = cfg.members || [];
    const tasks = cfg.tasks || [];
    if (!tasks.length) {
      el.innerHTML = '<span style="color:var(--muted)">No tasks configured.</span>';
      return;
    }
    el.innerHTML = '<table style="width:100%;border-collapse:collapse;">' +
      '<thead><tr style="color:var(--muted);font-size:12px;text-align:left;">' +
      '<th style="padding:4px 8px;">Task</th><th style="padding:4px 8px;">Pts</th>' +
      '<th style="padding:4px 8px;">Verify</th><th style="padding:4px 8px;">Assigned To</th><th style="padding:4px 8px;"></th>' +
      '</tr></thead><tbody>' +
      tasks.map(t => {
        const assignCheckboxes = members.map(m => `
          <label style="display:flex;align-items:center;gap:3px;font-size:11px;white-space:nowrap;">
            <input type="checkbox" onchange="sbToggleAssign('${t.id}','${m}',this.checked)" ${(t.assigned_to || []).includes(m) ? 'checked' : ''}>
            ${m.charAt(0).toUpperCase() + m.slice(1)}
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

  async function loadSbNotifications() {
    try {
      const d = await window.adminApi.scoreboard.getNotifications();
      const el = document.getElementById('sb-blind-names');
      if (el) el.value = d.blind_reminder_names || '';
    } catch (e) {
      console.warn('loadSbNotifications', e);
    }
  }

  async function loadPenalties() {
    try {
      const d = await window.adminApi.scoreboard.getPenalties();
      const penalties = d.penalties || [];
      const sel = document.getElementById('sb-deduct-penalty');
      if (sel) sel.innerHTML = penalties.map(p => `<option value="${p.id}">${p.label} (-${p.points}pts)</option>`).join('');
      const list = document.getElementById('sb-penalties-list');
      if (!list) return;
      if (!penalties.length) {
        list.innerHTML = '<span style="color:var(--muted);font-size:13px;">No penalty types configured.</span>';
        return;
      }
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
    } catch (e) {
      console.warn('loadPenalties', e);
    }
  }

  async function loadScoreboard() {
    loadSbNotifications();
    try {
      const d = await window.adminApi.scoreboard.getOverview();
      _sbConfig = d.config || {};
      _renderSbLeaderboard(d.weekly || []);
      _renderSbRecent(d.recent || []);
      _populateSbSelects(_sbConfig);
      _renderSbTasks(_sbConfig);
      _setSbToggleState(_sbConfig.show_widget !== false);
      loadPenalties();
    } catch (e) {
      console.error('loadScoreboard', e);
    }
  }

  async function sbToggleWidget() {
    const on = _sbConfig.show_widget !== false;
    const next = !on;
    try {
      await window.adminApi.scoreboard.setWidgetVisibility(next);
      _sbConfig.show_widget = next;
      _setSbToggleState(next);
    } catch (e) {
      toast('Failed to update', 'err');
    }
  }

  async function sbToggleAssign(taskId, member, checked) {
    const task = (_sbConfig.tasks || []).find(t => t.id === taskId);
    if (!task) return;
    const assigned = task.assigned_to || [];
    if (checked && !assigned.includes(member)) assigned.push(member);
    task.assigned_to = checked ? assigned : assigned.filter(m => m !== member);
    try {
      await window.adminApi.scoreboard.patchTaskAssignment(taskId, task.assigned_to);
    } catch (e) {
      toast('Failed to update assignment', 'err');
    }
  }

  async function sbDeleteTask(taskId) {
    if (!confirm(`Delete task "${taskId}"? This cannot be undone.`)) return;
    try {
      const r = await window.adminApi.scoreboard.deleteTask(taskId);
      if (r.ok) {
        toast('Task deleted', 'ok');
        loadScoreboard();
      } else {
        toast(r.error || 'Error', 'err');
      }
    } catch (_) {
      toast('Error', 'err');
    }
  }

  async function sbAddTask() {
    const id = document.getElementById('sb-new-id')?.value.trim();
    const label = document.getElementById('sb-new-label')?.value.trim();
    const points = parseInt(document.getElementById('sb-new-points')?.value || '5', 10);
    const cooldown = parseInt(document.getElementById('sb-new-cooldown')?.value || '16', 10);
    const verification = document.getElementById('sb-new-verification')?.value || 'honour';
    const keywords = (document.getElementById('sb-new-keywords')?.value || '').split(',').map(s => s.trim()).filter(Boolean);
    const assigned_to = [...document.querySelectorAll('input[name="sb-assign"]:checked')].map(cb => cb.value);
    const msg = document.getElementById('sb-add-task-msg');
    if (!id || !label) {
      if (msg) msg.textContent = 'ID and Label are required.';
      return;
    }
    try {
      const r = await window.adminApi.scoreboard.createTask({ id, label, points, cooldown_hours: cooldown, verification, keywords, assigned_to });
      if (r.ok) {
        if (msg) msg.textContent = 'Task added!';
        loadScoreboard();
        ['sb-new-id', 'sb-new-label', 'sb-new-keywords'].forEach(fieldId => {
          const el = document.getElementById(fieldId);
          if (el) el.value = '';
        });
      } else if (msg) {
        msg.textContent = r.error || 'Error';
      }
    } catch (e) {
      if (msg) msg.textContent = 'Error: ' + e.message;
    }
  }

  async function sbDeleteLog(id) {
    if (!confirm('Delete this log entry?')) return;
    try {
      await window.adminApi.scoreboard.deleteLog(id);
      loadScoreboard();
    } catch (_) {
      toast('Failed to delete', 'err');
    }
  }

  async function sbAward() {
    const person = document.getElementById('sb-award-person')?.value || '';
    const taskId = document.getElementById('sb-award-task')?.value || '';
    const msg = document.getElementById('sb-award-msg');
    if (!person || !taskId) {
      if (msg) msg.textContent = 'Select person and task.';
      return;
    }
    try {
      const r = await window.adminApi.scoreboard.awardTask(person, taskId);
      if (msg) msg.textContent = r.ok ? 'Points awarded!' : (r.error || 'Error');
      loadScoreboard();
    } catch (e) {
      if (msg) msg.textContent = 'Error: ' + e.message;
    }
  }

  async function sbLoadLogs() {
    const days = document.getElementById('sb-log-days')?.value || 7;
    const el = document.getElementById('sb-logs-table');
    try {
      const d = await window.adminApi.scoreboard.getLogs(days);
      const logs = d.logs || [];
      if (!logs.length) {
        if (el) el.innerHTML = '<span style="color:var(--muted)">No logs found.</span>';
        return;
      }
      if (el) {
        el.innerHTML = '<table style="width:100%;border-collapse:collapse;">' +
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
      }
    } catch (_) {
      if (el) el.innerHTML = 'Error loading logs.';
    }
  }

  async function sbSaveNotifications() {
    const names = (document.getElementById('sb-blind-names')?.value || '').trim();
    if (!names) return;
    try {
      await window.adminApi.scoreboard.saveNotifications(names);
      toast('Blind reminder names saved.', 'ok');
    } catch (e) {
      toast('Save failed: ' + e.message, 'err');
    }
  }

  async function sbIssueDeduction() {
    const person = document.getElementById('sb-deduct-person')?.value;
    const penaltyId = document.getElementById('sb-deduct-penalty')?.value;
    if (!person || !penaltyId) {
      toast('Select a member and penalty', 'err');
      return;
    }
    try {
      const r = await window.adminApi.scoreboard.issuePenalty(person, penaltyId);
      toast(`-${r.deducted}pts deducted from ${person} for ${r.label}`, 'ok');
      loadScoreboard();
    } catch (e) {
      toast('Deduction failed: ' + e.message, 'err');
    }
  }

  async function sbAddPenalty() {
    const id = (document.getElementById('sb-new-penalty-id')?.value || '').trim();
    const label = (document.getElementById('sb-new-penalty-label')?.value || '').trim();
    const points = parseInt(document.getElementById('sb-new-penalty-points')?.value || '10', 10);
    if (!id || !label) {
      toast('ID and label required', 'err');
      return;
    }
    try {
      await window.adminApi.scoreboard.createPenalty({ id, label, points });
      document.getElementById('sb-new-penalty-id').value = '';
      document.getElementById('sb-new-penalty-label').value = '';
      document.getElementById('sb-new-penalty-points').value = '10';
      toast('Penalty type added', 'ok');
      loadPenalties();
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function sbDeletePenalty(penaltyId) {
    if (!confirm('Remove this penalty type?')) return;
    try {
      await window.adminApi.scoreboard.deletePenalty(penaltyId);
      toast('Penalty removed', 'ok');
      loadPenalties();
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  document.getElementById('btn-sb-refresh')?.addEventListener('click', () => loadScoreboard());
  document.getElementById('btn-sb-award')?.addEventListener('click', () => sbAward());
  document.getElementById('btn-sb-logs')?.addEventListener('click', () => sbLoadLogs());
  document.getElementById('btn-sb-add-task')?.addEventListener('click', () => sbAddTask());

  window.registerAdminSection('scoreboard', {
    onEnter() {
      return loadScoreboard();
    },
  });

  Object.assign(window, {
    loadScoreboard,
    sbToggleWidget,
    sbToggleAssign,
    sbDeleteTask,
    sbAddTask,
    sbDeleteLog,
    sbAward,
    sbLoadLogs,
    sbSaveNotifications,
    sbIssueDeduction,
    sbAddPenalty,
    sbDeletePenalty,
  });
})();
