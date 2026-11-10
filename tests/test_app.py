import os, pathlib, tempfile
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["REFRESH_HOURS"] = "0"
os.environ["ADMIN_PASSWORD"] = "pw"

import pytest
from app import db, scraper, service

FX = pathlib.Path(__file__).parent / "fixtures"
PAGES = {
    "https://www.amazon.com/dp/B09XS7JWHH": "amazon.html",
    "https://www.bestbuy.com/site/sony-xm5/6505727.p": "bestbuy.html",
    "https://electronics.sony.com/audio/headphones/wh1000xm5": "sony.html",
    "https://www.rtings.com/headphones/reviews/sony/wh-1000xm5": "review.html",
    "https://blog.example.com/xm5-thoughts": "review.html",
}

def fake_fetch(url):
    if url not in PAGES:
        raise scraper.FetchError("nope")
    return url, (FX / PAGES[url]).read_text()

def test_price_parsing():
    assert scraper.parse_price("$1,299.99") == 1299.99
    assert scraper.parse_price("1.299,00 €") == 1299.0
    assert scraper.parse_price("19,99") == 19.99
    assert scraper.parse_price("0") is None

def test_amazon_url_cleanup():
    assert scraper.clean_amazon_url("https://www.amazon.com/Sony-WH-1000XM5/dp/B09XS7JWHH/ref=sr_1_3?th=1") == "https://www.amazon.com/dp/B09XS7JWHH"
    assert scraper.is_amazon_url("https://amzn.to/abc") and not scraper.is_amazon_url("https://notamazon.com/x")

def test_parse_amazon():
    p = scraper.parse((FX / "amazon.html").read_text(), "https://www.amazon.com/dp/B09XS7JWHH")
    assert p.name.startswith("Sony WH-1000XM5 Wireless")
    assert p.image.endswith("big.jpg")
    assert (p.price, p.list_price) == (328.0, 399.99)
    assert p.brand == "Sony" and p.mpn == "WH1000XM5/B" and p.gtin == "027242923232"
    assert p.in_stock is True

def test_parse_jsonld_and_meta():
    p = scraper.parse((FX / "bestbuy.html").read_text(), "https://www.bestbuy.com/site/x.p")
    assert (p.price, p.list_price, p.gtin, p.brand) == (299.99, 399.99, "0027242923232", "Sony")
    s = scraper.parse((FX / "sony.html").read_text(), "https://electronics.sony.com/x")
    assert s.price == 399.99 and s.image == "https://electronics.sony.com/img/xm5.png"
    e = scraper.parse((FX / "target_eu.html").read_text(), "https://x.test/p")
    assert (e.name, e.price, e.currency, e.image) == ("Lego Set 10497", 1299.0, "EUR", "https://x.test/lego.jpg")

def test_sale_detection_from_history():
    db.init()
    iid = db.insert_item({"source_url": "u", "track_url": "u", "price": 100.0})
    for p in (100, 100, 100):
        db.add_price(iid, p, None)
    assert not db.sale_info(db.get_item(iid))["on_sale"]
    db.update_item(iid, {"price": 80.0}); db.add_price(iid, 80, None)
    s = db.sale_info(db.get_item(iid))
    assert s["on_sale"] and s["was_price"] == 100 and s["percent_off"] == 20

def test_blocked_detection():
    assert scraper._looks_blocked("https://www.amazon.com/dp/X", 200, "<form action='/errors/validateCaptcha'>")
    assert scraper._looks_blocked("https://www.amazon.com/dp/X", 503, "")
    assert not scraper._looks_blocked("https://www.amazon.com/dp/X", 200, "<span id='productTitle'>x</span> captcha")
    assert not scraper._looks_blocked("https://bestbuy.com/x", 200, "captcha")

