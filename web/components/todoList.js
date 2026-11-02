import { el } from '../lib/dom.js';
import { store, api } from '../lib/store.js';
import { money } from '../lib/format.js';
import { toast } from './catRail.js';

const SHOW = 5;
let expandedList = false;    // "show all" survives rerenders
let folded = (() => { try { return localStorage.getItem('todoFolded') === '1'; } catch { return false; } })();
const setFolded = v => { folded = v; try { localStorage.setItem('todoFolded', v ? '1' : '0'); } catch {} };

const post = body => api('/api/todo', { method: 'POST', body: JSON.stringify(body) });
const ago = iso => {
  const d = Math.round((new Date(new Date().toDateString()) - new Date(iso + 'T00:00')) / 864e5);
  return d <= 0 ? 'today' : d === 1 ? 'yesterday' : d < 60 ? d + ' days' : Math.round(d / 30) + ' months';
};
const short = iso => new Date(iso + 'T00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

// Drag the ⋮⋮ handle to reorder. The row moves through the DOM as the pointer crosses a neighbour's middle;
// on release the new position is saved as "above the row now below it".
function reorderable(handle, row, list, onDrop) {
  handle.addEventListener('pointerdown', e => {
    if (e.button > 0) return;
    e.preventDefault();                                   // listeners sit on window: moving the row through the DOM drops pointer capture
    row.classList.add('lifting');
    const start = [...list.children].indexOf(row);
    const move = ev => {
      const sibs = [...list.querySelectorAll('.todo.open')].filter(n => n !== row);
      const below = sibs.find(n => { const r = n.getBoundingClientRect(); return ev.clientY < r.top + r.height / 2; });
      if (below) { if (row.nextElementSibling !== below) list.insertBefore(row, below); }
      else if (sibs.length && row !== sibs[sibs.length - 1].nextElementSibling) sibs[sibs.length - 1].after(row);
    };
    const up = () => {
      row.classList.remove('lifting');
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', up);
      if ([...list.children].indexOf(row) === start) return;
      const next = row.nextElementSibling;
      onDrop(next?.classList.contains('open') ? next.dataset.id : null);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
  });
}

// TodoList({ rerender }) — money you owe, on top of Spending. Venmo requests add rows, your payments check them off;
// ○ = you paid it, ✕ = not yours to pay, ⋮⋮ = reorder, + = your own ("Elliott $24 dinner" clears itself when you pay him).
export function TodoList({ rerender }) {
  const items = store.data.todos || [];
  const open = items.filter(t => t.status === 'open');
  const adding = store.state.addingTodo;
  if (!items.length && !adding) return null;

  const save = list => { store.data.todos = list; rerender(); };
  const act = async (t, action, said) => {
    try { save(await post({ action, id: t.id })); }
    catch (e) { return toast('could not update: ' + e.message); }
    toast(said, { label: 'Undo', onClick: async () => save(await post({ action: 'open', id: t.id }).catch(() => store.data.todos)) });
  };

  const list = el('ul', { class: 'todos', role: 'list' });
  const shown = expandedList ? items : [...open.slice(0, SHOW), ...items.filter(t => t.status !== 'open')];
  for (const t of shown) {
    const done = t.status !== 'open';
    const auto = done && t.closed_by && t.closed_by !== 'you';
    const meta = done
      ? (t.closed_by === 'canceled' ? `request canceled · ${short(t.closed_at)}` : `paid ${short(t.closed_at)}${auto ? ' · cleared itself' : ''}`)
      : [t.note && t.who ? t.note : null, t.source === 'venmo' ? 'Venmo request' : 'added', ago(t.date)].filter(Boolean).join(' · ');
    const title = t.who ? t.who : t.text;
    const handle = done ? el('span') : el('span', { class: 'todo-grip', 'aria-hidden': 'true', title: 'drag to reorder' }, '⋮⋮');
    const row = el('li', { class: 'todo' + (done ? ' done' : ' open'), 'data-id': t.id },
      handle,
      el('button', { class: 'todo-check', role: 'checkbox', 'aria-checked': String(done), 'aria-label': done ? 'mark not paid' : 'mark paid',
        onclick: () => done ? act(t, 'open', 'back on the list') : act(t, 'done', `${title} · paid`) }),
      el('div', { class: 'todo-body' },
        el('div', { class: 'todo-title' }, title),
        el('div', { class: 'todo-meta' }, meta)),
      t.amount != null ? el('div', { class: 'todo-amt num' }, money(t.amount)) : el('div'),
      done ? el('div') : el('button', { class: 'todo-x', 'aria-label': 'remove ' + title, title: 'not mine to pay', onclick: () => act(t, 'dismissed', `${title} · removed`) }, '✕'));
    if (!done) reorderable(handle, row, list, async before => {
      try { save(await post({ action: 'move', id: t.id, before })); } catch (e) { toast('could not reorder: ' + e.message); rerender(); }
    });
    list.append(row);
  }

  const input = adding ? el('input', { class: 'todo-input', placeholder: 'Elliott $24 dinner', 'aria-label': 'new to-do', enterkeyhint: 'done',
    onkeydown: async e => {
      if (e.key === 'Escape') { store.state.addingTodo = false; rerender(); }
      if (e.key !== 'Enter' || !e.target.value.trim()) return;
      try { store.data.todos = await post({ action: 'add', text: e.target.value }); } catch (ex) { return toast('could not add: ' + ex.message); }
      store.state.addingTodo = false; rerender();
    } }) : null;
  if (input) setTimeout(() => input.focus(), 0);

  const owed = open.reduce((s, t) => s + (t.amount || 0), 0);
  const fold = folded && !adding;
  return el('section', { class: 'panel todo-panel' + (fold ? ' folded' : ''), 'aria-label': 'to pay' },
    el('div', { class: 'todo-head' },
      el('button', { class: 'todo-fold', 'aria-expanded': String(!fold), onclick: () => { setFolded(!fold); rerender(); } },
        el('span', { class: 'chev', 'aria-hidden': 'true' }, '▶'),
        el('h3', {}, 'To pay', open.length ? el('span', { class: 'todo-count' }, open.length) : null),
        owed ? el('span', { class: 'num todo-owed' }, money(owed)) : null),
      el('span', { class: 'grow' }),
      el('button', { class: 'todo-add', 'aria-label': 'add a to-do', title: 'add your own', onclick: () => { store.state.addingTodo = !adding; rerender(); } }, adding ? '✕' : '+')),
    input ? el('div', { class: 'todo-new' }, input, el('div', { class: 'todo-hint' }, 'name and amount clear it when you pay them · Enter to add')) : null,
    items.length && !fold ? list : null,
    open.length > SHOW && !fold ? el('button', { class: 'todo-more', onclick: () => { expandedList = !expandedList; rerender(); } },
      expandedList ? 'show less' : `${open.length - SHOW} more`) : null);
}

// "+ to pay" pill for the Spending controls row when the list is empty (the panel itself only exists with items in it)
export function TodoAddPill({ rerender }) {
  if ((store.data.todos || []).length || store.state.addingTodo) return null;
  return el('button', { class: 'todo-pill', title: 'add something you owe', onclick: () => { store.state.addingTodo = true; rerender(); } }, '+ to pay');
}
