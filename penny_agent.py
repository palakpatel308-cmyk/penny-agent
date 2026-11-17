#!/usr/bin/env python3
"""
Penny Agent: a free, self-hosted penny/clearance deal watcher.

What it does every run:
  1. Reads the free community Home Depot Penny List (pennycentral.com).
  2. Reads Slickdeals RSS searches for Home Depot and Lowe's clearance.
  3. Flags anything new since the last run, ranks your home state(s) first.
  4. Sends a push notification to your phone via ntfy.sh (free, no account).

No subscriptions. No API keys. Runs on GitHub Actions for free.

Usage:
  python penny_agent.py              # normal run
  python penny_agent.py --dry-run    # print alerts, don't send or save
  python penny_agent.py --debug      # also save raw HTML to debug/ for parser fixes
"""

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen.json"
DEBUG_DIR = ROOT / "debug"

PENNY_LIST_URL = "https://www.pennycentral.com/penny-list"
SLICKDEALS_RSS = (
    "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals"
    "&searchin=first&rss=1&q={q}"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 PennyAgent/1.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)

# Home Depot SKUs: 6 digits (215-583) or 10 digits (1012-388-248)
SKU_FMT_RE = re.compile(r"(?<![\d-])(\d{4}-\d{3}-\d{3}|\d{3}-\d{3})(?![\d-])")
STATE_COUNT_RE = re.compile(r"([A-Z]{2})(\d{1,3})")
MORE_RE = re.compile(r"\+\s*(\d+)\s*more", re.I)
PRICE_RE = re.compile(r"\$\s?(\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)")


# ----------------------------------------------------------------- config/io

def load_config():
    cfg = {
        "home_states": ["FL"],
        "nearby_states": ["GA", "AL"],
        "min_original_price": 0,
        "slickdeals_queries": [
            "lowes clearance",
            "home depot clearance",
            "home depot penny",
            "lowes penny",
        ],
        "slickdeals_max_price": 5.00,
        "alert_all_states": True,
        "max_alerts_per_run": 15,
    }
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    # Env vars win (GitHub Actions secrets / variables)
    if os.getenv("HOME_STATES"):
        cfg["home_states"] = [s.strip().upper() for s in os.getenv("HOME_STATES").split(",")]
    cfg["ntfy_topic"] = os.getenv("NTFY_TOPIC", cfg.get("ntfy_topic", ""))
    cfg["ntfy_server"] = os.getenv("NTFY_SERVER", cfg.get("ntfy_server", "https://ntfy.sh"))
    return cfg


def load_seen():
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"penny": {}, "slickdeals": {}}


def save_seen(seen):
    # Keep the file small: drop entries older than 60 days
    cutoff = time.time() - 60 * 86400
    for bucket in seen.values():
        for k in [k for k, v in bucket.items() if v.get("t", 0) < cutoff]:
            del bucket[k]
    SEEN_PATH.write_text(json.dumps(seen, indent=1, sort_keys=True))


def fetch(url, debug_name=None, debug=False):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    if debug and debug_name:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / debug_name).write_text(r.text)
    return r.text


# ------------------------------------------------------- penny list parsing

def norm_sku(s):
    return re.sub(r"\D", "", str(s))


