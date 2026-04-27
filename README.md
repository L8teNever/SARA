# SARA — Story And Reel Automator

SARA generiert vollautomatisch TikTok- & Reels-Videos aus Reddit-Storys.
Aus einem einzigen Prompt entsteht eine mehrteilige Geschichte mit KI-Stimme, Wort-Highlighting-Untertiteln und Social-Media-Metadaten (Titel, Caption, Hashtags).

---

## Features

- **KI-Story-Generator** — Claude AI schreibt authentische Reddit-Storys (160–200 Wörter pro Teil, min. 75 Sek. Audio)
- **Automatische Untertitel** — Wort-für-Wort-Highlighting: das aktive Wort bleibt immer an derselben Bildschirmposition (gelb, zentriert)
- **Edge-TTS-Stimme** — natürliche, emotionale Erzählung (en-US-AriaNeural)
- **Hintergrundvideo-Loop** — zufällige Subway-Surfer/Minecraft-Videos als Hintergrund
- **Social-Media-Daten** — pro Video-Teil: TikTok-Titel, Caption, Hashtags direkt kopierbar
- **Upload-Tracking** — TikTok / Instagram / YouTube mit Datum abhaken
- **Prompt-Builder** — vorgefertigter Prompt für ChatGPT/Gemini ohne API-Key
- **SPA-Routing** — jede Seite hat eine eigene URL (`/story`, `/video`, `/library`, `/upload`, `/prompt`)
- **Material Design 3** — dunkles Theme, Navigation Rail, flüssige Seitenübergänge

---

## Schnellstart mit Docker

### 1. `.env`-Datei anlegen

```bash
cp .env.example .env
# ANTHROPIC_API_KEY eintragen
```

### 2. Container starten

```bash
# Image von GitHub Container Registry ziehen (kein Build nötig):
docker compose pull
docker compose up -d
```

SARA ist dann unter **http://localhost:7842** erreichbar.

---

## Selbst bauen (optional)

```bash
docker compose -f docker-compose.build.yml up -d --build
```

---

## Lokale Entwicklung (ohne Docker)

```bash
pip install -r requirements.txt
# FFmpeg muss im PATH sein
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

---

## Verzeichnisstruktur

```
SARA/
├── main.py               # Flask-Backend + Video-Pipeline
├── templates/
│   └── index.html        # SPA-Frontend (Material Design 3)
├── Dockerfile
├── docker-compose.yml    # Produktion (GHCR-Image)
├── docker-compose.build.yml  # Lokaler Build
├── requirements.txt
├── .env.example
└── data/                 # ← wird als Docker-Volume gemountet
    ├── sara.db           # SQLite-Datenbank
    ├── backgrounds/      # Hochgeladene Hintergrundvideos
    ├── outputs/          # Fertige Story-Videos
    ├── covers/           # Cover-Bilder
    └── tts/              # Temporäre TTS-Dateien
```

---

## Workflow

```
1. Story erstellen  →  Claude AI generiert Geschichte + Social-Daten
2. Story speichern  →  wird in SQLite gespeichert
3. Video erstellen  →  Warteschlange + Live-Fortschritt via SSE
4. Bibliothek       →  Videos ansehen, herunterladen, Caption kopieren, Upload abhaken
```

---

## Umgebungsvariablen

| Variable            | Beschreibung                       | Pflicht |
|---------------------|------------------------------------|---------|
| `ANTHROPIC_API_KEY` | Claude AI API-Key                  | Ja (für KI-Generator) |
| `PORT`              | Server-Port (Standard: `7842`)     | Nein    |

---

## Technologie

| Komponente | Technologie |
|------------|-------------|
| Backend    | Python 3.11 · Flask |
| KI         | Anthropic Claude (claude-sonnet-4-6) |
| TTS        | Edge-TTS (en-US-AriaNeural) · gTTS-Fallback |
| Video      | FFmpeg · ASS-Untertitel |
| Frontend   | Vanilla JS · Material Design 3 · Material Symbols |
| Datenbank  | SQLite (WAL-Modus) |
| Container  | Docker · GitHub Container Registry (ghcr.io) |

---

## Lizenz

MIT
