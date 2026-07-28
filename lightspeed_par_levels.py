#!/usr/bin/env python3
"""
lightspeed_par_levels.py

Calculates suggested par (reorder) levels for your top 100 best-selling
items, per store, across all locations -- based on real sales history
pulled from Lightspeed Retail.

THIS IS A REPORT-ONLY TOOL. It does NOT write anything back into
Lightspeed. It produces a CSV you can review before manually updating
any Reorder Points / Desired Inventory Levels in Lightspeed yourself.

-------------------------------------------------------------------------
HOW THE PAR LEVEL IS CALCULATED
-------------------------------------------------------------------------
For each of the top 100 items (ranked by total units sold across all
stores, over the full history window), and for EACH store separately:

  1. avg_daily_qty = that store's units sold for this item over the
     last 90 days, divided by 90
  2. base_par = avg_daily_qty * REORDER_CYCLE_DAYS (default 7, since you
     reorder weekly) -- this covers demand until your next delivery
  3. Volatility check using the FULL history window (2023-present):
       - "Gappy": item has a stretch of GAP_THRESHOLD_DAYS or more with
         zero sales at this store, sandwiched between periods where it
         DID sell (suggests bursty/seasonal demand, not just discontinued)
       - "Spiky": month-to-month unit sales at this store have high
         variability (coefficient of variation above SPIKE_CV_THRESHOLD)
     If either is true, an extra safety buffer is added on top of the
     base par so you don't run out during a burst.
  4. final_par = base_par + safety_buffer, rounded up to a whole unit

-------------------------------------------------------------------------
IMPORTANT: RUNTIME / SCALE WARNING
-------------------------------------------------------------------------
This pulls raw SALE TRANSACTION history (not just the item catalog),
across 5 locations, for up to 3 years. This is a MUCH larger pull than
the item-catalog scripts we built before, and could take a long time
depending on your actual sales volume.

RUN IN TEST_MODE FIRST. Set TEST_MODE = True below to pull only the last
7 days and confirm everything works before committing to a multi-hour
full historical pull. Once test mode looks right, set TEST_MODE = False
and FULL_HISTORY_START to run the real thing (ideally overnight).

-------------------------------------------------------------------------
SETUP
-------------------------------------------------------------------------
Same 4 Lightspeed credentials you've used in the other scripts
(Client ID, Client Secret, Refresh Token, Account ID) -- fill in below.
"""

import os
import sys
import time
import csv
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# =========================================================================
# CONFIG
# =========================================================================
CLIENT_ID = os.environ.get("LS_CLIENT_ID", "7dfceeee0dcd1f17eabfacd5b1d28261bc595aab95fcb322760654390bcfc531")
CLIENT_SECRET = os.environ.get("LS_CLIENT_SECRET", "1345e6f21ee779bd3b5a122ff7705e89caa92b84e74a062fc66391e97e7749f7")
REFRESH_TOKEN = os.environ.get("LS_REFRESH_TOKEN", "1e821d89ba59084c8d4317bf81c59149d103723c")
ACCOUNT_ID = os.environ.get("LS_ACCOUNT_ID", "290668")

# --- TEST MODE: run this first! ---
# True  = only pulls the last TEST_MODE_DAYS days (fast, ~minutes) so you
#         can confirm the script works before committing to a huge pull.
# False = pulls the FULL history from FULL_HISTORY_START to today.
TEST_MODE = False
TEST_MODE_DAYS = 7

FULL_HISTORY_START = "2024-01-01T00:00:00"  # only used when TEST_MODE = False

# How many days of recent sales the actual par-level math is based on.
PAR_WINDOW_DAYS = 90

# How often you reorder -- par needs to cover demand until the next
# delivery arrives.
REORDER_CYCLE_DAYS = 7

# How many of your best-selling items (by total units, across all
# stores, over the full pulled history) to generate par levels for.
TOP_N_ITEMS = 100

# A store is "gappy" for an item if there's a stretch of at least this
# many days with zero sales, sandwiched between periods where it DID
# sell (only checked using the full history window, not test mode).
GAP_THRESHOLD_DAYS = 30

