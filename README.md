# Gift List

A self-hosted wishlist. Paste a product link and it pulls in the name, picture and price.
Prices are re-checked automatically, and anything on sale gets a red **SALE −xx%** badge.

When you paste an **Amazon** link, it looks for the same product on the brand's own site or another
store. It only accepts a match when the other page has the same UPC, or the same brand and model
number. If it can't find one, it keeps the Amazon link.

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
| `REFRESH_HOURS` | 12 | How often all prices are re-checked (0 = never) |
| `SALE_DROP_PERCENT` | 5 | A price this % below the item's usual price counts as a sale |
| `MATCH_AMAZON` | true | Look for Amazon items on other stores |
| `SEARXNG_URL` | *(empty)* | Use your own SearXNG instance for the store search (recommended) |

## How "on sale" is decided

An item is marked on sale if either:
1. the store shows a "was" / list / strikethrough price higher than the current price, or
2. the current price is `SALE_DROP_PERCENT`% or more below its usual price (the median of the last
   60 days of price checks). This needs a few checks of history first.

The small line next to each store name is the item's recent price trend.

## Using the list

- **✎ (edit)**: rename an item, set the price by hand, add notes (size/color), mark it as a top pick,
  change the tracked link, or delete it.
- **Find on another store**: re-runs the Amazon matching, for example for an item added before a
  retailer stocked it.
- **Use Amazon link**: switch back to tracking Amazon if you don't like the match it found.
- **Mark purchased**: moves the item to a "Purchased" section that only you can see.
- Links that can't be read (blocked sites, non-store pages) are still added. Just fill in the name
  and price yourself.

## Making the Amazon matching more reliable

By default it searches DuckDuckGo, which sometimes rate-limits automated searches. For dependable
results run your own [SearXNG](https://docs.searxng.org/): uncomment the `searxng` service in
`docker-compose.yml`, set `SEARXNG_URL: "http://searxng:8080"`, and enable JSON output in
`./searxng/settings.yml`:

```yaml
search:
  formats: [html, json]
```

## Known limits

- **Amazon** fights automated price checks. Sometimes it shows a captcha, and that check is skipped
  (the last known price stays, with a ⚠ note). This is another reason matched non-Amazon links work better.
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
