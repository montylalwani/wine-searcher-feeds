#!/usr/bin/env python3
"""
lightspeed_to_winesearcher.py

Pulls item, inventory, and price data directly from the Lightspeed Retail
(R-Series) API and writes Wine-Searcher-compliant datafeeds -- ONE PER
STORE LOCATION -- matching Wine-Searcher's official datafeed spec at
https://www.wine-searcher.com/trade/datafeed

This replaces routing your Wine-Searcher feed through CityHive -- it pulls
straight from your POS, which is your real source of truth for price/stock.

-------------------------------------------------------------------------
WINE-SEARCHER DATAFEED FORMAT (per their published spec)
-------------------------------------------------------------------------
Field order, pipe-delimited, ONE PRODUCT PER LINE, no header row:

  SKU|name|description|vintage|unit-size|price|stock|url|min-order|tax|
  offer-type|delivery-time|LWIN|imageurl

- Must be hosted at a plain public URL, NOT password protected.
- LWIN (a wine-industry standard product code) is left blank per item
  unless you have LWIN codes -- Wine-Searcher will still match products
  by name/vintage/producer without it.
- "tax" field: US listings should show prices EXCLUSIVE of sales tax per
  Wine-Searcher's US pricing convention -- set to "excl" below. If Wine-
  Searcher support tells you a different accepted value, update TAX_STATUS.

-------------------------------------------------------------------------
SETUP (see README.md for the full walkthrough)
-------------------------------------------------------------------------
1. Register an app at https://cloud.lightspeedapp.com/oauth/register.php
   to get a CLIENT_ID and CLIENT_SECRET.
2. Complete the OAuth authorization flow once, by hand, to get a
   REFRESH_TOKEN. (Steps in README.md.)
3. Fill in the CONFIG block below (or set environment variables of the
   same names -- recommended so secrets aren't hardcoded).
4. Run:  python lightspeed_to_winesearcher.py
5. It writes one .txt feed per location configured in LOCATIONS below,
   e.g. wine_searcher_feed_las_olas.txt and wine_searcher_feed_bayview.txt
6. Host each .txt file at a stable, PUBLIC, NON-PASSWORD-PROTECTED URL so
   Wine-Searcher can fetch it, then give them each store's feed URL.

-------------------------------------------------------------------------
MULTI-STORE PRICING
-------------------------------------------------------------------------
Your Lightspeed account uses Pricing Levels named per location (e.g.
"Las Olas", "Bayview", "Beach", "Sunrise"). Each entry in LOCATIONS below
maps:
  - price_level: which Lightspeed Pricing Level to use for THIS feed
    (both locations are currently set to "Bayview" per your instruction --
    same price shown at both stores, using Bayview's lower price)
  - shop_match: text matched against Lightspeed Shop names to isolate
    stock-on-hand for just that location (not combined across stores)
  - output_file: the filename written for that store's feed
  - store_url_template: that store's product page URL pattern

-------------------------------------------------------------------------
NOTES ON FIELD MAPPING
-------------------------------------------------------------------------
Lightspeed does not have a native "vintage" field. This script tries, in
order:
  1. A custom field named "Vintage" on the Item (if you've created one)
  2. A 4-digit year found anywhere in the item Description/Name

Adjust WINE_CATEGORY_NAMES below to match how your Lightspeed categories
are actually named, so only wine/spirits items -- not cigars, tobacco,
mixers, etc. -- go into the feed.
"""

import os
import re
import sys
import time
import requests

# =========================================================================
# CONFIG -- fill these in, or set as environment variables instead
# =========================================================================
CLIENT_ID = os.environ.get("LS_CLIENT_ID", "7dfceeee0dcd1f17eabfacd5b1d28261bc595aab95fcb322760654390bcfc531")
CLIENT_SECRET = os.environ.get("LS_CLIENT_SECRET", "1345e6f21ee779bd3b5a122ff7705e89caa92b84e74a062fc66391e97e7749f7")
REFRESH_TOKEN = os.environ.get("LS_REFRESH_TOKEN", "1e821d89ba59084c8d4317bf81c59149d103723c")
ACCOUNT_ID = os.environ.get("LS_ACCOUNT_ID", "290668")

