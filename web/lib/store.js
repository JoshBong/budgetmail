// Loads /api/data once and exposes derived views + UI state. Views subscribe to `state` changes via render().
import { ym, today, monthRange } from './format.js';

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'];
const CATEGORY_COLORS = {
  'Food & Dining': 'var(--s2)', Shopping: 'var(--s5)', Travel: 'var(--s1)', Transport: 'var(--s4)',
  'Bills & Subscriptions': 'var(--s7)', People: 'var(--s3)', Other: 'var(--gray)',
};

export const store = {
  data: null,
  spend: [],            // rows that count as spending (purchases, refunds, zelle out) — excludes transfers/ignored
  months: [],           // continuous 'YYYY-MM' list, newest first
  accountColor: {},
  state: { tab: 'spending', month: ym(today()), mode: 'cat', expanded: null, detailQ: '', editing: null, trend: 'total', filter: { q: '', month: '', account: '', category: '' } },

  async load() {
    const r = await fetch('/api/data');
    if (!r.ok) throw new Error('data ' + r.status);
    this.data = await r.json();
    this.spend = this.data.tx.filter(t => !t.ignore && (t.pinned || t.type !== 'transfer'));   // pinned = you dragged it here; it counts
    const seen = new Set(this.spend.map(t => ym(t.date)));
    seen.add(ym(today()));
    const sorted = [...seen].sort();
    this.months = monthRange(sorted[0], sorted[sorted.length - 1]);
    this.accountColor = Object.fromEntries(this.data.accounts.map((a, i) => [a.name, `var(${SLOTS[i % 8]})`]));
    return this;
  },

  get budgets() { return this.data.budgets || {}; },
  // fraction of the month elapsed (1 for past months, 0..1 for the current one)
  elapsed(m) {
    const t = today();
    if (m < ym(t)) return 1;
    if (m > ym(t)) return 0;
    const days = new Date(+m.slice(0, 4), +m.slice(5, 7), 0).getDate();
    return +t.slice(8, 10) / days;
  },
  // average monthly spend for a category over the 3 months before `m`
  avg3(cat, m) {
    const i = this.months.indexOf(m);
    const prev = this.months.slice(i + 1, i + 4);
    if (!prev.length) return 0;
    return prev.reduce((s, pm) => s + this.inMonth(pm).filter(t => t.cat === cat).reduce((a, t) => a - t.amount, 0), 0) / prev.length;
  },
  get categories() { return this.data.categories; },
  get other() { return this.data.categories[this.data.categories.length - 1]; },
  categoryColor(c) { return CATEGORY_COLORS[c] || 'var(--gray)'; },
  colorFor(mode, key) { return mode === 'cat' ? this.categoryColor(key) : this.accountColor[key] || 'var(--gray)'; },
  keyFor(mode) { return mode === 'cat' ? (t => t.cat) : (t => t.card); },

  inMonth(m) { return this.spend.filter(t => ym(t.date) === m); },
  prevMonth(m) { const i = this.months.indexOf(m); return i >= 0 ? this.months[i + 1] : undefined; },
};

// Search box match: the name you see, the bank's original text, or the category.
export const matches = (t, q) => !q || (t.merchant + ' ' + t.orig + ' ' + t.cat).toLowerCase().includes(q);

// Sum of spend (positive = money out) grouped by key.
export function sumBy(rows, key) {
  const out = {};
  for (const t of rows) out[key(t)] = (out[key(t)] || 0) - t.amount;
  return out;
}

export async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error(j.error || r.status);
  return j;
}
