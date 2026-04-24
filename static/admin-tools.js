(() => {
  'use strict';

  async function testAnnounce() {
    const message = document.getElementById('announce-msg').value.trim();
    const priority = document.getElementById('announce-priority').value;
    const targetArea = document.getElementById('announce-target-area')?.value || '';
    if (!message) {
      toast('Enter a message', 'warn');
      return;
    }
    try {
      await window.adminApi.tools.sendAnnouncementTest({ message, priority, target_area: targetArea });
      toast('Announcement sent', 'ok');
    } catch (e) {
      toast('Announce failed: ' + e.message, 'err');
    }
  }

  async function loadAnnouncementLog() {
    const el = document.getElementById('announce-log-list');
    if (!el) return;
    try {
      const d = await window.adminApi.tools.getAnnouncements(200);
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
    } catch (e) {
      el.innerHTML = `<div class="text-sm" style="color:var(--danger,#ff453a);padding:8px 0;">Failed to load announcement log: ${_esc(e.message || String(e))}</div>`;
    }
  }

  async function clearAnnouncementLog() {
    if (!confirm('Clear all announcement history?')) return;
    try {
      await window.adminApi.tools.clearAnnouncements();
      await loadAnnouncementLog();
      toast('Announcement log cleared');
    } catch (e) {
      alert('Failed to clear: ' + (e.message || e));
    }
  }

  async function loadHeatingShadow() {
    const el = document.getElementById('heating-shadow-list');
    if (!el) return;
    try {
      const d = await window.adminApi.tools.getHeatingShadowHistory(80);
      const entries = d.entries || [];
      if (!entries.length) {
        el.innerHTML = '<div class="text-sm text-muted" style="padding:8px 0;">No shadow evaluations yet. Shadow runs automatically every 30 min alongside the primary heating eval.</div>';
        return;
      }

      const runs = [];
      let current = null;
      for (const e of entries) {
        if (e.kind === 'heating_shadow_eval_start') {
          current = { start: e, tools: [], comparison: null };
          runs.push(current);
        } else if (current) {
          if (e.kind === 'heating_shadow_tool_call') current.tools.push(e);
          else if (e.kind === 'heating_shadow_comparison') {
            current.comparison = e;
            current = null;
          } else if (['heating_shadow_round_silent', 'heating_shadow_eval_error', 'heating_shadow_max_rounds'].includes(e.kind)) {
            current.endEvent = e;
          }
        }
      }

      const firstStart = entries.find(e => e.kind === 'heating_shadow_eval_start');
      if (firstStart?.llm_model) {
        const lbl = document.getElementById('shadow-model-label');
        if (lbl) lbl.textContent = firstStart.llm_model;
      }

      el.innerHTML = runs.slice().reverse().map(run => {
        const cmp = run.comparison;
        const agreement = cmp?.agreement || (run.endEvent?.kind === 'heating_shadow_eval_error' ? 'error' : 'pending');
        const agreeLabel = {
          both_silent: '<span class="shadow-agree">✓ Both silent</span>',
          both_acted: '<span class="shadow-agree">✓ Both acted</span>',
          shadow_only: '<span class="shadow-diverge">⚠ Shadow acted, primary silent</span>',
          primary_only: '<span class="shadow-diverge">⚠ Primary acted, shadow silent</span>',
          error: '<span style="color:var(--danger)">✗ Error</span>',
          pending: '<span class="shadow-silent">… pending</span>',
        }[agreement] || `<span>${_esc(agreement)}</span>`;

        const writes = run.tools.filter(t => t.is_write);
        const reads = run.tools.filter(t => !t.is_write);
        const season = run.start?.season || '—';
        const ts = run.start?.ts || '';
        const shadowOnly = run.start?.shadow_only ? ' <span style="color:#60a5fa;font-size:10px;">[manual]</span>' : '';

        const toolRows = run.tools.map(t => {
          const cls = t.is_write ? 'shadow-tool-write' : 'shadow-tool-read';
          const icon = t.is_write ? '✎' : '↳';
          const entity = t.args?.entity_id || '';
          const argsStr = Object.entries(t.args || {}).map(([k, v]) => `${k}=${v}`).join(', ');
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
            ${writes.length} write${writes.length !== 1 ? 's' : ''} intercepted · ${reads.length} read${reads.length !== 1 ? 's' : ''} executed · ${run.tools.length} total calls
          </div>
          ${entityDiff}
          ${toolRows ? `<details style="margin-top:4px;"><summary style="font-size:11px;cursor:pointer;color:var(--text3);">Show tool calls (${run.tools.length})</summary>${toolRows}</details>` : ''}
        </div>`;
      }).join('');
    } catch (e) {
      el.innerHTML = `<div class="text-sm" style="color:var(--danger);padding:8px 0;">Failed to load: ${_esc(e.message || String(e))}</div>`;
    }
  }

  async function forceHeatingShadow(scenario) {
    const btn = event?.target;
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Running…';
    }
    try {
      const d = await window.adminApi.tools.forceHeatingShadow(scenario);
      if (d.ok) {
        toast(`Shadow ${scenario} test done — ${d.write_calls_intercepted} writes intercepted, ${d.read_calls_executed} reads executed`);
        await loadHeatingShadow();
      } else {
        alert('Shadow test failed: ' + (d.message || 'unknown error'));
      }
    } catch (e) {
      alert('Error: ' + (e.message || e));
    } finally {
      if (btn) btn.textContent = scenario === 'winter' ? '▶ Run Winter Test' : '▶ Run Spring Test';
      if (btn) btn.disabled = false;
    }
  }

  async function loadConversationAudit() {
    const el = document.getElementById('audit-list');
    if (!el) return;
    el.innerHTML = '<div class="text-sm text-muted" style="padding:8px 0;">Loading…</div>';
    const sid = (document.getElementById('audit-session-filter')?.value || '').trim();
    try {
      const d = await window.adminApi.tools.getConversationAudit(sid);
      const items = d.conversations || [];
      if (!items.length) {
        el.innerHTML = '<div class="text-sm text-muted" style="padding:8px 0;">No conversations found.</div>';
        return;
      }
      el.innerHTML = items.map(a => {
        const tc = Array.isArray(a.tool_calls) ? a.tool_calls : [];
        const toolBadges = tc.map(t =>
          `<span class="motion-chip ${t.status === 'allowed' ? 'good' : 'coral-plate'}" style="font-size:10px;padding:2px 7px;">${_esc(t.name)}</span>`
        ).join(' ');
        const ts = (a.ts || '').replace('T', ' ').slice(0, 19);
        return `<div style="padding:10px 0;border-bottom:1px solid var(--border);">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px;">
            <span class="motion-chip" style="font-size:10px;padding:2px 7px;">${_esc(a.model || '?')}</span>
            <span class="text-xs text-muted">${_esc(ts)}</span>
            <span class="text-xs text-muted">${a.processing_ms || 0}ms</span>
            ${toolBadges}
          </div>
          <div class="text-sm" style="margin-bottom:2px;"><strong>User:</strong> ${_esc((a.user_text || '').slice(0, 200))}</div>
          <div class="text-sm text-muted">${_esc((a.final_reply || '').slice(0, 300))}</div>
        </div>`;
      }).join('');
    } catch (e) {
      el.innerHTML = `<div class="text-sm" style="color:var(--danger,#ff453a);padding:8px 0;">Failed: ${_esc(e.message || String(e))}</div>`;
    }
  }

  async function loadWakeStatus() {
    const el = document.getElementById('wake-status');
    if (!el) return;
    try {
      const d = await window.adminApi.tools.getWakeStatus();
      const stages = (d.pipeline_stages || []).join(' → ');
      const lines = [
        `<strong>Pipeline:</strong> ${stages || 'not initialized'}`,
        `Coral TPU: ${d.coral_available ? '✅ Edge TPU (~1ms)' : d.cpu_tflite_available ? '✅ CPU TFLite (~3-8ms)' : '❌ Not available'}`,
      ];
      if (d.cpu_tflite_available) lines.push('CPU TFLite: ✅ Active (~3-8ms)');
      if (d.numpy_model_available) lines.push('Numpy Classifier: ✅ Active (~3-5ms)');
      lines.push(`Verifier: ${d.verifier_available ? '✅ Active' : (d.verifier_model_exists ? '✅ Trained' : '⚠ Not trained')}`);
      lines.push(`VAD Gate: ${d.vad_available ? '✅ Active' : '❌ Not available'}`);
      lines.push('Whisper Fallback: ✅ Ready');
      lines.push(`Edge TPU Model: ${d.coral_model_exists ? '✅ Present' : '⚠ Not present'}`);
      lines.push(`Edge TPU Compiler: ${d.edgetpu_compiler_available ? '✅ Installed' : '⚠ Not installed (optional)'}`);
      const compilerBtn = document.getElementById('wake-install-compiler-btn');
      if (compilerBtn) compilerBtn.style.display = d.edgetpu_compiler_available ? 'none' : '';
      el.innerHTML = lines.join('<br>');
    } catch (_) {
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
      const d = await window.adminApi.tools.installEdgeTpuCompiler();
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
    if (!wakeWord) {
      toast('Enter a wake word first', 'err');
      return;
    }
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

  function enterToolsSection() {
    loadWakeStatus();
    loadAnnouncementLog();
    loadHeatingShadow();
  }

  document.getElementById('btn-test-announce')?.addEventListener('click', () => testAnnounce());
  document.getElementById('btn-clear-announce-log')?.addEventListener('click', () => clearAnnouncementLog());
  document.getElementById('btn-refresh-heating-shadow')?.addEventListener('click', () => loadHeatingShadow());
  document.getElementById('btn-force-heating-winter')?.addEventListener('click', () => forceHeatingShadow('winter'));
  document.getElementById('btn-force-heating-spring')?.addEventListener('click', () => forceHeatingShadow('spring'));
  document.getElementById('wake-train-btn')?.addEventListener('click', () => trainWakeWord());
  document.getElementById('wake-install-compiler-btn')?.addEventListener('click', () => installEdgeTPUCompiler());
  document.getElementById('announce-log-toggle')?.addEventListener('click', () => {
    _toggleCollapsibleSection('announce-log-body', 'announce-log-chevron', loadAnnouncementLog);
  });
  document.getElementById('audit-toggle')?.addEventListener('click', () => {
    _toggleCollapsibleSection('audit-body', 'audit-chevron', loadConversationAudit);
  });
  document.getElementById('btn-audit-search')?.addEventListener('click', () => loadConversationAudit());
  document.getElementById('btn-audit-refresh')?.addEventListener('click', () => loadConversationAudit());

  window.registerAdminSection('tools', {
    onEnter() {
      enterToolsSection();
    },
  });

  Object.assign(window, {
    testAnnounce,
    loadAnnouncementLog,
    clearAnnouncementLog,
    loadHeatingShadow,
    forceHeatingShadow,
    loadConversationAudit,
    loadWakeStatus,
    installEdgeTPUCompiler,
    trainWakeWord,
  });
})();
