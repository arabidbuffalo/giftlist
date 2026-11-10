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
    variant: str | None = None  # e.g. "Vinyl LP" when a page sells several versions
    variants: list[dict] = field(default_factory=list)
    variant_key: str | None = None  # stable id of the selected version

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

class BlockedError(FetchError):
    """The site answered with a bot check instead of the page."""

    def __init__(self, msg: str, final_url: str = ""):
        super().__init__(msg)
        self.final_url = final_url


# Different browser fingerprints; Amazon often lets one through when another gets a captcha.
FINGERPRINTS = ["chrome", "safari", "chrome_android", "safari_ios", "edge"]


def _looks_blocked(final: str, status: int, text: str) -> bool:
    if not is_amazon_url(final):
        return False
    head = text[:30000].lower()
    if "productTitle" in text:
        return False
    return status == 503 or "captcha" in head or "/errors/validatecaptcha" in head or "robot check" in head


def _get(session, url: str):
    r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    return r.status_code, r.text, str(r.url)


def fetch(url: str) -> tuple[str, str]:
    """Return (final_url, html). Uses browser TLS impersonation to get past basic bot checks."""
    import random
    import time

    from curl_cffi import requests as creq

    tries = FINGERPRINTS if is_amazon_url(url) else FINGERPRINTS[:1]
    status, text, final = 0, "", url
    last_err = None
    for fp in tries:
        try:
            with creq.Session(impersonate=fp) as s:
                if is_amazon_url(url):
                    s.cookies.set("i18n-prefs", "USD", domain=".amazon.com")
                    s.cookies.set("lc-main", "en_US", domain=".amazon.com")
                status, text, final = _get(s, url)
                if _looks_blocked(final, status, text):
                    # Visiting the home page first gives the session normal cookies; then retry.
                    p = urlparse(final if is_amazon_url(final) else url)
                    _get(s, f"{p.scheme}://{p.netloc}/")
                    time.sleep(random.uniform(1.0, 2.5))
                    status, text, final = _get(s, url)
        except Exception as e:  # bad fingerprint name, network error, etc.
            last_err = e
            log.info("fetch %s with %s failed: %s", url, fp, e)
            continue
        if not _looks_blocked(final, status, text):
            break
        log.info("Amazon blocked fingerprint %s", fp)
        time.sleep(random.uniform(0.5, 1.5))
    else:
        if status == 0 and last_err:
            raise FetchError(f"Could not reach {store_name(url)}: {last_err}")
        if _looks_blocked(final, status, text):
            raise BlockedError("Amazon showed a bot check (captcha).", final)
    if status >= 400:
        raise FetchError(f"{store_name(url)} returned HTTP {status}")
    return final, text


def amazon_asin(url: str) -> str | None:
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", url)
    return m.group(1) if m else None


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


def variant_ids(url: str) -> set[str]:
    """IDs in a URL that usually name the selected version (?variant=, ?sku=, ?preselect=, ...)."""
    from urllib.parse import parse_qs as _pq

    q = _pq(urlparse(url).query)
    ids = set()
    for k in ("variant", "variantid", "variant_id", "sku", "skuid", "skuId", "selectedsku", "preselect", "pid",
              "id", "item", "option", "format", "edition", "size"):
        for key in (k, k.lower()):
            for v in q.get(key, []):
                if v:
                    ids.add(v.strip().lower())
    return ids


def _offer_list(offers: Any) -> list[dict]:
    if isinstance(offers, dict) and "offers" in offers and "price" not in offers and "lowPrice" not in offers:
        offers = offers["offers"]
    if not isinstance(offers, list):
        offers = [offers]
    return [o for o in offers if isinstance(o, dict)]


def _ids_of(d: dict) -> set[str]:
    out = set()
    for k in ("sku", "mpn", "gtin", "gtin12", "gtin13", "gtin14", "productID", "@id"):
        v = d.get(k)
        if isinstance(v, (str, int)) and str(v):
            out.add(str(v).strip().lower())
    u = d.get("url")
    if isinstance(u, str):
        out |= variant_ids(u)
    return out


def _offer_price(off: dict) -> tuple[float | None, float | None, str | None]:
    price = parse_price(off.get("price") or off.get("lowPrice"))
    list_price = None
    cur = off.get("priceCurrency")
    specs = off.get("priceSpecification") or []
    if isinstance(specs, dict):
        specs = [specs]
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        ptype = str(spec.get("priceType", "")).split("/")[-1]
        sp = parse_price(spec.get("price"))
        if ptype in ("StrikethroughPrice", "ListPrice", "MSRP", "SRP") and sp:
            list_price = list_price or sp
        elif sp and not price:
            price = sp
        cur = cur or spec.get("priceCurrency")
    return price, list_price, cur


