#!/usr/bin/env python3
"""
WARDOGS Steam-Update-Bot für Discord (per Webhook)

- Postet neue offizielle Steam-Ankündigungen und Patchnotes als Embed
- Optional: meldet neue Builds auf dem public-Branch (erkennt auch "stille" Patches ohne News-Post)
- Nur Python-Standardbibliothek, keine pip-Installation nötig

Konfiguration über Umgebungsvariablen:
  DISCORD_WEBHOOK_URL  (Pflicht)  Webhook-URL des Discord-Kanals
  PING_ROLE_ID         (optional) Rollen-ID, die bei jedem Post erwähnt wird
  CHECK_INTERVAL       (optional) Prüfintervall in Sekunden, Standard 600
  TRACK_BUILDS         (optional) "1" = Build-Änderungen melden (Standard), "0" = aus
  STATE_FILE           (optional) Pfad der Statusdatei, Standard wardogs_state.json

Aufruf:
  python3 wardogs_steam_bot.py          Dauerbetrieb (Schleife)
  python3 wardogs_steam_bot.py --once   Einmal prüfen und beenden (für cron / systemd-Timer)
  python3 wardogs_steam_bot.py --test   Neueste Ankündigung sofort posten (Webhook testen)

Ohne eigenen Server: über GitHub Actions mit .github/workflows/wardogs.yml (siehe dort).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

APP_ID = 1867240
GAME_NAME = "WARDOGS"
STORE_URL = f"https://store.steampowered.com/app/{APP_ID}/"
NEWS_API = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    f"?appid={APP_ID}&count=10&maxlength=0&format=json"
)
BUILD_API = f"https://api.steamcmd.net/v1/info/{APP_ID}"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
PING_ROLE_ID = os.environ.get("PING_ROLE_ID", "").strip()
INTERVAL = int(os.environ.get("CHECK_INTERVAL", "600"))
TRACK_BUILDS = os.environ.get("TRACK_BUILDS", "1") == "1"
STATE_FILE = Path(os.environ.get("STATE_FILE", "wardogs_state.json"))

USER_AGENT = "WardogsDiscordBot/1.0 (+https://discord.com)"  # Discord blockt den Standard-UA von urllib
COLOR_PATCH = 0xE67E22
COLOR_NEWS = 0x1B2838
COLOR_BUILD = 0x2ECC71


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------- Status ----------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------- HTTP ----------

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


class WebhookError(Exception):
    """Discord hat den Post abgelehnt (z. B. Webhook gelöscht oder URL falsch)."""


def post_webhook(embed):
    payload = {"username": f"{GAME_NAME} Updates", "embeds": [embed]}
    if PING_ROLE_ID:
        payload["content"] = f"<@&{PING_ROLE_ID}>"
        payload["allowed_mentions"] = {"roles": [PING_ROLE_ID]}
    else:
        payload["allowed_mentions"] = {"parse": []}

    data = json.dumps(payload).encode("utf-8")
    for _ in range(3):
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=20):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Rate-Limit: warten und erneut versuchen
                try:
                    retry = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                except (ValueError, json.JSONDecodeError):
                    retry = 2.0
                time.sleep(retry)
                continue
            raise WebhookError(f"Discord antwortet mit HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise WebhookError(f"Discord nicht erreichbar: {e.reason}") from e
    raise WebhookError("Webhook nach 3 Versuchen nicht erfolgreich (Rate-Limit)")


# ---------- Formatierung ----------

def clean(text, limit=700):
    """Steam-BBCode in lesbares Discord-Markdown umwandeln und kürzen."""
    t = text or ""
    t = re.sub(r"\[img[^\]]*\].*?\[/img\]", "", t, flags=re.I | re.S)
    t = re.sub(r"\[img[^\]]*\]", "", t, flags=re.I)
    t = re.sub(r"\{STEAM_CLAN_IMAGE\}\S*", "", t)
    t = re.sub(r"\[previewyoutube[^\]]*\].*?\[/previewyoutube\]", "", t, flags=re.I | re.S)
    t = re.sub(r"\[h[1-6][^\]]*\](.*?)\[/h[1-6]\]", r"\n**\1**\n", t, flags=re.I | re.S)
    t = re.sub(r"\[b\](.*?)\[/b\]", r"**\1**", t, flags=re.I | re.S)
    t = re.sub(r"\[i\](.*?)\[/i\]", r"*\1*", t, flags=re.I | re.S)
    # Links erst in Platzhalter umwandeln, damit die Tag-Bereinigung sie nicht entfernt
    t = re.sub(r"\[url=[\"']?([^\]\"' ]+)[\"']?[^\]]*\](.*?)\[/url\]", "\x00\\2\x01\\1\x02", t, flags=re.I | re.S)
    t = re.sub(r"\[\*\]", "\n• ", t)
    t = re.sub(r"\[/p\]|\[br\]|\[/list\]", "\n", t, flags=re.I)
    t = re.sub(r"\[/?[a-z0-9]+(?:[= ][^\]]*)?\]", "", t, flags=re.I)  # restliche Tags entfernen
    t = re.sub(r"\x00(.*?)\x01(.*?)\x02", r"[\1](\2)", t, flags=re.S)  # Platzhalter -> Markdown-Link
    t = re.sub(r"<[^>]+>", "", t)                                     # HTML-Reste
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip() + " …"
    return t


def news_embed(item):
    is_patch = "patchnotes" in (item.get("tags") or [])
    label = "Patchnotes" if is_patch else "Ankündigung"
    desc = clean(item.get("contents", ""))
    desc = (desc + "\n\n" if desc else "") + f"[Vollständig auf Steam lesen]({item['url']})"
    return {
        "author": {"name": f"{GAME_NAME} · {label}", "url": STORE_URL},
        "title": item["title"][:256],
        "url": item["url"],
        "description": desc[:4000],
        "color": COLOR_PATCH if is_patch else COLOR_NEWS,
        "timestamp": datetime.fromtimestamp(item["date"], timezone.utc).isoformat(),
        "footer": {"text": "Steam News"},
    }


# ---------- Checks ----------

def fetch_official_news():
    items = get_json(NEWS_API)["appnews"]["newsitems"]
    # Nur offizielle Entwickler-Posts, keine Presseartikel (PC Gamer etc.)
    return [i for i in items if i.get("feedname") == "steam_community_announcements"]


def check_news(state, seed):
    seen = state.setdefault("seen_gids", [])
    new = [i for i in fetch_official_news() if i["gid"] not in seen]
    for item in sorted(new, key=lambda i: i["date"]):
        if not seed:
            post_webhook(news_embed(item))
            log(f"News gepostet: {item['title']}")
            time.sleep(1)
        seen.append(item["gid"])
    state["seen_gids"] = seen[-200:]


def check_build(state, seed):
    data = get_json(BUILD_API)
    public = data["data"][str(APP_ID)]["depots"]["branches"]["public"]
    build_id = str(public["buildid"])
    old = state.get("build_id")
    if old == build_id:
        return
    if not seed and old:
        ts = int(public.get("timeupdated") or time.time())
        post_webhook({
            "author": {"name": f"{GAME_NAME} · Neuer Build", "url": STORE_URL},
            "title": "Update auf Steam verfügbar",
            "url": f"https://steamdb.info/app/{APP_ID}/patchnotes/",
            "description": (
                f"Build **{old}** → **{build_id}**\n"
                "Steam neu starten, falls der Download nicht automatisch beginnt."
            ),
            "color": COLOR_BUILD,
            "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "footer": {"text": "public-Branch"},
        })
        log(f"Build-Update gepostet: {old} -> {build_id}")
    state["build_id"] = build_id


def run_checks(state, seed):
    """Führt alle Prüfungen aus. Gibt False zurück, wenn ein Discord-Post fehlgeschlagen ist."""
    webhook_ok = True
    checks = [("News", check_news)] + ([("Build", check_build)] if TRACK_BUILDS else [])
    for name, fn in checks:
        try:
            fn(state, seed)
        except WebhookError as e:
            webhook_ok = False
            log(f"Discord-Fehler ({name}): {e}")
        except Exception as e:
            # Steam-API zeitweise nicht erreichbar o. Ä.: beim nächsten Lauf erneut versuchen
            log(f"Fehler bei {name}-Abfrage: {e}")
    save_state(state)
    return webhook_ok


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser(description="WARDOGS Steam-Update-Bot für Discord")
    ap.add_argument("--once", action="store_true", help="einmal prüfen und beenden")
    ap.add_argument("--test", action="store_true", help="neueste Ankündigung sofort posten")
    args = ap.parse_args()

    if not WEBHOOK_URL.startswith("https://"):
        sys.exit("DISCORD_WEBHOOK_URL ist nicht gesetzt.")

    if args.test:
        news = fetch_official_news()
        if not news:
            sys.exit("Keine offiziellen Ankündigungen gefunden.")
        latest = max(news, key=lambda i: i["date"])
        post_webhook(news_embed(latest))
        log(f"Test gepostet: {latest['title']}")
        return

    state = load_state()
    if not state:
        # Erster Start: aktuellen Stand merken, nichts posten (kein Spam mit alten News)
        log("Erster Start: aktueller Stand wird gespeichert, ab jetzt werden neue Updates gepostet.")
        run_checks(state, seed=True)
        if args.once:
            return

    while True:
        ok = run_checks(state, seed=False)
        if args.once:
            # Exit-Code 1, damit GitHub bei kaputtem Webhook eine Fehler-Mail schickt
            sys.exit(0 if ok else 1)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
