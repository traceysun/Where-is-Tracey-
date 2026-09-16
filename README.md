# Where is Tracey?

A tiny site that reads Tracey's latest Instagram story, pulls the tagged location off it, and shows
where she was last seen with a small, draggable 3D map of the spot.

- Story with a location in the last 24h → *"Tracey was at **Dolores Park, San Francisco** at 10:12 AM.
  Before that, you could've run into her in San Francisco, at Tartine Manufactory."*
- Nothing recent → *"Tracey is probably at work at Blue Truck Studio right now, nothing going on :/"* +
  a 3D map of the office.

## How it works

```
Instagram story ──► GitHub Action (every 30 min) ──► data/status.json ──► GitHub Pages
                     ├─ Instagram API: /me/stories
                     ├─ Claude reads the location sticker off the image
                     └─ OpenStreetMap geocodes it
```

- [`scripts/update_status.py`](scripts/update_status.py) — the updater. Only calls Claude for stories it
  hasn't seen, so a quiet day costs nothing.
- [`.github/workflows/update.yml`](.github/workflows/update.yml) — schedule + commit.
- [`index.html`](index.html) — the page. MapLibre + free OpenFreeMap tiles (no map API key).
- [`config.json`](config.json) — name, timezone, work location, Instagram username.
- [`data/status.json`](data/status.json) — generated; don't edit by hand.

## Setup (one time)

### 1. Instagram

Stories are only readable through the API for **Professional** accounts.

1. Instagram app → Settings → Account type and tools → switch to **Creator** (or Business). Free, and
   you can switch back later.
2. Go to [developers.facebook.com/apps](https://developers.facebook.com/apps) → **Create app** →
   use case *Other* → type *Business* → name it anything.
3. In the app dashboard, add the **Instagram** product → **API setup with Instagram login**.
4. Under *Generate access tokens*, **Add account**, log in as yourself, then **Generate token**.
   Copy it — that's a 60‑day long‑lived token. The app can stay in Development mode; it only ever
   reads your own account.

### 2. Anthropic

Make an API key at [console.anthropic.com](https://console.anthropic.com/settings/keys). Each new
story costs a fraction of a cent to read.

### 3. GitHub

1. Repo → **Settings → Secrets and variables → Actions** → add:
   - `IG_ACCESS_TOKEN` — the token from step 1
   - `ANTHROPIC_API_KEY`
   - `GH_PAT` *(optional)* — a fine‑grained personal access token with *Secrets: read & write* on this
     repo. With it, the workflow refreshes the Instagram token and stores the new one itself, so it
     never expires. Without it, paste a fresh Instagram token every ~50 days.
2. **Settings → Pages** → Source: *Deploy from a branch* → `main` / `/ (root)` → Save.
3. **Actions** tab → *Update location* → **Run workflow** to do the first fetch. After that it runs
   every 30 minutes.

Fill in `instagram_username` in `config.json` if you want the "her instagram ↗" link on the page.

## Posting

Add a **location sticker** (or just write the place in text) on your story. The next run picks it up.
Stories with no place named are ignored — the site never guesses from scenery.

## A note on privacy

This publishes your last tagged location and time to the open web. Anyone with the link can see it.
Stories already show this to your followers, but a public page is a bigger audience — keep the repo
name/URL as private as you'd like it to be.

## Local preview

```bash
python3 -m http.server 8765
```

Then open http://localhost:8765. To try the updater locally:

```bash
pip install -r requirements.txt
IG_ACCESS_TOKEN=... ANTHROPIC_API_KEY=... python scripts/update_status.py
```
