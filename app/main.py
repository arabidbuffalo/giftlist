from __future__ import annotations

import hashlib
import json
import hmac
import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, service

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("giftlist")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET = (os.environ.get("SECRET_KEY") or ADMIN_PASSWORD or "giftlist").encode()
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "12"))
LIST_TITLE = os.environ.get("LIST_TITLE", "My Wishlist")
THEME = os.environ.get("THEME", "auto").lower()  # auto / christmas / birthday / default
HERE = os.path.dirname(__file__)

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    os.makedirs(service.IMG_DIR, exist_ok=True)
    if REFRESH_HOURS > 0:
        scheduler.add_job(service.refresh_all, "interval", hours=REFRESH_HOURS, jitter=900,
                          id="refresh", max_instances=1, coalesce=True)
        scheduler.start()
        log.info("price refresh every %s hours", REFRESH_HOURS)
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Gift List", lifespan=lifespan)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    msg = str(exc)
    if "readonly database" in msg or isinstance(exc, PermissionError):
        msg = "The app can't write to its data folder. Check that the folder's owner matches the user: line in your compose file."
    return JSONResponse({"detail": f"Something went wrong: {msg}"}, status_code=500)


# ---------------------------------------------------------------- auth

def _token() -> str:
    return hmac.new(SECRET, b"admin:" + ADMIN_PASSWORD.encode(), hashlib.sha256).hexdigest()


def is_admin(request: Request) -> bool:
    if not ADMIN_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get("gl_auth", ""), _token())


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(401, "Sign in to edit the list")


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    if not ADMIN_PASSWORD or not hmac.compare_digest(body.password, ADMIN_PASSWORD):
        raise HTTPException(401, "Wrong password")
    response.set_cookie("gl_auth", _token(), max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("gl_auth")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return {"admin": is_admin(request), "auth_enabled": bool(ADMIN_PASSWORD), "title": LIST_TITLE, "theme": THEME,
            "refresh_hours": REFRESH_HOURS}


# ---------------------------------------------------------------- items

def _present(item: dict) -> dict:
    hist = db.history(item["id"], 60)
    try:
        item = {**item, "variants": json.loads(item.get("variants") or "[]")}
    except ValueError:
        item = {**item, "variants": []}
    return {**item, **db.sale_info(item, hist),
            "history": [{"p": h["price"], "t": h["checked_at"]} for h in hist[-30:]]}


@app.get("/api/items")
def list_items():
    return [_present(i) for i in db.all_items()]


class NewItem(BaseModel):
    url: str
    notes: str = ""


@app.post("/api/items", dependencies=[Depends(require_admin)])
def add_item(body: NewItem):
    if not body.url.strip():
        raise HTTPException(400, "Paste a product link")
    return _present(service.add_item(body.url, body.notes))


class ItemPatch(BaseModel):
    name: str | None = None
    notes: str | None = None
    price: float | None = None
    image: str | None = None
    track_url: str | None = None
    priority: int | None = None
    purchased: bool | None = None
    use_amazon: bool | None = None
    variant_key: str | None = None


@app.patch("/api/items/{item_id}", dependencies=[Depends(require_admin)])
def edit_item(item_id: int, body: ItemPatch):
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(404)
    upd: dict = {}
    if body.name is not None:
        upd.update(name=body.name.strip(), name_locked=1)
    if body.notes is not None:
        upd["notes"] = body.notes
    if body.priority is not None:
        upd["priority"] = body.priority
    if body.purchased is not None:
        upd["purchased"] = int(body.purchased)
    if body.image is not None:
        upd["image"] = body.image.strip() or None
    if body.price is not None:
        upd.update(price=body.price, list_price=None, last_error=None)
        db.add_price(item_id, body.price, None)
    db.update_item(item_id, upd)

    new_url = None
    if body.use_amazon and item["amazon_url"]:
        new_url = item["amazon_url"]
    elif body.track_url and body.track_url.strip() != item["track_url"]:
        new_url = body.track_url.strip()
    if new_url:
        db.update_item(item_id, {"track_url": new_url, "variant_key": None, "variants": None})
        service.refresh_item(item_id)
    elif body.variant_key and body.variant_key != item.get("variant_key"):
        service.set_variant(item_id, body.variant_key)
    return _present(db.get_item(item_id))


@app.post("/api/items/{item_id}/refresh", dependencies=[Depends(require_admin)])
def refresh(item_id: int):
    item = service.refresh_item(item_id)
    if not item:
        raise HTTPException(404)
    return _present(item)


@app.delete("/api/items/{item_id}", dependencies=[Depends(require_admin)])
def remove(item_id: int):
    db.delete_item(item_id)
    return {"ok": True}


@app.post("/api/refresh-all", dependencies=[Depends(require_admin)])
def refresh_all():
    scheduler.add_job(service.refresh_all, id="refresh-now", replace_existing=True) if scheduler.running \
        else service.refresh_all()
    return {"ok": True, "message": "Refreshing prices in the background"}


@app.get("/healthz")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- static

try:
    os.makedirs(os.path.join(db.DATA_DIR, "images"), exist_ok=True)
except PermissionError:
    raise SystemExit(
        f"\n*** Can't write to {db.DATA_DIR}. The app runs as user {os.getuid()}:{os.getgid()}; "
        f"make the data folder on the host owned by that user (chown -R {os.getuid()}:{os.getgid()} <folder>). ***\n")
app.mount("/images", StaticFiles(directory=os.path.join(db.DATA_DIR, "images")), name="images")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))
