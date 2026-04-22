'use strict';

(() => {
  const musicApi = window.adminApi?.music;
  if (!musicApi) return;

  const BRANDS = {
    sonos: { match: p => p.entity_id.includes('sonos'), logo: '<span style="font-size:16px;font-weight:800;letter-spacing:2px;text-transform:uppercase;">Sonos</span>' },
    denon: { match: p => p.entity_id.includes('denon'), logo: '<span style="font-size:16px;font-weight:800;letter-spacing:1px;text-transform:uppercase;">DENON</span>' },
    alexa: { match: p => p.entity_id.includes('echo') || p.entity_id.includes('alexa'), logo: '<span style="font-size:16px;font-weight:800;letter-spacing:1px;">🔵 Alexa</span>' },
  };

  async function loadMusicPlayers() {
    const maEl = document.getElementById('music-ma-status');
    if (maEl) {
      try {
        const st = await musicApi.getStatus();
        if (!st.configured) {
          maEl.innerHTML = '⚪ Not configured — add <code>MUSIC_ASSISTANT_URL</code> to .env';
        } else if (st.available) {
          maEl.innerHTML = '<span style="color:var(--green);">● Connected</span> — search and play available';
        } else {
          maEl.innerHTML = '<span style="color:var(--red);">● Offline</span> — start with: <code>docker compose up -d music-assistant</code>';
        }
      } catch {
        maEl.innerHTML = '⚪ Status unknown';
      }
    }

    const npEl = document.getElementById('music-now-playing');
    const allEl = document.getElementById('music-players-list');
    if (!npEl || !allEl) return;
    try {
      const d = await musicApi.getPlayers();
      const players = d.players || [];
      const active = players.filter(p => ['playing', 'paused', 'buffering', 'idle'].includes(p.state) && (p.media_title || p.state === 'paused'));

      const sel = document.getElementById('music-target-players');
      if (sel) {
        const available = players.filter(p => p.state !== 'unavailable');
        const wasChecked = new Set([...document.querySelectorAll('.music-speaker-chk:checked')].map(c => c.value));
        const brandGroups = {
          sonos: { match: id => id.includes('sonos'), label: 'SONOS', color: '#000' },
          denon: { match: id => id.includes('denon'), label: 'DENON', color: '#0a2463' },
          alexa: { match: id => id.includes('echo') || id.includes('alexa'), label: '🔵 Alexa', color: '#232f3e' },
        };
        let html = '';
        const used = new Set();
        for (const [, brand] of Object.entries(brandGroups)) {
          const group = available.filter(p => brand.match(p.entity_id));
          group.forEach(p => used.add(p.entity_id));
          if (!group.length) continue;
          html += `<div style="margin-bottom:8px;"><div style="font-size:10px;font-weight:700;color:var(--text3);letter-spacing:1px;margin-bottom:4px;">${brand.label}</div><div style="display:flex;flex-wrap:wrap;gap:6px;">`;
          html += group.map(p => {
            const eid = _esc(p.entity_id);
            const chk = wasChecked.has(p.entity_id) ? 'checked' : '';
            return `<label style="display:flex;align-items:center;gap:5px;font-size:12px;padding:6px 10px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:all .15s;" onmouseover="this.style.borderColor='${brand.color}'" onmouseout="this.style.borderColor='var(--border)'">
              <input type="checkbox" class="music-speaker-chk" value="${eid}" ${chk} style="accent-color:${brand.color};">
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
        renderMusicPlayersList(active, npEl);
      }
      if (!players.length) {
        allEl.innerHTML = '<div class="text-sm text-muted">No media players found.</div>';
      } else {
        renderMusicPlayersList(players, allEl);
      }
    } catch (e) {
      npEl.innerHTML = `<div class="text-sm" style="color:var(--danger);">Failed: ${_esc(e.message)}</div>`;
    }
  }

  function renderMusicPlayer(p, showControls) {
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
        <button style="width:36px;height:36px;border-radius:50%;border:none;background:${accent};color:#fff;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;" onclick="musicCtrl('${eid}','${p.state === 'playing' ? 'pause' : 'play'}')">${p.state === 'playing' ? '⏸' : '▶'}</button>
        <button class="btn btn-outline btn-sm" onclick="musicCtrl('${eid}','next')" style="font-size:13px;padding:4px 8px;">⏭</button>
        <button class="btn btn-outline btn-sm" onclick="musicCtrl('${eid}','stop')" style="font-size:11px;padding:4px 8px;">⏹</button>
        <div style="flex:1;display:flex;align-items:center;gap:6px;margin-left:8px;">
          <span style="font-size:10px;color:var(--text3);">🔊</span>
          <input type="range" min="0" max="100" value="${p.volume_level != null ? Math.round(p.volume_level * 100) : 50}" style="flex:1;max-width:100px;accent-color:${accent};" onchange="musicCtrl('${eid}','volume',this.value/100)">
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

  function renderMusicPlayersList(players, container) {
    let html = '';
    const used = new Set();
    for (const [, cfg] of Object.entries(BRANDS)) {
      const group = players.filter(p => cfg.match(p));
      group.forEach(p => used.add(p.entity_id));
      if (!group.length) continue;
      html += `<div style="margin-bottom:16px;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;padding-bottom:8px;border-bottom:2px solid var(--border);">
          ${cfg.logo}
        </div>
        ${group.map(p => renderMusicPlayer(p, false)).join('')}
      </div>`;
    }
    const other = players.filter(p => !used.has(p.entity_id));
    if (other.length) {
      html += `<div style="margin-bottom:16px;">
        <div style="font-weight:600;font-size:13px;margin-bottom:8px;padding-bottom:8px;border-bottom:2px solid var(--border);">Other</div>
        ${other.map(p => renderMusicPlayer(p, false)).join('')}
      </div>`;
    }
    if (!html) html = '<div class="text-sm text-muted">No speakers found.</div>';
    container.innerHTML = html;
  }

  async function musicCtrl(entityId, action, value) {
    try {
      await musicApi.control({ entity_id: entityId, action, value: value ?? null });
      setTimeout(loadMusicPlayers, 500);
    } catch (e) {
      toast('Music control failed: ' + e.message);
    }
  }

  async function musicSearch() {
    const q = (document.getElementById('music-search-input')?.value || '').trim();
    const el = document.getElementById('music-search-results');
    if (!q || !el) return;
    el.innerHTML = '<div class="text-sm text-muted">Searching…</div>';
    try {
      const d = await musicApi.search(q);
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
    } catch (e) {
      el.innerHTML = `<div class="text-sm" style="color:var(--danger);">Search failed: ${_esc(e.message)}</div>`;
    }
  }

  async function musicPlayUri(uri) {
    const checked = [...document.querySelectorAll('.music-speaker-chk:checked')].map(c => c.value);
    if (!checked.length) {
      toast('Select at least one speaker');
      return;
    }
    try {
      for (const eid of checked) {
        await musicApi.control({ entity_id: eid, action: 'play', value: uri });
      }
      toast('Playing on ' + checked.length + ' speaker' + (checked.length > 1 ? 's' : ''));
      setTimeout(loadMusicPlayers, 1000);
    } catch (e) {
      toast('Play failed: ' + e.message);
    }
  }

  document.getElementById('btn-refresh-music')?.addEventListener('click', () => loadMusicPlayers());
  document.getElementById('music-search-input')?.addEventListener('keydown', e => { if (e.key === 'Enter') musicSearch(); });
  document.getElementById('btn-music-search')?.addEventListener('click', () => musicSearch());
  document.getElementById('music-players-toggle')?.addEventListener('click', () => {
    window._toggleCollapsibleSection('music-players-body', 'music-players-chevron', loadMusicPlayers);
  });

  window.registerAdminSection?.('music', {
    onEnter() {
      loadMusicPlayers();
    },
  });

  Object.assign(window, {
    loadMusicPlayers,
    musicCtrl,
    musicSearch,
    musicPlayUri,
  });
})();
