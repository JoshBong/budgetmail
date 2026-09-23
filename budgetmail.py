#!/usr/bin/env python3
"""budgetmail — bank alert emails → ledger → dashboard. One process: hourly sync + web UI.

  python3 budgetmail.py setup       interactive: Gmail login (tested), accounts, port
  python3 budgetmail.py sync        pull mail now (--full = whole mailbox)
  python3 budgetmail.py import <files…>   backfill from bank CSV exports or statement PDFs (account auto-detected; --account to force)
  python3 budgetmail.py backup [path]     zip of ledger + rules + config (no password) → move to another machine
  python3 budgetmail.py backup --mail     email that zip to yourself (the service also does this weekly)
  python3 budgetmail.py restore <zip>     merge a backup into this machine's ledger (adds history + newer edits, never deletes)
  python3 budgetmail.py restore --from-mail   same, merging every recent backup email in your Gmail
  python3 budgetmail.py classify    run the local merchant classifier now over everything still in Other (--dry to preview)
  python3 budgetmail.py serve       run forever: sync every N minutes + dashboard on :PORT
  python3 budgetmail.py install     register `serve` as a system service (systemd / launchd) and start it
  python3 budgetmail.py uninstall
  python3 budgetmail.py stop | start  pause / resume the installed service (registration stays)
  python3 budgetmail.py status      service state, last sync, ledger totals, URL
  python3 budgetmail.py doctor      check python, config, Gmail login, database, service, port
  python3 budgetmail.py config      show config (password masked)
  python3 budgetmail.py config set port 8090 | sync_interval_min 30 | default_checking.Chase 1234 | classifier.model qwen2.5:3b
  python3 budgetmail.py config gmail        re-enter Gmail login (tested)
  python3 budgetmail.py update      git pull, run tests, restart the service

`./budgetmail <cmd>` is the same thing.
"""
import argparse
import getpass
import json
import os
import plistlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import classify
import config
import ledger
import mail
import parsers

HERE = Path(__file__).resolve().parent
SERVICE = "budgetmail"


# ---------------------------------------------------------------- events (SSE)
_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()


def publish(event: dict):
    """Push an event to every open /api/events stream. No-op when nothing is listening (CLI sync)."""
    with _sub_lock:
        subs = list(_subscribers)
    for q in subs:
        q.put(json.dumps(event, separators=(",", ":")))


# ---------------------------------------------------------------- sync
def sync(con, cfg: dict, full: bool = False) -> dict:
    parsers.configure(cfg.get("default_checking", {}))
    since = None if full else date.today() - timedelta(days=14)
    counts = {"txn": 0, "statement": 0, "skip": 0, "unparsed": 0, "unknown_sender": 0}
    new_txns = 0
    config.ensure_home()
    if full and config.FAILURES.exists():
        config.FAILURES.unlink()                                   # a full re-scan rebuilds the unparsed list from scratch
    with open(config.FAILURES, "a") as failures:
        for e in mail.fetch(cfg["gmail_user"], cfg["gmail_app_password"], since):
            bank = parsers.for_sender(e.sender)
            if not bank:
                counts["unknown_sender"] += 1
                continue
            p = bank.parse(e)
            if p is None:
                counts["unparsed"] += 1
                failures.write(json.dumps({"bank": bank.name, "subject": e.subject, "date": e.received.isoformat(), "body": e.body[:1500]}) + "\n")
            elif p.kind == "skip":
                counts["skip"] += 1
            else:
                k = ledger.record(con, bank.name, p, e.subject)
                if k == "txn_new":
                    k = "txn"
                    new_txns += 1
                counts[k] += 1
                if (counts["txn"] + counts["statement"]) % 50 == 0:
                    con.commit()                                       # short transactions: a full sync must not lock the DB for minutes
    ledger.tidy(con)
    try:
        r = classify.run(con, cfg)                                 # merchants still in Other → the local model, once each
        if r.get("classified") or r.get("abstained"):
            counts["classified"], counts["abstained"] = r["classified"], r["abstained"]
    except Exception as ex:
        print("classifier failed:", ex, flush=True)                # never let the model take the sync down
    ledger.set_meta(con, "last_sync", datetime.now().isoformat(timespec="seconds"))
    ledger.set_meta(con, "last_counts", json.dumps(counts))
    con.commit()
    if new_txns:
        publish({"new_txns": new_txns})                            # only genuinely new rows wake open dashboards
    return counts


