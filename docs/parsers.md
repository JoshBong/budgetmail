# Adding a bank (or any money source)

Every source is one file in `parsers/` — a subclass of `BankParser`. Subclassing registers it; nothing else to wire.
A bank supports up to three inputs, all methods on the same class:

| Hook | Input | Used by |
|---|---|---|
| `rules` + handler methods | alert **emails** (the live feed) | hourly sync |
| `csv_rows(header, rows)` | the bank's activity **CSV export** | Data-tab drop / `import` |
| `pdf_rows(text, filename)` | the bank's **statement PDF** (`pdftotext -layout` text) | Data-tab drop / `import` |

Implement what the bank offers. Unimplemented hooks raise `ValueError`, and the importer tries the next bank.
`parsers/chase.py` implements all three; `parsers/bofa.py` all three (card PDFs pending); `parsers/venmo.py` email only.

## 0. Get real samples first
Never write a parser from memory — banks' wording is weird and changes. Turn the alerts on, let a few arrive, then
`./budgetmail sync`. Anything from an unknown sender is counted as `unknown_sender`; anything from a registered bank the
parser can't read lands in `~/.budgetmail/parse_failures.jsonl` **with the flattened body**. That file is your spec.
For CSV/PDF, download one of each account type; run `pdftotext -layout file.pdf -` to see the text the parser gets.

## 1. The class
```python
# parsers/acme.py
import re
from datetime import date
from . import BankParser, Email, Parsed

class Acme(BankParser):
    name = "Acme Bank"                              # display name; key in config "default_checking"
    senders = ("alerts@acme.com",)                  # exact From addresses of alert mail. Marketing senders: leave out
    rules = [                                       # subject regex → handler. First match wins, top to bottom
        (r"statement is ready", "statement"),
        (r"purchase of \$|charge on your card", "purchase"),
        (r"refund|credit posted", "refund"),
        (r"payment (?:received|scheduled)|security|password|welcome", "skip"),   # known noise → skip, never logged
    ]                                               # no match → None → logged to parse_failures.jsonl
```
Register it: add `acme` to the import at the bottom of `parsers/__init__.py`.

## 2. Email handlers
Each handler gets an `Email` (`subject`, `sender`, `body` = HTML flattened to one whitespace-normalised line with pipes
and tables removed, `received` date) and returns a `Parsed`:

```python
    def purchase(self, e: Email):
        amt = self.amount(e.subject) or self.amount(e.body, "Amount")
        merchant = self.field(e.body, "Merchant") or self.field(e.body, "Where")
        if amt is None or not merchant:
            return None                             # recognised the subject, couldn't read it → logged, fix later
        return Parsed("purchase", amt, merchant, self.last4(e.body), self.date(e.body, e.received, "Date"))

    def statement(self, e: Email):
        bal = self.amount(e.body, "Statement balance")
        return Parsed("statement", last4=self.last4(e.body), date=e.received, balance=bal) if bal is not None else None
```
`Parsed(kind, amount, merchant, last4, date, balance=None, due=None, category=None, posted=False, extra={})`

`kind` decides everything downstream — **amount is always positive**; the sign and the spend/income split come from kind:

| kind | meaning | counts as |
|---|---|---|
| `purchase` | card charge, ATM, fee | spend (category from rules.toml) |
| `refund` | merchant credit | negative spend, same category |
| `zelle_out` | you paid a person (Zelle, Venmo, Apple Cash…) | spend, People |
| `zelle_in` | a person paid you | nets against People |
| `transfer_out` | you moved money to another account of yours (paid your card, funded savings) | nothing — the charges were already counted |
| `deposit` | paycheck, check, wire, other income | income, excluded from the budget |
| `statement` | statement closed | checksum only (needs `balance`, `date`) |
| `skip` | known noise | nothing |

Helpers on the base class:

| helper | does |
|---|---|
| `amount(text, label=None)` | first `$1,234.56`; with a label, the amount right after it |
| `field(text, label, stop=None)` | text after `label` up to the next boilerplate word (`Amount`, `Date`, `Where`, `Location`, `This may have`, …) or a `$`. Pass `stop` for one-offs |
| `last4(*texts)` | `(...1234)`, `ending in 1234`, `Card - 1234`; falls back to the bank's `default_checking` from config |
| `date(text, default, label=None)` | `Aug 3, 2026` / `August 3, 2026` / `08/03/2026`; with a label, only the date after it |

Emails fire at **authorization**; leave `posted=False` so a later CSV/PDF row replaces them with the bank's final amount.

