#!/usr/bin/env python3
"""
forever_ah.py - WoW: Forever auction house tracker for raw materials.

Pulls the auction house from Blizzard's API (updated ~hourly), keeps a price
history in a local SQLite file, and tells you:
  - fair price (market), cheapest listing, supply for each material
  - 24h / 7-day averages and the trend
  - BUY signals (cheap vs. 7-day average) and SELL HIGH signals
  - whether smelting / crafting beats selling raw (incl. Forever's 2x Bronze)

Standard library only. Python 3.9+.

SETUP (5 minutes)
  1. Go to https://develop.battle.net/access/clients, log in, "Create Client".
     Any name, any redirect URL (e.g. http://localhost). Copy the ID + secret.
  2. Set them as environment variables:
       Windows (PowerShell):  setx BLIZZARD_CLIENT_ID "xxx"; setx BLIZZARD_CLIENT_SECRET "yyy"
       Mac/Linux:             export BLIZZARD_CLIENT_ID=xxx BLIZZARD_CLIENT_SECRET=yyy
  3. Run:
       python forever_ah.py scan        # grab prices now + print report
       python forever_ah.py watch       # scan every hour, keep report.html updated
       python forever_ah.py report      # re-print report from saved history
       python forever_ah.py discover    # most-listed items you're NOT tracking yet
       python forever_ah.py houses      # list auction house IDs

  Defaults: EU, Normal ruleset, Alliance. Change with --region us,
  --ruleset 1 (PvP) / 3 (RP), --faction horde, --house 113 (one house only).

NOTE: The Forever API is undocumented and was found by community probing
(Blizzard forums, Nov 2026). Endpoints may change. Item IDs below are the
classic IDs; run `discover` once to sanity-check them.
"""

import argparse
import base64
import html
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

# --------------------------------------------------------------------------
# What to track. Edit freely: {item_id: "Name"}
# --------------------------------------------------------------------------
WATCHLIST = {
    "Ore": {
        2770: "Copper Ore", 2771: "Tin Ore", 2775: "Silver Ore",
        2772: "Iron Ore", 2776: "Gold Ore", 3858: "Mithril Ore",
    },
    "Bars": {
        2840: "Copper Bar", 3576: "Tin Bar", 2841: "Bronze Bar",
        2842: "Silver Bar", 3575: "Iron Bar", 3577: "Gold Bar",
        3859: "Steel Bar", 3860: "Mithril Bar",
    },
    "Stone": {
        2835: "Rough Stone", 2836: "Coarse Stone", 2838: "Heavy Stone",
        7912: "Solid Stone",
    },
    "Gems": {
        774: "Malachite", 818: "Tigerseye", 1210: "Shadowgem",
        1206: "Moss Agate", 1705: "Lesser Moonstone", 1529: "Jade",
    },
    "Engineering parts": {
        4357: "Rough Blasting Powder", 4359: "Handful of Copper Bolts",
        4361: "Copper Tube", 4364: "Coarse Blasting Powder",
        4371: "Bronze Tube", 4375: "Whirring Bronze Gizmo",
        4377: "Heavy Blasting Powder",
    },
    "Cloth & leather (you loot these)": {
        2589: "Linen Cloth", 2592: "Wool Cloth", 4306: "Silk Cloth",
        2318: "Light Leather", 2319: "Medium Leather",
    },
    "Elementals": {
        7067: "Elemental Earth",
    },
}

# Conversions you can do yourself. (name, {input_id: qty}, {output_id: qty})
# Engineering ones are the classic recipes - check your trade window.
RECIPES = [
    ("Smelt Copper",              {2770: 1},          {2840: 1}),
    ("Smelt Tin",                 {2771: 1},          {3576: 1}),
    ("Smelt Bronze (2 bars!)",    {2840: 1, 3576: 1}, {2841: 2}),
    ("Smelt Silver",              {2775: 1},          {2842: 1}),
    ("Smelt Iron",                {2772: 1},          {3575: 1}),
    ("Smelt Gold",                {2776: 1},          {3577: 1}),
    ("Smelt Mithril",             {3858: 1},          {3860: 1}),
    ("Rough Blasting Powder",     {2835: 1},          {4357: 1}),
    ("Handful of Copper Bolts",   {2840: 1},          {4359: 1}),
    ("Coarse Blasting Powder",    {2836: 1},          {4364: 1}),
    ("Heavy Blasting Powder",     {2838: 1},          {4377: 1}),
]

