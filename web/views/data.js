import { el, option } from '../lib/dom.js';
import { store, api } from '../lib/store.js';
import { money } from '../lib/format.js';
import { Table } from '../components/table.js';
import { DropZone } from '../components/dropZone.js';
import { SyncButton } from '../components/syncButton.js';

const panel = (title, ...kids) => el('section', { class: 'panel' }, el('h3', {}, title), ...kids);

export function DataView() {
  const d = store.data, c = d.last_counts || {};

  const syncBtn = SyncButton({ onDone: () => location.reload() });

  const results = el('div', { class: 'import-results' });
  const acctSel = el('select', { 'aria-label': 'account (only if it cannot be detected)' }, option('detect account from file', ''), ...d.accounts.map(a => option(a.name, a.id)));
  const onFiles = async files => {
    let changed = false;
    const summary = el('div', { class: 'muted' }, `importing ${files.length} file${files.length > 1 ? 's' : ''}…`);
    results.prepend(summary);
    for (const f of files) {
      const line = el('div', { class: 'import-line' }, el('b', {}, f.name), ' ', el('span', { class: 'muted' }, 'importing…'));
      results.prepend(line);
      try {
        const q = '?name=' + encodeURIComponent(f.name) + (acctSel.value ? '&account=' + encodeURIComponent(acctSel.value) : '');
        const j = await api('/api/import' + q, { method: 'POST', body: await f.arrayBuffer() });
        const acct = d.accounts.find(a => a.id === j.account)?.name || j.account;
        line.lastChild.textContent = j.restored ? `backup restored — ${j.transactions} transactions` : j.skipped ? `skipped — ${j.reason}` : `${acct} · ${j.new} new · ${j.upgraded} alert rows → posted · ${j.dup} already there` + (j.statements ? ` · ${j.statements} statement` : '');
        line.lastChild.className = j.skipped ? 'warn' : 'down';
        if (j.restored || (!j.skipped && (j.new || j.upgraded || j.statements))) changed = true;
      } catch (e) { line.lastChild.textContent = 'failed: ' + e.message; line.lastChild.className = 'up'; }
    }
    summary.textContent = changed ? 'done — reloading…' : 'done — nothing new';
    if (changed) setTimeout(() => location.reload(), 1200);
  };
  return el('div', {},
    panel('Sync', el('div', { class: 'row' },
      el('div', { class: 'kv grow' }, el('b', {}, 'last sync'), el('span', {}, d.last_sync ? d.last_sync.replace('T', ' ') : 'never'),
        el('b', {}, 'last run'), el('span', {}, Object.keys(c).length ? `${c.txn} transactions · ${c.statement} statements · ${c.skip} skipped · ${c.unparsed} unparsed` : '—')),
      syncBtn)),
    panel('Accounts', el('div', { class: 'kv' }, ...d.accounts.flatMap(a =>
      [el('b', {}, a.name), el('span', {}, `${d.tx.filter(t => t.acct === a.id).length} transactions · id `, el('code', {}, a.id))]))),
    panel('Backup / move to another machine',
      el('div', { class: 'row' }, el('a', { class: 'btn', href: '/api/backup', download: '' }, '⬇ Download backup'),
        el('span', { class: 'muted' }, 'ledger + budgets + rules + settings (not the Gmail password). To move: download here, drop the zip on the other machine\'s Data tab.'))),
    panel('Import statements / exports',
      el('p', { class: 'muted', style: { margin: '0 0 8px' } }, 'Backfills history and upgrades alert rows to the bank\'s posted amounts. The same file twice is ignored.'),
      DropZone({ onImport: onFiles }), el('div', { class: 'row', style: { marginTop: '8px' } }, el('span', { class: 'muted' }, 'if the account can\'t be detected:'), acctSel), results),
    panel(el('span', {}, 'Statement checksum ', el('span', { class: 'muted' }, 'bank balance vs. what we have, per cycle')),
      Table({ columns: [{ title: 'card' }, { title: 'statement' }, { title: 'bank', numeric: true }, { title: 'ours', numeric: true }, { title: 'gap', numeric: true }],
        rows: d.checksum.slice(0, 12).map(r => [r.card, r.date, money(r.bank), money(r.ours), el('span', { class: Math.abs(r.gap) < 1 ? 'down' : 'up' }, money(r.gap))]),
        empty: 'no statement emails yet' })),
    panel(el('span', {}, 'Unparsed emails ', el('span', { class: 'muted' }, 'last 50')),
      Table({ columns: [{ title: 'date' }, { title: 'bank' }, { title: 'subject' }], rows: d.failures.map(f => [f.date, f.bank, f.subject]), empty: 'none — every bank email was recognised' })),
  );
}
