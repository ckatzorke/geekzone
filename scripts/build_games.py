#!/usr/bin/env python3
"""Baut die Spiele-Daten aus der Backloggd-Liste der Geekzone.

Backloggd blockiert automatische Abrufe (Bot-Check). Die Liste wird deshalb im Browser
als HTML gespeichert ("Webseite, nur HTML") und unter data/backloggd/*.html abgelegt.
Mehrere Dateien (bei mehrseitigen Listen) werden in Dateinamen-Reihenfolge eingelesen.

Aufruf:  python3 scripts/build_games.py [--year 2026]
Ergebnis: data/games-<jahr>.json/.js  (+ Cover unter assets/games/)
Folgen-Zuordnung über die PLAY-Folgen im Podcast-Feed (Korrekturen: data/episode-overrides.json,
Schlüssel = Backloggd-Slug, z.B. "nioh-3").
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_letterboxd as lb  # noqa: E402  (Feed, Folgen-Zuordnung, HTTP-Cache)

ROOT = lb.ROOT
SRC = ROOT / "data" / "backloggd"
BASE = "https://backloggd.com"


def load_entries():
    files = sorted(SRC.glob("*.html"))
    if not files:
        sys.exit(f"Keine Backloggd-Seiten unter {SRC.relative_to(ROOT)}/*.html")
    meta, entries = {}, []
    for f in files:
        h = f.read_text(encoding="utf-8")
        if not meta:
            t = re.search(r'<h1 class="mb-0">(.*?)</h1>', h, re.S)
            n = re.search(r'subtitle-text">\s*(\d+)\s+Games', h)
            owner = re.search(r'href="/u/([^/"]+)/list/([^/"]+)/', h)
            meta = {
                "listName": html.unescape(t.group(1).strip()) if t else None,
                "total": int(n.group(1)) if n else None,
                "listUrl": f"{BASE}/u/{owner.group(1)}/list/{owner.group(2)}/" if owner else None,
            }
        for part in h.split('grid-list-entry"')[1:]:
            slug = re.search(r'href="/games/([^"/]+)/"', part)
            if not slug:
                continue
            img = re.search(r'<img class="card-img[^"]*" src="([^"]+)"', part)
            alt = re.search(r'<img [^>]*alt="([^"]*)"', part)
            name = re.search(r'game-text-centered">(.*?)</div>', part, re.S)
            title = html.unescape((alt.group(1) if alt else "") or (name.group(1).strip() if name else slug.group(1)))
            entries.append({"slug": slug.group(1), "title": title, "cover": img.group(1) if img else None})
    return meta, entries


def aliases(title):
    """Kurzformen, wie sie in Folgentexten vorkommen: ohne Untertitel, ohne 'Marvel's ' etc."""
    out = set()
    t = re.sub(r"^(?:Marvel's|Clive Barker's|Tom Clancy's|Sid Meier's)\s+", "", title)
    out.add(t)
    if ":" in t:
        head, tail = t.split(":", 1)
        out.update({head.strip(), tail.strip()})
    out.add(re.sub(r"\s+(?:Remastered|Remake|Enhanced|Legacy Edition|Definitive Edition)\b.*$", "", t))
    out.add(re.sub(r"\s+-\s+.*$", "", t))
    out.discard(title)
    return sorted(a for a in out if a)


def cover(slug, src):
    if not src:
        return None
    dest = ROOT / "assets" / "games" / f"{slug}.jpg"
    if not dest.exists():
        data = lb.fetch(src, binary=True)
        if data:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    return dest.relative_to(ROOT).as_posix() if dest.exists() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    args = ap.parse_args()

    meta, entries = load_entries()
    print(f"Backloggd-Liste '{meta.get('listName')}': {len(entries)} Einträge"
          + (f" (laut Liste {meta['total']})" if meta.get("total") else ""), file=sys.stderr)
    if meta.get("total") and meta["total"] != len(entries):
        print("WARNUNG: Anzahl weicht ab – fehlt eine Seite der Liste?", file=sys.stderr)

    # Jeder Listeneintrag einzeln (Reihenfolge = chronologisch), Doppelte bekommen eigene Schlüssel
    seen = {}
    items = []
    for pos, e in enumerate(entries):
        k = seen.get(e["slug"], 0)
        seen[e["slug"]] = k + 1
        items.append({"slug": e["slug"] if k == 0 else f"{e['slug']}@{k}", "game": e["slug"], "title": e["title"],
                      "year": None, "aliases": aliases(e["title"]), "position": pos, "coverSrc": e["cover"]})

    episodes = lb.assign_episodes(items, args.year, play=True)

    # Doppelte Einträge zu einem Spiel zusammenführen (alle Folgen, erste Listenposition)
    games = {}
    for it in items:
        g = games.get(it["game"])
        if not g:
            g = games[it["game"]] = {
                "slug": it["game"], "title": it["title"], "position": it["position"], "entries": 0,
                "url": f"{BASE}/games/{it['game']}/", "cover": cover(it["game"], it["coverSrc"]), "episodes": [],
            }
        g["entries"] += 1
        for e in it["episodes"]:
            old = next((x for x in g["episodes"] if x["id"] == e["id"]), None)
            if not old:
                g["episodes"].append(dict(e))
            elif old["how"] == "inferred" and e["how"] != "inferred":
                old["how"] = e["how"]
    ov_file = ROOT / "data" / "episode-overrides.json"
    overrides = json.loads(ov_file.read_text()) if ov_file.exists() else {}
    by_id = {e["id"]: e for e in episodes}
    for g in games.values():
        if g["slug"] in overrides:
            g["episodes"] = [{"id": i, "no": int(i[2:]), "title": by_id.get(i, {}).get("title", ""),
                              "url": by_id.get(i, {}).get("url"), "date": by_id.get(i, {}).get("date"), "how": "manual"}
                             for i in overrides[g["slug"]]]
        g["episodes"].sort(key=lambda e: e["no"])

    out = {
        "year": args.year,
        "listName": meta.get("listName"),
        "listUrl": meta.get("listUrl"),
        "generated": dt.date.today().isoformat(),
        "entries": len(items),
        "count": len(games),
        "episodes": episodes,
        "games": sorted(games.values(), key=lambda g: g["position"]),
    }
    dest = ROOT / "data" / f"games-{args.year}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    dest.with_suffix(".js").write_text(f"window.GZ_GAMES = {json.dumps(out, ensure_ascii=False)};\n")
    print(f"{len(games)} Spiele → {dest.relative_to(ROOT)}, {dest.with_suffix('.js').relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
