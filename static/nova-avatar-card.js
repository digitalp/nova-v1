/**
 * nova-avatar-card — Lovelace custom card for the Nova AI avatar
 *
 * Install:
 *   Settings → Dashboards → Resources → Add
 *   URL:  https://192.168.0.249:8443/static/nova-avatar-card.js
 *   Type: JavaScript module
 *
 * Usage (YAML):
 *   type: custom:nova-avatar-card
 *   url:    https://192.168.0.249:8443/avatar?api_key=YOUR_KEY&session_id=ha-dashboard
 *   height: 480px          # any CSS height value — default 480px
 *   title:  Nova           # optional card header label
 */
class NovaAvatarCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
  }

  setConfig(config) {
    if (!config.url) throw new Error('nova-avatar-card requires a url');
    this._config = config;
    this._render();
  }

  _render() {
    const { url, height = '480px', title } = this._config;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        .card-header {
          padding: 12px 16px 4px;
          font-size: 14px; font-weight: 500;
          color: var(--primary-text-color, #e2e8f0);
        }
        iframe {
          display: block; width: 100%; height: ${height};
          border: none;
          border-radius: var(--ha-card-border-radius, 12px);
          background: #080d16;
        }
      </style>
      ${title ? `<div class="card-header">${title}</div>` : ''}
      <iframe
        src="${url}"
        allow="microphone; autoplay; camera"
        referrerpolicy="no-referrer-when-downgrade"
        scrolling="no"
      ></iframe>
    `;
  }

  // Called by HA when state changes — not needed but must exist
  set hass(_) {}

  getCardSize() {
    const px = parseInt(this._config?.height) || 480;
    return Math.ceil(px / 50);
  }

  static getStubConfig() {
    return {
      url: 'https://192.168.0.249:8443/avatar?api_key=YOUR_KEY&session_id=ha-dashboard',
      height: '480px',
    };
  }
}

customElements.define('nova-avatar-card', NovaAvatarCard);