def test_readonly_db_gives_readable_error(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    import sqlite3
    def boom(*a, **k): raise sqlite3.OperationalError("attempt to write a readonly database")
    monkeypatch.setattr(service, "add_item", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/api/login", json={"password": "pw"})
        r = c.post("/api/items", json={"url": "https://x.com"})
        assert r.status_code == 500 and "data folder" in r.json()["detail"]

def test_target_embedded_and_api(monkeypatch):
    html = (FX / "target.html").read_text()
    url = "https://www.target.com/p/lego-orchid/-/A-83829876"
    assert scraper.target_tcin(url) == "83829876"
    # API unreachable -> embedded data fallback
    monkeypatch.setattr(scraper, "_target_api", lambda p, h: None)
    p = scraper.enrich(scraper.parse(html, url), html)
    assert (p.price, p.list_price, p.gtin) == (39.99, 49.99, "673419355889")
    assert p.name.startswith("LEGO Icons Orchid")

def test_target_api_response(monkeypatch):
    import curl_cffi.requests as creq
    seen = {}
    class R:
        status_code = 200
        def json(self):
            return {"data": {"product": {"price": {"current_retail": 34.99, "reg_retail": 49.99},
                    "item": {"product_description": {"title": "LEGO Icons Orchid &#38; Vase"}, "primary_barcode": "673419355889",
                             "enrichment": {"images": {"primary_image_url": "https://target.scene7.com/orchid.jpg"}},
                             "primary_brand": {"name": "LEGO"}}}}}
    def get(url, params=None, **k):
        seen.update(params); return R()
    monkeypatch.setattr(creq, "get", get)
    html = (FX / "target.html").read_text()
    p = scraper.Product(url="https://www.target.com/p/x/-/A-83829876", store="target.com")
    scraper._target_api(p, html)
    assert seen["key"] == "0123456789abcdef0123456789abcdef01234567" and seen["tcin"] == "83829876"
    assert (p.price, p.list_price, p.name, p.brand) == (34.99, 49.99, "LEGO Icons Orchid & Vase", "LEGO")

def test_api_end_to_end(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(scraper, "fetch", fake_fetch)
    monkeypatch.setattr(service, "_save_image", lambda i, u: u)
    with TestClient(app) as c:
        assert c.post("/api/items", json={"url": "x"}).status_code == 401
        assert c.post("/api/login", json={"password": "pw"}).status_code == 200
        it = c.post("/api/items", json={"url": "https://www.amazon.com/Sony/dp/B09XS7JWHH/ref=x"}).json()
        assert it["store"] == "amazon.com" and it["price"] == 328.0      # Amazon link is used as-is
        assert it["on_sale"] and it["was_price"] == 399.99
        bb = c.post("/api/items", json={"url": "https://www.bestbuy.com/site/sony-xm5/6505727.p"}).json()
        assert bb["store"] == "bestbuy.com" and bb["percent_off"] == 25
        bad = c.post("/api/items", json={"url": "https://shop.example.com/thing"}).json()
        assert bad["last_error"]
        bad = c.patch(f"/api/items/{bad['id']}", json={"name": "Hand-made scarf", "price": 45}).json()
        assert bad["name"] == "Hand-made scarf" and bad["price"] == 45
        assert c.post(f"/api/items/{it['id']}/rematch").status_code in (404, 405)
        c.cookies.clear()
        assert len(c.get("/api/items").json()) >= 3   # guests can view
        assert c.get("/").status_code == 200

def test_amazon_blocked_adds_placeholder(monkeypatch):
    def fetch(url):
        raise scraper.BlockedError("Amazon showed a bot check (captcha).", url)
    monkeypatch.setattr(scraper, "fetch", fetch)
    it = service.add_item("https://www.amazon.com/Sony/dp/B09XS7JWHH?ref=abc")
    assert it["track_url"] == "https://www.amazon.com/dp/B09XS7JWHH" and "bot check" in it["last_error"]

def test_multi_version_page(monkeypatch):
    monkeypatch.setattr(scraper, "_shopify", lambda p: None)  # use the JSON-LD path
    html = (FX / "band_store.html").read_text()
    base = "https://shop.lanterns.test/products/midnight-garden"
    lp = scraper.enrich(scraper.parse(html, base + "?variant=222"), html)
    cd = scraper.enrich(scraper.parse(html, base + "?variant=111"), html)
    assert (lp.price, scraper.display_name(lp), lp.variant_key) == (28.0, "Midnight Garden – Vinyl LP", "LAN-LP")
    assert (cd.price, scraper.display_name(cd), cd.variant_key) == (12.0, "Midnight Garden – CD", "LAN-CD")
    plain = scraper.parse(html, base)
    assert plain.price == 12.0 and len(plain.variants) == 2
    assert scraper.select_variant(plain, "LAN-LP") and plain.price == 28.0 and plain.url.endswith("variant=222")
    assert not scraper.select_variant(plain, "nope")

SHOPIFY = {"title": "Screwdriver", "vendor": "LTT", "featured_image": "//cdn.shopify.com/a.jpg", "variants": [
    {"id": 111, "public_title": "Black", "price": 6999, "compare_at_price": None, "available": False, "barcode": "111111111111"},
    {"id": 222, "public_title": "Orange", "price": 5999, "compare_at_price": 6999, "available": True, "barcode": "222222222222",
     "featured_image": {"src": "//cdn.shopify.com/orange.jpg"}}]}

def test_shopify_version_picker_end_to_end(monkeypatch):
    import curl_cffi.requests as creq
    from fastapi.testclient import TestClient
    from app.main import app
    class R:
        status_code = 200
        def json(self): return SHOPIFY
    monkeypatch.setattr(creq, "get", lambda url, **k: R())
    monkeypatch.setattr(scraper, "fetch", lambda url: (url, '<html><script src="https://cdn.shopify.com/x.js"></script><title>Screwdriver</title></html>'))
    monkeypatch.setattr(service, "_save_image", lambda i, u: u)
    base = "https://www.lttstore.com/products/screwdriver"
    with TestClient(app) as c:
        c.post("/api/login", json={"password": "pw"})
        # link without a version: first in-stock version, and the choice is remembered
        a = c.post("/api/items", json={"url": base}).json()
        assert (a["name"], a["price"], a["variant_key"]) == ("Screwdriver – Orange", 59.99, "222")
        assert a["on_sale"] and [v["title"] for v in a["variants"]] == ["Black", "Orange"]
        # link with a version
        b = c.post("/api/items", json={"url": base + "?variant=111"}).json()
        assert (b["name"], b["price"], b["in_stock"]) == ("Screwdriver – Black", 69.99, 0)
        # pick another version in the edit screen
        a2 = c.patch(f"/api/items/{a['id']}", json={"variant_key": "111"}).json()
        assert (a2["name"], a2["price"], a2["variant_key"]) == ("Screwdriver – Black", 69.99, "111")
        assert a2["track_url"].endswith("?variant=111") and not a2["on_sale"]
        # a later price check keeps the picked version even though Orange is the in-stock default
        a3 = c.post(f"/api/items/{a['id']}/refresh").json()
        assert (a3["price"], a3["variant_key"]) == (69.99, "111")

def test_old_database_is_upgraded(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "giftlist.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT NOT NULL, track_url TEXT NOT NULL, name TEXT, price REAL, purchased INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0, priority INTEGER DEFAULT 0, created_at TEXT)")
    c.execute("INSERT INTO items (source_url, track_url, name) VALUES ('u','u','Old item')"); c.commit(); c.close()
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init()
    it = db.all_items()[0]
    assert it["name"] == "Old item" and "variant_key" in it and "variants" in it
