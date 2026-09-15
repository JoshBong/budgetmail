import { el, option } from '../lib/dom.js';
import { store, matches } from '../lib/store.js';
import { money0, monthLabel, ym } from '../lib/format.js';
import { TxList } from '../components/txList.js';
import { recatOptions } from '../lib/recat.js';
import { txActions } from '../lib/txActions.js';

export function TransactionsView({ rerender }) {
  const f = store.state.filter;
  const rows = store.data.tx.filter(t =>
    (!f.month || ym(t.date) === f.month) && (!f.account || t.card === f.account) &&
    (!f.category || (t.cat === f.category && !t.ignore && t.type !== 'transfer')) &&
    matches(t, f.q));
  const out = rows.filter(t => !t.ignore && t.type !== 'transfer').reduce((s, t) => s - t.amount, 0);
  const inn = rows.filter(t => t.type === 'transfer' && !t.dupe).reduce((s, t) => s + t.amount, 0);

  const select = (name, opts, all) => {
    const s = el('select', { 'aria-label': name, onchange: e => { f[name] = e.target.value; rerender(); } }, option(all, ''), ...opts.map(([v, l]) => option(l, v)));
    s.value = f[name];
    return s;
  };
  const search = el('input', { placeholder: 'search merchant…', value: f.q, oninput: e => { f.q = e.target.value.toLowerCase(); rerender({ keepFocus: e.target }); } });

  return el('div', {},
    el('div', { class: 'filters' }, search,
      select('month', store.months.map(m => [m, monthLabel(m)]), 'all months'),
      select('account', store.data.accounts.map(a => [a.name, a.name]), 'all accounts'),
      select('category', store.categories.map(c => [c, c]), 'all categories')),
    el('div', { class: 'muted', style: { marginBottom: '8px' } }, `${rows.length} transactions · ${money0(out)} out` + (inn ? ` · ${money0(inn)} in` : '')),
    TxList({ rows, colorFor: c => store.categoryColor(c), drag: recatOptions(rerender), onTap: txActions(rerender) }),
  );
}
