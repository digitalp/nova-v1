'use strict';

(() => {
  const energyApi = window.adminApi?.energy;
  if (!energyApi) return;

  async function loadEnergy() {
    try {
      const [sumR, devR] = await Promise.all([
        energyApi.getSummary(),
        energyApi.getDevices(),
      ]);
      const s = sumR.summary || {};
      const devices = devR.devices || [];

      const cards = document.getElementById('energy-summary-cards');
      const card = (icon, label, val, unit, color) =>
        `<div style="background:var(--surface2);border-radius:12px;padding:14px 16px;border-left:3px solid ${color};">
          <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">${icon} ${label}</div>
          <div style="font-size:22px;font-weight:700;">${val}<span style="font-size:12px;font-weight:400;color:var(--text3);margin-left:4px;">${unit}</span></div>
        </div>`;
      const value = key => s[key]?.value != null ? s[key].value : '—';
      const unit = key => s[key]?.unit || '';
      if (cards) {
        cards.innerHTML = [
          card('⚡', 'Live Power', value('total_power'), 'W', '#ff9500'),
          card('💰', 'Cost/Hour', value('total_cost_hourly'), '£/h', '#34c759'),
          card('📅', 'Today', value('daily_cost'), '£', '#007aff'),
          card('📆', 'This Month', value('monthly_cost'), '£', '#5856d6'),
          card('🔌', 'Today Usage', value('smart_elec_today'), 'kWh', '#ff3b30'),
          card('🔥', 'Gas Today', value('smart_gas_cost_today'), '£', '#ff6b35'),
        ].join('');
      }

      const glow = document.getElementById('energy-glow');
      if (glow) {
        glow.innerHTML = `
          <div style="background:var(--surface2);border-radius:10px;padding:14px;border-left:3px solid #007aff;">
            <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">⚡ Electricity Today</div>
            <div style="font-size:20px;font-weight:700;">${value('smart_elec_today')} <span style="font-size:12px;color:var(--text3);">kWh</span></div>
            <div style="font-size:14px;font-weight:600;color:#007aff;margin-top:4px;">£${value('smart_elec_cost_today')}</div>
          </div>
          <div style="background:var(--surface2);border-radius:10px;padding:14px;border-left:3px solid #ff6b35;">
            <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">🔥 Gas Today</div>
            <div style="font-size:20px;font-weight:700;">${value('smart_gas_today')} <span style="font-size:12px;color:var(--text3);">kWh</span></div>
            <div style="font-size:14px;font-weight:600;color:#ff6b35;margin-top:4px;">£${value('smart_gas_cost_today')}</div>
          </div>
          <div style="background:var(--surface2);border-radius:10px;padding:14px;border-left:3px solid #5856d6;">
            <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">⚡ Electricity This Month</div>
            <div style="font-size:20px;font-weight:700;">£${value('monthly_cost')}</div>
          </div>
          <div style="background:var(--surface2);border-radius:10px;padding:14px;border-left:3px solid #af52de;">
            <div style="font-size:11px;color:var(--text3);margin-bottom:4px;">🔥 Gas Yesterday</div>
            <div style="font-size:20px;font-weight:700;">£${value('gas_prev_cost')}</div>
            <div style="font-size:11px;color:var(--text3);margin-top:2px;">${value('gas_prev_kwh')} kWh</div>
          </div>
        `;
      }

      const tariff = document.getElementById('energy-tariff');
      if (tariff) {
        tariff.innerHTML = `
          <div><span class="text-sm text-muted">Electricity Rate</span><div style="font-size:16px;font-weight:600;">${value('elec_rate')} <span class="text-xs text-muted">${unit('elec_rate')}</span></div></div>
          <div><span class="text-sm text-muted">Standing Charge</span><div style="font-size:16px;font-weight:600;">${value('elec_standing')} <span class="text-xs text-muted">${unit('elec_standing')}</span></div></div>
          <div><span class="text-sm text-muted">Gas Rate</span><div style="font-size:16px;font-weight:600;">${value('gas_rate')} <span class="text-xs text-muted">${unit('gas_rate')}</span></div></div>
          <div><span class="text-sm text-muted">Gas Standing</span><div style="font-size:16px;font-weight:600;">${value('gas_standing')} <span class="text-xs text-muted">${unit('gas_standing')}</span></div></div>
        `;
      }

      const devEl = document.getElementById('energy-devices');
      if (devEl) {
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
      }

      const yest = document.getElementById('energy-yesterday');
      if (yest) {
        yest.innerHTML = `
          <div><span class="text-sm text-muted">Electricity</span><div style="font-size:16px;font-weight:600;">${value('elec_prev_kwh')} kWh — £${value('elec_prev_cost')}</div></div>
          <div><span class="text-sm text-muted">Gas</span><div style="font-size:16px;font-weight:600;">${value('gas_prev_kwh')} kWh — £${value('gas_prev_cost')}</div></div>
        `;
      }
    } catch (e) {
      const cards = document.getElementById('energy-summary-cards');
      if (cards) cards.innerHTML = `<div class="text-sm" style="color:var(--danger);">Failed: ${_esc(e.message)}</div>`;
    }
  }

  document.getElementById('btn-refresh-energy')?.addEventListener('click', () => loadEnergy());

  window.registerAdminSection?.('energy', {
    onEnter() {
      loadEnergy();
      if (!window._energyInterval) window._energyInterval = setInterval(loadEnergy, 15000);
    },
    onLeave() {
      if (window._energyInterval) {
        clearInterval(window._energyInterval);
        window._energyInterval = null;
      }
    },
  });

  Object.assign(window, {
    loadEnergy,
  });
})();
