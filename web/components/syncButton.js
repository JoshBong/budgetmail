import { el } from '../lib/dom.js';
import { api } from '../lib/store.js';

// ⟳ Sync now — pulls new bank emails, then onDone() (reload data + rerender). compact = icon-sized for the Spending header.
export function SyncButton({ onDone, compact }) {
  const idle = compact ? '⟳' : '⟳ Sync now';
  const btn = el('button', { class: compact ? 'sync-btn' : 'primary', title: 'sync bank emails now', 'aria-label': 'sync now', onclick: async () => {
    btn.textContent = compact ? '…' : 'syncing…'; btn.disabled = true;
    try { await api('/api/sync', { method: 'POST' }); await onDone(); }
    catch (e) { btn.textContent = compact ? '!' : 'failed: ' + e.message; btn.title = 'sync failed: ' + e.message; btn.disabled = false; }
  } }, idle);
  return btn;
}
