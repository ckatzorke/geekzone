#!/usr/bin/env python3
"""Holt die Geekzone-Jahresliste von Letterboxd und berechnet Statistiken.

Aufruf:  python3 scripts/fetch_letterboxd.py [--year 2026] [--refresh]

Ergebnis: data/films-<jahr>.json  (+ Poster unter assets/posters/)
Podcaster-Daten: Letterboxd-Export (Settings → Data → Export) je Person als
  data/exports/<user>.zip  oder entpackt unter  data/exports/<user>/
HTTP-Antworten werden unter .cache/ zwischengespeichert; --refresh ignoriert den Cache.
"""
import argparse
import csv
import datetime as dt
import hashlib
import html
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
BASE = "https://letterboxd.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) GeekzoneJahresrueckblick/1.0"

LIST_OWNER = "ckatzorke"
PODCASTERS = [
    {"name": "Tax", "user": "audiotax"},
    {"name": "Fipps", "user": "philst"},
    {"name": "Peter", "user": "misterbeam5000"},
    {"name": "Christian", "user": "ckatzorke"},
]

REFRESH = False


def fetch(url, binary=False):
    """GET mit Datei-Cache. Gibt None bei 404 zurück."""
    CACHE.mkdir(exist_ok=True)
    key = CACHE / hashlib.sha1(url.encode()).hexdigest()
    if key.exists() and not REFRESH:
        data = key.read_bytes()
        return None if data == b"__404__" else (data if binary else data.decode("utf-8"))
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                key.write_bytes(b"__404__")
                return None
            if attempt == 3:
                raise
            wait = 20 * (attempt + 1) if e.code in (403, 429) else 3 * (attempt + 1)
            print(f"    HTTP {e.code} bei {url} – warte {wait}s", file=sys.stderr)
            time.sleep(wait)
        except urllib.error.URLError:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    key.write_bytes(data)
    time.sleep(0.6)  # höflich bleiben
    return data if binary else data.decode("utf-8")


def paged_slugs(path):
    """Liefert [(slug, rating|None)] über alle Seiten eines Poster-Grids."""
    out, page = [], 1
    while True:
        url = f"{BASE}{path}" if page == 1 else f"{BASE}{path}page/{page}/"
        h = fetch(url)
        if not h:
            break
        items = re.findall(r'<li class="(?:griditem|posteritem)[^"]*".*?</li>', h, re.S)
        if not items:
            break
        for it in items:
            m = re.search(r'data-item-slug="([^"]+)"', it)
            if not m:
                continue
            r = re.search(r"\brated-(\d+)\b", it)
            out.append((m.group(1), int(r.group(1)) / 2 if r else None))
        if f'/page/{page + 1}/"' not in h:
            break
        page += 1
    return out


def film_details(slug):
    h = fetch(f"{BASE}/film/{slug}/")
    m = re.search(r'<script type="application/ld\+json">\s*(?:/\*.*?\*/)?\s*(\{.*?\})\s*(?:/\*.*?\*/)?\s*</script>', h, re.S)
    ld = json.loads(m.group(1)) if m else {}
    og = re.search(r'<meta property="og:title" content="([^"]*)"', h)
    year = re.search(r"/films/year/(\d{4})/", h)
    if not year and og:
        year = re.search(r"\((\d{4})\)\s*$", html.unescape(og.group(1)))
    genres = ld.get("genre") or []
    if isinstance(genres, str):
        genres = [genres]
    agg = ld.get("aggregateRating") or {}
    directors = [d.get("name") for d in (ld.get("director") or []) if d.get("name")]
    return {
        "slug": slug,
        "title": html.unescape(ld.get("name") or slug),
        "year": int(year.group(1)) if year else None,
        "genres": genres,
        "directors": directors,
        "lbRating": agg.get("ratingValue"),
        "lbRatingCount": agg.get("ratingCount"),
        "posterSrc": ld.get("image"),
        "url": f"{BASE}/film/{slug}/",
    }


def download_poster(film):
    src = film.pop("posterSrc", None)
    if not src:
        film["poster"] = None
        return
    small = re.sub(r"-0-\d+-0-\d+-crop", "-0-230-0-345-crop", src)
    dest = ROOT / "assets" / "posters" / f"{film['slug']}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or REFRESH:
        data = fetch(small, binary=True) or fetch(src, binary=True)
        if data:
            dest.write_bytes(data)
    film["poster"] = f"assets/posters/{film['slug']}.jpg" if dest.exists() else None


def norm(title):
    t = html.unescape(title or "").lower().replace("&", "and")
    return re.sub(r"[^\w]+", "", t)


