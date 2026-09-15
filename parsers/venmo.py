"""Venmo. Payment emails come from venmo@venmo.com (marketing comes from email.venmo.com and is ignored by sender).
Bank-side VENMO rows (funding pulls, cash-outs) are marked as transfers by ledger.tidy, so Venmo spend is counted once, here."""
import re

from . import BankParser, Email, Parsed


class Venmo(BankParser):
    name = "Venmo"
    senders = ("venmo@venmo.com",)
    rules = [
        (r"^You paid (.+?) \$", "paid"),
        (r"^You completed .+ charge request", "paid"),
        (r"^(.+?) paid (?:your? )?\$|^You received \$", "received"),      # "paid you $", "paid your $X request", "paid $X to your Venmo account"
        (r"transfer has been initiated|transfer .* complete|added money|Instant transfer", "skip"),   # moving your own money
        (r"requests? \$|Reminder:|wants to be friends|transaction history|verify|welcome|security|password|sign-in|"
         r"payment method|profile|account|statement|changes to|commented on|declined|cancel|is now your friend|coming soon|paypal|invited you|expense activity|group", "skip"),
    ]

    def paid(self, e: Email):
        m = re.match(r"You paid (.+?) \$", e.subject) or re.match(r"You completed (.+?)'s \$[\d.,]+ charge request", e.subject)
        who = (m.group(1) if m else "?").strip()
        amt = self.amount(e.subject) or self._spaced_amount(e.body)
        if amt is None:
            return None
        return Parsed("zelle_out", amt, "Venmo to " + who, None, self.date(e.body, e.received, "Date"), posted=True, extra={"note": self._note(e.body, who), "ref": self._ref(e.body)})   # Venmo emails are final

    def received(self, e: Email):
        m = re.match(r"(.+?) paid (?:your? )?\$", e.subject) or re.match(r"You received \$[\d.,]+ from (.+)$", e.subject)
        who = (m.group(1) if m else "?").strip()
        amt = self.amount(e.subject) or self._spaced_amount(e.body)
        return Parsed("zelle_in", amt, "Venmo from " + who, None, self.date(e.body, e.received, "Date"), posted=True, extra={"ref": self._ref(e.body)}) if amt else None

    @staticmethod
    def _ref(body):                  # Venmo's own transaction id: the exact identity of a payment
        m = re.search(r"Transaction ID\s*:?\s*(\d{6,})", body)
        return m.group(1) if m else None

    @staticmethod
    def _spaced_amount(text):        # bodies render "$ 5. 00"
        m = re.search(r"\$\s*([0-9,]+)\s*\.\s*(\d{2})", text)
        return float(m.group(1).replace(",", "") + "." + m.group(2)) if m else None

    @staticmethod
    def _note(body, who):
        head = body.split("See transaction", 1)[0]                          # headline is repeated; the note sits after the LAST amount
        ms = list(re.finditer(r"\$\s*[0-9,]+\s*\.\s*\d{2}", head))
        note = head[ms[-1].end():].strip() if ms else ""
        return "" if not note or note.startswith("You paid") or note == "." else note[:40]
