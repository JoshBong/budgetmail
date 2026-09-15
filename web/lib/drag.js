// Pointer-based drag. One engine for mouse and touch: mouse starts after 6px of movement, touch after a
// 320ms long-press (so a normal swipe still scrolls the list). Drop targets are any element carrying
// [data-drop-cat]; they light up under the pointer.
const HOLD_MS = 320, SLOP = 6;

let session = null;   // { item, ghost, source, target, dx, dy, opts }
let lastEnd = 0;      // a drag's pointerup can still land a click on the row: taps check justDragged()

const swallow = e => e.preventDefault();   // while dragging with a finger, the page must not scroll under it

function targetAt(x, y) {
  for (const node of document.elementsFromPoint(x, y)) {
    const t = node.closest?.('[data-drop-cat]');
    if (t) return t;
  }
  return null;
}

function highlight(t) {
  if (session.target === t) return;
  session.target?.classList.remove('over');
  session.target = t;
  t?.classList.add('over');
}

function begin(e, node, item, opts) {
  const r = node.getBoundingClientRect();
  const ghost = node.cloneNode(true);
  ghost.className = 'drag-ghost ' + node.className;
  ghost.style.width = Math.min(r.width, 280) + 'px';
  document.body.append(ghost);
  const g = ghost.getBoundingClientRect();
  node.classList.add('dragging');
  document.body.classList.add('drag-active');
  document.addEventListener('touchmove', swallow, { passive: false });
  session = { item, ghost, source: node, target: null, w: g.width, h: g.height, opts };
  opts.onStart?.(item);
  move(e);
}

// The ghost rides just above the pointer, never under it — whatever you are about to drop onto stays visible.
function move(e) {
  const s = session;
  s.ghost.style.transform = `translate(${e.clientX - s.w / 2}px, ${e.clientY - s.h - 14}px)`;
  s.ghost.style.visibility = 'hidden';                     // don't hit-test the ghost itself
  highlight(targetAt(e.clientX, e.clientY));
  s.ghost.style.visibility = '';
}

function end(drop) {
  const { ghost, source, target, item, opts } = session;
  ghost.remove();
  source.classList.remove('dragging');
  target?.classList.remove('over');
  document.body.classList.remove('drag-active');
  document.removeEventListener('touchmove', swallow);
  session = null;
  lastEnd = Date.now();
  opts.onEnd?.();
  if (drop && target) opts.onDrop(item, target.dataset.dropCat, target);
}

// makeDraggable(node, item, { onDrop(item, category, targetEl), onStart, onEnd })
export function makeDraggable(node, item, opts) {
  node.addEventListener('pointerdown', e => {
    if (e.button > 0 || session) return;
    const touch = e.pointerType !== 'mouse';
    const start = { x: e.clientX, y: e.clientY };
    let timer = null;

    const onMove = ev => {
      if (session) { ev.preventDefault(); move(ev); return; }
      if (Math.hypot(ev.clientX - start.x, ev.clientY - start.y) <= SLOP) return;
      if (touch) cancel();                                 // moved before the hold finished: they're scrolling
      else begin(ev, node, item, opts);
    };
    const onUp = () => { if (session) end(true); done(); };
    const cancel = () => { if (session) end(false); done(); };
    const done = () => {                                 // must run on every exit, drop included:
      clearTimeout(timer);                               // a listener left on window would fire on the NEXT
      node.classList.remove('holding');                  // drag with this row's stale closure
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', cancel);
    };

    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', cancel);
    if (touch) {
      node.classList.add('holding');
      timer = setTimeout(() => { node.classList.remove('holding'); begin(e, node, item, opts); navigator.vibrate?.(8); }, HOLD_MS);
    }
  });
}

export const dragging = () => !!session;
export const justDragged = () => !!session || Date.now() - lastEnd < 400;
