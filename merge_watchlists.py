"""
merge_watchlists.py

Combines data/gecko_watchlist.json and data/dexscreener_watchlist.json -
the two independently-maintained, age-filtered (1-15 day launch age)
watchlists - into the single watchlist.json that index.html reads and
displays. Deduplication is by TOKEN CONTRACT (mint) ADDRESS ONLY: a token
that qualifies from both sources appears once in the output, tagged with
both source names.

Runs every 15 minutes via GitHub Actions, right after fetch_gecko.py and
fetch_dexscreener.py.
"""

import json
from pathlib import Path

GECKO_FILE = Path("data/gecko_watchlist.json")
DEX_FILE = Path("data/dexscreener_watchlist.json")
OUT_FILE = Path("watchlist.json")


def load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            print(f"WARN: {path} was unreadable, treating as empty.")
    return {}


def earliest(a, b):
    """Both are ISO datetime strings in the same format (UTC, isoformat()) -
    plain string comparison sorts them correctly."""
    if not a:
        return b
    if not b:
        return a
    return a if a < b else b


def merge():
    gecko = load_json(GECKO_FILE)
    dex = load_json(DEX_FILE)

    merged = {}

    for mint, entry in gecko.items():
        merged[mint] = dict(entry)
        merged[mint]["sources"] = ["geckoterminal"]

    for mint, entry in dex.items():
        if mint in merged:
            existing = merged[mint]
            existing["sources"].append("dexscreener")
            # Prefer DexScreener's live price/market-cap/liquidity when
            # present (it's the source we already enrich per-token from),
            # otherwise keep whatever GeckoTerminal had.
            existing["price_usd"] = entry.get("price_usd") if entry.get("price_usd") is not None else existing.get("price_usd")
            existing["market_cap"] = entry.get("market_cap") if entry.get("market_cap") is not None else existing.get("market_cap")
            existing["liquidity_usd"] = entry.get("liquidity_usd") if entry.get("liquidity_usd") is not None else existing.get("liquidity_usd")
            existing["boost_amount"] = entry.get("boost_amount")
            existing["boost_total_amount"] = entry.get("boost_total_amount")
            existing["added_at_utc"] = earliest(existing.get("added_at_utc"), entry.get("added_at_utc"))
            existing["last_seen_utc"] = max(
                existing.get("last_seen_utc") or "", entry.get("last_seen_utc") or ""
            ) or None
        else:
            merged[mint] = dict(entry)
            merged[mint]["sources"] = ["dexscreener"]

    OUT_FILE.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    both_count = sum(1 for e in merged.values() if len(e.get("sources", [])) > 1)
    print(
        f"Merged watchlist: {len(merged)} unique token(s) "
        f"({len(gecko)} from GeckoTerminal, {len(dex)} from DexScreener, "
        f"{both_count} appeared in both)."
    )


if __name__ == "__main__":
    merge()
