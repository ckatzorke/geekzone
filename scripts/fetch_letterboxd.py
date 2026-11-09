#!/usr/bin/env python3
"""Holt die Geekzone-Jahresliste von Letterboxd und berechnet Statistiken.

Aufruf:  python3 scripts/fetch_letterboxd.py [--year 2026] [--refresh]

Ergebnis: data/films-<jahr>.json  (+ Poster unter assets/posters/)
Podcaster-Daten: Letterboxd-Export (Settings → Data → Export) je Person als
  data/exports/<user>.zip  oder entpackt unter  data/exports/<user>/
Podcast-Folgen: aus https://www.geekzone.de/feed/mp3 (Korrekturen: data/episode-overrides.json)
HTTP-Antworten werden unter .cache/ zwischengespeichert; --refresh ignoriert den Cache.
"""
import argparse
import csv
import datetime as dt
import email.utils
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


def fetch(url, binary=False, fresh=False):
    """GET mit Datei-Cache. Gibt None bei 404 zurück.

    fresh=True: immer neu laden (z.B. Podcast-Feed), Cache nur als Rückfall bei Netzfehlern.
    """
    CACHE.mkdir(exist_ok=True)
    key = CACHE / hashlib.sha1(url.encode()).hexdigest()
    if fresh and key.exists():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            key.write_bytes(data)
            return data if binary else data.decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"    {url} nicht erreichbar ({e}) – nutze Cache", file=sys.stderr)
    if key.exists() and (not REFRESH or fresh):
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
        paths = [p.relative_to(folder).as_posix() for p in folder.rglob("*.csv")]
        read = lambda p: (folder / p).read_text(encoding="utf-8")
    else:
        z = next((p for p in reversed(zips) if p.exists()), None)
        if not z:
            return None
        zf = zipfile.ZipFile(z)
        paths = zf.namelist()
        read = lambda p: zf.read(p).decode("utf-8")
    for name in ("watched.csv", "ratings.csv"):
        # Datei kann auch in einem Unterordner liegen (z.B. am Mac neu gepackt);
        # Mac-Metadaten und die Export-Unterordner deleted/ orphaned/ ignorieren.
        cands = [p for p in paths
                 if p.rsplit("/", 1)[-1] == name
                 and not re.search(r"(^|/)(__MACOSX|deleted|orphaned|likes|lists)/", p)]
        if cands:
            files[name] = read(min(cands, key=lambda p: p.count("/")))
    if not files:
        return None
    out = {}
    for row in _read_csv(files.get("watched.csv", "")):
        out[(norm(row["Name"]), int(row["Year"]) if row.get("Year") else None)] = None
    for row in _read_csv(files.get("ratings.csv", "")):
        key = (norm(row["Name"]), int(row["Year"]) if row.get("Year") else None)
        out[key] = float(row["Rating"]) if row.get("Rating") else None
    return out


# ---------------------------------------------------------------------------
# Podcast-Folgen: In welcher Geekzone wurde ein Film besprochen?
# ---------------------------------------------------------------------------
FEED_URL = "https://www.geekzone.de/feed/mp3"
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}
ARTICLES = {"the", "a", "an"}
APOS = str.maketrans({c: "'" for c in "‘’´`ʼ"})
PEOPLE_PAREN = re.compile(r"\s*\((?=[^()]*\b(?:Peter|Philipp|Fipps|Christian|Tax|Sidekick|alle)\b)[^()]*\)", re.I)


def tokens(s):
    s = html.unescape(s or "").lower().replace("&", " and ").translate(APOS)
    toks = re.findall(r"[^\W_]+", s.replace("'", ""))
    return tuple(ROMAN.get(t, t) for t in toks if t not in ARTICLES)


def load_episodes(year):
    """Liest den Podcast-Feed; liefert Folgen des Jahres (+ Januar des Folgejahres)."""
    xml = fetch(FEED_URL, fresh=True)
    eps = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = html.unescape(re.search(r"<title>(.*?)</title>", it, re.S).group(1)).strip()
        date = email.utils.parsedate_to_datetime(re.search(r"<pubDate>(.*?)</pubDate>", it).group(1))
        if not (date.year == year or (date.year == year + 1 and date.month == 1)):
            continue
        m = re.match(r"GZ\s*(\d+)", title)
        if not m or re.search(r"\bPLAY\b", title):  # PLAY-Folgen behandeln Spiele, nicht Filme
            continue
        link = (re.search(r"<link>(.*?)</link>", it) or [None, ""])[1].strip()
        path = link.rstrip("/").rsplit("/", 1)[-1]
        desc = html.unescape((re.search(r"<description>(.*?)</description>", it, re.S) or [None, ""])[1])
        eps.append({
            "id": f"GZ{m.group(1)}",
            "no": int(m.group(1)),
            "title": re.sub(r"^GZ\s*\d+\s*:\s*", "", title),
            "date": date.date().isoformat(),
            "url": f"https://www.geekzone.de/{path}/" if path else link,
            "bullets": re.findall(r"^\s*[-–•*]\s*(.+?)\s*$", desc, re.M),
        })
    return sorted(eps, key=lambda e: e["no"])


