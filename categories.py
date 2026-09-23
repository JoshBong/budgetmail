"""The fixed category taxonomy. Few and human, so the donut stays readable. rules.toml maps merchants into these;
bank-supplied categories (Chase CSV) are mapped through BANK_MAP."""
CATEGORIES = ["Food & Dining", "Shopping", "Travel", "Transport", "Bills & Subscriptions", "People", "Other"]
OTHER = "Other"

# What each box means, in the words the local classifier (classify.py) reads. Edit freely; nothing retrains.
HINTS = {
    "Food & Dining": "restaurants, cafes, bars, groceries, convenience stores, food delivery",
    "Shopping": "retail stores, online shopping, clothing, electronics, pharmacy, entertainment",
    "Travel": "flights, hotels, hostels, airlines, booking sites, tours",
    "Transport": "rideshare, taxi, transit, trains, buses, fuel, parking, tolls",
    "Bills & Subscriptions": "software subscriptions, cloud services, phone, utilities, insurance, memberships, tuition",
    "People": "money sent to a person, peer-to-peer payment, Zelle, Venmo, Apple Cash",
}


def hints() -> dict:
    """category → label text for the classifier: the name plus its hint, or just the name."""
    return {c: f"{c}: {HINTS[c]}" if HINTS.get(c) else c for c in CATEGORIES}

# bank category → ours
BANK_MAP = {
    "food & drink": "Food & Dining", "restaurants": "Food & Dining", "dining": "Food & Dining",
    "groceries": "Food & Dining", "grocery": "Food & Dining",
    "shopping": "Shopping", "entertainment": "Shopping", "gifts & donations": "Shopping", "personal": "Shopping",
    "travel": "Travel", "lodging": "Travel", "airlines": "Travel",
    "gas": "Transport", "automotive": "Transport", "transportation": "Transport",
    "bills & utilities": "Bills & Subscriptions", "utilities": "Bills & Subscriptions", "subscriptions": "Bills & Subscriptions",
    "professional services": "Bills & Subscriptions", "education": "Bills & Subscriptions", "fees & adjustments": "Bills & Subscriptions",
    "health & wellness": "Shopping", "healthcare": "Shopping", "pharmacy": "Shopping",
}


def normalize(cat: str | None) -> str:
    if not cat:
        return OTHER
    if cat in CATEGORIES:
        return cat
    return BANK_MAP.get(cat.strip().lower(), OTHER)
