// Tap a transaction: give it a readable name (this one / all from the merchant) or mark it a duplicate.
// Both are undoable from the toast, like drag-to-recategorize.
import { el } from './dom.js';
import { store, api } from './store.js';
import { money, dayLabel } from './format.js';
import { toast } from '../components/catRail.js';

export function txActions(rerender) {
  const reload = async () => { await store.load(); rerender(); };
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });

  return tx => {
    const count = store.data.tx.filter(t => t.mkey === tx.mkey).length;
    const esc = e => { if (e.key === 'Escape') close(); };
    const close = () => { back.remove(); document.removeEventListener('keydown', esc); };

    const rename = async (scope, name) => {
      close();
      let r;
      try { r = await post('/api/rename', { id: tx.id, name, scope }); }
      catch (e) { return toast('could not rename: ' + e.message); }
      await reload();
      toast(name ? `${r.changed > 1 ? r.changed + ' charges' : 'renamed'} → ${name}` : 'back to ' + tx.orig, {
        label: 'Undo', onClick: async () => { await post('/api/rename/revert', r.revert).catch(() => {}); await reload(); },
      });
    };
    const dupe = async on => {
      close();
      try { await post('/api/dupe', { id: tx.id, dupe: on }); }
      catch (e) { return toast('could not update: ' + e.message); }
      await reload();
      toast(on ? `${tx.merchant} · marked duplicate` : `${tx.merchant} · counts again`, {
        label: 'Undo', onClick: async () => { await post('/api/dupe', { id: tx.id, dupe: !on }).catch(() => {}); await reload(); },
      });
    };

    const input = el('input', { value: tx.merchant, 'aria-label': 'name', enterkeyhint: 'done',
      onkeydown: e => { if (e.key === 'Enter') submit('one'); } });
    const submit = scope => {
      const name = input.value.trim();
      if (!name || (scope === 'one' && name === tx.merchant)) return close();
      rename(scope, name);
    };
    const card = el('div', { class: 'panel ask' },
      el('h3', {}, tx.merchant),
      el('div', { class: 'muted' }, `${money(tx.amount)} · ${dayLabel(tx.date)} · ${tx.card}`),
      tx.renamed ? el('div', { class: 'muted orig' }, 'originally ' + tx.orig) : null,
      el('label', { class: 'ask-field' }, 'Name', input),
      el('div', { class: 'ask-actions' },
        el('button', { class: 'primary', onclick: () => submit('one') }, 'Rename this one'),
        count > 1 ? el('button', { onclick: () => submit('merchant') }, `Rename all ${count} · and future ones`) : null,
        tx.renamed ? el('button', { onclick: () => rename(tx.renamed, '') }, 'Reset to original name') : null,
        el('button', { onclick: () => dupe(!tx.dupe) }, tx.dupe ? 'Not a duplicate' : 'Mark as duplicate'),
        el('button', { class: 'muted', onclick: close }, 'Cancel')));
    const back = el('div', { class: 'backdrop', onclick: e => { if (e.target === back) close(); } }, card);
    document.body.append(back);
    document.addEventListener('keydown', esc);
    if (matchMedia('(pointer: fine)').matches) { input.focus(); input.select(); }   // no surprise keyboard on a phone
  };
}