def _walk_json(obj, out):
    """Collect any dict that looks like a penny item (has a sku-ish key)."""
    if isinstance(obj, dict):
        keys = {k.lower(): k for k in obj.keys()}
        sku_key = next((keys[k] for k in keys if k in ("sku", "itemsku", "item_sku", "skunumber")), None)
        if sku_key and len(norm_sku(obj[sku_key])) in (6, 10):
            out.append(obj)
        for v in obj.values():
            _walk_json(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json(v, out)


def _item_from_json(d):
    low = {k.lower(): v for k, v in d.items()}

    def first(*names):
        for n in names:
            if n in low and low[n] not in (None, ""):
                return low[n]
        return None

    sku = norm_sku(first("sku", "itemsku", "item_sku", "skunumber"))
    name = first("name", "itemname", "item_name", "title", "productname", "product_name") or ""
    brand = first("brand", "brandname") or ""
    orig = first("originalprice", "original_price", "retailprice", "price", "wasprice")
    states = {}
    raw_states = first("states", "statecounts", "state_counts", "locations")
    if isinstance(raw_states, dict):
        states = {k.upper(): int(v) if str(v).isdigit() else 1 for k, v in raw_states.items() if k.upper() in US_STATES}
    elif isinstance(raw_states, list):
        for s in raw_states:
            if isinstance(s, str) and s.upper() in US_STATES:
                states[s.upper()] = states.get(s.upper(), 0) + 1
            elif isinstance(s, dict):
                code = str(s.get("state") or s.get("code") or "").upper()
                if code in US_STATES:
                    states[code] = states.get(code, 0) + int(s.get("count", 1) or 1)
    single = first("state")
    if isinstance(single, str) and single.upper() in US_STATES:
        states[single.upper()] = states.get(single.upper(), 0) + 1
    try:
        orig = float(str(orig).replace("$", "").replace(",", "")) if orig is not None else None
    except ValueError:
        orig = None
    return {
        "sku": sku,
        "name": f"{brand} {name}".strip() if brand and brand not in name else str(name),
        "orig_price": orig,
        "states": states,
        "more_states": 0,
        "last_seen": str(first("lastseen", "last_seen", "date", "reportedat", "updatedat") or ""),
    }


def parse_penny_json(html):
    soup = BeautifulSoup(html, "html.parser")
    blobs = []
    for tag in soup.find_all("script"):
        t = (tag.get("type") or "").lower()
        txt = tag.string or tag.get_text() or ""
        if tag.get("id") == "__NEXT_DATA__" or t in ("application/json", "application/ld+json"):
            try:
                blobs.append(json.loads(txt))
            except json.JSONDecodeError:
                continue
    found = []
    for b in blobs:
        _walk_json(b, found)
    items = {}
    for d in found:
        it = _item_from_json(d)
        if it["sku"] and it["sku"] not in items:
            items[it["sku"]] = it
    return list(items.values())


def parse_penny_html(html):
    """Fallback: find SKU text on the page, climb to its card, read the card."""
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["script", "style", "noscript"]):
        bad.decompose()
    items = {}
    for node in soup.find_all(string=SKU_FMT_RE):
        m = SKU_FMT_RE.search(node)
        sku = norm_sku(m.group(1))
        if sku in items:
            continue
        card = node.parent
        # climb until the container holds a price or state info, max 8 levels
        for _ in range(8):
            text = card.get_text(" ", strip=True)
            if ("$" in text and (STATE_COUNT_RE.search(text) or "more" in text.lower())) or card.parent is None:
                break
            card = card.parent
        text = card.get_text(" ", strip=True)
        heading = card.find(["h1", "h2", "h3", "h4", "h5", "a", "strong"])
        name = heading.get_text(" ", strip=True) if heading else text[:80]
        prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        orig = max(prices) if prices else None
        # States appear like "CA8 NJ6" or glued "CA8NJ6TX6"
        states = {}
        for code, cnt in STATE_COUNT_RE.findall(text.replace(" ", "")):
            if code in US_STATES:
                states[code] = states.get(code, 0) + int(cnt)
        more = MORE_RE.search(text)
        last = re.search(r"(today|yesterday|\d+\s+(?:minute|hour|day|week)s?\s+ago)", text, re.I)
        items[sku] = {
            "sku": sku,
            "name": name,
            "orig_price": orig if orig and orig > 0.01 else None,
            "states": states,
            "more_states": int(more.group(1)) if more else 0,
            "last_seen": last.group(1) if last else "",
        }
    return list(items.values())


def get_penny_items(debug=False):
    html = fetch(PENNY_LIST_URL, "penny_list.html", debug)
    items = parse_penny_json(html)
    source = "json"
    if not items:
        items = parse_penny_html(html)
        source = "html"
    print(f"[penny-list] parsed {len(items)} items via {source}")
    if not items:
        print("[penny-list] WARNING: 0 items parsed. The site layout may have changed. "
              "Run with --debug and check debug/penny_list.html", file=sys.stderr)
    return items


# ---------------------------------------------------------------- slickdeals

def get_slickdeals(cfg, debug=False):
    deals = []
    for q in cfg["slickdeals_queries"]:
        url = SLICKDEALS_RSS.format(q=quote_plus(q))
        try:
            xml = fetch(url, f"sd_{q.replace(' ', '_')}.xml", debug)
            root = ET.fromstring(xml)
        except Exception as e:  # noqa: BLE001
            print(f"[slickdeals] '{q}' failed: {e}", file=sys.stderr)
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            guid = (it.findtext("guid") or link).strip()
            store = "Lowe's" if re.search(r"lowe'?s", title, re.I) else (
                "Home Depot" if re.search(r"home\s*depot", title, re.I) else None)
            if not store:
                continue
            prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(title)]
            price = min(prices) if prices else None
            penny_word = re.search(r"penny|\$0?\.01\b|1\s?cent", title, re.I)
            if penny_word or (price is not None and price <= cfg["slickdeals_max_price"]):
                deals.append({"id": guid, "title": title, "link": link, "store": store, "price": price})
        time.sleep(2)  # be polite
    uniq = {d["id"]: d for d in deals}
    print(f"[slickdeals] {len(uniq)} matching deals")
    return list(uniq.values())