# ---------------------------------------------------------------- file import (csv / pdf)
def pdf_to_text(data: bytes) -> str:
    import shutil, tempfile
    # services (launchd/systemd) run with a minimal PATH; look in the usual install spots too
    exe = shutil.which("pdftotext") or next((p for p in ("/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext", "/usr/bin/pdftotext") if os.path.exists(p)), None)
    if not exe:
        raise ValueError("PDF import needs pdftotext (poppler): macOS `brew install poppler` · Debian/Pi `sudo apt install poppler-utils`")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data); path = f.name
    try:
        r = subprocess.run([exe, "-layout", path, "-"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise ValueError("pdftotext failed: " + r.stderr.strip()[:200])
        return r.stdout
    finally:
        os.unlink(path)


def detect_account(con, filename: str, text: str, bank=None):
    """Account id from the filename (Chase: 20260821-statements-2637-.pdf / Chase2637_Activity.csv) or the text; else None."""
    accts = [dict(r) for r in con.execute("SELECT * FROM accounts")]
    if bank:
        accts = [a for a in accts if a["institution"] == bank.name]
    for m in re.finditer(r"(?<!\d)(\d{4})(?!\d)", filename):
        for a in accts:
            if a["last_four"] == m.group(1):
                return a["id"]
    for pat in (r"\(\s*\.{3}\s*(\d{4})\s*\)", r"Account Number:\s*(?:X{4}\s*){3}(\d{4})", r"Account (?:number|#):?\s*(?:\d{4}\s+){2}(\d{4})", r"ending in\s*(\d{4})"):
        for m in re.finditer(pat, text, re.I):
            for a in accts:
                if a["last_four"] == m.group(1):
                    return a["id"]
    return accts[0]["id"] if len(accts) == 1 else None


def import_file(con, filename: str, data: bytes, account_id_: str | None = None, force: bool = False) -> dict:
    import csv, hashlib, io
    sha = hashlib.sha256(data).hexdigest()
    prev = ledger.seen_file(con, sha)
    if prev and not force:
        return {"skipped": True, "reason": f"already imported as {prev['filename']} on {prev['imported_at']}", "new": 0, "upgraded": 0, "dup": 0}
    is_pdf = data[:5] == b"%PDF-" or filename.lower().endswith(".pdf")
    banks = list(parsers._REGISTRY.values())
    if is_pdf:
        text = pdf_to_text(data)
        bank, rows_iter = None, None
        for b in banks:
            try:
                rows_iter = list(b.pdf_rows(text, filename)); bank = b; break
            except ValueError:
                continue
        if not bank:
            raise ValueError("no parser recognised this PDF (Chase card + checking statements are supported)")
    else:
        text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            raise ValueError("empty file")
        bank = next((b for b in banks if b.sniff_csv(rows[0])), None)
        if not bank:   # BofA headers sit below a preamble
            bank = next((b for b in banks if any(b.sniff_csv(r) for r in rows[:12])), None)
        if not bank:
            raise ValueError("no parser recognised this CSV header")
        rows_iter = list(bank.csv_rows(rows[0], rows[1:]))
    acct = account_id_ or detect_account(con, filename, text, bank)
    if not acct:
        ids = [r["id"] for r in con.execute("SELECT id FROM accounts WHERE institution=?", (bank.name,))]
        raise ValueError(f"which {bank.name} account is this? choose one of: {', '.join(ids)}")
    if not con.execute("SELECT 1 FROM accounts WHERE id=?", (acct,)).fetchone():
        raise ValueError(f"unknown account {acct}")
    counts = {"new": 0, "upgraded": 0, "dup": 0, "statements": 0, "account": acct, "bank": bank.name, "skipped": False}
    for p in rows_iter:
        if p.kind == "statement":
            ledger.record_statement(con, acct, p); counts["statements"] += 1
        else:
            counts[ledger.record_posted(con, bank.name, acct, p)] += 1
    con.commit()
    counts["transfers"] = sum(ledger.tidy(con).values())
    ledger.remember_file(con, sha, filename, acct, counts["new"])
    return counts


# ---------------------------------------------------------------- backup / restore
def recategorize(con, tx_id: str, category: str | None, scope: str) -> dict:
    """Move a transaction into another category, by hand, from the dashboard.
      scope 'one'       → this transaction only (an override row).
      scope 'merchant'  → every transaction from the same merchant, past and future (a merchant rule).
                          Per-row overrides on that merchant are cleared — the rule subsumes them.
    Returns {changed, merchant, revert}; `revert` is the body to POST to /api/category/revert to undo it."""
    import report
    row = con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if row is None:
        raise KeyError("no such transaction")
    key = report.merchant_key(row)
    if scope == "one":
        prev = ledger.overrides(con).get(tx_id)
        ledger.set_override(con, tx_id, category)
        return {"changed": 1, "merchant": key, "revert": {"scope": "one", "id": tx_id, "category": prev}}
    ids = [r["id"] for r in con.execute("SELECT * FROM transactions") if report.merchant_key(r) == key]
    prev_over = {i: c for i, c in ledger.overrides(con).items() if i in ids}
    prev_cat = ledger.merchant_cats(con).get(key)
    for i in prev_over:
        ledger.set_override(con, i, None)
    ledger.set_merchant_cat(con, key, category)
    return {"changed": len(ids), "merchant": key,
            "revert": {"scope": "merchant", "id": tx_id, "merchant": key, "category": prev_cat, "overrides": prev_over}}


def revert_recategorize(con, r: dict):
    """Undo one recategorize() using the `revert` blob it returned."""
    if r.get("scope") == "one":
        ledger.set_override(con, r["id"], r.get("category"))
        return {"changed": 1}
    ledger.set_merchant_cat(con, r["merchant"], r.get("category"))
    for i, c in (r.get("overrides") or {}).items():
        ledger.set_override(con, i, c)
    return {"changed": 1}


def rename(con, tx_id: str, name: str | None, scope: str) -> dict:
    """Give a transaction a readable name. scope 'one' → this row; 'merchant' → every row from the same merchant, past
    and future (per-row names on it are cleared, the merchant name subsumes them). A blank name resets to the bank's text.
    Returns {changed, revert}; `revert` is the body to POST to /api/rename/revert."""
    import report
    row = con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if row is None:
        raise KeyError("no such transaction")
    name = (name or "").strip()[:80] or None
    by_row, by_merch = ledger.names(con)
    if scope == "one":
        ledger.set_name(con, tx_id, name)
        return {"changed": 1, "revert": {"scope": "one", "id": tx_id, "name": by_row.get(tx_id)}}
    key = report.merchant_key(row)
    ids = [r["id"] for r in con.execute("SELECT * FROM transactions") if report.merchant_key(r) == key]
    prev_rows = {i: n for i, n in by_row.items() if i in ids}
    for i in prev_rows:
        ledger.set_name(con, i, None)
    ledger.set_merchant_name(con, key, name)
    return {"changed": len(ids), "revert": {"scope": "merchant", "merchant": key, "name": by_merch.get(key), "rows": prev_rows}}


def revert_rename(con, r: dict):
    """Undo one rename() using the `revert` blob it returned."""
    if r.get("scope") == "one":
        ledger.set_name(con, r["id"], r.get("name"))
        return {"changed": 1}
    ledger.set_merchant_name(con, r["merchant"], r.get("name"))
    for i, n in (r.get("rows") or {}).items():
        ledger.set_name(con, i, n)
    return {"changed": 1}


def make_backup() -> bytes:
    """Zip of everything that is state: ledger.db, rules.toml, config.json minus the Gmail password."""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if config.DB.exists():
            z.write(config.DB, "ledger.db")
        if (HERE / "rules.toml").exists():
            z.write(HERE / "rules.toml", "rules.toml")
        if config.CONFIG.exists():
            c = json.loads(config.CONFIG.read_text()); c.pop("gmail_app_password", None)
            z.writestr("config.json", json.dumps(c, indent=2))
        z.writestr("README.txt", "budgetmail backup. Restore: drop this zip on the Data tab, or `./budgetmail restore <zip>`. "
                                 "The Gmail app password is not included; the restoring machine keeps its own.")
    return buf.getvalue()


def restore_backup(data: bytes, con=None, bak: bool = True) -> dict:
    """Merge a backup zip into this ledger (ledger.merge): nothing local is lost, whichever machine made the backup and
    however old it is. rules.toml comes along only where this machine has none (or just the example); config only fills
    keys this machine doesn't have, so its Gmail login and port stay. Returns counts + the connection."""
    import io, shutil, tempfile, zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        if "ledger.db" not in names:
            raise ValueError("not a budgetmail backup (no ledger.db inside)")
        config.ensure_home()
        con = con or ledger.connect(str(config.DB))
        con.commit()
        if bak and config.DB.exists():
            shutil.copy(config.DB, config.DB.with_suffix(".db.bak"))            # one-step undo
        with tempfile.TemporaryDirectory() as tmp:
            theirs = Path(tmp) / "ledger.db"
            theirs.write_bytes(z.read("ledger.db"))
            added = ledger.merge(con, str(theirs))
        rules, example = HERE / "rules.toml", HERE / "rules.example.toml"
        if "rules.toml" in names and (not rules.exists() or (example.exists() and rules.read_bytes() == example.read_bytes())):
            rules.write_bytes(z.read("rules.toml"))
        if "config.json" in names:
            cur = json.loads(config.CONFIG.read_text()) if config.CONFIG.exists() else {}
            config.save({**json.loads(z.read("config.json")), **cur})
    n = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return {"merged": True, "added": added, "total": n, "con": con}


def mail_backup(con, cfg: dict) -> str:
    """Email a backup to yourself; remember when, so the serve loop can space them out."""
    con.commit()
    con.execute("PRAGMA wal_checkpoint(FULL)")                              # the zip reads the .db file, not the connection
    n = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    subject = mail.send_backup(cfg["gmail_user"], cfg["gmail_app_password"], make_backup(), n)
    ledger.set_meta(con, "last_mail_backup", datetime.now().isoformat(timespec="seconds"))
    con.commit()                                                            # uncommitted, a restart forgot it and mailed again
    return subject


def restore_from_mail(cfg: dict, yes: bool = False) -> dict | None:
    """Merge every recent backup email (any machine's), oldest first. Merging only adds, so there's nothing to confirm;
    `yes` is accepted for old scripts. None when no backup email exists."""
    print("looking for backup emails…", flush=True)
    found = mail.backups(cfg["gmail_user"], cfg["gmail_app_password"])
    if not found:
        print("no backup email found (send one with: ./budgetmail backup --mail)")
        return None
    con = ledger.connect(str(config.DB))
    if config.DB.exists():
        import shutil
        shutil.copy(config.DB, config.DB.with_suffix(".db.bak"))
    r = None
    for data, subject in found:
        r = restore_backup(data, con, bak=False)
        print(f"  merged {subject}: +{r['added']['transactions']} transactions ({r['total']} now), {r['added']['edits']} edits applied", flush=True)
    return r


# ---------------------------------------------------------------- serve
class Handler(BaseHTTPRequestHandler):
    con = None
    cfg = None
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache"); self.end_headers()
        self.wfile.write(body)

    WEB = HERE / "web"
    TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send((self.WEB / "index.html").read_bytes())
        elif path == "/api/data":
            import dashboard
            self._send(json.dumps(dashboard.data(self.con), separators=(",", ":")).encode(), "application/json")
        elif path.startswith("/static/"):
            f = (self.WEB / path[len("/static/"):]).resolve()
            if self.WEB.resolve() in f.parents and f.is_file():
                self._send(f.read_bytes(), self.TYPES.get(f.suffix, "application/octet-stream"))
            else:
                self._send(b"not found", "text/plain", 404)
        elif path == "/api/backup":
            from datetime import date as _d
            body = make_backup()
            self.send_response(200); self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="budgetmail-backup-{_d.today().isoformat()}.zip"')
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif path == "/api/status":
            self._send(json.dumps({"last_sync": ledger.get_meta(self.con, "last_sync"), "counts": json.loads(ledger.get_meta(self.con, "last_counts", "{}"))}).encode(), "application/json")
        elif path == "/api/events":
            self._events()
        else:
            self._send(b"not found", "text/plain", 404)

    def _events(self):
        """Server-sent events: one long-lived response per open dashboard (each on its own ThreadingHTTPServer thread)."""
        q = queue.Queue()
        with _sub_lock:
            _subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(f"data: {q.get(timeout=25)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")                # keepalive; also how we notice a closed tab
                self.wfile.flush()
        except OSError:
            pass                                                   # client went away
        finally:
            with _sub_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    def do_POST(self):
        if self.path.startswith("/api/import"):
            from urllib.parse import parse_qs, urlparse, unquote
            q = parse_qs(urlparse(self.path).query)
            acct = q.get("account", [""])[0] or None
            name = unquote(q.get("name", ["upload"])[0])
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with self.lock:
                try:
                    if body[:2] == b"PK" or name.lower().endswith(".zip"):                 # a backup zip → restore
                        r = restore_backup(body, self.con); Handler.con = self.con = r.pop("con")
                        self._send(json.dumps(r).encode(), "application/json")
                    else:
                        self._send(json.dumps(import_file(self.con, name, body, acct)).encode(), "application/json")
                except Exception as ex:
                    self._send(json.dumps({"error": str(ex)}).encode(), "application/json", 400)
        elif self.path == "/api/budget":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            import categories
            if body.get("category") not in categories.CATEGORIES + ["__total__"]:
                self._send(json.dumps({"error": "unknown category"}).encode(), "application/json", 400); return
            with self.lock:
                ledger.set_budget(self.con, body["category"], body.get("amount"))
            self._send(json.dumps(ledger.budgets(self.con)).encode(), "application/json")
        elif self.path == "/api/category":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            import categories
            if body.get("category") not in categories.CATEGORIES:
                self._send(json.dumps({"error": "unknown category"}).encode(), "application/json", 400); return
            if body.get("scope") not in ("one", "merchant"):
                self._send(json.dumps({"error": "scope must be one|merchant"}).encode(), "application/json", 400); return
            with self.lock:
                try:
                    r = recategorize(self.con, body.get("id"), body["category"], body["scope"])
                except KeyError as ex:
                    self._send(json.dumps({"error": ex.args[0]}).encode(), "application/json", 404); return
            self._send(json.dumps(r).encode(), "application/json")
        elif self.path == "/api/category/revert":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with self.lock:
                self._send(json.dumps(revert_recategorize(self.con, body)).encode(), "application/json")
        elif self.path == "/api/rename":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if body.get("scope") not in ("one", "merchant"):
                self._send(json.dumps({"error": "scope must be one|merchant"}).encode(), "application/json", 400); return
            with self.lock:
                try:
                    r = rename(self.con, body.get("id"), body.get("name"), body["scope"])
                except KeyError as ex:
                    self._send(json.dumps({"error": ex.args[0]}).encode(), "application/json", 404); return
            self._send(json.dumps(r).encode(), "application/json")
        elif self.path == "/api/rename/revert":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with self.lock:
                self._send(json.dumps(revert_rename(self.con, body)).encode(), "application/json")
        elif self.path == "/api/dupe":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with self.lock:
                if not self.con.execute("SELECT 1 FROM transactions WHERE id=?", (body.get("id"),)).fetchone():
                    self._send(json.dumps({"error": "no such transaction"}).encode(), "application/json", 404); return
                ledger.set_dupe(self.con, body["id"], bool(body.get("dupe", True)))
            self._send(json.dumps({"id": body["id"], "dupe": bool(body.get("dupe", True))}).encode(), "application/json")
        elif self.path == "/api/sync":
            with self.lock:
                try:
                    counts = sync(self.con, self.cfg)
                    self._send(json.dumps(counts).encode(), "application/json")
                except Exception as ex:  # surface the error to the button, don't kill the server
                    self._send(json.dumps({"error": str(ex)}).encode(), "application/json", 500)
        else:
            self._send(b"not found", "text/plain", 404)


def serve(cfg: dict):
    con = ledger.connect(str(config.DB))
    Handler.con, Handler.cfg = con, cfg
    interval = max(5, int(cfg.get("sync_interval_min", 60))) * 60

    def loop():
        while True:
            with Handler.lock:
                try:
                    c = sync(con, cfg)
                    print(datetime.now().strftime("%H:%M"), "sync", c, flush=True)
                except Exception as ex:
                    print(datetime.now().strftime("%H:%M"), "sync failed:", ex, flush=True)
                days = int(cfg.get("mail_backup_days", 1))
                last = ledger.get_meta(con, "last_mail_backup")
                if days > 0 and (not last or datetime.fromisoformat(last) < datetime.now() - timedelta(days=days)):
                    try:
                        print(datetime.now().strftime("%H:%M"), "mailed backup:", mail_backup(con, cfg), flush=True)
                    except Exception as ex:
                        print(datetime.now().strftime("%H:%M"), "mail backup failed:", ex, flush=True)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True).start()
    port = int(cfg.get("port", 8080))
    print(f"budgetmail on http://0.0.0.0:{port}  (sync every {interval // 60} min)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ---------------------------------------------------------------- setup
def setup():
    cur = {}
    if config.CONFIG.exists():
        cur = json.loads(config.CONFIG.read_text())
    print("budgetmail setup — Gmail is read over IMAP with an app password (myaccount.google.com/apppasswords)\n")
    user = input(f"Gmail address [{cur.get('gmail_user', '')}]: ").strip() or cur.get("gmail_user", "")
    pw = getpass.getpass("App password (hidden, 16 chars): ").strip() or cur.get("gmail_app_password", "")
    print("testing login…", end=" ", flush=True)
    try:
        mail.login(user, pw).logout()
        print("ok")
    except Exception as ex:
        raise SystemExit(f"FAILED: {ex}\nCheck the address, that 2-Step Verification is on, and that the app password is fresh.")
    dc = dict(cur.get("default_checking", {}))
    print("\nZelle/deposit emails don't always name the account. For each bank, the last 4 of the checking account they belong to (blank to skip):")
    for name in ("Chase", "Bank of America"):
        v = input(f"  {name} checking last-4 [{dc.get(name, '')}]: ").strip() or dc.get(name)
        if v:
            dc[name] = v
    port = input(f"Dashboard port [{cur.get('port', 8080)}]: ").strip() or cur.get("port", 8080)
    config.save({**cur, "gmail_user": user, "gmail_app_password": pw, "default_checking": dc, "port": int(port),
                 "sync_interval_min": int(cur.get("sync_interval_min", 60))})
    if not (HERE / "rules.toml").exists():
        (HERE / "rules.toml").write_text((HERE / "rules.example.toml").read_text())
    print(f"\nsaved {config.CONFIG}. Next: python3 budgetmail.py sync --full   (first pull, a few minutes)")


# ---------------------------------------------------------------- status / doctor / config / update
def _service_state() -> str:
    if sys.platform == "darwin":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.endswith(f"io.{SERVICE}"):
                pid = line.split()[0]
                return f"launchd: running (pid {pid})" if pid != "-" else "launchd: loaded, not running"
        return "launchd: installed, stopped" if (Path.home() / "Library/LaunchAgents" / f"io.{SERVICE}.plist").exists() else "launchd: not installed"
    out = subprocess.run(["systemctl", "is-active", SERVICE], capture_output=True, text=True).stdout.strip()
    return f"systemd: {out or 'not installed'}"


def _restart_service():
    if sys.platform == "darwin":
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/io.{SERVICE}"], capture_output=True, text=True)
    else:
        r = subprocess.run(["systemctl", "restart", SERVICE], capture_output=True, text=True)
    return r.returncode == 0


def stop():
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"io.{SERVICE}.plist"
        if not plist.exists():
            raise SystemExit("not installed — nothing to stop")
        r = subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, text=True)
    else:
        r = subprocess.run(["systemctl", "disable", "--now", SERVICE], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"nothing to stop: {r.stderr.strip() or 'not installed'}")
    for _ in range(20):
        if not _port_open(config.load()["port"]):
            break
        time.sleep(0.5)
    print("  ■ budgetmail stopped — won't start at boot until ./budgetmail start")


def start():
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"io.{SERVICE}.plist"
        if not plist.exists():
            raise SystemExit("not installed — run ./budgetmail install")
        r = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/io.{SERVICE}"], capture_output=True)  # load alone may not launch
    else:
        r = subprocess.run(["systemctl", "enable", "--now", SERVICE], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"failed: {r.stderr.strip()}")
    _announce(config.load()["port"])


def _lan_ip() -> str | None:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except Exception:
        return None


def _tailscale() -> tuple[str, str, bool] | None:
    """(ip, magicdns name, running) if the tailscale CLI is installed and running, else None."""
    exe = shutil.which("tailscale") or ("/Applications/Tailscale.app/Contents/MacOS/Tailscale" if sys.platform == "darwin" else None)
    if not exe or not Path(exe).exists():
        return None
    try:
        ip = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=3).stdout.strip().splitlines()
        if not ip:
            return None
        st = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=3).stdout or "{}")
        name = (st.get("Self") or {}).get("DNSName", "").rstrip(".")
        return ip[0], name, st.get("BackendState") == "Running"
    except Exception:
        return None


