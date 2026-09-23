"""Merchants nothing else can place → a category, decided once per merchant by a small local LLM through Ollama.

Runs after every sync over the merchants that still resolve to "Other". A merchant string like "LAWSON" or "CURSOR.COM"
is a world-knowledge question, not a text-classification one (zero-shot encoders scored at chance on this ledger), so
the model has to be one that knows what the shop is. The category list is whatever categories exist at call time —
rename or add a box and the prompt follows, nothing retrains. Precedence is unchanged: a drag beats rules.toml, rules
beat the bank's own category, and all of those beat this. "Other" from the model is an abstention: the merchant stays
Other and is asked again after RETRY_DAYS. Optional — with no Ollama running this is a no-op."""
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import categories
import ledger
import report

DEFAULTS = {"enabled": True, "url": "http://localhost:11434", "model": "qwen2.5:3b"}
RETRY_DAYS = 30
_missing_said = False


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("classifier", {})}


def available(cfg: dict | None = None) -> bool:
    """Ollama answering, and the configured model pulled."""
    s = settings(cfg or {})
    try:
        with urllib.request.urlopen(s["url"].rstrip("/") + "/api/tags", timeout=3) as r:
            names = {m["name"] for m in json.load(r).get("models", [])}
        return s["model"] in names or s["model"] + ":latest" in names
    except (urllib.error.URLError, OSError, ValueError):
        return False


def candidates(con, cl: report.Classifier) -> dict:
    """merchant_key → one description, for spend that still lands in Other with no verdict worth keeping."""
    tried = {r["merchant"]: r for r in con.execute("SELECT merchant, category, tried_at FROM merchant_model")}
    stale = (date.today() - timedelta(days=RETRY_DAYS)).isoformat()
    out = {}
    for row, cat in report.spend_rows(con, cl):
        if cat != categories.OTHER:
            continue
        k = report.merchant_key(row)
        t = tried.get(k)
        if t and (t["category"] or t["tried_at"] > stale):
            continue
        out.setdefault(k, row["description"] or row["counterparty"] or "")
    return out


EXAMPLES = (("FAMILYMART TAIPEI", "Food & Dining"), ("GITHUB.COM SAN FRANCISCO", "Bills & Subscriptions"),
            ("GRAB* A-3XK2 KUALA LUMPUR", "Transport"), ("UNIQLO SHINJUKU", "Shopping"), ("AGODA.COM SINGAPORE", "Travel"))


def prompt(desc: str, cats: list[str]) -> str:
    hints = categories.hints()
    lines = "\n".join(f"{c} = {hints[c].split(': ', 1)[1]}" if ": " in hints[c] else c for c in cats)
    shots = "\n".join(f"{d} -> {c}" for d, c in EXAMPLES if c in cats)
    return ("You categorize credit-card merchant strings for a personal budget. The string is the bank's abbreviated "
            "merchant name, sometimes followed by a city. The city or country only says where the shop is; it never "
            "makes the purchase Travel. Decide from what kind of business the merchant is. Convenience stores and "
            f"supermarkets are Food & Dining; software and web services are Bills & Subscriptions. Answer \"{categories.OTHER}\" "
            f"only when the name tells you nothing.\n\n{lines}\n{categories.OTHER} = the name tells you nothing\n\n"
            f"Examples:\n{shots}\n\nMerchant: {desc}\nAnswer with JSON: {{\"category\": \"...\"}}")


def predict_ollama(texts: list[str], cats: list[str], s: dict) -> list[tuple[str, float]]:
    """→ (category, 1.0) per text; an answer outside the list comes back as ("", 0.0). One request per merchant."""
    out = []
    for t in texts:
        body = json.dumps({"model": s["model"], "prompt": prompt(t, cats), "stream": False, "format": "json",
                           "options": {"temperature": 0, "num_predict": 40}}).encode()
        req = urllib.request.Request(s["url"].rstrip("/") + "/api/generate", body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                cat = json.loads(json.load(r)["response"]).get("category", "")
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            cat = ""
        out.append((cat, 1.0) if cat in cats else ("", 0.0))
    return out


def run(con, cfg: dict, cl: report.Classifier | None = None, predict=None, dry: bool = False) -> dict:
    """Classify every candidate merchant and write the verdicts. `predict(texts, cats)` is injectable for tests."""
    global _missing_said
    s = settings(cfg)
    if not s["enabled"]:
        return {}
    if predict is None:
        if not available(cfg):
            if not _missing_said:
                print(f"classifier: no Ollama with {s['model']} at {s['url']} — merchants stay 'Other' (see README)", flush=True)
                _missing_said = True
            return {}
        predict = lambda texts, cats: predict_ollama(texts, cats, s)   # noqa: E731
    cl = cl or report.Classifier(con)
    cands = candidates(con, cl)
    if not cands:
        return {"classified": 0, "abstained": 0}
    cats = [c for c in categories.CATEGORIES if c != categories.OTHER]
    keys = list(cands)
    t0 = time.time()
    verdicts = predict([cands[k] for k in keys], cats)
    now = datetime.now().isoformat(timespec="seconds")
    n = a = 0
    decided = []
    for k, (cat, score) in zip(keys, verdicts):
        cat = cat if cat in cats else None
        n += cat is not None; a += cat is None
        decided.append((k, cands[k], cat, round(score, 3)))
        if not dry:
            con.execute("INSERT INTO merchant_model VALUES (?,?,?,?,?) ON CONFLICT(merchant) DO UPDATE SET "
                        "category=excluded.category, score=excluded.score, model=excluded.model, tried_at=excluded.tried_at",
                        (k, cat, score, s["model"], now))
    if not dry:
        con.commit()
    return {"classified": n, "abstained": a, "seconds": round(time.time() - t0, 1), "decided": decided}