## 3. CSV export
```python
    def csv_rows(self, header, rows):
        h = [c.strip().lower() for c in header]
        if not {"date", "description", "amount"} <= set(h):
            raise ValueError("not an Acme CSV")            # lets the importer try the next bank
        i = {k: h.index(k) for k in ("date", "description", "amount")}
        for r in rows:
            if len(r) <= i["amount"] or not r[i["amount"]]: continue
            amt, desc = float(r[i["amount"]].replace(",", "")), r[i["description"]].strip()
            if self.OWN_ACCOUNT.search(desc): continue      # transfers to your own accounts, card payments: not spend
            yield Parsed("refund" if amt > 0 else "purchase", abs(amt), desc, None, self.date(r[i["date"]], None), posted=True)
```
Sniff the header — banks ship one layout per account type. The CSV never says which account it is; the app detects the
account from the filename (`Chase2637_Activity…csv`) or asks the user. If the bank supplies a category column, pass it as
`category=` — `categories.BANK_MAP` maps it into ours.

## 4. Statement PDF
The app runs `pdftotext -layout` and hands you the text. Rows are aligned columns, so one regex usually does it:
```python
    ROW = re.compile(r"^\s*(\d{2})/(\d{2})\s+(.+?)\s{2,}(-?[\d,]+\.\d{2})\s*$")

    def pdf_rows(self, text, filename=""):
        m = re.search(r"Statement period (\d{2}/\d{2}/\d{4}) - (\d{2}/\d{2}/\d{4})", text)
        if not m: raise ValueError("not an Acme statement")
        start, end = self.date(m.group(1), None), self.date(m.group(2), None)
        section = None
        for line in text.splitlines():
            u = line.strip().upper()
            if u.startswith("PURCHASES"): section = "purchase"; continue
            if u.startswith("PAYMENTS AND CREDITS"): section = "credit"; continue
            if u.startswith("FEES"): section = "fee"; continue
            r = self.ROW.match(line)
            if not r or not section: continue
            desc, amt = r.group(3).strip(), float(r.group(4).replace(",", ""))
            d = date(end.year if int(r.group(1)) <= end.month else start.year, int(r.group(1)), int(r.group(2)))   # rows have no year
            if section == "credit" and "PAYMENT" in desc.upper(): continue
            if "EXCHG RATE" in desc.upper(): continue                        # foreign-exchange continuation lines
            yield Parsed("refund" if amt < 0 or section == "credit" else "purchase", abs(amt),
                         ("Fee: " if section == "fee" else "") + desc, None, d, posted=True)
        nb = re.search(r"New Balance\s+\$?([\d,]+\.\d{2})", text)
        if nb: yield Parsed("statement", date=end, balance=float(nb.group(1).replace(",", "")))
```
Things every statement parser has to handle: rows without a year (derive from the period), page-break section
headers (`… (CONTINUED)`), FX continuation lines, fees/interest sections, the bank's own payments (skip), and the
closing balance (`statement` row → the checksum on the Data tab). Account detection: the filename (`…-2637-.pdf`), or
`Account number: XXXX XXXX 1234` in the text — see `detect_account()` in `budgetmail.py`; add a regex there if your
bank prints it differently.

## 5. Transfers between your own accounts
Skip them in the parser where the wording is bank-specific (`Online Banking transfer`, `Payment to Chase card`,
`DES:Ext Trnsfr`) — put the patterns in a class-level `OWN_ACCOUNT` regex like `bofa.py`. `ledger.tidy()` is the safety
net after that: any row that names another tracked account's last-4 or a bank you have an account with (`CHASE CREDIT
CRD`), or that pairs with the same amount in another account within 3 days, becomes a transfer and counts for nothing.
Alert emails that announce a transfer (Chase "Transfer alert": `You sent $X from account ending in …`) parse to
`transfer_out` so they never look like a Zelle payment. A transfer stays out of spend even when a rules.toml pattern
matches its text; only a hand-set category (drag) can pull it back in.

## 6. Tests, then a PR
`tests/test_parsers.py` — copy a real body per email shape, a CSV snippet per layout, a chunk of `pdftotext` output
per statement type (**scrub your card digits and names**), and assert `kind / amount / merchant / date`. Run
`python3 -m unittest -v`. Add the bank to the README table. That's the whole PR.

Rule of thumb: **one handler per shape, explicit `skip` for known noise, `None` for anything you can't read.**
Silence is the bug; the failures file is the feature.
