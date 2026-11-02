"""To-do list: money you owe. Venmo request emails add items, your own payments clear them; you can also add, check off,
dismiss (✕) and reorder by hand. Rows carry updated_at so backups merge them newest-wins like every other hand edit."""
import hashlib
import re
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
  id TEXT PRIMARY KEY, text TEXT NOT NULL, who TEXT, amount REAL, note TEXT, date TEXT NOT NULL, source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', closed_by TEXT, closed_at TEXT, pos REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
"""
SHOW_CLOSED_DAYS = 3        # an item that cleared itself stays visible (checked) this long, so you see it happen


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _top(con) -> float:     # new items go above everything you already ordered
    return (con.execute("SELECT MIN(pos) FROM todos").fetchone()[0] or 0) - 1


def from_request(con, who: str, amount: float, note: str, d: date, reminder: bool = False) -> str:
    """A Venmo "X requests $Y" email. Returns 'new' | 'seen'. A reminder never adds a second item for a request we have."""
    if reminder and con.execute("SELECT 1 FROM todos WHERE source='venmo' AND lower(who)=lower(?) AND ABS(amount-?)<0.005 AND date<=?",
                                (who, amount, d.isoformat())).fetchone():
        return "seen"
    tid = "venmo_" + hashlib.sha1(f"{who.lower()}|{amount:.2f}|{note.lower()}|{d.isoformat()}".encode()).hexdigest()[:16]
    if con.execute("SELECT 1 FROM todos WHERE id=?", (tid,)).fetchone():
        return "seen"
    text = f"Pay {who}" + (f" · {note}" if note else "")
    con.execute("INSERT INTO todos (id, text, who, amount, note, date, source, pos, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, text, who, amount, note or None, d.isoformat(), "venmo", _top(con), _now()))
    return "new"


def canceled(con, who: str, amount: float, d: date):
    """ "X canceled a request for $Y": nothing to pay any more."""
    r = con.execute("""SELECT id FROM todos WHERE status='open' AND source='venmo' AND lower(who)=lower(?) AND ABS(amount-?)<0.005 AND date<=?
                       ORDER BY date LIMIT 1""", (who, amount, d.isoformat())).fetchone()
    if r:
        _close(con, r["id"], "done", "canceled", d.isoformat())


def add(con, text: str) -> str:
    """Your own item. "Elliott $24 dinner" → who Elliott, amount 24, so it clears itself when you pay him; anything else is plain text."""
    text = text.strip()
    m = re.match(r"^(?:pay\s+)?([A-Za-z][\w'.-]*(?:\s+[A-Za-z][\w'.-]*)?)\s+\$?(\d+(?:\.\d{1,2})?)\b\s*(.*)$", text, re.I)
    who, amount, note = (m.group(1), float(m.group(2)), m.group(3).strip(" -·")) if m else (None, None, None)
    tid = "todo_" + hashlib.sha1(f"{text}|{_now()}".encode()).hexdigest()[:16]
    con.execute("INSERT INTO todos (id, text, who, amount, note, date, source, pos, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, text, who, amount, note or None, date.today().isoformat(), "manual", _top(con), _now()))
    con.commit()
    return tid


def _close(con, tid, status, by, at=None):
    con.execute("UPDATE todos SET status=?, closed_by=?, closed_at=?, updated_at=? WHERE id=?",
                (status, by, at or date.today().isoformat(), _now(), tid))


def set_status(con, tid: str, status: str):
    """open | done (you checked it) | dismissed (✕). Reopening forgets what closed it."""
    if not con.execute("SELECT 1 FROM todos WHERE id=?", (tid,)).fetchone():
        raise KeyError("no such to-do")
    if status == "open":
        con.execute("UPDATE todos SET status='open', closed_by=NULL, closed_at=NULL, updated_at=? WHERE id=?", (_now(), tid))
    else:
        _close(con, tid, status, "you")
    con.commit()


def move(con, tid: str, before: str | None):
    """Put `tid` just above `before` (None = the bottom)."""
    order = [r["id"] for r in con.execute("SELECT id FROM todos WHERE status='open' ORDER BY pos, date DESC") if r["id"] != tid]
    order.insert(order.index(before) if before in order else len(order), tid)
    at = _now()
    con.executemany("UPDATE todos SET pos=?, updated_at=? WHERE id=?", [(i, at, t) for i, t in enumerate(order)])
    con.commit()


def _paid_to(desc: str, who: str) -> bool:
    """ "Venmo to Elliott Kalt" pays "Elliott Kalt", and "Elliott" (a name you typed) too."""
    m = re.match(r"(?:venmo|zelle) to (.+)$", desc or "", re.I)
    if not m:
        return False
    paid, who = m.group(1).strip().lower(), who.strip().lower()
    return paid == who or paid.startswith(who + " ")


def reconcile(con) -> int:
    """Clear every open item you've since paid: a Venmo/Zelle payment to that person, same amount, on or after the
    item's date (a day of slack). Each payment clears one item, oldest first. Returns how many cleared."""
    used = {r[0] for r in con.execute("SELECT closed_by FROM todos WHERE closed_by IS NOT NULL")}
    pays = con.execute("""SELECT id, description, date, amount FROM transactions WHERE amount<0
                          AND (description LIKE 'Venmo to %' OR description LIKE 'Zelle to %') ORDER BY date""").fetchall()
    n = 0
    for t in con.execute("SELECT id, who, amount, date FROM todos WHERE status='open' AND who IS NOT NULL AND amount IS NOT NULL ORDER BY date").fetchall():
        for p in pays:
            if p["id"] not in used and abs(-p["amount"] - t["amount"]) < 0.005 and _paid_to(p["description"], t["who"]) \
                    and (date.fromisoformat(p["date"]) - date.fromisoformat(t["date"])).days >= -1:
                _close(con, t["id"], "done", p["id"], p["date"]); used.add(p["id"]); n += 1
                break
    con.commit()
    return n


def listing(con) -> list[dict]:
    """Open items in your order, then the recently closed (so an auto-clear is visible for a few days)."""
    rows = con.execute(f"""SELECT * FROM todos WHERE status='open'
                           OR (status='done' AND closed_by!='you' AND closed_at >= date('now', '-{SHOW_CLOSED_DAYS} days'))
                           ORDER BY status='open' DESC, pos, date DESC""").fetchall()
    return [{k: r[k] for k in r.keys() if k != "updated_at"} for r in rows]


def merge(con, db: str = "b"):
    """Backup merge: rows missing here are added; a row both sides have goes to whichever changed it last."""
    con.execute(f"""INSERT INTO main.todos SELECT * FROM {db}.todos WHERE true
                    ON CONFLICT(id) DO UPDATE SET text=excluded.text, who=excluded.who, amount=excluded.amount, note=excluded.note,
                      status=excluded.status, closed_by=excluded.closed_by, closed_at=excluded.closed_at, pos=excluded.pos,
                      updated_at=excluded.updated_at WHERE excluded.updated_at > todos.updated_at""")