AH_CUT = 0.05          # AH takes 5% of the sale
MARKET_DEPTH = 0.15    # "market price" = avg of the cheapest 15% of supply
BUY_BELOW = 0.75       # BUY if cheapest listing < 75% of 7-day average
SELL_ABOVE = 1.25      # SELL HIGH if market > 125% of 7-day average
# Classic-style item auctions list the price of the whole stack. If prices for
# stacked items look 5-20x too low, set this to False.
BUYOUT_IS_STACK_TOTAL = True

RULESETS = {1: "PvP", 2: "Normal", 3: "Roleplay", 15: "Roleplay PvP"}
DB_FILE = "forever_ah.sqlite"
NAME_CACHE = "forever_item_names.json"

ALL_NAMES = {i: n for g in WATCHLIST.values() for i, n in g.items()}


# --------------------------------------------------------------------------
# Money formatting
# --------------------------------------------------------------------------
def money(copper):
    if copper is None:
        return "-"
    c = int(round(copper))
    neg = c < 0
    c = abs(c)
    g, s, cc = c // 10000, (c // 100) % 100, c % 100
    parts = []
    if g:
        parts.append(f"{g}g")
    if s or g:
        parts.append(f"{s}s")
    parts.append(f"{cc}c")
    return ("-" if neg else "") + " ".join(parts)


# --------------------------------------------------------------------------
# Blizzard API
# --------------------------------------------------------------------------
class BlizzardAPI:
    def __init__(self, region, client_id, client_secret):
        self.region = region
        self.base = f"https://{region}.api.blizzard.com"
        self.locale = "en_GB" if region == "eu" else "en_US"
        self.cid, self.secret = client_id, client_secret
        self._token, self._token_exp = None, 0

    def token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        auth = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
        req = urllib.request.Request(
            "https://oauth.battle.net/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        self._token = d["access_token"]
        self._token_exp = time.time() + d.get("expires_in", 3600)
        return self._token

    def get_url(self, url):
        """GET a full URL. Returns parsed JSON, or None on 404."""
        if "locale=" not in url:
            url += ("&" if "?" in url else "?") + f"locale={self.locale}"
        for attempt in range(4):
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.token()}",
                              "Accept-Encoding": "identity"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 3)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2 ** attempt * 3)
                    continue
                raise

    def get(self, path, namespace):
        q = urllib.parse.urlencode({"namespace": f"{namespace}-forever-{self.region}"})
        return self.get_url(f"{self.base}{path}?{q}")


def iter_listings(api, block):
    """A house field may be an inline list or a {'href': ...} link."""
    if isinstance(block, dict) and "href" in block:
        block = (api.get_url(block["href"]) or {}).get("auctions", [])
    return block or []


def list_houses(api, ruleset):
    # The trailing slash matters (community finding, Nov 2026).
    idx = api.get(f"/data/wow/ruleset/{ruleset}/auctions/", "dynamic")
    if idx is None:
        sys.exit("Auction index returned 404. The Forever API may have changed.")
    return idx.get("auctions", [])


def fetch_listings(api, ruleset, faction, house_ids=None):
    """Returns {item_id: [(unit_price_copper, quantity), ...]} and houses used."""
    houses = list_houses(api, ruleset)
    if house_ids:
        houses = [h for h in houses if h.get("id") in house_ids]
    else:
        houses = [h for h in houses if faction.lower() in h.get("name", "").lower()]
    if not houses:
        sys.exit(f"No auction houses matched (faction={faction}). Try: python forever_ah.py houses")

    listings = defaultdict(list)
    for h in houses:
        hid = h["id"]
        base = f"/data/wow/ruleset/{ruleset}/auctions/{hid}"
        house = api.get(base, "dynamic") or {}
        for a in iter_listings(api, house.get("auctions")):
            buyout = a.get("buyout")
            if not buyout:
                continue  # bid-only
            qty = a.get("quantity", 1) or 1
            unit = buyout / qty if BUYOUT_IS_STACK_TOTAL else buyout
            listings[a["item"]["id"]].append((unit, qty))
        comm = api.get(base + "/commodities", "dynamic") or {}
        for a in comm.get("auctions", []) or []:
            if a.get("unit_price"):
                listings[a["item"]["id"]].append((a["unit_price"], a.get("quantity", 1) or 1))
    return listings, houses