def _apply_offer(p: Product, off: dict) -> None:
    price, list_price, cur = _offer_price(off)
    if price and p.price is None:
        p.price = price
        p.list_price = p.list_price or list_price
    p.currency = p.currency or cur
    avail = str(off.get("availability", ""))
    if avail and p.in_stock is None:
        p.in_stock = "InStock" in avail or "OnlineOnly" in avail or "LimitedAvailability" in avail


def _from_jsonld(soup: BeautifulSoup, p: Product) -> None:
    products: list[dict] = []
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
            if {"Product", "ProductGroup", "IndividualProduct", "ProductModel"} & _types(o):
                products.append(o)
    if not products:
        return

    # Pick the version the URL points at; otherwise the first product/offer (not the cheapest).
    wanted = variant_ids(p.url)
    chosen_prod, chosen_offer = None, None
    if wanted:
        for prod in products:
            offers = _offer_list(prod.get("offers"))
            if wanted & _ids_of(prod):
                chosen_prod = prod
                chosen_offer = offers[0] if offers else None
                break
            for off in offers:
                if wanted & _ids_of(off):
                    chosen_prod, chosen_offer = prod, off
                    break
            if chosen_prod:
                break
    if chosen_prod is None:
        for prod in products:
            offers = _offer_list(prod.get("offers"))
            if offers:
                chosen_prod, chosen_offer = prod, offers[0]
                break
    group = next((x for x in products if "ProductGroup" in _types(x)), None)
    ordered = [x for x in (chosen_prod, group) if x] + [x for x in products if x is not chosen_prod and x is not group]

    for o in ordered:
        p.name = p.name or (o.get("name") if isinstance(o.get("name"), str) else None)
        p.image = p.image or _first_image(o.get("image"), p.url)
        brand = o.get("brand") or o.get("manufacturer")
        if isinstance(brand, list):
            brand = brand[0] if brand else None
        if isinstance(brand, dict):
            brand = brand.get("name")
        if isinstance(brand, str) and brand.strip():
            p.brand = p.brand or brand.strip()
        if o is chosen_prod or o is group:
            for k in ("gtin12", "gtin13", "gtin", "gtin14", "gtin8", "upc", "isbn"):
                if o.get(k):
                    p.gtin = p.gtin or str(o[k])
            if o.get("mpn"):
                p.mpn = p.mpn or str(o["mpn"])
            if o.get("sku"):
                p.extra_ids.append(str(o["sku"]))
            if o.get("model") and isinstance(o["model"], str):
                p.extra_ids.append(o["model"])
    if chosen_offer:
        _apply_offer(p, chosen_offer)
        for k in ("gtin12", "gtin13", "gtin", "gtin14", "sku", "mpn"):
            if chosen_offer.get(k) and k.startswith("gtin"):
                p.gtin = p.gtin or str(chosen_offer[k])
        if isinstance(chosen_offer.get("name"), str) and chosen_offer["name"] and len(products) == 1 \
                and len(_offer_list(chosen_prod.get("offers"))) > 1:
            p.variant = chosen_offer["name"]

    # Remember every version so the user can pick one (e.g. vinyl vs CD)
    for prod in products:
        offers = _offer_list(prod.get("offers"))
        for off in offers:
            price, lp, _ = _offer_price(off)
            if price is None:
                continue
            title = off.get("name") or prod.get("name")
            key = off.get("sku") or (prod.get("sku") if len(offers) == 1 else None) or title
            avail = str(off.get("availability", ""))
            p.variants.append({
                "key": str(key) if key else None, "title": title if isinstance(title, str) else None,
                "price": price, "list_price": lp,
                "gtin": next((str(x.get(k)) for x in (off, prod) for k in ("gtin12", "gtin13", "gtin", "gtin14")
                              if x.get(k)), None),
                "url": off.get("url") or prod.get("url"), "image": _first_image(prod.get("image"), p.url),
                "in_stock": ("InStock" in avail or "OnlineOnly" in avail or "LimitedAvailability" in avail) if avail else None,
            })
            if off is chosen_offer and key:
                p.variant_key = str(key)


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
    # Selected format (Vinyl / CD / Paperback ...) for music, books and movies
    fmt = _text(soup.select_one("#tmmSwatches li.selected .slot-title, #tmmSwatches .swatchElement.selected .a-button-text span,"
                                " #tmm-grid-swatch-container .selected .slot-title, .swatch-list-item-text.selected"))
    fmt = fmt or d("format", "binding", "media format")
    if fmt:
        p.variant = re.sub(r"\s*\$.*$", "", fmt).strip() or None
    avail = _text(soup.select_one("#availability")).lower()
    if avail:
        p.in_stock = not ("unavailable" in avail or "out of stock" in avail)


