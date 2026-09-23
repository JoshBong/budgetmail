# budgetmail

**100% free, email-based personal finance.** Your bank already emails you every purchase — budgetmail reads those alerts
from your Gmail and turns them into a ledger, a spending dashboard, and budgets. No aggregator, no Plaid, no bank
credentials, no subscription, no data leaving your machine.

- **Free forever, by construction.** The data feed is your bank's own alert emails, which banks send for free and won't
  stop sending. Nothing to renew, no API that can shut down (this was built the week Teller did).
- **Your data stays yours.** One process on a box you own (a Raspberry Pi, an old laptop, a Mac), SQLite on disk, plain
  HTML. View it from your phone over your LAN or Tailscale.
- **Zero dependencies.** Python 3.11+ standard library only. `git clone` and run.
- **Backfill history** by dropping bank statement PDFs / CSV exports; imports dedupe and replace the alert-time rows.
- **Sources today:** Chase, Bank of America, Venmo. **Adding a bank** = one subclass with email, CSV and PDF hooks →
  [docs/parsers.md](docs/parsers.md). PRs welcome.

```
bank purchase alert ──► Gmail ──► budgetmail (hourly IMAP pull + web UI) ──► http://your-box:8080
statement PDF / CSV ──► drop on the Data tab ─┘                              └─ ~/.budgetmail/ledger.db
```

**Dashboard:** Spending (one month, donut by category or account, tap to drill), Budget (per-category + total, with a
"where you should be today" tick and month-end pace), Transactions (filter/search), Trends (12 months, recurring charges),
Data (sync, imports, backup). Zelle/Venmo paybacks net against what you sent; transfers between your own accounts and
card payments count for nothing.

**Something in the wrong category?** Drag the transaction onto the category it belongs in — the cards on Spending, or the
rail of categories that slides up while you drag (long-press to start on a phone). It asks whether that's just this one
charge or every charge from that merchant; "every" is remembered, so future ones land right too. Undo is in the toast.
Rules you set this way outrank `rules.toml`, and they ride along in your backup.

**Still a lot in Other?** Run [Ollama](https://ollama.com/download) on the box and `ollama pull qwen2.5:3b` (the
installer offers it). After each sync, every merchant that nothing else could place is decided once by that small local
model — no API, no key, nothing leaves the machine. The category list in the prompt is whatever your categories are,
with the hints in `categories.py`; when the model says it can't tell, the merchant stays Other and is retried in a
month. Your drags, `rules.toml` and the bank's own category all outrank it. `./budgetmail classify --dry` previews what
it would decide; `config set classifier.model <name>` picks another Ollama model.

## Setup

Runs anywhere with Python 3.11+ — a Raspberry Pi, an old laptop, a Mac, a $4 VPS. One machine runs it; phones and laptops
just open the page.

```
# 1. banks → email alerts for every purchase (lowest threshold). Gmail receives them.
# 2. a Gmail app password:  myaccount.google.com/apppasswords
# 3.
sudo apt install -y git python3 poppler-utils        # Debian / Raspberry Pi OS   (macOS: brew install poppler)
git clone https://github.com/JoshBong/budgetmail && cd budgetmail && ./install.sh
```
The wizard asks for the Gmail address + app password (login is tested before anything is saved), which checking accounts
Zelle mails belong to, and a port. It pulls your mailbox once, registers a service (systemd on Linux, launchd on macOS),
and prints the URL — `http://<host>:8080`. Then drop your bank's statement PDFs / CSV exports on the **Data** tab for history.

**Viewing from anywhere:** the page is plain HTTP on your LAN. Don't port-forward it. Install [Tailscale](https://tailscale.com)
on the box and your phone/laptop (free) and the same URL works from any network, encrypted — that's the recommended
"hosting". Any VPN or SSH tunnel works the same way.

**Backups / moving machines:** the service emails a backup zip to your own Gmail once a week (ledger, budgets, rules,
settings — never the Gmail password; it's the same data your inbox already holds). A fresh install finds the newest one and
restores it before the first pull, so moving to a new box is `git clone` + `./install.sh` + your Gmail login. By hand:
`./budgetmail backup --mail` and `./budgetmail restore --from-mail`. The Data tab's **Download backup** / drop-to-restore
still work for a file on disk. Run one server at a time.

## Commands
```
./install.sh                      first install: setup wizard → first pull → register the service
./budgetmail status              service state · LAN + Tailscale URLs · last sync · last backup · ledger totals
./budgetmail stop | start        pause / resume the service (stays installed)
./budgetmail update              git pull → run tests → restart
./budgetmail doctor              check python · config · Gmail login · database · service · port
./budgetmail config              show config (password masked)
./budgetmail config set port 8090            also: sync_interval_min 30 · default_checking.Chase 1234
./budgetmail config gmail        re-enter the Gmail login (tested before saving)
./budgetmail sync [--full]       pull mail now from the terminal (--full = whole mailbox)
./budgetmail import <files…>     backfill from statement PDFs / CSV exports (account auto-detected)
./budgetmail backup [--mail]     zip of everything (no password) to a file, or emailed to yourself
./budgetmail restore <zip> | --from-mail   replace this machine's data from a zip, or the newest backup email
./budgetmail serve               run in the foreground (what the service runs)
./budgetmail install | uninstall register / remove the service (systemd on Linux, launchd on macOS)
```

Caveats: alerts fire at authorization, so amounts can drift when they post (tips) — the checksum shows the drift. No history
before you turned alerts on (except what your bank already emailed). Two identical purchases on the same day at the same
merchant collapse into one row.