# Coefficient of variation (std dev / mean) of monthly unit sales above
# this threshold marks an item as "spiky" at that store.
SPIKE_CV_THRESHOLD = 0.75

# Extra buffer days added to the base par for items flagged gappy/spiky.
VOLATILITY_BUFFER_DAYS = 5
BASELINE_SAFETY_DAYS = 2  # small buffer applied to every item regardless

OUTPUT_CSV = "par_levels_report.csv"

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


def fetch_shops(session):
    data = _get_url(session, f"{API_BASE}/Account/{ACCOUNT_ID}/Shop.json",
                     params={"limit": 100})
    shops = data.get("Shop", [])
    if isinstance(shops, dict):
        shops = [shops]
    return {s.get("shopID"): s.get("name", "") for s in shops}


def fetch_item_names(session):
    """Pull item descriptions once so the report is human-readable
    (SKU -> name), without needing to re-fetch this per sale line."""
    names = {}
    params = {"limit": 100}
    url = f"{API_BASE}/Account/{ACCOUNT_ID}/Item.json"
    while url:
        data = _get_url(session, url, params=params)
        batch = data.get("Item", [])
        if isinstance(batch, dict):
            batch = [batch]
        if not batch:
            break
        for item in batch:
            names[item.get("itemID")] = (
                item.get("customSku") or item.get("systemSku") or "",
                item.get("description", ""),
            )
        print(f"  Loaded {len(names)} item names so far...")
        next_url = data.get("@attributes", {}).get("next", "")
        if not next_url:
            break
        url = next_url
        params = None
    return names


def fetch_sales(session, start_dt, end_dt):
    """
    Yields (shopID, itemID, unitQuantity, timeStamp) for every completed
    sale line between start_dt and end_dt (both timezone-aware datetimes).
    """
    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

    params = {
        "limit": 100,
        "load_relations": '["SaleLines"]',
        "timeStamp": f"><,{start_str},{end_str}",
        "completed": "true",
        "sort": "timeStamp",
    }
    url = f"{API_BASE}/Account/{ACCOUNT_ID}/Sale.json"
    count = 0
    while url:
        data = _get_url(session, url, params=params)
        sales = data.get("Sale", [])
        if isinstance(sales, dict):
            sales = [sales]
        if not sales:
            break

        for sale in sales:
            shop_id = sale.get("shopID")
            lines = sale.get("SaleLines", {}).get("SaleLine", [])
            if isinstance(lines, dict):
                lines = [lines]
            ts = sale.get("timeStamp", "")
            for line in lines:
                item_id = line.get("itemID")
                try:
                    qty = float(line.get("unitQuantity", 0))
                except (TypeError, ValueError):
                    qty = 0
                if item_id and qty:
                    yield shop_id, item_id, qty, ts

        count += len(sales)
        print(f"  Processed {count} sales so far...")

        next_url = data.get("@attributes", {}).get("next", "")
        if not next_url:
            break
        url = next_url
        params = None