# Which Lightspeed Category names should be included in the feed.
WINE_CATEGORY_NAMES = {"Wine", "Spirits", "Beer", "Champagne", "Sake", "Vodka",
                       "Tequila", "Whiskey", "Rum", "Gin", "Cognac", "Liqueur"}

# US convention: Wine-Searcher lists US merchant prices EXCLUSIVE of sales
# tax. Per Wine-Searcher's datafeed spec, this field should read exactly
# "Ex. tax" or "Inc. tax" -- not an abbreviation.
TAX_STATUS = "Ex. tax"

# Default fields applied to every product line (override per-item below
# if you track these more granularly in Lightspeed).
DEFAULT_MIN_ORDER = "1"
DEFAULT_OFFER_TYPE = "retail"
DEFAULT_DELIVERY_TIME = "1-3 business days"
DEFAULT_UNIT_SIZE = "750ml"

# Local path to the cloned wine-searcher-feeds GitHub repo. The script
# writes feed files directly here, then commits and pushes automatically
# so GitHub Pages always serves the latest data.
GIT_REPO_DIR = r"C:\Users\monty\wine-searcher-feeds"

# One entry per store you want a separate Wine-Searcher feed for.
LOCATIONS = [
    {
        "name": "Las Olas",
        "price_level": "Bayview",   # using Bayview's lower price for both stores
        "shop_match": "Las Olas",   # stock still isolated to Las Olas only
        # NOTE: this points to the general shop page, not per-item product
        # pages, because Lightspeed SKUs don't map to CityHive's internal
        # product IDs. See README for how to get real per-item URLs from
        # CityHive support.
        "store_url_template": "https://oceansliquor.com/shop/",
        "output_file": os.path.join(GIT_REPO_DIR, "wine_searcher_feed_las_olas.txt"),
    },
    {
        "name": "Bayview",
        "price_level": "Bayview",
        "shop_match": "Bayview",
        "store_url_template": "https://oceansliquor.com/shop/",
        "output_file": os.path.join(GIT_REPO_DIR, "wine_searcher_feed_bayview.txt"),
    },
]

# Lightspeed API base
API_BASE = "https://api.lightspeedapp.com/API/V3"
TOKEN_URL = "https://cloud.lightspeedapp.com/oauth/access_token.php"

# =========================================================================


def git_commit_and_push():
    """Commit any changed feed files in GIT_REPO_DIR and push to GitHub,
    so GitHub Pages serves the freshly generated data. Safe to call even
    if nothing changed (git will just report nothing to commit)."""
    import subprocess

    def run(cmd):
        result = subprocess.run(
            cmd, cwd=GIT_REPO_DIR, capture_output=True, text=True
        )
        return result

    run(["git", "add", "-A"])
    commit = run(["git", "commit", "-m", "Auto-update Wine-Searcher feeds"])
    combined_output = (commit.stdout + commit.stderr).lower()
    if "nothing to commit" in combined_output:
        print("  No changes to commit (feeds are already up to date).")
        return
    if commit.returncode != 0:
        print(f"  ERROR: git commit failed, not pushing.", file=sys.stderr)
        print(f"  {commit.stdout.strip()}", file=sys.stderr)
        print(f"  {commit.stderr.strip()}", file=sys.stderr)
        return
    push = run(["git", "push", "origin", "main"])
    if push.returncode != 0:
        print(f"  ERROR pushing to GitHub: {push.stderr.strip()}", file=sys.stderr)
    else:
        print("  Pushed updated feeds to GitHub Pages successfully.")


