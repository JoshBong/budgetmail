"""Bank alert-email parsers.

Every bank is a subclass of BankParser. Subclassing registers it automatically. A subclass declares:
  name      display name, e.g. "Chase"
  senders   the exact From addresses its alerts come from
  rules     ordered (subject regex, handler-method-name) pairs — first match wins

Handlers receive an Email and return a Parsed (or None to say "I don't understand this one";
those are written to parse_failures.jsonl so new formats surface instead of vanishing).
The base class provides the field-extraction helpers so a new bank is usually ~30 lines.
See docs/parsers.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator

AMOUNT = r"\$\s?([0-9,]+\.\d{2})"


@dataclass
class Email:
    subject: str
    sender: str
    body: str          # HTML already flattened to one whitespace-normalised line
    received: date


@dataclass
class Parsed:
    kind: str                    # purchase | refund | zelle_out | zelle_in | transfer_out | deposit | statement | skip
    amount: float = 0.0          # positive number; sign is applied by kind
    merchant: str = ""
    last4: str | None = None
    date: date | None = None
    balance: float | None = None   # statements only
    due: date | None = None        # statements only
    category: str | None = None    # bank-supplied category (CSV imports), mapped by categories.normalize
    posted: bool = False           # True for CSV rows (bank's posted data), False for alert emails (authorization-time)
    extra: dict = field(default_factory=dict)

    SPEND = {"purchase", "zelle_out", "transfer_out"}
    INCOME = {"refund", "zelle_in", "deposit"}

    @property
    def signed_amount(self) -> float:
        return -self.amount if self.kind in self.SPEND else self.amount

    @property
    def txn_type(self) -> str:      # feeds reports: transfers are excluded from spend; zelle_in nets against People
        return {"purchase": "card_payment", "refund": "card_payment", "zelle_out": "zelle",
                "zelle_in": "zelle", "deposit": "transfer", "transfer_out": "transfer"}.get(self.kind, self.kind)


_REGISTRY: dict[str, "BankParser"] = {}


class BankParser:
    name: str = ""
    senders: tuple[str, ...] = ()
    rules: list[tuple[str, str]] = []          # (subject regex, handler name)
    default_checking: str | None = None        # last4 to assume when a mail names no account (set from config)

    # words that terminate a free-text field value in flattened bodies (bank boilerplate that follows)
    STOP = (r"Amount|When|Date|Card|Account|Sent|To|From|Merchant|Where|Location|Transaction|Memo|View|If|Confirmation|"
            r"This may have|Your message|Message|Balance|Check amount|Credit posts")

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if cls.name:
            _REGISTRY[cls.name] = cls()
            cls._compiled = [(re.compile(p, re.I), h) for p, h in cls.rules]

    # ---- CSV export files ------------------------------------------------------------------
    def csv_rows(self, header: list[str], rows) -> "Iterator[Parsed]":
        """Turn rows of the bank's activity-export CSV into Parsed (posted=True). Override per bank; sniff `header`
        because one bank ships different layouts per account type. Raise ValueError if the header isn't yours."""
        raise ValueError(f"{self.name}: CSV import not implemented")

    def pdf_rows(self, text: str, filename: str = "") -> "Iterator[Parsed]":
        """Turn `pdftotext -layout` output of a bank statement into Parsed rows (posted=True), plus one
        kind='statement' row for the closing balance where the statement has one. Raise ValueError if it isn't yours."""
        raise ValueError(f"{self.name}: PDF import not implemented")

    def sniff_csv(self, header: list[str]) -> bool:
        try:
            next(iter(self.csv_rows(header, [])), None); return True
        except (ValueError, StopIteration):
            return False

    # ---- dispatch -------------------------------------------------------------------------
    def parse(self, e: Email) -> Parsed | None:
        for rx, handler in self._compiled:
            if rx.search(e.subject):
                return getattr(self, handler)(e)
        return None

    def skip(self, e: Email) -> Parsed:
        return Parsed("skip")

    # ---- helpers for subclasses -----------------------------------------------------------
    def amount(self, text: str, label: str | None = None) -> float | None:
        rx = (re.escape(label) + r"\s*:?\s*" + AMOUNT) if label else AMOUNT
        m = re.search(rx, text, re.I)
        return float(m.group(1).replace(",", "")) if m else None

    def field(self, text: str, label: str, stop: str | None = None) -> str | None:
        """Value following `label`, ending at the next boilerplate word (STOP) or a $ amount."""
        stop = stop or self.STOP
        m = re.search(re.escape(label) + r"\s*:?\s+(.+?)(?=\s+(?:" + stop + r")\b|\s*\$|$)", text, re.I)
        return re.sub(r"\s+", " ", m.group(1)).strip(" .:") if m else None

    def last4(self, *texts: str) -> str | None:
        pats = (r"\(\s*\.{3}\s*(\d{4})\s*\)",                                   # (...1234)
                r"ending (?:in|with)\s*:?\s*(?:\*+|x+)?(\d{4})",                 # ending in 1234
                r"(?:Signature|Card|Banking|Savings|Checking)\s*-\s*(\d{4})\b")  # Visa Signature - 1234
        for t in texts:
            for p in pats:
                m = re.search(p, t, re.I)
                if m:
                    return m.group(1)
        return self.default_checking

    def date(self, text: str, default: date, label: str | None = None) -> date:
        hay = text
        if label:
            m = re.search(re.escape(label) + r"\s*:?\s*([A-Za-z0-9/, ]+?\d{4})", text, re.I)
            hay = m.group(1) if m else ""
        for rx, fmts in ((r"\b([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4})", ("%b %d, %Y", "%B %d, %Y", "%b. %d, %Y")),
                         (r"\b(\d{1,2}/\d{1,2}/\d{4})", ("%m/%d/%Y",))):
            for m in re.finditer(rx, hay):
                for f in fmts:
                    try:
                        return datetime.strptime(m.group(1), f).date()
                    except ValueError:
                        pass
        return default


def for_sender(sender: str) -> BankParser | None:
    s = sender.lower()
    for p in _REGISTRY.values():
        if any(a.lower() in s for a in p.senders):
            return p
    return None


def all_senders() -> list[str]:
    return [a for p in _REGISTRY.values() for a in p.senders]


def configure(default_checking: dict[str, str]):
    for name, last4 in default_checking.items():
        if name in _REGISTRY:
            _REGISTRY[name].default_checking = last4


from . import chase, bofa, venmo  # noqa: E402,F401  — importing registers them