def main():
    if CLIENT_ID.startswith("YOUR_"):
        print("ERROR: fill in your 4 Lightspeed credentials at the top "
              "of this script.", file=sys.stderr)
        sys.exit(1)

    print("Requesting access token...")
    access_token = get_access_token()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}"})

    print("Fetching shop/location list...")
    shop_id_to_name = fetch_shops(session)
    print(f"  Found {len(shop_id_to_name)} shop(s): "
          f"{', '.join(shop_id_to_name.values())}")

    print("Loading item names/SKUs (one-time)...")
    item_names = fetch_item_names(session)
    print(f"  Loaded {len(item_names)} item names.")

    now = datetime.now(timezone.utc)
    if TEST_MODE:
        history_start = now - timedelta(days=TEST_MODE_DAYS)
        print(f"\n*** TEST MODE: pulling only the last {TEST_MODE_DAYS} "
              f"days. Set TEST_MODE = False for the real run. ***\n")
    else:
        history_start = datetime.fromisoformat(FULL_HISTORY_START).replace(
            tzinfo=timezone.utc)
        print(f"\nFULL RUN: pulling sales from {FULL_HISTORY_START} to "
              f"now. This may take a long time.\n")

    par_window_start = now - timedelta(days=PAR_WINDOW_DAYS)

    # sales_by_item_shop_month[item_id][shop_id][YYYY-MM] = qty
    sales_by_item_shop_month = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    # sale_dates[item_id][shop_id] = sorted list of sale dates (for gap detection)
    sale_dates = defaultdict(lambda: defaultdict(list))
    # total_qty_90d[item_id][shop_id]
    total_qty_90d = defaultdict(lambda: defaultdict(float))
    # total_qty_full[item_id] -- across all shops, for ranking top N
    total_qty_full = defaultdict(float)

    print("Fetching sales history from Lightspeed (this is the slow part)...")
    for shop_id, item_id, qty, ts in fetch_sales(session, history_start, now):
        try:
            sale_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        month_key = sale_dt.strftime("%Y-%m")
        sales_by_item_shop_month[item_id][shop_id][month_key] += qty
        sale_dates[item_id][shop_id].append(sale_dt.date())
        total_qty_full[item_id] += qty

        if sale_dt >= par_window_start:
            total_qty_90d[item_id][shop_id] += qty

    print(f"\nDone fetching. {len(total_qty_full)} distinct items had sales "
          f"in the pulled window.")

    top_items = sorted(total_qty_full.items(), key=lambda x: x[1], reverse=True)[:TOP_N_ITEMS]
    print(f"Top {len(top_items)} items by total units sold identified.")

    rows = []
    for item_id, total_units in top_items:
        sku, name = item_names.get(item_id, ("", f"Unknown item {item_id}"))

        for shop_id, shop_name in shop_id_to_name.items():
            qty_90d = total_qty_90d[item_id].get(shop_id, 0)
            avg_daily = qty_90d / PAR_WINDOW_DAYS
            base_par = avg_daily * REORDER_CYCLE_DAYS

            # --- volatility checks (only meaningful with full history) ---
            gappy = False
            spiky = False
            if not TEST_MODE:
                dates = sorted(sale_dates[item_id].get(shop_id, []))
                if len(dates) >= 2:
                    for i in range(1, len(dates)):
                        gap = (dates[i] - dates[i - 1]).days
                        if gap >= GAP_THRESHOLD_DAYS:
                            gappy = True
                            break

                months = sales_by_item_shop_month[item_id].get(shop_id, {})
                monthly_values = list(months.values())
                if len(monthly_values) >= 3:
                    mean_val = statistics.mean(monthly_values)
                    if mean_val > 0:
                        stdev_val = statistics.pstdev(monthly_values)
                        cv = stdev_val / mean_val
                        if cv >= SPIKE_CV_THRESHOLD:
                            spiky = True

            safety_days = BASELINE_SAFETY_DAYS
            if gappy or spiky:
                safety_days += VOLATILITY_BUFFER_DAYS

            safety_buffer = avg_daily * safety_days
            final_par = base_par + safety_buffer

            rows.append({
                "SKU": sku,
                "Item Name": name,
                "Store": shop_name,
                "Total Units Sold (full window)": round(total_units, 1),
                "Units Sold (last 90 days, this store)": round(qty_90d, 1),
                "Avg Daily Units (this store)": round(avg_daily, 3),
                "Gappy Sales Pattern": "Yes" if gappy else "",
                "Spiky Sales Pattern": "Yes" if spiky else "",
                "Suggested Par Level": int(round(final_par + 0.5)),  # round up
            })

    fieldnames = ["SKU", "Item Name", "Store", "Total Units Sold (full window)",
                  "Units Sold (last 90 days, this store)",
                  "Avg Daily Units (this store)", "Gappy Sales Pattern",
                  "Spiky Sales Pattern", "Suggested Par Level"]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone. Wrote {len(rows)} rows ({len(top_items)} items x "
          f"{len(shop_id_to_name)} stores) to {OUTPUT_CSV}")
    if TEST_MODE:
        print("\nThis was a TEST MODE run (only last "
              f"{TEST_MODE_DAYS} days). Review the output, then set "
              "TEST_MODE = False for the full historical run.")


if __name__ == "__main__":
    main()
