/* Help & Tips section */
(function () {
  let _currentDoc = null;

  async function helpLoad() {
    const listEl = document.getElementById('help-doc-list');
    if (!listEl) return;
    try {
      const d = await window.adminApi.help.listDocs();
      const docs = d.docs || [];
      listEl.innerHTML = docs.map(doc => `
        <button class="help-doc-btn" data-name="${_esc(doc.name)}" onclick="helpOpenDoc('${_esc(doc.name)}', this)">
          ${_esc(doc.title)}
          ${doc.generated ? '<span style="font-size:10px;color:var(--text3);"> ✦</span>' : ''}
        </button>
      `).join('');
      if (docs.length) helpOpenDoc(docs[0].name, listEl.querySelector('.help-doc-btn'));
    } catch(e) {
      listEl.innerHTML = `<div class="text-muted text-sm">Failed to load docs: ${e.message}</div>`;
    }
  }

  async function helpOpenDoc(name, btn) {
    const content = document.getElementById('help-doc-content');
    if (!content) return;
    document.querySelectorAll('.help-doc-btn').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    _currentDoc = name;
    content.innerHTML = '<div class="text-muted text-sm">Loading…</div>';
    try {
      const html = await window.adminApi.help.getDoc(name);
      content.innerHTML = html;
    } catch(e) {
      content.innerHTML = `<div style="color:#fca5a5;">Failed: ${e.message}</div>`;
    }
  }

  function _esc(s) {
    return String(s || '').replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  window.registerAdminSection('help', {
    onEnter() { helpLoad(); },
  });

  Object.assign(window, { helpLoad, helpOpenDoc });
})();
