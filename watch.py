#!/usr/bin/env python3
"""Watch shop listings and alert when a product becomes worth buying.

Each run checks every product ("target") in targets.json once and exits. A
scheduler (Windows Task Scheduler, cron, GitHub Actions, ...) is responsible
for running it repeatedly.

Supported sites:
  amazon   With `asin`: a product page, alerting when it's buyable and (if
           `max_price` is set) at or under that price.
           With `listing` + `match`: wait for a variant to appear and be buyable.
           Reads the listing's variant table (`dimensionValuesDisplayData`),
           finds the variant whose labels contain every `match` term, then
           checks that variant's page for an Add to Cart button.
  flipkart A product page, alerting when it's in stock and (if `max_price` is
           set) at or under that price. Reads the page's schema.org Product data, the
           structured block Flipkart publishes for search engines, rather
           than its visual layout, which changes far more often.

Each target remembers its last status in state.json and alerts only when the
status changes, so you get one alert per event rather than one every run.

If a site serves a CAPTCHA, a bot check, or a page without the expected
markers, the run is recorded as "blocked" instead of being misread as "not
available". Sustained blocks, and pages that look redesigned, send a warning.

Standard library only, so it runs anywhere Python 3.9+ is installed.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape as xml_escape

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "targets.json"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "watch.log"
NTFY_TOPIC_FILE = HERE / "ntfy_topic.txt"  # git-ignored; see notify_ntfy

BLOCKED_ALERT_AFTER = 6  # consecutive blocked runs before warning
LAYOUT_ALERT_AFTER = 2   # consecutive "page redesigned" runs before warning

# The single-product version kept its state at the top level of state.json;
# it now lives under this target's key (see migrate_state).
LEGACY_TARGET_KEY = "amazon-s25u-whitesilver-512"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

log = logging.getLogger("watch")


class Blocked(Exception):
    """The site refused the request (CAPTCHA, bot check, error status)."""


class LayoutChanged(Blocked):
    """The page loaded but the markers we parse are missing: the site redesigned it."""


@dataclass
class Result:
    status: str          # machine-readable, compared between runs
    good: bool           # True when this is the state worth alerting about
    status_text: str     # human wording, e.g. "in stock", "not listed yet"
    title: str | None = None
    price: int | None = None  # whole rupees
    url: str | None = None
    detail: str | None = None


@dataclass
class Target:
    key: str    # stable id; names this target's entry in state.json
    site: str   # display name, e.g. "Amazon"
    label: str  # what is being watched, e.g. "S25 Ultra Whitesilver 512GB"
    check: Callable[[], Result]


def format_inr(rupees: int) -> str:
    """Indian digit grouping: 109999 -> '₹1,09,999'."""
    digits = str(rupees)
    if len(digits) <= 3:
        return f"₹{digits}"
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    groups.insert(0, head)
    return "₹" + ",".join(groups + [tail])


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch(url: str, marker: str, attempts: int = 3) -> str:
    """Fetch a page, retrying when the site blocks the request.

    `marker` is a string only the real page contains; its absence means we got
    a block page or the site changed. Blocks are intermittent (on GitHub's
    runners roughly every other request), so a short randomised wait and
    another try often gets through.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fetch_once(url, marker)
        except LayoutChanged:
            raise  # retrying won't fix a redesign
        except Blocked as e:
            if attempt == attempts:
                raise
            wait = random.uniform(20, 40)
            log.info("attempt %d/%d blocked (%s); retrying in %.0fs",
                     attempt, attempts, e, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def fetch_once(url: str, marker: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # Bots typically get 403/429/503 plus a challenge page.
        raise Blocked(f"HTTP {e.code} for {url}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise Blocked(f"network error for {url}: {e}") from e

    if marker in body:
        return body
    lowered = body.lower()
    if "captcha" in lowered:
        raise Blocked(f"CAPTCHA page for {url}")
    if "bm-verify" in lowered or "/_sec/verify" in lowered:
        raise Blocked(f"bot-check challenge page for {url}")
    raise LayoutChanged(f"expected marker {marker!r} missing on {url}")


def normalise(text: str) -> str:
    """Lower-case and drop spaces so '512 GB' and '512GB' compare equal."""
    return re.sub(r"\s+", "", text).lower()


# --------------------------------------------------------------------------- #
# Amazon
# --------------------------------------------------------------------------- #

AMAZON_MARKER = 'id="productTitle"'


def parse_variants(page: str) -> dict[str, list[str]]:
    m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{[^{}]*\})', page)
    if not m:
        raise LayoutChanged("variant table (dimensionValuesDisplayData) not found")
    return json.loads(m.group(1))


def find_variant(variants: dict[str, list[str]], terms: list[str]) -> str | None:
    wanted = [normalise(t) for t in terms]
    for asin, labels in sorted(variants.items()):  # sorted: don't rely on page order
        haystack = normalise(" ".join(labels))
        if all(w in haystack for w in wanted):
            return asin
    return None


def text_of(page: str, element_id: str, limit: int = 2000) -> str:
    # Skip the rest of the opening tag, then strip tags and inline JSON blobs.
    m = re.search(rf'id="{element_id}"[^>]*>(.{{0,{limit}}})', page, re.S)
    if not m:
        return ""
    stripped = re.sub(r"<[^>]+>|\{.*", " ", m.group(1), flags=re.S)
    return html.unescape(re.sub(r"\s+", " ", stripped)).strip()


def parse_amazon_product(page: str) -> tuple[str, int | None, bool, str]:
    title_m = re.search(r'id="productTitle"[^>]*>(.*?)</span>', page, re.S)
    title = html.unescape(re.sub(r"\s+", " ", title_m.group(1))).strip() if title_m else "?"

    price_m = re.search(r'a-price-whole">([\d,]+)', page)
    price = int(price_m.group(1).replace(",", "")) if price_m else None

    availability = text_of(page, "availability", 400)[:120]
    unavailable = "currently unavailable" in availability.lower()
    buyable = 'id="add-to-cart-button"' in page and not unavailable
    return title, price, buyable, availability


def stock_and_price_result(title: str, price: int | None, in_stock: bool,
                           max_price: int | None, url: str) -> Result:
    """Shared rule for 'buy when in stock (and at or under max_price, if given)'."""
    if not in_stock:
        return Result("out_of_stock", good=False, status_text="sold out",
                      title=title, price=price, url=url)
    if max_price is None:
        return Result("in_stock", good=True, status_text="in stock",
                      title=title, price=price, url=url)
    target = format_inr(max_price)
    if price is not None and price <= max_price:
        return Result("at_target", good=True,
                      status_text=f"at {format_inr(price)} (target {target})",
                      title=title, price=price, url=url)
    # No visible price counts as "not at target": never alert on a guess.
    return Result("above_target", good=False, status_text=f"above your {target} target",
                  title=title, price=price, url=url)


def check_amazon_product(host: str, asin: str, max_price: int | None) -> Result:
    """A specific product page: alert when buyable (and at or under max_price)."""
    url = f"https://{host}/dp/{asin}?th=1&psc=1"
    title, price, buyable, availability = parse_amazon_product(fetch(url, AMAZON_MARKER))
    result = stock_and_price_result(title, price, buyable, max_price, url)
    result.detail = availability or None
    return result


def check_amazon(host: str, listing: str, match: list[str]) -> Result:
    """A variant that may not be listed yet: alert when it appears and is buyable."""
    listing_page = fetch(f"https://{host}/dp/{listing}?th=1", AMAZON_MARKER)
    variants = parse_variants(listing_page)
    asin = find_variant(variants, match)
    if asin is None:
        return Result("not_listed", good=False, status_text="not listed yet",
                      detail=f"{len(variants)} variants listed, none match")

    url = f"https://{host}/dp/{asin}?th=1&psc=1"
    title, price, buyable, availability = parse_amazon_product(fetch(url, AMAZON_MARKER))
    if buyable:
        return Result("in_stock", good=True, status_text="in stock",
                      title=title, price=price, url=url, detail=availability or None)
    return Result("out_of_stock", good=False, status_text="listed, not buyable yet",
                  title=title, price=price, url=url, detail=availability or None)


# --------------------------------------------------------------------------- #
# Flipkart
# --------------------------------------------------------------------------- #

FLIPKART_MARKER = "application/ld+json"


def parse_flipkart_product(page: str) -> tuple[str, int, bool]:
    """Return (name, price in rupees, in stock) from the schema.org Product block."""
    blocks = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                        page, re.S)
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not (isinstance(item, dict) and item.get("@type") == "Product"):
                continue
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if offers.get("price") is None:
                raise LayoutChanged("schema.org Product has no offers.price")
            in_stock = str(offers.get("availability", "")).endswith("/InStock")
            return item.get("name", "?"), round(float(offers["price"])), in_stock
    raise LayoutChanged("no schema.org Product data on the page")


def check_flipkart(url: str, max_price: int | None) -> Result:
    name, price, in_stock = parse_flipkart_product(fetch(url, FLIPKART_MARKER))
    return stock_and_price_result(name, price, in_stock, max_price, url)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SITES = {"amazon": "Amazon", "flipkart": "Flipkart"}


def build_target(entry: dict) -> Target:
    site = entry["site"]
    max_price = int(entry["max_price"]) if entry.get("max_price") is not None else None
    if site == "amazon" and "asin" in entry:
        host, asin = entry["host"], entry["asin"]

        def check() -> Result:
            return check_amazon_product(host, asin, max_price)
    elif site == "amazon":
        host, listing, match = entry["host"], entry["listing"], entry["match"]

        def check() -> Result:
            return check_amazon(host, listing, match)
    elif site == "flipkart":
        url = entry["url"]

        def check() -> Result:
            return check_flipkart(url, max_price)
    else:
        raise ValueError(f"unknown site {site!r} in target {entry.get('key')!r}")
    return Target(key=entry["key"], site=SITES[site], label=entry["label"], check=check)


def load_entries(path: Path | None = None) -> list[dict]:
    """Raw target entries; an absent file means nothing is being watched."""
    path = path or CONFIG_FILE  # resolved at call time, so tests can repoint it
    if not path.exists():
        return []
    entries = json.loads(path.read_text())["targets"]
    keys = [e["key"] for e in entries]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate target keys in {path}")
    return entries


def save_entries(entries: list[dict], path: Path | None = None) -> None:
    path = path or CONFIG_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"targets": entries}, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def load_targets(path: Path | None = None) -> list[Target]:
    return [build_target(e) for e in load_entries(path)]


