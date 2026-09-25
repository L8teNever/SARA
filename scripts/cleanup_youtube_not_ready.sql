-- Einmalige Aufraeumhilfe fuer die YouTube-Queue, nachdem "Video nicht bereit"
-- faelschlich als error + neuer Tages-Slot behandelt wurde.
-- NICHT ausfuehren gegen done-Jobs. Vorher Backup der SQLite-Datei anlegen.
--
-- Beispiel:
--   sqlite3 /app/data/sara.db < scripts/cleanup_youtube_not_ready.sql
--
BEGIN;

-- 1) Error-Jobs ohne Video, zu denen bereits ein pending/processing-Retry
--    fuer dasselbe Teil+Konto existiert: den Error-Datensatz entfernen
--    (der Retry bleibt).
DELETE FROM youtube_queue
WHERE status = 'error'
  AND (
        error_msg LIKE 'Video nicht gefunden oder noch nicht fertig produziert%'
     OR error_msg LIKE 'Videodatei nicht gefunden%'
  )
  AND EXISTS (
    SELECT 1 FROM youtube_queue AS q2
    WHERE q2.story_part_id = youtube_queue.story_part_id
      AND q2.youtube_account_id = youtube_queue.youtube_account_id
      AND q2.id != youtube_queue.id
      AND q2.status IN ('pending', 'processing')
  );

-- 2) Uebrige solche Error-Jobs wieder auf pending setzen (kein neuer Slot).
UPDATE youtube_queue
SET status = 'pending',
    started_at = NULL,
    finished_at = NULL,
    error_msg = 'Produktion noch nicht fertig — Job nach Cleanup reaktiviert',
    scheduled_at = datetime('now', '+10 minutes')
WHERE status = 'error'
  AND (
        error_msg LIKE 'Video nicht gefunden oder noch nicht fertig produziert%'
     OR error_msg LIKE 'Videodatei nicht gefunden%'
  );

-- 3) Doppelte pending-Retries fuer dasselbe Teil+Konto: den aeltesten behalten.
DELETE FROM youtube_queue
WHERE status = 'pending'
  AND EXISTS (
    SELECT 1 FROM youtube_queue AS q2
    WHERE q2.story_part_id = youtube_queue.story_part_id
      AND q2.youtube_account_id = youtube_queue.youtube_account_id
      AND q2.status IN ('pending', 'processing')
      AND q2.id < youtube_queue.id
  );

-- 4) Pending-Jobs, deren Video noch fehlt, nicht wochenlang in der Zukunft
--    liegen lassen (done bleibt unangetastet).
UPDATE youtube_queue
SET scheduled_at = datetime('now', '+10 minutes'),
    error_msg = 'Produktion noch nicht fertig — Zeitplan nach Cleanup korrigiert'
WHERE status = 'pending'
  AND story_part_id IN (
    SELECT id FROM story_parts WHERE video_path IS NULL OR TRIM(video_path) = ''
  );

COMMIT;