def _bullet_parts(bullet):
    """Bereinigt einen Themenpunkt und zerlegt ihn in Teilstücke [(tokens, jahr|None)]."""
    b = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", bullet)      # Markdown-Links
    b = re.sub(r"https?://\S+", "", b)                         # nackte URLs
    b = PEOPLE_PAREN.sub("", b)                                # (Peter, Sidekick: …)
    b = re.sub(r"\((?!\d{4}\))[^()]*\)", "", b)                # sonstige Klammern außer (1979)
    b = re.sub(r"\s+im\s+(?:Kino|Stream\w*)\b", "", b, flags=re.I)
    pieces = [b] + re.split(r"\s+[-–]\s+|,\s+|\s+&\s+|:\s+|\s+und\s+", b)
    out = []
    for p in pieces:
        t = tokens(p)
        yr = None
        if t and re.fullmatch(r"(19|20)\d\d", t[-1]):
            yr, t = int(t[-1]), t[:-1]
        if t:
            out.append((t, yr))
    return out


def _contains(hay, needle):
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def assign_episodes(films, year):
    eps = load_episodes(year)
    print(f"Feed: {len(eps)} Folgen aus {year}", file=sys.stderr)
    ftok = {f["slug"]: tokens(f["title"]) for f in films}
    hits = {f["slug"]: {} for f in films}  # slug -> {ep_id: "match"|"inferred"|"manual"}

    for ep in eps:
        for bullet in ep["bullets"]:
            if re.search(r"youtube\.com|youtu\.be", bullet):  # Trailer-/News-Links, keine Besprechung
                continue
            exact, partial = set(), set()
            for part, yr in _bullet_parts(bullet):
                for f in films:
                    t = ftok[f["slug"]]
                    if yr and f["year"] != yr:
                        continue
                    if t == part:
                        exact.add(f["slug"])
                    elif len(t) >= 2 and _contains(part, t):
                        partial.add(f["slug"])
            # "Evil Dead" nicht zusätzlich zu "Evil Dead Burn" zählen
            found = exact | {s for s in partial
                             if not any(o != s and len(ftok[o]) > len(ftok[s]) and _contains(ftok[o], ftok[s])
                                        for o in exact | partial)}
            for s in found:
                hits[s][ep["id"]] = "match"

    # Lücken über die (chronologische) Listenreihenfolge schließen:
    # liegen die nächsten erkannten Nachbarn in derselben Folge, gehört der Film vermutlich dazu.
    matched = [i for i, f in enumerate(films) if hits[f["slug"]]]
    for i, f in enumerate(films):
        if hits[f["slug"]]:
            continue
        prev = next((j for j in reversed(matched) if j < i), None)
        nxt = next((j for j in matched if j > i), None)
        if prev is None or nxt is None:
            continue
        common = set(hits[films[prev]["slug"]]) & set(hits[films[nxt]["slug"]])
        if common:
            hits[f["slug"]][min(common, key=lambda e: int(e[2:]))] = "inferred"

    # Manuelle Korrekturen: data/episode-overrides.json  {"slug": ["GZ121"], …}  ([] = keine Folge)
    ov_file = ROOT / "data" / "episode-overrides.json"
    if ov_file.exists():
        for slug, ids in json.loads(ov_file.read_text()).items():
            if slug in hits:
                hits[slug] = {i: "manual" for i in ids}

    by_id = {e["id"]: e for e in eps}
    for f in films:
        f["episodes"] = [
            {"id": i, "no": by_id[i]["no"] if i in by_id else int(i[2:]),
             "title": by_id.get(i, {}).get("title", ""), "url": by_id.get(i, {}).get("url"),
             "date": by_id.get(i, {}).get("date"), "how": how}
            for i, how in sorted(hits[f["slug"]].items(), key=lambda kv: int(kv[0][2:]))
        ]
    n_match = sum(1 for f in films if any(e["how"] == "match" for e in f["episodes"]))
    n_inf = sum(1 for f in films if f["episodes"] and all(e["how"] == "inferred" for e in f["episodes"]))
    missing = [f["title"] for f in films if not f["episodes"]]
    print(f"  Folgen zugeordnet: {n_match} per Titel, {n_inf} über Listenreihenfolge, {len(missing)} ohne", file=sys.stderr)
    for f in films:
        if f["episodes"] and all(e["how"] == "inferred" for e in f["episodes"]):
            print(f"    abgeleitet: {f['title']} ({f['year']}) → {f['episodes'][0]['id']}", file=sys.stderr)
    for t in missing:
        print(f"    ohne Folge: {t}", file=sys.stderr)
    return [{k: e[k] for k in ("id", "no", "title", "date", "url")} for e in eps]


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

    episodes = assign_episodes(films, args.year)

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
        "episodes": episodes,
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
