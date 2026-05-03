"""
SARA — Story And Reel Automator
Flask-Backend: Story-Generierung, Video-Erstellung, Warteschlange

Neu (v2):
 - 9:16 Hochformat (1080×1920)
 - Cover-Bild für jede Story (einheitliches Design)
 - Edge-TTS für natürliche Stimme (Fallback: gTTS)
 - Wort-Highlighting via ASS-Untertitel (aktives Wort = gelb)
 - Nahtloser Hintergrundvideo-Loop (zufälliger Wechsel bei Videoende)
 - SSE-basierter Job-Progress (Live-Status auf der Website)
"""

import os
import re
import json
import uuid
import random
import string
import sqlite3
import asyncio
import threading
import subprocess
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

# Globale Liste für aktive Subprozesse (FFmpeg etc.)
_ACTIVE_SUBPROCESSES = []
def _run_sub(args, **kwargs):
    import subprocess
    # capture_output auf Popen-Argumente umbiegen
    if kwargs.pop('capture_output', False):
        kwargs['stdout'] = subprocess.PIPE
        kwargs['stderr'] = subprocess.PIPE
    
    check = kwargs.pop('check', False)
    
    p = subprocess.Popen(args, **kwargs)
    _ACTIVE_SUBPROCESSES.append(p)
    try:
        out, err = p.communicate()
        if check and p.returncode != 0:
            raise subprocess.CalledProcessError(p.returncode, args, out, err)
        return subprocess.CompletedProcess(args, p.returncode, out, err)
    finally:
        if p in _ACTIVE_SUBPROCESSES:
            _ACTIVE_SUBPROCESSES.remove(p)

def _kill_active_subs():
    import signal
    import os
    while _ACTIVE_SUBPROCESSES:
        p = _ACTIVE_SUBPROCESSES.pop()
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)], capture_output=True)
            else:
                p.terminate()
        except: pass


from flask import (
    Flask, request, jsonify, render_template,
    send_from_directory, abort, Response, stream_with_context
)
import anthropic

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
BASE_DIR        = Path(__file__).parent
DATA_DIR        = BASE_DIR / "data"
BACKGROUNDS_DIR = DATA_DIR / "backgrounds"
OUTPUTS_DIR     = DATA_DIR / "outputs"
TTS_DIR         = DATA_DIR / "tts"
COVERS_DIR      = DATA_DIR / "covers"
DB_PATH         = DATA_DIR / "sara.db"

# Video-Format: 9:16 Hochformat (TikTok/Reels)
VIDEO_WIDTH     = 1080
VIDEO_HEIGHT    = 1920

ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "avi"}
MAX_WORDS_PER_PART = 200
QUEUE_POLL_INTERVAL = 2  # Sekunden zwischen Queue-Durchläufen

# Globaler Job-Progress (story_part_id → {step, total_steps, label})
_job_progress: dict = {}
_progress_lock = threading.Lock()

# ---------------------------------------------------------------------------
# FFmpeg / FFprobe — automatische Pfaderkennung (Windows + Docker)
# ---------------------------------------------------------------------------
def _find_ffmpeg() -> str:
    import shutil
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"

FFMPEG_EXE = _find_ffmpeg()

# Lokale TTS-Konfiguration
# Wir nutzen Piper-TTS für 100% lokale Sprachgenerierung.

# ---------------------------------------------------------------------------
# Schrift für drawtext — plattformübergreifend
# ---------------------------------------------------------------------------
def _find_font() -> str:
    """Sucht eine passende TTF-Schrift auf dem aktuellen Betriebssystem."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return ""

DRAWTEXT_FONT = _find_font()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB Upload-Limit

# ---------------------------------------------------------------------------
# Datenbank — Initialisierung & Hilfsfunktionen
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    # WAL-Modus aktivieren für bessere Nebenläufigkeit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn

@contextmanager
def db_session():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_session() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stories (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                code         TEXT    NOT NULL UNIQUE,
                title        TEXT    NOT NULL,
                keywords_json TEXT   NOT NULL,
                total_parts  INTEGER NOT NULL,
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS story_parts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id     INTEGER NOT NULL REFERENCES stories(id),
                part_number  INTEGER NOT NULL,
                text         TEXT    NOT NULL,
                cliffhanger  TEXT,
                video_path   TEXT,
                cover_path   TEXT,
                status       TEXT    NOT NULL DEFAULT 'pending',
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS queue (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                story_part_id   INTEGER NOT NULL REFERENCES story_parts(id),
                status          TEXT    NOT NULL DEFAULT 'pending',
                error_msg       TEXT,
                progress_label  TEXT    DEFAULT '',
                progress_pct    INTEGER DEFAULT 0,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                started_at      TEXT,
                scheduled_at    TEXT,
                finished_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS backgrounds (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                filename      TEXT    NOT NULL UNIQUE,
                original_name TEXT    NOT NULL,
                uploaded_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS video_uploads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                story_part_id   INTEGER NOT NULL REFERENCES story_parts(id),
                platform        TEXT    NOT NULL DEFAULT 'tiktok',
                uploaded_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                notes           TEXT    DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            INSERT OR IGNORE INTO settings (key, value) VALUES ('prod_interval_min', '0');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('prod_slots', ''); 
            INSERT OR IGNORE INTO settings (key, value) VALUES ('prod_active_windows', '');
            INSERT OR IGNORE INTO settings (key, value) VALUES ('prod_active', '1');
        """)
        
        # NEU: Beim Start alle "processing" Jobs zurücksetzen, falls der Server abgestürzt ist
        conn.execute("UPDATE queue SET status = 'pending', progress_label = 'Wartend (Restart)...' WHERE status = 'processing'")
        conn.execute("UPDATE story_parts SET status = 'pending' WHERE status = 'processing'")
        
        # Migration: cover_path Spalte falls noch nicht vorhanden
        try:
            conn.execute("ALTER TABLE story_parts ADD COLUMN cover_path TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE queue ADD COLUMN progress_label TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE queue ADD COLUMN progress_pct INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE queue ADD COLUMN started_at TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE queue ADD COLUMN scheduled_at TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE story_parts ADD COLUMN social_json TEXT")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def generate_story_code() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(chars, k=6))
        with db_session() as conn:
            row = conn.execute("SELECT id FROM stories WHERE code = ?", (code,)).fetchone()
        if row is None:
            return code


def check_duplicate(keywords: list) -> dict:
    new_set = set(k.lower() for k in keywords)
    with db_session() as conn:
        rows = conn.execute("SELECT id, code, title, keywords_json FROM stories").fetchall()
    for row in rows:
        existing = set(k.lower() for k in json.loads(row["keywords_json"]))
        overlap = len(new_set & existing)
        if overlap >= 6:
            return {
                "is_duplicate": True,
                "similar_story": {"id": row["id"], "code": row["code"], "title": row["title"]},
                "overlap": overlap,
            }
    return {"is_duplicate": False, "similar_story": None, "overlap": 0}