def _urls(port: int) -> list[tuple[str, str]]:
    """Every address the dashboard answers on, as (label, url) — the Tailscale one is what works off-LAN."""
    host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    out = [("this machine", f"http://localhost:{port}"), ("on your LAN", f"http://{host}:{port}")]
    ip = _lan_ip()
    if ip and not ip.startswith("100."):
        out.append(("", f"http://{ip}:{port}"))
    ts = _tailscale()
    if ts:
        out.append(("via Tailscale", f"http://{ts[0]}:{port}" + ("" if ts[2] else "  (tailscale is stopped — start it)")))
        if ts[1]:
            out.append(("", f"http://{ts[1]}:{port}"))
    else:
        out.append(("via Tailscale", f"install it → http://<tailscale-ip>:{port}"))
    return out


def _announce(port: int, wait: int = 15):
    """Block until the server answers (or `wait` seconds), then print where it lives — or the log tail if it didn't come up."""
    import urllib.request
    for _ in range(wait * 2):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as r:
                st = json.load(r)
            print("\n  ✓ budgetmail is up")
            for label, url in _urls(port):
                print(f"    {label:<14} {url}")
            print(f"    {'last sync':<14} {st.get('last_sync') or 'never — first sync starts now'}")
            print(f"    {'logs':<14} {config.HOME / 'serve.log' if sys.platform == 'darwin' else 'journalctl -u ' + SERVICE + ' -f'}\n")
            return True
        except Exception:
            time.sleep(0.5)
    print(f"\n  ✗ not answering on :{port} after {wait}s. Last log lines:")
    log = config.HOME / "serve.log"
    if log.exists():
        print("    " + "\n    ".join(log.read_text().splitlines()[-8:]))
    else:
        subprocess.run(["journalctl", "-u", SERVICE, "-n", "8", "--no-pager"])
    return False


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def status():
    cfg = config.load()
    con = ledger.connect(str(config.DB))
    n_tx, n_st = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], con.execute("SELECT COUNT(*) FROM statements").fetchone()[0]
    accts = [r["name"] for r in con.execute("SELECT name FROM accounts ORDER BY name")]
    print(f"service    {_service_state()}  ({'listening' if _port_open(cfg['port']) else 'port closed'})")
    for i, (label, url) in enumerate(_urls(cfg["port"])):
        print(f"{'dashboard' if i == 0 else '':<10} {label:<14} {url}")
    print(f"last sync  {ledger.get_meta(con, 'last_sync') or 'never'}  {ledger.get_meta(con, 'last_counts', '')}")
    print(f"backup     last emailed {ledger.get_meta(con, 'last_mail_backup') or 'never'} · every {cfg.get('mail_backup_days', 1)} days")
    print(f"ledger     {n_tx} transactions · {n_st} statements · accounts: {', '.join(accts) or 'none'}")
    print(f"interval   every {cfg['sync_interval_min']} min · gmail {cfg['gmail_user']} · config {config.CONFIG}")


