"""
SARA — Story And Reel Automator
Flask backend: story generation, video creation, queue management
"""

import os
import re
import json
import time
import uuid
import random
import shutil
import string
import sqlite3
import threading
import subprocess
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

from flask import (
    Flask, request, jsonify, render_template,
    send_from_directory, abort, Response, stream_with_context,
    session, redirect, url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from google_auth_oauthlib.flow import Flow as GoogleOAuthFlow
from google.oauth2.credentials import Credentials as GoogleCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build as yt_build
from googleapiclient.http import MediaFileUpload


# ---------------------------------------------------------------------------
# Subprocess management
# ---------------------------------------------------------------------------

_ACTIVE_SUBPROCESSES: list = []


def _run_sub(args, **kwargs) -> subprocess.CompletedProcess:
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    check = kwargs.pop("check", False)
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
    while _ACTIVE_SUBPROCESSES:
        p = _ACTIVE_SUBPROCESSES.pop()
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            else:
                p.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR        = Path(__file__).parent
DATA_DIR        = BASE_DIR / "data"
BACKGROUNDS_DIR = DATA_DIR / "backgrounds"
OUTPUTS_DIR     = DATA_DIR / "outputs"
TTS_DIR         = DATA_DIR / "tts"
COVERS_DIR      = DATA_DIR / "covers"
DB_PATH         = DATA_DIR / "sara.db"

VIDEO_WIDTH              = 1080
VIDEO_HEIGHT             = 1920
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "avi"}
QUEUE_POLL_INTERVAL      = 2


def _find_ffmpeg() -> str:
    import shutil as _shutil
    ff = _shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _find_font() -> str:
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


FFMPEG_EXE    = _find_ffmpeg()
DRAWTEXT_FONT = _find_font()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)
# Hinter Cloudflare Tunnel: X-Forwarded-Proto/-Host vertrauen, damit
# request.url korrekt https:// zeigt (sonst schlaegt der OAuth-Callback fehl).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    """Sorgt dafuer, dass /api/-Routen bei einer unerwarteten Exception immer
    JSON zurueckgeben statt Flasks HTML-Fehlerseite (die im Frontend beim
    res.json()-Parsing mit "Unexpected token '<'" abstuerzt)."""
    if isinstance(e, HTTPException) and not request.path.startswith("/api/"):
        return e
    code = e.code if isinstance(e, HTTPException) else 500
    if not isinstance(e, HTTPException):
        traceback.print_exc()
    return jsonify({"error": str(e) or e.__class__.__name__}), code

# ---------------------------------------------------------------------------
# YouTube / Google OAuth
# ---------------------------------------------------------------------------
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
OAUTH_REDIRECT_BASE  = os.environ.get("OAUTH_REDIRECT_BASE", "").strip().rstrip("/")
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=3, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_session(write: bool = True):
    conn = get_db()
    stmt = "BEGIN IMMEDIATE" if write else "BEGIN"
    deadline = time.monotonic() + 30
    while True:
        try:
            conn.execute(stmt)
            break
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and time.monotonic() < deadline:
                time.sleep(0.05)
            else:
                conn.close()
                raise
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def init_db():
    conn = get_db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS stories (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                code          TEXT    NOT NULL UNIQUE,
                title         TEXT    NOT NULL,
                keywords_json TEXT    NOT NULL,
                total_parts   INTEGER NOT NULL,
                created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS story_parts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id     INTEGER NOT NULL REFERENCES stories(id),
                part_number  INTEGER NOT NULL,
                text         TEXT    NOT NULL,
                cliffhanger  TEXT,
                video_path   TEXT,
                cover_path   TEXT,
                social_json  TEXT,
                status       TEXT    NOT NULL DEFAULT 'pending',
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                story_part_id  INTEGER NOT NULL REFERENCES story_parts(id),
                status         TEXT    NOT NULL DEFAULT 'pending',
                error_msg      TEXT,
                progress_label TEXT    DEFAULT '',
                progress_pct   INTEGER DEFAULT 0,
                created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                started_at     TEXT,
                scheduled_at   TEXT,
                finished_at    TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backgrounds (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                filename      TEXT    NOT NULL UNIQUE,
                original_name TEXT    NOT NULL,
                uploaded_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS video_uploads (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                story_part_id  INTEGER NOT NULL REFERENCES story_parts(id),
                platform       TEXT    NOT NULL DEFAULT 'tiktok',
                uploaded_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                notes          TEXT    DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS youtube_accounts (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id     TEXT    NOT NULL UNIQUE,
                channel_title  TEXT    NOT NULL,
                email          TEXT,
                refresh_token  TEXT    NOT NULL,
                is_active      INTEGER NOT NULL DEFAULT 1,
                added_at       TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS youtube_queue (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                story_part_id       INTEGER NOT NULL REFERENCES story_parts(id),
                youtube_account_id  INTEGER NOT NULL REFERENCES youtube_accounts(id),
                status              TEXT    NOT NULL DEFAULT 'pending',
                video_id            TEXT,
                error_msg           TEXT,
                scheduled_at        TEXT,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
                started_at          TEXT,
                finished_at         TEXT
            )
        """)

        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('prod_interval_min', '0')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('prod_slots', '')")
        # YouTube-Upload-Einstellungen: 5 Minuten Pause zwischen Uploads (wirkt
        # weniger nach Bot als Uploads im Sekundentakt), automatischer Upload an,
        # Videos standardmaessig oeffentlich.
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('yt_upload_interval_min', '5')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('yt_auto_upload', '1')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('yt_privacy_status', 'public')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('prod_active_windows', '')")
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('prod_active', '1')")

        # Reset jobs stuck mid-processing from last shutdown
        conn.execute(
            "UPDATE queue SET status='pending', progress_label='Wartend (Restart)...' WHERE status='processing'"
        )
        conn.execute("UPDATE story_parts SET status='pending' WHERE status='processing'")

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()

    # Column migrations — each runs separately and silently skips if column already exists
    for stmt in [
        "ALTER TABLE story_parts ADD COLUMN cover_path TEXT",
        "ALTER TABLE story_parts ADD COLUMN social_json TEXT",
        "ALTER TABLE queue ADD COLUMN progress_label TEXT DEFAULT ''",
        "ALTER TABLE queue ADD COLUMN progress_pct INTEGER DEFAULT 0",
        "ALTER TABLE queue ADD COLUMN started_at TEXT",
        "ALTER TABLE queue ADD COLUMN scheduled_at TEXT",
    ]:
        try:
            mc = get_db()
            try:
                mc.execute(stmt)
            finally:
                mc.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_story_code() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(chars, k=6))
        with db_session(write=False) as conn:
            if conn.execute("SELECT 1 FROM stories WHERE code=?", (code,)).fetchone() is None:
                return code


