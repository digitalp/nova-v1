'use strict';

(() => {
  const chatApi = window.adminApi?.chat;
  if (!chatApi) return;

  let chatSessionId = 'admin_chat_' + Date.now();
  let chatApiKey = '';
  let chatLastText = '';

  async function ensureChatApiKey() {
    const _authEl = document.getElementById('chat-summary-auth');
    const _sessEl = document.getElementById('chat-summary-session');
    if (_sessEl) _sessEl.textContent = chatSessionId.replace('admin_chat_', '#') ;
    if (chatApiKey) {
      if (_authEl) _authEl.textContent = 'Key loaded';
      return;
    }
    try {
      const result = await chatApi.getApiKey();
      chatApiKey = result.api_key || '';
      if (_authEl) _authEl.textContent = chatApiKey ? 'Key loaded' : 'No key set';
    } catch {
      if (_authEl) _authEl.textContent = 'Error';
    }
  }

  function chatTs() {
    return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function chatBubble(role, text) {
    const isUser = role === 'user';
    const wrap = document.createElement('div');
    wrap.style.cssText = `display:flex;flex-direction:column;${isUser ? 'align-items:flex-end;' : 'align-items:flex-start;'}`;
    const bubble = document.createElement('div');
    bubble.style.cssText = `max-width:80%;padding:10px 14px;border-radius:12px;word-wrap:break-word;white-space:pre-wrap;${
      isUser ? 'background:var(--accent);color:#fff;border-bottom-right-radius:4px;'
        : 'background:var(--bg1);color:var(--text1);border:1px solid var(--border);border-bottom-left-radius:4px;'
    }`;
    bubble.textContent = text;
    const ts = document.createElement('div');
    ts.style.cssText = 'font-size:10px;color:var(--text3);margin-top:2px;padding:0 4px;';
    ts.textContent = chatTs();
    wrap.appendChild(bubble);
    wrap.appendChild(ts);
    return wrap;
  }

  function chatToolBubble(calls) {
    const bubble = document.createElement('div');
    bubble.style.cssText = 'align-self:flex-start;padding:6px 10px;border-radius:8px;background:var(--bg1);border:1px solid var(--border);font-size:11px;color:var(--text3);font-family:monospace;';
    bubble.textContent = '🔧 ' + calls.map(c => c.function_name + '(' + Object.values(c.arguments || {}).join(', ') + ')').join(' → ');
    return bubble;
  }

  function chatClear() {
    chatSessionId = 'admin_chat_' + Date.now();
    const container = document.getElementById('chat-messages');
    if (container) {
      container.innerHTML = '<div style="color:var(--text3);text-align:center;padding:40px 0;">Say something to Nova...</div>';
    }
    toast('Chat cleared');
  }

  async function chatSend(e) {
    e.preventDefault();
    const input = document.getElementById('chat-input');
    const text = input?.value.trim() || '';
    if (!text) return;
    chatLastText = text;
    input.value = '';
    const container = document.getElementById('chat-messages');
    if (!container) return;
    if (container.children.length === 1 && container.children[0].style.textAlign === 'center') container.innerHTML = '';
    container.appendChild(chatBubble('user', text));
    container.scrollTop = container.scrollHeight;
    const thinking = document.createElement('div');
    thinking.style.cssText = 'align-self:flex-start;color:var(--text3);font-size:12px;padding:6px;';
    thinking.textContent = 'Nova is thinking...';
    container.appendChild(thinking);
    container.scrollTop = container.scrollHeight;
    const slowTimer = setTimeout(() => { thinking.textContent = 'Still thinking... (GPU may be busy)'; }, 15000);
    try {
      await ensureChatApiKey();
      const ctrl = new AbortController();
      const abortTimer = setTimeout(() => ctrl.abort(), 120000);
      const response = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': chatApiKey },
        credentials: 'include',
        body: JSON.stringify({ text, session_id: chatSessionId }),
        signal: ctrl.signal,
      });
      clearTimeout(abortTimer);
      clearTimeout(slowTimer);
      if (!response.ok) throw new Error(await response.text() || response.statusText);
      const data = await response.json();
      if (container.contains(thinking)) container.removeChild(thinking);
      if (data.tool_calls && data.tool_calls.length) container.appendChild(chatToolBubble(data.tool_calls));
      container.appendChild(chatBubble('assistant', data.text || '(no response)'));
    } catch (err) {
      clearTimeout(slowTimer);
      if (container.contains(thinking)) container.removeChild(thinking);
      const msg = err.name === 'AbortError' ? 'Request timed out (2 min). GPU may be overloaded.' : err.message;
      const errWrap = document.createElement('div');
      errWrap.style.cssText = 'align-self:flex-start;display:flex;flex-direction:column;gap:4px;';
      errWrap.appendChild(chatBubble('assistant', '⚠ ' + msg));
      const retryBtn = document.createElement('button');
      retryBtn.className = 'btn btn-outline';
      retryBtn.style.cssText = 'font-size:11px;padding:4px 10px;align-self:flex-start;';
      retryBtn.textContent = '↻ Retry';
      retryBtn.onclick = () => {
        errWrap.remove();
        document.getElementById('chat-input').value = chatLastText;
        chatSend(new Event('submit'));
      };
      errWrap.appendChild(retryBtn);
      container.appendChild(errWrap);
    }
    container.scrollTop = container.scrollHeight;
  }

  document.getElementById('chat-form')?.addEventListener('submit', e => chatSend(e));
  document.getElementById('btn-chat-clear')?.addEventListener('click', () => chatClear());

  window.registerAdminSection?.('chat', {
    onEnter() {
      ensureChatApiKey();
    },
  });

  Object.assign(window, {
    chatClear,
    chatSend,
  });
})();
