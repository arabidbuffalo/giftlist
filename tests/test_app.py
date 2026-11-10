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
