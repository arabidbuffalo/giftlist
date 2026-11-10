from __future__ import annotations

import os
import sqlite3
import statistics
import threading
from datetime import datetime, timedelta, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "giftlist.db")
SALE_DROP_PCT = float(os.environ.get("SALE_DROP_PERCENT", "5"))

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,          -- what the user pasted
    amazon_url TEXT,                   -- set when the source was Amazon
    track_url TEXT NOT NULL,           -- the listing whose price we track
    store TEXT,
    name TEXT,
    image TEXT,
    price REAL,
    list_price REAL,
    currency TEXT,
    brand TEXT,
    gtin TEXT,
    mpn TEXT,
    in_stock INTEGER,
    notes TEXT DEFAULT '',
    priority INTEGER DEFAULT 0,
    purchased INTEGER DEFAULT 0,
    name_locked INTEGER DEFAULT 0,     -- user edited the name; don't overwrite on refresh
    match_status TEXT,                 -- (unused; kept for older databases)
    variant_key TEXT,                  -- which version of the page is tracked
    variants TEXT,                     -- JSON list of the versions the page sells
    last_checked TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    price REAL NOT NULL,
    list_price REAL,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist_item ON price_history(item_id, checked_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init() -> None:
    with _lock, conn() as c:
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
        for col in ("variant_key", "variants"):
            if col not in have:  # database created by an older version
                c.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")


def insert_item(fields: dict) -> int:
    fields = {**fields, "created_at": now()}
    cols = ",".join(fields)
    qs = ",".join("?" for _ in fields)
    with _lock, conn() as c:
        cur = c.execute(f"INSERT INTO items ({cols}) VALUES ({qs})", list(fields.values()))
        return cur.lastrowid


def update_item(item_id: int, fields: dict) -> None:
    if not fields:
        return
    sets = ",".join(f"{k}=?" for k in fields)
    with _lock, conn() as c:
        c.execute(f"UPDATE items SET {sets} WHERE id=?", [*fields.values(), item_id])


def delete_item(item_id: int) -> None:
    with _lock, conn() as c:
        c.execute("DELETE FROM items WHERE id=?", (item_id,))


def get_item(item_id: int) -> dict | None:
    with _lock, conn() as c:
        r = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return dict(r) if r else None


def all_items() -> list[dict]:
    with _lock, conn() as c:
        rows = c.execute("SELECT * FROM items ORDER BY purchased, sort_order, priority DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def add_price(item_id: int, price: float, list_price: float | None) -> None:
    with _lock, conn() as c:
        c.execute(
            "INSERT INTO price_history (item_id, price, list_price, checked_at) VALUES (?,?,?,?)",
            (item_id, price, list_price, now()),
        )


def history(item_id: int, days: int = 120) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _lock, conn() as c:
        rows = c.execute(
            "SELECT price, list_price, checked_at FROM price_history WHERE item_id=? AND checked_at>=? ORDER BY checked_at",
            (item_id, since),
        ).fetchall()
    return [dict(r) for r in rows]


def sale_info(item: dict, hist: list[dict] | None = None) -> dict:
    """Decide whether an item is on sale.

    On sale if either:
      * the store itself shows a higher "was"/list price, or
      * the current price is SALE_DROP_PERCENT% or more below its usual price
        (the median of the last 60 days of checks, needs at least 2 earlier checks).
    """
    price = item.get("price")
    hist = hist if hist is not None else history(item["id"], 60)
    out = {"on_sale": False, "was_price": None, "percent_off": 0, "reason": None,
           "lowest_price": min([h["price"] for h in hist], default=price)}
    if not price:
        return out
    lp = item.get("list_price")
    if lp and lp > price * 1.01:
        out.update(on_sale=True, was_price=lp, reason="store")
    else:
        earlier = [h["price"] for h in hist[:-1]] if len(hist) > 1 else []
        if len(earlier) >= 2:
            usual = statistics.median(earlier)
            if price <= usual * (1 - SALE_DROP_PCT / 100):
                out.update(on_sale=True, was_price=round(usual, 2), reason="drop")
    if out["on_sale"]:
        out["percent_off"] = round((1 - price / out["was_price"]) * 100)
    return out