def get_access_token():
    """Exchange the long-lived refresh token for a short-lived access token."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(session, path, params=None):
    """GET from the Lightspeed API with basic rate-limit backoff."""
    url = f"{API_BASE}/Account/{ACCOUNT_ID}/{path}"
    return _get_url(session, url, params=params)


def _get_url(session, url, params=None):
    """GET a fully-formed or relative URL, with rate-limit backoff and
    automatic access-token refresh if the token expires mid-run (Lightspeed
    tokens expire after 30 minutes, which can happen on large catalogs)."""
    if url.startswith("/"):
        url = f"https://api.lightspeedapp.com{url}"
    while True:
        resp = session.get(url, params=params, timeout=60)
        if resp.status_code == 401:
            print("  Access token expired mid-run, refreshing...", file=sys.stderr)
            new_token = get_access_token()
            session.headers.update({"Authorization": f"Bearer {new_token}"})
            continue
        if resp.status_code == 429:
            wait = float(resp.headers.get("X-RateLimit-Wait", 5))
            print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            print(f"  ERROR {resp.status_code} response body:", file=sys.stderr)
            print(f"  {resp.text}", file=sys.stderr)
        resp.raise_for_status()
        return resp.json()


def fetch_shops(session):
    """Return {shopID: shopName} for every Shop (location) on the account."""
    data = api_get(session, "Shop.json", params={"limit": 100})
    shops = data.get("Shop", [])
    if isinstance(shops, dict):
        shops = [shops]
    return {s.get("shopID"): s.get("name", "") for s in shops}


def fetch_all_items(session):
    """
    Paginate through every Item using Lightspeed's cursor-based next/previous
    URLs (the old offset parameter is deprecated). Prices are included in
    every Item response by default -- do NOT request "Prices" as a relation,
    Lightspeed rejects that as an invalid relation name.
    """
    items = []
    limit = 100

    params = {
        "limit": limit,
        "load_relations": (
            '["ItemShops","Category","CustomFieldValues","Images"]'
        ),
    }
    url = f"{API_BASE}/Account/{ACCOUNT_ID}/Item.json"

    while url:
        data = _get_url(session, url, params=params)
        batch = data.get("Item", [])
        if isinstance(batch, dict):
            batch = [batch]
        if not batch:
            break

        items.extend(batch)
        print(f"  Fetched {len(items)} items so far...")

        next_url = data.get("@attributes", {}).get("next", "")
        if not next_url:
            break
        url = next_url
        params = None

    return items


def extract_vintage(item):
    """Look for a Vintage custom field first, then fall back to a 4-digit
    year found in the item description or name."""
    custom_fields = item.get("CustomFieldValues", {}).get("CustomFieldValue", [])
    if isinstance(custom_fields, dict):
        custom_fields = [custom_fields]
    for cf in custom_fields:
        val = cf.get("value", "")
        match = re.search(r"\b(19|20)\d{2}\b", str(val))
        if match:
            return match.group(0)

    text = f"{item.get('description', '')} {item.get('longDescription', '')}"
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return match.group(0) if match else ""


def extract_price(item, price_level_name):
    """Return the price tagged with this location's Pricing Level name.
    Falls back to Default if that specific level isn't set for this item."""
    prices = item.get("Prices", {}).get("ItemPrice", [])
    if isinstance(prices, dict):
        prices = [prices]

    for p in prices:
        if p.get("useType") == price_level_name:
            return p.get("amount", "")
    for p in prices:
        if p.get("useType") == "Default":
            return p.get("amount", "")
    return prices[0].get("amount", "") if prices else ""


def extract_stock(item, shop_id_to_name, shop_match):
    """Sum quantity-on-hand only for shops whose name contains shop_match
    (case-insensitive) -- i.e. just this one location."""
    shops = item.get("ItemShops", {}).get("ItemShop", [])
    if isinstance(shops, dict):
        shops = [shops]
    total = 0.0
    shop_match_lower = shop_match.lower()
    for s in shops:
        shop_id = s.get("shopID")
        shop_name = shop_id_to_name.get(shop_id, "")
        if shop_match_lower not in shop_name.lower():
            continue
        try:
            total += float(s.get("qoh", 0))
        except (TypeError, ValueError):
            pass
    return int(total)


