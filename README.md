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
- **YouTube-Shorts-Upload** — ein oder mehrere Google-Konten verbinden, fertige Videos werden automatisch (mit einstellbarer Pause zwischen den Uploads, damit es nicht wie automatisiertes Massen-Posting wirkt) als YouTube Shorts hochgeladen — gleichzeitig auf allen aktiven Kanälen
- **Upload-Tracking** — TikTok / Instagram / YouTube mit Datum abhaken (YouTube-Uploads werden bei automatischem Hochladen selbst eingetragen)
- **Prompt-Builder** — vorgefertigter Prompt für ChatGPT/Gemini/Claude ohne API-Key
- **SPA-Routing** — jede Seite hat eine eigene URL (`/story`, `/video`, `/library`, `/upload`, `/channels`, `/prompt`)
- **Material Design 3** — dunkles Theme, Navigation Rail, flüssige Seitenübergänge

---

## Schnellstart mit Docker

### 1. `.env`-Datei anlegen

```bash
cp .env.example .env
# ANTHROPIC_API_KEY eintragen (nur noetig fuer /api/generate-story, siehe unten)
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
├── main.py               # Flask-Backend + Video-Pipeline + YouTube-Upload
├── templates/
│   ├── base.html          # Shell, Navigation, gemeinsames JS
│   └── pages/
│       ├── story.html     # Story erstellen (Prompt-Builder + Import)
│       ├── video.html     # Produktions-Warteschlange
│       ├── library.html   # Fertige Videos + Upload-Tracking
│       ├── upload.html    # Hintergrundvideos hochladen
│       └── channels.html  # YouTube-Konten verbinden + Upload-Einstellungen
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
1. Story erstellen  →  Prompt kopieren, in einer KI einfügen, JSON-Antwort importieren
                        (oder: Claude AI generiert direkt über /api/generate-story)
2. Story speichern  →  wird in SQLite gespeichert
3. Video erstellen  →  Warteschlange + Live-Fortschritt via SSE
4. YouTube-Upload    →  optional automatisch: fertige Videos gehen zeitversetzt
                        an alle verbundenen, aktiven YouTube-Kanäle raus
5. Bibliothek        →  Videos ansehen, herunterladen, Caption kopieren, Upload abhaken
```

---

## YouTube-Upload einrichten

Der "YouTube"-Tab erlaubt es, ein oder mehrere Google-Konten per OAuth zu verbinden. Fertige Videos werden dann automatisch als YouTube Shorts hochgeladen — mit einstellbarer Pause zwischen den einzelnen Uploads (Standard: 5 Minuten), damit es nicht nach automatisiertem Massen-Posting aussieht. Bei mehreren verbundenen Kanälen bekommt jeder Kanal eine eigene Kopie, zeitlich versetzt nacheinander statt alle gleichzeitig.

Ohne verbundenen Kanal bleibt der Rest der App unverändert nutzbar — es passiert einfach nichts Automatisches.

**Einrichtung in der [Google Cloud Console](https://console.cloud.google.com):**

1. Neues Projekt anlegen (oder ein bestehendes verwenden).
2. **YouTube Data API v3** aktivieren (APIs & Dienste → Bibliothek).
3. OAuth-Zustimmungsbildschirm einrichten, Nutzertyp **Extern**. Der Status **Testing** reicht für den persönlichen Gebrauch aus — dort unter "Testnutzer" die eigenen Google-Konten eintragen, die verbunden werden sollen (bis zu 100 möglich, keine Google-Prüfung nötig, da nur selbst hinzugefügte Test-Nutzer den Zugriff bekommen).
4. OAuth-Client-ID anlegen (Typ **Webanwendung**), als autorisierte Weiterleitungs-URI genau `https://DEINE-DOMAIN/auth/youtube/callback` eintragen.
5. Client-ID und Client-Secret in die `.env` eintragen (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`), `OAUTH_REDIRECT_BASE` auf die öffentliche Basis-URL setzen.
6. Container neu starten, im "YouTube"-Tab auf "Google-Konto verbinden" klicken.

Jedes weitere Google-Konto (z.B. ein zweiter Kanal) wird genauso über denselben Button verbunden — solange es als Testnutzer im Zustimmungsbildschirm eingetragen ist.

---

## Umgebungsvariablen

| Variable              | Beschreibung                                                              | Pflicht |
|------------------------|----------------------------------------------------------------------------|---------|
| `ANTHROPIC_API_KEY`    | Claude AI API-Key — nur für `/api/generate-story`, nicht für den normalen Prompt-Builder-Workflow | Nein |
| `PORT`                 | Server-Port (Standard: `7842`)                                            | Nein    |
| `GOOGLE_CLIENT_ID`     | OAuth-Client-ID aus der Google Cloud Console — für YouTube-Upload         | Nein (nur für YouTube-Upload) |
| `GOOGLE_CLIENT_SECRET` | OAuth-Client-Secret aus der Google Cloud Console                          | Nein (nur für YouTube-Upload) |
| `OAUTH_REDIRECT_BASE`  | Öffentliche Basis-URL, z.B. `https://sara.deinedomain.com`                | Nein (nur für YouTube-Upload) |
| `SECRET_KEY`           | Geheimer Wert für Flask-Sessions während des OAuth-Logins                 | Nein (Zufallswert bei jedem Start reicht) |

---

## Technologie

| Komponente | Technologie |
|------------|-------------|
| Backend    | Python 3.11 · Flask |
| KI         | Anthropic Claude (claude-sonnet-4-6) |
| TTS        | Edge-TTS (en-US-AriaNeural) · gTTS-Fallback |
| Video      | FFmpeg · ASS-Untertitel |
| YouTube    | Google API Client · OAuth2 (google-auth-oauthlib) · YouTube Data API v3 |
| Frontend   | Vanilla JS · Material Design 3 · Material Symbols |
| Datenbank  | SQLite (WAL-Modus) |
| Container  | Docker · GitHub Container Registry (ghcr.io) |

---

## Lizenz

MIT
