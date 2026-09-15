"""SQLite store. accounts / transactions / statements / meta."""
import hashlib
import json
import sqlite3
from datetime import date

from parsers import Parsed

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, institution TEXT, name TEXT, last_four TEXT);
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, date TEXT NOT NULL, description TEXT, amount REAL NOT NULL,
  status TEXT, type TEXT, category TEXT, counterparty TEXT, raw TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS statements (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, statement_date TEXT NOT NULL, balance REAL NOT NULL, due_date TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS budgets (category TEXT PRIMARY KEY, amount REAL NOT NULL);
CREATE TABLE IF NOT EXISTS imports (sha TEXT PRIMARY KEY, filename TEXT, account_id TEXT, imported_at TEXT, rows INTEGER);
CREATE TABLE IF NOT EXISTS overrides (tx_id TEXT PRIMARY KEY, category TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS merchant_cats (merchant TEXT PRIMARY KEY, category TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS renames (tx_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS merchant_names (merchant TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS dupes (tx_id TEXT PRIMARY KEY, created_at TEXT);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
"""


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False, timeout=30)   # wait for a concurrent sync instead of "database is locked"
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def short(bank: str) -> str:
    return {"Bank of America": "BofA"}.get(bank, bank)


def account_label(bank: str, last4) -> str:
    return short(bank) if not last4 else f"{short(bank)} ••{last4}"


def account_id(bank: str, last4: str | None) -> str:
    return f"{bank.lower().replace(' ', '')}_{last4 or 'xxxx'}"


def ensure_account(con, bank: str, last4: str | None) -> str:
    aid = account_id(bank, last4)
    con.execute("INSERT OR IGNORE INTO accounts VALUES (?,?,?,?)", (aid, bank, account_label(bank, last4), last4))
    return aid


def record_posted(con, bank: str, account_id_: str, p: Parsed) -> str:
    """CSV row → posted transaction. A pending alert row for the same amount within 3 days is replaced (alerts fire at
    authorization; the CSV is the bank's final word). Returns 'new' | 'upgraded' | 'dup'."""
    d = p.date.isoformat()
    amt = p.signed_amount
    tid = "csv_" + hashlib.sha1(f"{account_id_}|{d}|{amt:.2f}|{p.merchant.lower()}".encode()).hexdigest()[:20]
    if con.execute("SELECT 1 FROM transactions WHERE id=?", (tid,)).fetchone():
        return "dup"
    pend = con.execute("""SELECT id FROM transactions WHERE account_id=? AND status='pending' AND ABS(amount-?)<0.005
                          AND ABS(julianday(date)-julianday(?))<=3 LIMIT 1""", (account_id_, amt, d)).fetchone()
    if pend:
        con.execute("DELETE FROM transactions WHERE id=?", (pend["id"],))
    today = date.today().isoformat()
    con.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, account_id_, d, p.merchant, amt, "posted", p.txn_type, p.category, p.merchant,
                 json.dumps({"kind": p.kind, "source": "csv"}), today, today))
    return "upgraded" if pend else "new"


def upsert_statement(con, account_id_: str, statement_date: str, balance: float, due: str | None, authoritative: bool):
    """One row per statement cycle. A statement email arrives a day after the PDF's closing date, so anything within
    3 days is the same statement; the PDF (authoritative) wins on date, otherwise keep the earliest date seen."""
    near = con.execute("SELECT id, statement_date, due_date FROM statements WHERE account_id=? AND ABS(julianday(statement_date)-julianday(?))<=3",
                       (account_id_, statement_date)).fetchone()
    if near:
        keep_date = statement_date if authoritative else min(near["statement_date"], statement_date)
        con.execute("DELETE FROM statements WHERE id=?", (near["id"],))
        due = due or near["due_date"]
        statement_date = keep_date
    con.execute("INSERT INTO statements VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET balance=excluded.balance, due_date=COALESCE(excluded.due_date, due_date)",
                (f"{account_id_}:{statement_date}", account_id_, statement_date, balance, due))


def record(con, bank: str, p: Parsed, subject: str) -> str:
    """Write a Parsed to the ledger. Returns 'txn_new' | 'txn' | 'statement'. 'txn_new' only for a row that wasn't
    there before — sync re-reads 14 days of mail, so most calls are re-seen alerts and must not look new."""
    aid = ensure_account(con, bank, p.last4)
    if p.kind == "statement":
        upsert_statement(con, aid, p.date.isoformat(), p.balance, p.due.isoformat() if p.due else None, authoritative=False)
        return "statement"
    d = p.date.isoformat()
    amt = p.signed_amount
    if con.execute("""SELECT 1 FROM transactions WHERE account_id=? AND status='posted' AND id NOT LIKE 'mail\\_%' ESCAPE '\\' AND ABS(amount-?)<0.005
                      AND ABS(julianday(date)-julianday(?))<=3 LIMIT 1""", (aid, amt, d)).fetchone():
        return "txn"                                   # the bank's posted row is already here (CSV/PDF); the alert adds nothing
    # Only a bank-posted row shadows an alert, never another email. Two emails with the same amount a few days apart are two payments
    # (friends splitting a bill); a real duplicate gets marked by hand, which beats silently losing money.
    key = f"{bank}|{p.last4}|{d}|{amt:.2f}|{p.merchant.lower()}"
    tid = "mail_" + hashlib.sha1(key.encode()).hexdigest()[:20]
    ref = p.extra.get("ref")                           # the sender's own transaction id (Venmo), when the email has one
    if ref:
        legacy = con.execute("SELECT raw FROM transactions WHERE id=?", (tid,)).fetchone()
        if not legacy or json.loads(legacy["raw"] or "{}").get("ref"):   # rows recorded before refs keep their old id
            tid = "mail_" + hashlib.sha1(f"{bank}|ref|{ref}".encode()).hexdigest()[:20]
    new = con.execute("SELECT 1 FROM transactions WHERE id=?", (tid,)).fetchone() is None
    today = date.today().isoformat()
    con.execute("""INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
                (tid, aid, d, p.merchant, amt, "posted" if p.posted else "pending", p.txn_type, None, p.merchant,
                 json.dumps({"subject": subject, "kind": p.kind, "ref": ref} if ref else {"subject": subject, "kind": p.kind}), today, today))
    return "txn_new" if new else "txn"


def set_meta(con, key: str, value: str):
    con.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_meta(con, key: str, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def budgets(con) -> dict:
    return {r["category"]: r["amount"] for r in con.execute("SELECT category, amount FROM budgets")}


def set_budget(con, category: str, amount: float | None):
    if amount is None or amount <= 0:
        con.execute("DELETE FROM budgets WHERE category=?", (category,))
    else:
        con.execute("INSERT INTO budgets VALUES (?,?) ON CONFLICT(category) DO UPDATE SET amount=excluded.amount", (category, float(amount)))
    con.commit()


def overrides(con) -> dict:
    """tx id → category, set by dragging one transaction into another category. Outranks everything."""
    return {r["tx_id"]: r["category"] for r in con.execute("SELECT tx_id, category FROM overrides")}


def merchant_cats(con) -> dict:
    """report.merchant_key → category, set by dragging with "all from this merchant". Outranks rules.toml."""
    return {r["merchant"]: r["category"] for r in con.execute("SELECT merchant, category FROM merchant_cats")}


def set_override(con, tx_id: str, category: str | None):
    if category is None:
        con.execute("DELETE FROM overrides WHERE tx_id=?", (tx_id,))
    else:
        con.execute("INSERT INTO overrides VALUES (?,?,?) ON CONFLICT(tx_id) DO UPDATE SET category=excluded.category",
                    (tx_id, category, date.today().isoformat()))
    con.commit()


def set_merchant_cat(con, merchant: str, category: str | None):
    if category is None:
        con.execute("DELETE FROM merchant_cats WHERE merchant=?", (merchant,))
    else:
        con.execute("INSERT INTO merchant_cats VALUES (?,?,?) ON CONFLICT(merchant) DO UPDATE SET category=excluded.category",
                    (merchant, category, date.today().isoformat()))
    con.commit()


def names(con) -> tuple[dict, dict]:
    """(tx id → name, merchant_key → name): display names you gave a row or a whole merchant. Display only —
    categories keep matching the bank's own text, so renaming never moves money between categories."""
    return ({r["tx_id"]: r["name"] for r in con.execute("SELECT tx_id, name FROM renames")},
            {r["merchant"]: r["name"] for r in con.execute("SELECT merchant, name FROM merchant_names")})


def set_name(con, tx_id: str, name: str | None):
    if not name:
        con.execute("DELETE FROM renames WHERE tx_id=?", (tx_id,))
    else:
        con.execute("INSERT INTO renames VALUES (?,?,?) ON CONFLICT(tx_id) DO UPDATE SET name=excluded.name", (tx_id, name, date.today().isoformat()))
    con.commit()


def set_merchant_name(con, merchant: str, name: str | None):
    if not name:
        con.execute("DELETE FROM merchant_names WHERE merchant=?", (merchant,))
    else:
        con.execute("INSERT INTO merchant_names VALUES (?,?,?) ON CONFLICT(merchant) DO UPDATE SET name=excluded.name", (merchant, name, date.today().isoformat()))
    con.commit()


def dupes(con) -> set:
    """tx ids you marked as a duplicate: kept in the ledger, left out of every total."""
    return {r["tx_id"] for r in con.execute("SELECT tx_id FROM dupes")}


def set_dupe(con, tx_id: str, on: bool):
    if on:
        con.execute("INSERT OR IGNORE INTO dupes VALUES (?,?)", (tx_id, date.today().isoformat()))
    else:
        con.execute("DELETE FROM dupes WHERE tx_id=?", (tx_id,))
    con.commit()


def seen_file(con, sha: str):
    return con.execute("SELECT filename, imported_at FROM imports WHERE sha=?", (sha,)).fetchone()


def remember_file(con, sha: str, filename: str, account_id_: str, rows: int):
    con.execute("INSERT OR REPLACE INTO imports VALUES (?,?,?,?,?)", (sha, filename, account_id_, date.today().isoformat(), rows))
    con.commit()


def record_statement(con, account_id_: str, p: Parsed):
    upsert_statement(con, account_id_, p.date.isoformat(), p.balance, p.due.isoformat() if p.due else None, authoritative=True)


TRANSFERISH = ("transfer", "trnsfr", "payment", "autopay", "deposit", "withdrawal", "cashout", "ext trnsfr")


def drop_shadowed_pending(con) -> int:
    """Pending alert rows that have a posted twin (same account, amount, ±3 days) — left over from imports that predate the guard."""
    rows = con.execute("""SELECT p.id FROM transactions p WHERE p.status='pending' AND EXISTS (
                            SELECT 1 FROM transactions q WHERE q.status='posted' AND q.account_id=p.account_id
                            AND ABS(q.amount-p.amount)<0.005 AND ABS(julianday(q.date)-julianday(p.date))<=3)""").fetchall()
    con.executemany("DELETE FROM transactions WHERE id=?", [(r["id"],) for r in rows])
    con.commit()
    return len(rows)


def tidy(con) -> dict:
    """The two dedupe rules that matter, run after every sync/import. Idempotent.
    1. A row that names another account we track (its last-4) and looks like a transfer/payment → type=transfer.
    2. Money in on one account + the same amount out on a different account within 3 days, either side transfer-looking
       → both type=transfer.  Transfers are excluded from spend and income."""
    last4s = [r["last_four"] for r in con.execute("SELECT last_four FROM accounts WHERE last_four IS NOT NULL")]
    n1 = n2 = 0
    for r in con.execute("SELECT id, description, account_id FROM transactions WHERE type!='transfer'").fetchall():
        d = (r["description"] or "").lower()
        if "venmo" in d and not r["account_id"].startswith("venmo"):          # bank ↔ Venmo money movement; spend is counted from Venmo's own emails
            con.execute("UPDATE transactions SET type='transfer' WHERE id=?", (r["id"],)); n1 += 1; continue
        if any(k in d for k in TRANSFERISH) and any(l in d for l in last4s):
            con.execute("UPDATE transactions SET type='transfer' WHERE id=?", (r["id"],)); n1 += 1
    ins = con.execute("""SELECT id, account_id, date, amount, description FROM transactions
                         WHERE amount>0 AND type!='transfer' AND status='posted'""").fetchall()
    for a in ins:
        b = con.execute("""SELECT id, description FROM transactions WHERE account_id!=? AND ABS(amount+?)<0.005 AND type!='transfer'
                           AND ABS(julianday(date)-julianday(?))<=3 ORDER BY ABS(julianday(date)-julianday(?)) LIMIT 1""",
                        (a["account_id"], a["amount"], a["date"], a["date"])).fetchone()
        if not b:
            continue
        text = ((a["description"] or "") + " " + (b["description"] or "")).lower()
        if any(k in text for k in TRANSFERISH):
            con.executemany("UPDATE transactions SET type='transfer' WHERE id=?", [(a["id"],), (b["id"],)]); n2 += 1
    con.commit()
    return {"named_transfers": n1, "paired_transfers": n2, "shadowed_pending": drop_shadowed_pending(con)}
