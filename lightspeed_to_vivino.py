#!/usr/bin/env python3
"""
lightspeed_to_vivino.py

Pulls WINE items specifically (using Lightspeed's real Category field --
not a text-based guess) from your Lightspeed Retail account and builds
the CSV format Vivino's merchant application asked for:

Product Name, Vintage, Price, Bottle Size, Product Link, Product ID,
Bottle Quantity

This is a one-time export for the Vivino merchant application, reusing
the same Lightspeed connection/credentials as lightspeed_to_winesearcher.py.

SETUP: paste in the same 4 credentials you've already been using
(Client ID, Client Secret, Refresh Token, Account ID).
"""

import os
import re
import csv
import sys
import time
import requests

# =========================================================================
CLIENT_ID = os.environ.get("LS_CLIENT_ID", "7dfceeee0dcd1f17eabfacd5b1d28261bc595aab95fcb322760654390bcfc531")
CLIENT_SECRET = os.environ.get("LS_CLIENT_SECRET", "1345e6f21ee779bd3b5a122ff7705e89caa92b84e74a062fc66391e97e7749f7")
REFRESH_TOKEN = os.environ.get("LS_REFRESH_TOKEN", "1e821d89ba59084c8d4317bf81c59149d103723c")
ACCOUNT_ID = os.environ.get("LS_ACCOUNT_ID", "290668")

# Only items whose Lightspeed Category name matches one of these (case-
# insensitive) will be included -- this is your store's full, exact list
# of wine categories as configured in Lightspeed.
WINE_CATEGORY_NAMES = {
    "cabernet franc", "cabernet sauvignon", "champagne", "chardonnay",
    "chenin blanc", "france", "greek", "ice wine", "italian red",
    "italian white", "italy", "kosher", "malbec", "merlot", "moscato",
    "pinot blanc", "pinot grigio", "pinot noir", "port", "portugal",
    "prosecco", "red blend", "riesling", "rose", "spain",
    "sauvignon blanc", "sake", "sancerre", "sangria", "sherry", "shiraz",
    "south africa", "syrah", "white wine", "white zinfandel", "zinfandel",
}

# Which pricing level to use for the price column (matches your existing
# Wine-Searcher setup -- Bayview's lower price for both stores).
PRICE_LEVEL_NAME = "Bayview"

STORE_URL_TEMPLATE = "https://shop.oceanwineandspirits.com/product/{sku}"
MERCHANT_NAME = "Ocean Wine & Spirits"
OUTPUT_CSV = f"{MERCHANT_NAME} - Vivino Assortment.csv"

API_BASE = "https://api.lightspeedapp.com/API/V3"
TOKEN_URL = "https://cloud.lightspeedapp.com/oauth/access_token.php"
# =========================================================================


def get_access_token():
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


def _get_url(session, url, params=None):
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
            print(f"  ERROR {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()
        return resp.json()


def fetch_all_items(session):
    items = []
    params = {
        "limit": 100,
        "load_relations": '["Category","CustomFieldValues"]',
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
    custom_fields = item.get("CustomFieldValues", {}).get("CustomFieldValue", [])
    if isinstance(custom_fields, dict):
        custom_fields = [custom_fields]
    for cf in custom_fields:
        val = str(cf.get("value", ""))
        match = re.search(r"\b(19|20)\d{2}\b", val)
        if match:
            return match.group(0)
    text = f"{item.get('description', '')} {item.get('longDescription', '')}"
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return match.group(0) if match else "nv"


def extract_price(item, price_level_name):
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


def extract_bottle_size(item):
    text = item.get("description", "")
    match = re.search(r"\b(187|200|375|500|750|1000)\s*ML\b", text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}ml"
    match = re.search(r"\b(1|1\.5|3|6)\s*L\b", text, re.IGNORECASE)
    if match:
        return f"{match.group(1)}L"
    return "750ml"


def extract_category_name(item):
    cat = item.get("Category", {})
    return cat.get("name", "") if isinstance(cat, dict) else ""


def main():
    if CLIENT_ID.startswith("YOUR_"):
        print("ERROR: fill in your 4 Lightspeed credentials at the top of "
              "this script.", file=sys.stderr)
        sys.exit(1)

    print("Requesting access token...")
    access_token = get_access_token()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}"})

    print("Fetching items from Lightspeed Retail...")
    items = fetch_all_items(session)
    print(f"Fetched {len(items)} total items.")

    rows = []
    for item in items:
        if item.get("archived") == "true":
            continue
        category_name = extract_category_name(item)
        if category_name.strip().lower() not in WINE_CATEGORY_NAMES:
            continue

        sku = item.get("customSku") or item.get("systemSku") or item.get("itemID", "")
        name = item.get("description", "")

        rows.append({
            "Product Name": name.strip(),
            "Vintage": extract_vintage(item),
            "Price": extract_price(item, PRICE_LEVEL_NAME),
            "Bottle Size": extract_bottle_size(item),
            "Product Link": STORE_URL_TEMPLATE.format(sku=sku),
            "Product ID": sku,
            "Bottle Quantity": "",
        })

    print(f"{len(rows)} items matched Wine category "
          f"({', '.join(WINE_CATEGORY_NAMES)}).")

    fieldnames = ["Product Name", "Vintage", "Price", "Bottle Size",
                  "Product Link", "Product ID", "Bottle Quantity"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Done. Wrote {len(rows)} rows to '{OUTPUT_CSV}'")


if __name__ == "__main__":
    main()