def extract_category_name(item):
    cat = item.get("Category", {})
    return cat.get("name", "") if isinstance(cat, dict) else ""


def extract_image_url(item):
    images = item.get("Images", {}).get("Image", [])
    if isinstance(images, dict):
        images = [images]
    if images:
        return images[0].get("baseImageURL", "") or images[0].get("url", "")
    return ""


def clean_field(value):
    """Strip pipe characters and newlines from a field so they can't break
    the pipe-delimited format Wine-Searcher expects."""
    text = str(value) if value is not None else ""
    return text.replace("|", "-").replace("\n", " ").replace("\r", " ").strip()


def build_feed_line(item, location, shop_id_to_name):
    """Return one pipe-delimited line matching Wine-Searcher's exact spec:
    SKU|name|description|vintage|unit-size|price|stock|url|min-order|tax|
    offer-type|delivery-time|LWIN|imageurl"""
    sku = item.get("customSku") or item.get("systemSku") or item.get("itemID", "")
    name = item.get("description", "")
    long_desc = item.get("longDescription", "") or name
    vintage = extract_vintage(item)
    price = extract_price(item, location["price_level"])
    stock = extract_stock(item, shop_id_to_name, location["shop_match"])
    image_url = extract_image_url(item)
    url = location["store_url_template"].format(
        sku=sku, item_id=item.get("itemID", "")
    )
    lwin = ""  # left blank -- populate if you obtain LWIN codes for your wines

    fields = [
        sku, name, long_desc, vintage, DEFAULT_UNIT_SIZE, price, stock, url,
        DEFAULT_MIN_ORDER, TAX_STATUS, DEFAULT_OFFER_TYPE,
        DEFAULT_DELIVERY_TIME, lwin, image_url,
    ]
    return "|".join(clean_field(f) for f in fields)


def main():
    if CLIENT_ID.startswith("YOUR_"):
        print(
            "ERROR: Fill in CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, and "
            "ACCOUNT_ID at the top of this script (or set them as "
            "environment variables) before running.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Requesting access token...")
    access_token = get_access_token()

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}"})

    print("Fetching shop/location list...")
    shop_id_to_name = fetch_shops(session)
    print(f"  Found {len(shop_id_to_name)} shop(s): "
          f"{', '.join(shop_id_to_name.values())}")

    print("Fetching items from Lightspeed Retail...")
    items = fetch_all_items(session)
    print(f"Fetched {len(items)} total items.")
    import csv
    uncategorized = [item for item in items if not extract_category_name(item)]
    print(f"\nFound {len(uncategorized)} uncategorized items. Writing to CSV...")

    csv_path = os.path.join(GIT_REPO_DIR, "uncategorized_items.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["SKU", "Name", "Description", "ItemID"])
        for item in uncategorized:
            sku = item.get("customSku") or item.get("systemSku") or item.get("itemID", "")
            name = item.get("description", "")
            long_desc = item.get("longDescription", "")
            writer.writerow([sku, name, long_desc, item.get("itemID", "")])

    print(f"Wrote {len(uncategorized)} uncategorized items to {csv_path}")
    sys.exit()
    wine_items = []
    for item in items:
        category_name = extract_category_name(item)
        if WINE_CATEGORY_NAMES and category_name not in WINE_CATEGORY_NAMES:
            continue
        if item.get("archived") == "true":
            continue
        wine_items.append(item)

    print(f"{len(wine_items)} items match wine/spirits categories.")

    for location in LOCATIONS:
        lines = [
            build_feed_line(item, location, shop_id_to_name)
            for item in wine_items
        ]
        with open(location["output_file"], "w", newline="\n", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"Done. Wrote {len(lines)} lines to {location['output_file']} "
              f"(price level: {location['price_level']}, "
              f"shop match: {location['shop_match']}, "
              f"format: pipe-delimited per Wine-Searcher spec)")

    print("Pushing updated feeds to GitHub...")
    git_commit_and_push()


if __name__ == "__main__":
    main()
