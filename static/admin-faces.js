'use strict';

(() => {
  const facesApi = window.adminApi?.faces;
  if (!facesApi) return;

  let trainFaceBytes = null;
  let trainFaceStream = null;

  async function loadFaces() {
    const unknownEl = document.getElementById('faces-unknown');
    const knownEl = document.getElementById('faces-known');
    if (!unknownEl || !knownEl) return;
    try {
      const [unknown, known] = await Promise.all([
        facesApi.getUnknown(),
        facesApi.getKnown(),
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
    } catch {
      unknownEl.innerHTML = '<div class="text-sm" style="color:var(--danger);">Failed to load faces</div>';
    }
  }

  async function registerFace(faceId) {
    const input = document.getElementById('face-name-' + faceId);
    const name = input?.value.trim();
    if (!name) {
      toast('Enter a name first', 'err');
      return;
    }
    try {
      const r = await facesApi.register({ face_id: faceId, name });
      if (r.ok) {
        toast(`Registered ${name}`, 'ok');
        loadFaces();
      } else {
        toast(r.error || 'Failed', 'err');
      }
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  async function dismissFace(faceId) {
    try {
      await facesApi.dismissUnknown(faceId);
      loadFaces();
    } catch {}
  }

  async function deleteFace(name) {
    if (!confirm(`Delete face "${name}"? They will need to be re-registered.`)) return;
    try {
      await facesApi.deleteKnown(name);
      toast(`Deleted ${name}`, 'ok');
      loadFaces();
    } catch (e) {
      toast('Failed: ' + e.message, 'err');
    }
  }

  function trainFaceSetPreview(blob) {
    const url = URL.createObjectURL(blob);
    const img = document.getElementById('train-face-preview');
    const wrap = document.getElementById('train-face-preview-wrap');
    if (img) img.src = url;
    if (wrap) wrap.style.display = '';
  }

  function trainFaceStopCam() {
    if (trainFaceStream) {
      trainFaceStream.getTracks().forEach(track => track.stop());
      trainFaceStream = null;
    }
    const vid = document.getElementById('train-face-video');
    const btns = document.getElementById('train-face-webcam-btns');
    if (vid) {
      vid.srcObject = null;
      vid.style.display = 'none';
    }
    if (btns) btns.style.display = 'none';
  }

  function handleTrainFaceFileChange() {
    const file = this.files?.[0];
    if (!file) return;
    const fnEl = document.getElementById('train-face-filename');
    if (fnEl) fnEl.textContent = file.name;
    const reader = new FileReader();
    reader.onload = e => {
      trainFaceBytes = new Uint8Array(e.target.result);
      trainFaceStopCam();
      fetch(URL.createObjectURL(file)).then(() => {});
      trainFaceSetPreview(file);
    };
    reader.readAsArrayBuffer(file);
  }

  async function startTrainFaceWebcam() {
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
      trainFaceStream = await navigator.mediaDevices.getUserMedia({ video: true });
      const vid = document.getElementById('train-face-video');
      const btns = document.getElementById('train-face-webcam-btns');
      if (vid) {
        vid.srcObject = trainFaceStream;
        vid.style.display = 'block';
      }
      if (btns) btns.style.display = 'flex';
      if (btn) btn.textContent = 'Use Webcam';
    } catch (e) {
      if (btn) btn.textContent = 'Use Webcam';
      const msg = e.name === 'NotAllowedError' ? 'Camera permission denied — check browser settings'
        : e.name === 'NotFoundError' ? 'No camera found on this device'
        : e.name === 'OverconstrainedError' ? 'Camera constraint error: ' + e.message
        : 'Camera error: ' + e.name + ' — ' + e.message;
      if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">${msg}</span>`;
    }
  }

  function snapTrainFaceWebcam() {
    const vid = document.getElementById('train-face-video');
    const canvas = document.getElementById('train-face-canvas');
    if (!vid || !canvas) return;
    canvas.width = vid.videoWidth;
    canvas.height = vid.videoHeight;
    canvas.getContext('2d').drawImage(vid, 0, 0);
    canvas.toBlob(blob => {
      if (!blob) return;
      blob.arrayBuffer().then(buf => {
        trainFaceBytes = new Uint8Array(buf);
        const fnEl = document.getElementById('train-face-filename');
        if (fnEl) fnEl.textContent = 'webcam snapshot';
        trainFaceSetPreview(blob);
      });
    }, 'image/jpeg', 0.92);
    trainFaceStopCam();
  }

  async function submitTrainFace() {
    const name = (document.getElementById('train-face-name')?.value || '').trim().toLowerCase();
    const statusEl = document.getElementById('train-face-status');
    if (!name) {
      toast('Enter a name first', 'err');
      return;
    }
    if (!trainFaceBytes) {
      toast('Choose a photo or snap from webcam', 'err');
      return;
    }
    if (statusEl) statusEl.textContent = 'Registering…';
    try {
      const fd = new FormData();
      fd.append('name', name);
      fd.append('image', new Blob([trainFaceBytes], { type: 'image/jpeg' }), 'face.jpg');
      const r = await fetch('/admin/faces/train', { method: 'POST', credentials: 'include', body: fd });
      if (r.status === 401) {
        window.location.href = '/admin/login';
        return;
      }
      const data = await r.json();
      if (data.ok) {
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--success)">Registered "${name}" successfully</span>`;
        toast(`Face registered: ${name}`, 'ok');
        trainFaceBytes = null;
        const nameInput = document.getElementById('train-face-name');
        if (nameInput) nameInput.value = '';
        const fn = document.getElementById('train-face-filename');
        if (fn) fn.textContent = '';
        const wrap = document.getElementById('train-face-preview-wrap');
        if (wrap) wrap.style.display = 'none';
        const fileInput = document.getElementById('train-face-file');
        if (fileInput) fileInput.value = '';
        loadFaces();
      } else {
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">${data.error || 'Registration failed'}</span>`;
        toast(data.error || 'Registration failed', 'err');
      }
    } catch (e) {
      if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">${e.message}</span>`;
      toast('Failed: ' + e.message, 'err');
    }
  }

  document.getElementById('btn-refresh-faces')?.addEventListener('click', () => loadFaces());
  document.getElementById('train-face-file')?.addEventListener('change', handleTrainFaceFileChange);
  document.getElementById('btn-train-face-webcam')?.addEventListener('click', () => startTrainFaceWebcam());
  document.getElementById('btn-train-face-snap')?.addEventListener('click', () => snapTrainFaceWebcam());
  document.getElementById('btn-train-face-cancel-cam')?.addEventListener('click', () => trainFaceStopCam());
  document.getElementById('btn-train-face-submit')?.addEventListener('click', () => submitTrainFace());

  window.registerAdminSection?.('faces', {
    onEnter() {
      loadFaces();
    },
    onLeave() {
      trainFaceStopCam();
    },
  });

  Object.assign(window, {
    loadFaces,
    registerFace,
    dismissFace,
    deleteFace,
  });
})();
