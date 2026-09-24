"""
fetch_dexscreener.py

Runs every 15 minutes (via GitHub Actions cron). Maintains its OWN growing
watchlist (data/dexscreener_watchlist.json), separate from the GeckoTerminal
one, so the two sources can later be merged (see merge_watchlists.py).

NOTE ON "TRENDING" VS "BOOSTED": DexScreener's public API has no separate
"trending tokens" endpoint - what DexScreener's own website shows as
"trending" is driven by its internal ranking algorithm, in which paid
boosts are the biggest signal. So the closest thing to "DexScreener
trending" available via free API is its boosted-token feeds, which is what
this script uses (combining BOTH feeds below for broader coverage):
  GET https://api.dexscreener.com/token-boosts/latest/v1  (recently boosted)
  GET https://api.dexscreener.com/token-boosts/top/v1     (most-boosted overall)

Each run:
  1. Fetches both boost feeds, filters to chainId == "solana", and merges
     them (a token boosted enough to appear in both is only counted once,
     tagged with both sources).
  2. For each Solana boosted token, calls DexScreener's pairs-by-token
     endpoint to get its best (highest-liquidity) trading pair, including
     that pair's creation date:
       GET https://api.dexscreener.com/token-pairs/v1/solana/{tokenAddress}
  3. Only tokens whose best pair is between MIN_AGE_DAYS and MAX_AGE_DAYS
     old are kept, per the user's "1 to 15 days" launch-age requirement.
  4. Loads/updates data/dexscreener_watchlist.json, keyed by token contract
     (mint) address. A token that ages out of the window on a later run is
     left alone (not deleted/refreshed) - it stays as a historical record.
  5. Saves data/dexscreener_watchlist.json.

All endpoints above are free, public, and require no API key. Rate limit is
60 requests/minute; this script makes 2 calls for the boost feeds plus one
extra pairs-lookup call per Solana-boosted token per run, which stays well
within that limit for realistic boost-feed sizes (~20-30 tokens/feed).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

LATEST_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
TOP_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
PAIRS_API_URL = "https://api.dexscreener.com/token-pairs/v1/solana/{address}"
OUT_FILE = Path("data/dexscreener_watchlist.json")
CHAIN_ID = "solana"

MIN_AGE_DAYS = 1
MAX_AGE_DAYS = 15

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "solana-trend-radar/1.0 (+github actions scheduled job)",
}


def to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_pair_created_at(v):
    """pairCreatedAt has been observed as either a unix-ms integer or an
    ISO datetime string depending on endpoint/version - handle both."""
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def age_days(dt):
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def fetch_boosted_solana_tokens():
    """Fetch both boost feeds, filter to Solana, dedup by tokenAddress,
    tagging which feed(s) each token came from."""
    merged = {}
    for url, source_name in ((LATEST_BOOSTS_URL, "latest-boosted"), (TOP_BOOSTS_URL, "top-boosted")):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            items = resp.json() or []
        except Exception as e:
            print(f"WARN: failed to fetch {url}: {e}")
            continue
        for item in items:
            if item.get("chainId") != CHAIN_ID:
                continue
            addr = item.get("tokenAddress")
            if not addr:
                continue
            if addr in merged:
                merged[addr]["_boost_sources"].append(source_name)
            else:
                item["_boost_sources"] = [source_name]
                merged[addr] = item
    return list(merged.values())


def fetch_best_pair_for_token(token_address):
    url = PAIRS_API_URL.format(address=token_address)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        pairs = resp.json() or []
    except Exception as e:
        print(f"WARN: could not fetch pairs for {token_address}: {e}")
        return None

    if not pairs:
        return None

    def liquidity_usd(p):
        return to_float((p.get("liquidity") or {}).get("usd")) or 0

    return max(pairs, key=liquidity_usd)


def load_watchlist():
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            print(f"WARN: {OUT_FILE} was unreadable, starting fresh.")
    return {}


def save_watchlist(watchlist):
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(watchlist, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main():
    now = datetime.now(timezone.utc).isoformat()

    try:
        boosted = fetch_boosted_solana_tokens()
    except Exception as e:
        print(f"ERROR: failed to fetch DexScreener boosted tokens: {e}")
        return

    if not boosted:
        print("No Solana boosted tokens returned this run.")
        return
    print(f"Got {len(boosted)} Solana boosted token entrie(s) from DexScreener.")

    watchlist = load_watchlist()
    kept = 0
    skipped_age = 0
    skipped_no_pair = 0

    for item in boosted:
        mint = item.get("tokenAddress")
        if not mint:
            continue

        best_pair = fetch_best_pair_for_token(mint)
        if not best_pair:
            skipped_no_pair += 1
            continue

        created_dt = parse_pair_created_at(best_pair.get("pairCreatedAt"))
        age = age_days(created_dt)
        if age is None or not (MIN_AGE_DAYS <= age <= MAX_AGE_DAYS):
            skipped_age += 1
            continue

        base_token = best_pair.get("baseToken") or {}

        entry = {
            "token_address": mint,
            "symbol": base_token.get("symbol"),
            "name": base_token.get("name"),
            "pair_address": best_pair.get("pairAddress"),
            "launched_at_utc": created_dt.isoformat(),
            "age_days_at_last_seen": round(age, 2),
            "last_seen_utc": now,
            "boost_amount": to_float(item.get("amount")),
            "boost_total_amount": to_float(item.get("totalAmount")),
            "boost_sources": item.get("_boost_sources", []),
            "price_usd": to_float(best_pair.get("priceUsd")),
            "market_cap": to_float(best_pair.get("marketCap")) or to_float(best_pair.get("fdv")),
            "liquidity_usd": to_float((best_pair.get("liquidity") or {}).get("usd")),
            "dexscreener_url": best_pair.get("url") or item.get("url") or f"https://dexscreener.com/solana/{mint}",
            "rugcheck_url": f"https://rugcheck.xyz/tokens/{mint}",
        }

        if mint in watchlist:
            entry["added_at_utc"] = watchlist[mint].get("added_at_utc", now)
        else:
            entry["added_at_utc"] = now

        watchlist[mint] = entry
        kept += 1

    save_watchlist(watchlist)
    print(
        f"DexScreener: {kept} token(s) in the {MIN_AGE_DAYS}-{MAX_AGE_DAYS} day "
        f"age window kept/updated ({skipped_age} outside age range, "
        f"{skipped_no_pair} had no pair data). Watchlist size: {len(watchlist)}"
    )


if __name__ == "__main__":
    main()
