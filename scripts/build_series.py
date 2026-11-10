#!/usr/bin/env python3
"""Baut die Serien-Daten aus der Serializd-Liste der Geekzone.

Serializd blockiert automatische Abrufe (Cloudflare). Die Listendaten werden deshalb
im Browser gesichert (DevTools → Network → Antworten der Liste) und als
data/serializd/page0.json, page1.json, page2.json … abgelegt:
  - page0.json: Next.js-Seitendaten (Listen-Metadaten + erste Einträge)
  - page1.json …: listItems aller Seiten (page1 enthält die Einträge aus page0 erneut)

Aufruf:  python3 scripts/build_series.py [--year 2026]
Ergebnis: data/series-<jahr>.json/.js  (+ Poster unter assets/series/)
Folgen-Zuordnung wie bei den Filmen über den Podcast-Feed (Korrekturen: data/episode-overrides.json,
Schlüssel der Serien siehe Feld "slug" in data/series-<jahr>.json).
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_letterboxd as lb  # noqa: E402  (Feed, Folgen-Zuordnung, HTTP-Cache)

ROOT = lb.ROOT
SRC = ROOT / "data" / "serializd"
TMDB_IMG = "https://image.tmdb.org/t/p/w185"


def load_items():
    pages = sorted(SRC.glob("page*.json"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    if not pages:
        sys.exit(f"Keine Serializd-Daten unter {SRC.relative_to(ROOT)}/page*.json")
    meta, items = {}, {}
    for p in pages:
        d = json.loads(p.read_text(encoding="utf-8"))
        data = d.get("pageProps", {}).get("data")
        if data:  # page0: Next.js-Seitendaten
            meta = {k: data.get(k) for k in ("listId", "listName", "listDescription", "totalItems", "dateUpdated")}
            meta["owner"] = (data.get("owner") or {}).get("username")
        for it in (data or {}).get("listItems", []) + d.get("listItems", []):
            items[it["position"]] = it  # Position ist eindeutig → keine Doppelten
    return meta, [items[k] for k in sorted(items)]


def url_name(name):
    return re.sub(r"\s+", "-", re.sub(r"[:,.!?/()\"]", "", name).strip())


def poster(it):
    season = it.get("season") or {}
    path = season.get("posterPath") or it.get("bannerImage")
    if not path:
        return None
    dest = ROOT / "assets" / "series" / f"{it['showId']}-{it.get('seasonId') or 0}.jpg"
    if not dest.exists():
        data = lb.fetch(TMDB_IMG + path, binary=True)
        if data:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    return dest.relative_to(ROOT).as_posix() if dest.exists() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    args = ap.parse_args()

    meta, raw = load_items()
    print(f"Serializd-Liste '{meta.get('listName')}': {len(raw)} Einträge"
          + (f" (laut Liste {meta['totalItems']})" if meta.get("totalItems") else ""), file=sys.stderr)
    if meta.get("totalItems") and meta["totalItems"] != len(raw):
        print("WARNUNG: Anzahl weicht ab – fehlt eine page*.json?", file=sys.stderr)

    series = []
    for it in raw:
        season = it.get("season") or {}
        sn = season.get("seasonNumber")
        show_url = f"https://www.serializd.com/show/{url_name(it['showName'])}-{it['showId']}"
        series.append({
            "slug": f"serie-{it['showId']}" + (f"-s{sn}" if sn is not None else ""),
            "title": it["showName"],
            "year": None,  # kein Jahresfilter beim Folgen-Abgleich
            "season": sn,
            "seasonName": season.get("name"),
            "episodeCount": season.get("episodeCount"),
            "premiere": season.get("airDate") or it.get("premiereDate"),
            "added": it.get("dateAdded"),
            "position": it["position"],
            # Serializd speichert 0–10; angezeigt wird wie bei Letterboxd auf 5 Sterne
            "rating": round(it["averageRating"] / 2, 2) if it.get("averageRating") is not None else None,
            "url": f"{show_url}/season/{it['seasonId']}/{sn}" if sn is not None and it.get("seasonId") else show_url,
            "poster": poster(it),
        })

    episodes = lb.assign_episodes(series, args.year)

    # Serializd kennt das Hinzufügedatum: Einträge, die am selben Tag auf die Liste kamen,
    # stammen meist aus derselben Folge → für noch fehlende Zuordnungen nutzen.
    by_day = {}
    for s in series:
        for e in s["episodes"]:
            if e["how"] == "match":
                by_day.setdefault((s["added"] or "")[:10], []).append(e)
    for s in series:
        cands = by_day.get((s["added"] or "")[:10]) if not s["episodes"] else None
        if cands:
            best = max({e["id"] for e in cands}, key=lambda i: (sum(e["id"] == i for e in cands), -int(i[2:])))
            e = next(e for e in cands if e["id"] == best)
            s["episodes"] = [{**e, "how": "inferred"}]
            print(f"    abgeleitet (gleiches Hinzufügedatum): {s['title']} → {best}", file=sys.stderr)

    out = {
        "year": args.year,
        "listName": meta.get("listName"),
        "listUrl": f"https://www.serializd.com/list/{meta.get('listId') or ''}",
        "generated": dt.date.today().isoformat(),
        "count": len(series),
        "shows": len({s["slug"].split("-s")[0] for s in series}),
        "episodes": episodes,
        "series": series,
    }
    dest = ROOT / "data" / f"series-{args.year}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    dest.with_suffix(".js").write_text(f"window.GZ_SERIES = {json.dumps(out, ensure_ascii=False)};\n")
    print(f"→ {dest.relative_to(ROOT)}, {dest.with_suffix('.js').relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