def doctor():
    ok = lambda c, m: print(("  ✓ " if c else "  ✗ ") + m) or c
    good = True
    good &= ok(sys.version_info >= (3, 11), f"python {sys.version.split()[0]} (need ≥ 3.11)")
    good &= ok(config.CONFIG.exists(), f"config {config.CONFIG}")
    if not config.CONFIG.exists():
        print("    run: ./budgetmail setup"); return
    cfg = config.load()
    good &= ok((config.CONFIG.stat().st_mode & 0o077) == 0, "config permissions 600")
    try:
        mail.login(cfg["gmail_user"], cfg["gmail_app_password"]).logout(); good &= ok(True, f"gmail login as {cfg['gmail_user']}")
    except Exception as ex:
        good &= ok(False, f"gmail login: {ex}  → ./budgetmail config gmail")
    try:
        ledger.connect(str(config.DB)).execute("SELECT 1"); good &= ok(True, f"database {config.DB}")
    except Exception as ex:
        good &= ok(False, f"database: {ex}")
    good &= ok((HERE / "rules.toml").exists(), "rules.toml (categories)")
    cs = classify.settings(cfg)
    print(("  ✓ " if classify.available(cfg) else "  – ") + (f"local merchant classifier ({cs['model']} via Ollama)" if classify.available(cfg)
          else f"local merchant classifier off — optional: install Ollama, then  ollama pull {cs['model']}"))
    st = _service_state(); good &= ok("running" in st or "active" == st.split()[-1], st)
    good &= ok(_port_open(cfg["port"]), f"port {cfg['port']} listening")
    print("all good" if good else "fix the ✗ lines above")


