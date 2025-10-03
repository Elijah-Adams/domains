#!/usr/bin/env python3
"""
Multithreaded scraper for INWX, Netim, and Regery.

ChatGPT: https://chatgpt.com/c/68e02f93-5aa4-8321-969f-73697af23d04

Goal:
  For each two-letter ccTLD in two_letter_tlds.txt, fetch data from:
    - INWX   : https://www.inwx.com/en/{tld}-domain
    - Netim  : https://www.netim.com/en/domain-name/{tld}-domain
    - Regery : https://regery.com/en/domains/zone/{tld}

Extract:
  - min_char, max_char  (character limits)
  - registration_price  (rounded to nearest dollar, HALF_UP)

Save:
  - CSV columns: tld,min_char,max_char,registration_price,provider
    where provider is the site (inwx|netim|regery) offering the LOWEST price found.

Progress:
  - Prints a status line per TLD after scraping all three providers, e.g.:
      [5/120] th: inwx OK; netim ERR(HTTP 404); regery OK -> chosen=inwx $200 len=2-63

Usage:
  python scrape_ccTLDs_lowest_price.py \
      --input two_letter_tlds.txt \
      --output output.csv \
      --max-workers 16 \
      --timeout 20 \
      --retries 2 \
      --backoff 0.75

Notes:
  - We combine character bounds across sources:
      min_char = minimum of available mins
      max_char = maximum of available maxes
    (gives a safe range if sites disagree or one is missing)
  - Currency is not normalized; we parse numeric price values shown.
  - Requests sessions are per-thread (thread-local) for performance.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple, List, Dict

import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

INWX_URL   = "https://www.inwx.com/en/{tld}-domain"
NETIM_URL  = "https://www.netim.com/en/domain-name/{tld}-domain"
REGERY_URL = "https://regery.com/en/domains/zone/{tld}"

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

# Patterns
LEN_RANGE_DASH_RE   = re.compile(r"(\d+)\s*[-–]\s*(\d+)\s*(?:characters|letters)?", re.I)
LEN_RANGE_BETWEEN_RE = re.compile(r"between\s+(\d+)\s+and\s+(\d+)\s+(?:characters|letters)", re.I)
PRICE_NUM_RE        = re.compile(r"(\d+(?:[.,]\d{3})*(?:[.,]\d{2})|\d+(?:[.,]\d{0,2})?)")

# Thread-local session (requests.Session is not thread-safe across threads)
_tls = threading.local()


@dataclass
class ProviderResult:
    provider: str  # "inwx" | "netim" | "regery"
    min_char: Optional[int]
    max_char: Optional[int]
    price: Optional[int]
    error: Optional[str] = None


@dataclass
class TLDResult:
    tld: str
    min_char: Optional[int]
    max_char: Optional[int]
    registration_price: Optional[int]
    provider: str  # inwx|netim|regery or ""
    inwx_status: str
    netim_status: str
    regery_status: str


def _get_session(user_agent: str) -> requests.Session:
    sess = getattr(_tls, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"User-Agent": user_agent})
        _tls.session = sess
    return sess


def _round_to_int_half_up(num_str: str) -> int:
    """Round a numeric string to the nearest integer with HALF_UP semantics."""
    try:
        ns = num_str.replace(",", "")
        d = Decimal(ns)
    except Exception:
        # EU style: "1.234,56" -> "1234.56"
        ns = num_str.replace(".", "").replace(",", ".")
        d = Decimal(ns)
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _http_get(url: str, timeout: int, user_agent: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        sess = _get_session(user_agent)
        r = sess.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.text, None
        return None, f"HTTP {r.status_code}"
    except Exception as e:
        return None, str(e)


# ---------- Generic helpers ----------

def _find_len_range_in_text(text: str) -> Tuple[Optional[int], Optional[int]]:
    """Try multiple patterns to extract min/max char range from free text."""
    m = LEN_RANGE_BETWEEN_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = LEN_RANGE_DASH_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


# ---------- INWX parsing ----------

def parse_inwx_min_max(soup: BeautifulSoup) -> Tuple[Optional[int], Optional[int]]:
    # Primary: "Domain characteristics" table row "Minimum and maximum length" -> "X - Y characters"
    table = soup.select_one("#fs_characteristics table")
    if table:
        for tr in table.select("tr"):
            tds = tr.select("td")
            if len(tds) >= 2:
                label = tds[0].get_text(" ", strip=True).casefold()
                if "minimum" in label and "maximum" in label and "length" in label:
                    val = tds[1].get_text(" ", strip=True)
                    m = LEN_RANGE_DASH_RE.search(val)
                    if m:
                        return int(m.group(1)), int(m.group(2))
    # Fallback: scan whole text
    return _find_len_range_in_text(soup.get_text(" ", strip=True))


def parse_inwx_price(soup: BeautifulSoup) -> Optional[int]:
    # Prefer JSON-LD Product.offers.price
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        objs = [data] if isinstance(data, dict) else [d for d in data if isinstance(d, dict)]
        for obj in objs:
            if obj.get("@type") == "Product" and isinstance(obj.get("offers"), dict):
                p = obj["offers"].get("price")
                if p is not None:
                    try:
                        return _round_to_int_half_up(str(p))
                    except Exception:
                        pass
    # Fallback: visible .tld-page-price span[data-price]
    span = soup.select_one(".tld-page-price span[data-price]")
    if span and span.has_attr("data-price"):
        try:
            return _round_to_int_half_up(span["data-price"])
        except Exception:
            pass
    # Last resort: any number in the price block
    block = soup.select_one(".tld-page-price")
    if block:
        txt = block.get_text(" ", strip=True)
        m = PRICE_NUM_RE.search(txt)
        if m:
            try:
                return _round_to_int_half_up(m.group(1))
            except Exception:
                pass
    return None


def scrape_inwx(tld: str, timeout: int, user_agent: str) -> ProviderResult:
    url = INWX_URL.format(tld=tld)
    html, err = _http_get(url, timeout, user_agent)
    if err or not html:
        return ProviderResult("inwx", None, None, None, err or "No HTML")
    soup = BeautifulSoup(html, "html.parser")
    mn, mx = parse_inwx_min_max(soup)
    price = parse_inwx_price(soup)
    if mn is None and mx is None and price is None:
        return ProviderResult("inwx", None, None, None, "Could not parse")
    return ProviderResult("inwx", mn, mx, price, None)


# ---------- Netim parsing ----------

def parse_netim_min_max(soup: BeautifulSoup) -> Tuple[Optional[int], Optional[int]]:
    # Badge like "1-63 characters"
    badges = [b.get_text(" ", strip=True) for b in soup.select("#informations .etiquette_item")]
    m = LEN_RANGE_DASH_RE.search(" | ".join(badges))
    if m:
        return int(m.group(1)), int(m.group(2))
    # Fallback: full page
    return _find_len_range_in_text(soup.get_text(" ", strip=True))


def parse_netim_price(soup: BeautifulSoup) -> Optional[int]:
    # Registration price in the detailed information box
    for block in soup.select("#detailed-information .id_item"):
        label = block.select_one(".id_item-label")
        if label and "registration" in label.get_text(strip=True).lower():
            val = block.select_one(".id_item-value")
            if not val:
                continue
            # Prefer a <span class="new-price"> or <span class="price"> number
            txt = val.get_text(" ", strip=True)
            m = PRICE_NUM_RE.search(txt)
            if m:
                try:
                    return _round_to_int_half_up(m.group(1))
                except Exception:
                    pass
    return None


def scrape_netim(tld: str, timeout: int, user_agent: str) -> ProviderResult:
    url = NETIM_URL.format(tld=tld)
    html, err = _http_get(url, timeout, user_agent)
    if err or not html:
        return ProviderResult("netim", None, None, None, err or "No HTML")
    soup = BeautifulSoup(html, "html.parser")
    mn, mx = parse_netim_min_max(soup)
    price = parse_netim_price(soup)
    if mn is None and mx is None and price is None:
        return ProviderResult("netim", None, None, None, "Could not parse")
    return ProviderResult("netim", mn, mx, price, None)


# ---------- Regery parsing ----------

def parse_regery_min_max(soup: BeautifulSoup) -> Tuple[Optional[int], Optional[int]]:
    # They often write: "A domain in the .TH space is between 2 and 63 letters long."
    text = soup.get_text(" ", strip=True)
    return _find_len_range_in_text(text)


def parse_regery_price(soup: BeautifulSoup) -> Optional[int]:
    # Prefer schema.org/Offer meta price
    meta = soup.select_one('[itemtype="http://schema.org/Offer"] meta[itemprop="price"]')
    if meta and meta.get("content"):
        try:
            return _round_to_int_half_up(meta["content"])
        except Exception:
            pass
    # Fallback: search visible price strings (e.g., "4499.99 $/yr")
    # Prefer occurrences near "Prices for domains" or "Registration"
    candidates = []
    for el in soup.find_all(string=re.compile(r"Registration|Prices for domains|/yr", re.I)):
        seg = el.parent.get_text(" ", strip=True) if hasattr(el, "parent") else str(el)
        candidates.append(seg)
    blob = " | ".join(candidates) or soup.get_text(" ", strip=True)
    m = PRICE_NUM_RE.search(blob)
    if m:
        try:
            return _round_to_int_half_up(m.group(1))
        except Exception:
            pass
    return None


def scrape_regery(tld: str, timeout: int, user_agent: str) -> ProviderResult:
    url = REGERY_URL.format(tld=tld)
    html, err = _http_get(url, timeout, user_agent)
    if err or not html:
        return ProviderResult("regery", None, None, None, err or "No HTML")
    soup = BeautifulSoup(html, "html.parser")
    mn, mx = parse_regery_min_max(soup)
    price = parse_regery_price(soup)
    if mn is None and mx is None and price is None:
        return ProviderResult("regery", None, None, None, "Could not parse")
    return ProviderResult("regery", mn, mx, price, None)


# ---------- Worker & Orchestration ----------

def scrape_all_for_tld(tld: str, timeout: int, retries: int, backoff: float, user_agent: str) -> TLDResult:
    def do_with_retry(fn):
        attempt = 0
        while True:
            res = fn()
            if res.error is None or attempt >= retries:
                return res
            time.sleep(backoff * (2 ** attempt))
            attempt += 1

    inwx_res   = do_with_retry(lambda: scrape_inwx(tld, timeout, user_agent))
    netim_res  = do_with_retry(lambda: scrape_netim(tld, timeout, user_agent))
    regery_res = do_with_retry(lambda: scrape_regery(tld, timeout, user_agent))

    # Combine character limits across all providers (safe bounds)
    mins = [r.min_char for r in (inwx_res, netim_res, regery_res) if r.min_char is not None]
    maxs = [r.max_char for r in (inwx_res, netim_res, regery_res) if r.max_char is not None]
    min_char = min(mins) if mins else None
    max_char = max(maxs) if maxs else None

    # Choose lowest price & provider
    price_map = {r.provider: r.price for r in (inwx_res, netim_res, regery_res) if r.price is not None}
    if price_map:
        provider, price = sorted(price_map.items(), key=lambda kv: kv[1])[0]
    else:
        provider, price = "", None

    def status(r: ProviderResult) -> str:
        return "OK" if r.error is None else f"ERR({r.error})"

    return TLDResult(
        tld=tld,
        min_char=min_char,
        max_char=max_char,
        registration_price=price,
        provider=provider,
        inwx_status=status(inwx_res),
        netim_status=status(netim_res),
        regery_status=status(regery_res),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="two_letter_tlds.txt", help="Path to TLD list file")
    ap.add_argument("--output", default="output.csv", help="CSV output path")
    ap.add_argument("--max-workers", type=int, default=16, help="Max concurrent threads")
    ap.add_argument("--timeout", type=int, default=20, help="HTTP timeout (seconds)")
    ap.add_argument("--retries", type=int, default=2, help="Retry count on errors")
    ap.add_argument("--backoff", type=float, default=0.75, help="Exponential backoff base (seconds)")
    ap.add_argument("--user-agent", default=DEFAULT_UA, help="Custom User-Agent")
    args = ap.parse_args()

    # Load & normalize TLDs (two letters), keep input order, dedupe
    seen = set()
    tlds: List[str] = []
    with open(args.input, "r", encoding="utf-8") as f:
        for line in f:
            t = line.strip().lower().lstrip(".")
            if re.fullmatch(r"[a-z]{2}", t) and t not in seen:
                seen.add(t)
                tlds.append(t)

    total = len(tlds)
    if total == 0:
        print("No valid two-letter TLDs found in input.")
        return

    results: Dict[str, TLDResult] = {}
    done = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        future_map = {
            ex.submit(scrape_all_for_tld, tld, args.timeout, args.retries, args.backoff, args.user_agent): tld
            for tld in tlds
        }
        for fut in as_completed(future_map):
            res = fut.result()
            results[res.tld] = res
            with lock:
                done += 1
                pieces = [
                    f"inwx {res.inwx_status}",
                    f"netim {res.netim_status}",
                    f"regery {res.regery_status}",
                ]
                extra = []
                if res.registration_price is not None and res.provider:
                    extra.append(f"chosen={res.provider} ${res.registration_price}")
                if res.min_char is not None and res.max_char is not None:
                    extra.append(f"len={res.min_char}-{res.max_char}")
                tail = " | ".join(extra) if extra else "no data"
                print(f"[{done}/{total}] {res.tld}: " + "; ".join(pieces) + f" -> {tail}", flush=True)

    # Write CSV
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["tld", "min_char", "max_char", "registration_price", "provider"],
        )
        writer.writeheader()
        for tld in tlds:
            r = results.get(tld)
            writer.writerow({
                "tld": tld,
                "min_char": r.min_char if r and r.min_char is not None else "",
                "max_char": r.max_char if r and r.max_char is not None else "",
                "registration_price": r.registration_price if r and r.registration_price is not None else "",
                "provider": r.provider if r and r.provider else "",
            })

    print(f"Done. Wrote {total} rows to {args.output}")


if __name__ == "__main__":
    main()
