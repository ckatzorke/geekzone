#!/usr/bin/env python3
"""Kennzahlen für die Startseite aus dem Podcast-Feed (alle Folgen eines Jahres).

Aufruf:  python3 scripts/build_overview.py [--year 2026]
Ergebnis: data/overview-<jahr>.json/.js
"""
import argparse
import datetime as dt
import email.utils
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_letterboxd as lb  # noqa: E402

# Running Gags: Regex über die Folgentexte
GAGS = {
    "dinos": r"saurier|dinos?\b|dinosaurier|echsen",
    "strumpfhosen": r"strumpfhose",
    "partygewalt": r"partygewalt",
    "erotik": r"erotis|sexy|sex-",
    "horroctober": r"horroctober",
}
PEOPLE = {"Christian": r"\bchristian\b", "Fipps": r"\bfipp?s\b|\bphilipp", "Peter": r"\bpeter\b", "Tax": r"\btax\b"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    args = ap.parse_args()

    xml = lb.fetch(lb.FEED_URL, fresh=True)
    eps = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = html.unescape(re.search(r"<title>(.*?)</title>", it, re.S).group(1)).strip()
        date = email.utils.parsedate_to_datetime(re.search(r"<pubDate>(.*?)</pubDate>", it).group(1))
        if date.year != args.year:
            continue
        m = re.match(r"GZ\s*(\d+)\s*:\s*(?:(WATCH|PLAY|SPECIAL)\s*-\s*)?(.*)", title)
        if not m:
            continue
        dur = re.search(r"<itunes:duration>([\d:]+)</itunes:duration>", it)
        secs = 0
        if dur:
            for part in dur.group(1).split(":"):
                secs = secs * 60 + int(part)
        link = (re.search(r"<link>(.*?)</link>", it) or [None, ""])[1].strip()
        path = link.rstrip("/").rsplit("/", 1)[-1]
        desc = html.unescape((re.search(r"<description>(.*?)</description>", it, re.S) or [None, ""])[1])
        eps.append({
            "id": f"GZ{m.group(1)}", "no": int(m.group(1)),
            "type": (m.group(2) or "SPECIAL").upper(),
            "title": m.group(3).strip(),
            "date": date.date().isoformat(),
            "duration": secs,
            "url": f"https://www.geekzone.de/{path}/" if path else link,
            "_desc": desc,
        })
    eps.sort(key=lambda e: e["no"])

    gags = {k: {"count": sum(len(re.findall(p, e["_desc"], re.I)) for e in eps),
                "episodes": [e["id"] for e in eps if re.search(p, e["_desc"], re.I)]}
            for k, p in GAGS.items()}
    mentions = {k: sum(len(re.findall(p, e["_desc"], re.I)) for e in eps) for k, p in PEOPLE.items()}
    for e in eps:
        del e["_desc"]

    out = {
        "year": args.year,
        "generated": dt.date.today().isoformat(),
        "episodes": eps,
        "totalSeconds": sum(e["duration"] for e in eps),
        "byType": {t: sum(e["type"] == t for e in eps) for t in ("WATCH", "PLAY", "SPECIAL")},
        "gags": gags,
        "mentions": mentions,
    }
    dest = lb.ROOT / "data" / f"overview-{args.year}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    dest.with_suffix(".js").write_text(f"window.GZ_OVERVIEW = {json.dumps(out, ensure_ascii=False)};\n")
    h = out["totalSeconds"] / 3600
    print(f"{len(eps)} Folgen, {h:.1f} h, {out['byType']} → {dest.relative_to(lb.ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