def show_config(argv):
    cfg = config.load()
    if not argv:
        masked = {**cfg, "gmail_app_password": "•" * 12 + cfg["gmail_app_password"][-4:]}
        print(json.dumps(masked, indent=2, ensure_ascii=False)); return
    if argv[0] == "gmail":
        user = input(f"Gmail address [{cfg['gmail_user']}]: ").strip() or cfg["gmail_user"]
        pw = getpass.getpass("App password (hidden): ").strip() or cfg["gmail_app_password"]
        mail.login(user, pw).logout(); print("login ok")
        config.save({**cfg, "gmail_user": user, "gmail_app_password": pw})
    elif argv[0] == "set" and len(argv) == 3:
        key, val = argv[1], argv[2]
        if key.startswith("default_checking."):
            cfg.setdefault("default_checking", {})[key.split(".", 1)[1]] = val
        elif key in ("port", "sync_interval_min"):
            cfg[key] = int(val)
        elif key.startswith("classifier.") and key.split(".", 1)[1] in classify.DEFAULTS:
            sub = key.split(".", 1)[1]
            cfg.setdefault("classifier", {})[sub] = (val.lower() in ("1", "true", "yes", "on")) if sub == "enabled" else val
        else:
            raise SystemExit(f"unknown key {key}. settable: port, sync_interval_min, default_checking.<Bank>, classifier.enabled|model|url")
        config.save(cfg); print(f"{key} = {val}")
    else:
        raise SystemExit("usage: config | config gmail | config set <key> <value>")
    if _restart_service():
        print("service restarted")
    else:
        print("service not running — start with ./budgetmail install (or serve)")


