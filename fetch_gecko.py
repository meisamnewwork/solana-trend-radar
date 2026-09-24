"""
fetch_gecko.py

Runs every 15 minutes (via GitHub Actions cron). Maintains its OWN growing
watchlist (data/gecko_watchlist.json), separate from the DexScreener one,
so the two sources can later be merged (see merge_watchlists.py).

Each run:
  1. Calls GeckoTerminal's free, public, keyless trending-pools API for
     Solana:
       GET https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?include=base_token
  2. For each trending pool, checks the pool's age (now - pool_created_at).
     Only pools between MIN_AGE_DAYS and MAX_AGE_DAYS old are kept - this
     is meant to catch tokens that already launched (not brand new, minutes
     old) but are still fresh, per the user's "1 to 15 days" requirement.
     A pool with no creation-date data is skipped (excluded) rather than
     guessed at.
  3. Loads/updates data/gecko_watchlist.json, keyed by token contract
     (mint) address - same growing-list/dedup pattern as the rest of this
     project. A token that ages out of the 1-15 day window on a later run
     is simply left alone (not deleted, not refreshed) - it stays as a
     historical record with its last known data.
  4. Saves data/gecko_watchlist.json.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

TRENDING_API_URL = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?include=base_token"
OUT_FILE = Path("data/gecko_watchlist.json")

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


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def age_days(dt):
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def fetch_trending_pools():
    resp = requests.get(TRENDING_API_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    pools = payload.get("data") or []
    included = payload.get("included") or []
    token_lookup = {
        item["id"]: item for item in included if item.get("type") == "token"
    }
    return pools, token_lookup


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
        pools, token_lookup = fetch_trending_pools()
    except Exception as e:
        print(f"ERROR: failed to fetch GeckoTerminal trending pools: {e}")
        return

    if not pools:
        print("No trending pools returned this run.")
        return

    watchlist = load_watchlist()
    kept = 0
    skipped_age = 0
    skipped_no_age = 0

    for rank, pool in enumerate(pools, start=1):
        attrs = pool.get("attributes") or {}
        pool_address = attrs.get("address")
        if not pool_address:
            continue

        base_token_rel = (
            (pool.get("relationships") or {}).get("base_token", {}).get("data", {})
        )
        token_res_id = base_token_rel.get("id")  # e.g. "solana_<mint>"
        token = token_lookup.get(token_res_id) if token_res_id else None

        if token:
            token_attrs = token.get("attributes") or {}
            mint = token_attrs.get("address")
            name = token_attrs.get("name")
            symbol = token_attrs.get("symbol")
        else:
            mint = token_res_id.split("_", 1)[-1] if token_res_id else None
            name = attrs.get("name")
            symbol = None

        if not mint:
            continue

        created_dt = parse_iso(attrs.get("pool_created_at"))
        age = age_days(created_dt)

        if age is None:
            skipped_no_age += 1
            continue
        if not (MIN_AGE_DAYS <= age <= MAX_AGE_DAYS):
            skipped_age += 1
            continue

        price_usd = to_float(attrs.get("base_token_price_usd"))
        market_cap = to_float(attrs.get("market_cap_usd")) or to_float(attrs.get("fdv_usd"))
        liquidity = to_float(attrs.get("reserve_in_usd"))

        entry = {
            "token_address": mint,
            "symbol": symbol,
            "name": name,
            "pair_address": pool_address,
            "launched_at_utc": created_dt.isoformat(),
            "age_days_at_last_seen": round(age, 2),
            "last_seen_utc": now,
            "last_rank": rank,
            "price_usd": price_usd,
            "market_cap": market_cap,
            "liquidity_usd": liquidity,
            "dexscreener_url": f"https://dexscreener.com/solana/{pool_address}",
            "rugcheck_url": f"https://rugcheck.xyz/tokens/{mint}",
        }

        if mint in watchlist:
            entry["added_at_utc"] = watchlist[mint].get("added_at_utc", now)
            entry["first_seen_rank"] = watchlist[mint].get("first_seen_rank", rank)
        else:
            entry["added_at_utc"] = now
            entry["first_seen_rank"] = rank

        watchlist[mint] = entry
        kept += 1

    save_watchlist(watchlist)
    print(
        f"GeckoTerminal: {kept} token(s) in the {MIN_AGE_DAYS}-{MAX_AGE_DAYS} day "
        f"age window kept/updated ({skipped_age} outside age range, "
        f"{skipped_no_age} missing age data). Watchlist size: {len(watchlist)}"
    )


if __name__ == "__main__":
    main()
