import { el } from '../lib/dom.js';
import { store, sumBy, api } from '../lib/store.js';
import { money0, monthLabel, ym, today } from '../lib/format.js';
import { MonthNav } from '../components/monthNav.js';
import { CategoryCards } from '../components/categoryCards.js';

// Budget view: every budgetable category as a card with its bar, whether or not it had spend this month.
export function BudgetView({ rerender }) {
  const { month } = store.state;
  const rows = store.inMonth(month);
  const cur = sumBy(rows, t => t.cat);
  const cats = store.categories.filter(c => c !== store.other);
  const budgets = store.budgets;
  const items = cats.map(c => ({ key: c, value: cur[c] || 0, color: store.categoryColor(c), count: rows.filter(t => t.cat === c).length,
    budget: budgets[c], suggest: store.avg3(c, month), budgetable: true, delta: 0 }));
  const total = budgets.__total__ || 0;                                   // one number: total monthly spend budget, independent of categories
  const spent = Object.values(cur).reduce((s, v) => s + v, 0);           // ALL spend (incl. Other) counts against it
  const otherSpend = cur[store.other] || 0;
  const elapsed = store.elapsed(month);
  const isCurrent = month === ym(today());
  const days = new Date(+month.slice(0, 4), +month.slice(5, 7), 0).getDate();
  const save = async (cat, amount) => {
    try { store.data.budgets = await api('/api/budget', { method: 'POST', body: JSON.stringify({ category: cat, amount }) }); }
    catch (e) { alert('could not save budget: ' + e.message); }
    store.state.editing = null; rerender();
  };
  const overall = total ? el('section', { class: 'panel overall' },
    el('div', { class: 'row' }, el('div', { class: 'grow' }, el('div', { class: 'muted' }, isCurrent ? `day ${+today().slice(8, 10)} of ${days}` : monthLabel(month)),
      el('div', { class: 'big num' }, `${money0(spent)} `, el('span', { class: 'muted' }, `of ${money0(total)} `), totalEditor())),
      el('div', { class: 'pct num' }, Math.round(100 * spent / total) + '%')),
    el('div', { class: 'budget' }, el('div', { class: 'bar' + (spent > total ? ' over' : spent > total * elapsed * 1.05 ? ' ahead' : '') },
      el('div', { class: 'fill', style: { width: Math.min(100, 100 * spent / total) + '%' } }),
      elapsed > 0 && elapsed < 1 ? el('div', { class: 'tick', style: { left: (elapsed * 100) + '%' } }) : null)),
    el('div', { class: 'muted' }, spent > total ? `over by ${money0(spent - total)}` : `${money0(total - spent)} left` + (elapsed > 0 && elapsed < 1 ? ` · on pace for ${money0(spent / elapsed)}` : '')),
    el('div', { class: 'stack', title: 'spend by category' }, ...[...items, { label: 'Other', value: otherSpend, color: store.categoryColor(store.other) }].filter(i => i.value > 0).map(i =>
      el('div', { class: 'seg', style: { width: (100 * i.value / Math.max(total, spent)) + '%', '--c': i.color }, title: `${i.label}: ${money0(i.value)}` }))),
    el('div', { class: 'legend' }, ...[...items, { label: 'Other', value: otherSpend, color: store.categoryColor(store.other) }].filter(i => i.value > 0).map(i => el('span', {}, el('i', { class: 'dot', style: { '--c': i.color } }), `${i.label || i.key} ${money0(i.value)}`))))
    : el('section', { class: 'panel overall' }, el('div', { class: 'row' }, el('div', { class: 'grow' }, el('div', { class: 'big num' }, `${money0(spent)} `, el('span', { class: 'muted' }, 'spent · ')), el('span', { class: 'muted' }, 'set a total monthly budget: ')), totalEditor()));
  function totalEditor() {
    if (store.state.editing === '__total__') {
      const input = el('input', { type: 'number', min: 0, step: 50, value: total || '', placeholder: 'monthly $', style: { width: '110px', padding: '3px 8px' },
        onkeydown: e => { if (e.key === 'Enter') save('__total__', +input.value); if (e.key === 'Escape') { store.state.editing = null; rerender(); } } });
      setTimeout(() => input.focus(), 0);
      return el('span', { class: 'row', style: { display: 'inline-flex', gap: '4px' } }, input, el('button', { class: 'primary', style: { padding: '3px 10px' }, onclick: () => save('__total__', +input.value) }, 'Save'));
    }
    return el('button', { class: 'budget-btn', onclick: () => { store.state.editing = '__total__'; rerender(); } }, '✎');
  }
  return el('div', {},
    MonthNav({ months: store.months, month, onChange: m => { store.state.month = m; rerender(); } }),
    overall,
    CategoryCards({ items, total: spent || 1, hasPrev: false, elapsed, selected: null, editing: store.state.editing,
      onSelect: () => {}, onEdit: k => { store.state.editing = k; rerender(); }, onSaveBudget: save }),
  );
}
