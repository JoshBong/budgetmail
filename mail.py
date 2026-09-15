"""Gmail over IMAP: search All Mail by the registered bank senders, yield flattened Emails."""
import email
import email.utils
import imaplib
import re
from datetime import date
from email.header import decode_header, make_header
from html import unescape
from typing import Iterator

from parsers import Email, all_senders

MAILBOX = "[Gmail]/All Mail"
BACKUP_SUBJECT = "budgetmail backup"


def flatten(msg) -> str:
    parts = []
    for p in msg.walk():
        ct = p.get_content_type()
        if ct in ("text/plain", "text/html") and p.get_content_disposition() is None:
            raw = p.get_payload(decode=True) or b""
            s = raw.decode(p.get_content_charset() or "utf-8", errors="replace")
            if ct == "text/html":
                s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
                s = re.sub(r"<[^>]+>", " ", s)
            parts.append(unescape(s))
    return norm("\n".join(parts))


def norm(s: str) -> str:
    s = s.replace("\xa0", " ").replace("|", " ").replace("®", "").replace("℠", "")
    s = re.sub(r"\[\]\([^)]*\)", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def login(user: str, app_password: str) -> imaplib.IMAP4_SSL:
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, app_password)
    return M


def fetch(user: str, app_password: str, since: date | None) -> Iterator[Email]:
    M = login(user, app_password)
    if M.select(f'"{MAILBOX}"', readonly=True)[0] != "OK":
        raise RuntimeError(f"cannot open {MAILBOX}")
    senders = all_senders()
    crit = "OR " * (len(senders) - 1) + " ".join(f'FROM "{s}"' for s in senders)
    if since:
        crit += f' SINCE {since.strftime("%d-%b-%Y")}'
    _, data = M.search(None, f"({crit})")
    for i in data[0].split():
        _, d = M.fetch(i, "(RFC822)")
        msg = email.message_from_bytes(d[0][1])
        try:
            received = email.utils.parsedate_to_datetime(msg["Date"]).date()
        except Exception:
            received = date.today()
        yield Email(norm(str(make_header(decode_header(msg.get("Subject", ""))))), msg.get("From", ""), flatten(msg), received)
    M.logout()


# ---------------------------------------------------------------- backups over email (self-addressed)
def send_backup(user: str, app_password: str, data: bytes, n_tx: int) -> str:
    """Email the backup zip to yourself over SMTP with the same app password. Returns the subject."""
    import hashlib
    import smtplib
    from email.message import EmailMessage
    today = date.today().isoformat()
    msg = EmailMessage()
    msg["From"] = msg["To"] = user
    msg["Subject"] = f"{BACKUP_SUBJECT} {today} · {n_tx} tx"
    msg["X-Budgetmail-Backup"] = "1"
    msg.set_content(f"budgetmail backup {today} · {n_tx} transactions · sha256 {hashlib.sha256(data).hexdigest()}\n"
                    f"Restore on any machine: ./budgetmail restore --from-mail")
    msg.add_attachment(data, maintype="application", subtype="zip", filename=f"budgetmail-backup-{today}.zip")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, app_password)
        smtp.send_message(msg)
    return msg["Subject"]


def backups(user: str, app_password: str, days: int = 60, limit: int = 30) -> list[tuple[bytes, str]]:
    """Self-sent backups from the last `days` days (newest `limit`), oldest first, each as (zip bytes, subject).
    Every machine's backups come back, so merging them all rebuilds history and hand edits wherever they were made.
    A backup whose attachment is missing or fails its sha256 is skipped; the others still count."""
    from datetime import timedelta
    M = login(user, app_password)
    msgs = []
    try:
        if M.select(f'"{MAILBOX}"', readonly=True)[0] != "OK":
            raise RuntimeError(f"cannot open {MAILBOX}")
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = M.search(None, f'(FROM "{user}" SUBJECT "{BACKUP_SUBJECT}" SINCE {since})')
        dated = []
        for i in data[0].split():                                  # headers first: sequence order isn't guaranteed chronological
            _, d = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (DATE)])")
            try:
                dated.append((email.utils.parsedate_to_datetime(email.message_from_bytes(d[0][1])["Date"]), i))
            except Exception:
                continue
        for _, i in sorted(dated)[-limit:]:
            _, d = M.fetch(i, "(RFC822)")
            msgs.append(email.message_from_bytes(d[0][1]))
    finally:
        M.logout()
    out = []
    for msg in msgs:
        try:
            out.append(_backup_zip(msg))
        except ValueError:
            continue
    return out


def _backup_zip(msg) -> tuple[bytes, str]:
    import hashlib
    zip_bytes, body = None, ""
    for p in msg.walk():
        if p.get_content_disposition() == "attachment" and (p.get_filename() or "").endswith(".zip"):
            zip_bytes = p.get_payload(decode=True)
        elif p.get_content_type() == "text/plain":
            body += (p.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    if not zip_bytes:
        raise ValueError("backup email has no zip attachment")
    m = re.search(r"sha256 ([0-9a-f]{64})", body)
    if m and m.group(1) != hashlib.sha256(zip_bytes).hexdigest():
        raise ValueError("backup attachment is corrupt (sha256 mismatch)")
    return zip_bytes, str(make_header(decode_header(msg.get("Subject", ""))))
