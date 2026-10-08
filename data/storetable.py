"""storetable.py — shared helpers for the AP-12 store-table scripts (coverage.py, build.py).

The name matcher here is a Python replica of the app's STORE-name matcher
(`ItemClassifier.storeNameContainsFragment`, AP-11-fix BE): possessive 's folds to s on both
sides, tokens are letter/number runs, the key's last token must equal the name token or its
plural (+s, +es, y->ies), never shorter. The brand tables and the big-format list are frozen JSON
beside this file (they were read off the Swift until AP-13 BI; BL deletes the Swift copies).
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.dirname(HERE)
ROOT = os.path.dirname(DOCS)
CACHE = os.path.join(HERE, "cache")
CATEGORY_MAP = os.path.join(HERE, "category-map.json")

OUR_CATEGORIES = ("grocery", "hardware", "pharmacy", "electronics", "general", "garden", "other")

# footprint-fit.py owns `metres` and `contains`; import it rather than copy (hyphenated filename).
_spec = importlib.util.spec_from_file_location("footprint_fit", os.path.join(DOCS, "footprint-fit.py"))
footprint_fit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(footprint_fit)
metres = footprint_fit.metres
contains = footprint_fit.contains
parse_fence = footprint_fit.parse_fence


# --- the matcher replica ----------------------------------------------------------------------

def fold_possessive(s: str) -> str:
    out = []
    n = len(s)
    i = 0
    while i < n:
        c = s[i]
        if (c in "'’" and i + 1 < n and s[i + 1] == "s" and i > 0 and s[i - 1].isalnum()
                and (i + 2 == n or not s[i + 2].isalnum())):
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def word_tokens(s: str) -> list[str]:
    return re.findall(r"[^\W_]+", s)


def tokens(s: str) -> list[str]:
    return word_tokens(fold_possessive((s or "").lower()))


def _last_ok(name_tok: str, key_tok: str) -> bool:
    return (name_tok == key_tok or name_tok == key_tok + "s" or name_tok == key_tok + "es"
            or (key_tok.endswith("y") and name_tok == key_tok[:-1] + "ies"))


def seq_in(name_toks: list[str], key_toks: list[str]) -> bool:
    """`tokenSequenceMatches` with the one-way plural on the last key token."""
    k = len(key_toks)
    if k == 0 or len(name_toks) < k:
        return False
    for i in range(len(name_toks) - k + 1):
        if name_toks[i:i + k - 1] == key_toks[:-1] and _last_ok(name_toks[i + k - 1], key_toks[-1]):
            return True
    return False


def store_name_contains(name: str, fragment: str) -> bool:
    return seq_in(tokens(name), tokens(fragment))


def norm(s: str) -> str:
    """Normalized name as the table stores it: possessive-folded, lowercase, alphanumerics only."""
    return "".join(ch for ch in fold_possessive((s or "").lower()) if ch.isalnum())


# Words that say what kind of place it is, not whose it is. A name that is only these never
# "agrees" with another name on its own.
GENERIC = {"the", "store", "stores", "market", "markets", "supermarket", "pharmacy", "grocery",
           "groceries", "shop", "mart", "food", "foods", "deli", "and", "of", "inc", "llc", "co",
           "center", "centre", "gas", "station", "fuel", "express", "supercenter", "supercentre",
           "drug", "drugs", "hardware", "home", "de", "la",
           "company", "corp", "corporation", "ltd"}

# Company-suffix tokens (AP-13 BI, review M1): dropped from the END of both names before
# `names_agree` compares them, so "Tractor Supply Co" agrees with "Tractor Supply Company".
COMPANY_SUFFIXES = {"co", "company", "corp", "corporation", "inc", "llc", "ltd"}


def strip_company_suffix(toks: list[str]) -> list[str]:
    """Trailing company-suffix tokens removed; a name that is only suffixes keeps its first token."""
    out = list(toks)
    while len(out) > 1 and out[-1] in COMPANY_SUFFIXES:
        out.pop()
    return out


def names_agree(a: str, b: str) -> bool:
    """Two place names name the same business, once trailing company suffixes are dropped from
    both: their joined tokens are equal (catches "7-Eleven" vs "7 Eleven", "Walgreens" vs
    "Wal greens"); or one's token sequence sits inside the other's (one-way plural on the last
    token) and the shorter carries a non-generic token.

    The app's `StoreTable.namesAgree` (StoreTable.swift) is a line-for-line port: change one,
    change both."""
    ta, tb = strip_company_suffix(tokens(a)), strip_company_suffix(tokens(b))
    if not ta or not tb:
        return False
    if "".join(ta) == "".join(tb):
        return True
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not any(t not in GENERIC for t in short):
        return False
    return seq_in(long_, short)


# --- the brand tables (frozen JSON) --------------------------------------------------------------

# Frozen from the Swift on 2026-09-26 (AP-13 Part BI): AP-13 BL deletes the Swift tables, so the
# pipeline reads these files. The chain carries table the app keeps is BK's; these stay as they were.
TABLE_FILES = {
    "brandCategories": "brand-categories.json",       # was brandCategoryOverrides (Reminder.swift)
    "brandDestinations": "brand-destinations.json",   # was brandDestinationOverrides (Reminder.swift)
    "bigFormat": "big-format-brands.json",            # was bigFormatBrandFragments (StoreFinder.swift)
    "convenience": "convenience-fragments.json",      # was convenienceNameFragments (StoreFinder.swift)
}

_T: dict | None = None


def tables() -> dict:
    global _T
    if _T is None:
        _T = {k: json.load(open(os.path.join(HERE, f)))["table"] for k, f in TABLE_FILES.items()}
    return _T


_CC: dict | None = None


def chain_carries() -> dict:
    """The chain carries table (chain-carries.json): key -> {carries, convenienceRowOK}."""
    global _CC
    if _CC is None:
        _CC = json.load(open(os.path.join(HERE, "chain-carries.json")))["table"]
    return _CC


def chain_key(brand: str) -> str | None:
    """The chain carries key a row's brand matches (store matcher; the longest matching key wins,
    so "Lowes Foods" is a Lowes Foods key, not lowe's). The app's StoreTable.chainKey is the port."""
    if not brand:
        return None
    best = None
    for k in sorted(chain_carries()):          # ties: the first key in sorted order
        if store_name_contains(brand, k) and (best is None or len(k) > len(best)):
            best = k
    return best


def brand_hits(name: str) -> list[str]:
    """Brand-table keys the name carries (store matcher), a key dropped when a longer hit contains it."""
    t = tables()
    keys = set(t["brandCategories"]) | set(t["brandDestinations"])
    hits = [k for k in keys if store_name_contains(name, k)]
    return sorted(h for h in hits if not any(norm(o) != norm(h) and norm(h) in norm(o) for o in hits))


def brand_categories(name: str) -> set[str]:
    cats: set[str] = set()
    for k in brand_hits(name):
        cats.update(tables()["brandCategories"].get(k, []))
    return cats


def is_big_format(*names: str) -> bool:
    frags = tables()["bigFormat"]
    return any(n and any(store_name_contains(n, f) for f in frags) for n in names)


# --- the category map ---------------------------------------------------------------------------

_CM: dict | None = None


def category_map() -> dict:
    global _CM
    if _CM is None:
        _CM = json.load(open(CATEGORY_MAP))
    return _CM


def osm_kind(tags: dict) -> tuple[str, str] | None:
    """('shop', value) or ('amenity', value) for a store-table feature, else None."""
    if tags.get("shop"):
        return ("shop", tags["shop"])
    a = tags.get("amenity")
    if a in ("pharmacy", "fuel", "marketplace"):
        return ("amenity", a)
    return None


def map_kind(kind: tuple[str, str] | None) -> dict | None:
    """The map entry {categories, convenience} for a (key, value), or None when unmapped."""
    if not kind:
        return None
    table = category_map()[kind[0]]
    if kind[1] in table:
        return table[kind[1]]
    # OSM allows a semicolon list ("coffee;tea"): the union of the mapped parts, convenience if any is
    parts = [table[p.strip()] for p in kind[1].split(";") if p.strip() in table]
    if not parts:
        return None
    cats = sorted({c for e in parts for c in e["categories"]}, key=OUR_CATEGORIES.index)
    if len(cats) > 1 and "other" in cats:
        cats.remove("other")
    return {"categories": cats, "convenience": any(e["convenience"] for e in parts)}


# --- geometry -----------------------------------------------------------------------------------

def ring_centroid(ring: list) -> tuple[float, float]:
    """Area centroid of a [lon, lat] ring (planar, fine at store scale); vertex mean if degenerate."""
    a = cx = cy = 0.0
    n = len(ring)
    for i in range(n - 1):
        x0, y0 = ring[i]
        x1, y1 = ring[i + 1]
        c = x0 * y1 - x1 * y0
        a += c
        cx += (x0 + x1) * c
        cy += (y0 + y1) * c
    if abs(a) < 1e-14:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(ys) / len(ys), sum(xs) / len(xs)
    a *= 0.5
    return cy / (6 * a), cx / (6 * a)          # (lat, lon)


def ring_area(ring: list) -> float:
    return abs(sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
                   for i in range(len(ring) - 1))) / 2


def far_m(lat: float, lon: float, ring: list) -> float:
    """The footprint-fit rule: max metres from the point to the outline's vertices."""
    return max(metres(lat, lon, v[1], v[0]) for v in ring)


def lot_adjoins(outline: list, parking_rings: list, lot_m: float = 25.0) -> bool:
    """The AZ rule: a parking vertex within 25 m of an outline vertex."""
    return any(metres(v[1], v[0], w[1], w[0]) <= lot_m for q in parking_rings for w in q for v in outline)


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)
