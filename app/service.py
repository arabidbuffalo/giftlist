"""Add / refresh logic shared by the API and the scheduler."""
from __future__ import annotations

import logging
import os
import random
import time

from . import db, matcher, scraper
from .scraper import BlockedError, FetchError, Product

log = logging.getLogger(__name__)

MATCH_ENABLED = os.environ.get("MATCH_AMAZON", "true").lower() in ("1", "true", "yes")
IMG_DIR = os.path.join(db.DATA_DIR, "images")


def _save_image(item_id: int, url: str | None) -> str | None:
    """Keep a local copy so the list still shows pictures if a store blocks hotlinking."""
    if not url or url.startswith("/images/"):
        return url
    try:
        from curl_cffi import requests as creq

        r = creq.get(url, impersonate="chrome", timeout=20)
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) < 500:
            return url
        ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}.get(
            ctype.split(";")[0], "jpg")
        os.makedirs(IMG_DIR, exist_ok=True)
        for old in os.listdir(IMG_DIR):
            if old.startswith(f"{item_id}."):
                os.remove(os.path.join(IMG_DIR, old))
        fname = f"{item_id}.{ext}"
        with open(os.path.join(IMG_DIR, fname), "wb") as f:
            f.write(r.content)
        return f"/images/{fname}?v={int(time.time())}"
    except Exception as e:
        log.info("image download failed: %s", e)
        return url


def _fields_from(p: Product) -> dict:
    return {
        "track_url": p.url,
        "store": p.store,
        "price": p.price,
        "list_price": p.list_price,
        "currency": p.currency or "USD",
        "in_stock": None if p.in_stock is None else int(p.in_stock),
    }


def add_item(url: str, notes: str = "", try_match: bool = True) -> dict:
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    try:
        src = scraper.scrape(url)
    except BlockedError as e:
        # Amazon refused us. Find the product name through search and look for it elsewhere.
        amz = scraper.clean_amazon_url(e.final_url or url)
        found = _amazon_fallback(amz, url, notes, try_match)
        if found:
            return found
        item_id = db.insert_item({
            "source_url": url, "track_url": amz, "store": scraper.store_name(amz),
            "name": "Amazon item (couldn't read the page)", "notes": notes,
            "last_error": "Amazon blocked the request. Edit the name and price, or tap Check price later.",
            "amazon_url": amz, "match_status": "not_found", "last_checked": db.now(),
        })
        return db.get_item(item_id)
    except FetchError as e:
        item_id = db.insert_item({
            "source_url": url, "track_url": url, "store": scraper.store_name(url),
            "name": scraper.store_name(url), "notes": notes, "last_error": str(e),
            "amazon_url": url if scraper.is_amazon_url(url) else None, "match_status": "manual",
            "last_checked": db.now(),
        })
        return db.get_item(item_id)

    chosen = src
    status = "not_amazon"
    if src.is_amazon:
        status = "not_found"
        if MATCH_ENABLED and try_match:
            alt = matcher.find_alternative(src)
            if alt:
                chosen, status = alt, "matched"

    fields = _fields_from(chosen)
    fields.update({
        "source_url": url,
        "amazon_url": src.url if src.is_amazon else None,
        "name": chosen.name or src.name or chosen.store,
        "image": chosen.image or src.image,
        "brand": src.brand or chosen.brand,
        "gtin": src.gtin or chosen.gtin,
        "mpn": src.mpn or chosen.mpn,
        "notes": notes,
        "match_status": status,
        "last_checked": db.now(),
        "last_error": None if chosen.price else "Couldn't find a price on the page — you can enter it by hand.",
    })
    item_id = db.insert_item(fields)
    if chosen.price:
        db.add_price(item_id, chosen.price, chosen.list_price)
    img = _save_image(item_id, fields["image"])
    if img != fields["image"]:
        db.update_item(item_id, {"image": img})
    return db.get_item(item_id)


def _amazon_fallback(amz_url: str, source_url: str, notes: str, try_match: bool) -> dict | None:
    asin = scraper.amazon_asin(amz_url)
    title = matcher.title_from_search(asin) if asin else None
    if not title:
        return None
    src = matcher.product_from_title(amz_url, title)
    alt = matcher.find_alternative(src) if (MATCH_ENABLED and try_match) else None
    chosen = alt or src
    fields = _fields_from(chosen)
    fields.update({
        "source_url": source_url, "amazon_url": amz_url,
        "name": chosen.name or title, "image": chosen.image,
        "brand": (alt and alt.brand) or src.brand, "gtin": alt.gtin if alt else None,
        "mpn": (alt and alt.mpn) or src.mpn, "notes": notes,
        "match_status": "matched" if alt else "not_found", "last_checked": db.now(),
        "last_error": None if chosen.price else
            "Amazon blocked the price check and no other store was found. Enter the price by hand or try Check price later.",
    })
    item_id = db.insert_item(fields)
    if chosen.price:
        db.add_price(item_id, chosen.price, chosen.list_price)
    img = _save_image(item_id, fields["image"])
    if img != fields["image"]:
        db.update_item(item_id, {"image": img})
    return db.get_item(item_id)


def refresh_item(item_id: int) -> dict | None:
    item = db.get_item(item_id)
    if not item:
        return None
    try:
        p = scraper.scrape(item["track_url"])
    except FetchError as e:
        db.update_item(item_id, {"last_error": str(e), "last_checked": db.now()})
        return db.get_item(item_id)
    upd = _fields_from(p)
    upd["track_url"] = item["track_url"]  # keep the URL the user chose, not a redirect variant
    if p.price is None:
        upd.pop("price"); upd.pop("list_price")
        upd["last_error"] = "Couldn't read the price this time; showing the last known price."
    else:
        upd["last_error"] = None
        db.add_price(item_id, p.price, p.list_price)
    if not item["name_locked"] and p.name:
        upd["name"] = p.name
    if not item["image"] and p.image:
        upd["image"] = _save_image(item_id, p.image)
    upd["last_checked"] = db.now()
    db.update_item(item_id, upd)
    return db.get_item(item_id)


def rematch(item_id: int) -> dict | None:
    """Look again for a non-Amazon listing of an Amazon item."""
    item = db.get_item(item_id)
    if not item or not item["amazon_url"]:
        return item
    try:
        src = scraper.scrape(item["amazon_url"])
    except FetchError as e:
        db.update_item(item_id, {"last_error": str(e)})
        return db.get_item(item_id)
    src.gtin = src.gtin or item["gtin"]
    src.mpn = src.mpn or item["mpn"]
    src.brand = src.brand or item["brand"]
    alt = matcher.find_alternative(src)
    chosen = alt or src
    upd = _fields_from(chosen)
    upd.update({"match_status": "matched" if alt else "not_found", "last_checked": db.now(),
                "last_error": None, "gtin": src.gtin, "mpn": src.mpn, "brand": src.brand})
    if not item["name_locked"]:
        upd["name"] = chosen.name or item["name"]
    db.update_item(item_id, upd)
    if chosen.price:
        db.add_price(item_id, chosen.price, chosen.list_price)
    if chosen.image:
        db.update_item(item_id, {"image": _save_image(item_id, chosen.image)})
    return db.get_item(item_id)


def refresh_all() -> None:
    items = [i for i in db.all_items() if not i["purchased"]]
    log.info("refreshing %d items", len(items))
    for i, item in enumerate(items):
        try:
            refresh_item(item["id"])
        except Exception:
            log.exception("refresh failed for item %s", item["id"])
        if i < len(items) - 1:
            time.sleep(random.uniform(4, 15))  # be polite; also avoids tripping bot checks