def check_duplicate(keywords: list, title: str = "") -> dict:
    new_set   = {k.lower() for k in keywords}
    title_cmp = title.lower().strip()
    with db_session(write=False) as conn:
        rows = conn.execute("SELECT id, code, title, keywords_json FROM stories").fetchall()
    for row in rows:
        existing  = {k.lower() for k in json.loads(row["keywords_json"])}
        overlap   = len(new_set & existing)
        is_exact  = (title_cmp and row["title"].lower().strip() == title_cmp) or overlap == len(new_set)
        if is_exact or overlap >= 6:
            return {
                "is_duplicate": True,
                "is_exact":     is_exact,
                "similar_story": {"id": row["id"], "code": row["code"], "title": row["title"]},
                "overlap": overlap,
            }
    return {"is_duplicate": False, "is_exact": False, "similar_story": None, "overlap": 0}


def allowed_video(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_VIDEO_EXTENSIONS


def _set_progress(job_id: int, pct: int, label: str):
    try:
        with db_session() as conn:
            conn.execute(
                "UPDATE queue SET progress_pct=?, progress_label=? WHERE id=?",
                (pct, label, job_id),
            )
    except Exception:
        pass


def _enqueue_parts(conn: sqlite3.Connection, part_ids: list, interval_min: int) -> list:
    """
    Adds story parts to the queue with optional scheduling interval.
    Skips parts already pending/processing. Returns list of queue job IDs.
    """
    # Find reference time for scheduling (last scheduled job or now)
    row = conn.execute("SELECT MAX(scheduled_at) AS m FROM queue").fetchone()
    last = row["m"] if row and row["m"] else None
    if last:
        try:
            ref_time = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        except Exception:
            ref_time = datetime.now()
    else:
        ref_time = datetime.now()
    ref_time = max(ref_time, datetime.now())

    job_ids = []
    for part_id in part_ids:
        existing = conn.execute(
            "SELECT id FROM queue WHERE story_part_id=? AND status IN ('pending','processing')",
            (part_id,),
        ).fetchone()
        if existing:
            job_ids.append(existing["id"])
            continue

        sched_time = None
        if interval_min > 0:
            ref_time += timedelta(minutes=interval_min)
            sched_time = ref_time.strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.execute(
            "INSERT INTO queue (story_part_id, scheduled_at) VALUES (?,?)",
            (part_id, sched_time),
        )
        job_ids.append(cur.lastrowid)
        conn.execute("UPDATE story_parts SET status='pending' WHERE id=?", (part_id,))

    return job_ids


def _enqueue_youtube(conn: sqlite3.Connection, story_part_id: int, account_ids: list, interval_min: float) -> list:
    """
    Reiht ein fertiges Video fuer einen oder mehrere verbundene YouTube-Kanaele
    zum Hochladen ein. Spannt die Uploads zeitlich auseinander (Kette ab dem
    zuletzt geplanten Job in der youtube_queue), damit nicht mehrere Uploads
    gleichzeitig/im Sekundentakt rausgehen -- das wuerde nach Bot aussehen.
    """
    row = conn.execute("SELECT MAX(scheduled_at) AS m FROM youtube_queue").fetchone()
    last = row["m"] if row and row["m"] else None
    if last:
        try:
            ref_time = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        except Exception:
            ref_time = datetime.now()
    else:
        ref_time = datetime.now()
    ref_time = max(ref_time, datetime.now())

    job_ids = []
    for account_id in account_ids:
        existing = conn.execute(
            "SELECT id FROM youtube_queue WHERE story_part_id=? AND youtube_account_id=? "
            "AND status IN ('pending','processing')",
            (story_part_id, account_id),
        ).fetchone()
        if existing:
            job_ids.append(existing["id"])
            continue
        ref_time += timedelta(minutes=max(interval_min, 0.25))
        cur = conn.execute(
            "INSERT INTO youtube_queue (story_part_id, youtube_account_id, scheduled_at) VALUES (?,?,?)",
            (story_part_id, account_id, ref_time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        job_ids.append(cur.lastrowid)
    return job_ids


# ---------------------------------------------------------------------------
# AI — Story generation
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



# ---------------------------------------------------------------------------
# TTS — Kokoro-82M (local, offline)
# ---------------------------------------------------------------------------

def _ensure_kokoro_model() -> tuple:
    import urllib.request
    model_dir = DATA_DIR / "kokoro_models"
    model_dir.mkdir(parents=True, exist_ok=True)

    onnx_path   = model_dir / "kokoro-v1.0.int8.onnx"
    voices_path = model_dir / "voices-v1.0.bin"

    if not onnx_path.exists():
        print("Lade Kokoro-82M ONNX Modell herunter...")
        urllib.request.urlretrieve(
            "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
            onnx_path,
        )
    if not voices_path.exists():
        print("Lade Kokoro Stimmen-Datei herunter...")
        urllib.request.urlretrieve(
            "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
            voices_path,
        )
    return onnx_path, voices_path


def tts_to_file(text: str, output_path: Path) -> Path:
    try:
        from kokoro_onnx import Kokoro
        import soundfile as sf
    except ImportError:
        raise RuntimeError("Bitte 'pip install kokoro-onnx soundfile' ausführen.")

    onnx_path, voices_path = _ensure_kokoro_model()
    kokoro = Kokoro(str(onnx_path), str(voices_path))
    samples, sample_rate = kokoro.create(text, voice="af_heart", speed=1.0, lang="en-us")
    sf.write(str(output_path), samples, sample_rate)
    return output_path


# ---------------------------------------------------------------------------
# Audio / video duration
# ---------------------------------------------------------------------------

def _get_duration(file_path: Path) -> float:
    result = _run_sub(
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


def get_audio_duration(path: Path) -> float:
    return _get_duration(path)


def get_video_duration(path: Path) -> float:
    return _get_duration(path)


# ---------------------------------------------------------------------------
# Word timing + ASS subtitles
# ---------------------------------------------------------------------------

_WHISPER_MODEL = None


def _get_word_boundaries(text: str, audio_path: Path) -> list:
    global _WHISPER_MODEL
    try:
        from faster_whisper import WhisperModel
        if _WHISPER_MODEL is None:
            print("Lade lokales Whisper-Modell...")
            _WHISPER_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")

        segments, _ = _WHISPER_MODEL.transcribe(
            str(audio_path), word_timestamps=True, initial_prompt=text
        )

        whisper_words = [w for seg in segments for w in seg.words if w.word.strip()]
        if not whisper_words:
            return _uniform_word_timing(text, audio_path)

        original_words = text.split()
        aligned = []
        w_idx = 0

        for orig_w in original_words:
            orig_clean = "".join(c for c in orig_w.lower() if c.isalnum())
            if not orig_clean:
                t = aligned[-1][2] if aligned else 0.0
                aligned.append((orig_w, t, t + 0.1))
                continue

            found = False
            for i in range(w_idx, min(w_idx + 4, len(whisper_words))):
                wc = "".join(c for c in whisper_words[i].word.lower() if c.isalnum())
                if wc and (wc in orig_clean or orig_clean in wc):
                    aligned.append((orig_w, whisper_words[i].start, whisper_words[i].end))
                    w_idx = i + 1
                    found = True
                    break

            if not found:
                t = aligned[-1][2] if aligned else 0.0
                aligned.append((orig_w, t, t + 0.3))

        return aligned

    except Exception as e:
        print(f"Whisper-Fehler: {e}")
        return _uniform_word_timing(text, audio_path)


def _uniform_word_timing(text: str, audio_path: Path) -> list:
    words = text.split()
    try:
        total = get_audio_duration(audio_path)
    except Exception:
        total = len(words) * 0.4
    dur = total / max(len(words), 1)
    return [(w, i * dur, (i + 1) * dur) for i, w in enumerate(words)]


def _build_ass_from_events(word_events: list) -> str:
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

    def _fmt(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        cs = int((s - int(s)) * 100)
        return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"

    lines = []
    for word, start, end in word_events:
        clean = word.strip().upper()
        if clean:
            lines.append(f"Dialogue: 0,{_fmt(start)},{_fmt(end)},TikTok,,0,0,0,,{clean}")

    return header + "\n".join(lines) + "\n"


def build_word_timed_ass(text: str, output_path: Path, audio_path: Path) -> Path:
    events = _get_word_boundaries(text, audio_path)
    output_path.write_text(_build_ass_from_events(events), encoding="utf-8")
    return output_path


# ---------------------------------------------------------------------------
# Cover image
# ---------------------------------------------------------------------------

def create_cover_image(
    story_title: str, story_code: str, part_number: int,
    output_path: Path, bg_frame_path=None
) -> Path:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
        import textwrap

        W, H = VIDEO_WIDTH, VIDEO_HEIGHT

        if bg_frame_path and Path(bg_frame_path).exists():
            bg = Image.open(str(bg_frame_path)).convert("RGB")
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
            bg   = bg.crop((left, top, left + W, top + H))
            bg   = bg.filter(ImageFilter.GaussianBlur(radius=3))
            img  = bg
        else:
            img = Image.new("RGB", (W, H), (0, 0, 0))
            d0  = ImageDraw.Draw(img)
            for y in range(H):
                t = y / H
                d0.line([(0, y), (W, y)], fill=(int(30 * (1 - t)), int(10 * (1 - t)), int(60 * (1 - t))))

        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.rectangle([(0, 0),    (W, H // 2)], fill=(0, 0, 0, 160))
        od.rectangle([(0, H//2), (W, H)],      fill=(0, 0, 0, 140))
        img  = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        font_path = DRAWTEXT_FONT or ""
        try:
            font_title = ImageFont.truetype(font_path, 88) if font_path else ImageFont.load_default()
            font_med   = ImageFont.truetype(font_path, 56) if font_path else ImageFont.load_default()
            font_small = ImageFont.truetype(font_path, 38) if font_path else ImageFont.load_default()
        except Exception:
            font_title = font_med = font_small = ImageFont.load_default()

        draw.text((W // 2, 160), "SARA", font=font_med,
                  fill=(255, 255, 255), anchor="mm",
                  stroke_width=2, stroke_fill=(100, 60, 180))
        draw.rectangle([(120, 240), (W - 120, 243)], fill=(200, 160, 255))

        badge  = f"Part {part_number}"
        bx, by = W // 2, 330
        bw = int(draw.textlength(badge, font=font_small)) + 60
        draw.rounded_rectangle(
            [(bx - bw // 2, by - 31), (bx + bw // 2, by + 31)],
            radius=31, fill=(120, 60, 220),
        )
        draw.text((bx, by), badge, font=font_small, fill=(240, 220, 255), anchor="mm")

        wrapped = textwrap.wrap(story_title, width=20)
        line_h  = 120
        y_start = H // 2 - len(wrapped) * line_h // 2 + 60
        for i, line in enumerate(wrapped):
            draw.text(
                (W // 2, y_start + i * line_h), line.strip(), font=font_title,
                fill=(255, 255, 255), anchor="mm", stroke_width=6, stroke_fill=(0, 0, 0),
            )

        draw.rectangle([(120, H - 185), (W - 120, H - 182)], fill=(80, 50, 150))
        draw.text((W // 2, H - 130), story_code, font=font_small,
                  fill=(200, 200, 220), anchor="mm")

        img.save(str(output_path), "JPEG", quality=92)

    except Exception:
        try:
            from PIL import Image
            Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (20, 10, 40)).save(
                str(output_path), "JPEG", quality=85
            )
        except Exception:
            pass

    return output_path


# ---------------------------------------------------------------------------
# Background video assembly
# ---------------------------------------------------------------------------

def _assemble_background_loop(bg_videos: list, target_duration: float, output: Path):
    random.shuffle(bg_videos)
    accumulated = 0.0
    clip_list   = []

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
            random.shuffle(bg_videos)

    tmp_clips   = []
    concat_list = None
    try:
        for i, (vid, _) in enumerate(clip_list):
            tmp_clip = output.parent / f"_tmp_clip_{i}_{uuid.uuid4().hex[:6]}.mp4"
            result = _run_sub(
                [
                    FFMPEG_EXE, "-y", "-i", str(vid),
                    "-vf", (
                        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
                        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
                    ),
                    "-c:v", "libx264", "-an", "-preset", "ultrafast", "-crf", "28",
                    str(tmp_clip),
                ],
                capture_output=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Clip-Skalierung fehlgeschlagen (exit {result.returncode}): "
                    f"{result.stderr.decode('utf-8', errors='replace')[-800:]}"
                )
            tmp_clips.append(tmp_clip)

        concat_list = output.parent / f"_concat_{uuid.uuid4().hex[:6]}.txt"
        with open(concat_list, "w", encoding="utf-8") as f:
            for clip in tmp_clips:
                f.write(f"file '{str(clip.resolve()).replace(chr(92), '/')}'\n")

        result = _run_sub(
            [
                FFMPEG_EXE, "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-t", str(target_duration),
                "-c:v", "libx264", "-an", "-preset", "fast", "-crf", "23",
                str(output),
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Concat fehlgeschlagen (exit {result.returncode}): "
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
# Video creation
# ---------------------------------------------------------------------------

def create_video_for_part(story_part_id: int, job_id: int) -> Path:
    def progress(pct: int, label: str):
        _set_progress(job_id, pct, label)

    with db_session(write=False) as conn:
        part = conn.execute(
            """SELECT sp.*, s.code, s.title
               FROM story_parts sp
               JOIN stories s ON s.id = sp.story_id
               WHERE sp.id = ?""",
            (story_part_id,),
        ).fetchone()

    if not part:
        raise ValueError(f"Story-Teil {story_part_id} nicht gefunden.")

    story_code  = part["code"]
    story_title = part["title"]
    part_number = part["part_number"]
    text        = part["text"]

    output_dir = OUTPUTS_DIR / story_code
    output_dir.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / f"part_{part_number}.mp4"
    cover_path  = COVERS_DIR / f"{story_code}_part{part_number}.jpg"
    audio_path  = TTS_DIR / f"{story_code}_part{part_number}.wav"
    ass_path    = TTS_DIR / f"{story_code}_part{part_number}.ass"
    bg_concat   = TTS_DIR / f"{story_code}_part{part_number}_bg.mp4"
    tmp_files   = [audio_path, ass_path, bg_concat]

    try:
        # Step 1 — TTS
        progress(10, "Stimme wird generiert (Kokoro, lokal)...")
        tts_to_file(text, audio_path)
        audio_duration = get_audio_duration(audio_path)

        # Step 2 — ASS subtitles with word highlighting
        progress(30, "Wort-Timing wird berechnet…")
        build_word_timed_ass(text, ass_path, audio_path)

        # Step 3 — Background video loop
        progress(45, "Hintergrundvideos werden zusammengestellt…")
        bg_videos = [
            v for v in BACKGROUNDS_DIR.glob("*")
            if v.suffix.lower().lstrip(".") in ALLOWED_VIDEO_EXTENSIONS
        ]
        if not bg_videos:
            raise FileNotFoundError("Keine Hintergrundvideos gefunden. Bitte zuerst hochladen.")
        _assemble_background_loop(bg_videos, audio_duration, bg_concat)

        # Step 4 — Cover image
        progress(55, "Cover wird erstellt...")
        bg_frame_path = TTS_DIR / f"{story_code}_part{part_number}_cover_frame.jpg"
        try:
            _run_sub(
                [FFMPEG_EXE, "-y", "-i", str(bg_concat),
                 "-vframes", "1", "-q:v", "2", str(bg_frame_path)],
                check=True, capture_output=True,
            )
        except Exception:
            bg_frame_path = None

        create_cover_image(story_title, story_code, part_number, cover_path,
                           bg_frame_path=bg_frame_path)
        if bg_frame_path and Path(bg_frame_path).exists():
            Path(bg_frame_path).unlink(missing_ok=True)

        # Step 5 — FFmpeg render (9:16)
        progress(65, "Video wird gerendert (9:16 Hochformat)...")
        ass_escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
        vf_filter = (
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
            f"ass='{ass_escaped}'"
        )
        if DRAWTEXT_FONT:
            font_escaped = DRAWTEXT_FONT.replace("\\", "/").replace(":", "\\:")
            vf_filter += (
                ",drawtext=text='KI-generiert':"
                f"fontfile='{font_escaped}':"
                "fontsize=30:fontcolor=white@0.85:"
                "x=w-text_w-30:y=h-text_h-60:"
                "box=1:boxcolor=black@0.45:boxborderw=14"
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
            check=True, capture_output=True,
        )

        # Step 6 — Update DB
        progress(95, "Datenbank aktualisiert…")
        rel_cover = str(cover_path.relative_to(BASE_DIR)).replace("\\", "/")
        with db_session() as conn:
            conn.execute(
                "UPDATE story_parts SET cover_path=? WHERE id=?",
                (rel_cover, story_part_id),
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


# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------

def queue_worker():
    import time

    while True:
        try:
            job_id = None
            story_part_id = None
            with db_session() as conn:
                is_active = conn.execute(
                    "SELECT value FROM settings WHERE key='prod_active'"
                ).fetchone()
                if not (is_active and is_active["value"] == "0"):
                    # Atomic UPDATE+RETURNING: picks next pending job only if none is processing
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

                    if row is not None:
                        job_id        = row["id"]
                        story_part_id = row["story_part_id"]
                        conn.execute(
                            "UPDATE story_parts SET status='processing' WHERE id=?",
                            (story_part_id,),
                        )

            # Sleep NACH dem "with"-Block -- die Schreibsperre ist da laengst wieder frei.
            if job_id is None:
                time.sleep(QUEUE_POLL_INTERVAL)
                continue

            try:
                output_path = create_video_for_part(story_part_id, job_id)
                rel_path = str(output_path.relative_to(BASE_DIR)).replace("\\", "/")
                with db_session() as conn:
                    conn.execute(
                        "UPDATE queue SET status='done', progress_pct=100, "
                        "progress_label='Fertig! ✅', finished_at=datetime('now') WHERE id=?",
                        (job_id,),
                    )
                    conn.execute(
                        "UPDATE story_parts SET status='done', video_path=? WHERE id=?",
                        (rel_path, story_part_id),
                    )
                    # Automatischer YouTube-Upload: nur wenn eingeschaltet (Standard: an)
                    # und mindestens ein aktiver Kanal verbunden ist. Ohne verbundenen
                    # Kanal passiert hier bewusst nichts (kein Fehler).
                    auto_row = conn.execute(
                        "SELECT value FROM settings WHERE key='yt_auto_upload'"
                    ).fetchone()
                    if not auto_row or auto_row["value"] != "0":
                        active_accounts = [
                            r["id"] for r in conn.execute(
                                "SELECT id FROM youtube_accounts WHERE is_active=1"
                            ).fetchall()
                        ]
                        if active_accounts:
                            iv_row = conn.execute(
                                "SELECT value FROM settings WHERE key='yt_upload_interval_min'"
                            ).fetchone()
                            iv_min = float(iv_row["value"]) if iv_row and iv_row["value"] else 5.0
                            _enqueue_youtube(conn, story_part_id, active_accounts, iv_min)
            except Exception as e:
                with db_session() as conn:
                    conn.execute(
                        "UPDATE queue SET status='error', error_msg=?, "
                        "progress_label='Fehler ❌', finished_at=datetime('now') WHERE id=?",
                        (str(e), job_id),
                    )
                    conn.execute(
                        "UPDATE story_parts SET status='error' WHERE id=?",
                        (story_part_id,),
                    )

        except Exception:
            time.sleep(QUEUE_POLL_INTERVAL)


def start_queue_worker():
    threading.Thread(target=queue_worker, daemon=True, name="QueueWorker").start()


# ---------------------------------------------------------------------------
# YouTube-Upload
# ---------------------------------------------------------------------------

def _oauth_redirect_uri() -> str:
    if OAUTH_REDIRECT_BASE:
        return f"{OAUTH_REDIRECT_BASE}/auth/youtube/callback"
    return url_for("auth_youtube_callback", _external=True)


def _build_google_flow(redirect_uri: str, state: str = None, code_verifier: str = None) -> GoogleOAuthFlow:
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    flow = GoogleOAuthFlow.from_client_config(
        client_config, scopes=YOUTUBE_SCOPES, redirect_uri=redirect_uri, state=state
    )
    if code_verifier:
        # PKCE: denselben Verifier wie beim urspruenglichen authorization_url()-Aufruf
        # setzen, sonst lehnt Google den Token-Tausch mit invalid_grant ab.
        flow.code_verifier = code_verifier
    return flow


def _yt_privacy_status() -> str:
    with db_session(write=False) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='yt_privacy_status'").fetchone()
    val = (row["value"] if row and row["value"] else "public").strip().lower()
    return val if val in ("public", "unlisted", "private") else "public"


def _any_youtube_credentials() -> "GoogleCredentials | None":
    """Liefert authentifizierte Credentials von irgendeinem verbundenen Kanal --
    fuer oeffentlich lesbare Daten wie Aufrufzahlen reicht ein beliebiger
    verbundener Kanal, es muss nicht der hochladende Kanal sein."""
    with db_session(write=False) as conn:
        acc = conn.execute("SELECT * FROM youtube_accounts LIMIT 1").fetchone()
    if not acc:
        return None
    creds = GoogleCredentials(
        None,
        refresh_token=acc["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=YOUTUBE_SCOPES,
    )
    creds.refresh(GoogleAuthRequest())
    return creds


def upload_to_youtube(story_part_id: int, youtube_account_id: int) -> str:
    """
    Laedt ein fertiges Story-Teil-Video auf den angegebenen, per OAuth
    verbundenen YouTube-Kanal hoch (Resumable Upload, YouTube Data API v3).
    Nutzt Titel/Beschreibung/Hashtags aus den bereits vorhandenen Social-Daten
    des Teils und haengt #Shorts an, damit YouTube das Video als Short
    einordnet (vertikal + kurz reicht dafuer eigentlich schon, das Tag hilft
    zusaetzlich). Gibt die neue YouTube-Video-ID zurueck.
    """
    with db_session(write=False) as conn:
        part = conn.execute(
            "SELECT sp.*, s.title AS story_title FROM story_parts sp "
            "JOIN stories s ON s.id = sp.story_id WHERE sp.id=?",
            (story_part_id,),
        ).fetchone()
        account = conn.execute(
            "SELECT * FROM youtube_accounts WHERE id=?", (youtube_account_id,)
        ).fetchone()

    if not part or not part["video_path"]:
        raise ValueError("Video nicht gefunden oder noch nicht fertig produziert.")
    if not account:
        raise ValueError("YouTube-Konto nicht gefunden (evtl. entfernt).")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise ValueError("GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET nicht konfiguriert.")

    creds = GoogleCredentials(
        None,
        refresh_token=account["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=YOUTUBE_SCOPES,
    )
    creds.refresh(GoogleAuthRequest())

    social = json.loads(part["social_json"]) if part["social_json"] else {}
    title = (social.get("video_title") or f'{part["story_title"]} — Teil {part["part_number"]}').strip()
    if "#shorts" not in title.lower():
        title = (title + " #Shorts")[:100]
    else:
        title = title[:100]
    description = (social.get("description") or "").strip()
    hashtags = (social.get("hashtags") or "").strip()
    if "#shorts" not in hashtags.lower():
        hashtags = ("#Shorts " + hashtags).strip()
    full_description = "\n\n".join([p for p in [description, hashtags] if p])[:4900]

    video_path = BASE_DIR / part["video_path"]
    if not video_path.exists():
        raise ValueError(f"Videodatei nicht gefunden: {video_path}")

    youtube = yt_build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": title,
            "description": full_description,
            "categoryId": "24",  # Entertainment
        },
        "status": {
            "privacyStatus": _yt_privacy_status(),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _status, response = req.next_chunk()
    video_id = response["id"]

    cover_path = BASE_DIR / part["cover_path"] if part["cover_path"] else None
    if cover_path and cover_path.exists():
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(cover_path), mimetype="image/jpeg"),
            ).execute()
        except Exception as e:
            # Eigene Thumbnails brauchen ein telefonisch verifiziertes YouTube-Konto --
            # ohne das schlaegt dieser Aufruf fehl (z.B. 403 "doesn't have permissions
            # to upload and set custom video thumbnails"). Der Video-Upload selbst ist
            # davon unabhaengig schon erfolgreich -- Fehler nur ins Log, nicht abbrechen.
            print(f"[YouTube] Thumbnail fuer {video_id} konnte nicht gesetzt werden: {e}")

    return video_id


def youtube_worker():
    """
    Spiegelt genau das Muster von queue_worker(): pollt youtube_queue,
    verarbeitet immer nur EINEN faelligen Job gleichzeitig (nie mehrere
    parallel), respektiert scheduled_at fuer den zeitlichen Abstand zwischen
    Uploads. Laeuft komplett unabhaengig von der Video-Produktions-Queue.
    """
    import time as _time

    while True:
        try:
            with db_session() as conn:
                row = conn.execute("""
                    UPDATE youtube_queue SET
                        status     = 'processing',
                        started_at = datetime('now')
                    WHERE id = (
                        SELECT id FROM youtube_queue
                        WHERE  status = 'pending'
                          AND  (scheduled_at IS NULL OR scheduled_at <= datetime('now'))
                          AND  NOT EXISTS (SELECT 1 FROM youtube_queue WHERE status = 'processing')
                        ORDER BY id ASC LIMIT 1
                    )
                    RETURNING id, story_part_id, youtube_account_id
                """).fetchone()

            # Sleep NACH dem "with"-Block -- die Schreibsperre ist da laengst wieder frei.
            if row is None:
                _time.sleep(QUEUE_POLL_INTERVAL)
                continue

            job_id     = row["id"]
            part_id    = row["story_part_id"]
            account_id = row["youtube_account_id"]

            try:
                video_id = upload_to_youtube(part_id, account_id)
                with db_session() as conn:
                    conn.execute(
                        "UPDATE youtube_queue SET status='done', video_id=?, finished_at=datetime('now') "
                        "WHERE id=?",
                        (video_id, job_id),
                    )
                    acc = conn.execute(
                        "SELECT channel_title FROM youtube_accounts WHERE id=?", (account_id,)
                    ).fetchone()
                    ch_title = acc["channel_title"] if acc else "?"
                    conn.execute(
                        "INSERT INTO video_uploads (story_part_id, platform, notes) VALUES (?,?,?)",
                        (
                            part_id,
                            f"youtube:{account_id}",
                            f"{ch_title} · https://youtube.com/watch?v={video_id}",
                        ),
                    )
            except Exception as e:
                with db_session() as conn:
                    conn.execute(
                        "UPDATE youtube_queue SET status='error', error_msg=?, finished_at=datetime('now') "
                        "WHERE id=?",
                        (str(e), job_id),
                    )

        except Exception:
            _time.sleep(QUEUE_POLL_INTERVAL)


def start_youtube_worker():
    threading.Thread(target=youtube_worker, daemon=True, name="YouTubeWorker").start()


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------

@app.route("/")
@app.route("/story")
@app.route("/video")
@app.route("/library")
@app.route("/library/<path:rest>")
@app.route("/upload")
@app.route("/channels")
def index(rest=None):
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — Config
# ---------------------------------------------------------------------------

@app.route("/favicon.svg")
def favicon_svg():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="6" fill="#4F378B"/>'
        '<text x="16" y="23" font-family="sans-serif" font-size="19" font-weight="700"'
        ' text-anchor="middle" fill="#EADDFF">S</text>'
        "</svg>"
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/favicon.ico")
def favicon_ico():
    return "", 204



# ---------------------------------------------------------------------------
# Routes — Stories
# ---------------------------------------------------------------------------

@app.route("/api/stories", methods=["GET"])
def api_get_stories():
    with db_session(write=False) as conn:
        stories = conn.execute(
            "SELECT id, code, title, total_parts, created_at FROM stories ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for s in stories:
            row = dict(s)
            row["video_done_parts"] = conn.execute(
                "SELECT COUNT(*) FROM story_parts WHERE story_id=? AND video_path IS NOT NULL AND video_path!=''",
                (s["id"],),
            ).fetchone()[0]
            row["video_queue_parts"] = conn.execute(
                """SELECT COUNT(*) FROM story_parts sp
                   JOIN queue q ON q.story_part_id = sp.id
                   WHERE sp.story_id=? AND q.status IN ('pending','processing')""",
                (s["id"],),
            ).fetchone()[0]
            result.append(row)
    return jsonify(result)


@app.route("/api/stories/<int:story_id>", methods=["GET"])
def api_get_story(story_id: int):
    with db_session(write=False) as conn:
        story = conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
        if not story:
            abort(404)
        parts = conn.execute(
            "SELECT * FROM story_parts WHERE story_id=? ORDER BY part_number",
            (story_id,),
        ).fetchall()
    return jsonify({"story": dict(story), "parts": [dict(p) for p in parts]})




@app.route("/api/stories/<int:story_id>", methods=["DELETE"])
def api_delete_story(story_id: int):
    with db_session() as conn:
        story = conn.execute("SELECT code FROM stories WHERE id=?", (story_id,)).fetchone()
        if not story:
            return jsonify({"error": "Story nicht gefunden"}), 404

        story_code = story["code"]
        parts = conn.execute(
            "SELECT id, video_path, cover_path FROM story_parts WHERE story_id=?",
            (story_id,),
        ).fetchall()

        for p in parts:
            for rel_path in [p["video_path"], p["cover_path"]]:
                if rel_path:
                    try:
                        (BASE_DIR / rel_path).unlink(missing_ok=True)
                    except Exception:
                        pass
            conn.execute("DELETE FROM video_uploads WHERE story_part_id=?", (p["id"],))
            conn.execute("DELETE FROM queue WHERE story_part_id=?", (p["id"],))

        story_out_dir = OUTPUTS_DIR / story_code
        if story_out_dir.exists():
            try:
                shutil.rmtree(story_out_dir)
            except Exception:
                pass

        try:
            for f in TTS_DIR.glob(f"{story_code}_part*"):
                f.unlink()
        except Exception:
            pass

        conn.execute("DELETE FROM story_parts WHERE story_id=?", (story_id,))
        conn.execute("DELETE FROM stories WHERE id=?", (story_id,))

    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Routes — Video creation & queue
# ---------------------------------------------------------------------------

@app.route("/api/create-video", methods=["POST"])
def api_create_video():
    data        = request.get_json(silent=True) or {}
    story_id    = data.get("story_id")
    part_number = data.get("part_number")

    if not story_id:
        return jsonify({"error": "story_id fehlt."}), 400

    with db_session() as conn:
        query  = "SELECT id FROM story_parts WHERE story_id=?"
        params = [story_id]
        if part_number:
            query += " AND part_number=?"
            params.append(part_number)
        parts = conn.execute(query, params).fetchall()

        if not parts:
            return jsonify({"error": "Keine Story-Teile gefunden."}), 404

        interval_min = int(
            conn.execute("SELECT value FROM settings WHERE key='prod_interval_min'").fetchone()["value"] or 0
        )
        job_ids = _enqueue_parts(conn, [p["id"] for p in parts], interval_min)

    return jsonify({"success": True, "job_ids": job_ids})


@app.route("/api/queue", methods=["GET"])
def api_get_queue():
    with db_session(write=False) as conn:
        jobs = conn.execute(
            """SELECT q.id, q.status, q.error_msg, q.created_at, q.started_at, q.finished_at,
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
    with db_session(write=False) as conn:
        stats = conn.execute(
            "SELECT status, COUNT(*) AS count FROM queue GROUP BY status"
        ).fetchall()
    return jsonify({row["status"]: row["count"] for row in stats})


@app.route("/api/queue/live")
def api_queue_live():
    """SSE stream: sends current queue state every 1.5 s."""
    def generate():
        import time
        while True:
            try:
                conn = get_db()
                try:
                    is_active   = conn.execute("SELECT value FROM settings WHERE key='prod_active'").fetchone()
                    prod_active = (is_active["value"] != "0") if is_active else True
                    jobs = conn.execute(
                        """SELECT q.id, q.status, q.progress_pct, q.progress_label,
                                  q.error_msg, q.created_at, q.started_at, q.finished_at,
                                  sp.part_number, sp.story_id, s.title, s.code
                           FROM queue q
                           JOIN story_parts sp ON sp.id = q.story_part_id
                           JOIN stories s ON s.id = sp.story_id
                           ORDER BY q.id DESC LIMIT 100"""
                    ).fetchall()
                    payload = json.dumps({
                        "jobs": [dict(j) for j in jobs],
                        "prod_active": prod_active,
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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/queue/<int:job_id>", methods=["DELETE"])
def api_delete_queue_job(job_id: int):
    with db_session() as conn:
        job = conn.execute("SELECT status, story_part_id FROM queue WHERE id=?", (job_id,)).fetchone()
        if not job:
            return jsonify({"error": "Job nicht gefunden"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "Laufender Job kann nicht gelöscht werden."}), 400
        conn.execute("DELETE FROM queue WHERE id=?", (job_id,))
        if job["status"] != "done":
            conn.execute("UPDATE story_parts SET status='pending' WHERE id=?", (job["story_part_id"],))
    return jsonify({"success": True})


@app.route("/api/queue/<int:job_id>/restart", methods=["POST"])
def api_restart_queue_job(job_id: int):
    with db_session() as conn:
        job = conn.execute("SELECT status, story_part_id FROM queue WHERE id=?", (job_id,)).fetchone()
        if not job:
            return jsonify({"error": "Job nicht gefunden"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "Laufender Job kann nicht neugestartet werden."}), 400
        conn.execute(
            "UPDATE queue SET status='pending', error_msg=NULL, progress_label='Wartet...', progress_pct=0 WHERE id=?",
            (job_id,),
        )
        conn.execute("UPDATE story_parts SET status='pending' WHERE id=?", (job["story_part_id"],))
    return jsonify({"success": True})


@app.route("/api/queue/<int:job_id>/cancel", methods=["POST"])
def api_cancel_queue_job(job_id: int):
    with db_session() as conn:
        job = conn.execute("SELECT status, story_part_id FROM queue WHERE id=?", (job_id,)).fetchone()
        if not job:
            return jsonify({"error": "Job nicht gefunden"}), 404
        if job["status"] == "processing":
            return jsonify({"error": "Laufender Job kann nicht abgebrochen werden."}), 400
        conn.execute(
            "UPDATE queue SET status='error', error_msg='Abgebrochen durch Benutzer', "
            "progress_label='Abgebrochen' WHERE id=?",
            (job_id,),
        )
        conn.execute("UPDATE story_parts SET status='pending' WHERE id=?", (job["story_part_id"],))
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Routes — Settings
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json() or {}
        with db_session() as conn:
            for k, v in data.items():
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (k, str(v))
                )
                if k == "prod_active" and str(v) == "0":
                    _kill_active_subs()
        return jsonify({"success": True})

    with db_session(write=False) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


# ---------------------------------------------------------------------------
# Routes — Backgrounds
# ---------------------------------------------------------------------------

@app.route("/api/backgrounds", methods=["GET"])
def api_get_backgrounds():
    with db_session(write=False) as conn:
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
            "INSERT INTO backgrounds (filename, original_name) VALUES (?,?)",
            (unique_name, original_name),
        )
    return jsonify({"success": True, "filename": unique_name, "original_name": original_name})


@app.route("/api/backgrounds/<filename>", methods=["DELETE"])
def api_delete_background(filename: str):
    safe_name = Path(filename).name
    try:
        (BACKGROUNDS_DIR / safe_name).unlink(missing_ok=True)
    except Exception:
        pass
    with db_session() as conn:
        conn.execute("DELETE FROM backgrounds WHERE filename=?", (safe_name,))
    return jsonify({"success": True})


@app.route("/backgrounds/<filename>")
def serve_background(filename: str):
    return send_from_directory(str(BACKGROUNDS_DIR), Path(filename).name)


@app.route("/covers/<filename>")
def serve_cover(filename: str):
    return send_from_directory(str(COVERS_DIR), Path(filename).name)


# ---------------------------------------------------------------------------
# Routes — Library (completed videos)
# ---------------------------------------------------------------------------

@app.route("/api/videos", methods=["GET"])
def api_get_videos():
    with db_session(write=False) as conn:
        videos = conn.execute(
            """SELECT sp.id, sp.part_number, sp.video_path, sp.cover_path, sp.status,
                      sp.social_json, sp.text,
                      s.id AS story_id, s.code, s.title, s.created_at
               FROM story_parts sp
               JOIN stories s ON s.id = sp.story_id
               WHERE sp.status='done' AND sp.video_path IS NOT NULL
               ORDER BY s.created_at DESC, sp.part_number ASC"""
        ).fetchall()
        uploads = conn.execute("SELECT * FROM video_uploads").fetchall()

    uploads_by_part: dict = {}
    for u in uploads:
        uploads_by_part.setdefault(u["story_part_id"], []).append(dict(u))

    grouped: dict = {}
    for v in videos:
        sid = v["story_id"]
        if sid not in grouped:
            grouped[sid] = {
                "story_id":   sid,
                "code":       v["code"],
                "title":      v["title"],
                "created_at": v["created_at"],
                "parts":      [],
            }
        social = {}
        try:
            if v["social_json"]:
                social = json.loads(v["social_json"])
        except Exception:
            pass
        grouped[sid]["parts"].append({
            "id":          v["id"],
            "part_number": v["part_number"],
            "video_path":  v["video_path"],
            "cover_path":  v["cover_path"],
            "text":        v["text"] or "",
            "social":      social,
            "uploads":     uploads_by_part.get(v["id"], []),
        })

    return jsonify(list(grouped.values()))


@app.route("/api/videos/<int:part_id>", methods=["DELETE"])
def api_delete_video(part_id: int):
    with db_session() as conn:
        part = conn.execute(
            "SELECT video_path, cover_path FROM story_parts WHERE id=?", (part_id,)
        ).fetchone()
        if not part:
            return jsonify({"error": "Teil nicht gefunden"}), 404

        for rel_path in [part["video_path"], part["cover_path"]]:
            if rel_path:
                try:
                    (BASE_DIR / rel_path).unlink(missing_ok=True)
                except Exception:
                    pass

        conn.execute(
            "UPDATE story_parts SET video_path=NULL, cover_path=NULL, status='pending' WHERE id=?",
            (part_id,),
        )
        conn.execute("DELETE FROM video_uploads WHERE story_part_id=?", (part_id,))
        conn.execute("DELETE FROM queue WHERE story_part_id=? AND status IN ('done','error')", (part_id,))

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
# Routes — Upload tracking
# ---------------------------------------------------------------------------

@app.route("/api/uploads", methods=["POST"])
def api_add_upload():
    data          = request.get_json(silent=True) or {}
    story_part_id = data.get("story_part_id")
    platform      = data.get("platform", "tiktok")
    notes         = data.get("notes", "")
    if not story_part_id:
        return jsonify({"error": "story_part_id fehlt."}), 400
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO video_uploads (story_part_id, platform, notes) VALUES (?,?,?)",
            (story_part_id, platform, notes),
        )
        new_id = cur.lastrowid
    return jsonify({
        "success":     True,
        "id":          new_id,
        "uploaded_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/uploads/<int:upload_id>", methods=["DELETE"])
def api_delete_upload(upload_id: int):
    with db_session() as conn:
        conn.execute("DELETE FROM video_uploads WHERE id=?", (upload_id,))
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Routes — Prompt builder & story import
# ---------------------------------------------------------------------------

@app.route("/api/get-prompt-template", methods=["POST"])
def api_get_prompt_template():
    data = request.get_json(silent=True) or {}
    idea = (data.get("idea") or "").strip()
    if not idea:
        return jsonify({"error": "Bitte eine Idee eingeben."}), 400
    user_msg = f"Create a story about: {idea}"
    return jsonify({
        "system_prompt": SYSTEM_PROMPT,
        "user_message":  user_msg,
        "combined":      f"[SYSTEM]\n{SYSTEM_PROMPT}\n\n[USER]\n{user_msg}",
    })


@app.route("/api/import-story", methods=["POST"])
def api_import_story():
    data       = request.get_json(silent=True) or {}
    raw_json   = (data.get("json_text") or "").strip()
    dry_run    = data.get("dry_run", False)
    auto_queue = data.get("auto_queue", False)

    if not raw_json:
        return jsonify({"error": "Kein JSON eingegeben."}), 400

    match = re.search(r"\{.*\}", raw_json, re.DOTALL)
    if not match:
        return jsonify({"error": "Kein gültiges JSON gefunden."}), 400

    try:
        story = json.loads(match.group())
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSON-Parsing-Fehler: {e}"}), 400

    for field in ["title", "total_parts", "keywords", "parts"]:
        if field not in story:
            return jsonify({"error": f"Feld '{field}' fehlt im JSON."}), 400

    dup_check = check_duplicate(story["keywords"], story.get("title", ""))

    if dry_run:
        return jsonify({"success": True, "duplicate_check": dup_check, "story": story})

    if dup_check["is_exact"] and not data.get("force_exact"):
        return jsonify({"error": f"Diese Story existiert bereits: \"{dup_check['similar_story']['title']}\" [{dup_check['similar_story']['code']}]"}), 409

    code = generate_story_code()
    try:
        with db_session() as conn:
            cur = conn.execute(
                "INSERT INTO stories (code, title, keywords_json, total_parts) VALUES (?,?,?,?)",
                (code, story["title"], json.dumps(story["keywords"]), story["total_parts"]),
            )
            story_id = cur.lastrowid
            part_ids = []
            for part in story["parts"]:
                social_json = json.dumps(part["social"]) if part.get("social") else None
                pc = conn.execute(
                    "INSERT INTO story_parts (story_id, part_number, text, cliffhanger, social_json) VALUES (?,?,?,?,?)",
                    (story_id, part["part_number"], part["text"], part.get("cliffhanger_hint", ""), social_json),
                )
                part_ids.append(pc.lastrowid)

            if auto_queue:
                interval_min = int(
                    conn.execute("SELECT value FROM settings WHERE key='prod_interval_min'").fetchone()["value"] or 0
                )
                _enqueue_parts(conn, part_ids, interval_min)

    except Exception as e:
        return jsonify({"error": f"Datenbank-Fehler: {e}"}), 500

    return jsonify({
        "success":         True,
        "code":            code,
        "story_id":        story_id,
        "title":           story["title"],
        "duplicate_check": dup_check,
        "auto_queued":     auto_queue,
    })


# ---------------------------------------------------------------------------
# Routes — YouTube-Konten & Upload
# ---------------------------------------------------------------------------

@app.route("/auth/youtube/login")
def auth_youtube_login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return jsonify({
            "error": "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET nicht gesetzt. "
                     "Siehe README, Abschnitt YouTube-Upload."
        }), 500
    redirect_uri = _oauth_redirect_uri()
    flow = _build_google_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    session["yt_oauth_state"] = state
    session["yt_oauth_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/auth/youtube/callback")
def auth_youtube_callback():
    error = request.args.get("error")
    if error:
        return f"Google-Anmeldung abgebrochen oder fehlgeschlagen: {error}", 400
    state = session.get("yt_oauth_state")
    code_verifier = session.get("yt_oauth_code_verifier")
    redirect_uri = _oauth_redirect_uri()
    flow = _build_google_flow(redirect_uri, state=state, code_verifier=code_verifier)
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception as e:
        return f"OAuth-Fehler: {e}", 400
    creds = flow.credentials

    youtube = yt_build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        return (
            "Auf diesem Google-Konto wurde kein YouTube-Kanal gefunden. "
            "Erst einen Kanal anlegen, dann erneut verbinden.",
            400,
        )
    ch = items[0]
    channel_id = ch["id"]
    channel_title = ch["snippet"]["title"]

    if not creds.refresh_token:
        return (
            "Google hat keinen Refresh-Token geliefert (Konto war vermutlich schon "
            "einmal verbunden). Zugriff bei myaccount.google.com/permissions fuer "
            "diese App entfernen und erneut verbinden.",
            400,
        )

    with db_session() as conn:
        conn.execute(
            "INSERT INTO youtube_accounts (channel_id, channel_title, refresh_token, is_active) "
            "VALUES (?,?,?,1) "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "channel_title=excluded.channel_title, refresh_token=excluded.refresh_token, is_active=1",
            (channel_id, channel_title, creds.refresh_token),
        )
    return redirect("/channels")


@app.route("/api/youtube/accounts", methods=["GET"])
def api_youtube_accounts():
    with db_session(write=False) as conn:
        rows = conn.execute(
            "SELECT id, channel_id, channel_title, is_active, added_at "
            "FROM youtube_accounts ORDER BY added_at"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/youtube/accounts/<int:account_id>/toggle", methods=["POST"])
def api_youtube_account_toggle(account_id: int):
    data = request.get_json(silent=True) or {}
    active = 1 if data.get("is_active") else 0
    with db_session() as conn:
        conn.execute("UPDATE youtube_accounts SET is_active=? WHERE id=?", (active, account_id))
    return jsonify({"success": True})


@app.route("/api/youtube/accounts/<int:account_id>", methods=["DELETE"])
def api_youtube_account_delete(account_id: int):
    with db_session() as conn:
        conn.execute("DELETE FROM youtube_accounts WHERE id=?", (account_id,))
    return jsonify({"success": True})


@app.route("/api/youtube/upload", methods=["POST"])
def api_youtube_upload():
    """Manuelles Einreihen eines fertigen Videos fuer den YouTube-Upload --
    ohne account_ids werden alle aktiven verbundenen Kanaele gleichzeitig
    beliefert (jeweils zeitversetzt um das eingestellte Intervall)."""
    data = request.get_json(silent=True) or {}
    story_part_id = data.get("story_part_id")
    account_ids = data.get("account_ids") or None
    if not story_part_id:
        return jsonify({"error": "story_part_id fehlt."}), 400
    with db_session() as conn:
        if not account_ids:
            account_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM youtube_accounts WHERE is_active=1"
                ).fetchall()
            ]
        if not account_ids:
            return jsonify({"error": "Kein aktiver YouTube-Kanal verbunden."}), 400
        iv_row = conn.execute(
            "SELECT value FROM settings WHERE key='yt_upload_interval_min'"
        ).fetchone()
        iv_min = float(iv_row["value"]) if iv_row and iv_row["value"] else 5.0
        job_ids = _enqueue_youtube(conn, story_part_id, account_ids, iv_min)
    return jsonify({"success": True, "job_ids": job_ids})


@app.route("/api/youtube/stats", methods=["GET"])
def api_youtube_stats():
    """Aufrufzahlen/Likes/Kommentare fuer eine Liste von YouTube-Video-IDs --
    oeffentliche Daten, jeder verbundene Kanal darf sie abfragen."""
    ids = [v.strip() for v in (request.args.get("video_ids") or "").split(",") if v.strip()]
    if not ids:
        return jsonify({})
    creds = _any_youtube_credentials()
    if not creds:
        return jsonify({})
    youtube = yt_build("youtube", "v3", credentials=creds, cache_discovery=False)
    result = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        resp = youtube.videos().list(part="statistics", id=",".join(chunk)).execute()
        for item in resp.get("items", []):
            st = item.get("statistics", {})
            result[item["id"]] = {
                "views": int(st.get("viewCount", 0)),
                "likes": int(st["likeCount"]) if "likeCount" in st else None,
                "comments": int(st["commentCount"]) if "commentCount" in st else None,
            }
    return jsonify(result)


@app.route("/api/youtube/queue", methods=["GET"])
def api_youtube_queue():
    with db_session(write=False) as conn:
        rows = conn.execute("""
            SELECT q.*, a.channel_title, sp.part_number, s.title AS story_title, s.code AS story_code
            FROM youtube_queue q
            JOIN youtube_accounts a ON a.id = q.youtube_account_id
            JOIN story_parts sp     ON sp.id = q.story_part_id
            JOIN stories s          ON s.id = sp.story_id
            ORDER BY q.id DESC LIMIT 50
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/youtube/queue/<int:job_id>", methods=["DELETE"])
def api_youtube_queue_delete(job_id: int):
    with db_session() as conn:
        conn.execute(
            "DELETE FROM youtube_queue WHERE id=? AND status IN ('pending','error')", (job_id,)
        )
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for d in [DATA_DIR, BACKGROUNDS_DIR, OUTPUTS_DIR, TTS_DIR, COVERS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    init_db()
    start_queue_worker()
    start_youtube_worker()

    port = int(os.environ.get("PORT", 7842))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
