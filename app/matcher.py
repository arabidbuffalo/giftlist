"""Given an Amazon product, try to find the same item on a non-Amazon store.

Strategy:
  * Build search queries from the strongest identifiers we have: UPC/EAN, then brand + model number,
    then (weakest) brand + product name.
  * Search with SearXNG (if SEARXNG_URL is set) or DuckDuckGo's HTML endpoint.
  * Fetch candidate pages and only accept one when its product data proves it's the same item:
    matching UPC, or matching model number + brand. Name-only matches need a very close title + brand.
  * Prefer the brand's own website, then the cheapest verified retailer.
"""
from __future__ import annotations

import logging
import os
import re
from difflib import SequenceMatcher
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from . import scraper
from .scraper import Product, norm_gtin, norm_id, store_name

log = logging.getLogger(__name__)

SEARXNG_URL = os.environ.get("SEARXNG_URL", "").rstrip("/")
MAX_CANDIDATES = int(os.environ.get("MATCH_MAX_CANDIDATES", "8"))

# Sites that are never "the product's real website"
SKIP_DOMAINS = {
    "amazon", "amzn", "ebay", "aliexpress", "temu", "wish", "etsy", "reddit", "youtube", "facebook", "pinterest",
    "instagram", "tiktok", "twitter", "x", "wikipedia", "camelcamelcamel", "keepa", "slickdeals", "google",
    "bing", "duckduckgo", "quora", "rtings", "cnet", "pcmag", "theverge", "wired", "tomsguide", "techradar",
    "nytimes", "manualslib", "manua", "upcitemdb", "barcodelookup", "pricespider", "pricegrabber", "shopsavvy",
    "idealo", "pricerunner", "klarna", "joom", "dhgate", "alibaba", "mercari", "poshmark", "offerup", "craigslist",
}


def _domain_root(url: str) -> str:
    host = store_name(url)
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net"} and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else host


def _allowed(url: str) -> bool:
    if not url.startswith("http"):
        return False
    root = _domain_root(url)
    if root in SKIP_DOMAINS or scraper.is_amazon_url(url):
        return False
    path = urlparse(url).path.lower()
    # Search/category pages rarely carry a single product's data
    if re.search(r"/(search|s|category|categories|c|blog|news|reviews?|compare|support|manuals?)(/|$)", path) and "/p/" not in path:
        return False
    return True


# ---------------------------------------------------------------- search backends

def _search_searxng(q: str) -> list[str]:
    from curl_cffi import requests as creq

    r = creq.get(f"{SEARXNG_URL}/search", params={"q": q, "format": "json", "language": "en-US"}, timeout=20)
    r.raise_for_status()
    return [x["url"] for x in r.json().get("results", []) if x.get("url")]


def _search_ddg(q: str) -> list[str]:
    from curl_cffi import requests as creq

    r = creq.post("https://html.duckduckgo.com/html/", data={"q": q, "kl": "us-en"}, impersonate="chrome", timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.select("a.result__a, a.result-link"):
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        if href.startswith("//"):
            href = "https:" + href
        if href:
            out.append(href)
    return out


def search(q: str) -> list[str]:
    try:
        return _search_searxng(q) if SEARXNG_URL else _search_ddg(q)
    except Exception as e:
        log.warning("search failed for %r: %s", q, e)
        return []


# ---------------------------------------------------------------- verification

def _short_name(name: str, brand: str | None) -> str:
    n = re.sub(r"[\(\[].*?[\)\]]", " ", name or "")
    n = re.split(r"[,|–\-]\s", n)[0]
    words = n.split()
    if brand and words and words[0].lower() != brand.lower():
        words = [brand] + words
    return " ".join(words[:9])


def _similar(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+", (a or "").lower()))
    tb = set(re.findall(r"[a-z0-9]+", (b or "").lower()))
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb) / min(len(ta), len(tb))
    return max(overlap, SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio())


def verify(src: Product, cand: Product, page_text: str = "") -> int:
    """Return a confidence score (0 = reject)."""
    if cand.price is None or not cand.name:
        return 0
    text = norm_id(page_text)
    brand_ok = not src.brand or norm_id(src.brand) in norm_id(f"{cand.brand} {cand.name} {cand.store}") or norm_id(src.brand) in text

    g_src = norm_gtin(src.gtin)
    if g_src and len(g_src) >= 8:
        if norm_gtin(cand.gtin) == g_src:
            return 100
        if g_src in re.sub(r"\D", "", page_text or ""):
            return 90

    mpn = norm_id(src.mpn)
    if mpn and len(mpn) >= 4 and brand_ok:
        cand_ids = {norm_id(x) for x in [cand.mpn, cand.gtin, *cand.extra_ids] if x}
        if mpn in cand_ids:
            return 85
        if mpn in text or mpn in norm_id(cand.name):
            return 70

    if brand_ok and _similar(src.name or "", cand.name) >= 0.9:
        return 50
    return 0


def _queries(src: Product) -> list[str]:
    qs = []
    if src.gtin and len(norm_gtin(src.gtin)) >= 8:
        digits = re.sub(r"\D", "", src.gtin)
        qs.append(f'"{digits}"')
    if src.mpn:
        qs.append(f'{src.brand or ""} "{src.mpn}"'.strip())
    if src.name:
        qs.append(_short_name(src.name, src.brand))
    return qs


def find_alternative(src: Product, fetch=None, search_fn=None) -> Product | None:
    """Return the best verified non-Amazon listing, or None."""
    fetch = fetch or scraper.fetch
    search_fn = search_fn or search
    seen: set[str] = set()
    candidates: list[str] = []
    for q in _queries(src):
        for url in search_fn(q):
            key = url.split("#")[0]
            if key not in seen and _allowed(key):
                seen.add(key)
                candidates.append(key)
        if len(candidates) >= MAX_CANDIDATES:
            break

    brand_root = norm_id(src.brand)
    # Check brand's own site first
    candidates.sort(key=lambda u: 0 if brand_root and brand_root in norm_id(store_name(u)) else 1)

    best: tuple[int, Product] | None = None
    for url in candidates[:MAX_CANDIDATES]:
        try:
            final, html = fetch(url)
        except Exception as e:
            log.info("candidate %s failed: %s", url, e)
            continue
        if scraper.is_amazon_url(final):
            continue
        cand = scraper.parse(html, final)
        text = BeautifulSoup(html, "lxml").get_text(" ")[:400000]
        score = verify(src, cand, text)
        log.info("candidate %s score=%s price=%s", final, score, cand.price)
        if not score:
            continue
        if brand_root and brand_root in norm_id(cand.store):
            score += 35  # official brand site wins over a retailer
        if best is None or score > best[0] or (score == best[0] and (cand.price or 1e9) < (best[1].price or 1e9)):
            best = (score, cand)
        if best[0] >= 120:
            break
    return best[1] if best else None
