"""Chase. Alerts come from no.reply.alerts@chase.com. Bodies are `label value` pairs."""
import re
from datetime import date

from . import BankParser, Email, Parsed


class Chase(BankParser):
    name = "Chase"
    senders = ("no.reply.alerts@chase.com",)
    rules = [
        (r"credit card statement is available", "statement"),
        (r"transaction with|debit card transaction of", "purchase"),
        (r"refund|credit (?:was )?posted", "refund"),
        (r"^You sent \$[\d,.]+ from account ending in", "transfer_out"),   # "Transfer alert": you moved money, e.g. paid your own card
        (r"received money with Zelle", "zelle_in"),
        (r"sent money with Zelle|^You sent", "zelle_out"),
        (r"daily account summary", "skip"),          # TODO: parse posted transactions once a real one is seen
        (r"statement is now available|payment|letter|credit summary|zelle|transfer|external bank|approved|"
         r"account is open|data access|sharing data|security|sign.?in|password|alert|paperless|notifications|"
         r"digital wallet|external account|check deposit|welcome|identity|credit journey", "skip"),
    ]

    def statement(self, e: Email):
        bal = self.amount(e.body, "Statement balance")
        if bal is None:
            return None
        return Parsed("statement", last4=self.last4(e.body, e.subject), date=e.received, balance=bal,
                      due=self.date(e.body, e.received, "Due date"))

    def purchase(self, e: Email):
        amt = self.amount(e.subject) or self.amount(e.body, "Amount")
        merchant = self.field(e.subject, "transaction with") or self.field(e.body, "Merchant")
        if amt is None or not merchant:
            return None
        return Parsed("purchase", amt, merchant, self.last4(e.body, e.subject), self.date(e.body, e.received, "Date"))

    def refund(self, e: Email):
        amt = self.amount(e.subject) or self.amount(e.body, "Amount")
        merchant = self.field(e.body, "Merchant") or "refund"
        return Parsed("refund", amt, merchant, self.last4(e.body, e.subject), self.date(e.body, e.received)) if amt else None

    def transfer_out(self, e: Email):
        amt = self.amount(e.subject) or self.amount(e.body, "Amount")
        who = self.field(e.body, "Recipient") or self.field(e.body, "to") or "?"
        return Parsed("transfer_out", amt, "Transfer to " + who, self.last4(e.body, e.subject), self.date(e.body, e.received, "Sent on")) if amt else None

    def zelle_in(self, e: Email):
        amt = self.amount(e.body, "Amount")
        m = re.search(r"(?:Zelle payment\s+)?([A-Z][A-Za-z0-9 .'\-]{1,40}?) sent you money", e.body)
        return Parsed("zelle_in", amt, "Zelle from " + (m.group(1).strip() if m else "?"),
                      self.last4(e.body), self.date(e.body, e.received, "Sent on")) if amt else None

    def zelle_out(self, e: Email):
        amt = self.amount(e.subject) or self.amount(e.body, "Amount")
        who = self.field(e.subject, "to") or self.field(e.body, "To") or "?"
        return Parsed("zelle_out", amt, "Zelle to " + who, self.last4(e.body), self.date(e.body, e.received)) if amt else None

    # Chase → account → "Download activity" → CSV. Two layouts:
    #   card:     Transaction Date,Post Date,Description,Category,Type,Amount,Memo
    #   checking: Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
    def csv_rows(self, header, rows):
        h = [c.strip().lower() for c in header]
        if "transaction date" in h and "category" in h:
            i = {k: h.index(k) for k in ("transaction date", "post date", "description", "category", "type", "amount")}
            for r in rows:
                if len(r) <= i["amount"] or not r[i["amount"]]:
                    continue
                amt = float(r[i["amount"]])
                typ = r[i["type"]].strip().lower()
                if typ == "payment":
                    continue                                            # own-account payment, not spend
                kind = "refund" if amt > 0 else "purchase"
                yield Parsed(kind, abs(amt), r[i["description"]].strip(), None, self.date(r[i["post date"]] or r[i["transaction date"]], None),
                             category=r[i["category"]].strip() or None, posted=True)
        elif "details" in h and "posting date" in h:
            i = {k: h.index(k) for k in ("details", "posting date", "description", "amount", "type")}
            for r in rows:
                if len(r) <= i["type"] or not r[i["amount"]]:
                    continue
                amt = float(r[i["amount"]])
                typ = r[i["type"]].strip().upper()
                desc = r[i["description"]].strip()
                if typ in ("ACCT_XFER", "LOAN_PMT") or "PAYMENT TO CHASE CARD" in desc.upper():
                    continue                                            # transfers between own accounts
                if amt > 0:
                    kind, merchant = "deposit", ("Zelle from " + desc.split("FROM", 1)[-1].strip() if "ZELLE" in desc.upper() else desc)
                elif "ZELLE" in desc.upper():
                    kind, merchant = "zelle_out", "Zelle to " + desc.split(" TO ", 1)[-1].split(" ")[0] if " TO " in desc.upper() else desc
                else:
                    kind, merchant = "purchase", desc
                yield Parsed(kind, abs(amt), merchant, None, self.date(r[i["posting date"]], None), posted=True)
        else:
            raise ValueError("not a Chase activity CSV (expected card or checking headers)")

    # Chase statement PDFs (pdftotext -layout). Card: "Opening/Closing Date MM/DD/YY - MM/DD/YY", sections
    # PAYMENTS AND OTHER CREDITS / PURCHASE with "MM/DD  DESC  AMOUNT". Checking: "Month D, YYYY through Month D, YYYY",
    # TRANSACTION DETAIL with "MM/DD  DESC  AMOUNT  BALANCE".
    ROW = re.compile(r"^\s*(\d{2})/(\d{2})\s+(.+?)\s{2,}(-?[\d,]+\.\d{2})(?:\s+(-?[\d,]+\.\d{2}))?\s*$")

    def pdf_rows(self, text, filename=""):
        m = re.search(r"Opening/Closing Date\s+(\d{2})/(\d{2})/(\d{2})\s*-\s*(\d{2})/(\d{2})/(\d{2})", text)
        if m:
            yield from self._pdf_card(text, m)
            return
        m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4}) through ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
        if m and "TRANSACTION DETAIL" in text:
            yield from self._pdf_checking(text, self.date(m.group(1), None), self.date(m.group(2), None))
            return
        raise ValueError("not a Chase statement PDF")

    @staticmethod
    def _year_for(mm, dd, start: date, end: date) -> date:
        for y in (end.year, start.year):
            d = date(y, int(mm), int(dd))
            if start <= d <= end:
                return d
        return date(end.year, int(mm), int(dd))

    def _pdf_card(self, text, m):
        o = date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2))); c = date(2000 + int(m.group(6)), int(m.group(4)), int(m.group(5)))
        section = None
        for line in text.splitlines():
            u = line.strip().upper()
            if u.startswith("PAYMENTS AND OTHER CREDITS"): section = "credits"; continue
            if u == "PURCHASE" or u.startswith("PURCHASE "): section = "purchase"; continue
            if u.startswith("FEES CHARGED") or u.startswith("INTEREST CHARGED"): section = "fee"; continue
            if u.startswith(("TOTALS YEAR", "INTEREST CHARGES", "YEAR-TO-DATE")): section = None
            r = self.ROW.match(line)
            if not r or not section:
                continue
            desc, amt = r.group(3).strip(), float(r.group(4).replace(",", ""))
            if "EXCHG RATE" in desc.upper() or desc.upper() in ("WON", "NEW TAIWAN DOLLAR", "MALAYSIAN RINGGIT", "SRI LANKA RUPEE"):
                continue
            d = self._year_for(r.group(1), r.group(2), o, c)
            if section == "credits":
                if "PAYMENT" in desc.upper():
                    continue                                                # own payment
                yield Parsed("refund", abs(amt), desc, None, d, posted=True)
            elif section == "fee":
                if amt:
                    yield Parsed("purchase", abs(amt), "Fee: " + desc.title(), None, d, posted=True)
            else:
                yield Parsed("refund" if amt < 0 else "purchase", abs(amt), desc, None, d, posted=True)
        nb = re.search(r"New Balance\s+\$?([\d,]+\.\d{2})", text)
        if nb:
            yield Parsed("statement", last4=None, date=c, balance=float(nb.group(1).replace(",", "")), posted=True)

    def _pdf_checking(self, text, start: date, end: date):
        inside = False
        for line in text.splitlines():
            u = line.strip().upper()
            if u.startswith("TRANSACTION DETAIL"): inside = True; continue
            if inside and u.startswith("ENDING BALANCE"): break
            r = self.ROW.match(line) if inside else None
            if not r:
                continue
            desc, amt = r.group(3).strip(), float(r.group(4).replace(",", ""))
            d = self._year_for(r.group(1), r.group(2), start, end)
            up = desc.upper()
            if "ONLINE TRANSFER" in up or "AUTOPAY" in up or "PAYMENT TO CHASE CARD" in up:
                continue                                                    # own-account movement
            if "ZELLE PAYMENT TO" in up:
                who = re.sub(r"\s+\S*\d\S*$", "", desc.split(" To ", 1)[-1]) if " To " in desc else desc
                yield Parsed("zelle_out", abs(amt), "Zelle to " + who.strip(), None, d, posted=True); continue
            if "ZELLE PAYMENT FROM" in up:
                who = re.sub(r"\s+\d+$", "", desc.split(" From ", 1)[-1]) if " From " in desc else desc
                yield Parsed("zelle_in", abs(amt), "Zelle from " + who.strip(), None, d, posted=True); continue
            if amt > 0:
                yield Parsed("deposit", amt, desc, None, d, posted=True); continue
            merchant = re.sub(r"^(?:Recurring )?Card Purchase(?: W/Cash Back)?\s+\d{2}/\d{2}\s+", "", desc)
            merchant = re.sub(r"\s+Card \d{4}$", "", merchant).strip()
            yield Parsed("purchase", abs(amt), merchant, None, d, posted=True)