# ------------------------------------------------------------------- ranking

def score_penny(it, cfg):
    home = set(cfg["home_states"])
    near = set(cfg.get("nearby_states", []))
    s = 0
    s += 100 * sum(1 for st in it["states"] if st in home)
    s += 20 * sum(1 for st in it["states"] if st in near)
    s += len(it["states"]) + it["more_states"]  # spread = more likely at your store too
    if it["orig_price"]:
        s += min(it["orig_price"], 500) / 10
    return s


def hd_link(sku):
    return f"https://www.homedepot.com/s/{sku}"


# ------------------------------------------------------------------- alerts

def send_ntfy(cfg, title, body, click=None, priority="default", tags="moneybag"):
    if not cfg.get("ntfy_topic"):
        print("[ntfy] no NTFY_TOPIC set, printing only")
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    r = requests.post(f"{cfg['ntfy_server'].rstrip('/')}/{cfg['ntfy_topic']}",
                      data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()


def fmt_penny(it, cfg):
    home = [s for s in it["states"] if s in cfg["home_states"]]
    where = ", ".join(f"{k}{v}" for k, v in sorted(it["states"].items(), key=lambda x: -x[1])[:6])
    if it["more_states"]:
        where += f" +{it['more_states']} more"
    flag = "IN YOUR STATE " if home else ""
    orig = f" (was ${it['orig_price']:.2f})" if it["orig_price"] else ""
    title = f"{flag}HD penny: {it['name'][:60]}"
    body = (f"SKU {it['sku']}{orig}\n"
            f"Reported: {where or 'n/a'}\n"
            f"Last seen: {it['last_seen'] or 'n/a'}\n"
            f"Tap to check your store's stock.")
    return title, body, ("high" if home else "default")


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    seen = load_seen()
    now = time.time()
    first_run = not seen["penny"] and not seen["slickdeals"]
    alerts = []

    # 1) Home Depot penny list
    try:
        items = get_penny_items(args.debug)
    except Exception as e:  # noqa: BLE001
        print(f"[penny-list] fetch failed: {e}", file=sys.stderr)
        items = []
    for it in items:
        prev = seen["penny"].get(it["sku"])
        home_now = any(s in cfg["home_states"] for s in it["states"])
        is_new = prev is None
        newly_home = prev is not None and home_now and not prev.get("home")
        if it["orig_price"] is not None and it["orig_price"] < cfg["min_original_price"]:
            continue
        if (is_new or newly_home) and (cfg["alert_all_states"] or home_now):
            alerts.append(("penny", score_penny(it, cfg), it))
        seen["penny"][it["sku"]] = {"t": now, "home": home_now, "name": it["name"][:80]}

    # 2) Slickdeals (Lowe's + Home Depot)
    for d in get_slickdeals(cfg, args.debug):
        if d["id"] not in seen["slickdeals"]:
            score = 50 if d["price"] is not None and d["price"] <= 0.05 else 10
            alerts.append(("sd", score, d))
        seen["slickdeals"][d["id"]] = {"t": now}

    alerts.sort(key=lambda a: -a[1])

    if first_run and not args.dry_run:
        # Don't blast 100 notifications on day one: send a summary instead.
        top = [a for a in alerts if a[0] == "penny"][:5]
        lines = [f"- {a[2]['name'][:50]} (SKU {a[2]['sku']})" for a in top]
        send_ntfy(cfg, "Penny Agent is live",
                  f"Tracking {len(items)} penny items. From now on you'll only get NEW finds.\n"
                  f"Top right now:\n" + "\n".join(lines),
                  click=PENNY_LIST_URL, tags="white_check_mark")
        print("[agent] first run: baseline saved, summary sent")
    else:
        for kind, _, a in alerts[: cfg["max_alerts_per_run"]]:
            if kind == "penny":
                title, body, prio = fmt_penny(a, cfg)
                click = hd_link(a["sku"])
            else:
                title = f"{a['store']} deal: {a['price'] and '$%.2f' % a['price'] or 'see post'}"
                body, click, prio = a["title"][:300], a["link"], "default"
            print(f"\n== {title}\n{body}\n{click}")
            if not args.dry_run:
                send_ntfy(cfg, title, body, click=click, priority=prio)
                time.sleep(1)
        if len(alerts) > cfg["max_alerts_per_run"]:
            extra = len(alerts) - cfg["max_alerts_per_run"]
            print(f"[agent] {extra} more alerts skipped (max_alerts_per_run)")
        print(f"[agent] {len(alerts)} new alerts")

    if not args.dry_run:
        save_seen(seen)


if __name__ == "__main__":
    main()