def update():
    print("git pull…", flush=True)
    r = subprocess.run(["git", "-C", str(HERE), "pull", "--ff-only"], capture_output=True, text=True)
    print("  " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()))
    if r.returncode != 0:
        raise SystemExit("pull failed — resolve in the repo and rerun")
    t = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=HERE, capture_output=True, text=True)
    print("  tests: " + ("ok" if t.returncode == 0 else "FAILED\n" + t.stderr))
    if t.returncode != 0:
        raise SystemExit("not restarting a broken build")
    print("  service: " + ("restarted" if _restart_service() else "not installed — run ./budgetmail install"))


# ---------------------------------------------------------------- install
def install():
    py, script = sys.executable, str(HERE / "budgetmail.py")
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"io.{SERVICE}.plist"
        plist.write_bytes(plistlib.dumps({"Label": f"io.{SERVICE}", "ProgramArguments": [py, script, "serve"], "RunAtLoad": True, "KeepAlive": True,
                                          "StandardOutPath": str(config.HOME / "serve.log"), "StandardErrorPath": str(config.HOME / "serve.log")}))
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], check=True)
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/io.{SERVICE}"], capture_output=True)
        print(f"launchd: {plist} (starts at login, restarts if it dies)")
    else:
        unit = f"""[Unit]
Description=budgetmail
After=network-online.target

[Service]
ExecStart={py} {script} serve
Restart=always
RestartSec=10
User={os.environ.get('SUDO_USER') or getpass.getuser()}
Environment=BUDGETMAIL_HOME={config.HOME}

[Install]
WantedBy=multi-user.target
"""
        path = Path(f"/etc/systemd/system/{SERVICE}.service")
        try:
            path.write_text(unit)
        except PermissionError:
            raise SystemExit(f"need root to write {path}: rerun with  sudo -E {py} {script} install")
        for c in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", SERVICE]):
            subprocess.run(c, check=True)
        print(f"systemd: {SERVICE} enabled (starts at boot, restarts on failure)")
    _announce(config.load()["port"])


