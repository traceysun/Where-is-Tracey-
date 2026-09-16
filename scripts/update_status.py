"""Pull Tracey's latest Instagram story, read the location off it, and write data/status.json.

Runs on a schedule in GitHub Actions. Needs:
  IG_ACCESS_TOKEN   long-lived Instagram API token (Instagram Login flow)
  ANTHROPIC_API_KEY for reading the location sticker off the story image

Stories only live 24h, so an empty /me/stories response means "nothing recent" -> work mode.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import requests

ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = ROOT / "data" / "status.json"
CONFIG_PATH = ROOT / "config.json"

IG_API = "https://graph.instagram.com/v23.0"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "where-is-tracey/1.0 (github pages status site)"

# Only report a place that's actually written on the story - never guess from scenery.
EXTRACT_SYSTEM = """You read Instagram story screenshots and pull out the place the poster tagged.

Look for: a location sticker (rounded pill with a pin icon), a text overlay naming a place
("at Dolores Park", "Tartine 🥐"), a tagged venue, or a caption. Read the exact text.

Rules:
- Only report a location that is explicitly written on the image. Do NOT infer a place from
  scenery, landmarks, food, or vibes. If nothing names a place, has_location = false.
- place_name is the venue or spot as written (clean up emoji/casing). city is the city it's in;
  if the sticker shows "Venue, City" use that city, otherwise your best knowledge of where that
  venue is. If you can't tell the city, leave it null.
- geocode_query is one string a geocoder will resolve well: "Venue, City, State" when possible.
- latitude/longitude: your best estimate for that specific place if you know it, else null.
"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "has_location": {"type": "boolean"},
        "place_name": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "geocode_query": {"type": ["string", "null"]},
        "latitude": {"type": ["number", "null"]},
        "longitude": {"type": ["number", "null"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["has_location", "place_name", "city", "geocode_query", "latitude", "longitude", "confidence"],
    "additionalProperties": False,
}


def log(msg: str) -> None:
    print(msg, flush=True)


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------- Instagram ----------

def fetch_stories(token: str) -> list:
    """Active (last 24h) stories, newest first."""
    r = requests.get(
        f"{IG_API}/me/stories",
        params={
            "fields": "id,media_type,media_url,thumbnail_url,timestamp,permalink",
            "access_token": token,
        },
        timeout=30,
    )
    if r.status_code != 200:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code")
        if code == 190:
            log("ERROR: Instagram token is invalid or expired. Generate a new long-lived token "
                "and update the IG_ACCESS_TOKEN secret.")
        else:
            log(f"ERROR: Instagram API {r.status_code}: {err.get('message') or r.text[:300]}")
        sys.exit(1)
    stories = r.json().get("data", [])
    stories.sort(key=lambda s: s.get("timestamp", ""), reverse=True)
    return stories


def maybe_refresh_token(token: str) -> None:
    """Long-lived tokens expire after 60 days; refreshing keeps them alive.

    The refreshed token can only be written back to the repo secret if GH_PAT is set
    (see README). Without it we still refresh so the *current* token's clock resets, and
    warn when it's getting old.
    """
    r = requests.get(
        f"{IG_API}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=30,
    )
    if r.status_code != 200:
        log(f"warn: token refresh failed ({r.status_code}): {r.text[:200]}")
        return
    body = r.json()
    new_token = body.get("access_token")
    expires_in = body.get("expires_in", 0)
    log(f"token refreshed; expires in ~{expires_in // 86400} days")
    if new_token and new_token != token:
        pat = os.environ.get("GH_PAT")
        repo = os.environ.get("GITHUB_REPOSITORY")
        if pat and repo:
            import subprocess
            subprocess.run(
                ["gh", "secret", "set", "IG_ACCESS_TOKEN", "--repo", repo, "--body", new_token],
                env={**os.environ, "GH_TOKEN": pat},
                check=False,
            )
            log("wrote refreshed token to IG_ACCESS_TOKEN secret")
        else:
            log("warn: Instagram issued a new token but GH_PAT is not set, so the secret was not "
                "updated. Paste the new token into the IG_ACCESS_TOKEN secret before it expires.")


# ---------- Claude ----------

def extract_location(client: anthropic.Anthropic, story: dict) -> Optional[dict]:
    image_url = story.get("thumbnail_url") if story.get("media_type") == "VIDEO" else story.get("media_url")
    if not image_url:
        return None

    response = client.beta.messages.create(
        model="claude-opus-5",
        max_tokens=1024,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=EXTRACT_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "url", "url": image_url}},
                {"type": "text", "text": "Where was this story posted from? Fill in the schema."},
            ],
        }],
        output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        log(f"story {story['id']}: model declined to read image")
        return None
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return None
    return json.loads(text)


