import os, pathlib, tempfile
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["REFRESH_HOURS"] = "0"
os.environ["ADMIN_PASSWORD"] = "pw"

import pytest
from app import db, matcher, scraper, service

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

def fake_search(q):
    return ["https://www.amazon.com/dp/B09XS7JWHH", "https://www.rtings.com/headphones/reviews/sony/wh-1000xm5",
            "https://blog.example.com/xm5-thoughts", "https://www.bestbuy.com/site/sony-xm5/6505727.p",
            "https://electronics.sony.com/audio/headphones/wh1000xm5"]

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

def test_matcher_prefers_official_site_and_rejects_junk():
    src = scraper.parse((FX / "amazon.html").read_text(), "https://www.amazon.com/dp/B09XS7JWHH")
    alt = matcher.find_alternative(src, fetch=fake_fetch, search_fn=fake_search)
    assert alt is not None
    assert alt.store == "electronics.sony.com"  # verified by model number + brand, boosted as official
    # Without the official site, Best Buy matches by UPC
    alt2 = matcher.find_alternative(src, fetch=fake_fetch, search_fn=lambda q: fake_search(q)[:4])
    assert alt2.store == "bestbuy.com" and alt2.price == 299.99

def test_matcher_none_found():
    src = scraper.parse((FX / "amazon.html").read_text(), "https://www.amazon.com/dp/B09XS7JWHH")
    assert matcher.find_alternative(src, fetch=fake_fetch, search_fn=lambda q: fake_search(q)[:3]) is None

def test_sale_detection_from_history():
    db.init()
    iid = db.insert_item({"source_url": "u", "track_url": "u", "price": 100.0})
    for p in (100, 100, 100):
        db.add_price(iid, p, None)
    assert not db.sale_info(db.get_item(iid))["on_sale"]
    db.update_item(iid, {"price": 80.0}); db.add_price(iid, 80, None)
    s = db.sale_info(db.get_item(iid))
    assert s["on_sale"] and s["was_price"] == 100 and s["percent_off"] == 20

def test_api_end_to_end(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(scraper, "fetch", fake_fetch)
    monkeypatch.setattr(matcher, "search", lambda q: fake_search(q)[:4])
    monkeypatch.setattr(service, "_save_image", lambda i, u: u)
    with TestClient(app) as c:
        assert c.post("/api/items", json={"url": "x"}).status_code == 401
        assert c.post("/api/login", json={"password": "pw"}).status_code == 200
        r = c.post("/api/items", json={"url": "https://www.amazon.com/Sony/dp/B09XS7JWHH/ref=x"})
        it = r.json()
        assert it["match_status"] == "matched" and it["store"] == "bestbuy.com"
        assert it["on_sale"] and it["was_price"] == 399.99 and it["percent_off"] == 25
        assert it["amazon_url"] == "https://www.amazon.com/dp/B09XS7JWHH"
        # switch to Amazon link
        it = c.patch(f"/api/items/{it['id']}", json={"use_amazon": True}).json()
        assert it["store"] == "amazon.com" and it["price"] == 328.0
        # unreachable link still gets added for manual editing
        bad = c.post("/api/items", json={"url": "https://shop.example.com/thing"}).json()
        assert bad["last_error"]
        bad = c.patch(f"/api/items/{bad['id']}", json={"name": "Hand-made scarf", "price": 45}).json()
        assert bad["name"] == "Hand-made scarf" and bad["price"] == 45
        c.cookies.clear()
        assert len(c.get("/api/items").json()) == 3   # guests can view
        assert c.get("/").status_code == 200

def test_guess_model_and_title_cleanup():
    assert matcher.guess_model("Sony WH-1000XM5 Wireless Headphones, 30 Hour Battery, 256GB") == "WH-1000XM5"
    assert matcher.guess_model("Cozy Blanket Queen Size") is None
    t = matcher.title_from_search("B09XS7JWHH", results_fn=lambda q: [
        ("https://www.reddit.com/r/x", "Reddit thread"),
        ("https://www.amazon.com/Sony-WH-1000XM5/dp/B09XS7JWHH",
         "Amazon.com: Sony WH-1000XM5 Wireless Industry Leading Noise Canceling Headphones : Electronics")])
    assert t == "Sony WH-1000XM5 Wireless Industry Leading Noise Canceling Headphones"

def test_blocked_detection():
    assert scraper._looks_blocked("https://www.amazon.com/dp/X", 200, "<form action='/errors/validateCaptcha'>")
    assert scraper._looks_blocked("https://www.amazon.com/dp/X", 503, "")
    assert not scraper._looks_blocked("https://www.amazon.com/dp/X", 200, "<span id='productTitle'>x</span> captcha")
    assert not scraper._looks_blocked("https://bestbuy.com/x", 200, "captcha")

def test_amazon_blocked_falls_back_to_search(monkeypatch):
    def fetch(url):
        if scraper.is_amazon_url(url):
            raise scraper.BlockedError("blocked", url)
        return fake_fetch(url)
    monkeypatch.setattr(scraper, "fetch", fetch)
    monkeypatch.setattr(matcher, "search", lambda q: fake_search(q)[:4])
    monkeypatch.setattr(matcher, "search_results", lambda q: [
        ("https://www.amazon.com/dp/B09XS7JWHH", "Amazon.com: Sony WH-1000XM5 Wireless Noise Canceling Headphones : Electronics")])
    monkeypatch.setattr(service, "_save_image", lambda i, u: u)
    it = service.add_item("https://www.amazon.com/Sony/dp/B09XS7JWHH?ref=abc")
    assert it["match_status"] == "matched" and it["store"] == "bestbuy.com" and it["price"] == 299.99
    # No search hits at all -> placeholder item instead of a crash
    monkeypatch.setattr(matcher, "search_results", lambda q: [])
    it = service.add_item("https://www.amazon.com/dp/B000000000")
    assert it["last_error"] and it["amazon_url"] == "https://www.amazon.com/dp/B000000000"

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
