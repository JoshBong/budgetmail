import { el } from '../lib/dom.js';
import { money, dayLabel } from '../lib/format.js';
import { makeDraggable, justDragged } from '../lib/drag.js';

// TxList({ rows, colorFor(cat), limit, drag, onTap }) — rows newest first; grouped by day.
// `drag` (from lib/recat.js) makes each row draggable into another category; `onTap` (lib/txActions.js) opens rename / duplicate.
export function TxList({ rows, colorFor, limit = 400, drag, onTap }) {
  if (!rows.length) return el('div', { class: 'list' }, el('div', { class: 'empty' }, 'nothing here'));
  const list = el('div', { class: 'list' });
  let day = '';
  for (const t of rows.slice(0, limit)) {
    if (t.date !== day) { day = t.date; list.append(el('div', { class: 'day' }, dayLabel(t.date))); }
    const income = t.type === 'transfer' || t.amount > 0;
    const cat = t.dupe ? 'duplicate' : t.ignore ? 'ignored' : t.type === 'transfer' ? 'income' : t.amount > 0 && t.type === 'zelle' ? t.cat + ' · paid back' : t.cat;
    const row = el('div', { class: 'tx' + (drag ? ' grab' : '') + (t.pinned ? ' pinned' : '') + (t.dupe ? ' dupe' : '') + (onTap ? ' tappable' : '') },
      el('div', { class: 'merchant', title: t.merchant }, t.merchant),
      el('div', { class: 'amount num' + (income ? ' in' : '') }, money(t.amount)),
      el('div', { class: 'meta', style: { '--c': colorFor(t.cat) } }, el('i', { class: 'dot' }), cat,
        t.pinned ? el('span', { class: 'pin', title: 'category you set by hand' }, '⌾') : null,
        t.renamed ? el('span', { class: 'pin', title: 'renamed · originally ' + t.orig }, '✎') : null,
        '·', t.card, t.status === 'pending' ? '· pending' : null),
    );
    if (drag) makeDraggable(row, t, drag);
    if (onTap) row.addEventListener('click', () => { if (!justDragged()) onTap(t); });
    list.append(row);
  }
  if (rows.length > limit) list.append(el('div', { class: 'empty' }, `showing ${limit} of ${rows.length} — narrow the filters`));
  return list;
}