# --------------------------------------------------------------------------- #
# Turning a pasted link into a target
# --------------------------------------------------------------------------- #

SHORT_LINK_HOSTS = {"amzn.in", "amzn.to", "a.co", "dl.flipkart.com", "fkrt.it", "fkrt.cc"}


def resolve_short_link(url: str) -> str:
    """Follow redirects from share links (amzn.in/d/..., dl.flipkart.com/...)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.geturl()
    except urllib.error.HTTPError as e:
        return e.geturl() or url  # the redirect target is still known on a blocked page


def parse_link(url: str) -> dict:
    """Map a product link to a target entry (minus label and max_price)."""
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host in SHORT_LINK_HOSTS:
        return parse_link(resolve_short_link(url))

    if host == "amazon.in" or host.endswith(".amazon.in"):
        m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", parts.path)
        if not m:
            raise ValueError("couldn't find a product ID (ASIN) in that Amazon link")
        asin = m.group(1)
        return {"key": f"amazon-{asin.lower()}", "site": "amazon",
                "host": "www.amazon.in", "asin": asin}

    if host == "flipkart.com" or host.endswith(".flipkart.com"):
        m = re.search(r"/p/(itm[0-9a-z]+)", parts.path)
        if not m:
            raise ValueError("that Flipkart link isn't a product page (no /p/itm... in it)")
        # pid pins the exact variant (colour/storage); everything else is tracking.
        pid = urllib.parse.parse_qs(parts.query).get("pid", [None])[0]
        clean = f"https://www.flipkart.com{parts.path}" + (f"?pid={pid}" if pid else "")
        return {"key": f"flipkart-{(pid or m.group(1)).lower()}", "site": "flipkart",
                "url": clean}

    raise ValueError(f"unsupported site {host!r}: only amazon.in and flipkart.com links work")


def parse_price(text: str) -> int:
    """'1,09,999', '₹109999' or '109999' -> 109999."""
    digits = re.sub(r"[^\d]", "", text.split(".")[0])
    if not digits:
        raise ValueError(f"not a price: {text!r}")
    return int(digits)


def short_label(title: str) -> str:
    """Trim marketing tails: 'Phone 5G (White, 512GB), 200MP Camera, ...' -> up to ')'."""
    title = re.sub(r"\s+", " ", title).strip()
    close = title.find(")")
    if 0 < close < 120:
        return title[:close + 1]
    return re.split(r"[,|]", title)[0][:80].strip()


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def notify_windows(title: str, body: str, url: str | None) -> bool:
    """Show a Windows toast from WSL. Stays on screen until dismissed."""
    ps = shutil.which("powershell.exe")
    if not ps:
        return False
    actions = ""
    launch = ""
    if url:
        u = xml_escape(url, {'"': "&quot;"})
        launch = f' activationType="protocol" launch="{u}"'
        actions = (
            f'<actions><action content="Open page" activationType="protocol" '
            f'arguments="{u}"/><action content="Dismiss" activationType="system" '
            f'arguments="dismiss"/></actions>'
        )
    xml = (
        f'<toast scenario="reminder"{launch}><visual><binding template="ToastGeneric">'
        f"<text>{xml_escape(title)}</text><text>{xml_escape(body)}</text>"
        f'</binding></visual><audio src="ms-winsoundevent:Notification.Looping.Alarm"/>'
        f"{actions}</toast>"
    )
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml(@'
{xml}
'@)
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
    [Windows.UI.Notifications.ToastNotification]::new($doc))
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    proc = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        log.warning("Windows toast failed: %s", proc.stderr.strip()[:300])
        return False
    return True


def notify_ntfy(title: str, body: str, url: str | None, priority: str = "urgent") -> bool:
    """Push to a phone via ntfy.sh.

    The topic name acts as the password for the channel, so it comes from the
    NTFY_TOPIC env var (a GitHub secret in CI) or a git-ignored local file,
    never from the source.
    """
    topic = os.environ.get("NTFY_TOPIC")
    if not topic and NTFY_TOPIC_FILE.exists():
        topic = NTFY_TOPIC_FILE.read_text().strip()
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("utf-8"), "Priority": priority,
               "Tags": "rotating_light" if priority == "urgent" else "white_check_mark"}
    if url:
        headers["Click"] = url
    req = urllib.request.Request(
        f"{server}/{topic}", data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.URLError as e:
        log.warning("ntfy push failed: %s", e)
        return False


def notify(title: str, body: str, url: str | None = None) -> None:
    sent = [name for name, fn in (("windows", notify_windows), ("ntfy", notify_ntfy))
            if fn(title, body, url)]
    log.info("NOTIFY [%s] %s | %s", ",".join(sent) or "none delivered", title, body)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    return migrate_state(state)


def migrate_state(state: dict) -> dict:
    """Move single-product state (top-level keys) under its target key."""
    if "targets" in state:
        return state
    legacy = {k: v for k, v in state.items() if k != "price"}  # price was a string
    if not legacy:
        return {"targets": {}}
    # The old format had no "good" flag; in_stock was its only alert-worthy
    # status. Without this, an item already in stock would alert again.
    legacy["good"] = legacy.get("status") == "in_stock"
    return {"targets": {LEGACY_TARGET_KEY: legacy}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)  # atomic, so an interrupted run can't corrupt it


# --------------------------------------------------------------------------- #
# Running checks
# --------------------------------------------------------------------------- #

def describe(result: Result) -> str:
    price = format_inr(result.price) if result.price is not None else "price not shown"
    return f"{price} - {result.title}" if result.title else price


def check_target(target: Target, ts: dict) -> bool:
    """Run one target's check, alert on changes, update its state. False if blocked."""
    name = f"{target.site}: {target.label}"
    try:
        result = target.check()
    except Blocked as e:
        ts["blocked_streak"] = ts.get("blocked_streak", 0) + 1
        ts["blocked_since_heartbeat"] = ts.get("blocked_since_heartbeat", 0) + 1
        log.warning("[%s] blocked (%d in a row): %s", target.key, ts["blocked_streak"], e)

        # A redesign won't fix itself, so it alerts sooner (2 runs in a row,
        # to ride out a one-off odd page) than ordinary blocks do.
        if isinstance(e, LayoutChanged):
            ts["layout_streak"] = ts.get("layout_streak", 0) + 1
            if ts["layout_streak"] == LAYOUT_ALERT_AFTER:
                notify(f"{target.site} watcher needs updating",
                       f"{target.site}'s page no longer looks the way watch.py expects "
                       f"({e}). Checks for {target.label} are not working until the "
                       f"parser is fixed.")
        else:
            ts["layout_streak"] = 0
            if ts["blocked_streak"] == BLOCKED_ALERT_AFTER:
                notify(f"{target.site} watcher can't see the page",
                       f"{BLOCKED_ALERT_AFTER} checks in a row were blocked ({e}). "
                       f"Alerts for {target.label} may be missed; see watch.log.")
        return False

    if ts.get("blocked_streak", 0) >= BLOCKED_ALERT_AFTER:
        log.info("[%s] page readable again after %d blocked runs",
                 target.key, ts["blocked_streak"])
    ts["blocked_streak"] = ts["layout_streak"] = 0
    ts["ok_since_heartbeat"] = ts.get("ok_since_heartbeat", 0) + 1
    ts["last_ok"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    log.info("[%s] status=%s price=%s detail=%s",
             target.key, result.status, result.price, result.detail)

    previous, was_good = ts.get("status"), ts.get("good", False)
    if result.status != previous:
        if result.good and not was_good:
            notify(f"{name} is {result.status_text}!", describe(result), result.url)
        elif was_good and not result.good:
            notify(f"{name} is now {result.status_text}", describe(result), result.url)
        elif previous == "not_listed":
            notify(f"{name} is {result.status_text}", describe(result), result.url)

    ts.update(status=result.status, good=result.good, status_text=result.status_text,
              price=result.price, url=result.url,
              last_check=time.strftime("%Y-%m-%d %H:%M:%S"))
    return True


def send_heartbeat(targets: list[Target], state: dict) -> None:
    """Quiet daily phone push summarising every target, then reset the counters."""
    lines = []
    for t in targets:
        ts = state["targets"].get(t.key, {})
        price = f" at {format_inr(ts['price'])}" if ts.get("price") is not None else ""
        lines.append(
            f"{t.site} {t.label}: {ts.get('status_text', 'unknown')}{price}. "
            f"{ts.get('ok_since_heartbeat', 0)} checks ok, "
            f"{ts.get('blocked_since_heartbeat', 0)} blocked, "
            f"last good check {ts.get('last_ok', 'never')}.")
        ts["ok_since_heartbeat"] = ts["blocked_since_heartbeat"] = 0
    notify_ntfy("Watcher: still running", "\n".join(lines), None, priority="low")
    log.info("heartbeat sent: %s", " | ".join(lines))


def run(args: argparse.Namespace, targets: list[Target] | None = None) -> int:
    """Check every target. Exit code 2 if any were blocked, else 0."""
    if targets is None:
        targets = load_targets()
    if args.only:
        targets = [t for t in targets if t.key in args.only]
    state = load_state()

    all_ok = True
    for target in targets:
        ts = state["targets"].setdefault(target.key, {})
        all_ok &= check_target(target, ts)

    if args.heartbeat:
        send_heartbeat(targets, state)
    save_state(state)
    return 0 if all_ok else 2


# --------------------------------------------------------------------------- #
# Managing targets and schedules (add / list / remove / pause / resume)
# --------------------------------------------------------------------------- #

GITHUB_VARIABLE = "WATCH_TARGETS"  # the scheduled workflow writes this to targets.json
WINDOWS_TASK = "Price Watch"


def sync_github(entries: list[dict]) -> None:
    """Copy the target list to the repo variable the GitHub schedule reads."""
    if not shutil.which("gh"):
        print("  GitHub: not synced (gh CLI not found); laptop checks still use it.")
        return
    proc = subprocess.run(["gh", "variable", "set", GITHUB_VARIABLE],
                          input=json.dumps({"targets": entries}, ensure_ascii=False),
                          capture_output=True, text=True, cwd=HERE)
    if proc.returncode == 0:
        print("  GitHub: synced; the next scheduled check uses the new list.")
    else:
        print(f"  GitHub: sync FAILED ({proc.stderr.strip()[:200]}). "
              f"Laptop checks still use the new list.")


def rule_text(entry: dict) -> str:
    if entry.get("max_price") is not None:
        return f"in stock at or under {format_inr(entry['max_price'])}"
    if "match" in entry:
        return f"variant {' + '.join(entry['match'])} listed and in stock"
    return "in stock"


def cmd_add(args: argparse.Namespace) -> int:
    try:
        entry = parse_link(args.url)
        max_price = parse_price(args.max_price) if args.max_price else None
    except ValueError as e:
        print(f"Can't add that: {e}")
        return 1
    if max_price is not None:
        entry["max_price"] = max_price

    # Check the page once now, so a bad link fails here rather than at 3 a.m.
    print(f"Checking {entry.get('url') or entry['asin']} ...")
    try:
        result = build_target({**entry, "label": "?"}).check()
    except Blocked as e:
        print(f"Couldn't read the page ({e}). Nothing added; try again in a minute.")
        return 2
    entry["label"] = args.label or short_label(result.title or entry["key"])

    entries = load_entries()
    replaced = any(e["key"] == entry["key"] for e in entries)
    entries = [e for e in entries if e["key"] != entry["key"]] + [entry]
    save_entries(entries)

    now = format_inr(result.price) if result.price is not None else "no price shown"
    print(f"{'Updated' if replaced else 'Added'} [{entry['key']}] {entry['label']}")
    print(f"  Now: {now}, {'in stock' if result.status != 'out_of_stock' else 'sold out'}")
    print(f"  Alert when: {rule_text(entry)}"
          + ("  <- already true; you'll get an alert on the next check" if result.good else ""))
    sync_github(entries)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    entries = load_entries()
    if not entries:
        print("Nothing is being watched. Add something with: watch.py add <link> [max price]")
        return 0
    state = load_state()["targets"]
    for n, e in enumerate(entries, 1):
        ts = state.get(e["key"], {})
        price = f"{format_inr(ts['price'])}, " if ts.get("price") is not None else ""
        last = (f"{price}{ts.get('status_text', '?')} (checked {ts['last_check']})"
                if "last_check" in ts else "not checked yet")
        print(f"{n}. [{e['key']}] {SITES[e['site']]}: {e['label']}")
        print(f"   alert when {rule_text(e)}; last: {last}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    entries = load_entries()
    wanted = args.target
    if wanted.isdigit() and 1 <= int(wanted) <= len(entries):
        matches = [entries[int(wanted) - 1]]
    else:
        matches = [e for e in entries if e["key"] == wanted] or \
                  [e for e in entries if e["key"].startswith(wanted)]
    if len(matches) != 1:
        print(f"{'No' if not matches else 'More than one'} target matches {wanted!r}; "
              f"see: watch.py list")
        return 1
    gone = matches[0]
    entries = [e for e in entries if e["key"] != gone["key"]]
    save_entries(entries)
    state = load_state()
    state["targets"].pop(gone["key"], None)
    save_state(state)
    print(f"Removed [{gone['key']}] {gone['label']}")
    sync_github(entries)
    return 0


def set_schedules(enabled: bool) -> int:
    """Enable or disable both the Windows task and the GitHub schedule."""
    verb = "Enable" if enabled else "Disable"
    ok = True
    ps = shutil.which("powershell.exe")
    if ps:
        proc = subprocess.run([ps, "-NoProfile", "-Command",
                               f"{verb}-ScheduledTask -TaskName '{WINDOWS_TASK}' | Out-Null"],
                              capture_output=True, text=True)
        ok &= proc.returncode == 0
        print(f"  Laptop task: {'ok' if proc.returncode == 0 else 'FAILED ' + proc.stderr.strip()[:150]}")
    else:
        print("  Laptop task: skipped (not running under WSL)")
    if shutil.which("gh"):
        # gh only finds workflows in the opposite state, so check before toggling.
        listing = subprocess.run(["gh", "workflow", "list", "--all", "--json", "name,state"],
                                 capture_output=True, text=True, cwd=HERE)
        states = {w["name"]: w["state"] for w in json.loads(listing.stdout or "[]")}
        if (states.get("watch") == "active") == enabled:
            print(f"  GitHub schedule: already {'on' if enabled else 'paused'}")
        else:
            proc = subprocess.run(["gh", "workflow", verb.lower(), "watch"],
                                  capture_output=True, text=True, cwd=HERE)
            ok &= proc.returncode == 0
            print(f"  GitHub schedule: {'ok' if proc.returncode == 0 else 'FAILED ' + proc.stderr.strip()[:150]}")
    else:
        print("  GitHub schedule: skipped (gh CLI not found)")
    return 0 if ok else 1


def cmd_pause(args: argparse.Namespace) -> int:
    print("Pausing all checks:")
    return set_schedules(False)


def cmd_resume(args: argparse.Namespace) -> int:
    print("Resuming checks:")
    sync_github(load_entries())  # GitHub may hold an older list
    return set_schedules(True)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="With no command, checks every target once (what the schedulers run).")
    p.add_argument("--only", nargs="+", metavar="KEY",
                   help="check only these target keys from targets.json")
    p.add_argument("--jitter", type=int, default=0,
                   help="sleep a random 0..N seconds first, so checks aren't clockwork")
    p.add_argument("--heartbeat", action="store_true",
                   help="after checking, push a low-priority 'still running' summary")
    p.add_argument("--test-notify", action="store_true",
                   help="send a test notification and exit")
    sub = p.add_subparsers(dest="command", metavar="command")
    add = sub.add_parser("add", help="watch an amazon.in or flipkart.com product link")
    add.add_argument("url")
    add.add_argument("max_price", nargs="?", help="alert only at or under this price")
    add.add_argument("--label", help="name to show in alerts (default: from the page)")
    sub.add_parser("list", help="show what's being watched and its last status")
    rm = sub.add_parser("remove", help="stop watching a target")
    rm.add_argument("target", help="number from 'list', or a key (or unique key prefix)")
    sub.add_parser("pause", help="disable the laptop task and the GitHub schedule")
    sub.add_parser("resume", help="re-enable both schedules")
    args = p.parse_args()

    commands = {"add": cmd_add, "list": cmd_list, "remove": cmd_remove,
                "pause": cmd_pause, "resume": cmd_resume}
    if args.command in commands:
        return commands[args.command](args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )

    if args.test_notify:
        notify("Watcher test", "Notifications are working.")
        return 0

    if args.jitter:
        time.sleep(random.uniform(0, args.jitter))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