def uninstall():
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"io.{SERVICE}.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True); plist.unlink(missing_ok=True)
    else:
        subprocess.run(["systemctl", "disable", "--now", SERVICE], capture_output=True)
        Path(f"/etc/systemd/system/{SERVICE}.service").unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    print("removed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["setup", "sync", "serve", "install", "uninstall", "start", "stop", "status", "doctor", "config", "update", "import", "backup", "restore", "classify"])
    ap.add_argument("--dry", action="store_true", help="classify: show verdicts without writing them")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--account", help="account id for import when it can't be detected")
    ap.add_argument("--force", action="store_true", help="import: re-read a file already imported (rows still dedupe)")
    ap.add_argument("--mail", action="store_true", help="backup: email the zip to yourself instead of writing a file")
    ap.add_argument("--from-mail", action="store_true", help="restore: merge every recent backup email in your Gmail")
    ap.add_argument("--yes", action="store_true", help="(no longer needed: restore merges, it never replaces)")
    ap.add_argument("rest", nargs="*")
    a = ap.parse_args()
    if a.cmd == "setup":
        setup()
    elif a.cmd == "sync":
        c = sync(ledger.connect(str(config.DB)), config.load(), a.full)
        print(c, "" if not c["unparsed"] else f"→ {config.FAILURES}")
    elif a.cmd == "serve":
        serve(config.load())
    elif a.cmd == "classify":
        cfg = config.load()
        if not classify.available(cfg):
            s = classify.settings(cfg)
            raise SystemExit(f"no Ollama with {s['model']} at {s['url']}: install Ollama (https://ollama.com/download), then: ollama pull {s['model']}")
        r = classify.run(ledger.connect(str(config.DB)), cfg, dry=a.dry)
        for k, desc, cat, score in sorted(r.get("decided", []), key=lambda d: -d[3]):
            print(f"{score:.2f}  {cat or '—':22s} {desc[:50]}")
        print({k: v for k, v in r.items() if k != "decided"}, "(dry run, nothing written)" if a.dry else "")
    elif a.cmd == "install":
        install()
    elif a.cmd == "uninstall":
        uninstall()
    elif a.cmd == "start":
        start()
    elif a.cmd == "stop":
        stop()
    elif a.cmd == "status":
        status()
    elif a.cmd == "doctor":
        doctor()
    elif a.cmd == "config":
        show_config(a.rest)
    elif a.cmd == "update":
        update()
    elif a.cmd == "backup":
        if a.mail:
            print("sent:", mail_backup(ledger.connect(str(config.DB)), config.load()))
        else:
            out = Path(a.rest[0]) if a.rest else Path(f"budgetmail-backup-{date.today().isoformat()}.zip")
            out.write_bytes(make_backup()); print(out, f"({out.stat().st_size // 1024} KB)")
    elif a.cmd == "restore":
        if a.from_mail:
            r = restore_from_mail(config.load(), yes=a.yes)
        elif a.rest:
            r = restore_backup(Path(a.rest[0]).read_bytes())
        else:
            raise SystemExit("usage: restore <backup.zip>  |  restore --from-mail")
        if r:
            r.pop("con"); print(r)
    elif a.cmd == "import":
        if not a.rest:
            raise SystemExit("usage: import <file.csv|file.pdf> [...]   (add --account <id> if the account can't be detected)")
        con = ledger.connect(str(config.DB))
        for f in a.rest:
            try:
                print(Path(f).name, "→", import_file(con, Path(f).name, Path(f).read_bytes(), a.account, force=a.force))
            except Exception as ex:
                print(Path(f).name, "→ FAILED:", ex)
