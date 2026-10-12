"""Generate a reviewable offline catalog from the public scanner's market feed.

Usage: python scripts/import_perpdexlist.py --output path/to/catalog.json
The bot never imports remote mappings automatically during trading.
"""

import argparse
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SOURCE = "https://perpdexlist.com/api/dashboard/markets-v2"
VENUES = {
    "asterdex": "aster",
    "hyperliquid": "hyperliquid",
    "entropy": "entropy",
    "extended_starknet": "extended",
    "nado": "nado",
    "lighter_rh": "lighter_robinhood",
    "hibachi": "hibachi",
    "polymarket": "polymarket_perps",
}


def native_symbol(venue, raw):
    if venue == "asterdex":
        return raw.removesuffix("USDT").upper()
    if venue == "entropy":
        return raw.removeprefix("io:")
    if venue == "nado":
        return raw.removesuffix("-PERP_USDT0")
    if venue in ("extended_starknet", "polymarket"):
        return raw.removesuffix("-USD").upper()
    if venue == "hibachi":
        return raw.removesuffix("/USDT-P").upper()
    return raw


def build_catalog(payload):
    rows = []
    for market in payload["markets"]:
        venue = market["exchange"]
        if venue not in VENUES:
            continue
        if venue == "asterdex" and not market["symbol"].endswith("USDT"):
            continue
        native = native_symbol(venue, market["symbol"])
        asset = market["asset"]
        # Only explicit native bundle prefixes on a source-confirmed asset.
        # Never infer a multiplier from price ratios alone.
        factor = "1000" if native.upper() in ("K" + asset.upper(), "1000" + asset.upper()) else "1"
        if native.upper().endswith(asset.upper()):
            prefix = native[:-len(asset)]
            if prefix.isdigit() and int(prefix) > 0:
                factor = str(int(prefix))
        rows.append(
            {
                "exchange": VENUES[venue],
                "native_symbol": native,
                "asset": asset,
                "base_units": factor,
                "source_symbol": market["symbol"],
                "source_class": market.get("class"),
            }
        )
    keys = [(r["exchange"], r["native_symbol"]) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate source market keys; review required")
    return {
        "source": SOURCE,
        "source_generated_at": payload.get("generated_at"),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "markets": sorted(rows, key=lambda r: (r["exchange"], r["native_symbol"])),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with urllib.request.urlopen(SOURCE, timeout=30) as response:
        catalog = build_catalog(json.load(response))
    Path(args.output).write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(catalog['markets'])} market mappings to {args.output}; review before use")
