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


def latest_backup(user: str, app_password: str) -> tuple[bytes, str] | None:
    """The newest self-sent backup email, as (zip bytes, subject) — or None. Verifies the sha256 from the body."""
    import hashlib
    M = login(user, app_password)
    try:
        if M.select(f'"{MAILBOX}"', readonly=True)[0] != "OK":
            raise RuntimeError(f"cannot open {MAILBOX}")
        _, data = M.search(None, f'(FROM "{user}" SUBJECT "{BACKUP_SUBJECT}")')
        ids = data[0].split()
        if not ids:
            return None
        best = None                                                # (Date header, id) — sequence order isn't guaranteed chronological
        for i in ids[-30:]:
            _, d = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (DATE)])")
            try:
                dt = email.utils.parsedate_to_datetime(email.message_from_bytes(d[0][1])["Date"])
            except Exception:
                continue
            if best is None or dt > best[0]:
                best = (dt, i)
        if best is None:
            return None
        _, d = M.fetch(best[1], "(RFC822)")
        msg = email.message_from_bytes(d[0][1])
    finally:
        M.logout()
    zip_bytes, body = None, ""
    for p in msg.walk():
        if p.get_content_disposition() == "attachment" and (p.get_filename() or "").endswith(".zip"):
            zip_bytes = p.get_payload(decode=True)
        elif p.get_content_type() == "text/plain":
            body += (p.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    if not zip_bytes:
        raise ValueError("newest backup email has no zip attachment")
    m = re.search(r"sha256 ([0-9a-f]{64})", body)
    if m and m.group(1) != hashlib.sha256(zip_bytes).hexdigest():
        raise ValueError("backup attachment is corrupt (sha256 mismatch)")
    return zip_bytes, str(make_header(decode_header(msg.get("Subject", ""))))