TARGET_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"  # Target's public web key (also read from the page)


def _unescape_json_blob(html: str) -> str:
    """Target embeds its data as JSON inside a JS string, so quotes appear as \\" — flatten that."""
    return re.sub(r'\\+"', '"', html)


def _from_embedded_json(html: str, p: Product) -> None:
    """Prices some stores only ship inside embedded JSON (Target, and others built the same way)."""
    flat = _unescape_json_blob(html)

    def num(*keys):
        for k in keys:
            m = re.search(rf'"{k}"\s*:\s*"?\$?([\d,]+\.?\d*)', flat)
            if m and parse_price(m.group(1)):
                return parse_price(m.group(1))
        return None

    if p.price is None:
        p.price = num("current_retail", "current_retail_min", "formatted_current_price")
        if p.price:
            p.currency = p.currency or "USD"
    if p.price and not p.list_price:
        reg = num("reg_retail", "reg_retail_max", "formatted_comparison_price")
        if reg and reg > p.price:
            p.list_price = reg
    if not p.gtin:
        m = re.search(r'"primary_barcode"\s*:\s*"(\d{8,14})"', flat) or re.search(r'"upc"\s*:\s*"(\d{8,14})"', flat)
        if m:
            p.gtin = m.group(1)


def target_tcin(url: str) -> str | None:
    m = re.search(r"[?&]preselect=(\d{6,10})", url) or re.search(r"/A-(\d{6,10})", url)
    return m.group(1) if m else None


def _target_api(p: Product, html: str) -> None:
    """Ask Target's own product API (the one its page uses) for price, title, image and UPC."""
    tcin = target_tcin(p.url)
    if not tcin:
        return
    flat = _unescape_json_blob(html or "")
    m = re.search(r'"apiKey"\s*:\s*"([0-9a-f]{40})"', flat)
    key = m.group(1) if m else TARGET_KEY
    store = re.search(r'"store_id"\s*:\s*"?(\d{3,5})', flat)
    params = {"key": key, "tcin": tcin, "pricing_store_id": store.group(1) if store else "3991",
              "is_bot": "false", "channel": "WEB"}
    try:
        from curl_cffi import requests as creq

        r = creq.get("https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1", params=params,
                     impersonate="chrome", timeout=TIMEOUT, headers={"Accept": "application/json",
                                                                     "Origin": "https://www.target.com",
                                                                     "Referer": p.url})
        if r.status_code != 200:
            log.info("target api %s -> %s", tcin, r.status_code)
            return
        prod = (r.json().get("data") or {}).get("product") or {}
    except Exception as e:
        log.info("target api failed: %s", e)
        return
    price = prod.get("price") or {}
    if not price and prod.get("children"):
        price = (prod["children"][0] or {}).get("price") or {}
    cur = parse_price(price.get("current_retail") or price.get("current_retail_min")
                      or price.get("formatted_current_price"))
    reg = parse_price(price.get("reg_retail") or price.get("reg_retail_max") or price.get("formatted_comparison_price"))
    if cur:
        p.price, p.currency = cur, "USD"
        p.list_price = reg if reg and reg > cur else p.list_price
    item = prod.get("item") or {}
    title = (item.get("product_description") or {}).get("title")
    if title:
        from html import unescape
        p.name = unescape(title)
    img = ((item.get("enrichment") or {}).get("images") or {}).get("primary_image_url")
    p.image = img or p.image
    p.gtin = p.gtin or item.get("primary_barcode")
    brand = ((item.get("primary_brand") or {}).get("name"))
    p.brand = p.brand or brand


def select_variant(p: Product, key: str | None) -> bool:
    """Switch p to the version with this key. Returns True if it exists on the page."""
    if not key:
        return False
    pick = next((v for v in p.variants if v.get("key") == key), None)
    if pick is None:
        return False
    p.price, p.list_price = pick["price"], pick.get("list_price")
    p.gtin = pick.get("gtin") or p.gtin
    if pick.get("title") and pick["title"] != p.name:
        p.variant = pick["title"]
    if pick.get("image"):
        p.image = pick["image"]
    if pick.get("url"):
        p.url = urljoin(p.url, pick["url"])
    if pick.get("in_stock") is not None:
        p.in_stock = pick["in_stock"]
    p.variant_key = key
    return True