# ---------- Geocoding ----------

def geocode(query: str) -> Optional[dict]:
    try:
        r = requests.get(
            NOMINATIM,
            params={"q": query, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        r.raise_for_status()
        hits = r.json()
    except Exception as e:  # network hiccups shouldn't kill the run; we have a fallback
        log(f"warn: geocode failed for {query!r}: {e}")
        return None
    if not hits:
        return None
    hit = hits[0]
    addr = hit.get("address", {})
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
    return {"lat": float(hit["lat"]), "lng": float(hit["lon"]), "city": city}


def resolve_place(extracted: dict) -> Optional[dict]:
    """Turn Claude's reading into {place, city, lat, lng} or None if we can't pin it."""
    if not extracted.get("has_location") or not extracted.get("place_name"):
        return None
    place = extracted["place_name"]
    city = extracted.get("city")
    lat = extracted.get("latitude")
    lng = extracted.get("longitude")

    query = extracted.get("geocode_query") or ", ".join(x for x in [place, city] if x)
    hit = geocode(query)
    if hit is None and city and query != f"{place}, {city}":
        hit = geocode(f"{place}, {city}")
    if hit:
        lat, lng = hit["lat"], hit["lng"]
        city = city or hit.get("city")
    if lat is None or lng is None:
        log(f"could not geocode {query!r} and model had no coordinates; skipping")
        return None
    return {"place": place, "city": city, "lat": lat, "lng": lng}


# ---------- Main ----------

def main() -> int:
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        log("ERROR: IG_ACCESS_TOKEN not set")
        return 1

    status = load_json(STATUS_PATH, {"mode": "work", "current": None, "previous": None})
    status.setdefault("analyzed_without_location", [])
    before = json.dumps(status, sort_keys=True)

    stories = fetch_stories(token)
    log(f"{len(stories)} active stories")

    current = status.get("current")
    current_id = current["story_id"] if current else None
    skip_ids = set(status["analyzed_without_location"])

    client = None
    located = None
    for story in stories:
        if story["id"] == current_id:
            located = current  # newest located story is the one we already have
            break
        if story["id"] in skip_ids:
            continue
        client = client or anthropic.Anthropic()
        try:
            extracted = extract_location(client, story)
        except anthropic.APIStatusError as e:
            log(f"ERROR: Claude API {e.status_code}: {e.message}")
            return 1
        place = resolve_place(extracted) if extracted else None
        if place is None:
            log(f"story {story['id']}: no location on it")
            status["analyzed_without_location"].append(story["id"])
            continue
        located = {
            "story_id": story["id"],
            "timestamp": story["timestamp"],
            "permalink": story.get("permalink"),
            **place,
        }
        log(f"story {story['id']}: {place['place']} ({place['city']}) @ {story['timestamp']}")
        break

    if located and located.get("story_id") != current_id:
        status["previous"] = current
        status["current"] = located

    # Active located story -> show it. Otherwise everything's expired -> work mode.
    active_ids = {s["id"] for s in stories}
    status["mode"] = "story" if status.get("current") and status["current"]["story_id"] in active_ids else "work"

    status["analyzed_without_location"] = [i for i in status["analyzed_without_location"] if i in active_ids][-50:]

    if os.environ.get("REFRESH_TOKEN") == "1":
        maybe_refresh_token(token)

    # Only touch the file when something real changed, so the workflow doesn't commit every 30 min.
    strip = lambda d: json.dumps({k: v for k, v in d.items() if k != "updated_at"}, sort_keys=True)
    if strip(status) == strip(json.loads(before)):
        log(f"no change (mode={status['mode']})")
        return 0
    status["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(STATUS_PATH, status)
    log(f"updated (mode={status['mode']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
