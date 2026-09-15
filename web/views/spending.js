import { el } from '../lib/dom.js';
import { store, sumBy, api, matches } from '../lib/store.js';
import { money0, monthLabel, ym, today } from '../lib/format.js';
import { MonthNav } from '../components/monthNav.js';
import { Segmented } from '../components/segmented.js';
import { Donut } from '../components/donut.js';
import { CategoryCards } from '../components/categoryCards.js';
import { TxList } from '../components/txList.js';
import { SyncButton } from '../components/syncButton.js';
import { recatOptions } from '../lib/recat.js';
import { txActions } from '../lib/txActions.js';

const MAX_SLICES = 8;

export function SpendingView({ rerender, goto }) {
  const { month, mode } = store.state;
  const drag = recatOptions(rerender);
  const rows = store.inMonth(month);
  const prev = store.prevMonth(month);
  const key = store.keyFor(mode);
  const cur = sumBy(rows, key), old = prev ? sumBy(store.inMonth(prev), key) : {};
  const net = Object.values(cur).reduce((a, b) => a + b, 0);               // spend minus paybacks/refunds
  const total = Object.values(cur).filter(v => v > 0).reduce((a, b) => a + b, 0);   // ring = the positive slices

  let items = Object.entries(cur).filter(([, v]) => v !== 0).sort((a, b) => b[1] - a[1]);
  if (mode === 'cat' && items.length > MAX_SLICES) {                       // fold the tail into Other
    const head = items.slice(0, MAX_SLICES - 1), tail = items.slice(MAX_SLICES - 1);
    const other = tail.reduce((s, [, v]) => s + v, 0) + (head.find(([k]) => k === store.other)?.[1] || 0);
    items = [...head.filter(([k]) => k !== store.other), [store.other, other]];
  }
  const byCat = mode === 'cat';
  if (byCat) for (const c of Object.keys(store.budgets)) if (store.categories.includes(c) && !items.some(([k]) => k === c)) items.push([c, 0]);   // budgeted but unspent: still show
  const shaped = items.map(([k, v]) => ({
    key: k, value: v, color: store.colorFor(mode, k), delta: v - (old[k] || 0),
    count: rows.filter(t => key(t) === k).length,
    budget: byCat ? store.budgets[k] : undefined, suggest: byCat ? store.avg3(k, month) : 0, budgetable: byCat && k !== store.other,
  }));
  const elapsed = store.elapsed(month);
  const saveBudget = async (cat, amount) => {
    try { store.data.budgets = await api('/api/budget', { method: 'POST', body: JSON.stringify({ category: cat, amount }) }); }
    catch (e) { alert('could not save budget: ' + e.message); }
    store.state.editing = null; rerender();
  };

  const expanded = store.state.expanded;
  const toggle = k => { store.state.expanded = expanded === k ? null : k; store.state.detailQ = ''; rerender(); };
  const dq = store.state.detailQ;
  const catRows = expanded ? rows.filter(t => key(t) === expanded) : [];
  const detailRows = catRows.filter(t => matches(t, dq));
  const detail = expanded && catRows.length ? el('section', { class: 'panel detail', style: { '--c': store.colorFor(mode, expanded) } },
    el('div', { class: 'row', style: { marginBottom: '8px' } },
      el('h3', { style: { margin: 0 } }, el('i', { class: 'dot' }), ' ', expanded, el('span', { class: 'muted' }, ` · ${dq ? detailRows.length + ' of ' : ''}${catRows.length} in ${monthLabel(month)}`)),
      el('span', { class: 'grow' }),
      el('a', { href: '#transactions', class: 'muted', onclick: e => { e.preventDefault(); store.state.filter = { q: '', month, account: mode === 'cat' ? '' : expanded, category: mode === 'cat' ? expanded : '' }; goto('transactions'); } }, 'open in Transactions ›'),
      el('button', { class: 'muted', style: { padding: '2px 8px' }, onclick: () => toggle(expanded), 'aria-label': 'close' }, '✕')),
    el('input', { class: 'detail-search', type: 'search', placeholder: 'search merchant…', value: dq, 'aria-label': 'search ' + expanded,
      oninput: e => { store.state.detailQ = e.target.value.toLowerCase(); rerender({ keepFocus: true }); } }),
    TxList({ rows: detailRows, colorFor: c => store.categoryColor(c), limit: dq ? 400 : 60, drag, onTap: txActions(rerender) })) : null;
  const isCurrent = month === ym(today());
  const spentLine = 'spent in ' + monthLabel(month);
  return el('div', {},
    MonthNav({ months: store.months, month, onChange: m => { store.state.month = m; store.state.expanded = null; store.state.detailQ = ''; rerender(); } }),
    el('div', { class: 'row', style: { justifyContent: 'center', marginBottom: '6px' } },
      Segmented({ options: [{ value: 'cat', label: 'by category' }, { value: 'acct', label: 'by account' }], value: mode, onChange: v => { store.state.mode = v; rerender(); } }),
      SyncButton({ compact: true, onDone: () => store.load().then(() => rerender()) })),
    Donut({ items: shaped.filter(i => i.value > 0), total, onSelect: toggle, selected: expanded,
      title: net < 0 ? '+' + money0(-net) : money0(net), titleClass: net < 0 ? 'down' : '',
      subtitle: monthLabel(month) }),
    CategoryCards({ items: shaped, total, hasPrev: !!prev, elapsed, onSelect: toggle, selected: expanded, droppable: byCat,
      editing: store.state.editing, onEdit: k => { store.state.editing = k; rerender(); }, onSaveBudget: saveBudget }),
    detail,
  );
}