def _shopify(p: Product) -> None:
    """Shopify stores (most band / record shops) publish every version at /products/<handle>.js."""
    pu = urlparse(p.url)
    m = re.search(r"(/products/[^/?#]+)", pu.path)
    if not m:
        return
    try:
        from curl_cffi import requests as creq

        r = creq.get(f"{pu.scheme}://{pu.netloc}{m.group(1)}.js", impersonate="chrome", timeout=TIMEOUT)
        if r.status_code != 200:
            return
        data = r.json()
    except Exception as e:
        log.info("shopify lookup failed: %s", e)
        return
    variants = data.get("variants") or []
    if not variants:
        return
    base_img = data.get("featured_image")
    if isinstance(base_img, str) and base_img.startswith("//"):
        base_img = "https:" + base_img
    p.variants = []
    for v in variants:
        img = (v.get("featured_image") or {}).get("src") if isinstance(v.get("featured_image"), dict) else None
        if isinstance(img, str) and img.startswith("//"):
            img = "https:" + img
        price = (v.get("price") or 0) / 100 or None
        cap = (v.get("compare_at_price") or 0) / 100 or None
        title = v.get("public_title") or v.get("title")
        p.variants.append({
            "id": str(v.get("id")), "key": str(v.get("id")), "title": None if title in (None, "Default Title") else title,
            "price": price, "list_price": cap if cap and price and cap > price else None,
            "gtin": v.get("barcode") or None, "url": f"{m.group(1)}?variant={v.get('id')}",
            "image": img or base_img, "in_stock": v.get("available"),
        })
    wanted = variant_ids(p.url)
    pick = next((v for v in p.variants if v["id"] in wanted), None) \
        or next((v for v in p.variants if v["in_stock"]), None) or p.variants[0]
    p.name = data.get("title") or p.name
    p.brand = p.brand or data.get("vendor")
    p.price, p.list_price, p.currency = pick["price"], pick["list_price"], p.currency or "USD"
    p.in_stock = pick["in_stock"]
    p.gtin = pick["gtin"] or p.gtin
    p.image = pick["image"] or p.image
    p.variant = pick["title"]
    p.variant_key = pick["key"]
    if len(p.variants) > 1 and pick["id"] not in wanted:
        p.url = urljoin(p.url, pick["url"])


def enrich(p: Product, html: str) -> Product:
    """Extra, store-specific steps for pages whose price isn't in the HTML."""
    if "/products/" in p.url and ("cdn.shopify.com" in html or "Shopify.shop" in html or "shopify" in html[:5000].lower()):
        _shopify(p)
    if "target.com" in p.store and (p.price is None or not p.gtin):
        _target_api(p, html)
        if p.price is None:
            _from_embedded_json(html, p)
    return p


def parse(html: str, url: str) -> Product:
    soup = BeautifulSoup(html, "lxml")
    p = Product(url=url, store=store_name(url))
    if is_amazon_url(url):
        _from_amazon(soup, p)
    _from_jsonld(soup, p)
    _from_meta(soup, p)
    if p.name:
        p.name = re.sub(r"\s+", " ", p.name).strip()
        root = re.sub(r"\.(com|net|org|co\.uk|ca)$", "", p.store).split(".")[-1]
        p.name = re.sub(rf"\s*[:|\-–]\s*{re.escape(root)}(\.com)?\s*$", "", p.name, flags=re.I) or p.name
        # Strip trailing " | Store Name" / " - Store" suffixes from page titles
        p.name = re.sub(r"\s+[|\-–]\s+[^|\-–]{2,30}$", "", p.name) if len(p.name) > 40 else p.name
    if p.list_price and p.price and p.list_price <= p.price:
        p.list_price = None
    if p.currency is None and p.price is not None and ("$" in html[:200000]):
        p.currency = "USD"
    return p


def display_name(p: Product) -> str | None:
    """Product name plus the version, e.g. "Album Title – Vinyl LP", so CD and vinyl don't look identical."""
    if not p.name:
        return None
    v = (p.variant or "").strip()
    if v and v.lower() not in p.name.lower():
        return f"{p.name} – {v}"
    return p.name


def scrape(url: str, variant_key: str | None = None) -> Product:
    if is_amazon_url(url):
        url = clean_amazon_url(url)
    final, html = fetch(url)
    if is_amazon_url(final):
        final = clean_amazon_url(final)
    elif variant_ids(url) and not variant_ids(final):
        final = url  # keep ?variant= etc. if the store redirected it away
    p = enrich(parse(html, final), html)
    select_variant(p, variant_key)  # the version the user picked wins over the page's default
    return p
