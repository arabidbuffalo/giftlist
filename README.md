# Gift List

A self-hosted wishlist. Paste a product link and it pulls in the name, picture and price.
Prices are re-checked automatically, and anything on sale gets a red **SALE −xx%** badge.

When a page sells several versions (CD and vinyl, colors, sizes), it tracks the version your link
points to, and you can switch versions from the item's edit screen.

## Run it

Needs Docker with Compose.

```bash
# copy this folder to your server, then:
nano docker-compose.yml        # set ADMIN_PASSWORD and LIST_TITLE
docker compose up -d --build
```

Open `http://<server-ip>:8080`. Click **Sign in** and enter your password to add and edit items.
Anyone without the password can view the list but not change it, so you can share the link with family.

Everything is stored in `./data` (SQLite database + cached product images). Back up that folder.

## Dockge / any Docker host, via GitHub (recommended)

1. Create a GitHub repo named `giftlist` and push this folder to it. The included workflow
   (`.github/workflows/docker.yml`) builds the image on every push and publishes it as
   `ghcr.io/<you>/giftlist:latest`. Check the **Actions** tab; the first build takes about 2 minutes.
2. After the first build, go to your GitHub profile → **Packages** → `giftlist` → **Package settings**
   → **Change visibility** → Public, so your server can pull it without logging in. (Or keep it private
   and run `docker login ghcr.io` on the server with a token that has `read:packages`.)
3. In Dockge, create a stack named `giftlist`, paste `dockge-compose.yaml`, fill in your username,
   pool and password, and deploy.
4. **Updating:** push changes to GitHub, wait for the build, then click **Update** on the stack in Dockge.

## TrueNAS SCALE (24.10 or newer), without GitHub

1. **Datasets.** In *Datasets*, create `apps/giftlist` on your pool, then a child dataset `data`
   inside it. Edit the `data` dataset's permissions: owner user **apps**, group **apps**.
2. **Copy the code.** Copy this folder into `/mnt/POOL/apps/giftlist/src`, using an SMB share or
   `scp`, for example.
3. **Build the image.** Open *System > Shell* and run:
   ```bash
   sudo docker build -t giftlist:local /mnt/POOL/apps/giftlist/src
   ```
4. **Install.** Go to *Apps > Discover Apps*, open the **⋮** menu, choose **Install via YAML**, name
   the app `giftlist`, and paste in `truenas-app.yaml` (with POOL and the password changed).
5. Open `http://<truenas-ip>:8080`.

**Updating later:** copy the new code into `src`, run the same `docker build` command, then
**Stop** and **Start** the app in the Apps screen.

If port 8080 is already in use, change the left side of the port line, e.g. `"8095:8080"`.

## Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `ADMIN_PASSWORD` | *(empty)* | Password for editing. **Empty = anyone can edit.** |
| `LIST_TITLE` | My Wishlist | Heading on the page |
| `THEME` | auto | `auto` = Christmas look in Nov–Dec, birthday look in March, normal otherwise. Or force `christmas`, `birthday` or `default`. |
| `REFRESH_HOURS` | 12 | How often all prices are re-checked (0 = never) |
| `SALE_DROP_PERCENT` | 5 | A price this % below the item's usual price counts as a sale |

## How "on sale" is decided

An item is marked on sale if either:
1. the store shows a "was" / list / strikethrough price higher than the current price, or
2. the current price is `SALE_DROP_PERCENT`% or more below its usual price (the median of the last
   60 days of price checks). This needs a few checks of history first.

The small line next to each store name is the item's recent price trend.

## Using the list

- **✎ (edit)**: rename an item, set the price by hand, add notes (size/color), mark it as a top pick,
  change the tracked link, or delete it.
- **Version**: when a page sells several versions, pick the one to track.
- **Mark purchased**: moves the item to a "Purchased" section that only you can see.
- Links that can't be read (blocked sites, non-store pages) are still added. Just fill in the name
  and price yourself.

## Known limits

- **Amazon** fights automated price checks. Sometimes it shows a captcha, and that check is skipped
  (the last known price stays, with a ⚠ note). Links to the brand's own store are more reliable.
- Some stores build their pages entirely with JavaScript and publish no product data. For those,
  enter the price by hand.
- Checks are spaced a few seconds apart so stores don't treat it as a bot.

## Remote access

To reach it from outside your home, put it behind a reverse proxy with HTTPS (Caddy, Nginx Proxy
Manager, Traefik), or use Tailscale / Cloudflare Tunnel. Set a strong `ADMIN_PASSWORD` first.

## Development

```bash
pip install -r requirements.txt pytest httpx
DATA_DIR=./data uvicorn app.main:app --reload --port 8080
pytest tests
```