def _read_csv(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def load_export(user):
    """Liest einen Letterboxd-Export (ZIP oder entpackter Ordner).

    Gesucht wird data/exports/<user>.zip oder data/exports/<user>/ bzw. jede ZIP
    in data/exports/, deren Dateiname mit 'letterboxd-<user>-' beginnt.
    Liefert {(norm_titel, jahr): rating|None} oder None, falls kein Export da ist.
    """
    exp = ROOT / "data" / "exports"
    files = {}
    folder = exp / user
    zips = [exp / f"{user}.zip"] + sorted(exp.glob(f"letterboxd-{user}-*.zip"))
    if folder.is_dir():
        for name in ("watched.csv", "ratings.csv"):
            if (folder / name).exists():
                files[name] = (folder / name).read_text(encoding="utf-8")
    else:
        z = next((p for p in reversed(zips) if p.exists()), None)
        if not z:
            return None
        with zipfile.ZipFile(z) as zf:
            for name in ("watched.csv", "ratings.csv"):
                if name in zf.namelist():
                    files[name] = zf.read(name).decode("utf-8")
    if not files:
        return None
    out = {}
    for row in _read_csv(files.get("watched.csv", "")):
        out[(norm(row["Name"]), int(row["Year"]) if row.get("Year") else None)] = None
    for row in _read_csv(files.get("ratings.csv", "")):
        key = (norm(row["Name"]), int(row["Year"]) if row.get("Year") else None)
        out[key] = float(row["Rating"]) if row.get("Rating") else None
    return out


def extremes(films, key):
    rated = [f for f in films if f.get(key) is not None]
    if not rated:
        return {"best": [], "worst": []}
    hi = max(f[key] for f in rated)
    lo = min(f[key] for f in rated)
    return {
        "best": [f["slug"] for f in rated if f[key] == hi],
        "worst": [f["slug"] for f in rated if f[key] == lo],
        "bestValue": hi,
        "worstValue": lo,
    }


def main():
    global REFRESH
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    REFRESH = args.refresh

    list_path = f"/{LIST_OWNER}/list/geekzone-{args.year}/"
    print(f"Liste {list_path} …", file=sys.stderr)
    slugs = [s for s, _ in paged_slugs(list_path)]
    print(f"  {len(slugs)} Filme", file=sys.stderr)

    films = []
    for i, slug in enumerate(slugs, 1):
        print(f"  [{i}/{len(slugs)}] {slug}", file=sys.stderr)
        f = film_details(slug)
        download_poster(f)
        f["ratings"] = {}
        films.append(f)
    by_slug = {f["slug"]: f for f in films}

    # Podcaster-Daten aus den offiziellen Letterboxd-Exporten (data/exports/<user>/)
    by_key = {}
    for f in films:
        by_key.setdefault((norm(f["title"]), f["year"]), f)
    missing_exports = []
    for p in PODCASTERS:
        watched = load_export(p["user"])
        if watched is None:
            missing_exports.append(p["user"])
            p["hasData"] = False
            continue
        p["hasData"] = True
        hits = 0
        for key, rating in watched.items():
            f = by_key.get(key)
            if f:
                f["ratings"][p["user"]] = rating  # None = gesehen, nicht bewertet
                hits += 1
        print(f"Export {p['user']}: {len(watched)} Filme gesamt, {hits} aus der Liste", file=sys.stderr)
    if missing_exports:
        print(f"WARNUNG: kein Export für {', '.join(missing_exports)} unter data/exports/<user>/", file=sys.stderr)

    # --- Statistiken ---
    lb = [f["lbRating"] for f in films if f["lbRating"] is not None]
    genre_counts = Counter(g for f in films for g in f["genres"])
    year_counts = Counter(f["year"] for f in films if f["year"])
    stats = {
        "count": len(films),
        "avgLbRating": round(sum(lb) / len(lb), 2) if lb else None,
        "lbExtremes": extremes(films, "lbRating"),
        "genres": genre_counts.most_common(),
        "years": sorted(year_counts.items()),
    }

    people = []
    for p in PODCASTERS:
        u = p["user"]
        seen = [f for f in films if u in f["ratings"]]
        rated = [{"slug": f["slug"], "r": f["ratings"][u]} for f in seen if f["ratings"][u] is not None]
        ex = extremes(rated, "r")
        people.append({
            **p,
            "profile": f"{BASE}/{u}/",
            "watched": len(seen),
            "rated": len(rated),
            "avgRating": round(sum(x["r"] for x in rated) / len(rated), 2) if rated else None,
            "best": ex["best"], "bestValue": ex.get("bestValue"),
            "worst": ex["worst"], "worstValue": ex.get("worstValue"),
        })
    people.sort(key=lambda x: -x["watched"])

    out = {
        "year": args.year,
        "listUrl": f"{BASE}{list_path}",
        "generated": dt.date.today().isoformat(),
        "stats": stats,
        "podcasters": people,
        "films": films,
    }
    dest = ROOT / "data" / f"films-{args.year}.json"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    # Zusätzlich als JS, damit index.html auch per file:// (ohne Webserver) funktioniert
    js = dest.with_suffix(".js")
    js.write_text(f"window.GZ_FILMS = {json.dumps(out, ensure_ascii=False)};\n")
    print(f"→ {dest.relative_to(ROOT)}, {js.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