def summarize(entries):
    """min price, market price (cheapest 15% of supply), total qty, n listings."""
    entries = sorted(entries)
    total = sum(q for _, q in entries)
    target = max(1, total * MARKET_DEPTH)
    acc = spent = 0
    for price, q in entries:
        take = min(q, target - acc)
        spent += price * take
        acc += take
        if acc >= target:
            break
    return entries[0][0], spent / acc, total, len(entries)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB_FILE)
    con.execute("""CREATE TABLE IF NOT EXISTS snap (
        ts INTEGER, region TEXT, ruleset INTEGER, faction TEXT,
        item_id INTEGER, min_price REAL, market REAL, qty INTEGER, listings INTEGER)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix ON snap(region, ruleset, faction, item_id, ts)")
    return con


def store(con, args, listings):
    ts = int(time.time())
    rows = [(ts, args.region, args.ruleset, args.faction, iid, *summarize(e))
            for iid, e in listings.items() if e]
    con.executemany("INSERT INTO snap VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return ts, len(rows)


def history(con, args, item_id, since):
    return con.execute(
        """SELECT ts, min_price, market, qty FROM snap
           WHERE region=? AND ruleset=? AND faction=? AND item_id=? AND ts>=?
           ORDER BY ts""",
        (args.region, args.ruleset, args.faction, item_id, since)).fetchall()


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def analyze(con, args):
    now = time.time()
    rows = {}
    for group, items in WATCHLIST.items():
        for iid, name in items.items():
            h = history(con, args, iid, now - 7 * 86400)
            if not h:
                rows[iid] = dict(group=group, name=name, have=False)
                continue
            ts, mn, mk, qty = h[-1]
            day = [r[2] for r in h if r[0] >= now - 86400]
            week = [r[2] for r in h]
            avg24 = sum(day) / len(day) if day else None
            avg7 = sum(week) / len(week)
            trend = (mk - avg7) / avg7 if avg7 and len(week) > 1 else None
            signal = ""
            if len(week) >= 6:  # need a few hours of history before calling it
                if mn < BUY_BELOW * avg7:
                    signal = "BUY"
                elif mk > SELL_ABOVE * avg7:
                    signal = "SELL HIGH"
            stale = now - ts > 3 * 3600
            rows[iid] = dict(group=group, name=name, have=True, min=mn, market=mk,
                             qty=qty, avg24=avg24, avg7=avg7, trend=trend,
                             signal=signal, spark=[r[2] for r in h], stale=stale)
    return rows


def recipe_table(rows):
    out = []
    for name, ins, outs in RECIPES:
        ids = list(ins) + list(outs)
        if not all(rows.get(i, {}).get("have") for i in ids):
            continue
        cost = sum(q * rows[i]["market"] for i, q in ins.items())
        revenue = sum(q * rows[i]["market"] for i, q in outs.items()) * (1 - AH_CUT)
        profit = revenue - cost
        out.append(dict(name=name, cost=cost, revenue=revenue, profit=profit,
                        verdict="DO IT" if profit > 0 else "sell inputs raw"))
    return out


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def pct(x):
    return "-" if x is None else f"{x * 100:+.0f}%"


def print_report(rows, recipes, args):
    print(f"\nWoW Forever AH - {args.region.upper()} / {RULESETS.get(args.ruleset, args.ruleset)}"
          f" / {args.faction.title()} - {datetime.now():%Y-%m-%d %H:%M}\n")
    hdr = f"{'Item':<26}{'Market':>13}{'Cheapest':>13}{'7d avg':>13}{'Trend':>7}{'Supply':>8}  Signal"
    for group in WATCHLIST:
        items = [r for r in rows.values() if r["group"] == group]
        print(f"== {group} ==")
        print(hdr)
        for r in items:
            if not r["have"]:
                print(f"{r['name']:<26}{'(none listed)':>13}")
                continue
            print(f"{r['name']:<26}{money(r['market']):>13}{money(r['min']):>13}"
                  f"{money(r['avg7']):>13}{pct(r['trend']):>7}{r['qty']:>8}  {r['signal']}")
        print()
    if recipes:
        print("== Smelt / craft or sell raw? (after 5% AH cut) ==")
        for r in recipes:
            print(f"{r['name']:<26} inputs {money(r['cost']):>11}  ->  output {money(r['revenue']):>11}"
                  f"   profit {money(r['profit']):>11}   {r['verdict']}")
        print()


def sparkline(values, w=120, h=28):
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pts = " ".join(f"{i * w / (len(values) - 1):.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}"
                   for i, v in enumerate(values))
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{pts}"/></svg>')


def write_html(rows, recipes, args, path="report.html"):
    e = html.escape
    sig_cls = {"BUY": "buy", "SELL HIGH": "sell"}
    parts = [f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="600"><title>Forever AH Prices</title>
<style>
:root{{--bg:#fbfaf7;--fg:#222;--mut:#777;--line:#e4e0d8;--buy:#1a7f37;--sell:#b35900;--acc:#7a5c1e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16161a;--fg:#e8e6e1;--mut:#999;--line:#2c2c33;--buy:#4ac26b;--sell:#f0a050;--acc:#d4b06a}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;margin:0;padding:16px;max-width:1100px;margin:auto}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:var(--mut);margin-bottom:16px}}
h2{{font-size:15px;color:var(--acc);margin:24px 0 6px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;min-width:640px}}
td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}
td:first-child,th:first-child{{text-align:left}} th{{color:var(--mut);font-weight:500;font-size:12px}}
.buy{{color:var(--buy);font-weight:600}} .sell{{color:var(--sell);font-weight:600}}
.mut{{color:var(--mut)}} svg{{color:var(--acc);vertical-align:middle}}
</style></head><body>
<h1>Forever AH Prices</h1>
<div class="sub">{e(args.region.upper())} · {e(str(RULESETS.get(args.ruleset, args.ruleset)))} · {e(args.faction.title())} · updated {datetime.now():%Y-%m-%d %H:%M}</div>"""]
    if recipes:
        parts.append("<h2>Smelt / craft or sell raw?</h2><div class='wrap'><table>"
                     "<tr><th>Conversion</th><th>Inputs worth</th><th>Output after 5% cut</th><th>Profit</th><th>Verdict</th></tr>")
        for r in recipes:
            cls = "buy" if r["profit"] > 0 else "mut"
            parts.append(f"<tr><td>{e(r['name'])}</td><td>{money(r['cost'])}</td><td>{money(r['revenue'])}</td>"
                         f"<td class='{cls}'>{money(r['profit'])}</td><td class='{cls}'>{e(r['verdict'])}</td></tr>")
        parts.append("</table></div>")
    for group in WATCHLIST:
        parts.append(f"<h2>{e(group)}</h2><div class='wrap'><table><tr><th>Item</th><th>Market</th>"
                     "<th>Cheapest</th><th>24h avg</th><th>7d avg</th><th>Trend</th><th>Supply</th>"
                     "<th>7 days</th><th>Signal</th></tr>")
        for r in (x for x in rows.values() if x["group"] == group):
            if not r["have"]:
                parts.append(f"<tr><td>{e(r['name'])}</td><td colspan='8' class='mut'>none listed</td></tr>")
                continue
            parts.append(
                f"<tr><td>{e(r['name'])}{' <span class=mut>(stale)</span>' if r['stale'] else ''}</td>"
                f"<td>{money(r['market'])}</td><td>{money(r['min'])}</td><td>{money(r['avg24'])}</td>"
                f"<td>{money(r['avg7'])}</td><td>{pct(r['trend'])}</td><td>{r['qty']}</td>"
                f"<td>{sparkline(r['spark'])}</td><td class='{sig_cls.get(r['signal'], '')}'>{e(r['signal'])}</td></tr>")
        parts.append("</table></div>")
    parts.append("<p class='mut'>Market = average of the cheapest 15% of supply. BUY = cheapest listing under 75% "
                 "of the 7-day average. SELL HIGH = market over 125% of it. Signals start after ~6 scans.</p></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return path


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def make_api(args):
    cid = os.environ.get("BLIZZARD_CLIENT_ID")
    sec = os.environ.get("BLIZZARD_CLIENT_SECRET")
    if not cid or not sec:
        sys.exit("Set BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET first (see top of this file).")
    return BlizzardAPI(args.region, cid, sec)


def cmd_scan(args, api=None, quiet=False):
    api = api or make_api(args)
    listings, houses = fetch_listings(api, args.ruleset, args.faction, args.house)
    con = db()
    _, n = store(con, args, listings)
    if not quiet:
        print(f"Scanned {len(houses)} house(s): {', '.join(str(h['id']) for h in houses)} - {n} items stored.")
    cmd_report(args, con)


def cmd_report(args, con=None):
    con = con or db()
    rows = analyze(con, args)
    recipes = recipe_table(rows)
    print_report(rows, recipes, args)
    path = write_html(rows, recipes, args)
    print(f"Report written to {os.path.abspath(path)}")


def cmd_watch(args):
    api = make_api(args)
    print(f"Scanning every {args.every} min. Ctrl+C to stop.")
    while True:
        try:
            cmd_scan(args, api)
        except Exception as ex:  # keep going through API hiccups
            print(f"[{datetime.now():%H:%M}] scan failed: {ex}")
        time.sleep(args.every * 60)


def cmd_houses(args):
    api = make_api(args)
    for h in list_houses(api, args.ruleset):
        print(f"{h.get('id'):>5}  {h.get('name')}")


def cmd_discover(args):
    """Most-listed items not on your watchlist, with names - good for spotting new mats."""
    con = db()
    last = con.execute("SELECT MAX(ts) FROM snap WHERE region=? AND ruleset=? AND faction=?",
                       (args.region, args.ruleset, args.faction)).fetchone()[0]
    if not last:
        sys.exit("No data yet. Run: python forever_ah.py scan")
    top = con.execute("""SELECT item_id, market, qty FROM snap WHERE ts=? AND region=? AND ruleset=?
                         AND faction=? ORDER BY qty DESC""",
                      (last, args.region, args.ruleset, args.faction)).fetchall()
    names = {}
    if os.path.exists(NAME_CACHE):
        with open(NAME_CACHE, encoding="utf-8") as f:
            names = {int(k): v for k, v in json.load(f).items()}
    api = make_api(args)
    shown = 0
    print(f"{'ID':>7}  {'Item':<34}{'Market':>13}{'Supply':>9}")
    for iid, mk, qty in top:
        if iid in ALL_NAMES:
            continue
        if iid not in names:
            d = api.get(f"/data/wow/item/{iid}", "static")
            names[iid] = (d or {}).get("name") or "(unknown in item data)"
        print(f"{iid:>7}  {names[iid][:33]:<34}{money(mk):>13}{qty:>9}")
        shown += 1
        if shown >= args.top:
            break
    with open(NAME_CACHE, "w", encoding="utf-8") as f:
        json.dump(names, f)
    print("\nAdd any of these to WATCHLIST at the top of the file to track them.")


def main():
    p = argparse.ArgumentParser(description="WoW: Forever AH raw-material tracker")
    p.add_argument("command", choices=["scan", "report", "watch", "houses", "discover"])
    p.add_argument("--region", default="eu", choices=["eu", "us", "kr", "tw"])
    p.add_argument("--ruleset", type=int, default=2, help="1 PvP, 2 Normal, 3 RP, 15 RP-PvP")
    p.add_argument("--faction", default="alliance", help="alliance / horde / blackwater (neutral)")
    p.add_argument("--house", type=int, action="append", help="only these house IDs (repeatable)")
    p.add_argument("--every", type=int, default=60, help="watch: minutes between scans")
    p.add_argument("--top", type=int, default=40, help="discover: how many items")
    args = p.parse_args()
    {"scan": cmd_scan, "report": cmd_report, "watch": cmd_watch,
     "houses": cmd_houses, "discover": cmd_discover}[args.command](args)


if __name__ == "__main__":
    main()