def allowed_video(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_VIDEO_EXTENSIONS


def _set_progress(job_id: int, pct: int, label: str):
    """Aktualisiert den Fortschritt eines Jobs in der DB."""
    try:
        with db_session() as conn:
            conn.execute(
                "UPDATE queue SET progress_pct = ?, progress_label = ? WHERE id = ?",
                (pct, label, job_id)
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# AI — Story-Generierung via Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a creative storyteller specializing in Reddit-style personal stories for TikTok/Reels.
Generate compelling, emotional, and suspenseful stories that feel authentic and real.
Stories must be written in English and follow this EXACT JSON format — no text before or after:
{
  "title": "Story Title",
  "total_parts": <number>,
  "keywords": ["word1","word2","word3","word4","word5","word6","word7","word8","word9","word10"],
  "parts": [
    {
      "part_number": 1,
      "text": "Text of part 1 — MUST be 200 to 250 words...",
      "cliffhanger_hint": "brief hint of what comes next",
      "social": {
        "video_title": "Catchy TikTok/Reels title with Part 1/2 — max 80 chars",
        "description": "2-3 sentence caption that teases the story emotionally without spoiling it. End with a question or call-to-action.",
        "hashtags": "#storytime #reddit #viral #relationship #foryou #fyp #drama"
      }
    },
    ...
  ]
}

Rules for Content:
- Hook the audience within the very first sentence with a MASSIVE attention grabber. It must be shocking, emotional, or mysterious (e.g. "I thought my husband was at work, until I saw his car parked at my best friend's house.").
- Every part (except the last) MUST end at the absolute peak of tension.
- IMPORTANT: Every part that is not the final one MUST end with a verbal call-to-action integrated into the story text (e.g. "... and as I opened the door, my heart stopped. See what happened next in Part 2.").
- For Part 2 and beyond, start with a quick 1-sentence recap or continuation that keeps the momentum (e.g. "So, there I was, staring at the person I trusted most...").
- Be extremely creative and original. Avoid generic or repetitive tropes. Every story should have a unique plot twist or a perspective that haven't been heard a thousand times before. Explore a wide variety of themes: hidden family secrets, mysterious disappearances, complex revenge plots, heartwarming but shocking reunions, or psychological workplace drama.
- Each part MUST contain exactly 200–250 words (this guarantees at least 85–100 seconds of audio per part).
- Write in first person, past tense, raw and emotional style (like r/TIFU or r/relationships).
- keywords: genau 10 englische Stichwörter, die den Kerninhalt der Geschichte beschreiben. Diese werden für den Duplikat-Check genutzt.
- social.video_title: punchy and attention-grabbing, include "Part X/Y" for multi-part, max 80 chars.
- social.description: emotionally tease the story, never spoil the ending, end with a question or CTA.
- social.hashtags: 6–8 relevant hashtags as a single space-separated string."""


def generate_story(prompt: str, api_key: str = "") -> dict:
    key = api_key.strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ValueError("Kein API-Key angegeben. Trage deinen Anthropic-Key in den Einstellungen ein oder setze ANTHROPIC_API_KEY.")
    client = anthropic.Anthropic(api_key=key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Create a story about: {prompt}"}],
    )
    raw = message.content[0].text.strip()
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        raise ValueError(f"Claude hat kein gültiges JSON zurückgegeben: {raw[:200]}")
    return json.loads(json_match.group())


# ---------------------------------------------------------------------------
# TTS — Piper-TTS (100% lokal, offline, hohe Qualität)
# ---------------------------------------------------------------------------

def ensure_kokoro_model() -> tuple[Path, Path]:
    import urllib.request
    model_dir = DATA_DIR / "kokoro_models"
    model_dir.mkdir(parents=True, exist_ok=True)
    
    # Kokoro-82M v1.0 ONNX (int8 quantized for speed)
    onnx_path = model_dir / "kokoro-v1.0.int8.onnx"
    voices_path = model_dir / "voices-v1.0.bin"
    
    if not onnx_path.exists():
        print("Lade Kokoro-82M ONNX Modell herunter...")
        urllib.request.urlretrieve(
            "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx", 
            onnx_path
        )
    if not voices_path.exists():
        print("Lade Kokoro Stimmen-Datei herunter...")
        urllib.request.urlretrieve(
            "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin", 
            voices_path
        )
    
    return onnx_path, voices_path


def tts_to_file(text: str, output_path: Path) -> Path:
    """
    Lokale Sprachsynthese mit Kokoro-82M (ONNX).
    """
    try:
        from kokoro_onnx import Kokoro
        import soundfile as sf
    except ImportError:
        raise RuntimeError("Bitte 'pip install kokoro-onnx soundfile' ausführen.")
        
    onnx_path, voices_path = ensure_kokoro_model()
    kokoro = Kokoro(str(onnx_path), str(voices_path))
    
    # Stimmen: 'af_heart' (weiblich, herzlich), 'af_bella' (weiblich, klar), 
    # 'am_adam' (männlich, tief), 'bm_george' (männlich, britisch)
    samples, sample_rate = kokoro.create(
        text,
        voice="af_heart",
        speed=1.0,
        lang="en-us"
    )
    
    sf.write(str(output_path), samples, sample_rate)
    return output_path


# ---------------------------------------------------------------------------
# Timing & Dauer-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _get_duration(file_path: Path) -> float:
    result = subprocess.run(
        [FFMPEG_EXE, "-i", str(file_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr_text = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", stderr_text)
    if not match:
        raise ValueError(f"Konnte Dauer nicht ermitteln für: {file_path}")
    h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
    return h * 3600 + m * 60 + s


def get_audio_duration(audio_path: Path) -> float:
    return _get_duration(audio_path)


def get_video_duration(video_path: Path) -> float:
    return _get_duration(video_path)


# ---------------------------------------------------------------------------
# Word-Timing: Wort-Highlighting via Edge-TTS WordBoundary + ASS-Untertitel
# ---------------------------------------------------------------------------

def build_word_timed_ass(text: str, output_path: Path, audio_path: Path) -> Path:
    """
    Erstellt eine ASS-Untertiteldatei mit Wort-Highlighting.
    Aktiv gesprochenes Wort = gelb, Rest = weiß.
    Nutzt Edge-TTS WordBoundary-Events für exaktes Timing.
    Fallback: gleichmäßige Aufteilung.
    """
    word_events = _get_word_boundaries(text, audio_path)
    ass_content = _build_ass_from_events(word_events, text)
    output_path.write_text(ass_content, encoding="utf-8")
    return output_path


_WHISPER_MODEL = None

def _get_word_boundaries(text: str, audio_path: Path) -> list:
    """
    Analysiert das lokal generierte Audio mit Faster-Whisper, um präzise Wort-Zeitstempel 
    (Start/Ende) zu erhalten. Da wir den echten Text kennen, wird dieser als Referenz 
    genutzt und perfekt auf die Audio-Zeiten gemappt.
    """
    global _WHISPER_MODEL
    try:
        from faster_whisper import WhisperModel
        if _WHISPER_MODEL is None:
            print("Lade lokales Whisper-Modell zur exakten Untertitel-Synchronisation...")
            # Wir nutzen das tiny Modell auf der CPU (schnell und ausreichend für klaren TTS-Sound)
            _WHISPER_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")
            
        print("Synchronisiere Untertitel mit der Audiospur...")
        # Durch initial_prompt hilft der Originaltext Whisper, exakt die gleichen Worte zu erkennen
        segments, _ = _WHISPER_MODEL.transcribe(str(audio_path), word_timestamps=True, initial_prompt=text)
        
        whisper_words = []
        for segment in segments:
            for word in segment.words:
                if word.word.strip():
                    whisper_words.append(word)
                    
        if not whisper_words:
            return _uniform_word_timing(text, audio_path)
            
        original_words = text.split()
        aligned_events = []
        w_idx = 0
        
        for orig_w in original_words:
            orig_clean = "".join(c for c in orig_w.lower() if c.isalnum())
            if not orig_clean:
                # Z.B. nur Satzzeichen. Übernimmt Endzeitpunkt des letzten Wortes.
                start = aligned_events[-1][2] if aligned_events else 0.0
                aligned_events.append((orig_w, start, start + 0.1))
                continue
                
            found = False
            # Suche in den nächsten erkannten Wörtern nach einem Match
            for i in range(w_idx, min(w_idx + 4, len(whisper_words))):
                whisp_clean = "".join(c for c in whisper_words[i].word.lower() if c.isalnum())
                if whisp_clean and (whisp_clean in orig_clean or orig_clean in whisp_clean):
                    aligned_events.append((orig_w, whisper_words[i].start, whisper_words[i].end))
                    w_idx = i + 1
                    found = True
                    break
                    
            if not found:
                # Fallback, wenn das Wort übersprungen oder falsch verstanden wurde
                start = aligned_events[-1][2] if aligned_events else 0.0
                aligned_events.append((orig_w, start, start + 0.3))
                
        return aligned_events
        
    except Exception as e:
        print(f"Whisper-Fehler bei der Synchronisation: {e}")
        return _uniform_word_timing(text, audio_path)


def _uniform_word_timing(text: str, audio_path: Path) -> list:
    """Gleichmäßiges Timing als Fallback."""
    words = text.split()
    try:
        total = get_audio_duration(audio_path)
    except Exception:
        total = len(words) * 0.4
    dur = total / max(len(words), 1)
    return [(w, i * dur, (i + 1) * dur) for i, w in enumerate(words)]


def _build_ass_from_events(word_events: list, full_text: str) -> str:
    """
    Ultimativer TikTok-Style (Focus-Word):
    - Nur das aktuell gesprochene Wort wird angezeigt
    - Bleibt EXAKT in der Mitte des Bildes (kein Springen)
    - Riesige Schrift, knalliges Gelb, extrem dicker Rand
    - Alles in GROSSBUCHSTABEN für maximale Aufmerksamkeit
    """
    header = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TikTok,Arial Black,150,&H0000FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,12,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def fmt(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        cs = int((s - int(s)) * 100)
        return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"

    events_out = []
    for i, (word, start, end) in enumerate(word_events):
        # Nur das eine Wort, perfekt zentriert durch Alignment 5
        clean_word = word.strip().upper()
        if not clean_word: continue
        
        # Wir fügen einen winzigen "Pop"-Effekt hinzu durch Font-Scaling (optional)
        # Hier bleiben wir aber bei der festen Position wie gewünscht.
        line = clean_word
        events_out.append(f"Dialogue: 0,{fmt(start)},{fmt(end)},TikTok,,0,0,0,,{line}")

    return header + "\n".join(events_out) + "\n"


# ---------------------------------------------------------------------------
# Cover-Bild erstellen (einheitliches Design)
# ---------------------------------------------------------------------------

def create_cover_image(story_title: str, story_code: str, part_number: int,
                       output_path: Path, bg_frame_path: Path | None = None) -> Path:
    """
    Erstellt ein Cover-Bild (1080x1920) fuer die Story.
    Wenn bg_frame_path angegeben, wird dieser Frame als Hintergrund genutzt
    (mit dunklem Semi-transparent-Overlay). Sonst dunkler Gradient.
    Titel bricht automatisch um und ist hell/weiss.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
        import textwrap

        W, H = VIDEO_WIDTH, VIDEO_HEIGHT

        # ── Hintergrund ──────────────────────────────────────────────────────
        if bg_frame_path and Path(bg_frame_path).exists():
            bg = Image.open(str(bg_frame_path)).convert("RGB")
            # Auf 9:16 skalieren + croppen
            bg_ratio = bg.width / bg.height
            target_ratio = W / H
            if bg_ratio > target_ratio:
                new_h = H
                new_w = int(bg.width * H / bg.height)
            else:
                new_w = W
                new_h = int(bg.height * W / bg.width)
            bg = bg.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - W) // 2
            top  = (new_h - H) // 2
            bg = bg.crop((left, top, left + W, top + H))
            # Leichter Blur damit Text besser lesbar
            bg = bg.filter(ImageFilter.GaussianBlur(radius=3))
            img = bg
        else:
            # Gradient-Fallback (dunkelviolett -> schwarz)
            img = Image.new("RGB", (W, H), (0, 0, 0))
            d0 = ImageDraw.Draw(img)
            for y in range(H):
                t = y / H
                r = int(30 * (1 - t))
                g = int(10 * (1 - t))
                b = int(60 * (1 - t))
                d0.line([(0, y), (W, y)], fill=(r, g, b))

        draw = ImageDraw.Draw(img)

        # ── Dunkles Overlay fuer Lesbarkeit ──────────────────────────────────
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        # Oben und unten staerker abdunkeln
        od.rectangle([(0, 0), (W, H // 2)], fill=(0, 0, 0, 160))
        od.rectangle([(0, H // 2), (W, H)], fill=(0, 0, 0, 140))
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # ── Schriften ─────────────────────────────────────────────────────────
        font_path = DRAWTEXT_FONT or ""
        try:
            font_title = ImageFont.truetype(font_path, 88)  if font_path else ImageFont.load_default()
            font_med   = ImageFont.truetype(font_path, 56)  if font_path else ImageFont.load_default()
            font_small = ImageFont.truetype(font_path, 38)  if font_path else ImageFont.load_default()
        except Exception:
            font_title = ImageFont.load_default()
            font_med   = ImageFont.load_default()
            font_small = ImageFont.load_default()

        # ── "SARA" Logo oben ──────────────────────────────────────────────────
        draw.text((W // 2, 160), "SARA", font=font_med,
                  fill=(255, 255, 255), anchor="mm",
                  stroke_width=2, stroke_fill=(100, 60, 180))

        # ── Trennlinie ────────────────────────────────────────────────────────
        draw.rectangle([(120, 240), (W - 120, 243)], fill=(200, 160, 255))

        # ── Part-Badge ────────────────────────────────────────────────────────
        badge_text = f"Part {part_number}"
        badge_x, badge_y = W // 2, 330
        bw = int(draw.textlength(badge_text, font=font_small)) + 60
        bh = 62
        draw.rounded_rectangle(
            [(badge_x - bw // 2, badge_y - bh // 2),
             (badge_x + bw // 2, badge_y + bh // 2)],
            radius=31, fill=(120, 60, 220)
        )
        draw.text((badge_x, badge_y), badge_text, font=font_small,
                  fill=(240, 220, 255), anchor="mm")

        # ── Titel (mehrzeilig, hell, zentriert in der Mitte) ─────────────────
        # Zeichen pro Zeile abhängig von Titellänge anpassen
        # Für 88px Font sind ca. 18-22 Zeichen pro Zeile sicher auf 1080px Breite
        max_chars  = 20 
        wrapped = textwrap.wrap(story_title, width=max_chars)
        line_h  = 120
        total_h = len(wrapped) * line_h
        y_start = H // 2 - total_h // 2 + 60

        for idx, line in enumerate(wrapped):
            draw.text(
                (W // 2, y_start + idx * line_h),
                line.strip(),
                font=font_title,
                fill=(255, 255, 255),
                anchor="mm",
                stroke_width=6,
                stroke_fill=(0, 0, 0),
            )

        # ── Story-Code unten ──────────────────────────────────────────────────
        draw.text(
            (W // 2, H - 130),
            story_code,
            font=font_small,
            fill=(200, 200, 220),
            anchor="mm",
        )
        draw.rectangle([(120, H - 185), (W - 120, H - 182)], fill=(80, 50, 150))

        img.save(str(output_path), "JPEG", quality=92)
        return output_path

    except Exception as exc:
        try:
            from PIL import Image
            img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (20, 10, 40))
            img.save(str(output_path), "JPEG", quality=85)
        except Exception:
            pass
        return output_path


# ---------------------------------------------------------------------------
# Video-Erstellung (Hauptfunktion)
# ---------------------------------------------------------------------------

def create_video_for_part(story_part_id: int, job_id: int) -> Path:
    """
    Erstellt ein 9:16 Hochformat-Video (1080×1920) für einen Story-Teil.
    Features:
    - Edge-TTS natürliche Stimme
    - Wort-Highlighting (gelb) via ASS-Untertitel
    - Nahtloser Hintergrundvideo-Loop (bei Videoende zufällig neues)
    - Cover-Bild
    """
    def progress(pct: int, label: str):
        _set_progress(job_id, pct, label)

    with db_session() as conn:
        part = conn.execute(
            """SELECT sp.*, s.code, s.title
               FROM story_parts sp
               JOIN stories s ON s.id = sp.story_id
               WHERE sp.id = ?""",
            (story_part_id,)
        ).fetchone()

    if not part:
        raise ValueError(f"Story-Teil {story_part_id} nicht gefunden.")

    story_code  = part["code"]
    story_title = part["title"]
    part_number = part["part_number"]
    text        = part["text"]

    # Ausgabe-Verzeichnis anlegen
    output_dir = OUTPUTS_DIR / story_code
    output_dir.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / f"part_{part_number}.mp4"
    cover_path  = COVERS_DIR / f"{story_code}_part{part_number}.jpg"

    # Temporäre Dateien
    audio_path  = TTS_DIR / f"{story_code}_part{part_number}.wav"
    ass_path    = TTS_DIR / f"{story_code}_part{part_number}.ass"
    bg_concat   = TTS_DIR / f"{story_code}_part{part_number}_bg.mp4"
    tmp_files   = [audio_path, ass_path, bg_concat]

    try:
        # ---- Schritt 1: TTS (Kokoro, lokal) ----
        progress(10, "Stimme wird generiert (Kokoro, lokal)...")
        tts_to_file(text, audio_path)
        audio_duration = get_audio_duration(audio_path)

        # ---- Schritt 3: ASS-Untertitel mit Wort-Highlighting ----
        progress(30, "Wort-Timing wird berechnet…")
        build_word_timed_ass(text, ass_path, audio_path)

        # ---- Schritt 4: Hintergrundvideos für Loop vorbereiten ----
        progress(45, "Hintergrundvideos werden zusammengestellt…")

        bg_videos = list(BACKGROUNDS_DIR.glob("*"))
        bg_videos = [v for v in bg_videos if v.suffix.lower().lstrip(".") in ALLOWED_VIDEO_EXTENSIONS]

        if not bg_videos:
            raise FileNotFoundError("Keine Hintergrundvideos gefunden. Bitte zuerst hochladen.")

        # Nahtloser Loop: Zufällige Videos aneinanderreihen bis Audio-Dauer erreicht
        _assemble_background_loop(bg_videos, audio_duration, bg_concat)

        # ---- Schritt 4b: Frame aus Hintergrundvideo fuer Cover extrahieren ----
        progress(55, "Cover wird erstellt...")
        bg_frame_path = TTS_DIR / f"{story_code}_part{part_number}_cover_frame.jpg"
        try:
            _run_sub(
                [FFMPEG_EXE, "-y", "-i", str(bg_concat),
                 "-vframes", "1", "-q:v", "2", str(bg_frame_path)],
                check=True, capture_output=True
            )
        except Exception:
            bg_frame_path = None
        create_cover_image(story_title, story_code, part_number, cover_path,
                           bg_frame_path=bg_frame_path)
        if bg_frame_path and Path(bg_frame_path).exists():
            Path(bg_frame_path).unlink(missing_ok=True)

        # ---- Schritt 5: FFmpeg --- 9:16 Crop + ASS-Overlay + Audio ----
        progress(65, "Video wird gerendert (9:16 Hochformat)...")

        ass_escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")

        # Filter:
        # 1. scale+crop auf 1080×1920 (9:16)
        # 2. ASS-Untertitel (Wort-Highlighting)
        vf_filter = (
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
            f"ass='{ass_escaped}'"
        )

        _run_sub(
            [
                FFMPEG_EXE, "-y",
                "-i", str(bg_concat),
                "-i", str(audio_path),
                "-vf", vf_filter,
                "-c:v", "libx264",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                "-preset", "fast",
                "-crf", "23",
                "-movflags", "+faststart",
                str(output_path),
            ],
            check=True, capture_output=True
        )

        # ---- Schritt 6: DB aktualisieren ----
        progress(95, "Datenbank aktualisiert…")
        rel_cover = str(cover_path.relative_to(BASE_DIR)).replace("\\", "/")

        with db_session() as conn:
            conn.execute(
                "UPDATE story_parts SET cover_path = ? WHERE id = ?",
                (rel_cover, story_part_id)
            )

        progress(100, "Fertig! ✅")
        return output_path

    finally:
        for tmp in tmp_files:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


def _assemble_background_loop(bg_videos: list, target_duration: float, output: Path):
    """
    Reiht zufällige Hintergrundvideos aneinander (ohne Loop eines einzelnen),
    bis die Zieldauer erreicht ist. Nutzt FFmpeg concat-Filter.
    Skaliert auf 9:16 vor der Konkatenation.
    """
    random.shuffle(bg_videos)
    accumulated = 0.0
    clip_list = []

    while accumulated < target_duration:
        for vid in bg_videos:
            try:
                dur = get_video_duration(vid)
                clip_list.append((vid, dur))
                accumulated += dur
                if accumulated >= target_duration:
                    break
            except Exception:
                continue
        if not clip_list:
            raise RuntimeError("Konnte keine Hintergrundvideo-Dauer ermitteln.")
        if accumulated < target_duration:
            # Nochmal durchmischen und weitermachen
            random.shuffle(bg_videos)

    # Jedes Clip auf 1080×1920 skalieren
    tmp_clips = []
    concat_list = None
    try:
        for i, (vid, dur) in enumerate(clip_list):
            tmp_clip = output.parent / f"_tmp_clip_{i}_{uuid.uuid4().hex[:6]}.mp4"
            result = subprocess.run(
                [
                    FFMPEG_EXE, "-y",
                    "-i", str(vid),
                    "-vf", (
                        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
                        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
                    ),
                    "-c:v", "libx264",
                    "-an",
                    "-preset", "ultrafast",
                    "-crf", "28",
                    str(tmp_clip),
                ],
                capture_output=True
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"FFmpeg Clip-Skalierung fehlgeschlagen (exit {result.returncode}): "
                    f"{result.stderr.decode('utf-8', errors='replace')[-800:]}"
                )
            tmp_clips.append(tmp_clip)

        # Concat-List: absolute Pfade mit Forward-Slashes für FFmpeg auf Windows
        concat_list = output.parent / f"_concat_{uuid.uuid4().hex[:6]}.txt"
        with open(concat_list, "w", encoding="utf-8") as f:
            for clip in tmp_clips:
                safe_path = str(clip.resolve()).replace("\\", "/")
                f.write(f"file '{safe_path}'\n")

        result = subprocess.run(
            [
                FFMPEG_EXE, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-t", str(target_duration),
                "-c:v", "libx264",
                "-an",
                "-preset", "fast",
                "-crf", "23",
                str(output),
            ],
            capture_output=True
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"FFmpeg Concat fehlgeschlagen (exit {result.returncode}): "
                f"{result.stderr.decode('utf-8', errors='replace')[-800:]}"
            )

    finally:
        for c in tmp_clips:
            try:
                if c.exists():
                    c.unlink()
            except Exception:
                pass
        if concat_list is not None:
            try:
                if concat_list.exists():
                    concat_list.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Warteschlangen-Worker (Background-Thread)
# ---------------------------------------------------------------------------

def queue_worker():
    import time

    while True:
        try:
            job_id = None
            story_part_id = None

            with db_session() as conn:
                is_active = conn.execute(
                    "SELECT value FROM settings WHERE key = 'prod_active'"
                ).fetchone()
                if is_active and is_active["value"] == "0":
                    time.sleep(QUEUE_POLL_INTERVAL)
                    continue

                # Atomares UPDATE + RETURNING: prüft ob kein Job läuft UND startet den nächsten
                # in einer einzigen SQL-Transaktion — keine Race Condition möglich.
                row = conn.execute("""
                    UPDATE queue SET
                        status         = 'processing',
                        progress_pct   = 0,
                        progress_label = 'Starte…',
                        started_at     = datetime('now')
                    WHERE id = (
                        SELECT id FROM queue
                        WHERE  status = 'pending'
                          AND  (scheduled_at IS NULL OR scheduled_at <= datetime('now'))
                          AND  NOT EXISTS (SELECT 1 FROM queue WHERE status = 'processing')
                        ORDER BY id ASC LIMIT 1
                    )
                    RETURNING id, story_part_id
                """).fetchone()

                if row is None:
                    time.sleep(QUEUE_POLL_INTERVAL)
                    continue

                job_id        = row["id"]
                story_part_id = row["story_part_id"]
                conn.execute(
                    "UPDATE story_parts SET status = 'processing' WHERE id = ?",
                    (story_part_id,)
                )

            try:
                output_path = create_video_for_part(story_part_id, job_id)
                rel_path = str(output_path.relative_to(BASE_DIR)).replace("\\", "/")

                with db_session() as conn:
                    conn.execute(
                        "UPDATE queue SET status = 'done', progress_pct = 100, "
                        "progress_label = 'Fertig! ✅', finished_at = datetime('now') WHERE id = ?",
                        (job_id,)
                    )
                    conn.execute(
                        "UPDATE story_parts SET status = 'done', video_path = ? WHERE id = ?",
                        (rel_path, story_part_id)
                    )

            except Exception as e:
                err_msg = str(e)
                with db_session() as conn:
                    conn.execute(
                        "UPDATE queue SET status = 'error', error_msg = ?, "
                        "progress_label = 'Fehler ❌', finished_at = datetime('now') WHERE id = ?",
                        (err_msg, job_id)
                    )
                    conn.execute(
                        "UPDATE story_parts SET status = 'error' WHERE id = ?",
                        (story_part_id,)
                    )

        except Exception:
            time.sleep(QUEUE_POLL_INTERVAL)


def start_queue_worker():
    t = threading.Thread(target=queue_worker, daemon=True, name="QueueWorker")
    t.start()


# ---------------------------------------------------------------------------
# Flask-Routen — Story
# ---------------------------------------------------------------------------

@app.route("/")
@app.route("/story")
@app.route("/video")
@app.route("/library")
@app.route("/library/<path:rest>")
@app.route("/upload")
def index(rest=None):
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def api_config():
    return jsonify({"api_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())})


@app.route("/api/generate-story", methods=["POST"])
def api_generate_story():
    data    = request.get_json(silent=True) or {}
    prompt  = (data.get("prompt") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    if not prompt:
        return jsonify({"error": "Bitte einen Prompt eingeben."}), 400
    try:
        story_data = generate_story(prompt, api_key)
    except Exception as e:
        return jsonify({"error": f"AI-Fehler: {str(e)}"}), 500
    dup_check = check_duplicate(story_data.get("keywords", []))
    return jsonify({"story": story_data, "duplicate_check": dup_check})


@app.route("/api/save-story", methods=["POST"])
def api_save_story():
    data  = request.get_json(silent=True) or {}
    story = data.get("story")
    if not story:
        return jsonify({"error": "Keine Story-Daten."}), 400
    code = generate_story_code()
    try:
        with db_session() as conn:
            cursor = conn.execute(
                "INSERT INTO stories (code, title, keywords_json, total_parts) VALUES (?, ?, ?, ?)",
                (code, story["title"], json.dumps(story["keywords"]), story["total_parts"])
            )
            story_id = cursor.lastrowid
            for part in story["parts"]:
                social_json = json.dumps(part["social"]) if part.get("social") else None
                conn.execute(
                    "INSERT INTO story_parts (story_id, part_number, text, cliffhanger, social_json) VALUES (?, ?, ?, ?, ?)",
                    (story_id, part["part_number"], part["text"], part.get("cliffhanger_hint", ""), social_json)
                )
    except Exception as e:
        return jsonify({"error": f"Datenbank-Fehler: {str(e)}"}), 500
    return jsonify({"success": True, "code": code, "story_id": story_id})


@app.route("/api/stories", methods=["GET"])
def api_get_stories():
    with db_session() as conn:
        stories = conn.execute(
            "SELECT id, code, title, total_parts, created_at FROM stories ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for s in stories:
            row = dict(s)
            # Zähle Parts mit abgeschlossenem Video
            done = conn.execute(
                "SELECT COUNT(*) FROM story_parts WHERE story_id = ? AND video_path IS NOT NULL AND video_path != ''",
                (s['id'],)
            ).fetchone()[0]

            # Zähle Parts in der Warteschlange (pending oder processing)
            in_queue = conn.execute(
                """SELECT COUNT(*) FROM story_parts sp 
                   JOIN queue q ON q.story_part_id = sp.id 
                   WHERE sp.story_id = ? AND q.status IN ('pending', 'processing')""",
                (s['id'],)
            ).fetchone()[0]

            row['video_done_parts'] = done
            row['video_queue_parts'] = in_queue
            result.append(row)
    return jsonify(result)


@app.route("/api/stories/<int:story_id>", methods=["GET"])
def api_get_story(story_id: int):
    with db_session() as conn:
        story = conn.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
        if not story:
            abort(404)
        parts = conn.execute(
            "SELECT * FROM story_parts WHERE story_id = ? ORDER BY part_number",
            (story_id,)
        ).fetchall()
    return jsonify({"story": dict(story), "parts": [dict(p) for p in parts]})


# ---------------------------------------------------------------------------
# Flask-Routen — Video-Erstellung / Queue
# ---------------------------------------------------------------------------

@app.route("/api/create-video", methods=["POST"])
def api_create_video():
    data        = request.get_json(silent=True) or {}
    story_id    = data.get("story_id")
    part_number = data.get("part_number")

    if not story_id:
        return jsonify({"error": "story_id fehlt."}), 400

    with db_session() as conn:
        if part_number:
            parts = conn.execute(
                "SELECT id FROM story_parts WHERE story_id = ? AND part_number = ?",
                (story_id, part_number)
            ).fetchall()
        else:
            parts = conn.execute(
                "SELECT id FROM story_parts WHERE story_id = ?", (story_id,)
            ).fetchall()

    if not parts:
        return jsonify({"error": "Keine Story-Teile gefunden."}), 404

    # Settings laden
    with db_session() as conn:
        interval_min = int(conn.execute("SELECT value FROM settings WHERE key = 'prod_interval_min'").fetchone()["value"] or 0)
        
        # Letzten Planungszeitpunkt finden
        last_row = conn.execute("SELECT MAX(scheduled_at) as m FROM queue").fetchone()
        last_m = last_row["m"] if last_row and last_row["m"] else None
        
        if last_m:
            try:
                ref_time = datetime.strptime(last_m, "%Y-%m-%d %H:%M:%S")
            except:
                ref_time = datetime.now()
        else:
            ref_time = datetime.now()
            
        if ref_time < datetime.now():
            ref_time = datetime.now()

        job_ids = []
        for part in parts:
            existing = conn.execute(
                "SELECT id FROM queue WHERE story_part_id = ? AND status IN ('pending','processing')",
                (part["id"],)
            ).fetchone()
            if existing:
                job_ids.append(existing["id"])
                continue
            
            sched_time = None
            if interval_min > 0:
                ref_time += timedelta(minutes=interval_min)
                sched_time = ref_time.strftime("%Y-%m-%d %H:%M:%S")

            cursor = conn.execute(
                "INSERT INTO queue (story_part_id, scheduled_at) VALUES (?, ?)", 
                (part["id"], sched_time)
            )
            job_ids.append(cursor.lastrowid)
            conn.execute(
                "UPDATE story_parts SET status = 'pending' WHERE id = ?", (part["id"],)
            )

    return jsonify({"success": True, "job_ids": job_ids})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json() or {}
        with db_session() as conn:
            for k, v in data.items():
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
                if k == 'prod_active' and str(v) == '0':
                    # Wenn gestoppt wird, alle laufenden Prozesse killen
                    _kill_active_subs()
        return jsonify({"success": True})
    else:
        with db_session() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/api/queue", methods=["GET"])
def api_get_queue():
    with db_session() as conn:
        jobs = conn.execute(
            """SELECT q.id, q.status, q.error_msg, q.created_at, q.finished_at,
                      q.progress_pct, q.progress_label,
                      sp.part_number, sp.story_id,
                      s.title, s.code
               FROM queue q
               JOIN story_parts sp ON sp.id = q.story_part_id
               JOIN stories s ON s.id = sp.story_id
               ORDER BY q.id DESC
               LIMIT 500"""
        ).fetchall()
    return jsonify([dict(j) for j in jobs])


@app.route("/api/queue/stats", methods=["GET"])
def api_queue_stats():
    with db_session() as conn:
        stats = conn.execute(
            "SELECT status, COUNT(*) as count FROM queue GROUP BY status"
        ).fetchall()
    return jsonify({row["status"]: row["count"] for row in stats})


@app.route("/api/queue/live")
def api_queue_live():
    """SSE-Stream: sendet alle 1.5s den aktuellen Queue-Status."""
    def generate():
        import time
        while True:
            try:
                conn = get_db()
                try:
                    is_active = conn.execute("SELECT value FROM settings WHERE key = 'prod_active'").fetchone()
                    prod_active = (is_active["value"] != "0") if is_active else True
                    jobs = conn.execute(
                        """SELECT q.id, q.status, q.progress_pct, q.progress_label,
                                  q.error_msg, q.created_at, q.started_at, q.finished_at,
                                  sp.part_number, sp.story_id,
                                  s.title, s.code
                           FROM queue q
                           JOIN story_parts sp ON sp.id = q.story_part_id
                           JOIN stories s ON s.id = sp.story_id
                           ORDER BY q.id DESC LIMIT 100"""
                    ).fetchall()
                    payload = json.dumps({
                        "jobs": [dict(j) for j in jobs],
                        "prod_active": prod_active
                    })
                finally:
                    conn.close()
                yield f"data: {payload}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            time.sleep(1.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.route("/api/queue/<int:job_id>", methods=["DELETE"])
def api_delete_queue_job(job_id: int):
    with db_session() as conn:
        job = conn.execute("SELECT status, story_part_id FROM queue WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return jsonify({"error": "Job nicht gefunden"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "Laufender Job kann nicht gelöscht werden."}), 400
        
        conn.execute("DELETE FROM queue WHERE id = ?", (job_id,))
        if job["status"] != "done":
            conn.execute("UPDATE story_parts SET status = 'pending' WHERE id = ?", (job["story_part_id"],))
    return jsonify({"success": True})


@app.route("/api/queue/<int:job_id>/restart", methods=["POST"])
def api_restart_queue_job(job_id: int):
    with db_session() as conn:
        job = conn.execute("SELECT status, story_part_id FROM queue WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return jsonify({"error": "Job nicht gefunden"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "Laufender Job kann nicht neugestartet werden."}), 400
        
        conn.execute(
            "UPDATE queue SET status = 'pending', error_msg = NULL, progress_label = 'Wartet...', progress_pct = 0 WHERE id = ?",
            (job_id,)
        )
        conn.execute("UPDATE story_parts SET status = 'pending' WHERE id = ?", (job["story_part_id"],))
    return jsonify({"success": True})


@app.route("/api/queue/<int:job_id>/cancel", methods=["POST"])
def api_cancel_queue_job(job_id: int):
    with db_session() as conn:
        job = conn.execute("SELECT status, story_part_id FROM queue WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return jsonify({"error": "Job nicht gefunden"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "Laufender Job kann nicht abgebrochen werden."}), 400
        
        conn.execute(
            "UPDATE queue SET status = 'error', error_msg = 'Abgebrochen durch Benutzer', progress_label = 'Abgebrochen' WHERE id = ?",
            (job_id,)
        )
        conn.execute("UPDATE story_parts SET status = 'pending' WHERE id = ?", (job["story_part_id"],))
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Flask-Routen — Hintergrundvideos
# ---------------------------------------------------------------------------

@app.route("/api/backgrounds", methods=["GET"])
def api_get_backgrounds():
    with db_session() as conn:
        bgs = conn.execute("SELECT * FROM backgrounds ORDER BY uploaded_at DESC").fetchall()
    return jsonify([dict(b) for b in bgs])


@app.route("/api/backgrounds/upload", methods=["POST"])
def api_upload_background():
    if "video" not in request.files:
        return jsonify({"error": "Keine Datei hochgeladen."}), 400
    file = request.files["video"]
    if not file.filename:
        return jsonify({"error": "Kein Dateiname."}), 400
    if not allowed_video(file.filename):
        return jsonify({"error": "Nur mp4, mov, avi erlaubt."}), 400
    original_name = file.filename
    ext           = original_name.rsplit(".", 1)[1].lower()
    unique_name   = f"{uuid.uuid4().hex}.{ext}"
    save_path     = BACKGROUNDS_DIR / unique_name
    file.save(str(save_path))
    with db_session() as conn:
        conn.execute(
            "INSERT INTO backgrounds (filename, original_name) VALUES (?, ?)",
            (unique_name, original_name)
        )
    return jsonify({"success": True, "filename": unique_name, "original_name": original_name})


@app.route("/api/backgrounds/<filename>", methods=["DELETE"])
def api_delete_background(filename: str):
    safe_name = Path(filename).name
    file_path = BACKGROUNDS_DIR / safe_name
    if file_path.exists():
        file_path.unlink()
    with db_session() as conn:
        conn.execute("DELETE FROM backgrounds WHERE filename = ?", (safe_name,))
    return jsonify({"success": True})


@app.route("/backgrounds/<filename>")
def serve_background(filename: str):
    safe_name = Path(filename).name
    return send_from_directory(str(BACKGROUNDS_DIR), safe_name)


@app.route("/covers/<filename>")
def serve_cover(filename: str):
    safe_name = Path(filename).name
    return send_from_directory(str(COVERS_DIR), safe_name)


# ---------------------------------------------------------------------------
# Flask-Routen — Video-Bibliothek & Download
# ---------------------------------------------------------------------------

@app.route("/api/videos", methods=["GET"])
def api_get_videos():
    with db_session() as conn:
        videos = conn.execute(
            """SELECT sp.id, sp.part_number, sp.video_path, sp.cover_path, sp.status,
                      sp.social_json,
                      s.id as story_id, s.code, s.title, s.created_at
               FROM story_parts sp
               JOIN stories s ON s.id = sp.story_id
               WHERE sp.status = 'done' AND sp.video_path IS NOT NULL
               ORDER BY s.created_at DESC, sp.part_number ASC"""
        ).fetchall()
        uploads = conn.execute("SELECT * FROM video_uploads").fetchall()

    uploads_by_part: dict = {}
    for u in uploads:
        pid = u["story_part_id"]
        if pid not in uploads_by_part:
            uploads_by_part[pid] = []
        uploads_by_part[pid].append(dict(u))

    grouped = {}
    for v in videos:
        sid = v["story_id"]
        if sid not in grouped:
            grouped[sid] = {
                "story_id": sid, "code": v["code"], "title": v["title"],
                "created_at": v["created_at"], "parts": [],
            }
        social = {}
        try:
            if v["social_json"]:
                social = json.loads(v["social_json"])
        except Exception:
            pass
        grouped[sid]["parts"].append({
            "id": v["id"], "part_number": v["part_number"],
            "video_path": v["video_path"], "cover_path": v["cover_path"],
            "social": social,
            "uploads": uploads_by_part.get(v["id"], []),
        })

    return jsonify(list(grouped.values()))


@app.route("/api/videos/<int:part_id>", methods=["DELETE"])
def api_delete_video(part_id: int):
    """Löscht ein Video (Datei + Cover + Queue + Uploads) aus dem System."""
    with db_session() as conn:
        part = conn.execute(
            "SELECT video_path, cover_path FROM story_parts WHERE id = ?",
            (part_id,)
        ).fetchone()
        if not part:
            return jsonify({"error": "Teil nicht gefunden"}), 404

        # Datei vom Datenträger löschen
        for rel_path in [part["video_path"], part["cover_path"]]:
            if rel_path:
                try:
                    full = BASE_DIR / rel_path
                    if full.exists():
                        full.unlink()
                except Exception:
                    pass

        # DB zurücksetzen
        conn.execute(
            "UPDATE story_parts SET video_path = NULL, cover_path = NULL, status = 'pending' WHERE id = ?",
            (part_id,)
        )
        conn.execute("DELETE FROM video_uploads WHERE story_part_id = ?", (part_id,))
        conn.execute("DELETE FROM queue WHERE story_part_id = ? AND status IN ('done', 'error')", (part_id,))

    return jsonify({"success": True})


@app.route("/api/stories/<int:story_id>", methods=["DELETE"])
def api_delete_story(story_id: int):
    """Löscht eine komplette Story inklusive aller Parts, Dateien und Ordner."""
    with db_session() as conn:
        story = conn.execute("SELECT code FROM stories WHERE id = ?", (story_id,)).fetchone()
        if not story:
            return jsonify({"error": "Story nicht gefunden"}), 404
        
        story_code = story["code"]
        
        # Alle Parts dieser Story finden
        parts = conn.execute(
            "SELECT id, video_path, cover_path FROM story_parts WHERE story_id = ?",
            (story_id,)
        ).fetchall()
        
        # 1. Einzelne Dateien löschen (Video + Cover)
        for p in parts:
            for rel_path in [p["video_path"], p["cover_path"]]:
                if rel_path:
                    try:
                        full = BASE_DIR / rel_path
                        if full.exists():
                            full.unlink()
                    except Exception:
                        pass
            
            # Verknüpfte Daten löschen
            conn.execute("DELETE FROM video_uploads WHERE story_part_id = ?", (p["id"],))
            conn.execute("DELETE FROM queue WHERE story_part_id = ?", (p["id"],))

        # 2. Ganzen Ausgabe-Ordner löschen
        story_out_dir = OUTPUTS_DIR / story_code
        if story_out_dir.exists() and story_out_dir.is_dir():
            try:
                import shutil
                shutil.rmtree(story_out_dir)
            except Exception:
                pass

        # 3. Temporäre TTS/ASS Dateien löschen
        try:
            for f in TTS_DIR.glob(f"{story_code}_part*"):
                f.unlink()
        except Exception:
            pass

        # Story und Parts aus DB löschen
        conn.execute("DELETE FROM story_parts WHERE story_id = ?", (story_id,))
        conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
        
    return jsonify({"success": True})


@app.route("/video/<path:video_path>")
def serve_video(video_path: str):
    full_path = BASE_DIR / video_path
    if not str(full_path.resolve()).startswith(str(DATA_DIR.resolve())):
        abort(403)
    if not full_path.exists():
        abort(404)
    return send_from_directory(str(full_path.parent), full_path.name, mimetype="video/mp4")


@app.route("/download/<path:video_path>")
def download_video(video_path: str):
    full_path = BASE_DIR / video_path
    if not str(full_path.resolve()).startswith(str(DATA_DIR.resolve())):
        abort(403)
    if not full_path.exists():
        abort(404)
    return send_from_directory(
        str(full_path.parent), full_path.name,
        as_attachment=True, mimetype="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# Flask-Routen — Upload-Tracking
# ---------------------------------------------------------------------------

@app.route("/api/uploads", methods=["POST"])
def api_add_upload():
    data = request.get_json(silent=True) or {}
    story_part_id = data.get("story_part_id")
    platform      = data.get("platform", "tiktok")
    notes         = data.get("notes", "")
    if not story_part_id:
        return jsonify({"error": "story_part_id fehlt."}), 400
    with db_session() as conn:
        cursor = conn.execute(
            "INSERT INTO video_uploads (story_part_id, platform, notes) VALUES (?, ?, ?)",
            (story_part_id, platform, notes)
        )
        new_id = cursor.lastrowid
    return jsonify({"success": True, "id": new_id,
                    "uploaded_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})


@app.route("/api/uploads/<int:upload_id>", methods=["DELETE"])
def api_delete_upload(upload_id: int):
    with db_session() as conn:
        conn.execute("DELETE FROM video_uploads WHERE id = ?", (upload_id,))
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Flask-Routen — Prompt-Builder
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE_SYSTEM = SYSTEM_PROMPT


@app.route("/api/get-prompt-template", methods=["POST"])
def api_get_prompt_template():
    data = request.get_json(silent=True) or {}
    idea = (data.get("idea") or "").strip()
    if not idea:
        return jsonify({"error": "Bitte eine Idee eingeben."}), 400
    user_msg = f"Create a story about: {idea}"
    return jsonify({
        "system_prompt": PROMPT_TEMPLATE_SYSTEM,
        "user_message":  user_msg,
        "combined":      f"[SYSTEM]\n{PROMPT_TEMPLATE_SYSTEM}\n\n[USER]\n{user_msg}",
    })


@app.route("/api/import-story", methods=["POST"])
def api_import_story():
    data      = request.get_json(silent=True) or {}
    raw_json  = (data.get("json_text") or "").strip()
    dry_run   = data.get("dry_run", False)
    auto_queue = data.get("auto_queue", False)

    if not raw_json:
        return jsonify({"error": "Kein JSON eingegeben."}), 400
    json_match = re.search(r"\{.*\}", raw_json, re.DOTALL)
    if not json_match:
        return jsonify({"error": "Kein gültiges JSON gefunden."}), 400
    try:
        story = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON-Parsing-Fehler: {str(e)}"}), 400
    for field in ["title", "total_parts", "keywords", "parts"]:
        if field not in story:
            return jsonify({"error": f"Feld '{field}' fehlt im JSON."}), 400
    
    dup_check = check_duplicate(story["keywords"])
    
    if dry_run:
        return jsonify({
            "success": True, 
            "duplicate_check": dup_check, 
            "story": story
        })

    code = generate_story_code()
    try:
        with db_session() as conn:
            cursor = conn.execute(
                "INSERT INTO stories (code, title, keywords_json, total_parts) VALUES (?, ?, ?, ?)",
                (code, story["title"], json.dumps(story["keywords"]), story["total_parts"])
            )
            story_id = cursor.lastrowid
            part_ids = []
            for part in story["parts"]:
                social_json = json.dumps(part["social"]) if part.get("social") else None
                pc = conn.execute(
                    "INSERT INTO story_parts (story_id, part_number, text, cliffhanger, social_json) VALUES (?, ?, ?, ?, ?)",
                    (story_id, part["part_number"], part["text"], part.get("cliffhanger_hint", ""), social_json)
                )
                part_ids.append(pc.lastrowid)
            
            if auto_queue:
                interval_min = int(conn.execute("SELECT value FROM settings WHERE key = 'prod_interval_min'").fetchone()["value"] or 0)
                last_row = conn.execute("SELECT MAX(scheduled_at) as m FROM queue").fetchone()
                last_m = last_row["m"] if last_row and last_row["m"] else None
                ref_time = datetime.strptime(last_m, "%Y-%m-%d %H:%M:%S") if last_m else datetime.now()
                if ref_time < datetime.now(): ref_time = datetime.now()

                for p_id in part_ids:
                    sched_time = None
                    if interval_min > 0:
                        ref_time += timedelta(minutes=interval_min)
                        sched_time = ref_time.strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute("INSERT INTO queue (story_part_id, scheduled_at) VALUES (?, ?)", (p_id, sched_time))
                    conn.execute("UPDATE story_parts SET status = 'pending' WHERE id = ?", (p_id,))

    except Exception as e:
        return jsonify({"error": f"Datenbank-Fehler: {str(e)}"}), 500
    
    return jsonify({
        "success": True, "code": code, "story_id": story_id, 
        "title": story["title"], "duplicate_check": dup_check,
        "auto_queued": auto_queue
    })


# ---------------------------------------------------------------------------
# App-Start
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for d in [DATA_DIR, BACKGROUNDS_DIR, OUTPUTS_DIR, TTS_DIR, COVERS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    init_db()
    start_queue_worker()

    port = int(os.environ.get("PORT", 7842))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
