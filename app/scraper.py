"""Fetch a product page and pull out name, image, price, list price and identifiers.

Works in layers, most reliable first:
  1. schema.org Product data in JSON-LD (most stores publish this)
  2. microdata (itemprop="price" etc.)
  3. OpenGraph / product meta tags
  4. Amazon-specific selectors (Amazon publishes little structured data)
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "20"))
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class Product:
    url: str
    store: str = ""
    name: str | None = None
    image: str | None = None
    price: float | None = None
    list_price: float | None = None  # "was" / strikethrough price, if the store shows one
    currency: str | None = None
    brand: str | None = None
    gtin: str | None = None  # UPC / EAN
    mpn: str | None = None  # manufacturer model number
    in_stock: bool | None = None
    extra_ids: list[str] = field(default_factory=list)

    @property
    def is_amazon(self) -> bool:
        return is_amazon_url(self.url)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FetchError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def is_amazon_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return bool(re.search(r"(^|\.)amazon\.[a-z.]+$", host)) or host in {"a.co", "amzn.to", "amzn.com"}


def store_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    host = re.sub(r"^(www\d?|m|smile)\.", "", host)
    return host


def clean_amazon_url(url: str) -> str:
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", url)
    if m:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}/dp/{m.group(1)}"
    return url


def parse_price(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = str(value).strip()
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    # Decide decimal separator: the last of '.' or ',' followed by 1-2 digits.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if re.search(r",\d{1,2}$", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def norm_id(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def norm_gtin(s: str | None) -> str:
    d = re.sub(r"\D", "", s or "")
    return d.lstrip("0")


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


# ---------------------------------------------------------------- fetching

def fetch(url: str) -> tuple[str, str]:
    """Return (final_url, html). Uses browser TLS impersonation to get past basic bot checks."""
    try:
        from curl_cffi import requests as creq

        r = creq.get(url, impersonate="chrome", headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        status, text, final = r.status_code, r.text, str(r.url)
    except ImportError:  # pragma: no cover
        import httpx

        r = httpx.get(url, headers={**HEADERS, "User-Agent": "Mozilla/5.0"}, timeout=TIMEOUT, follow_redirects=True)
        status, text, final = r.status_code, r.text, str(r.url)
    except Exception as e:
        raise FetchError(f"Could not reach {store_name(url)}: {e}") from e
    if status >= 400:
        raise FetchError(f"{store_name(url)} returned HTTP {status}")
    if is_amazon_url(final) and ("captcha" in text.lower()[:20000] and "productTitle" not in text):
        raise FetchError("Amazon showed a bot check (captcha). Try again later.")
    return final, text


# ---------------------------------------------------------------- parsing

def _walk_jsonld(obj: Any):
    if isinstance(obj, list):
        for o in obj:
            yield from _walk_jsonld(o)
    elif isinstance(obj, dict):
        yield obj
        for key in ("@graph", "mainEntity", "hasVariant", "itemListElement", "item"):
            if key in obj:
                yield from _walk_jsonld(obj[key])


def _types(o: dict) -> set[str]:
    t = o.get("@type", [])
    if isinstance(t, str):
        t = [t]
    return {str(x).split("/")[-1] for x in t}


def _first_image(img: Any, base: str) -> str | None:
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl")
    return urljoin(base, img) if isinstance(img, str) and img else None


def _apply_offers(p: Product, offers: Any) -> None:
    if isinstance(offers, dict) and "offers" in offers and "price" not in offers and "lowPrice" not in offers:
        offers = offers["offers"]
    if not isinstance(offers, list):
        offers = [offers]
    prices = []
    for off in offers:
        if not isinstance(off, dict):
            continue
        price = parse_price(off.get("price") or off.get("lowPrice"))
        specs = off.get("priceSpecification") or []
        if isinstance(specs, dict):
            specs = [specs]
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            ptype = str(spec.get("priceType", "")).split("/")[-1]
            sp = parse_price(spec.get("price"))
            if ptype in ("StrikethroughPrice", "ListPrice", "MSRP", "SRP") and sp:
                p.list_price = p.list_price or sp
            elif sp and not price:
                price = sp
            p.currency = p.currency or spec.get("priceCurrency")
        if price:
            prices.append(price)
        p.currency = p.currency or off.get("priceCurrency")
        avail = str(off.get("availability", ""))
        if avail:
            p.in_stock = "InStock" in avail or "OnlineOnly" in avail or "LimitedAvailability" in avail
    if prices and p.price is None:
        p.price = min(prices)


def _from_jsonld(soup: BeautifulSoup, p: Product) -> None:
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            try:  # some stores leave trailing commas / control chars
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.strip()), strict=False)
            except Exception:
                continue
        for o in _walk_jsonld(data):
            if not ({"Product", "ProductGroup", "IndividualProduct", "ProductModel"} & _types(o)):
                continue
            p.name = p.name or (o.get("name") or None)
            p.image = p.image or _first_image(o.get("image"), p.url)
            brand = o.get("brand") or o.get("manufacturer")
            if isinstance(brand, list):
                brand = brand[0] if brand else None
            if isinstance(brand, dict):
                brand = brand.get("name")
            if isinstance(brand, str) and brand.strip():
                p.brand = p.brand or brand.strip()
            for k in ("gtin12", "gtin13", "gtin", "gtin14", "gtin8", "upc", "isbn"):
                if o.get(k):
                    p.gtin = p.gtin or str(o[k])
            if o.get("mpn"):
                p.mpn = p.mpn or str(o["mpn"])
            if o.get("sku"):
                p.extra_ids.append(str(o["sku"]))
            if o.get("model") and isinstance(o["model"], str):
                p.extra_ids.append(o["model"])
            if "offers" in o:
                _apply_offers(p, o["offers"])


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for n in names:
        el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) or soup.find(
            "meta", attrs={"itemprop": n}
        )
        if el and el.get("content"):
            return el["content"].strip()
    return None


def _from_meta(soup: BeautifulSoup, p: Product) -> None:
    p.name = p.name or _meta(soup, "og:title", "twitter:title")
    img = _meta(soup, "og:image", "og:image:secure_url", "twitter:image")
    if img and not p.image:
        p.image = urljoin(p.url, img)
    sale = parse_price(_meta(soup, "product:sale_price:amount"))
    price = parse_price(_meta(soup, "product:price:amount", "og:price:amount"))
    orig = parse_price(_meta(soup, "product:original_price:amount"))
    if p.price is None:
        p.price = sale or price
    if sale and price and price > sale:
        p.list_price = p.list_price or price
    if orig and p.price and orig > p.price:
        p.list_price = p.list_price or orig
    p.currency = p.currency or _meta(soup, "product:price:currency", "og:price:currency")
    p.brand = p.brand or _meta(soup, "product:brand", "og:brand")
    p.gtin = p.gtin or _meta(soup, "product:upc", "product:ean", "gtin13", "gtin12")
    p.mpn = p.mpn or _meta(soup, "product:mfr_part_no", "mpn")
    # microdata
    if p.price is None:
        el = soup.find(attrs={"itemprop": "price"})
        if el:
            p.price = parse_price(el.get("content") or _text(el))
    if not p.name:
        p.name = _text(soup.find("h1")) or (soup.title.string.strip() if soup.title and soup.title.string else None)


AMZ_PRICE_BOXES = [
    "#corePrice_feature_div",
    "#corePriceDisplay_desktop_feature_div",
    "#corePrice_desktop",
    "#apex_desktop",
    "#apex_offerDisplay_desktop",
    "#price",
    "#buybox",
]


def _from_amazon(soup: BeautifulSoup, p: Product) -> None:
    p.name = _text(soup.select_one("#productTitle")) or p.name
    img = soup.select_one("#landingImage, #imgBlkFront, #imgTagWrapperId img, #main-image")
    if img:
        src = img.get("data-old-hires") or None
        if not src and img.get("data-a-dynamic-image"):
            try:
                imgs = json.loads(img["data-a-dynamic-image"])
                src = max(imgs.items(), key=lambda kv: kv[1][0] * kv[1][1])[0]
            except Exception:
                pass
        p.image = src or img.get("src") or p.image

    for sel in AMZ_PRICE_BOXES:
        box = soup.select_one(sel)
        if not box:
            continue
        cur = box.select_one(".priceToPay .a-offscreen, .apexPriceToPay .a-offscreen, .a-price:not(.a-text-price) .a-offscreen")
        if cur and parse_price(_text(cur)):
            p.price = parse_price(_text(cur))
            was = box.select_one(".basisPrice .a-offscreen, .a-text-price[data-a-strike] .a-offscreen, .a-price.a-text-price .a-offscreen")
            if was:
                wp = parse_price(_text(was))
                if wp and p.price and wp > p.price:
                    p.list_price = wp
            sym = _text(box.select_one(".a-price-symbol"))
            if sym == "$":
                p.currency = p.currency or "USD"
            break
    if p.price is None:
        el = soup.select_one("#priceblock_dealprice, #priceblock_ourprice, #priceblock_saleprice, #kindle-price, .a-price .a-offscreen")
        if el:
            p.price = parse_price(_text(el))

    # Detail tables: brand, model number, UPC
    details: dict[str, str] = {}
    for row in soup.select("#productDetails_techSpec_section_1 tr, #productDetails_detailBullets_sections1 tr, "
                           "#productDetails_techSpec_section_2 tr, table.prodDetTable tr, #technicalSpecifications_section_1 tr"):
        k, v = row.find("th"), row.find("td")
        if k and v:
            details[_text(k).lower().strip(" :\u200e\u200f")] = _text(v).strip("\u200e\u200f ")
    for li in soup.select("#detailBullets_feature_div li, #detail-bullets li"):
        bold = li.select_one(".a-text-bold")
        if bold:
            k = re.sub(r"[\s:\u200e\u200f]+$", "", _text(bold)).lower()
            v = _text(li)[len(_text(bold)):].strip(" :\u200e\u200f")
            details[k.strip(" :\u200e\u200f")] = v
    for row in soup.select("#poExpander tr, #productOverview_feature_div tr"):
        tds = row.find_all("td")
        if len(tds) >= 2:
            details.setdefault(_text(tds[0]).lower(), _text(tds[1]))

    def d(*keys):
        for k in keys:
            for dk, dv in details.items():
                if dk == k or dk.startswith(k):
                    return dv
        return None

    p.mpn = p.mpn or d("item model number", "model number", "part number", "model name")
    upc = d("upc", "ean", "gtin")
    if upc:
        p.gtin = p.gtin or upc.split()[0]
    brand = d("brand", "manufacturer", "brand name")
    if not brand:
        byline = _text(soup.select_one("#bylineInfo"))
        m = re.match(r"(?:Visit the (.+?) Store|Brand: (.+))", byline)
        if m:
            brand = m.group(1) or m.group(2)
    p.brand = p.brand or brand
    asin = d("asin")
    if asin:
        p.extra_ids.append(asin)
    avail = _text(soup.select_one("#availability")).lower()
    if avail:
        p.in_stock = not ("unavailable" in avail or "out of stock" in avail)


def parse(html: str, url: str) -> Product:
    soup = BeautifulSoup(html, "lxml")
    p = Product(url=url, store=store_name(url))
    if is_amazon_url(url):
        _from_amazon(soup, p)
    _from_jsonld(soup, p)
    _from_meta(soup, p)
    if p.name:
        p.name = re.sub(r"\s+", " ", p.name).strip()
        # Strip trailing " | Store Name" / " - Store" suffixes from page titles
        p.name = re.sub(r"\s+[|\-–]\s+[^|\-–]{2,30}$", "", p.name) if len(p.name) > 40 else p.name
    if p.list_price and p.price and p.list_price <= p.price:
        p.list_price = None
    if p.currency is None and p.price is not None and ("$" in html[:200000]):
        p.currency = "USD"
    return p


def scrape(url: str) -> Product:
    if is_amazon_url(url):
        url = clean_amazon_url(url)
    final, html = fetch(url)
    if is_amazon_url(final):
        final = clean_amazon_url(final)
    return parse(html, final)
