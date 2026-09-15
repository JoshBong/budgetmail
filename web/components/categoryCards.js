import { el } from '../lib/dom.js';
import { money0, pct } from '../lib/format.js';

// Budget bar: fill = spent/budget, tick = where you "should" be today (elapsed fraction of the month).
function BudgetBar({ spent, budget, elapsed }) {
  const fill = Math.min(1, spent / budget), expected = budget * elapsed;
  const over = spent > budget, ahead = !over && spent > expected * 1.05;
  const left = budget - spent;
  const pace = elapsed > 0 && elapsed < 1 ? spent / elapsed : null;
  const status = over ? `+${money0(spent - budget)} over` : `${money0(left)} left`;
  return el('div', { class: 'budget', title: pace != null && !over ? `on pace for ${money0(pace)}` : null },
    el('div', { class: 'bar' + (over ? ' over' : ahead ? ' ahead' : '') },
      el('div', { class: 'fill', style: { width: (fill * 100) + '%' } }),
      elapsed > 0 && elapsed < 1 ? el('div', { class: 'tick', style: { left: (elapsed * 100) + '%' }, title: 'where you should be today' }) : null),
    el('div', { class: 'sub' }, el('span', {}, `${money0(spent)} / ${money0(budget)}`), el('span', { class: over ? 'up' : ahead ? 'warn' : '' }, status)));
}

// Inline editor shown on the card when editing: number input + "use avg" + save/remove.
function BudgetEditor({ value, suggest, onSave, onCancel }) {
  const input = el('input', { type: 'number', min: 0, step: 10, value: value || '', placeholder: 'monthly $', inputmode: 'decimal',
    onkeydown: e => { if (e.key === 'Enter') onSave(+input.value); if (e.key === 'Escape') onCancel(); }, onclick: e => e.stopPropagation() });
  setTimeout(() => input.focus(), 0);
  return el('div', { class: 'editor', onclick: e => e.stopPropagation() }, input,
    suggest > 0 ? el('button', { class: 'muted', onclick: () => { input.value = Math.ceil(suggest / 10) * 10; } }, `avg ${money0(suggest)}`) : null,
    el('button', { class: 'primary', onclick: () => onSave(+input.value) }, 'Save'),
    value ? el('button', { onclick: () => onSave(0) }, 'Remove') : null,
    el('button', { onclick: onCancel }, '✕'));
}

// card-width names so the header stays on one line
const SHORT = { 'Bills & Subscriptions': 'Bills & Subs' };

// CategoryCards({ items:[{key,label,value,color,count,delta,budget,suggest}], total, hasPrev, elapsed, onSelect, selected, editing, onEdit, onSaveBudget, droppable })
export function CategoryCards({ items, total, hasPrev, elapsed, onSelect, selected, editing, onEdit, onSaveBudget, droppable }) {
  if (!items.length) return el('div', { class: 'empty' }, 'no spending recorded this month');
  const cards = el('div', { class: 'cards' }, ...items.map(it => {
    const canBudget = it.budgetable !== false;
    return el('div', { class: 'card' + (it.key === selected ? ' on' : ''), style: { '--c': it.color }, role: 'button', tabindex: 0,
      'aria-expanded': it.key === selected, 'data-drop-cat': droppable ? it.key : null, onclick: () => onSelect?.(it.key) },
      el('div', { class: 'name', title: it.label ?? it.key }, el('i', { class: 'dot' }), el('span', { class: 'grow' }, it.label ?? SHORT[it.key] ?? it.key),
        canBudget ? el('button', { class: 'budget-btn', title: it.budget ? 'edit budget' : 'set a budget', 'aria-label': it.budget ? 'edit budget' : 'set a budget', onclick: e => { e.stopPropagation(); onEdit?.(it.key); } }, '✎') : null),
      el('div', { class: 'value num' + (it.value < 0 ? ' down' : '') }, it.value < 0 ? '+' + money0(-it.value) : money0(it.value)),
      editing === it.key
        ? BudgetEditor({ value: it.budget, suggest: it.suggest, onSave: v => onSaveBudget?.(it.key, v), onCancel: () => onEdit?.(null) })
        : it.budget
          ? BudgetBar({ spent: it.value, budget: it.budget, elapsed })
          : el('div', { class: 'sub' },
              el('span', {}, it.value < 0 ? '' : pct(it.value, total) + ' of total'),
              hasPrev ? el('span', { class: it.delta > 0 ? 'up' : 'down' }, (it.delta > 0 ? '▲ ' : '▼ ') + money0(it.delta)) : el('span', {}, it.count + ' txns')));
  }));
  // once laid out at their natural width, size every card to the widest one (capped at the row width)
  requestAnimationFrame(() => {
    const w = Math.max(...[...cards.children].map(c => c.offsetWidth));
    cards.style.setProperty('--card-w', Math.min(w, cards.clientWidth) + 'px');
  });
  return cards;
}
