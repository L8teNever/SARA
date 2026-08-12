#!/usr/bin/env python3
"""SARA -- Stories per Kommandozeile importieren.

Nimmt eine oder mehrere JSON-Dateien (oder ein Verzeichnis voller
JSON-Dateien) entgegen, jede im SARA-Story-Format (title, total_parts,
keywords, parts[]), und importiert sie ueber die bestehende
/api/import-story-Route -- ohne Umweg ueber die GUI.

Beispiele:
    python import_stories.py stories/*.json
    python import_stories.py stories_ordner/
    python import_stories.py eine_story.json --url https://sara.deinedomain.com
    python import_stories.py stories/*.json --no-queue   # nur importieren, nicht produzieren

Standard-URL ist http://localhost:7842 (lokal) -- mit --url auf eine andere
SARA-Instanz zeigen, z.B. die interne WireGuard-Adresse oder die oeffentliche
Domain (dann muss der Zugriff von aussen erreichbar sein, z.B. ohne
Cloudflare Access davor, oder von einem Rechner mit gueltiger Session).
"""
import argparse
import glob
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path


def _collect_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        elif any(ch in p for ch in "*?[]"):
            files.extend(sorted(Path(g) for g in glob.glob(p)))
        elif path.is_file():
            files.append(path)
        else:
            print(f"[WARN] nicht gefunden, wird uebersprungen: {p}")
    return files


def import_story(url: str, path: Path, auto_queue: bool) -> tuple[bool, str]:
    story = json.loads(path.read_text(encoding="utf-8"))
    for field in ("title", "total_parts", "keywords", "parts"):
        if field not in story:
            return False, f"Feld '{field}' fehlt im JSON"

    payload = json.dumps({
        "json_text": json.dumps(story),
        "auto_queue": auto_queue,
    }).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/api/import-story",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("success"):
            return True, f"code={body['code']}  {story['title']}"
        return False, str(body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        if e.code == 409:
            return False, f"Duplikat: {detail}"
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return False, str(e)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="JSON-Dateien, Glob-Muster oder ein Verzeichnis")
    parser.add_argument("--url", default="http://localhost:7842", help="Basis-URL von SARA (Standard: http://localhost:7842)")
    parser.add_argument("--no-queue", action="store_true", help="Nur importieren, nicht automatisch zur Produktion einreihen")
    args = parser.parse_args()

    files = _collect_files(args.paths)
    if not files:
        print("Keine JSON-Dateien gefunden.")
        return 1

    ok = 0
    for f in files:
        success, msg = import_story(args.url, f, auto_queue=not args.no_queue)
        status = "OK  " if success else "FAIL"
        print(f"{status} {f.name:30s} {msg}")
        ok += success

    print(f"\n{ok}/{len(files)} Stories erfolgreich importiert.")
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
