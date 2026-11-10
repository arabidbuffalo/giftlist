"""Add / refresh logic shared by the API and the scheduler."""
from __future__ import annotations

import json
import logging
import os
import random
import time

from . import db, scraper
from .scraper import FetchError, Product

log = logging.getLogger(__name__)

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


def _variants_json(p: Product) -> str | None:
    """The versions a page sells (CD / Vinyl, colors, sizes), for the picker in the edit screen."""
    vs = [{"key": v["key"], "title": v["title"], "price": v["price"]}
          for v in p.variants if v.get("key") and v.get("title")]
    if len(vs) < 2 or len({v["title"] for v in vs}) < 2 or len({v["key"] for v in vs}) < len(vs):
        return None
    return json.dumps(vs[:100])


def _fields_from(p: Product) -> dict:
    return {
        "track_url": p.url,
        "store": p.store,
        "price": p.price,
        "list_price": p.list_price,
        "currency": p.currency or "USD",
        "in_stock": None if p.in_stock is None else int(p.in_stock),
        "variants": _variants_json(p),
        "variant_key": p.variant_key,
    }


def add_item(url: str, notes: str = "") -> dict:
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    amazon = scraper.is_amazon_url(url)
    try:
        src = scraper.scrape(url)
    except FetchError as e:
        track = scraper.clean_amazon_url(getattr(e, "final_url", "") or url) if amazon else url
        item_id = db.insert_item({
            "source_url": url, "track_url": track, "store": scraper.store_name(track),
            "name": "Amazon item (couldn't read the page)" if amazon else scraper.store_name(url),
            "notes": notes, "amazon_url": track if amazon else None, "last_checked": db.now(),
            "last_error": f"{e} Edit the name and price by hand, or tap Check price later.",
        })
        return db.get_item(item_id)

    fields = _fields_from(src)
    fields.update({
        "source_url": url,
        "amazon_url": src.url if src.is_amazon else None,
        "name": scraper.display_name(src) or src.store,
        "image": src.image,
        "brand": src.brand, "gtin": src.gtin, "mpn": src.mpn,
        "notes": notes,
        "last_checked": db.now(),
        "last_error": None if src.price else "Couldn't find a price on the page — you can enter it by hand.",
    })
    item_id = db.insert_item(fields)
    if src.price:
        db.add_price(item_id, src.price, src.list_price)
    img = _save_image(item_id, fields["image"])
    if img != fields["image"]:
        db.update_item(item_id, {"image": img})
    return db.get_item(item_id)


def refresh_item(item_id: int, adopt: bool = False) -> dict | None:
    """Re-read the tracked page. adopt=True also takes the page's link and picture (after a version change)."""
    item = db.get_item(item_id)
    if not item:
        return None
    try:
        p = scraper.scrape(item["track_url"], item.get("variant_key"))
    except FetchError as e:
        db.update_item(item_id, {"last_error": str(e), "last_checked": db.now()})
        return db.get_item(item_id)
    upd = _fields_from(p)
    if not adopt:
        upd["track_url"] = item["track_url"]  # keep the URL the user chose, not a redirect variant
    if upd["variants"] is None:
        upd.pop("variants")
    upd["variant_key"] = p.variant_key or item.get("variant_key")
    if p.price is None:
        upd.pop("price"); upd.pop("list_price")
        upd["last_error"] = "Couldn't read the price this time; showing the last known price."
    else:
        upd["last_error"] = None
        db.add_price(item_id, p.price, p.list_price)
    if not item["name_locked"] and p.name:
        upd["name"] = scraper.display_name(p)
    if p.image and (adopt or not item["image"]):
        upd["image"] = _save_image(item_id, p.image)
    upd["last_checked"] = db.now()
    db.update_item(item_id, upd)
    return db.get_item(item_id)


def set_variant(item_id: int, key: str) -> dict | None:
    """Track a specific version (e.g. Vinyl instead of CD) of a page that sells several."""
    db.update_item(item_id, {"variant_key": key})
    return refresh_item(item_id, adopt=True)


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
