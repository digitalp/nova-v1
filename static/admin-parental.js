(() => {
  'use strict';

  let _parentalSelectedDevice = null;
  let _parentalConfigs = [];
  let _parentalDevices = [];
  let _parentalSelectedDeviceNumbers = new Set();
  let _parentalAppCatalog = [];
  let _parentalSelectedAppPkg = '';
  let _parentalSelectedAppName = '';
  let _parentalDeviceLocations = new Map();
  let _parentalMap = null;
  let _parentalMapMarkers = null;
  let _parentalMapAutoFitDone = false;
  let _parentalMapRefreshTimer = null;
  let _parentalMapRefreshInFlight = false;
  const _parentalMapRefreshPeriodMs = 10000;

  function parentalSectionVisible() {
    const section = document.getElementById('section-parental');
    return !!section && section.offsetParent !== null;
  }

  function parentalSetMapStatus(text) {
    const el = document.getElementById('parental-map-status');
    if (el) el.textContent = text;
  }

  function parentalStopAutoRefresh() {
    if (_parentalMapRefreshTimer) {
      clearInterval(_parentalMapRefreshTimer);
      _parentalMapRefreshTimer = null;
    }
  }

  function parentalStartAutoRefresh() {
    parentalStopAutoRefresh();
    _parentalMapRefreshTimer = setInterval(() => {
      if (document.hidden || !parentalSectionVisible()) return;
      parentalRefreshDeviceLocations();
    }, _parentalMapRefreshPeriodMs);
  }

  async function loadParentalSection() {
    try {
      const s = await window.adminApi.parental.getStatus();
      const bar = document.getElementById('parental-status-bar');
      const badge = document.getElementById('parental-hmdm-status');
      if (bar) bar.style.display = '';
      if (s.hmdm_reachable) {
        badge.textContent = 'MDM Connected';
        badge.className = 'badge badge-green';
      } else {
        badge.textContent = 'MDM Unreachable';
        badge.className = 'badge badge-red';
      }
    } catch (_) {}
    await loadParentalConfigs();
    await loadParentalDevices();
    await parentalLoadAppCatalog();
    parentalSyncSelectedPackageInputs();
    parentalStartAutoRefresh();
  }

  async function loadParentalConfigs() {
    try {
      const d = await window.adminApi.parental.getConfigurations();
      _parentalConfigs = d.configurations || [];
      const sel = document.getElementById('parental-enroll-config');
      if (sel) {
        sel.innerHTML = _parentalConfigs.map(c =>
          `<option value="${c.id}">${esc(c.name)}</option>`
        ).join('');
      }
    } catch (e) {
      console.error('parental configs', e);
    }
  }

  async function loadParentalDevices() {
    const el = document.getElementById('parental-device-list');
    if (!el) return;
    try {
      const d = await window.adminApi.parental.getDevices();
      _parentalDevices = d.devices || [];
      const devices = _parentalDevices;
      _parentalSelectedDeviceNumbers = new Set(
        [..._parentalSelectedDeviceNumbers].filter(number => devices.some(dev => String(dev.number) === String(number)))
      );
      parentalRenderSelectedSummary();
      if (!devices.length) {
        el.innerHTML = '<p class="text-muted text-sm">No devices enrolled yet. Use the QR code above to enroll a device.</p>';
        _parentalDeviceLocations = new Map();
        parentalRenderDeviceMap();
        return;
      }
      el.innerHTML = devices.map(dev => {
        const ts = dev.lastUpdate ? new Date(dev.lastUpdate).toLocaleString() : 'Never';
        const statusColor = dev.statusCode === 'green' ? '#22c55e' : dev.statusCode === 'red' ? '#ef4444' : '#f59e0b';
        const checked = _parentalSelectedDeviceNumbers.has(String(dev.number)) ? 'checked' : '';
        return `<div class="flex-between" style="cursor:pointer;padding:8px 0;border-bottom:1px solid var(--border);gap:10px;" onclick='parentalSelectDevice(${JSON.stringify(JSON.stringify(dev))})'>
          <div style="display:flex;align-items:center;gap:10px;min-width:0;">
            <input type="checkbox" ${checked} onclick="event.stopPropagation()" onchange='parentalToggleDeviceSelection(${JSON.stringify(JSON.stringify(dev))}, this.checked)'>
            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${statusColor};margin-right:6px"></span>
            <div style="min-width:0;">
              <strong>${esc(dev.description || dev.number)}</strong>
              <span class="text-muted text-sm" style="margin-left:6px">${esc(dev.number)}</span>
              <div class="text-sm text-muted">Config ${esc(String(dev.configurationId || '—'))}</div>
            </div>
          </div>
          <div class="text-sm text-muted">${esc(ts)}</div>
        </div>`;
      }).join('');
      await parentalRefreshDeviceLocations(devices);
    } catch (e) {
      el.innerHTML = `<p class="text-sm" style="color:var(--red)">Error loading devices: ${esc(e.message)}</p>`;
      parentalSetMapStatus('Unable to load device locations.');
    }
  }

  function parentalRenderSelectedSummary() {
    const el = document.getElementById('parental-selected-summary');
    if (!el) return;
    const count = _parentalSelectedDeviceNumbers.size;
    if (!count) {
      el.textContent = '0 selected';
      return;
    }
    const names = (_parentalDevices || [])
      .filter(dev => _parentalSelectedDeviceNumbers.has(String(dev.number)))
      .slice(0, 3)
      .map(dev => dev.description || dev.number);
    const suffix = count > names.length ? ` +${count - names.length} more` : '';
    el.textContent = `${count} selected: ${names.join(', ')}${suffix}`;
  }

  function parentalSelectDevice(devJson) {
    const dev = JSON.parse(devJson);
    _parentalSelectedDevice = dev;
    _parentalSelectedDeviceNumbers.add(String(dev.number));
    parentalRenderSelectedSummary();
    const panel = document.getElementById('parental-device-panel');
    document.getElementById('parental-panel-title').textContent =
      (dev.description || dev.number) + ' (' + dev.number + ')';
    panel.style.display = '';
    const pkgInput = document.getElementById('parental-block-pkg');
    if (pkgInput) pkgInput.value = document.getElementById('parental-selected-pkg')?.value || pkgInput.value;
    parentalLoadDeviceInfo(dev.number);
    parentalFocusDeviceOnMap(dev.number);
  }

  function parentalToggleDeviceSelection(devJson, checked) {
    const dev = JSON.parse(devJson);
    const number = String(dev.number);
    if (checked) _parentalSelectedDeviceNumbers.add(number);
    else _parentalSelectedDeviceNumbers.delete(number);
    parentalRenderSelectedSummary();
  }

  function parentalSelectAllDevices() {
    _parentalSelectedDeviceNumbers = new Set((_parentalDevices || []).map(dev => String(dev.number)));
    parentalRenderSelectedSummary();
    loadParentalDevices();
  }

  function parentalClearDeviceSelection() {
    _parentalSelectedDeviceNumbers.clear();
    parentalRenderSelectedSummary();
    loadParentalDevices();
  }

  function parentalExtractDeviceLocation(dev, info) {
    let devInfo = dev?.info || null;
    if (typeof devInfo === 'string') {
      try { devInfo = JSON.parse(devInfo); } catch (_) { devInfo = null; }
    }
    const explicitDeviceLocation = dev?.location || null;
    const latestDynamic = info?.latestDynamicData || devInfo?.latestDynamicData || null;
    const dynamicGps = latestDynamic && typeof latestDynamic === 'object' ? latestDynamic : null;
    const embeddedLocation = info?.location || explicitDeviceLocation || devInfo?.location || null;
    const rawLat =
      info?.lat ??
      info?.latitude ??
      dev?.lat ??
      dev?.latitude ??
      embeddedLocation?.lat ??
      dynamicGps?.gpsLat;
    const rawLon =
      info?.lon ??
      info?.longitude ??
      info?.lng ??
      dev?.lon ??
      dev?.longitude ??
      dev?.lng ??
      embeddedLocation?.lon ??
      dynamicGps?.gpsLon;
    const lat = Number(rawLat);
    const lon = Number(rawLon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    const number = String(dev?.number || info?.deviceNumber || '');
    const label = dev?.description || number || 'Device';
    return {
      number,
      label,
      lat,
      lon,
      lastUpdate:
        embeddedLocation?.ts ||
        explicitDeviceLocation?.ts ||
        info?.latestUpdateTime ||
        dev?.lastUpdate ||
        info?.lastUpdate ||
        null,
      statusCode: dev?.statusCode || '',
    };
  }

  async function parentalLoadDeviceInfo(number) {
    const locEl = document.getElementById('parental-location');
    const device = (_parentalDevices || []).find(dev => String(dev.number) === String(number));
    try {
      const info = await window.adminApi.parental.getDeviceInfo(number);
      const location = parentalExtractDeviceLocation(device, info);
      if (location) {
        _parentalDeviceLocations.set(String(number), location);
        locEl.innerHTML = `${location.lat.toFixed(5)}, ${location.lon.toFixed(5)} — <a href="https://www.google.com/maps?q=${location.lat},${location.lon}" target="_blank">View on map</a>`;
        parentalRenderDeviceMap(String(number));
      } else {
        locEl.textContent = 'GPS tracking is enabled for this device; waiting for the first location report.';
      }
    } catch (_) {
      const fallbackLocation = parentalExtractDeviceLocation(device, null);
      if (fallbackLocation) {
        _parentalDeviceLocations.set(String(number), fallbackLocation);
        locEl.innerHTML = `${fallbackLocation.lat.toFixed(5)}, ${fallbackLocation.lon.toFixed(5)} — <a href="https://www.google.com/maps?q=${fallbackLocation.lat},${fallbackLocation.lon}" target="_blank">View on map</a>`;
        parentalRenderDeviceMap(String(number));
      } else {
        locEl.textContent = 'Location unavailable';
      }
    }
  }

  function parentalEnsureMap() {
    const mapEl = document.getElementById('parental-device-map');
    if (!mapEl || typeof window.L === 'undefined') return null;
    if (_parentalMap) return _parentalMap;
    _parentalMap = L.map(mapEl, {
      zoomControl: true,
      scrollWheelZoom: false,
    }).setView([53.48, -2.24], 6);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(_parentalMap);
    _parentalMapMarkers = L.layerGroup().addTo(_parentalMap);
    setTimeout(() => _parentalMap?.invalidateSize(), 0);
    return _parentalMap;
  }

  async function parentalRefreshDeviceLocations(devices = _parentalDevices) {
    if (_parentalMapRefreshInFlight) return;
    _parentalMapRefreshInFlight = true;
    const emptyEl = document.getElementById('parental-device-map-empty');
    try {
      if (!devices || !devices.length) {
        _parentalDeviceLocations = new Map();
        _parentalMapAutoFitDone = false;
        if (emptyEl) {
          emptyEl.textContent = 'No enrolled devices yet.';
          emptyEl.style.display = '';
        }
        parentalSetMapStatus('No enrolled devices.');
        parentalRenderDeviceMap();
        return;
      }

      parentalSetMapStatus('Loading device locations…');
      const results = await Promise.allSettled(
        devices.map(async (dev) => {
          try {
            const info = await window.adminApi.parental.getDeviceInfo(dev.number);
            return parentalExtractDeviceLocation(dev, info);
          } catch (_) {
            return parentalExtractDeviceLocation(dev, null);
          }
        })
      );
      const nextLocations = new Map();
      for (const result of results) {
        if (result.status !== 'fulfilled' || !result.value) continue;
        nextLocations.set(result.value.number, result.value);
      }
      _parentalDeviceLocations = nextLocations;
      const waitingCount = Math.max(0, devices.length - nextLocations.size);
      if (emptyEl) {
        if (nextLocations.size) {
          emptyEl.style.display = 'none';
        } else {
          emptyEl.textContent = `GPS tracking is enabled for ${devices.length} device${devices.length === 1 ? '' : 's'}; waiting for the first location report.`;
          emptyEl.style.display = '';
        }
      }
      const refreshedAt = new Date().toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
      parentalSetMapStatus(
        nextLocations.size
          ? `${nextLocations.size} device location${nextLocations.size === 1 ? '' : 's'} available${waitingCount ? ` · ${waitingCount} waiting for first report` : ''} · live refresh every 10s · updated ${refreshedAt}`
          : `GPS tracking enabled · waiting for first location report from ${devices.length} device${devices.length === 1 ? '' : 's'} · live refresh every 10s`
      );
      parentalRenderDeviceMap();
    } finally {
      _parentalMapRefreshInFlight = false;
    }
  }

  function parentalRenderDeviceMap(focusNumber = '') {
    const map = parentalEnsureMap();
    const emptyEl = document.getElementById('parental-device-map-empty');
    if (!map || !_parentalMapMarkers) return;
    _parentalMapMarkers.clearLayers();
    const locations = [..._parentalDeviceLocations.values()];
    if (!locations.length) {
      if (emptyEl) emptyEl.style.display = '';
      return;
    }
    if (emptyEl) emptyEl.style.display = 'none';
    const bounds = [];
    let focusMarker = null;
    const shouldFit = Boolean(focusNumber) || !_parentalMapAutoFitDone;
    for (const loc of locations) {
      const marker = L.marker([loc.lat, loc.lon]);
      const when = loc.lastUpdate ? new Date(loc.lastUpdate).toLocaleString() : 'Unknown';
      const displayName = String(loc.number || loc.label || 'Device');
      marker.bindPopup(
        `<strong>${esc(displayName)}</strong><br>${esc(loc.label || displayName)}<br>${loc.lat.toFixed(5)}, ${loc.lon.toFixed(5)}<br><span class="text-muted">Updated ${esc(when)}</span><br><a href="https://www.google.com/maps?q=${loc.lat},${loc.lon}" target="_blank">Open in Maps</a>`
      );
      marker.bindTooltip(esc(displayName), {
        permanent: true,
        direction: 'right',
        offset: [12, 0],
        className: 'parental-map-label',
      });
      marker.addTo(_parentalMapMarkers);
      bounds.push([loc.lat, loc.lon]);
      if (focusNumber && String(loc.number) === String(focusNumber)) focusMarker = marker;
    }
    if (shouldFit) {
      if (bounds.length === 1) map.setView(bounds[0], 14);
      else map.fitBounds(bounds, { padding: [24, 24] });
      _parentalMapAutoFitDone = true;
    }
    setTimeout(() => {
      map.invalidateSize();
      if (focusMarker) focusMarker.openPopup();
    }, 0);
  }

  function parentalFocusDeviceOnMap(number) {
    if (!number || !_parentalDeviceLocations.has(String(number))) return;
    parentalRenderDeviceMap(String(number));
  }

  async function parentalSendAlert() {
    if (!_parentalSelectedDevice) return;
    const msg = document.getElementById('parental-alert-msg').value.trim();
    if (!msg) {
      toast('Enter a message first', 'err');
      return;
    }
    try {
      await window.adminApi.parental.sendAlert({
        device_number: _parentalSelectedDevice.number,
        message: msg,
        title: 'Nova Alert',
      });
      document.getElementById('parental-alert-msg').value = '';
      toast('Alert sent to ' + (_parentalSelectedDevice.description || _parentalSelectedDevice.number), 'ok');
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function parentalLoadAppCatalog(query = '') {
    const resultsEl = document.getElementById('parental-app-results');
    if (!resultsEl) return;
    resultsEl.innerHTML = '<p class="text-sm text-muted">Loading available packages…</p>';
    try {
      const q = query.trim();
      const d = await window.adminApi.parental.getApps(q);
      _parentalAppCatalog = d.apps || [];
      parentalRenderAppCatalog();
    } catch (e) {
      resultsEl.innerHTML = `<p class="text-sm" style="color:var(--red)">Error loading packages: ${esc(e.message)}</p>`;
    }
  }

  async function parentalSearchApps() {
    const q = document.getElementById('parental-app-search')?.value || '';
    await parentalLoadAppCatalog(q);
  }

  function parentalSelectedPackage() {
    const pkg = (document.getElementById('parental-selected-pkg')?.value || '').trim();
    return pkg || (document.getElementById('parental-block-pkg')?.value || '').trim();
  }

  function parentalSelectedAppRecord() {
    const pkg = parentalSelectedPackage();
    return (_parentalAppCatalog || []).find(app => app.pkg === pkg) || null;
  }

  function parentalUpdateSelectedAppDisplay(name = '', pkg = '') {
    const el = document.getElementById('parental-selected-app-display');
    if (!el) return;
    const value = pkg || parentalSelectedPackage();
    if (!value) {
      el.textContent = 'No app selected.';
      return;
    }
    el.textContent = `Selected app: ${name || value} (${value})`;
  }

  function parentalUpdateSelectedAppNote() {
    const el = document.getElementById('parental-selected-app-note');
    if (!el) return;
    const app = parentalSelectedAppRecord();
    if (!app) {
      el.textContent = 'Apps marked “Allow only” cannot be silently installed by Headwind; Deploy will just allow them on the selected configuration.';
      return;
    }
    if (app.installable) {
      el.textContent = `${app.name || app.pkg} is installable in Headwind. Deploy will mark it for installation on the selected configuration(s).`;
      return;
    }
    el.textContent = `${app.name || app.pkg} is an allow-only Headwind entry. Deploy will allow it on the selected configuration(s), but Headwind cannot silently install it because this catalog entry has no APK URL.`;
  }

  function parentalSyncSelectedPackageInputs() {
    const pkg = (_parentalSelectedAppPkg || '').trim();
    const selected = document.getElementById('parental-selected-pkg');
    const single = document.getElementById('parental-block-pkg');
    if (selected) {
      selected.value = pkg;
      selected.setAttribute('value', pkg);
    }
    if (single && pkg) {
      single.value = pkg;
      single.setAttribute('value', pkg);
    }
    parentalUpdateSelectedAppDisplay(_parentalSelectedAppName || pkg, pkg);
  }

  function parentalRenderAppCatalog() {
    const resultsEl = document.getElementById('parental-app-results');
    if (!resultsEl) return;
    parentalSyncSelectedPackageInputs();
    if (!_parentalAppCatalog.length) {
      resultsEl.innerHTML = '<p class="text-sm text-muted">No packages found.</p>';
      return;
    }
    resultsEl.innerHTML = _parentalAppCatalog.map(app => {
      const name = esc(app.name || app.pkg);
      const pkg = esc(app.pkg || '');
      const version = esc(app.version || '');
      const installBadge = app.installable
        ? '<span class="badge badge-green" style="font-size:11px;">Installable</span>'
        : '<span class="badge badge-yellow" style="font-size:11px;">Allow only</span>';
      const systemBadge = app.system
        ? '<span class="badge badge-gray" style="font-size:11px;">System</span>'
        : '';
      const selected = _parentalSelectedAppPkg === (app.pkg || '');
      const rowStyle = selected
        ? 'display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 4px;border-bottom:1px solid var(--border2);background:rgba(34,197,94,0.08);border-radius:8px;'
        : 'display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 4px;border-bottom:1px solid var(--border2);';
      const buttonClass = selected ? 'btn btn-primary' : 'btn btn-outline';
      const buttonLabel = selected ? 'Selected' : 'Use';
      return `<div style="${rowStyle}">
        <div style="min-width:0;">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
            <div style="font-weight:600;">${name}</div>
            ${installBadge}
            ${systemBadge}
          </div>
          <div class="text-sm text-muted" style="word-break:break-all;">${pkg}${version ? ` · ${version}` : ''}</div>
        </div>
        <button class="${buttonClass}" style="font-size:12px;flex-shrink:0;" onclick='return parentalSelectAppFromCatalog(${JSON.stringify(app.pkg)}, ${JSON.stringify(app.name || app.pkg)})'>${buttonLabel}</button>
      </div>`;
    }).join('');
    parentalUpdateSelectedAppNote();
  }

  function parentalSetSelectedApp(pkg, name = '') {
    _parentalSelectedAppPkg = pkg;
    _parentalSelectedAppName = name || pkg;
    parentalSyncSelectedPackageInputs();
  }

  function parentalSelectAppFromCatalog(pkg, name = '') {
    parentalSetSelectedApp(pkg, name);
    const search = document.getElementById('parental-app-search');
    if (search && name) search.value = name;
    parentalRenderAppCatalog();
    parentalUpdateSelectedAppNote();
    parentalUpdateSelectedAppDisplay(name || pkg, pkg);
    return false;
  }

  function parentalPickApp(pkg, name = '') {
    return parentalSelectAppFromCatalog(pkg, name);
  }

  async function parentalApplyToSelectedDevices(action) {
    const selected = [..._parentalSelectedDeviceNumbers];
    if (!selected.length) {
      toast('Select at least one device first', 'err');
      return;
    }
    const pkg = parentalSelectedPackage();
    if (!pkg) {
      toast('Choose a package first', 'err');
      return;
    }
    const match = (_parentalAppCatalog || []).find(app => app.pkg === pkg);
    const payload = {
      pkg,
      name: match?.name || pkg,
      device_numbers: selected,
    };
    if (action !== 2) payload.action = action;
    try {
      const res = action === 2
        ? await window.adminApi.parental.deployApp(payload)
        : await window.adminApi.parental.blockApp(payload);
      const verb = action === 2
        ? (res.result_mode === 'install' ? 'Deployed' : 'Allowed')
        : action === 0 ? 'Blocked' : 'Unblocked';
      const affected = res.affected_devices?.length || selected.length;
      const detail = res.message ? ` ${res.message}` : '';
      toast(`${verb} ${pkg} for ${affected} device${affected === 1 ? '' : 's'}.${detail}`, 'ok');
      await loadParentalDevices();
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function parentalBlockApp(action) {
    if (!_parentalSelectedDevice) {
      toast('Select a device first', 'err');
      return;
    }
    const pkg = document.getElementById('parental-block-pkg').value.trim();
    if (!pkg) {
      toast('Enter a package name', 'err');
      return;
    }
    const configId = _parentalSelectedDevice.configurationId;
    if (!configId) {
      toast('Device has no configuration assigned', 'err');
      return;
    }
    try {
      const match = (_parentalAppCatalog || []).find(app => app.pkg === pkg);
      const payload = { config_id: configId, pkg, name: match?.name || pkg };
      if (action !== 2) payload.action = action;
      const res = action === 2
        ? await window.adminApi.parental.deployApp(payload)
        : await window.adminApi.parental.blockApp(payload);
      document.getElementById('parental-block-pkg').value = '';
      const bulkInput = document.getElementById('parental-selected-pkg');
      if (bulkInput && !bulkInput.value) bulkInput.value = pkg;
      parentalUpdateSelectedAppNote();
      const verb = action === 2
        ? (res.result_mode === 'install' ? 'Deployed' : 'Allowed')
        : action === 0 ? 'Blocked' : 'Unblocked';
      const detail = res.message ? ` ${res.message}` : '';
      toast(`${verb} ${pkg}.${detail}`, 'ok');
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  function parentalQuickBlock(pkg) {
    parentalPickApp(pkg, pkg);
  }

  async function parentalShowEnroll() {
    const sel = document.getElementById('parental-enroll-config');
    const configId = sel?.value;
    if (!configId) {
      toast('Select a configuration first', 'err');
      return;
    }
    try {
      const d = await window.adminApi.parental.getEnrollQr(configId);
      const area = document.getElementById('parental-qr-area');
      const img = document.getElementById('parental-qr-img');
      const urlEl = document.getElementById('parental-enroll-url');
      img.src = d.qr_image_url;
      urlEl.textContent = d.enroll_url;
      area.style.display = '';
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function parentalShowProvisioningQr() {
    const sel = document.getElementById('parental-enroll-config');
    const configId = sel?.value;
    if (!configId) {
      toast('Select a configuration first', 'err');
      return;
    }
    try {
      const d = await window.adminApi.parental.getProvisioningQr(configId);
      const area = document.getElementById('parental-provision-qr-area');
      const img = document.getElementById('parental-provision-qr-img');
      img.src = d.qr_image_url;
      area.style.display = '';
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    if (!parentalSectionVisible()) return;
    parentalRefreshDeviceLocations();
  });

  window.registerAdminSection('parental', {
    onEnter() {
      return loadParentalSection();
    },
    onLeave() {
      parentalStopAutoRefresh();
    },
  });

  Object.assign(window, {
    loadParentalSection,
    loadParentalConfigs,
    loadParentalDevices,
    parentalSectionVisible,
    parentalStopAutoRefresh,
    parentalStartAutoRefresh,
    parentalSelectDevice,
    parentalToggleDeviceSelection,
    parentalSelectAllDevices,
    parentalClearDeviceSelection,
    parentalLoadDeviceInfo,
    parentalExtractDeviceLocation,
    parentalSetMapStatus,
    parentalEnsureMap,
    parentalRefreshDeviceLocations,
    parentalRenderDeviceMap,
    parentalFocusDeviceOnMap,
    parentalSendAlert,
    parentalLoadAppCatalog,
    parentalSearchApps,
    parentalRenderAppCatalog,
    parentalSetSelectedApp,
    parentalSelectAppFromCatalog,
    parentalPickApp,
    parentalSelectedAppRecord,
    parentalUpdateSelectedAppNote,
    parentalUpdateSelectedAppDisplay,
    parentalSyncSelectedPackageInputs,
    parentalSelectedPackage,
    parentalApplyToSelectedDevices,
    parentalBlockApp,
    parentalQuickBlock,
    parentalShowEnroll,
    parentalShowProvisioningQr,
  });
})();
