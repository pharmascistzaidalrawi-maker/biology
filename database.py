import asyncio
import os
import threading
from datetime import date, timedelta

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

def _env_int(name,default):
    value=os.getenv(name)
    try: return int(str(value).strip()) if value is not None and str(value).strip() else int(default)
    except (TypeError,ValueError): return int(default)

def _env_bool(name,default=False):
    value=os.getenv(name)
    if value is None or not str(value).strip(): return bool(default)
    return str(value).strip().lower() in {'1','true','yes','on','enabled'}

DATABASE_URL = os.getenv("DATABASE_URL", "")
OWNER_CHAT_ID = _env_int("OWNER_CHAT_ID",0)
NEON_ECO_MODE = _env_bool("NEON_ECO_MODE",True)
DB_POOL_MIN = max(0,_env_int("DB_POOL_MIN",0 if NEON_ECO_MODE else 1))
DB_POOL_MAX = max(DB_POOL_MIN or 1,_env_int("DB_POOL_MAX",4 if NEON_ECO_MODE else 6))
DB_CONNECT_TIMEOUT = max(3,_env_int("DB_CONNECT_TIMEOUT",10))
DB_POOL_MAX_IDLE = max(10,_env_int("DB_POOL_MAX_IDLE",120))
_DB_POOL = None
_DB_POOL_LOCK = threading.Lock()


def _pool():
    global _DB_POOL
    if _DB_POOL is None:
        with _DB_POOL_LOCK:
            if _DB_POOL is None:
                if not DATABASE_URL: raise RuntimeError("DATABASE_URL is required")
                _DB_POOL=ConnectionPool(
                    conninfo=DATABASE_URL,min_size=DB_POOL_MIN,max_size=DB_POOL_MAX,
                    timeout=DB_CONNECT_TIMEOUT,max_idle=DB_POOL_MAX_IDLE,max_lifetime=900,
                    kwargs={"sslmode":"require","row_factory":dict_row,"connect_timeout":DB_CONNECT_TIMEOUT},
                    check=ConnectionPool.check_connection,
                    open=True,name="physics-neon-pool")
    return _DB_POOL


def connect():
    """Return a pooled connection context manager safe for worker threads."""
    return _pool().connection(timeout=DB_CONNECT_TIMEOUT)


def close_pool():
    global _DB_POOL
    with _DB_POOL_LOCK:
        if _DB_POOL is not None:
            _DB_POOL.close(); _DB_POOL=None


async def run(fn):
    return await asyncio.get_running_loop().run_in_executor(None, fn)


def _set_xp_event(cur,user_id,delta,reason,event_key):
    """Apply an idempotent XP event and keep the ledger equal to the balance."""
    cur.execute("SELECT xp FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); student=cur.fetchone()
    if not student: return 0
    cur.execute("SELECT delta FROM biology_xp_log WHERE event_key=%s FOR UPDATE;",(event_key,)); old=cur.fetchone()
    old_delta=int(old["delta"] if old else 0)
    base_balance=max(0,int(student["xp"] or 0)-old_delta)
    applied_delta=max(-base_balance,int(delta))
    difference=applied_delta-old_delta
    if old: cur.execute("UPDATE biology_xp_log SET delta=%s,reason=%s,created_at=CURRENT_TIMESTAMP WHERE event_key=%s;",(applied_delta,reason,event_key))
    else: cur.execute("INSERT INTO biology_xp_log(user_id,delta,reason,event_key) VALUES(%s,%s,%s,%s);",(user_id,applied_delta,reason,event_key))
    if difference: cur.execute("UPDATE biology_students SET xp=%s WHERE user_id=%s;",(base_balance+applied_delta,user_id))
    return difference


def init_db():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS biology_students(
            user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT NOT NULL,
            school TEXT NOT NULL, target_grade TEXT NOT NULL,
            approved BOOLEAN NOT NULL DEFAULT FALSE, xp INTEGER NOT NULL DEFAULT 0,
            warnings INTEGER NOT NULL DEFAULT 0, registered_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_preparations(
            prep_no INTEGER PRIMARY KEY, target_date DATE NOT NULL UNIQUE,
            lectures TEXT NOT NULL, published BOOLEAN NOT NULL DEFAULT FALSE,
            published_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS biology_tasks(
            id SERIAL PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('homework','exam')),
            title TEXT NOT NULL, chat_id BIGINT NOT NULL, thread_id BIGINT,
            source_message_id BIGINT NOT NULL, payload_type TEXT NOT NULL DEFAULT 'text',
            file_id TEXT, media_group_id TEXT, text_content TEXT NOT NULL DEFAULT '', deadline TIMESTAMPTZ NOT NULL,
            xp_reward INTEGER NOT NULL DEFAULT 10, closed BOOLEAN NOT NULL DEFAULT FALSE,
            warned BOOLEAN NOT NULL DEFAULT FALSE, created_by BIGINT, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, source_message_id)
        );
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS linked_lectures TEXT NOT NULL DEFAULT '';
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS exam_pending_activation BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS exam_definition_id INTEGER;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS target_scope TEXT NOT NULL DEFAULT 'course';
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS exam_duration_hours INTEGER NOT NULL DEFAULT 2;
        CREATE UNIQUE INDEX IF NOT EXISTS biology_linked_exam_student_unique ON biology_tasks(exam_definition_id,target_scope) WHERE exam_definition_id IS NOT NULL AND target_scope LIKE 'student:%';
        CREATE INDEX IF NOT EXISTS biology_linked_exam_student_idx ON biology_tasks(exam_definition_id,target_scope);
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS optional_practice BOOLEAN NOT NULL DEFAULT FALSE;
        CREATE TABLE IF NOT EXISTS biology_pending_tasks(
            id SERIAL PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('homework','exam')),
            title TEXT NOT NULL, chat_id BIGINT NOT NULL, thread_id BIGINT,
            source_message_id BIGINT NOT NULL, payload_type TEXT NOT NULL DEFAULT 'text',
            file_id TEXT, media_group_id TEXT, text_content TEXT NOT NULL DEFAULT '',
            created_by BIGINT NOT NULL, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, source_message_id)
        );
        CREATE TABLE IF NOT EXISTS biology_pending_task_media(
            id SERIAL PRIMARY KEY, pending_id INTEGER REFERENCES biology_pending_tasks(id) ON DELETE CASCADE,
            payload_type TEXT NOT NULL, file_id TEXT NOT NULL, source_message_id BIGINT NOT NULL,
            UNIQUE(pending_id,file_id)
        );
        CREATE TABLE IF NOT EXISTS biology_task_media(
            id SERIAL PRIMARY KEY, task_id INTEGER REFERENCES biology_tasks(id) ON DELETE CASCADE,
            payload_type TEXT NOT NULL, file_id TEXT NOT NULL, source_message_id BIGINT NOT NULL,
            UNIQUE(task_id,file_id)
        );
        CREATE TABLE IF NOT EXISTS biology_task_students(
            task_id INTEGER REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(task_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS biology_personal_preparations(
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            target_date DATE NOT NULL,
            chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
            lectures TEXT NOT NULL,
            prep_no INTEGER,
            notified BOOLEAN NOT NULL DEFAULT FALSE,
            notified_at TIMESTAMPTZ,
            UNIQUE(user_id,target_date)
        );
        CREATE TABLE IF NOT EXISTS biology_submissions(
            task_id INTEGER REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            submission_no INTEGER NOT NULL DEFAULT 1, message_id BIGINT,
            file_unique_id TEXT NOT NULL DEFAULT '', submitted_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(task_id,user_id)
        );
        ALTER TABLE biology_submissions ADD COLUMN IF NOT EXISTS media_group_id TEXT;
        CREATE TABLE IF NOT EXISTS biology_submission_files(
            task_id INTEGER REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            message_id BIGINT NOT NULL, file_unique_id TEXT NOT NULL,
            media_group_id TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(task_id,user_id,message_id), UNIQUE(task_id,file_unique_id)
        );
        INSERT INTO biology_submission_files(task_id,user_id,message_id,file_unique_id)
        SELECT task_id,user_id,message_id,file_unique_id FROM biology_submissions
        WHERE message_id IS NOT NULL AND file_unique_id<>'' ON CONFLICT DO NOTHING;
        CREATE TABLE IF NOT EXISTS biology_submission_review_messages(
            chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
            task_id INTEGER REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            PRIMARY KEY(chat_id,message_id)
        );
        CREATE TABLE IF NOT EXISTS biology_exam_archive(
            id SERIAL PRIMARY KEY, chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
            lecture INTEGER, title TEXT NOT NULL, created_by BIGINT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_exam_archive_media(
            id SERIAL PRIMARY KEY, archive_id INTEGER REFERENCES biology_exam_archive(id) ON DELETE CASCADE,
            payload_type TEXT NOT NULL, file_id TEXT NOT NULL,
            UNIQUE(archive_id,file_id)
        );
        CREATE TABLE IF NOT EXISTS biology_exam_corrections(
            id SERIAL PRIMARY KEY, task_id INTEGER REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            payload_type TEXT NOT NULL, file_id TEXT NOT NULL, grade INTEGER NOT NULL CHECK(grade BETWEEN 0 AND 100),
            corrected_by BIGINT NOT NULL, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_resources(
            id SERIAL PRIMARY KEY,
            category TEXT NOT NULL CHECK(category IN ('booklet','summary','model_answer','ministerial')),
            chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
            prep_no INTEGER NOT NULL,
            title TEXT NOT NULL, created_by BIGINT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_resource_media(
            id SERIAL PRIMARY KEY, resource_id INTEGER REFERENCES biology_resources(id) ON DELETE CASCADE,
            payload_type TEXT NOT NULL, file_id TEXT NOT NULL,
            UNIQUE(resource_id,file_id)
        );
        CREATE TABLE IF NOT EXISTS biology_lecture_progress(
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            chapter INTEGER NOT NULL, lecture INTEGER NOT NULL,
            opened_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
            PRIMARY KEY(user_id,chapter,lecture)
        );
        CREATE TABLE IF NOT EXISTS biology_warning_log(
            id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, task_id INTEGER,
            reason TEXT NOT NULL, issued_by BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_cumulative_exam(
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
            exam_at TIMESTAMPTZ NOT NULL, syllabus TEXT NOT NULL,
            updated_by BIGINT NOT NULL, updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_settings(
            key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS biology_scheduled_tasks(
            id SERIAL PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('homework','exam')),
            title TEXT NOT NULL,
            payload_type TEXT NOT NULL,
            file_id TEXT NOT NULL,
            publish_at TIMESTAMPTZ NOT NULL,
            submission_hours INTEGER NOT NULL CHECK(submission_hours BETWEEN 1 AND 720),
            created_by BIGINT NOT NULL,
            published BOOLEAN NOT NULL DEFAULT FALSE,
            published_message_id BIGINT,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_scheduled_task_media(
            id SERIAL PRIMARY KEY,
            schedule_id INTEGER REFERENCES biology_scheduled_tasks(id) ON DELETE CASCADE,
            payload_type TEXT NOT NULL,
            file_id TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            UNIQUE(schedule_id,file_id)
        );
        CREATE TABLE IF NOT EXISTS biology_observed_members(
            user_id BIGINT PRIMARY KEY,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            activation_deadline TIMESTAMPTZ NOT NULL,
            removed BOOLEAN NOT NULL DEFAULT FALSE,
            removed_at TIMESTAMPTZ
        );
        CREATE TABLE IF NOT EXISTS biology_student_topics(
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            thread_id BIGINT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,chat_id)
        );
        CREATE TABLE IF NOT EXISTS biology_communication_routes(
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            student_id BIGINT NOT NULL,
            reply_group_id BIGINT NOT NULL,
            reply_thread_id BIGINT,
            role TEXT NOT NULL DEFAULT 'student',
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(chat_id,message_id)
        );
        CREATE TABLE IF NOT EXISTS biology_backlog(
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
            lecture INTEGER NOT NULL,
            planned_date DATE,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            added_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,chapter,lecture)
        );
        CREATE TABLE IF NOT EXISTS biology_task_extensions(
            task_id INTEGER REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            extended_until TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(task_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS biology_xp_log(
            id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            delta INTEGER NOT NULL, reason TEXT NOT NULL, event_key TEXT UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_exam_access(
            task_id INTEGER REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied')),
            approved_by BIGINT, requested_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, approved_at TIMESTAMPTZ,
            PRIMARY KEY(task_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS biology_leave_requests(
            id SERIAL PRIMARY KEY, user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            leave_date DATE NOT NULL, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied')),
            parent_decided_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id,leave_date)
        );
        CREATE TABLE IF NOT EXISTS biology_extension_requests(
            id SERIAL PRIMARY KEY, task_id INTEGER REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            hours INTEGER NOT NULL CHECK(hours BETWEEN 1 AND 24),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied')),
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, decided_at TIMESTAMPTZ,
            UNIQUE(task_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS biology_audit(
            id BIGSERIAL PRIMARY KEY, actor_id BIGINT NOT NULL, action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_parent_links(
            student_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE,
            parent_chat_id BIGINT NOT NULL, parent_username TEXT, parent_full_name TEXT,
            approved BOOLEAN NOT NULL DEFAULT FALSE, notify_student BOOLEAN NOT NULL DEFAULT TRUE,
            linked_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(student_id,parent_chat_id)
        );
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS media_group_id TEXT;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS six_hour_reminder_sent BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS champion_announced BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS teacher_deadline_reminder_sent BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_preparations ADD COLUMN IF NOT EXISTS chapter INTEGER NOT NULL DEFAULT 3;
        ALTER TABLE biology_preparations ADD COLUMN IF NOT EXISTS chapter_prep_no INTEGER;
        ALTER TABLE biology_students ALTER COLUMN approved SET DEFAULT FALSE;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS parent_chat_id BIGINT;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS parent_username TEXT;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS parent_full_name TEXT;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS parent_approved BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS parent_link_code TEXT;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS onboarding_version INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS study_track TEXT;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS start_chapter INTEGER;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS start_prep_no INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS current_chapter INTEGER;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS track_started_on DATE;
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS schedule_mode TEXT NOT NULL DEFAULT 'regular';
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS study_days INTEGER[] NOT NULL DEFAULT ARRAY[1,3,5,6];
        ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS schedule_change_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE biology_personal_preparations ADD COLUMN IF NOT EXISTS prep_no INTEGER;
        ALTER TABLE biology_scheduled_tasks ADD COLUMN IF NOT EXISTS linked_chapter INTEGER;
        ALTER TABLE biology_scheduled_tasks ADD COLUMN IF NOT EXISTS linked_prep_no INTEGER;
        ALTER TABLE biology_scheduled_tasks ADD COLUMN IF NOT EXISTS linked_student_id BIGINT;
        ALTER TABLE biology_scheduled_tasks ADD COLUMN IF NOT EXISTS linked_definition_id INTEGER;
        CREATE TABLE IF NOT EXISTS biology_linked_exam_definitions(
            id SERIAL PRIMARY KEY,
            chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
            prep_no INTEGER,
            title TEXT NOT NULL,
            created_by BIGINT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_linked_exam_media(
            id SERIAL PRIMARY KEY,
            definition_id INTEGER REFERENCES biology_linked_exam_definitions(id) ON DELETE CASCADE,
            payload_type TEXT NOT NULL,
            file_id TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            UNIQUE(definition_id,file_id)
        );
        CREATE TABLE IF NOT EXISTS biology_linked_exam_preparations(
            definition_id INTEGER REFERENCES biology_linked_exam_definitions(id) ON DELETE CASCADE,
            chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
            prep_no INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(definition_id,chapter,prep_no),
            UNIQUE(definition_id,position)
        );
        ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS target_scope TEXT NOT NULL DEFAULT 'chapter';
        ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS exam_type TEXT NOT NULL DEFAULT 'normal';
        ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS duration_hours INTEGER NOT NULL DEFAULT 2;
        ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
        UPDATE biology_linked_exam_definitions SET exam_type='cumulative' WHERE title LIKE '[تراكمي]%%';
        CREATE TABLE IF NOT EXISTS biology_linked_exam_lectures(
            definition_id INTEGER REFERENCES biology_linked_exam_definitions(id) ON DELETE CASCADE,
            chapter INTEGER NOT NULL, lecture INTEGER NOT NULL, position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(definition_id,chapter,lecture), UNIQUE(definition_id,position)
        );
        UPDATE biology_linked_exam_definitions SET target_scope='chapter' WHERE target_scope IS NULL;
        INSERT INTO biology_linked_exam_preparations(definition_id,chapter,prep_no,position)
        SELECT id,chapter,prep_no,0 FROM biology_linked_exam_definitions
        WHERE prep_no IS NOT NULL
        ON CONFLICT DO NOTHING;
        ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS prep_no INTEGER;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS target_scope TEXT NOT NULL DEFAULT 'course';
        ALTER TABLE biology_pending_tasks ADD COLUMN IF NOT EXISTS target_scope TEXT NOT NULL DEFAULT 'course';
        ALTER TABLE biology_scheduled_tasks ADD COLUMN IF NOT EXISTS target_scope TEXT NOT NULL DEFAULT 'course';
        ALTER TABLE biology_students ALTER COLUMN parent_link_code
        SET DEFAULT UPPER(SUBSTRING(MD5(RANDOM()::text || CLOCK_TIMESTAMP()::text),1,8));
        ALTER TABLE biology_submissions ADD COLUMN IF NOT EXISTS grade INTEGER;
        ALTER TABLE biology_submissions ADD COLUMN IF NOT EXISTS graded_by BIGINT;
        ALTER TABLE biology_submissions ADD COLUMN IF NOT EXISTS graded_at TIMESTAMPTZ;
        ALTER TABLE biology_submissions ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE biology_lecture_progress ADD COLUMN IF NOT EXISTS completion_method TEXT NOT NULL DEFAULT 'bot_lecture';
        ALTER TABLE biology_resources DROP CONSTRAINT IF EXISTS biology_resources_category_check;
        ALTER TABLE biology_resources ADD CONSTRAINT biology_resources_category_check
        CHECK(category IN ('booklet','summary','model_answer','ministerial'));
        UPDATE biology_students SET parent_link_code=UPPER(SUBSTRING(MD5(user_id::text || RANDOM()::text),1,8))
        WHERE parent_link_code IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS biology_students_parent_code_idx ON biology_students(parent_link_code);
        INSERT INTO biology_parent_links(student_id,parent_chat_id,parent_username,parent_full_name,approved)
        SELECT user_id,parent_chat_id,parent_username,parent_full_name,parent_approved FROM biology_students
        WHERE parent_chat_id IS NOT NULL ON CONFLICT(student_id,parent_chat_id) DO NOTHING;
        UPDATE biology_parent_links SET approved=TRUE;
        UPDATE biology_students SET parent_approved=TRUE WHERE parent_chat_id IS NOT NULL;
        DELETE FROM biology_parent_links WHERE student_id=parent_chat_id;
        UPDATE biology_students SET parent_chat_id=NULL,parent_username=NULL,parent_full_name=NULL,parent_approved=FALSE
        WHERE parent_chat_id=user_id;
        ALTER TABLE biology_exam_archive ADD COLUMN IF NOT EXISTS lecture INTEGER;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS questions_released BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_scheduled_tasks ADD COLUMN IF NOT EXISTS parent_reminder_sent BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_backlog ADD COLUMN IF NOT EXISTS target_completion DATE;
        UPDATE biology_tasks SET questions_released=FALSE WHERE kind='exam' AND closed=FALSE;
        DELETE FROM biology_scheduled_tasks WHERE kind='exam' AND linked_definition_id IS NOT NULL AND published=FALSE;
        INSERT INTO biology_task_students(task_id,user_id)
        SELECT t.id,s.user_id FROM biology_tasks t
        JOIN biology_students s ON s.approved=TRUE AND s.registered_at<=t.created_at
        WHERE NOT EXISTS (SELECT 1 FROM biology_settings WHERE key='task_roster_migrated_v17')
        ON CONFLICT DO NOTHING;
        INSERT INTO biology_settings(key,value) VALUES('task_roster_migrated_v17','done')
        ON CONFLICT(key) DO NOTHING;
        CREATE INDEX IF NOT EXISTS biology_tasks_deadline_open_idx ON biology_tasks(closed,deadline);
        CREATE INDEX IF NOT EXISTS biology_extensions_deadline_idx ON biology_task_extensions(task_id,extended_until);
        CREATE INDEX IF NOT EXISTS biology_warning_task_student_idx ON biology_warning_log(task_id,user_id);
        CREATE INDEX IF NOT EXISTS biology_submission_task_student_idx ON biology_submissions(task_id,user_id);
        CREATE INDEX IF NOT EXISTS biology_personal_prep_due_idx ON biology_personal_preparations(target_date,notified);
        """)
        # v28 Academic Engine schema
        cur.execute("""
        ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS availability_mode TEXT NOT NULL DEFAULT 'completion_approval';
        ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS release_hour INTEGER NOT NULL DEFAULT 18;
        ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS release_next_day BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS exam_available_at TIMESTAMPTZ;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS exam_approval_required BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS exam_extension_hours INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
        CREATE TABLE IF NOT EXISTS biology_notifications(
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, actor_id BIGINT, kind TEXT NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal',
            entity_type TEXT, entity_id BIGINT, dedupe_key TEXT UNIQUE, read_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS biology_notifications_user_idx ON biology_notifications(user_id,read_at,created_at DESC);
        CREATE TABLE IF NOT EXISTS biology_gamification_daily(
            user_id BIGINT PRIMARY KEY REFERENCES biology_students(user_id) ON DELETE CASCADE,
            current_streak INTEGER NOT NULL DEFAULT 0, best_streak INTEGER NOT NULL DEFAULT 0,
            last_activity_date DATE, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_achievements_v28(
            user_id BIGINT REFERENCES biology_students(user_id) ON DELETE CASCADE, code TEXT NOT NULL,
            title TEXT NOT NULL, earned_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,code)
        );
        CREATE TABLE IF NOT EXISTS biology_exam_notices(
            id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            target_scope TEXT NOT NULL, exam_at TIMESTAMPTZ NOT NULL, created_by BIGINT NOT NULL,
            sent BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS biology_weekly_reports(
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            week_start DATE NOT NULL, week_end DATE NOT NULL, summary TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id,week_start,week_end)
        );
        """)
        conn.commit()


async def register_student(user_id, username, full_name, school, target_grade):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_students(user_id,username,full_name,school,target_grade,approved)
            VALUES(%s,%s,%s,%s,%s,FALSE) ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,
            full_name=EXCLUDED.full_name,school=EXCLUDED.school,target_grade=EXCLUDED.target_grade,
            last_seen=CURRENT_TIMESTAMP RETURNING *;""",
            (user_id, username, full_name, school, target_grade)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def set_student_approval(user_id, approved):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_students SET approved=%s,last_seen=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;",(approved,user_id)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def delete_student(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM biology_students WHERE user_id=%s;",(user_id,)); conn.commit()
    await run(op)


async def get_student(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s;",(user_id,)); return cur.fetchone()
    return await run(op)


async def all_preparations():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_preparations ORDER BY target_date,prep_no;")
            return cur.fetchall()
    return await run(op)


async def student_finish_date(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track,current_chapter,schedule_mode FROM biology_students WHERE user_id=%s;",(user_id,)); s=cur.fetchone()
            if not s: return None
            if s["study_track"]=="course" and s.get("schedule_mode")!="custom":
                cur.execute("SELECT MAX(target_date) finish_date FROM biology_preparations;")
            else:
                cur.execute("SELECT MAX(target_date) finish_date FROM biology_personal_preparations WHERE user_id=%s;",(user_id,))
            r=cur.fetchone(); return r["finish_date"] if r else None
    return await run(op)


async def leave_month_usage(user_id,leave_date):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) n FROM biology_leave_requests WHERE user_id=%s
                AND leave_date >= date_trunc('month',%s::date)::date
                AND leave_date < (date_trunc('month',%s::date)+INTERVAL '1 month')::date
                AND status IN ('pending','approved');""",(user_id,leave_date,leave_date)); return cur.fetchone()["n"]
    return await run(op)


async def shift_personal_schedule_for_leave(user_id,leave_date):
    """Shift the leave-day personal slot and every later unnotified slot by one study slot."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_days FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); student=cur.fetchone()
            days=student.get("study_days") if student else None
            cur.execute("SELECT id,target_date FROM biology_personal_preparations WHERE user_id=%s AND target_date=%s AND notified=FALSE FOR UPDATE;",(user_id,leave_date)); first=cur.fetchone()
            if not first: conn.commit(); return {"shifted":False,"next_date":None}
            cur.execute("SELECT id,target_date FROM biology_personal_preparations WHERE user_id=%s AND target_date>%s AND notified=FALSE ORDER BY target_date,id FOR UPDATE;",(user_id,leave_date)); later=cur.fetchall()
            ordered=[first]+later
            if not later: conn.commit(); return {"shifted":False,"next_date":None}
            original=[r["target_date"] for r in later]
            original.append(_next_study_date(original[-1],days))
            cur.execute("UPDATE biology_personal_preparations SET target_date=target_date+10000 WHERE user_id=%s AND target_date>=%s AND notified=FALSE;",(user_id,leave_date))
            for i,r in enumerate(ordered):
                cur.execute("UPDATE biology_personal_preparations SET target_date=%s WHERE id=%s;",(original[i],r["id"]))
            conn.commit(); return {"shifted":True,"next_date":original[0]}
    return await run(op)


async def set_student_onboarding(user_id,study_track,current_chapter,start_date,plan_rows):
    """Atomically save the selected track and replace its personal study plan."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE biology_students SET onboarding_version=19,study_track=%s,
                current_chapter=%s,track_started_on=%s,last_seen=CURRENT_TIMESTAMP
                WHERE user_id=%s RETURNING *;""",(study_track,current_chapter,start_date,user_id)); student=cur.fetchone()
            # Switching tracks must preserve historical preparation/task data.
            # Only future personal preparations and future student-specific schedules
            # are removed; the current course receives all currently-open course tasks.
            cur.execute("DELETE FROM biology_personal_preparations WHERE user_id=%s AND target_date >= %s;",(user_id,start_date))
            cur.execute("DELETE FROM biology_scheduled_tasks WHERE published=FALSE AND linked_student_id=%s;",(user_id,))
            if study_track=="course":
                cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
                    SELECT t.id,%s FROM biology_tasks t
                    WHERE t.closed=FALSE AND t.deadline>CURRENT_TIMESTAMP
                      AND t.target_scope IN ('course','all')
                    ON CONFLICT DO NOTHING;""",(user_id,))
            else:
                for row in plan_rows:
                    target_date,chapter,lectures,prep_no = row if len(row)==4 else (*row,None)
                    cur.execute("""INSERT INTO biology_personal_preparations(user_id,target_date,chapter,lectures,prep_no)
                        VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,target_date,chapter,prep_no) DO UPDATE SET
                        chapter=EXCLUDED.chapter,lectures=EXCLUDED.lectures,prep_no=EXCLUDED.prep_no,notified=FALSE,notified_at=NULL;""",
                        (user_id,target_date,chapter,lectures,prep_no))
            conn.commit(); return student
    return await run(op)



async def student_schedule(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT schedule_mode,study_days,schedule_change_count FROM biology_students WHERE user_id=%s;",(user_id,)); return cur.fetchone()
    return await run(op)

async def set_student_schedule(user_id, days, mode="custom"):
    days=sorted({int(x) for x in days})
    if len(days)<1 or len(days)>7 or any(x<0 or x>6 for x in days):
        return {"status":"days"}
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT schedule_change_count FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); row=cur.fetchone()
            if not row: return {"status":"missing"}
            count=int(row["schedule_change_count"] or 0)
            if count>=3: return {"status":"limit","count":count}
            cur.execute("UPDATE biology_students SET schedule_mode=%s,study_days=%s,schedule_change_count=schedule_change_count+1 WHERE user_id=%s RETURNING *;",(mode,days,user_id)); student=cur.fetchone()
            # Rebuild only future personal slots, preserving all historical rows.
            cur.execute("SELECT current_chapter FROM biology_students WHERE user_id=%s;",(user_id,)); st=cur.fetchone()
            cur.execute("SELECT id,target_date,chapter,lectures,prep_no FROM biology_personal_preparations WHERE user_id=%s AND target_date>=CURRENT_DATE ORDER BY target_date,id;",(user_id,)); future=cur.fetchall()
            if future:
                cursor=future[0]["target_date"]-timedelta(days=1)
                for r in future:
                    while True:
                        cursor += timedelta(days=1)
                        if cursor.weekday() in days: break
                    cur.execute("UPDATE biology_personal_preparations SET target_date=%s WHERE id=%s;",(cursor,r["id"]))
            elif student and student.get("study_track")=="course":
                cur.execute("SELECT chapter,chapter_prep_no,lectures,target_date FROM biology_preparations WHERE target_date>=CURRENT_DATE ORDER BY target_date,prep_no;")
                source=cur.fetchall(); cursor=date_today=None
                # Avoid duplicate dates by placing each future course preparation on selected weekdays.
                from datetime import date
                cursor=date.today()-timedelta(days=1)
                for r in source:
                    while True:
                        cursor += timedelta(days=1)
                        if cursor.weekday() in days: break
                    cur.execute("INSERT INTO biology_personal_preparations(user_id,target_date,chapter,lectures,prep_no) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,target_date,chapter,prep_no) DO NOTHING;",(user_id,cursor,r["chapter"],r["lectures"],r["chapter_prep_no"]))
            conn.commit(); return {"status":"ok","count":count+1,"student":student}
    return await run(op)

async def reset_personal_schedule_to_regular(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT schedule_change_count FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); r=cur.fetchone()
            if not r or int(r["schedule_change_count"] or 0)>=3: return None
            cur.execute("UPDATE biology_students SET schedule_mode='regular',schedule_change_count=schedule_change_count+1 WHERE user_id=%s RETURNING *;",(user_id,)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)

async def linked_exam_definition_status(definition_id,user_id):
    """Single eligibility engine. A definition can combine preparation links and individual lectures."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s AND approved=TRUE AND onboarding_version>=19;",(user_id,)); st=cur.fetchone()
            if not st: return None
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE id=%s;",(definition_id,)); definition=cur.fetchone()
            if not definition: return None
            scope_ok=(definition['target_scope']=='course' and st.get('study_track')=='course') or (definition['target_scope']=='chapter' and st.get('study_track')=='chapter' and st.get('current_chapter')==definition['chapter'])
            if not scope_ok: return None
            cur.execute("SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s ORDER BY position,chapter,lecture;",(definition_id,)); explicit=cur.fetchall()
            cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position;",(definition_id,)); pairs=cur.fetchall()
            lectures=list(explicit); seen={(x["chapter"],x["lecture"]) for x in explicit}
            for pair in pairs:
                cur.execute("SELECT lectures FROM biology_personal_preparations WHERE user_id=%s AND chapter=%s AND prep_no=%s ORDER BY target_date DESC LIMIT 1;",(user_id,pair["chapter"],pair["prep_no"]))
                x=cur.fetchone(); vals=x["lectures"].split(',') if x else []
                if not vals:
                    cur.execute("SELECT lectures FROM biology_preparations WHERE chapter=%s AND chapter_prep_no=%s ORDER BY prep_no DESC LIMIT 1;",(pair["chapter"],pair["prep_no"]))
                    x=cur.fetchone(); vals=x["lectures"].split(',') if x else []
                for v in vals:
                    key=(pair["chapter"],int(v))
                    if key not in seen: lectures.append({"chapter":key[0],"lecture":key[1]}); seen.add(key)
            missing=[]
            for r in lectures:
                cur.execute("SELECT 1 FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL;",(user_id,r["chapter"],r["lecture"]))
                if not cur.fetchone(): missing.append(r)
            return {"ready":not missing,"missing":missing,"lectures":lectures,"student":st,"definition":definition}
    return await run(op)



async def task_for_linked_exam_student(definition_id,user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT t.* FROM biology_tasks t WHERE t.kind='exam' AND t.exam_definition_id=%s AND t.target_scope=%s ORDER BY t.id DESC LIMIT 1;",(definition_id,f"student:{user_id}")); task=cur.fetchone()
            if task: return task
            cur.execute("SELECT id FROM biology_scheduled_tasks WHERE kind='exam' AND linked_definition_id=%s AND linked_student_id=%s AND published=FALSE LIMIT 1;",(definition_id,user_id))
            return {"legacy_scheduled":True} if cur.fetchone() else None
    return await run(op)

async def create_linked_exam_task_for_student(definition_id,user_id,title,media,linked_lectures,created_by,duration_hours=2):
    def op():
        with connect() as conn, conn.cursor() as cur:
            # Long pending deadline; activation resets it to the real exam window.
            deadline=datetime_now(cur)+timedelta(days=3650)
            ptype,fid=media[0]
            synthetic=-(900000000000000000+(int(definition_id)*1000000000000+int(user_id))%100000000000000000)
            cur.execute("""INSERT INTO biology_tasks(kind,title,chat_id,thread_id,source_message_id,payload_type,file_id,text_content,deadline,xp_reward,created_by,target_scope,linked_lectures,exam_pending_activation,exam_definition_id,exam_duration_hours)
            VALUES('exam',%s,%s,0,%s,%s,%s,%s,%s,20,%s,%s,%s,TRUE,%s,%s)
            ON CONFLICT (exam_definition_id,target_scope) WHERE exam_definition_id IS NOT NULL AND target_scope LIKE 'student:%' DO NOTHING RETURNING *;""",(title,OWNER_CHAT_ID or created_by,synthetic,ptype,fid,title,deadline,created_by,f"student:{user_id}",linked_lectures,definition_id,max(1,int(duration_hours or 2))))
            row=cur.fetchone()
            if not row:
                cur.execute("SELECT * FROM biology_tasks WHERE exam_definition_id=%s AND target_scope=%s ORDER BY id DESC LIMIT 1;",(definition_id,f"student:{user_id}")); row=cur.fetchone()
            for pos,(t,f) in enumerate(media): cur.execute("INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;",(row["id"],t,f,synthetic-pos))
            cur.execute("INSERT INTO biology_task_students(task_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING;",(row["id"],user_id))
            conn.commit(); return row
    return await run(op)

async def activate_exam(task_id,user_id,approved_by,hours=None):
    """Only activate an assigned, pending legacy exam once; never reset an active deadline."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT user_id FROM biology_students WHERE user_id=%s AND approved=TRUE AND reset_pending=FALSE FOR UPDATE;",(user_id,))
            if not cur.fetchone(): return None
            cur.execute("""SELECT t.* FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.id=%s AND t.kind='exam' AND t.exam_pending_activation=TRUE
                AND t.exam_approval_required=TRUE AND (d.id IS NULL OR d.deleted_at IS NULL)
                AND NOT EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL)
                FOR UPDATE OF t;""",(user_id,task_id,user_id))
            current=cur.fetchone()
            if not current: return None
            effective_hours=max(1,int(hours or current.get('exam_duration_hours') or 24))
            now=datetime_now(cur);deadline=now+timedelta(hours=effective_hours)
            cur.execute("""UPDATE biology_tasks SET exam_pending_activation=FALSE,deadline=%s,closed=FALSE,
                exam_available_at=%s,published_at=COALESCE(published_at,%s)
                WHERE id=%s RETURNING *;""",(deadline,now,now,task_id));task=cur.fetchone()
            cur.execute("UPDATE biology_exam_access SET status='approved',approved_by=%s,approved_at=CURRENT_TIMESTAMP WHERE task_id=%s AND user_id=%s;",(approved_by,task_id,user_id))
            conn.commit();return task
    return await run(op)

async def unlock_next_preparation_after_exam(user_id,task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT linked_definition_id,linked_lectures FROM biology_tasks WHERE id=%s;",(task_id,)); task=cur.fetchone()
            if not task: return None
            cur.execute("SELECT target_date,prep_no FROM biology_personal_preparations WHERE user_id=%s AND target_date>CURRENT_DATE ORDER BY target_date,prep_no LIMIT 1;",(user_id,)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)

async def preparation_catalog(chapter):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_preparations WHERE chapter=%s ORDER BY chapter_prep_no,prep_no;",(chapter,))
            return cur.fetchall()
    return await run(op)


async def personal_preparations_for_chapter(chapter):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT pp.*,s.full_name,s.user_id FROM biology_personal_preparations pp
                JOIN biology_students s ON s.user_id=pp.user_id
                WHERE pp.chapter=%s AND s.approved=TRUE ORDER BY pp.target_date,pp.prep_no,pp.user_id;""",(chapter,))
            return cur.fetchall()
    return await run(op)


async def add_linked_exam_lectures(definition_id, lectures):
    pairs=sorted({(int(c),int(l)) for c,l in lectures})
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL FOR UPDATE;",(definition_id,))
            if not cur.fetchone(): return 0
            cur.execute("SELECT COALESCE(MAX(position),-1)+1 AS next_position FROM biology_linked_exam_lectures WHERE definition_id=%s;",(definition_id,))
            position=int(cur.fetchone()['next_position']);added=0
            for ch,lec in pairs:
                cur.execute("""INSERT INTO biology_linked_exam_lectures(definition_id,chapter,lecture,position)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(definition_id,chapter,lecture) DO NOTHING;""",(definition_id,ch,lec,position))
                if cur.rowcount: added+=1;position+=1
            if added:
                cur.execute("DELETE FROM biology_exam_release_preparations WHERE definition_id=%s;",(definition_id,))
                cur.execute("""UPDATE biology_linked_exam_definitions
                    SET release_links_ready=FALSE,actual_publish_at=NULL,actual_deadline=NULL
                    WHERE id=%s;""",(definition_id,))
            conn.commit();return added
    return await run(op)


async def create_linked_exam_definition(selected_pairs,title,created_by,media,target_scope="chapter",selected_lectures=None,exam_type="normal",duration_hours=2):
    pairs=sorted({(int(c),int(p)) for c,p in (selected_pairs or [])})
    lectures=sorted({(int(c),int(l)) for c,l in (selected_lectures or [])})
    if not pairs and not lectures: raise ValueError("At least one preparation or lecture is required")
    chapter=(pairs[0][0] if pairs else lectures[0][0])
    def op():
        with connect() as conn, conn.cursor() as cur:
            clean_type='cumulative' if exam_type=='cumulative' else 'normal'
            clean_title=(title.strip() if isinstance(title,str) else str(title)).strip()
            if clean_type=='cumulative' and not clean_title.startswith('[تراكمي]'):
                clean_title='[تراكمي] '+clean_title
            clean_hours=max(1,min(168,int(duration_hours or 2)))
            cur.execute("INSERT INTO biology_linked_exam_definitions(chapter,prep_no,title,created_by,target_scope,exam_type,duration_hours,availability_mode,release_hour,release_next_day) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,18,%s) RETURNING *;",(chapter,pairs[0][1] if pairs else None,clean_title,created_by,target_scope,clean_type,clean_hours,"course_next_day" if target_scope=="course" else "completion_approval",target_scope=="course"))
            row=cur.fetchone()
            for pos,(ch,pno) in enumerate(pairs):
                cur.execute("INSERT INTO biology_linked_exam_preparations(definition_id,chapter,prep_no,position) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;",(row["id"],ch,pno,pos))
            offset=len(pairs)
            for pos,(ch,lec) in enumerate(lectures):
                cur.execute("INSERT INTO biology_linked_exam_lectures(definition_id,chapter,lecture,position) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;",(row["id"],ch,lec,offset+pos))
            for pos,(ptype,fid) in enumerate(media):
                cur.execute("INSERT INTO biology_linked_exam_media(definition_id,payload_type,file_id,position) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;",(row["id"],ptype,fid,pos))
            conn.commit(); return row
    return await run(op)


async def linked_exam_lectures_text(definition_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s ORDER BY position,chapter,lecture;",(definition_id,)); explicit=cur.fetchall()
            cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position;",(definition_id,)); preps=cur.fetchall()
            return explicit + preps
    return await run(op)


async def linked_exam_media(definition_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_media WHERE definition_id=%s ORDER BY position,id;",(definition_id,)); return cur.fetchall()
    return await run(op)


async def linked_exam_preparations(definition_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position;",(definition_id,))
            return cur.fetchall()
    return await run(op)


async def linked_exam_definitions():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions ORDER BY id;")
            return cur.fetchall()
    return await run(op)


async def update_linked_exam_definition(definition_id,title=None,exam_type=None,duration_hours=None):
    """Update metadata and sync only unsubmitted student exam snapshots."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE id=%s FOR UPDATE;",(definition_id,)); d=cur.fetchone()
            if not d: return None
            new_title=(title.strip() if isinstance(title,str) and title.strip() else d['title'])
            new_type=('cumulative' if exam_type=='cumulative' else 'normal') if exam_type is not None else d.get('exam_type','normal')
            new_hours=max(1,min(168,int(duration_hours))) if duration_hours is not None else int(d.get('duration_hours') or 2)
            cur.execute("UPDATE biology_linked_exam_definitions SET title=%s,exam_type=%s,duration_hours=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING *;",(new_title,new_type,new_hours,definition_id)); updated=cur.fetchone()
            cur.execute("""UPDATE biology_tasks t SET title=%s,exam_duration_hours=%s
                WHERE t.exam_definition_id=%s AND t.kind='exam'
                AND NOT EXISTS(SELECT 1 FROM biology_submissions s WHERE s.task_id=t.id AND s.submitted_at IS NOT NULL);""",(new_title,new_hours,definition_id))
            conn.commit(); return updated
    return await run(op)


async def active_students_for_linked_exam(definition_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.* FROM biology_students s JOIN biology_linked_exam_definitions d ON d.id=%s
            WHERE s.approved=TRUE AND s.onboarding_version>=19 AND
              ((d.target_scope='course' AND s.study_track='course') OR (d.target_scope='chapter' AND s.study_track='chapter' AND s.current_chapter=d.chapter))
            ORDER BY s.user_id;""",(definition_id,)); return cur.fetchall()
    return await run(op)


async def backfill_personal_prep_numbers(distribution):
    def op():
        with connect() as conn, conn.cursor() as cur:
            for chapter,groups in distribution.items():
                for prep_no,nums in enumerate(groups,1):
                    lectures=",".join(map(str,nums))
                    cur.execute("""UPDATE biology_personal_preparations
                        SET prep_no=%s
                        WHERE chapter=%s AND lectures=%s AND prep_no IS NULL;""",(prep_no,chapter,lectures))
            conn.commit()
    return await run(op)


async def personal_preparations_for_student_prep(user_id,chapter,prep_no):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_personal_preparations WHERE user_id=%s AND chapter=%s AND prep_no=%s ORDER BY target_date DESC LIMIT 1;",(user_id,chapter,prep_no)); return cur.fetchone()
    return await run(op)


async def personal_preparation_for_student(user_id,target_date):
    # Personal tracks are sequential: always resume the first incomplete block.
    # This also recovers correctly after an exam lock, leave, or Render restart.
    return await v37_current_personal_preparation(user_id)


async def due_personal_preparation_notifications(now):
    """Notify only the earliest incomplete block, never an entire overdue backlog."""
    def op():
        from zoneinfo import ZoneInfo
        local_now=now.astimezone(ZoneInfo(os.getenv('TIMEZONE','Asia/Baghdad'))).replace(tzinfo=None) if getattr(now,'tzinfo',None) else now
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT pp.*,s.full_name,s.parent_chat_id FROM biology_personal_preparations pp
                JOIN biology_students s ON s.user_id=pp.user_id
                WHERE s.study_track='chapter' AND s.approved=TRUE AND s.reset_pending=FALSE AND s.onboarding_version>=19
                AND pp.notified=FALSE AND ((pp.target_date-1)+TIME '23:00')<=%s
                AND EXISTS(SELECT 1 FROM unnest(string_to_array(pp.lectures,',')::integer[]) AS l(lecture)
                    WHERE NOT EXISTS(SELECT 1 FROM biology_lecture_progress p WHERE p.user_id=pp.user_id
                        AND p.chapter=pp.chapter AND p.lecture=l.lecture AND p.completed_at IS NOT NULL))
                AND NOT EXISTS(SELECT 1 FROM biology_personal_preparations earlier
                    WHERE earlier.user_id=pp.user_id AND (earlier.target_date,earlier.id)<(pp.target_date,pp.id)
                    AND EXISTS(SELECT 1 FROM unnest(string_to_array(earlier.lectures,',')::integer[]) AS l(lecture)
                        WHERE NOT EXISTS(SELECT 1 FROM biology_lecture_progress p WHERE p.user_id=earlier.user_id
                            AND p.chapter=earlier.chapter AND p.lecture=l.lecture AND p.completed_at IS NOT NULL)))
                ORDER BY pp.target_date,pp.id;""",(local_now,));return cur.fetchall()
    return await run(op)


async def mark_personal_preparation_notified(prep_id,user_id,chapter):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_personal_preparations SET notified=TRUE,notified_at=CURRENT_TIMESTAMP WHERE id=%s AND notified=FALSE;",(prep_id,))
            changed=cur.rowcount
            if changed: cur.execute("UPDATE biology_students SET current_chapter=%s WHERE user_id=%s;",(chapter,user_id))
            conn.commit(); return changed==1
    return await run(op)


async def students_requiring_onboarding(version=19):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE approved=TRUE AND onboarding_version<%s ORDER BY user_id;",(version,)); return cur.fetchall()
    return await run(op)


async def assigned_students(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.* FROM biology_task_students ts JOIN biology_students s ON s.user_id=ts.user_id
                WHERE ts.task_id=%s AND s.approved=TRUE ORDER BY s.user_id;""",(task_id,)); return cur.fetchall()
    return await run(op)


async def students_for_scope(scope):
    def op():
        with connect() as conn, conn.cursor() as cur:
            if scope and scope.startswith("student:"):
                try:
                    only_id=int(scope.split(":",1)[1])
                except (TypeError,ValueError):
                    return []
                cur.execute("""SELECT * FROM biology_students
                    WHERE user_id=%s AND approved=TRUE AND onboarding_version>=19
                    ORDER BY user_id;""",(only_id,))
                return cur.fetchall()
            cur.execute("""SELECT * FROM biology_students WHERE approved=TRUE AND onboarding_version>=19 AND
                (%s='all' OR (%s='course' AND study_track='course') OR
                 (%s LIKE 'chapter_%%' AND study_track='chapter' AND current_chapter=SUBSTRING(%s FROM 9)::INTEGER))
                ORDER BY user_id;""",(scope,scope,scope,scope)); return cur.fetchall()
    return await run(op)


async def get_student_by_parent_code(code):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE UPPER(parent_link_code)=UPPER(%s);",(code,)); return cur.fetchone()
    return await run(op)


async def seed_preparations(rows):
    def op():
        with connect() as conn, conn.cursor() as cur:
            # Remove only the preparations preceding the official course start.
            # Their lectures remain available normally in the chapter library.
            cur.execute("""DELETE FROM biology_preparations
                WHERE chapter<3 OR (chapter=3 AND chapter_prep_no<11);""")
            cur.execute("""DELETE FROM biology_early_preparation_unlocks
                WHERE chapter<3 OR (chapter=3 AND EXISTS(
                    SELECT 1 FROM UNNEST(STRING_TO_ARRAY(lectures,',')) value
                    WHERE value::INTEGER<11));""")
            for prep_no,target_date,lectures,chapter,chapter_prep_no in rows:
                cur.execute("""INSERT INTO biology_preparations(prep_no,target_date,lectures,chapter,chapter_prep_no)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(prep_no) DO UPDATE SET
                target_date=EXCLUDED.target_date,lectures=EXCLUDED.lectures,
                chapter=EXCLUDED.chapter,chapter_prep_no=EXCLUDED.chapter_prep_no
                WHERE biology_preparations.published=FALSE;""",
                (prep_no,target_date,lectures,chapter,chapter_prep_no))
            conn.commit()
    await run(op)


async def due_preparations(now):
    def op():
        local_now = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_preparations WHERE published=FALSE
            AND ((target_date - 1) + TIME '23:00') <= %s ORDER BY prep_no;""",(local_now,)); return cur.fetchall()
    return await run(op)


async def mark_preparation_published(prep_no):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_preparations SET published=TRUE,published_at=CURRENT_TIMESTAMP WHERE prep_no=%s;",(prep_no,)); conn.commit()
    await run(op)


async def preparation_for_date(target_date):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_preparations WHERE target_date=%s;",(target_date,)); return cur.fetchone()
    return await run(op)


async def latest_preparation():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_preparations WHERE published=TRUE ORDER BY target_date DESC LIMIT 1;"); return cur.fetchone()
    return await run(op)


def _next_study_date(value, days=None):
    allowed=set(days or (1,3,5,6))
    value+=timedelta(days=1)
    while value.weekday() not in allowed: value+=timedelta(days=1)
    return value


def _previous_study_date(value):
    value-=timedelta(days=1)
    while value.weekday() not in (1,3,5,6): value-=timedelta(days=1)
    return value


async def next_unpublished_preparation():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_preparations WHERE published=FALSE ORDER BY target_date,prep_no LIMIT 1;"); return cur.fetchone()
    return await run(op)


async def reschedule_unpublished_preparations(first_date):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_preparations WHERE published=FALSE ORDER BY target_date,prep_no FOR UPDATE;"); rows=cur.fetchall()
            if not rows: return None
            cur.execute("UPDATE biology_preparations SET target_date=target_date+10000 WHERE published=FALSE;")
            current=first_date
            for row in rows:
                cur.execute("UPDATE biology_preparations SET target_date=%s WHERE prep_no=%s;",(current,row["prep_no"])); current=_next_study_date(current)
            conn.commit()
            cur.execute("SELECT * FROM biology_preparations WHERE prep_no=%s;",(rows[0]["prep_no"],)); return cur.fetchone()
    return await run(op)


async def shift_unpublished_preparations(direction,earliest_date=None):
    first=await next_unpublished_preparation()
    if not first: return None
    new_date=_next_study_date(first["target_date"]) if direction=="delay" else _previous_study_date(first["target_date"])
    if earliest_date and new_date<=earliest_date: return "too_early"
    return await reschedule_unpublished_preparations(new_date)


async def create_task(kind,title,chat_id,thread_id,message_id,payload_type,file_id,media_group_id,text_content,deadline,xp_reward,created_by,target_scope="course",linked_lectures=""):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_tasks(kind,title,chat_id,thread_id,source_message_id,payload_type,file_id,media_group_id,text_content,deadline,xp_reward,created_by,target_scope,linked_lectures)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(chat_id,source_message_id) DO UPDATE SET title=EXCLUDED.title,deadline=EXCLUDED.deadline,target_scope=EXCLUDED.target_scope
            RETURNING *;""",(kind,title,chat_id,thread_id,message_id,payload_type,file_id,media_group_id,text_content,deadline,xp_reward,created_by,target_scope,linked_lectures)); row=cur.fetchone()
            if file_id: cur.execute("INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;",(row["id"],payload_type,file_id,message_id))
            if target_scope.startswith("student:"):
                only_id=int(target_scope.split(":",1)[1])
                cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
                    SELECT %s,user_id FROM biology_students
                    WHERE user_id=%s AND approved=TRUE AND onboarding_version>=19
                    ON CONFLICT DO NOTHING;""",(row["id"],only_id))
            else:
                cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
                    SELECT %s,user_id FROM biology_students WHERE approved=TRUE AND onboarding_version>=19 AND
                    (%s='all' OR (%s='course' AND study_track='course') OR
                     (%s LIKE 'chapter_%%' AND study_track='chapter' AND current_chapter=SUBSTRING(%s FROM 9)::INTEGER))
                    ON CONFLICT DO NOTHING;""",(row["id"],target_scope,target_scope,target_scope,target_scope))
            conn.commit(); return row
    return await run(op)


async def create_scheduled_task(kind,title,media,publish_at,submission_hours,created_by,target_scope="course"):
    def op():
        with connect() as conn, conn.cursor() as cur:
            payload_type,file_id=media[0]
            cur.execute("""INSERT INTO biology_scheduled_tasks
            (kind,title,payload_type,file_id,publish_at,submission_hours,created_by,target_scope)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *;""",
            (kind,title,payload_type,file_id,publish_at,submission_hours,created_by,target_scope))
            row=cur.fetchone()
            for position,(item_type,item_file_id) in enumerate(media):
                cur.execute("""INSERT INTO biology_scheduled_task_media(schedule_id,payload_type,file_id,position)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;""",(row["id"],item_type,item_file_id,position))
            conn.commit(); return row
    return await run(op)


async def due_scheduled_tasks():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_scheduled_tasks WHERE published=FALSE AND publish_at<=CURRENT_TIMESTAMP ORDER BY publish_at,id;")
            return cur.fetchall()
    return await run(op)


async def pending_scheduled_tasks():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_scheduled_tasks WHERE published=FALSE ORDER BY publish_at,id;")
            return cur.fetchall()
    return await run(op)


async def scheduled_task_media(schedule_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_scheduled_task_media WHERE schedule_id=%s ORDER BY position,id;",(schedule_id,))
            return cur.fetchall()
    return await run(op)


async def cancel_scheduled_task(schedule_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM biology_scheduled_tasks WHERE id=%s AND published=FALSE;",(schedule_id,))
            changed=cur.rowcount; conn.commit(); return changed==1
    return await run(op)


async def mark_scheduled_task_published(schedule_id,message_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_scheduled_tasks SET published=TRUE,published_message_id=%s WHERE id=%s AND published=FALSE;",(message_id,schedule_id))
            changed=cur.rowcount; conn.commit(); return changed==1
    return await run(op)


async def add_pending_media_by_id(pending_id,payload_type,file_id,message_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_pending_task_media(pending_id,payload_type,file_id,source_message_id)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;""",(pending_id,payload_type,file_id,message_id))
            conn.commit()
    await run(op)


async def add_task_media_by_id(task_id,payload_type,file_id,message_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;""",(task_id,payload_type,file_id,message_id))
            conn.commit()
    await run(op)


async def delete_task(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT user_id,COUNT(*) AS n FROM biology_warning_log
            WHERE task_id=%s GROUP BY user_id;""",(task_id,)); warning_counts=cur.fetchall()
            cur.execute("DELETE FROM biology_warning_log WHERE task_id=%s;",(task_id,))
            for item in warning_counts:
                cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-%s) WHERE user_id=%s;",(item["n"],item["user_id"]))
            cur.execute("DELETE FROM biology_tasks WHERE id=%s RETURNING *;",(task_id,))
            row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def observe_group_member(user_id,hours=48):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_observed_members(user_id,activation_deadline)
            VALUES(%s,CURRENT_TIMESTAMP+(%s || ' hours')::INTERVAL)
            ON CONFLICT(user_id) DO NOTHING;""",(user_id,hours)); conn.commit()
    await run(op)


async def observe_known_unactivated_members(hours=48):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_observed_members(user_id,activation_deadline)
            SELECT user_id,CURRENT_TIMESTAMP+(%s || ' hours')::INTERVAL FROM biology_students
            WHERE approved=FALSE OR parent_chat_id IS NULL ON CONFLICT(user_id) DO NOTHING;""",(hours,)); conn.commit()
    await run(op)


async def due_unactivated_members():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT o.*,s.approved,s.parent_chat_id,s.full_name FROM biology_observed_members o
            LEFT JOIN biology_students s ON s.user_id=o.user_id
            WHERE o.removed=FALSE AND o.activation_deadline<=CURRENT_TIMESTAMP
            AND (s.user_id IS NULL OR s.approved=FALSE OR s.parent_chat_id IS NULL);""")
            return cur.fetchall()
    return await run(op)


async def mark_member_compliant(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM biology_observed_members WHERE user_id=%s;",(user_id,)); conn.commit()
    await run(op)


async def mark_member_removed(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_observed_members SET removed=TRUE,removed_at=CURRENT_TIMESTAMP WHERE user_id=%s;",(user_id,)); conn.commit()
    await run(op)


async def student_topic(user_id,chat_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT thread_id FROM biology_student_topics WHERE user_id=%s AND chat_id=%s;",(user_id,chat_id)); row=cur.fetchone()
            return row["thread_id"] if row else None
    return await run(op)


async def save_student_topic(user_id,chat_id,thread_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_student_topics(user_id,chat_id,thread_id) VALUES(%s,%s,%s)
            ON CONFLICT(user_id,chat_id) DO UPDATE SET thread_id=EXCLUDED.thread_id;""",(user_id,chat_id,thread_id)); conn.commit()
    await run(op)


async def student_by_parent(parent_chat_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.* FROM biology_parent_links p JOIN biology_students s ON s.user_id=p.student_id
            WHERE p.parent_chat_id=%s AND p.approved=TRUE ORDER BY p.linked_at LIMIT 1;""",(parent_chat_id,)); return cur.fetchone()
    return await run(op)


async def students_by_parent(parent_chat_id,approved_only=True):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.*,p.parent_chat_id,p.parent_username,p.parent_full_name,p.approved parent_link_approved,p.notify_student
            FROM biology_parent_links p JOIN biology_students s ON s.user_id=p.student_id
            WHERE p.parent_chat_id=%s AND (%s=FALSE OR p.approved=TRUE) ORDER BY s.full_name;""",(parent_chat_id,approved_only)); return cur.fetchall()
    return await run(op)


async def student_parents(student_id,approved_only=True):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_parent_links WHERE student_id=%s AND (%s=FALSE OR approved=TRUE) ORDER BY linked_at;",(student_id,approved_only)); return cur.fetchall()
    return await run(op)


async def save_communication_route(chat_id,message_id,student_id,reply_group_id,reply_thread_id,role='student'):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_communication_routes(chat_id,message_id,student_id,reply_group_id,reply_thread_id,role)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(chat_id,message_id) DO UPDATE SET
            student_id=EXCLUDED.student_id,reply_group_id=EXCLUDED.reply_group_id,reply_thread_id=EXCLUDED.reply_thread_id,role=EXCLUDED.role;""",
            (chat_id,message_id,student_id,reply_group_id,reply_thread_id,role)); conn.commit()
    await run(op)


async def communication_route(chat_id,message_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_communication_routes WHERE chat_id=%s AND message_id=%s;",(chat_id,message_id)); return cur.fetchone()
    return await run(op)


async def unwatched_lectures_for_student(user_id, now=None):
    """Return every lecture already due for this student but not yet completed.
    The list is derived from the student's active track/preparation schedule, so
    completing a lecture by the normal preparation flow or the private-source
    oath automatically removes it from this list.
    """
    from datetime import datetime
    now = now or datetime.now()
    today = now.date()
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track,current_chapter FROM biology_students WHERE user_id=%s AND approved=TRUE;", (user_id,))
            student=cur.fetchone()
            if not student:
                return []
            rows=[]
            if student["study_track"] == "course":
                cur.execute("""SELECT p.chapter,p.prep_no,p.target_date,p.lectures
                    FROM biology_preparations p
                    WHERE p.published=TRUE AND p.target_date<=%s
                    ORDER BY p.target_date,p.chapter_prep_no,p.prep_no;""", (today,))
                preps=cur.fetchall()
            else:
                cur.execute("""SELECT pp.chapter,pp.prep_no,pp.target_date,pp.lectures
                    FROM biology_personal_preparations pp
                    WHERE pp.user_id=%s AND pp.target_date<=%s
                    ORDER BY pp.target_date,pp.chapter,pp.prep_no;""", (user_id,today))
                preps=cur.fetchall()
            seen=set()
            for prep in preps:
                for raw in str(prep["lectures"]).split(","):
                    if not raw.strip():
                        continue
                    lecture=int(raw)
                    key=(int(prep["chapter"]),lecture)
                    if key in seen:
                        continue
                    seen.add(key)
                    cur.execute("""SELECT completed_at FROM biology_lecture_progress
                        WHERE user_id=%s AND chapter=%s AND lecture=%s;""", (user_id,key[0],key[1]))
                    progress=cur.fetchone()
                    if progress and progress["completed_at"]:
                        continue
                    rows.append({"chapter":key[0],"lecture":key[1],"prep_no":prep["prep_no"],"target_date":prep["target_date"]})
            return rows
    return await run(op)


async def toggle_backlog(user_id,chapter,lecture):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM biology_backlog WHERE user_id=%s AND chapter=%s AND lecture=%s;",(user_id,chapter,lecture))
            if cur.fetchone():
                cur.execute("DELETE FROM biology_backlog WHERE user_id=%s AND chapter=%s AND lecture=%s;",(user_id,chapter,lecture)); conn.commit(); return False
            cur.execute("INSERT INTO biology_backlog(user_id,chapter,lecture) VALUES(%s,%s,%s);",(user_id,chapter,lecture)); conn.commit(); return True
    return await run(op)


async def backlog_items(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_backlog WHERE user_id=%s AND completed=FALSE ORDER BY chapter,lecture;",(user_id,)); return cur.fetchall()
    return await run(op)


async def complete_backlog(user_id,chapter,lecture):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_backlog SET completed=TRUE WHERE user_id=%s AND chapter=%s AND lecture=%s RETURNING *;",(user_id,chapter,lecture)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def plan_backlog(user_id,start_date,weekly_count=3,end_date=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_backlog WHERE user_id=%s AND completed=FALSE ORDER BY chapter,lecture FOR UPDATE;",(user_id,)); rows=cur.fetchall()
            offsets=(0,2,4)
            for index,row in enumerate(rows):
                if end_date and len(rows)>1:
                    span=max(0,(end_date-start_date).days); planned=start_date+timedelta(days=round(index*span/(len(rows)-1)))
                else: planned=start_date+timedelta(days=(index//weekly_count)*7+offsets[index%weekly_count])
                cur.execute("UPDATE biology_backlog SET planned_date=%s WHERE user_id=%s AND chapter=%s AND lecture=%s;",(planned,user_id,row["chapter"],row["lecture"]))
            conn.commit()
            for index,row in enumerate(rows):
                row["planned_date"]=(start_date+timedelta(days=round(index*max(0,(end_date-start_date).days)/(len(rows)-1)))) if end_date and len(rows)>1 else (start_date if end_date else start_date+timedelta(days=(index//weekly_count)*7+offsets[index%weekly_count]))
            return rows
    return await run(op)


async def create_pending_task(kind,title,chat_id,thread_id,message_id,payload_type,file_id,media_group_id,text_content,created_by):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_pending_tasks
            (kind,title,chat_id,thread_id,source_message_id,payload_type,file_id,media_group_id,text_content,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(chat_id,source_message_id) DO UPDATE SET title=EXCLUDED.title
            RETURNING *;""",(kind,title,chat_id,thread_id,message_id,payload_type,file_id,media_group_id,text_content,created_by))
            row=cur.fetchone()
            if file_id:
                cur.execute("""INSERT INTO biology_pending_task_media(pending_id,payload_type,file_id,source_message_id)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;""",(row["id"],payload_type,file_id,message_id))
            conn.commit(); return row
    return await run(op)


async def set_pending_task_scope(pending_id,target_scope):
    if target_scope not in {"all","course",*(f"chapter_{chapter}" for chapter in range(1,6))}: return None
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_pending_tasks SET target_scope=%s WHERE id=%s RETURNING *;",(target_scope,pending_id)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def append_pending_media(media_group_id,payload_type,file_id,message_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM biology_pending_tasks WHERE media_group_id=%s ORDER BY id DESC LIMIT 1;",(media_group_id,)); row=cur.fetchone()
            if not row: return None
            cur.execute("""INSERT INTO biology_pending_task_media(pending_id,payload_type,file_id,source_message_id)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;""",(row["id"],payload_type,file_id,message_id)); conn.commit(); return row["id"]
    return await run(op)


async def get_pending_task(pending_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_pending_tasks WHERE id=%s;",(pending_id,)); return cur.fetchone()
    return await run(op)


async def confirm_pending_task(pending_id,deadline):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_pending_tasks WHERE id=%s FOR UPDATE;",(pending_id,)); p=cur.fetchone()
            if not p: return None
            reward=30 if p["kind"]=="homework" else 20
            cur.execute("""INSERT INTO biology_tasks
            (kind,title,chat_id,thread_id,source_message_id,payload_type,file_id,media_group_id,text_content,deadline,xp_reward,created_by,target_scope)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(chat_id,source_message_id) DO UPDATE SET title=EXCLUDED.title,deadline=EXCLUDED.deadline,target_scope=EXCLUDED.target_scope
            RETURNING *;""",(p["kind"],p["title"],p["chat_id"],p["thread_id"],p["source_message_id"],p["payload_type"],p["file_id"],p["media_group_id"],p["text_content"],deadline,reward,p["created_by"],p["target_scope"]))
            task=cur.fetchone()
            cur.execute("""INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id)
            SELECT %s,payload_type,file_id,source_message_id FROM biology_pending_task_media WHERE pending_id=%s
            ON CONFLICT DO NOTHING;""",(task["id"],pending_id))
            scope=p["target_scope"]
            cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
            SELECT %s,user_id FROM biology_students WHERE approved=TRUE AND onboarding_version>=19 AND
            (%s='all' OR (%s='course' AND study_track='course') OR
             (%s LIKE 'chapter_%%' AND study_track='chapter' AND current_chapter=SUBSTRING(%s FROM 9)::INTEGER))
            ON CONFLICT DO NOTHING;""",(task["id"],scope,scope,scope,scope))
            cur.execute("DELETE FROM biology_pending_tasks WHERE id=%s;",(pending_id,)); conn.commit(); return task
    return await run(op)


async def delete_pending_task(pending_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM biology_pending_tasks WHERE id=%s;",(pending_id,)); changed=cur.rowcount; conn.commit(); return changed==1
    return await run(op)


async def append_task_media(media_group_id,payload_type,file_id,message_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM biology_tasks WHERE media_group_id=%s ORDER BY id DESC LIMIT 1;",(media_group_id,)); row=cur.fetchone()
            if not row: return None
            cur.execute("INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;",(row["id"],payload_type,file_id,message_id)); conn.commit(); return row["id"]
    return await run(op)


async def get_task_media(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_task_media WHERE task_id=%s ORDER BY id;",(task_id,)); return cur.fetchall()
    return await run(op)


async def get_task(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_tasks WHERE id=%s;",(task_id,)); return cur.fetchone()
    return await run(op)


async def open_tasks(kind,user_id=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            if user_id is None:
                cur.execute("SELECT * FROM biology_tasks WHERE kind=%s AND closed=FALSE ORDER BY deadline,id;",(kind,))
            else:
                cur.execute("""SELECT t.* FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
                    WHERE t.kind=%s AND t.closed=FALSE AND ts.user_id=%s ORDER BY t.deadline,t.id;""",(kind,user_id))
            return cur.fetchall()
    return await run(op)


async def open_exam_tasks(cumulative=False,user_id=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            operator="LIKE" if cumulative else "NOT LIKE"
            if user_id is None:
                cur.execute(f"SELECT * FROM biology_tasks WHERE kind='exam' AND school_review_id IS NULL AND closed=FALSE AND title {operator} '[تراكمي]%%' ORDER BY deadline,id;")
            else:
                cur.execute(f"""SELECT t.* FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
                    WHERE t.kind='exam' AND t.school_review_id IS NULL AND t.title {operator} '[تراكمي]%%' AND ts.user_id=%s
                    AND (t.closed=FALSE OR (t.closed=TRUE AND NOT EXISTS(SELECT 1 FROM biology_submissions s WHERE s.task_id=t.id AND s.user_id=%s)))
                    ORDER BY t.closed DESC,t.deadline,t.id;""",(user_id,user_id))
            return cur.fetchall()
    return await run(op)


async def record_submission(task_id,user_id,message_id,file_unique_id,media_group_id=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,));student=cur.fetchone()
            if not student or not student['approved'] or student.get('reset_pending'): return "not_allowed"
            cur.execute("""SELECT t.*,GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline)) AS effective_deadline,
                e.extended_until FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=%s
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.id=%s AND (d.id IS NULL OR d.deleted_at IS NULL) FOR UPDATE OF t;""",(user_id,user_id,task_id));eligible=cur.fetchone()
            if not eligible or eligible.get('exam_pending_activation'): return "not_allowed"
            cur.execute("SELECT clock_timestamp() AS now;");now=cur.fetchone()['now']
            if eligible['effective_deadline']<=now: return "expired"
            if eligible.get('closed') and not (eligible.get('extended_until') and eligible['extended_until']>now): return "not_allowed"
            if eligible.get('exam_available_at') and eligible['exam_available_at']>now: return "not_allowed"
            if eligible.get('exam_approval_required'):
                cur.execute("SELECT 1 FROM biology_exam_access WHERE task_id=%s AND user_id=%s AND status='approved';",(task_id,user_id))
                if not cur.fetchone(): return "not_allowed"
            if file_unique_id:
                cur.execute("SELECT user_id FROM biology_submission_files WHERE task_id=%s AND file_unique_id=%s;",(task_id,file_unique_id))
                if cur.fetchone(): return "duplicate"
            cur.execute("SELECT submission_no,submitted_at,media_group_id FROM biology_submissions WHERE task_id=%s AND user_id=%s FOR UPDATE;",(task_id,user_id)); old=cur.fetchone(); number=(old["submission_no"]+1) if old else 1
            cur.execute("SELECT kind,xp_reward FROM biology_tasks WHERE id=%s;",(task_id,)); task=cur.fetchone()
            same_album=bool(old and old.get("submitted_at") and media_group_id and old.get("media_group_id")==media_group_id)
            if old and old.get("submitted_at") and not same_album: return "exam_locked"
            cur.execute("""INSERT INTO biology_submission_files(task_id,user_id,message_id,file_unique_id,media_group_id)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(task_id,file_unique_id) DO NOTHING RETURNING message_id;""",
                (task_id,user_id,message_id,file_unique_id,media_group_id))
            if not cur.fetchone(): return "duplicate"
            if same_album:
                conn.commit(); return "album_part"
            cur.execute("""INSERT INTO biology_submissions(task_id,user_id,submission_no,message_id,file_unique_id,media_group_id)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET submission_no=EXCLUDED.submission_no,
            message_id=EXCLUDED.message_id,file_unique_id=EXCLUDED.file_unique_id,submitted_at=CURRENT_TIMESTAMP,
            grade=NULL,graded_by=NULL,graded_at=NULL,media_group_id=EXCLUDED.media_group_id;""",
            (task_id,user_id,number,message_id,file_unique_id,media_group_id))
            if not old:
                _set_xp_event(cur,user_id,task["xp_reward"],"تسليم الواجب" if task["kind"]=="homework" else "تسليم الامتحان",f"submission:{task_id}:{user_id}")
                # A reopened task gives the student a real second chance. If a
                # non-submission warning was already issued, remove it once the
                # valid late submission is accepted.
                cur.execute("""DELETE FROM biology_warning_log WHERE id IN
                (SELECT id FROM biology_warning_log WHERE task_id=%s AND user_id=%s
                 AND reason LIKE 'عدم إرسال%%' ORDER BY created_at LIMIT 1) RETURNING id;""",(task_id,user_id))
                if cur.fetchone():
                    cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-1) WHERE user_id=%s;",(user_id,))
            conn.commit(); return "replaced" if old else "added"
    return await run(op)


async def prepare_submission_retry(task_id,user_id):
    """Delete the current answer and allow at most two student-requested retries."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT retry_count FROM biology_submissions WHERE task_id=%s AND user_id=%s FOR UPDATE;",(task_id,user_id))
            row=cur.fetchone()
            if not row: return {"status":"missing","messages":[]}
            if row["retry_count"]>=2: return {"status":"limit","messages":[]}
            cur.execute("SELECT chat_id,message_id FROM biology_submission_review_messages WHERE task_id=%s AND user_id=%s;",(task_id,user_id))
            messages=cur.fetchall()
            next_count=row["retry_count"]+1
            cur.execute("DELETE FROM biology_submission_review_messages WHERE task_id=%s AND user_id=%s;",(task_id,user_id))
            cur.execute("DELETE FROM biology_submission_files WHERE task_id=%s AND user_id=%s;",(task_id,user_id))
            cur.execute("DELETE FROM biology_submission_delivery_outbox WHERE task_id=%s AND user_id=%s;",(task_id,user_id))
            cur.execute("""UPDATE biology_submissions SET message_id=NULL,file_unique_id='',submitted_at=NULL,
                grade=NULL,graded_by=NULL,graded_at=NULL,media_group_id=NULL,retry_count=%s WHERE task_id=%s AND user_id=%s;""",(next_count,task_id,user_id))
            conn.commit(); return {"status":"ok","used":next_count,"remaining":2-next_count,"messages":messages}
    return await run(op)


async def set_submission_grade(task_id,user_id,grade,graded_by):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT kind,title FROM biology_tasks WHERE id=%s;",(task_id,)); task=cur.fetchone()
            if not task: return None
            cur.execute("""UPDATE biology_submissions SET grade=%s,graded_by=%s,graded_at=CURRENT_TIMESTAMP
                WHERE task_id=%s AND user_id=%s RETURNING task_id;""",(grade,graded_by,task_id,user_id))
            if not cur.fetchone(): return None
            if task["kind"]=="exam":
                _set_xp_event(cur,user_id,grade-80,"تعديل XP حسب درجة الامتحان",f"grade:{task_id}:{user_id}")
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s;",(user_id,)); student=cur.fetchone()
            conn.commit(); return {"task":task,"student":student,"grade":grade}
    return await run(op)


async def add_submission_review_message(chat_id,message_id,task_id,user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_submission_review_messages(chat_id,message_id,task_id,user_id)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;""",(chat_id,message_id,task_id,user_id)); conn.commit()
    await run(op)


async def grade_submission_by_review(chat_id,message_id,grade,graded_by):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT r.task_id,r.user_id FROM biology_submission_review_messages r
            WHERE r.chat_id=%s AND r.message_id=%s;""",(chat_id,message_id)); ref=cur.fetchone()
            if not ref: return None
            cur.execute("""UPDATE biology_submissions SET grade=%s,graded_by=%s,graded_at=CURRENT_TIMESTAMP
            WHERE task_id=%s AND user_id=%s;""",(grade,graded_by,ref["task_id"],ref["user_id"]))
            cur.execute("SELECT kind FROM biology_tasks WHERE id=%s;",(ref["task_id"],)); task_kind=cur.fetchone()
            if task_kind and task_kind["kind"]=="exam":
                bonus=grade-80
                _set_xp_event(cur,ref["user_id"],bonus,"تعديل XP حسب درجة الامتحان",f"grade:{ref['task_id']}:{ref['user_id']}")
            cur.execute("""SELECT s.user_id,s.full_name,s.parent_chat_id,t.title,t.kind,%s::INTEGER AS grade
            FROM biology_students s JOIN biology_tasks t ON t.id=%s WHERE s.user_id=%s;""",
            (grade,ref["task_id"],ref["user_id"])); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def submission_by_review(chat_id,message_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT r.task_id,r.user_id,t.kind,t.title,s.full_name,s.parent_chat_id
            FROM biology_submission_review_messages r
            JOIN biology_tasks t ON t.id=r.task_id
            JOIN biology_students s ON s.user_id=r.user_id
            WHERE r.chat_id=%s AND r.message_id=%s;""",(chat_id,message_id)); return cur.fetchone()
    return await run(op)


async def save_exam_correction(task_id,user_id,payload_type,file_id,grade,corrected_by):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE biology_submissions SET grade=%s,graded_by=%s,graded_at=CURRENT_TIMESTAMP
            WHERE task_id=%s AND user_id=%s;""",(grade,corrected_by,task_id,user_id))
            cur.execute("""INSERT INTO biology_exam_corrections(task_id,user_id,payload_type,file_id,grade,corrected_by)
            VALUES(%s,%s,%s,%s,%s,%s);""",(task_id,user_id,payload_type,file_id,grade,corrected_by))
            cur.execute("SELECT kind FROM biology_tasks WHERE id=%s;",(task_id,)); task=cur.fetchone()
            if task and task["kind"]=="exam":
                _set_xp_event(cur,user_id,grade-80,"تعديل XP حسب درجة الامتحان",f"grade:{task_id}:{user_id}")
            conn.commit()
    await run(op)


async def link_parent(parent_link_code,parent_chat_id,parent_username=None,parent_full_name=None,notify_student=True):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE UPPER(parent_link_code)=UPPER(%s);",(parent_link_code,)); row=cur.fetchone()
            if not row: return None
            if row["user_id"]==parent_chat_id: return {"status":"self_parent_forbidden","full_name":row["full_name"],"user_id":row["user_id"]}
            cur.execute("""INSERT INTO biology_parent_links(student_id,parent_chat_id,parent_username,parent_full_name,approved,notify_student)
            VALUES(%s,%s,%s,%s,TRUE,%s) ON CONFLICT(student_id,parent_chat_id) DO UPDATE SET
            parent_username=EXCLUDED.parent_username,parent_full_name=EXCLUDED.parent_full_name,notify_student=EXCLUDED.notify_student,approved=TRUE,linked_at=CURRENT_TIMESTAMP;""",
            (row["user_id"],parent_chat_id,parent_username,parent_full_name,notify_student))
            if not row.get("parent_chat_id"):
                cur.execute("UPDATE biology_students SET parent_chat_id=%s,parent_username=%s,parent_full_name=%s,parent_approved=TRUE WHERE user_id=%s RETURNING *;",(parent_chat_id,parent_username,parent_full_name,row["user_id"])); row=cur.fetchone()
            conn.commit(); row["notify_student"]=notify_student; row["linked_parent_chat_id"]=parent_chat_id; return row
    return await run(op)


async def approve_parent(student_id,approved=True,parent_chat_id=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            selected_parent_chat_id=parent_chat_id
            if selected_parent_chat_id is None:
                cur.execute("SELECT parent_chat_id FROM biology_parent_links WHERE student_id=%s ORDER BY linked_at DESC LIMIT 1;",(student_id,)); link=cur.fetchone()
                selected_parent_chat_id=link["parent_chat_id"] if link else None
            if not selected_parent_chat_id: return None
            cur.execute("UPDATE biology_parent_links SET approved=%s WHERE student_id=%s AND parent_chat_id=%s RETURNING *;",(approved,student_id,selected_parent_chat_id)); link=cur.fetchone()
            if not link: return None
            cur.execute("UPDATE biology_students SET parent_approved=TRUE WHERE user_id=%s RETURNING *;",(student_id,)); row=cur.fetchone(); conn.commit()
            row["approved_parent_chat_id"]=selected_parent_chat_id; row["approved_parent_username"]=link.get("parent_username"); row["approved_parent_full_name"]=link.get("parent_full_name"); return row
    return await run(op)


async def decide_parent_link(student_id,parent_chat_id,approved):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_parent_links WHERE student_id=%s AND parent_chat_id=%s FOR UPDATE;",(student_id,parent_chat_id)); link=cur.fetchone()
            if not link: return None
            if approved:
                cur.execute("UPDATE biology_parent_links SET approved=TRUE WHERE student_id=%s AND parent_chat_id=%s RETURNING *;",(student_id,parent_chat_id)); link=cur.fetchone()
                cur.execute("""UPDATE biology_students SET parent_chat_id=COALESCE(parent_chat_id,%s),
                parent_username=CASE WHEN parent_chat_id IS NULL THEN %s ELSE parent_username END,
                parent_full_name=CASE WHEN parent_chat_id IS NULL THEN %s ELSE parent_full_name END,
                parent_approved=TRUE WHERE user_id=%s RETURNING *;""",(parent_chat_id,link.get("parent_username"),link.get("parent_full_name"),student_id)); student=cur.fetchone()
            else:
                cur.execute("DELETE FROM biology_parent_links WHERE student_id=%s AND parent_chat_id=%s;",(student_id,parent_chat_id))
                cur.execute("SELECT * FROM biology_students WHERE user_id=%s;",(student_id,)); student=cur.fetchone()
            conn.commit(); return {"student":student,"link":link,"approved":approved}
    return await run(op)


async def all_parents_admin_view():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT p.*,s.full_name student_name,s.xp,s.warnings FROM biology_parent_links p
            JOIN biology_students s ON s.user_id=p.student_id ORDER BY p.linked_at DESC;"""); return cur.fetchall()
    return await run(op)


async def request_extension(task_id,user_id,hours=24):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS n FROM biology_task_extensions
            WHERE user_id=%s AND requested_at>=DATE_TRUNC('week',CURRENT_TIMESTAMP);""",(user_id,)); used=cur.fetchone()["n"]
            if used>=2: return {"status":"limit","used":used}
            cur.execute("SELECT deadline,closed FROM biology_tasks WHERE id=%s;",(task_id,)); task=cur.fetchone()
            if not task or task["closed"]: return {"status":"closed"}
            extended_until=max(task["deadline"],datetime_now(cur))+timedelta(hours=min(24,max(1,hours)))
            cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,extended_until) VALUES(%s,%s,%s)
            ON CONFLICT(task_id,user_id) DO NOTHING RETURNING *;""",(task_id,user_id,extended_until)); row=cur.fetchone()
            if not row: return {"status":"exists"}
            conn.commit(); return {"status":"ok","extended_until":extended_until,"used":used+1}
    return await run(op)


def datetime_now(cur):
    cur.execute("SELECT CURRENT_TIMESTAMP AS now;")
    return cur.fetchone()["now"]


async def student_exam_lock(user_id):
    """Return the first unpaid exam; any unsubmitted exam blocks the next preparation."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.* FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s
                AND t.closed=FALSE AND (d.id IS NULL OR d.deleted_at IS NULL)
                AND NOT EXISTS(SELECT 1 FROM biology_submissions s WHERE s.task_id=t.id AND s.user_id=%s)
                ORDER BY CASE WHEN t.exam_pending_activation THEN 0 ELSE 1 END,t.created_at,t.id LIMIT 1;""",(user_id,user_id)); return cur.fetchone()
    return await run(op)


async def effective_task_deadline(task_id,user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline)) AS deadline,t.closed,
            EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL) AS submitted,
            EXISTS(SELECT 1 FROM biology_task_students ts WHERE ts.task_id=t.id AND ts.user_id=%s) AS assigned
            FROM biology_tasks t LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=%s WHERE t.id=%s;""",(user_id,user_id,user_id,task_id)); return cur.fetchone()
    return await run(op)


async def create_archive_exam(chapter,title,created_by,lecture=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO biology_exam_archive(chapter,lecture,title,created_by) VALUES(%s,%s,%s,%s) RETURNING *;",(chapter,lecture,title,created_by)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def add_archive_exam_media(archive_id,payload_type,file_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_exam_archive_media(archive_id,payload_type,file_id)
            VALUES(%s,%s,%s) ON CONFLICT DO NOTHING;""",(archive_id,payload_type,file_id)); conn.commit()
    await run(op)


async def archive_exams(chapter):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_exam_archive WHERE chapter=%s ORDER BY id DESC;",(chapter,)); return cur.fetchall()
    return await run(op)


async def archive_lectures(chapter):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT lecture FROM biology_exam_archive WHERE chapter=%s AND lecture IS NOT NULL ORDER BY lecture;",(chapter,)); return [r["lecture"] for r in cur.fetchall()]
    return await run(op)


async def archive_exams_by_lecture(chapter,lecture):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_exam_archive WHERE chapter=%s AND lecture=%s ORDER BY id DESC;",(chapter,lecture)); return cur.fetchall()
    return await run(op)


async def get_archive_exam(archive_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_exam_archive WHERE id=%s;",(archive_id,)); return cur.fetchone()
    return await run(op)


async def get_archive_exam_media(archive_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_exam_archive_media WHERE archive_id=%s ORDER BY id;",(archive_id,)); return cur.fetchall()
    return await run(op)


async def create_resource(category,chapter,title,created_by):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_resources(category,chapter,title,created_by)
            VALUES(%s,%s,%s,%s) RETURNING *;""",(category,chapter,title,created_by)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def add_resource_media(resource_id,payload_type,file_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_resource_media(resource_id,payload_type,file_id)
            VALUES(%s,%s,%s) ON CONFLICT DO NOTHING;""",(resource_id,payload_type,file_id)); conn.commit()
    await run(op)


async def resources_by_chapter(category,chapter):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_resources WHERE category=%s AND chapter=%s ORDER BY id DESC;",(category,chapter)); return cur.fetchall()
    return await run(op)


async def get_resource(resource_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_resources WHERE id=%s;",(resource_id,)); return cur.fetchone()
    return await run(op)


async def get_resource_media(resource_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_resource_media WHERE resource_id=%s ORDER BY id;",(resource_id,)); return cur.fetchall()
    return await run(op)


async def parent_report_bundle(user_id,current_start,current_end,previous_start):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s;",(user_id,)); student=cur.fetchone()
            cur.execute("""SELECT t.id,t.kind,t.title,t.deadline,sub.submitted_at,sub.grade
            FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
            LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=%s
            WHERE t.created_at>=%s AND t.created_at<%s ORDER BY t.created_at;""",(user_id,user_id,current_start,current_end)); current=cur.fetchall()
            cur.execute("""SELECT t.id,t.kind,t.title,t.deadline,sub.submitted_at,sub.grade
            FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
            LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=%s
            WHERE t.created_at>=%s AND t.created_at<%s ORDER BY t.created_at;""",(user_id,user_id,previous_start,current_start)); previous=cur.fetchall()
            cur.execute("""SELECT reason,created_at FROM biology_warning_log
            WHERE user_id=%s AND created_at>=%s AND created_at<%s ORDER BY created_at;""",(user_id,current_start,current_end)); warnings=cur.fetchall()
            return {"student":student,"current":current,"previous":previous,"warnings":warnings}
    return await run(op)


async def student_warning_history(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id,reason,created_at FROM biology_warning_log WHERE user_id=%s ORDER BY created_at DESC;",(user_id,)); return cur.fetchall()
    return await run(op)


async def mark_lecture_progress(user_id,chapter,lecture,completed=False,completion_method="bot_lecture"):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT completed_at FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s;",(user_id,chapter,lecture)); old=cur.fetchone()
            cur.execute("""INSERT INTO biology_lecture_progress(user_id,chapter,lecture,opened_at,completed_at,completion_method)
            VALUES(%s,%s,%s,CURRENT_TIMESTAMP,CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,%s)
            ON CONFLICT(user_id,chapter,lecture) DO UPDATE SET opened_at=COALESCE(biology_lecture_progress.opened_at,CURRENT_TIMESTAMP),
            completed_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE biology_lecture_progress.completed_at END,
            completion_method=CASE WHEN %s THEN %s ELSE biology_lecture_progress.completion_method END
            RETURNING *;""",(user_id,chapter,lecture,completed,completion_method,completed,completed,completion_method)); row=cur.fetchone()
            conn.commit(); return row
    return await run(op)


async def lecture_progress(user_id,chapter,lecture):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s;",(user_id,chapter,lecture))
            return cur.fetchone()
    return await run(op)


async def lecture_opened_in_assigned_preparation(user_id,chapter,lecture):
    """Keep an already opened lecture finishable when the next prep is published."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.study_track,s.track_started_on,lp.opened_at
                FROM biology_students s JOIN biology_lecture_progress lp ON lp.user_id=s.user_id
                WHERE s.user_id=%s AND lp.chapter=%s AND lp.lecture=%s
                  AND s.approved=TRUE AND s.reset_pending=FALSE;""",
                (user_id,chapter,lecture))
            row=cur.fetchone()
            if not row or not row['opened_at']: return False
            cur.execute("""SELECT EXISTS(
                SELECT 1 FROM biology_early_preparation_unlocks e
                WHERE e.user_id=%s AND e.study_track=%s AND e.chapter=%s
                  AND %s=ANY(STRING_TO_ARRAY(e.lectures,',')::INTEGER[])
                  AND e.unlocked_at<=%s
                UNION ALL
                SELECT 1 FROM biology_personal_preparations pp
                WHERE %s='chapter' AND pp.user_id=%s AND pp.chapter=%s
                  AND %s=ANY(STRING_TO_ARRAY(pp.lectures,',')::INTEGER[])
                  AND pp.target_date<=((%s AT TIME ZONE 'Asia/Baghdad')::DATE)
                  AND pp.target_date>=COALESCE(%s::DATE,pp.target_date)
                UNION ALL
                SELECT 1 FROM biology_preparations p
                WHERE %s='course' AND p.chapter=%s AND p.published=TRUE
                  AND %s=ANY(STRING_TO_ARRAY(p.lectures,',')::INTEGER[])
                  AND p.published_at<=%s
                  AND p.target_date>=COALESCE(%s::DATE,p.target_date)
                ) AS allowed;""",
                (user_id,row['study_track'],chapter,lecture,row['opened_at'],
                 row['study_track'],user_id,chapter,lecture,row['opened_at'],row['track_started_on'],
                 row['study_track'],chapter,lecture,row['opened_at'],row['track_started_on']))
            return bool(cur.fetchone()['allowed'])
    return await run(op)


async def award_daily_preparation(user_id,chapter,lecture):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT pp.* FROM biology_personal_preparations pp WHERE pp.user_id=%s AND pp.chapter=%s
            AND %s=ANY(STRING_TO_ARRAY(pp.lectures,',')::INTEGER[]) ORDER BY pp.target_date DESC LIMIT 1;""",(user_id,chapter,lecture)); personal=cur.fetchone()
            if personal:
                nums=[int(x) for x in personal["lectures"].split(',')]
                cur.execute("SELECT COUNT(*) n FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s) AND completed_at IS NOT NULL;",(user_id,chapter,nums))
                if cur.fetchone()["n"]<len(nums): return {"awarded":False,"prep":personal}
                change=_set_xp_event(cur,user_id,15,"إكمال التحضير الشخصي",f"personal_prep:{personal['id']}:{user_id}")
                conn.commit(); return {"awarded":change>0,"prep":personal}
            cur.execute("""SELECT p.* FROM biology_preparations p WHERE p.chapter=%s AND %s=ANY(STRING_TO_ARRAY(p.lectures,',')::INTEGER[])
            AND p.published=TRUE ORDER BY p.target_date DESC LIMIT 1;""",(chapter,lecture)); prep=cur.fetchone()
            if not prep: return None
            nums=[int(x) for x in prep["lectures"].split(',')]
            cur.execute("SELECT COUNT(*) n FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s) AND completed_at IS NOT NULL;",(user_id,chapter,nums))
            if cur.fetchone()["n"]<len(nums): return {"awarded":False,"prep":prep}
            change=_set_xp_event(cur,user_id,15,"إكمال التحضير اليومي",f"prep:{prep['prep_no']}:{user_id}")
            conn.commit(); return {"awarded":change>0,"prep":prep}
    return await run(op)


async def incomplete_preparation_students(stage="overdue"):
    def op():
        with connect() as conn, conn.cursor() as cur:
            window="p.published_at<=CURRENT_TIMESTAMP-INTERVAL '24 hours'" if stage=="overdue" else "p.published_at<=CURRENT_TIMESTAMP-INTERVAL '18 hours' AND p.published_at>CURRENT_TIMESTAMP-INTERVAL '24 hours'"
            cur.execute(f"""SELECT s.user_id,s.full_name,p.prep_no,p.chapter,p.lectures,p.published_at
            FROM biology_students s CROSS JOIN biology_preparations p
            WHERE s.approved=TRUE AND s.onboarding_version>=19 AND s.study_track='course'
            AND p.published=TRUE AND p.published_at IS NOT NULL AND {window}
            AND p.published_at>CURRENT_TIMESTAMP-INTERVAL '3 days'
            AND NOT EXISTS (SELECT 1 FROM biology_leave_requests lr WHERE lr.user_id=s.user_id AND lr.leave_date=p.target_date AND lr.status='approved')
            AND EXISTS (SELECT 1 FROM UNNEST(STRING_TO_ARRAY(p.lectures,',')) x
              WHERE NOT EXISTS (SELECT 1 FROM biology_lecture_progress lp WHERE lp.user_id=s.user_id AND lp.chapter=p.chapter AND lp.lecture=x::INTEGER AND lp.completed_at IS NOT NULL));"""); return cur.fetchall()
    return await run(op)


async def incomplete_personal_preparation_students(stage="overdue"):
    def op():
        with connect() as conn, conn.cursor() as cur:
            window="pp.notified_at<=CURRENT_TIMESTAMP-INTERVAL '24 hours'" if stage=="overdue" else "pp.notified_at<=CURRENT_TIMESTAMP-INTERVAL '18 hours' AND pp.notified_at>CURRENT_TIMESTAMP-INTERVAL '24 hours'"
            cur.execute(f"""SELECT s.user_id,s.full_name,pp.id AS prep_id,pp.chapter,pp.lectures,pp.target_date,pp.notified_at
            FROM biology_personal_preparations pp JOIN biology_students s ON s.user_id=pp.user_id
            WHERE s.approved=TRUE AND pp.notified=TRUE AND pp.notified_at IS NOT NULL AND {window}
            AND pp.notified_at>CURRENT_TIMESTAMP-INTERVAL '3 days'
            AND NOT EXISTS (SELECT 1 FROM biology_leave_requests lr WHERE lr.user_id=s.user_id AND lr.leave_date=pp.target_date AND lr.status='approved')
            AND EXISTS (SELECT 1 FROM UNNEST(STRING_TO_ARRAY(pp.lectures,',')) x
              WHERE NOT EXISTS (SELECT 1 FROM biology_lecture_progress lp WHERE lp.user_id=s.user_id AND lp.chapter=pp.chapter AND lp.lecture=x::INTEGER AND lp.completed_at IS NOT NULL));""")
            return cur.fetchall()
    return await run(op)


async def all_students_admin_view():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT user_id,username,full_name,school,target_grade,approved,xp,warnings,parent_chat_id,parent_username,parent_full_name,parent_approved,parent_link_code,registered_at
            FROM biology_students ORDER BY registered_at DESC;"""); return cur.fetchall()
    return await run(op)


async def weekly_parent_reports():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.user_id,s.full_name,p.parent_chat_id,s.xp,s.warnings,
            COUNT(DISTINCT t.id) FILTER (WHERE t.created_at>=CURRENT_TIMESTAMP-INTERVAL '7 days') AS assigned,
            COUNT(DISTINCT sub.task_id) FILTER (WHERE t.created_at>=CURRENT_TIMESTAMP-INTERVAL '7 days') AS submitted,
            ROUND(AVG(sub.grade) FILTER (WHERE sub.graded_at>=CURRENT_TIMESTAMP-INTERVAL '7 days'),1) AS grade_average,
            COUNT(DISTINCT w.id) FILTER (WHERE w.created_at>=CURRENT_TIMESTAMP-INTERVAL '7 days') AS weekly_warnings
            FROM biology_students s JOIN biology_parent_links p ON p.student_id=s.user_id AND p.approved=TRUE
            LEFT JOIN biology_task_students ts ON ts.user_id=s.user_id
            LEFT JOIN biology_tasks t ON t.id=ts.task_id AND t.created_at>=CURRENT_TIMESTAMP-INTERVAL '7 days'
            LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=s.user_id
            LEFT JOIN biology_warning_log w ON w.user_id=s.user_id AND w.created_at>=CURRENT_TIMESTAMP-INTERVAL '7 days'
            WHERE s.approved=TRUE
            GROUP BY s.user_id,s.full_name,p.parent_chat_id,s.xp,s.warnings ORDER BY s.user_id;"""); return cur.fetchall()
    return await run(op)


async def setting_value(key):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM biology_settings WHERE key=%s;",(key,)); row=cur.fetchone(); return row["value"] if row else None
    return await run(op)


async def set_setting_value(key,value):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_settings(key,value) VALUES(%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;""",(key,value)); conn.commit()
    await run(op)


async def adjust_xp(user_id,delta,reason,actor_id=0,event_key=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            key=event_key or f"manual:{actor_id}:{user_id}:{datetime_now(cur).timestamp()}"
            change=_set_xp_event(cur,user_id,delta,reason,key)
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s;",(user_id,)); row=cur.fetchone(); conn.commit(); return row,change
    return await run(op)


async def update_student_profile(user_id,field,value):
    if field not in {"full_name","school","target_grade"}: return None
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE biology_students SET {field}=%s,last_seen=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;",(value,user_id)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def student_achievements(user_id,start_at):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(delta),0) xp_earned,COUNT(*) events FROM biology_xp_log WHERE user_id=%s AND created_at>=%s;",(user_id,start_at)); xp=cur.fetchone()
            cur.execute("""SELECT COUNT(*) FILTER(WHERE t.kind='homework') homeworks,COUNT(*) FILTER(WHERE t.kind='exam') exams,
            ROUND(AVG(s.grade) FILTER(WHERE t.kind='exam'),1) average FROM biology_submissions s JOIN biology_tasks t ON t.id=s.task_id
            WHERE s.user_id=%s AND s.submitted_at>=%s;""",(user_id,start_at)); tasks=cur.fetchone()
            cur.execute("SELECT COUNT(*) lectures FROM biology_lecture_progress WHERE user_id=%s AND completed_at>=%s;",(user_id,start_at)); lectures=cur.fetchone()
            return {**xp,**tasks,**lectures}
    return await run(op)


async def set_backlog_deadline(user_id,target_date):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_backlog SET target_completion=%s WHERE user_id=%s AND completed=FALSE;",(target_date,user_id)); conn.commit()
    await run(op)


async def exam_access(task_id,user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_exam_access WHERE task_id=%s AND user_id=%s;",(task_id,user_id)); return cur.fetchone()
    return await run(op)


async def request_exam_access(task_id,user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_exam_access(task_id,user_id) VALUES(%s,%s)
            ON CONFLICT(task_id,user_id) DO UPDATE SET requested_at=CURRENT_TIMESTAMP WHERE biology_exam_access.status='denied' RETURNING *;""",(task_id,user_id)); row=cur.fetchone()
            if not row:
                cur.execute("SELECT * FROM biology_exam_access WHERE task_id=%s AND user_id=%s;",(task_id,user_id)); row=cur.fetchone()
            conn.commit(); return row
    return await run(op)


async def decide_exam_access(task_id,user_id,status,approved_by):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_exam_access(task_id,user_id,status,approved_by,approved_at) VALUES(%s,%s,%s,%s,CURRENT_TIMESTAMP)
            ON CONFLICT(task_id,user_id) DO UPDATE SET status=EXCLUDED.status,approved_by=EXCLUDED.approved_by,approved_at=CURRENT_TIMESTAMP RETURNING *;""",(task_id,user_id,status,approved_by)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def create_extension_request(task_id,user_id,hours):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT xp,parent_chat_id,full_name FROM biology_students WHERE user_id=%s;",(user_id,)); student=cur.fetchone()
            if not student or student["xp"]<150: return {"status":"xp","student":student}
            cur.execute("SELECT kind,closed,deadline FROM biology_tasks WHERE id=%s;",(task_id,)); task=cur.fetchone()
            if not task or task["kind"]!="exam" or task["closed"]: return {"status":"closed","student":student}
            if task["deadline"]<=datetime_now(cur): return {"status":"late","student":student}
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(task_id,user_id))
            if cur.fetchone(): return {"status":"submitted","student":student}
            if not student.get("parent_chat_id"): return {"status":"parent_missing","student":student}
            cur.execute("SELECT COUNT(*) n FROM biology_extension_requests WHERE user_id=%s AND status='approved' AND created_at>=DATE_TRUNC('week',CURRENT_TIMESTAMP);",(user_id,))
            if cur.fetchone()["n"]>=2: return {"status":"limit","student":student}
            cur.execute("""INSERT INTO biology_extension_requests(task_id,user_id,hours) VALUES(%s,%s,%s)
            ON CONFLICT(task_id,user_id) DO UPDATE SET hours=EXCLUDED.hours,status='pending',created_at=CURRENT_TIMESTAMP WHERE biology_extension_requests.status='denied' RETURNING *;""",(task_id,user_id,hours)); req=cur.fetchone()
            if not req: return {"status":"exists","student":student}
            conn.commit(); return {"status":"ok","request":req,"student":student}
    return await run(op)


async def decide_extension_request(request_id,approved):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT r.*,t.deadline,t.closed,s.xp FROM biology_extension_requests r JOIN biology_tasks t ON t.id=r.task_id
            JOIN biology_students s ON s.user_id=r.user_id WHERE r.id=%s FOR UPDATE;""",(request_id,)); row=cur.fetchone()
            if not row or row["status"]!='pending': return None
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(row["task_id"],row["user_id"]))
            already_submitted=bool(cur.fetchone())
            status='approved' if approved and row['xp']>=150 and not row['closed'] and not already_submitted else 'denied'
            cur.execute("UPDATE biology_extension_requests SET status=%s,decided_at=CURRENT_TIMESTAMP WHERE id=%s;",(status,request_id))
            if status=='approved':
                until=max(row["deadline"],datetime_now(cur))+timedelta(hours=row["hours"])
                cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,extended_until) VALUES(%s,%s,%s)
                ON CONFLICT(task_id,user_id) DO UPDATE SET extended_until=EXCLUDED.extended_until;""",(row["task_id"],row["user_id"],until))
                _set_xp_event(cur,row["user_id"],-150,"تمديد وقت الامتحان",f"extension:{request_id}")
                row["extended_until"]=until
            row["status"]=status; row["already_submitted"]=already_submitted; conn.commit(); return row
    return await run(op)


async def create_leave_request(user_id,leave_date):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT xp,parent_chat_id,full_name,study_track,schedule_mode,study_days FROM biology_students WHERE user_id=%s;",(user_id,)); s=cur.fetchone()
            if not s or not (s.get("study_track")=="chapter" or s.get("schedule_mode")=="custom"): return {"status":"track","student":s}
            if s["xp"]<400: return {"status":"xp","student":s}
            cur.execute("SELECT COUNT(*) n FROM biology_leave_requests WHERE user_id=%s AND leave_date>=date_trunc('month',%s::date)::date AND leave_date<(date_trunc('month',%s::date)+INTERVAL '1 month')::date AND status IN ('pending','approved');",(user_id,leave_date,leave_date))
            if cur.fetchone()["n"]>=4: return {"status":"limit","student":s}
            cur.execute("INSERT INTO biology_leave_requests(user_id,leave_date) VALUES(%s,%s) ON CONFLICT(user_id,leave_date) DO UPDATE SET status='pending',created_at=CURRENT_TIMESTAMP WHERE biology_leave_requests.status='denied' RETURNING *;",(user_id,leave_date)); req=cur.fetchone()
            if not req: return {"status":"exists","student":s}
            conn.commit(); return {"status":"ok","request":req,"student":s}
    return await run(op)


async def create_parent_leave(user_id,leave_date,parent_chat_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM biology_parent_links WHERE student_id=%s AND parent_chat_id=%s AND approved=TRUE) ok;",(user_id,parent_chat_id))
            if not cur.fetchone()["ok"]: return {"status":"forbidden"}
            cur.execute("SELECT xp,full_name,study_track,schedule_mode,study_days FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); s=cur.fetchone()
            if not s or not (s.get("study_track")=="chapter" or s.get("schedule_mode")=="custom"): return {"status":"track","student":s}
            if s["xp"]<400: return {"status":"xp","student":s}
            cur.execute("""SELECT COUNT(*) n FROM biology_leave_requests WHERE user_id=%s AND leave_date>=date_trunc('month',%s::date)::date AND leave_date<(date_trunc('month',%s::date)+INTERVAL '1 month')::date AND status IN ('pending','approved');""",(user_id,leave_date,leave_date))
            if cur.fetchone()["n"]>=4: return {"status":"limit","student":s}
            cur.execute("""INSERT INTO biology_leave_requests(user_id,leave_date,status,parent_decided_at) VALUES(%s,%s,'approved',CURRENT_TIMESTAMP) ON CONFLICT(user_id,leave_date) DO NOTHING RETURNING *;""",(user_id,leave_date)); req=cur.fetchone()
            if not req: return {"status":"exists","student":s}
            _set_xp_event(cur,user_id,-400,"إجازة يوم كامل بطلب ولي الأمر",f"leave:{req['id']}")
            # Shift the personal preparation schedule one slot forward when the leave day has an unnotified prep.
            cur.execute("SELECT id,target_date FROM biology_personal_preparations WHERE user_id=%s AND target_date=%s AND notified=FALSE FOR UPDATE;",(user_id,leave_date)); first=cur.fetchone()
            if first:
                cur.execute("SELECT id,target_date FROM biology_personal_preparations WHERE user_id=%s AND target_date>%s AND notified=FALSE ORDER BY target_date,id FOR UPDATE;",(user_id,leave_date)); later=cur.fetchall()
                dates=[x["target_date"] for x in later]
                if dates:
                    dates.append(_next_study_date(dates[-1],s.get('study_days')))
                    cur.execute("UPDATE biology_personal_preparations SET target_date=target_date+10000 WHERE user_id=%s AND target_date>=%s AND notified=FALSE;",(user_id,leave_date))
                    for i,x in enumerate([first]+later):
                        if i>=len(dates): break
                        cur.execute("UPDATE biology_personal_preparations SET target_date=%s WHERE id=%s;",(dates[i],x["id"]))
            conn.commit(); return {"status":"ok","student":s,"request":req}
    return await run(op)


async def decide_leave_request(request_id,approved):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT r.*,s.xp,s.study_track,s.schedule_mode,s.study_days FROM biology_leave_requests r JOIN biology_students s ON s.user_id=r.user_id WHERE r.id=%s FOR UPDATE;",(request_id,)); row=cur.fetchone()
            if not row or row["status"]!='pending': return None
            status='approved' if approved and row['xp']>=400 and (row.get('study_track')=='chapter' or row.get('schedule_mode')=='custom') else 'denied'
            if status=='approved':
                cur.execute("SELECT COUNT(*) n FROM biology_leave_requests WHERE user_id=%s AND leave_date>=date_trunc('month',%s::date)::date AND leave_date<(date_trunc('month',%s::date)+INTERVAL '1 month')::date AND status='approved' AND id<>%s;",(row['user_id'],row['leave_date'],row['leave_date'],request_id))
                if cur.fetchone()['n']>=4: status='denied'
            cur.execute("UPDATE biology_leave_requests SET status=%s,parent_decided_at=CURRENT_TIMESTAMP WHERE id=%s;",(status,request_id))
            if status=='approved':
                _set_xp_event(cur,row["user_id"],-400,"إجازة يوم كامل",f"leave:{request_id}")
                cur.execute("SELECT id,target_date FROM biology_personal_preparations WHERE user_id=%s AND target_date=%s AND notified=FALSE FOR UPDATE;",(row['user_id'],row['leave_date']))
                first=cur.fetchone()
                if first:
                    cur.execute("SELECT id,target_date FROM biology_personal_preparations WHERE user_id=%s AND target_date>%s AND notified=FALSE ORDER BY target_date,id FOR UPDATE;",(row['user_id'],row['leave_date']))
                    later=cur.fetchall(); dates=[x['target_date'] for x in later]
                    if dates:
                        dates.append(_next_study_date(dates[-1],row.get('study_days')))
                        cur.execute("UPDATE biology_personal_preparations SET target_date=target_date+10000 WHERE user_id=%s AND target_date>=%s AND notified=FALSE;",(row['user_id'],row['leave_date']))
                        ordered=[first]+later
                        for i,x in enumerate(ordered):
                            if i>=len(dates): break
                            cur.execute("UPDATE biology_personal_preparations SET target_date=%s WHERE id=%s;",(dates[i],x['id']))
            row['status']=status; conn.commit(); return row
    return await run(op)


async def has_leave(user_id,target_date):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM biology_leave_requests WHERE user_id=%s AND leave_date=%s AND status='approved') ok;",(user_id,target_date)); return cur.fetchone()["ok"]
    return await run(op)


async def scheduled_exam_parent_reminders():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_scheduled_tasks WHERE kind='exam' AND published=FALSE AND parent_reminder_sent=FALSE
            AND publish_at>CURRENT_TIMESTAMP AND publish_at<=CURRENT_TIMESTAMP+INTERVAL '30 minutes';"""); return cur.fetchall()
    return await run(op)


async def mark_scheduled_parent_reminder(schedule_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_scheduled_tasks SET parent_reminder_sent=TRUE WHERE id=%s;",(schedule_id,)); conn.commit()
    await run(op)


async def weekly_schedule(start_date,end_date):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_preparations WHERE target_date BETWEEN %s AND %s ORDER BY target_date;",(start_date,end_date)); preps=cur.fetchall()
            cur.execute("SELECT * FROM biology_scheduled_tasks WHERE publish_at::date BETWEEN %s AND %s ORDER BY publish_at;",(start_date,end_date)); tasks=cur.fetchall()
            return preps,tasks
    return await run(op)


async def weekly_top_student(start_at):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.user_id,s.full_name,s.parent_chat_id,COALESCE(SUM(x.delta),0) earned
            FROM biology_students s LEFT JOIN biology_xp_log x ON x.user_id=s.user_id AND x.created_at>=%s
            WHERE s.approved=TRUE GROUP BY s.user_id,s.full_name,s.parent_chat_id ORDER BY earned DESC,s.user_id LIMIT 1;""",(start_at,)); return cur.fetchone()
    return await run(op)


async def buy_remove_warning(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT xp,warnings FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); s=cur.fetchone()
            if not s or s["warnings"]<1: return "none"
            if s["xp"]<500: return "xp"
            cur.execute("DELETE FROM biology_warning_log WHERE id=(SELECT id FROM biology_warning_log WHERE user_id=%s ORDER BY created_at DESC LIMIT 1) RETURNING id;",(user_id,)); deleted=cur.fetchone()
            if not deleted: return "none"
            cur.execute("UPDATE biology_students SET warnings=warnings-1 WHERE user_id=%s;",(user_id,))
            _set_xp_event(cur,user_id,-500,"فك إنذار واحد",f"unwarn-purchase:{user_id}:{deleted['id']}")
            conn.commit(); return "ok"
    return await run(op)


async def swap_next_preparations():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT prep_no,target_date FROM biology_preparations WHERE published=FALSE ORDER BY target_date,prep_no LIMIT 2 FOR UPDATE;"); rows=cur.fetchall()
            if len(rows)<2: return None
            first,second=rows; temp=first["target_date"]+timedelta(days=10000)
            cur.execute("UPDATE biology_preparations SET target_date=%s WHERE prep_no=%s;",(temp,first["prep_no"]))
            cur.execute("UPDATE biology_preparations SET target_date=%s WHERE prep_no=%s;",(first["target_date"],second["prep_no"]))
            cur.execute("UPDATE biology_preparations SET target_date=%s WHERE prep_no=%s;",(second["target_date"],first["prep_no"]))
            conn.commit(); return True
    return await run(op)


async def add_extra_preparation(target_date,chapter,lectures):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(prep_no),0)+1 n FROM biology_preparations;"); n=cur.fetchone()["n"]
            cur.execute("INSERT INTO biology_preparations(prep_no,target_date,lectures,chapter,chapter_prep_no) VALUES(%s,%s,%s,%s,%s) RETURNING *;",(n,target_date,lectures,chapter,n)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def due_tasks():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_tasks WHERE closed=FALSE AND deadline<=CURRENT_TIMESTAMP ORDER BY deadline;"); return cur.fetchall()
    return await run(op)


async def unreleased_closed_exams():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_tasks WHERE kind='exam' AND closed=TRUE AND questions_released=FALSE ORDER BY deadline;"); return cur.fetchall()
    return await run(op)


async def recently_closed_tasks_for_warning_recovery(days=30):
    """Return recent closed tasks so missing warnings can be repaired after restarts/failures."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE closed=TRUE
            AND deadline<=CURRENT_TIMESTAMP
            AND deadline>=CURRENT_TIMESTAMP-(%s || ' days')::INTERVAL
            ORDER BY deadline,id;""",(max(1,min(90,days)),))
            return cur.fetchall()
    return await run(op)


async def closed_exams_pending_champion(days=30):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE kind='exam' AND closed=TRUE
            AND champion_announced=FALSE AND deadline>=CURRENT_TIMESTAMP-(%s || ' days')::INTERVAL
            ORDER BY deadline,id;""",(max(1,min(90,days)),))
            return cur.fetchall()
    return await run(op)


async def task_warning_audit(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_tasks WHERE id=%s;",(task_id,)); task=cur.fetchone()
            if not task: return None
            cur.execute("""SELECT s.user_id,s.full_name,
            EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=%s AND sub.user_id=s.user_id AND sub.submitted_at IS NOT NULL) AS submitted,
            EXISTS(SELECT 1 FROM biology_warning_log w WHERE w.task_id=%s AND w.user_id=s.user_id) AS warned,
            (SELECT e.extended_until FROM biology_task_extensions e WHERE e.task_id=%s AND e.user_id=s.user_id) AS extended_until
            FROM biology_task_students r JOIN biology_students s ON s.user_id=r.user_id
            WHERE r.task_id=%s ORDER BY s.full_name,s.user_id;""",(task_id,task_id,task_id,task_id))
            rows=cur.fetchall(); return {"task":task,"students":rows}
    return await run(op)


async def due_teacher_exam_deadline_reminders():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE kind='exam' AND closed=FALSE AND teacher_deadline_reminder_sent=FALSE
            AND deadline>CURRENT_TIMESTAMP AND deadline<=CURRENT_TIMESTAMP+INTERVAL '1 hour' ORDER BY deadline;"""); return cur.fetchall()
    return await run(op)


async def mark_teacher_exam_deadline_reminder(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_tasks SET teacher_deadline_reminder_sent=TRUE WHERE id=%s;",(task_id,)); conn.commit()
    await run(op)


async def teacher_change_exam_deadline(task_id,hours=0,close_now=False):
    def op():
        with connect() as conn, conn.cursor() as cur:
            if close_now:
                cur.execute("UPDATE biology_tasks SET deadline=CURRENT_TIMESTAMP WHERE id=%s RETURNING *;",(task_id,))
            else:
                cur.execute("UPDATE biology_tasks SET deadline=GREATEST(deadline,CURRENT_TIMESTAMP)+(%s || ' hours')::INTERVAL,teacher_deadline_reminder_sent=FALSE WHERE id=%s RETURNING *;",(hours,task_id))
            row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def latest_daily_exam():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE kind='exam' AND title NOT LIKE '[تراكمي]%%'
            ORDER BY created_at DESC,id DESC LIMIT 1;"""); return cur.fetchone()
    return await run(op)


async def reopen_latest_daily_exam(hours):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE kind='exam' AND title NOT LIKE '[تراكمي]%%'
            ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE;"""); task=cur.fetchone()
            if not task: return None
            cur.execute("""UPDATE biology_tasks SET closed=FALSE,warned=FALSE,six_hour_reminder_sent=FALSE,
            teacher_deadline_reminder_sent=FALSE,deadline=CURRENT_TIMESTAMP+(%s || ' hours')::INTERVAL
            WHERE id=%s RETURNING *;""",(hours,task["id"])); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def due_exam_reminders():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE closed=FALSE AND six_hour_reminder_sent=FALSE
            AND deadline>CURRENT_TIMESTAMP AND deadline<=CURRENT_TIMESTAMP+INTERVAL '6 hours' ORDER BY deadline;"""); return cur.fetchall()
    return await run(op)


async def students_pending_task(task_id):
    """Approved roster members who have not submitted this task yet."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.* FROM biology_task_students roster
                JOIN biology_students s ON s.user_id=roster.user_id
                JOIN biology_tasks t ON t.id=roster.task_id
                WHERE roster.task_id=%s AND s.approved=TRUE AND t.optional_practice=FALSE
                AND NOT EXISTS (
                    SELECT 1 FROM biology_submissions sub
                    WHERE sub.task_id=roster.task_id AND sub.user_id=roster.user_id
                    AND sub.submitted_at IS NOT NULL
                ) ORDER BY s.user_id;""",(task_id,))
            return cur.fetchall()
    return await run(op)


async def mark_exam_reminder_sent(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_tasks SET six_hour_reminder_sent=TRUE WHERE id=%s;",(task_id,)); conn.commit()
    await run(op)


async def approved_students():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE approved=TRUE ORDER BY user_id;"); return cur.fetchall()
    return await run(op)


async def exam_champions_if_ready(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT kind,closed,champion_announced,title FROM biology_tasks WHERE id=%s FOR UPDATE;",(task_id,)); task=cur.fetchone()
            if not task or task["kind"]!="exam" or not task["closed"] or task["champion_announced"]: return []
            cur.execute("SELECT COUNT(*) total,COUNT(grade) graded FROM biology_submissions WHERE task_id=%s;",(task_id,)); counts=cur.fetchone()
            if not counts["total"] or counts["total"]!=counts["graded"]: return []
            cur.execute("""SELECT s.user_id,s.full_name,s.parent_chat_id,sub.grade,%s::TEXT title FROM biology_submissions sub
            JOIN biology_students s ON s.user_id=sub.user_id WHERE sub.task_id=%s AND sub.grade=(SELECT MAX(grade) FROM biology_submissions WHERE task_id=%s);""",(task["title"],task_id,task_id)); rows=cur.fetchall()
            return rows
    return await run(op)


async def mark_champion_announced(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_tasks SET champion_announced=TRUE WHERE id=%s AND champion_announced=FALSE;",(task_id,))
            changed=cur.rowcount; conn.commit(); return changed==1
    return await run(op)


async def missing_students(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.* FROM biology_task_students roster
            JOIN biology_students s ON s.user_id=roster.user_id
            JOIN biology_tasks t ON t.id=roster.task_id
            WHERE roster.task_id=%s AND s.approved=TRUE AND t.optional_practice=FALSE AND NOT EXISTS
            (SELECT 1 FROM biology_submissions x WHERE x.task_id=%s AND x.user_id=s.user_id AND x.submitted_at IS NOT NULL)
            AND NOT EXISTS (SELECT 1 FROM biology_warning_log w WHERE w.task_id=%s AND w.user_id=s.user_id)
            AND NOT EXISTS (SELECT 1 FROM biology_leave_requests lr WHERE lr.user_id=s.user_id AND lr.leave_date=t.deadline::date AND lr.status='approved')
            AND NOT EXISTS (SELECT 1 FROM biology_task_extensions e WHERE e.task_id=%s AND e.user_id=s.user_id AND e.extended_until>CURRENT_TIMESTAMP);""",
            (task_id,task_id,task_id,task_id)); return cur.fetchall()
    return await run(op)


async def task_has_active_extensions(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM biology_task_extensions WHERE task_id=%s AND extended_until>CURRENT_TIMESTAMP) AS active;",(task_id,)); return cur.fetchone()["active"]
    return await run(op)


async def close_task(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_tasks SET closed=TRUE,warned=TRUE WHERE id=%s AND closed=FALSE;",(task_id,)); changed=cur.rowcount; conn.commit(); return changed==1
    return await run(op)


async def mark_questions_released(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_tasks SET questions_released=TRUE WHERE id=%s;",(task_id,)); conn.commit()
    await run(op)


async def add_warning(user_id,reason,issued_by=0,task_id=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT warnings FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); student=cur.fetchone()
            if not student: return 0
            if task_id:
                cur.execute("SELECT 1 FROM biology_warning_log WHERE user_id=%s AND task_id=%s;",(user_id,task_id))
                if cur.fetchone():
                    return student["warnings"]
            cur.execute("INSERT INTO biology_warning_log(user_id,task_id,reason,issued_by) VALUES(%s,%s,%s,%s);",(user_id,task_id,reason,issued_by))
            cur.execute("UPDATE biology_students SET warnings=warnings+1 WHERE user_id=%s RETURNING warnings;",(user_id,)); row=cur.fetchone(); conn.commit(); return row["warnings"] if row else 0
    return await run(op)


async def add_warning_once(user_id,reason,issued_by=0,task_id=None):
    """Atomically add one task warning and report whether this call created it."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT warnings,approved,reset_pending FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); student=cur.fetchone()
            if not student: return {"count":0,"created":False}
            if task_id and int(issued_by or 0)==0:
                if not student['approved'] or student.get('reset_pending'): return {"count":student['warnings'],"created":False}
                # Recheck under the same student lock used by record_submission.
                cur.execute("""SELECT t.id FROM biology_tasks t
                    JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                    LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                    LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=%s
                    WHERE t.id=%s AND t.optional_practice=FALSE
                    AND (d.id IS NULL OR d.deleted_at IS NULL)
                    AND GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline))<=clock_timestamp()
                    AND NOT EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL)
                    AND NOT EXISTS(SELECT 1 FROM biology_exam_warning_waivers w WHERE w.task_id=t.id AND w.user_id=%s)
                    AND NOT EXISTS(SELECT 1 FROM biology_leave_requests lr WHERE lr.user_id=%s AND lr.leave_date=t.deadline::date AND lr.status='approved')
                    FOR UPDATE OF t;""",(user_id,user_id,task_id,user_id,user_id,user_id))
                if not cur.fetchone(): return {"count":student['warnings'],"created":False}
            if task_id:
                cur.execute("SELECT 1 FROM biology_warning_log WHERE user_id=%s AND task_id=%s;",(user_id,task_id))
                if cur.fetchone(): return {"count":student["warnings"],"created":False}
            cur.execute("INSERT INTO biology_warning_log(user_id,task_id,reason,issued_by) VALUES(%s,%s,%s,%s);",(user_id,task_id,reason,issued_by))
            cur.execute("UPDATE biology_students SET warnings=warnings+1 WHERE user_id=%s RETURNING warnings;",(user_id,)); row=cur.fetchone()
            conn.commit(); return {"count":row["warnings"] if row else 0,"created":True}
    return await run(op)


async def remove_warning(user_id,removed_by,warning_id=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            selected_warning_id=warning_id
            if selected_warning_id is None:
                cur.execute("SELECT id FROM biology_warning_log WHERE user_id=%s ORDER BY created_at DESC LIMIT 1;",(user_id,)); target=cur.fetchone(); selected_warning_id=target["id"] if target else None
            if selected_warning_id is None: return None
            cur.execute("DELETE FROM biology_warning_log WHERE id=%s AND user_id=%s RETURNING id;",(selected_warning_id,user_id)); deleted=cur.fetchone()
            if not deleted: return None
            cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-1) WHERE user_id=%s RETURNING warnings;",(user_id,)); row=cur.fetchone()
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'remove_warning',%s);",(removed_by,f"{user_id}:{selected_warning_id}")); conn.commit(); return row["warnings"] if row else 0
    return await run(op)


async def set_cumulative_exam(exam_at,syllabus,updated_by):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_cumulative_exam(singleton,exam_at,syllabus,updated_by) VALUES(TRUE,%s,%s,%s)
            ON CONFLICT(singleton) DO UPDATE SET exam_at=EXCLUDED.exam_at,syllabus=EXCLUDED.syllabus,updated_by=EXCLUDED.updated_by,updated_at=CURRENT_TIMESTAMP;""",(exam_at,syllabus,updated_by)); conn.commit()
    await run(op)


async def get_cumulative_exam():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_cumulative_exam WHERE singleton=TRUE;"); return cur.fetchone()
    return await run(op)

# ========================= v28 ACADEMIC ENGINE =========================
async def v28_notification(user_id, kind, title, body, priority="normal", actor_id=None, entity_type=None, entity_id=None, dedupe_key=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_notifications(user_id,actor_id,kind,title,body,priority,entity_type,entity_id,dedupe_key)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(dedupe_key) DO NOTHING RETURNING *;""",
            (user_id,actor_id,kind,title,body,priority,entity_type,entity_id,dedupe_key))
            row=cur.fetchone(); conn.commit(); return row
    return await run(op)

async def v28_unread_notifications(user_id, limit=20):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_notifications WHERE user_id=%s AND read_at IS NULL ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,created_at DESC LIMIT %s;",(user_id,max(1,min(50,limit))))
            return cur.fetchall()
    return await run(op)

async def v28_mark_notifications_read(user_id, notification_ids=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            if notification_ids:
                cur.execute("UPDATE biology_notifications SET read_at=CURRENT_TIMESTAMP WHERE user_id=%s AND id=ANY(%s);",(user_id,list(map(int,notification_ids))))
            else:
                cur.execute("UPDATE biology_notifications SET read_at=CURRENT_TIMESTAMP WHERE user_id=%s AND read_at IS NULL;",(user_id,))
            n=cur.rowcount; conn.commit(); return n
    return await run(op)

async def v28_exam_definitions_for_student(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.* FROM biology_linked_exam_definitions d
            JOIN biology_students s ON s.user_id=%s
            WHERE s.approved=TRUE AND ((d.target_scope='course' AND s.study_track='course') OR
            (d.target_scope='chapter' AND s.study_track='chapter' AND s.current_chapter=d.chapter)) ORDER BY d.id DESC;""",(user_id,))
            return cur.fetchall()
    return await run(op)

async def v28_linked_lecture_list(cur, definition_id, user_id):
    cur.execute("SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s ORDER BY position,chapter,lecture;",(definition_id,)); rows=cur.fetchall(); seen={(r['chapter'],r['lecture']) for r in rows}
    cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position;",(definition_id,)); pairs=cur.fetchall()
    for p in pairs:
        cur.execute("SELECT lectures FROM biology_personal_preparations WHERE user_id=%s AND chapter=%s AND prep_no=%s ORDER BY target_date DESC LIMIT 1;",(user_id,p['chapter'],p['prep_no'])); x=cur.fetchone()
        if not x:
            cur.execute("SELECT lectures FROM biology_preparations WHERE chapter=%s AND chapter_prep_no=%s ORDER BY target_date DESC LIMIT 1;",(p['chapter'],p['prep_no'])); x=cur.fetchone()
        if x:
            for raw in (x['lectures'] or '').split(','):
                if raw.strip().isdigit():
                    k=(p['chapter'],int(raw));
                    if k not in seen: rows.append({'chapter':k[0],'lecture':k[1]}); seen.add(k)
    return rows

async def v28_course_exam_release_at(definition_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position LIMIT 1;",(definition_id,)); p=cur.fetchone()
            if not p: return None
            cur.execute("SELECT target_date FROM biology_preparations WHERE chapter=%s AND chapter_prep_no=%s ORDER BY target_date DESC LIMIT 1;",(p['chapter'],p['prep_no'])); r=cur.fetchone()
            if not r: return None
            d=r['target_date']+timedelta(days=1)
            cur.execute("SELECT (%s::date + INTERVAL '18 hours') AT TIME ZONE 'Asia/Baghdad' AS release_at;",(d,)); return cur.fetchone()['release_at']
    return await run(op)

async def v28_create_or_get_exam_task(definition_id,user_id,available_at=None,approval_required=False):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE id=%s;",(definition_id,)); d=cur.fetchone()
            if not d: return None
            cur.execute("SELECT * FROM biology_tasks WHERE kind='exam' AND exam_definition_id=%s AND target_scope=%s ORDER BY id DESC LIMIT 1;",(definition_id,f'student:{user_id}')); existing=cur.fetchone()
            if existing: return existing
            lectures=[]
            cur.execute("SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s ORDER BY position;",(definition_id,)); lectures=cur.fetchall()
            linked=','.join(f"ف{x['chapter']}/م{x['lecture']}" for x in lectures)
            placeholder=(available_at or datetime_now(cur)+timedelta(days=3650))
            active=not approval_required and available_at is not None and available_at<=datetime_now(cur)
            exam_hours=max(1,min(168,int(d.get('duration_hours') or 2)))
            deadline=(available_at+timedelta(hours=exam_hours)) if active else placeholder
            ptype='text'; fid=None
            cur.execute("SELECT payload_type,file_id FROM biology_linked_exam_media WHERE definition_id=%s ORDER BY position,id LIMIT 1;",(definition_id,)); m=cur.fetchone()
            if m: ptype,fid=m['payload_type'],m['file_id']
            synthetic=-(800000000000000000+(int(definition_id)*1000000000000+int(user_id))%100000000000000000)
            cur.execute("""INSERT INTO biology_tasks(kind,title,chat_id,thread_id,source_message_id,payload_type,file_id,text_content,deadline,xp_reward,created_by,target_scope,linked_lectures,exam_pending_activation,exam_definition_id,exam_duration_hours,exam_available_at,exam_approval_required,closed)
            VALUES('exam',%s,%s,0,%s,%s,%s,%s,%s,20,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exam_definition_id,target_scope) WHERE exam_definition_id IS NOT NULL AND target_scope LIKE 'student:%%' DO UPDATE SET exam_available_at=EXCLUDED.exam_available_at,exam_approval_required=EXCLUDED.exam_approval_required RETURNING *;""",
            (d['title'],OWNER_CHAT_ID or d['created_by'],synthetic,ptype,fid,d['title'],deadline,d['created_by'],f'student:{user_id}',linked,not active,definition_id,exam_hours,available_at,approval_required,not active))
            row=cur.fetchone()
            cur.execute("INSERT INTO biology_task_students(task_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING;",(row['id'],user_id))
            cur.execute("SELECT * FROM biology_linked_exam_media WHERE definition_id=%s ORDER BY position,id;",(definition_id,))
            for pos,m in enumerate(cur.fetchall()): cur.execute("INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;",(row['id'],m['payload_type'],m['file_id'],synthetic-pos))
            conn.commit(); return row
    return await run(op)

async def v28_activate_due_course_exams():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.id FROM biology_tasks t WHERE t.kind='exam' AND t.exam_pending_activation=TRUE
            AND t.exam_approval_required=FALSE AND t.exam_available_at IS NOT NULL AND t.exam_available_at<=CURRENT_TIMESTAMP AND t.closed=TRUE;""")
            ids=[r['id'] for r in cur.fetchall()]
            changed=[]
            for tid in ids:
                cur.execute("UPDATE biology_tasks SET exam_pending_activation=FALSE,closed=FALSE,deadline=exam_available_at+(exam_duration_hours||' hours')::INTERVAL,published_at=COALESCE(published_at,exam_available_at) WHERE id=%s RETURNING *;",(tid,)); r=cur.fetchone()
                if r: changed.append(r)
            conn.commit(); return changed
    return await run(op)

async def v28_ready_personal_exams():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.id definition_id,s.user_id FROM biology_linked_exam_definitions d JOIN biology_students s ON
            s.approved=TRUE AND d.target_scope='chapter' AND s.study_track='chapter' AND s.current_chapter=d.chapter
            WHERE NOT EXISTS(SELECT 1 FROM biology_tasks t WHERE t.exam_definition_id=d.id AND t.target_scope='student:'||s.user_id);""")
            candidates=cur.fetchall(); ready=[]
            for c in candidates:
                cur.execute("SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s ORDER BY position;",(c['definition_id'],)); lectures=cur.fetchall()
                cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position;",(c['definition_id'],)); pairs=cur.fetchall()
                for p in pairs:
                    cur.execute("SELECT lectures FROM biology_personal_preparations WHERE user_id=%s AND chapter=%s AND prep_no=%s ORDER BY target_date DESC LIMIT 1;",(c['user_id'],p['chapter'],p['prep_no'])); x=cur.fetchone()
                    if x:
                        for raw in x['lectures'].split(','):
                            if raw.strip().isdigit(): lectures.append({'chapter':p['chapter'],'lecture':int(raw)})
                lectures={(x['chapter'],x['lecture']) for x in lectures}
                if lectures:
                    cur.execute("SELECT COUNT(*) n FROM biology_lecture_progress WHERE user_id=%s AND completed_at IS NOT NULL AND (chapter,lecture) IN (%s);" % ','.join(['(%s,%s)']*len(lectures)), [c['user_id']]+[v for pair in lectures for v in pair])
                    if cur.fetchone()['n']==len(lectures): ready.append(c)
            return ready
    return await run(op)

async def v28_extend_exam(task_id, hours):
    def op():
        with connect() as conn, conn.cursor() as cur:
            h=max(1,min(168,int(hours))); cur.execute("UPDATE biology_tasks SET deadline=GREATEST(deadline,CURRENT_TIMESTAMP)+(%s||' hours')::interval,exam_extension_hours=exam_extension_hours+%s,teacher_deadline_reminder_sent=FALSE WHERE id=%s AND kind='exam' RETURNING *;",(h,h,task_id)); r=cur.fetchone(); conn.commit(); return r
    return await run(op)

async def v28_record_activity(user_id, reason, xp=0):
    def op():
        with connect() as conn, conn.cursor() as cur:
            today=datetime_now(cur).date()
            cur.execute("SELECT * FROM biology_gamification_daily WHERE user_id=%s FOR UPDATE;",(user_id,)); row=cur.fetchone()
            if not row:
                cur.execute("INSERT INTO biology_gamification_daily(user_id,current_streak,best_streak,last_activity_date) VALUES(%s,1,1,%s) RETURNING *;",(user_id,today)); row=cur.fetchone()
            elif row['last_activity_date']==today: pass
            elif row['last_activity_date']==today-timedelta(days=1):
                streak=row['current_streak']+1; cur.execute("UPDATE biology_gamification_daily SET current_streak=%s,best_streak=GREATEST(best_streak,%s),last_activity_date=%s,updated_at=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;",(streak,streak,today,user_id)); row=cur.fetchone()
            else:
                cur.execute("UPDATE biology_gamification_daily SET current_streak=1,last_activity_date=%s,updated_at=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;",(today,user_id)); row=cur.fetchone()
            conn.commit(); return row
    result=await run(op)
    if xp:
        await adjust_xp(user_id,xp,reason)
    return result

async def v28_dashboard(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s;",(user_id,)); s=cur.fetchone()
            cur.execute("SELECT COUNT(*) n FROM biology_lecture_progress WHERE user_id=%s AND completed_at IS NOT NULL;",(user_id,)); lectures=cur.fetchone()['n']
            cur.execute("SELECT COUNT(*) n FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id WHERE ts.user_id=%s AND t.kind='homework' AND EXISTS(SELECT 1 FROM biology_submissions x WHERE x.task_id=t.id AND x.user_id=%s);",(user_id,user_id)); hw=cur.fetchone()['n']
            cur.execute("SELECT COUNT(*) n,AVG(sub.grade) avg FROM biology_submissions sub JOIN biology_tasks t ON t.id=sub.task_id WHERE sub.user_id=%s AND t.kind='exam' AND sub.grade IS NOT NULL;",(user_id,)); ex=cur.fetchone()
            cur.execute("SELECT current_streak,best_streak FROM biology_gamification_daily WHERE user_id=%s;",(user_id,)); st=cur.fetchone() or {'current_streak':0,'best_streak':0}
            return {'student':s,'lectures':lectures,'homeworks':hw,'exams':ex['n'],'average':float(ex['avg']) if ex['avg'] is not None else None,'streak':st['current_streak'],'best_streak':st['best_streak']}
    return await run(op)

async def v28_student_exam_tasks(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.* FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
            WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s AND (t.closed=FALSE OR t.exam_pending_activation=TRUE)
            ORDER BY COALESCE(t.exam_available_at,t.deadline),t.id DESC;""",(user_id,)); return cur.fetchall()
    return await run(op)


async def v41_set_study_days(user_id,days,_repair=False):
    """Change weekdays for the current chapter while preserving later chapter rules."""
    days=sorted({int(day) for day in days})
    def default_days(chapter):
        return {6,0,1,2,3} if chapter==1 else ({6,0,1,3} if chapter==2 else {6,1,3})
    def op():
        from datetime import date
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(int(user_id),)); student=cur.fetchone()
            if not student: return {"status":"missing"}
            if student.get("study_track")!="chapter": return {"status":"course"}
            current=int(student.get("current_chapter") or 1); required=5 if current==1 else 4 if current==2 else 3
            if len(days)!=required or any(day<0 or day>6 for day in days): return {"status":"count","required":required}
            cur.execute("SELECT * FROM biology_personal_preparations WHERE user_id=%s ORDER BY chapter,prep_no,target_date,id FOR UPDATE;",(int(user_id),)); rows=cur.fetchall(); pending=[]
            for row in rows:
                lectures=sorted({int(x) for x in str(row.get("lectures") or "").split(',') if x.strip().isdigit()})
                if not lectures: continue
                cur.execute("""SELECT COUNT(*) AS n FROM biology_lecture_progress
                    WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s) AND completed_at IS NOT NULL;""",(int(user_id),row["chapter"],lectures))
                if int(cur.fetchone()["n"])<len(lectures): pending.append(row)
            pending_ids=[int(row["id"]) for row in pending]; pending_set=set(pending_ids)
            occupied={row["target_date"] for row in rows if int(row["id"]) not in pending_set}
            if pending_ids:
                cur.execute("UPDATE biology_personal_preparations SET target_date=target_date+10000 WHERE id=ANY(%s);",(pending_ids,))
                cursor=date.today()-timedelta(days=1)
                for row in pending:
                    allowed=set(days) if int(row["chapter"])==current else default_days(int(row["chapter"]))
                    while True:
                        cursor+=timedelta(days=1)
                        if cursor.weekday() in allowed and cursor not in occupied: break
                    cur.execute("UPDATE biology_personal_preparations SET target_date=%s,notified=FALSE,notified_at=NULL WHERE id=%s;",(cursor,row["id"])); occupied.add(cursor)
            if _repair:
                cur.execute("UPDATE biology_students SET study_days=%s WHERE user_id=%s RETURNING *;",(days,int(user_id)))
            else:
                cur.execute("""UPDATE biology_students SET schedule_mode='custom',study_days=%s,
                    schedule_change_count=COALESCE(schedule_change_count,0)+1 WHERE user_id=%s RETURNING *;""",(days,int(user_id)))
            updated=cur.fetchone()
            conn.commit(); return {"status":"ok","required":required,"student":updated,"pending":len(pending)}
    return await run(op)


_v42_reliable_set_study_days=v41_set_study_days


async def v42_repair_chapter_schedules():
    """Repair finish dates previously calculated with one weekday count for all chapters."""
    def students_op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT user_id,current_chapter,study_days FROM biology_students WHERE approved=TRUE AND study_track='chapter';"); return cur.fetchall()
    repaired=0
    for student in await run(students_op):
        chapter=int(student.get("current_chapter") or 1); required=5 if chapter==1 else 4 if chapter==2 else 3
        selected=sorted({int(x) for x in (student.get("study_days") or []) if 0<=int(x)<=6})
        if len(selected)!=required: selected=sorted({6,0,1,2,3} if chapter==1 else ({6,0,1,3} if chapter==2 else {6,1,3}))
        result=await v41_set_study_days(student["user_id"],selected,True)
        repaired+=1 if result.get("status")=="ok" else 0
    return repaired


async def v37_chapter_completion_plan(user_id):
    """Use the actual track as the only source of truth for finish dates."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track,current_chapter FROM biology_students WHERE user_id=%s;",(int(user_id),)); student=cur.fetchone()
            if not student: return {"chapters":[],"full_finish":None}
            if student.get("study_track")=="course":
                cur.execute("""SELECT chapter,MAX(target_date) AS finish_date,COUNT(*) AS prep_count
                    FROM biology_preparations GROUP BY chapter ORDER BY chapter;""")
            else:
                cur.execute("""SELECT chapter,MAX(target_date) AS finish_date,COUNT(*) AS prep_count
                    FROM biology_personal_preparations WHERE user_id=%s AND chapter>=%s
                    GROUP BY chapter ORDER BY chapter;""",(int(user_id),int(student.get("current_chapter") or 1)))
            rows=cur.fetchall(); return {"chapters":rows,"full_finish":max((row["finish_date"] for row in rows),default=None)}
    return await run(op)


_v42_reliable_completion_plan=v37_chapter_completion_plan

async def v28_student_homeworks(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.* FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
            WHERE t.kind='homework' AND ts.user_id=%s AND t.closed=FALSE ORDER BY t.deadline,t.id DESC;""",(user_id,)); return cur.fetchall()
    return await run(op)

async def v28_create_exam_notice(title, body, target_scope, exam_at, created_by):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO biology_exam_notices(title,body,target_scope,exam_at,created_by) VALUES(%s,%s,%s,%s,%s) RETURNING *;",(title,body,target_scope,exam_at,created_by)); r=cur.fetchone(); conn.commit(); return r
    return await run(op)

async def v28_due_exam_notices():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_exam_notices WHERE sent=FALSE AND exam_at<=CURRENT_TIMESTAMP+INTERVAL '24 hours' ORDER BY exam_at,id;")
            return cur.fetchall()
    return await run(op)

async def v28_mark_exam_notice_sent(notice_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_exam_notices SET sent=TRUE WHERE id=%s AND sent=FALSE;",(notice_id,)); n=cur.rowcount; conn.commit(); return n==1
    return await run(op)

async def v28_has_activity_since(user_id, since):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT EXISTS(SELECT 1 FROM biology_lecture_progress WHERE user_id=%s AND completed_at>= %s)
            OR EXISTS(SELECT 1 FROM biology_submissions WHERE user_id=%s AND submitted_at>= %s);""",(user_id,since,user_id,since)); return cur.fetchone()['exists']
    return await run(op)

# ========================= v29 persistence fixes =========================
async def v29_create_or_get_exam_task(definition_id,user_id,available_at=None,approval_required=False):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE id=%s;", (definition_id,)); d=cur.fetchone()
            if not d: return None
            target=f"student:{user_id}"
            cur.execute("SELECT * FROM biology_tasks WHERE kind='exam' AND exam_definition_id=%s AND target_scope=%s ORDER BY id DESC LIMIT 1;", (definition_id,target)); existing=cur.fetchone()
            if existing: return existing
            lecture_set=set()
            cur.execute("SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s ORDER BY position;", (definition_id,))
            for r in cur.fetchall(): lecture_set.add((int(r['chapter']),int(r['lecture'])))
            cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position;", (definition_id,))
            for p in cur.fetchall():
                # Use the student's actual personal preparation when available; otherwise use catalog.
                cur.execute("SELECT lectures FROM biology_personal_preparations WHERE user_id=%s AND chapter=%s AND prep_no=%s ORDER BY target_date DESC LIMIT 1;", (user_id,p['chapter'],p['prep_no']))
                x=cur.fetchone()
                if not x:
                    cur.execute("SELECT lectures FROM biology_preparations WHERE chapter=%s AND chapter_prep_no=%s ORDER BY target_date DESC LIMIT 1;", (p['chapter'],p['prep_no']))
                    x=cur.fetchone()
                if x:
                    for raw in (x['lectures'] or '').split(','):
                        if raw.strip().isdigit(): lecture_set.add((int(p['chapter']),int(raw)))
            linked=','.join(f"ف{c}/م{l}" for c,l in sorted(lecture_set))
            cur.execute("SELECT payload_type,file_id FROM biology_linked_exam_media WHERE definition_id=%s ORDER BY position,id LIMIT 1;", (definition_id,)); m=cur.fetchone()
            ptype=m['payload_type'] if m else 'text'; fid=m['file_id'] if m else None
            now=datetime_now(cur)
            active=(not approval_required and available_at is not None and available_at<=now)
            exam_hours=max(1,min(168,int(d.get('duration_hours') or 2)))
            deadline=(available_at+timedelta(hours=exam_hours)) if active else now+timedelta(days=3650)
            synthetic=-(700000000000000000+(int(definition_id)*1000000000000+int(user_id))%100000000000000000)
            cur.execute("""INSERT INTO biology_tasks(kind,title,chat_id,thread_id,source_message_id,payload_type,file_id,text_content,deadline,xp_reward,created_by,target_scope,linked_lectures,exam_pending_activation,exam_definition_id,exam_duration_hours,exam_available_at,exam_approval_required,closed)
                VALUES('exam',%s,%s,0,%s,%s,%s,%s,%s,20,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (exam_definition_id,target_scope) WHERE exam_definition_id IS NOT NULL AND target_scope LIKE 'student:%%' DO NOTHING RETURNING *;""",
                (d['title'], OWNER_CHAT_ID or d['created_by'], synthetic, ptype, fid, d['title'], deadline, d['created_by'], target, linked, not active, definition_id, exam_hours, available_at, approval_required, not active))
            row=cur.fetchone()
            if not row:
                cur.execute("SELECT * FROM biology_tasks WHERE kind='exam' AND exam_definition_id=%s AND target_scope=%s ORDER BY id DESC LIMIT 1;", (definition_id,target)); row=cur.fetchone()
            if not row: return None
            if active:
                cur.execute("UPDATE biology_tasks SET published_at=COALESCE(published_at,%s) WHERE id=%s RETURNING *;", (available_at,row['id']))
                row=cur.fetchone()
            cur.execute("INSERT INTO biology_task_students(task_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING;", (row['id'],user_id))
            cur.execute("SELECT payload_type,file_id FROM biology_linked_exam_media WHERE definition_id=%s ORDER BY position,id;", (definition_id,))
            for pos,m in enumerate(cur.fetchall()):
                cur.execute("INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;", (row['id'],m['payload_type'],m['file_id'],synthetic-pos))
            conn.commit(); return row
    return await run(op)


# ========================= v54 school-review programme =========================

_V54_SCHOOL_REVIEW_SEED=(
    (1,'الأول','الفصل الأول','المحاضرات 1، 2، 3، 4، 5، 6','2026-09-27','2026-09-27'),
    (2,'الثاني','الفصل الأول','المحاضرات 7، 8، 9','2026-09-27','2026-10-03'),
    (3,'الثالث','الفصل الأول','المحاضرات 6، 7، 8، 9، 10، 11، 12','2026-10-03','2026-10-10'),
    (4,'الرابع','الفصل الأول','المحاضرات 9، 10، 11، 12، 13، 14، 15','2026-10-10','2026-10-17'),
    (5,'الخامس','الفصل الأول','الفصل الأول كاملاً','2026-10-17','2026-10-24'),
)


_v54_previous_init_db=init_db
def init_db():
    """Install the selective school-review programme and global exam race rewards."""
    _v54_previous_init_db()
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS biology_school_reviews(
            id SERIAL PRIMARY KEY,
            week_no INTEGER NOT NULL UNIQUE CHECK(week_no BETWEEN 1 AND 60),
            week_label TEXT NOT NULL,
            chapter_label TEXT NOT NULL,
            topics TEXT NOT NULL,
            publish_date DATE NOT NULL,
            exam_date DATE NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            published_at TIMESTAMPTZ,
            group_message_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(publish_date<=exam_date));
        CREATE TABLE IF NOT EXISTS biology_school_review_students(
            user_id BIGINT PRIMARY KEY REFERENCES biology_students(user_id) ON DELETE CASCADE,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            enabled_by BIGINT NOT NULL,
            enabled_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            disabled_at TIMESTAMPTZ);
        CREATE TABLE IF NOT EXISTS biology_school_review_progress(
            review_id INTEGER NOT NULL REFERENCES biology_school_reviews(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            completed_at TIMESTAMPTZ,
            xp_awarded BOOLEAN NOT NULL DEFAULT FALSE,
            task_id INTEGER REFERENCES biology_tasks(id) ON DELETE SET NULL,
            approval_notified_at TIMESTAMPTZ,
            PRIMARY KEY(review_id,user_id));
        CREATE TABLE IF NOT EXISTS biology_school_review_exam_media(
            id BIGSERIAL PRIMARY KEY,
            review_id INTEGER NOT NULL REFERENCES biology_school_reviews(id) ON DELETE CASCADE,
            payload_type TEXT NOT NULL CHECK(payload_type IN ('text','photo','document','video')),
            file_id TEXT,
            text_content TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0,
            created_by BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(review_id,position));
        CREATE TABLE IF NOT EXISTS biology_school_review_notifications(
            review_id INTEGER NOT NULL REFERENCES biology_school_reviews(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            kind TEXT NOT NULL,
            sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(review_id,user_id,kind));
        CREATE TABLE IF NOT EXISTS biology_exam_speed_rewards(
            task_id INTEGER NOT NULL REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            race_key TEXT NOT NULL,
            submission_rank INTEGER NOT NULL CHECK(submission_rank BETWEEN 1 AND 3),
            bonus_xp INTEGER NOT NULL CHECK(bonus_xp IN (3,5,10)),
            awarded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(task_id,user_id),
            UNIQUE(race_key,submission_rank));
        ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS school_review_id INTEGER
            REFERENCES biology_school_reviews(id) ON DELETE SET NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS biology_school_review_student_task_unique
            ON biology_tasks(school_review_id,target_scope)
            WHERE school_review_id IS NOT NULL AND target_scope LIKE 'student:%';
        CREATE INDEX IF NOT EXISTS biology_school_review_due_idx
            ON biology_school_reviews(active,publish_date,exam_date,published_at);
        CREATE INDEX IF NOT EXISTS biology_school_review_student_active_idx
            ON biology_school_review_students(active,user_id);
        """)
        for row in _V54_SCHOOL_REVIEW_SEED:
            cur.execute("""INSERT INTO biology_school_reviews
                (week_no,week_label,chapter_label,topics,publish_date,exam_date)
                VALUES(%s,%s,%s,%s,%s::DATE,%s::DATE)
                ON CONFLICT(week_no) DO NOTHING;""",row)
        conn.commit()


async def v54_school_review_access(user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT e.*,s.full_name,s.parent_chat_id,s.study_track
                FROM biology_school_review_students e
                JOIN biology_students s ON s.user_id=e.user_id
                WHERE e.user_id=%s AND e.active=TRUE AND s.approved=TRUE
                  AND s.reset_pending=FALSE AND s.study_track='course';""",(int(user_id),))
            return cur.fetchone()
    return await run(op)


async def v54_school_review_admin_students(limit=200,offset=0):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT s.user_id,s.full_name,s.school,s.parent_chat_id,
                    COALESCE(e.active,FALSE) AS school_review_enabled,e.enabled_at
                FROM biology_students s
                LEFT JOIN biology_school_review_students e ON e.user_id=s.user_id
                WHERE s.approved=TRUE AND s.reset_pending=FALSE AND s.study_track='course'
                ORDER BY COALESCE(e.active,FALSE) DESC,s.full_name,s.user_id
                LIMIT %s OFFSET %s;""",(max(1,min(500,int(limit))),max(0,int(offset))))
            return cur.fetchall()
    return await run(op)


async def v54_toggle_school_review_student(user_id,enabled_by):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT user_id,study_track,approved,reset_pending
                FROM biology_students WHERE user_id=%s FOR UPDATE;""",(int(user_id),))
            student=cur.fetchone()
            if not student or student['study_track']!='course' or not student['approved'] or student.get('reset_pending'):
                return {'status':'not_course'}
            cur.execute("SELECT * FROM biology_school_review_students WHERE user_id=%s FOR UPDATE;",(int(user_id),))
            current=cur.fetchone(); enabled=not bool(current and current['active'])
            cur.execute("""INSERT INTO biology_school_review_students(user_id,active,enabled_by,enabled_at,disabled_at)
                VALUES(%s,%s,%s,CURRENT_TIMESTAMP,CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP END)
                ON CONFLICT(user_id) DO UPDATE SET active=EXCLUDED.active,enabled_by=EXCLUDED.enabled_by,
                    enabled_at=CASE WHEN EXCLUDED.active THEN CURRENT_TIMESTAMP ELSE biology_school_review_students.enabled_at END,
                    disabled_at=CASE WHEN EXCLUDED.active THEN NULL ELSE CURRENT_TIMESTAMP END
                RETURNING *;""",(int(user_id),enabled,int(enabled_by),enabled))
            row=cur.fetchone()
            cur.execute("""UPDATE biology_tasks t SET closed=%s,
                    exam_pending_activation=CASE WHEN %s THEN TRUE ELSE t.exam_pending_activation END
                WHERE t.school_review_id IS NOT NULL AND t.target_scope=%s
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub
                    WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL);""",
                (not enabled,enabled,f'student:{int(user_id)}',int(user_id)))
            cur.execute("""UPDATE biology_exam_access a SET status=%s,approved_by=NULL,approved_at=NULL
                FROM biology_tasks t WHERE t.id=a.task_id AND t.school_review_id IS NOT NULL
                  AND a.user_id=%s;""",('pending' if enabled else 'denied',int(user_id)))
            if enabled:
                cur.execute("UPDATE biology_school_review_progress SET approval_notified_at=NULL WHERE user_id=%s;",
                    (int(user_id),))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);",
                (int(enabled_by),'school_review_enabled' if enabled else 'school_review_disabled',f'user={int(user_id)}'))
            conn.commit(); return {'status':'ok','enabled':enabled,'row':row}
    return await run(op)


async def v54_school_review_catalog(user_id=None):
    def op():
        with connect() as conn,conn.cursor() as cur:
            params=[]; join=''; columns=''
            if user_id is not None:
                columns=",p.completed_at,p.xp_awarded,p.task_id,t.exam_pending_activation,t.deadline,t.closed,sub.submitted_at,access.status AS approval_status"
                join="""LEFT JOIN biology_school_review_progress p ON p.review_id=r.id AND p.user_id=%s
                    LEFT JOIN biology_tasks t ON t.id=p.task_id
                    LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=%s
                    LEFT JOIN biology_exam_access access ON access.task_id=t.id AND access.user_id=%s"""
                params=[int(user_id),int(user_id),int(user_id)]
            cur.execute(f"""SELECT r.*,
                    (SELECT COUNT(*)::INTEGER FROM biology_school_review_exam_media m WHERE m.review_id=r.id) AS media_count
                    {columns}
                FROM biology_school_reviews r {join}
                WHERE r.active=TRUE ORDER BY r.week_no;""",params)
            return cur.fetchall()
    return await run(op)


async def v54_add_school_review(week_no,chapter_label,topics,exam_date,created_by):
    """Add one future review week from the admin panel.

    Publication is exactly seven days before the exam.  An existing week is
    never silently overwritten, which protects published schedules.
    """
    labels={
        1:'الأول',2:'الثاني',3:'الثالث',4:'الرابع',5:'الخامس',6:'السادس',
        7:'السابع',8:'الثامن',9:'التاسع',10:'العاشر',11:'الحادي عشر',
        12:'الثاني عشر',13:'الثالث عشر',14:'الرابع عشر',15:'الخامس عشر',
        16:'السادس عشر',17:'السابع عشر',18:'الثامن عشر',19:'التاسع عشر',20:'العشرون',
    }
    def op():
        number=int(week_no)
        if number<1 or number>60: return {'status':'invalid_week'}
        exam=exam_date if hasattr(exam_date,'year') else date.fromisoformat(str(exam_date))
        chapter=str(chapter_label or '').strip(); subject=str(topics or '').strip()
        if not chapter or not subject: return {'status':'invalid_text'}
        with connect() as conn,conn.cursor() as cur:
            cur.execute('SELECT id FROM biology_school_reviews WHERE week_no=%s;',(number,))
            if cur.fetchone(): return {'status':'exists'}
            cur.execute("""INSERT INTO biology_school_reviews
                (week_no,week_label,chapter_label,topics,publish_date,exam_date)
                VALUES(%s,%s,%s,%s,%s,%s) RETURNING *;""",
                (number,labels.get(number,f'رقم {number}'),chapter,subject,exam-timedelta(days=7),exam))
            row=cur.fetchone()
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'school_review_added',%s);",
                (int(created_by),f'week={number}; exam={exam.isoformat()}'))
            conn.commit(); return {'status':'ok','review':row}
    return await run(op)


async def v54_school_review_by_id(review_id,user_id=None):
    rows=await v54_school_review_catalog(user_id)
    return next((row for row in rows if int(row['id'])==int(review_id)),None)


async def v54_replace_school_review_exam_media(review_id,items,created_by):
    clean=[]
    for item in items:
        payload=str(item.get('payload_type') or '')
        file_id=item.get('file_id'); content=str(item.get('text_content') or '')[:4000]
        if payload not in {'text','photo','document','video'}: continue
        if payload=='text' and not content.strip(): continue
        if payload!='text' and not file_id: continue
        clean.append((payload,file_id,content))
    if not clean: return 0
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT id FROM biology_school_reviews WHERE id=%s AND active=TRUE FOR UPDATE;",(int(review_id),))
            if not cur.fetchone(): return 0
            cur.execute("DELETE FROM biology_school_review_exam_media WHERE review_id=%s;",(int(review_id),))
            for position,(payload,file_id,content) in enumerate(clean):
                cur.execute("""INSERT INTO biology_school_review_exam_media
                    (review_id,payload_type,file_id,text_content,position,created_by)
                    VALUES(%s,%s,%s,%s,%s,%s);""",
                    (int(review_id),payload,file_id,content,position,int(created_by)))
            # Existing student tasks must receive the replacement too; otherwise
            # a late teacher edit would leave old questions visible.
            cur.execute("SELECT id,title FROM biology_tasks WHERE school_review_id=%s FOR UPDATE;",(int(review_id),))
            tasks=cur.fetchall(); first=next((item for item in clean if item[0]!='text'),clean[0])
            combined_text='\n\n'.join(item[2] for item in clean if item[0]=='text' and item[2].strip())
            for task in tasks:
                cur.execute("""UPDATE biology_tasks SET payload_type=%s,file_id=%s,
                    text_content=%s WHERE id=%s;""",
                    (first[0],first[1],combined_text or task['title'],task['id']))
                cur.execute("DELETE FROM biology_task_media WHERE task_id=%s;",(task['id'],))
                for position,(payload,file_id,content) in enumerate(clean):
                    if payload=='text': continue
                    cur.execute("""INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id)
                        VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;""",
                        (task['id'],payload,file_id,-(870000000000000000+int(task['id'])*1000+position)))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'school_review_exam_saved',%s);",
                (int(created_by),f'review={int(review_id)};items={len(clean)}'))
            conn.commit(); return len(clean)
    return await run(op)


async def v54_school_review_exam_media(review_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_school_review_exam_media WHERE review_id=%s ORDER BY position,id;",(int(review_id),))
            return cur.fetchall()
    return await run(op)


def _v54_ensure_school_review_task(cur,review_id,user_id):
    cur.execute("""SELECT r.* FROM biology_school_reviews r
        JOIN biology_school_review_students e ON e.user_id=%s AND e.active=TRUE
        JOIN biology_students s ON s.user_id=e.user_id AND s.approved=TRUE
            AND s.reset_pending=FALSE AND s.study_track='course'
        JOIN biology_school_review_progress p ON p.review_id=r.id AND p.user_id=e.user_id
            AND p.completed_at IS NOT NULL
        WHERE r.id=%s AND r.active=TRUE
          AND r.exam_date<=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
        FOR UPDATE OF r;""",(int(user_id),int(review_id)))
    review=cur.fetchone()
    if not review: return None
    cur.execute("SELECT * FROM biology_school_review_exam_media WHERE review_id=%s ORDER BY position,id;",(int(review_id),))
    media=cur.fetchall()
    if not media: return None
    scope=f'student:{int(user_id)}'
    cur.execute("SELECT * FROM biology_tasks WHERE school_review_id=%s AND target_scope=%s ORDER BY id DESC LIMIT 1;",
        (int(review_id),scope))
    task=cur.fetchone()
    if not task:
        first=next((item for item in media if item['payload_type']!='text'),media[0])
        combined_text='\n\n'.join(item['text_content'] for item in media
            if item['payload_type']=='text' and str(item.get('text_content') or '').strip())
        synthetic=-(880000000000000000+int(review_id)*100000000000+int(user_id)%100000000000)
        title=f"مراجعة المدرسة — الأسبوع {review['week_label']}"
        deadline=datetime_now(cur)+timedelta(days=3650)
        cur.execute("""INSERT INTO biology_tasks
            (kind,title,chat_id,thread_id,source_message_id,payload_type,file_id,text_content,deadline,
             xp_reward,created_by,target_scope,linked_lectures,exam_pending_activation,
             exam_duration_hours,exam_approval_required,closed,optional_practice,school_review_id)
            VALUES('exam',%s,%s,0,%s,%s,%s,%s,%s,20,%s,%s,'',TRUE,24,TRUE,FALSE,TRUE,%s)
            ON CONFLICT (school_review_id,target_scope)
              WHERE school_review_id IS NOT NULL AND target_scope LIKE 'student:%%'
            DO NOTHING RETURNING *;""",
            (title,OWNER_CHAT_ID or 0,synthetic,first['payload_type'],first.get('file_id'),combined_text or title,
             deadline,OWNER_CHAT_ID or 0,scope,int(review_id)))
        task=cur.fetchone()
        if not task:
            cur.execute("SELECT * FROM biology_tasks WHERE school_review_id=%s AND target_scope=%s ORDER BY id DESC LIMIT 1;",
                (int(review_id),scope)); task=cur.fetchone()
        for position,item in enumerate(media):
            if item['payload_type']=='text': continue
            cur.execute("""INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;""",
                (task['id'],item['payload_type'],item['file_id'],synthetic-position))
        cur.execute("INSERT INTO biology_task_students(task_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING;",
            (task['id'],int(user_id)))
        cur.execute("""INSERT INTO biology_exam_access(task_id,user_id,status)
            VALUES(%s,%s,'pending') ON CONFLICT(task_id,user_id) DO NOTHING;""",(task['id'],int(user_id)))
        cur.execute("UPDATE biology_school_review_progress SET task_id=%s WHERE review_id=%s AND user_id=%s;",
            (task['id'],int(review_id),int(user_id)))
    return task


async def v54_complete_school_review(review_id,user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT r.* FROM biology_school_reviews r
                JOIN biology_school_review_students e ON e.user_id=%s AND e.active=TRUE
                JOIN biology_students s ON s.user_id=e.user_id AND s.approved=TRUE
                    AND s.reset_pending=FALSE AND s.study_track='course'
                WHERE r.id=%s AND r.active=TRUE AND r.published_at IS NOT NULL
                  AND r.publish_date<=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                  AND r.exam_date>=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                FOR UPDATE OF r,s;""",(int(user_id),int(review_id)))
            review=cur.fetchone()
            if not review: return {'status':'unavailable'}
            cur.execute("""INSERT INTO biology_school_review_progress(review_id,user_id,completed_at,xp_awarded)
                VALUES(%s,%s,CURRENT_TIMESTAMP,FALSE)
                ON CONFLICT(review_id,user_id) DO NOTHING RETURNING *;""",(int(review_id),int(user_id)))
            inserted=cur.fetchone(); awarded=0
            if inserted:
                awarded=_set_xp_event(cur,int(user_id),30,'إكمال مراجعة المدرسة',f'school_review:{int(review_id)}:{int(user_id)}')
                cur.execute("UPDATE biology_school_review_progress SET xp_awarded=TRUE WHERE review_id=%s AND user_id=%s;",
                    (int(review_id),int(user_id)))
            task=_v54_ensure_school_review_task(cur,int(review_id),int(user_id))
            conn.commit(); return {'status':'ok','new':bool(inserted),'xp':30 if awarded>0 else 0,'review':review,'task':task}
    return await run(op)


async def v54_prepare_school_review_exam(review_id,user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            task=_v54_ensure_school_review_task(cur,int(review_id),int(user_id))
            if not task: return {'status':'waiting'}
            cur.execute("SELECT submitted_at FROM biology_submissions WHERE task_id=%s AND user_id=%s;",(task['id'],int(user_id)))
            submitted=cur.fetchone()
            if submitted and submitted.get('submitted_at'): return {'status':'submitted','task':task}
            cur.execute("SELECT status FROM biology_exam_access WHERE task_id=%s AND user_id=%s;",(task['id'],int(user_id)))
            access=cur.fetchone(); status=(access or {}).get('status') or 'pending'
            cur.execute("SELECT approval_notified_at FROM biology_school_review_progress WHERE review_id=%s AND user_id=%s;",
                (int(review_id),int(user_id)))
            progress=cur.fetchone() or {}
            conn.commit(); return {'status':'open' if status=='approved' and not task['exam_pending_activation'] else 'approval',
                'notify':status=='pending' and not progress.get('approval_notified_at'),'task':task}
    return await run(op)


async def v54_due_school_review_publications(limit=10):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_school_reviews
                WHERE active=TRUE AND published_at IS NULL
                  AND publish_date<=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                ORDER BY week_no LIMIT %s;""",(max(1,min(20,int(limit))),))
            return cur.fetchall()
    return await run(op)


async def v54_mark_school_review_published(review_id,message_id=None):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_school_reviews SET published_at=COALESCE(published_at,CURRENT_TIMESTAMP),
                group_message_id=COALESCE(group_message_id,%s) WHERE id=%s RETURNING *;""",
                (message_id,int(review_id)))
            row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def v54_enabled_school_review_students():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT s.user_id,s.full_name,s.parent_chat_id
                FROM biology_school_review_students e
                JOIN biology_students s ON s.user_id=e.user_id
                WHERE e.active=TRUE AND s.approved=TRUE AND s.reset_pending=FALSE
                  AND s.study_track='course' ORDER BY s.user_id;""")
            return cur.fetchall()
    return await run(op)


async def v54_due_school_review_reminders(limit=100):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""WITH due AS (
                SELECT r.*,s.user_id,s.full_name,s.parent_chat_id,
                    CASE
                      WHEN r.exam_date-(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE<=1 THEN 'reminder_1d'
                      WHEN r.exam_date-(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE<=2 THEN 'reminder_2d'
                      WHEN r.exam_date-(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE<=4 THEN 'reminder_4d'
                    END AS reminder_kind
                FROM biology_school_reviews r
                JOIN biology_school_review_students e ON e.active=TRUE
                JOIN biology_students s ON s.user_id=e.user_id AND s.approved=TRUE
                    AND s.reset_pending=FALSE AND s.study_track='course'
                LEFT JOIN biology_school_review_progress p ON p.review_id=r.id AND p.user_id=s.user_id
                WHERE r.active=TRUE AND r.published_at IS NOT NULL AND p.completed_at IS NULL
                  AND r.exam_date>=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                  AND r.exam_date-(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE<=4)
                SELECT due.* FROM due
                WHERE reminder_kind IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM biology_school_review_notifications n
                    WHERE n.review_id=due.id AND n.user_id=due.user_id AND n.kind=due.reminder_kind)
                ORDER BY exam_date,user_id LIMIT %s;""",(max(1,min(500,int(limit))),))
            return cur.fetchall()
    return await run(op)


async def v54_mark_school_review_notification(review_id,user_id,kind):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_school_review_notifications(review_id,user_id,kind)
                VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *;""",
                (int(review_id),int(user_id),str(kind)))
            row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def v54_due_school_review_exams(limit=100):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT r.id AS review_id,p.user_id,s.full_name,s.parent_chat_id,p.task_id,p.approval_notified_at
                FROM biology_school_review_progress p
                JOIN biology_school_reviews r ON r.id=p.review_id AND r.active=TRUE
                JOIN biology_school_review_students e ON e.user_id=p.user_id AND e.active=TRUE
                JOIN biology_students s ON s.user_id=p.user_id AND s.approved=TRUE
                    AND s.reset_pending=FALSE AND s.study_track='course'
                WHERE p.completed_at IS NOT NULL
                  AND r.exam_date<=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                  AND EXISTS(SELECT 1 FROM biology_school_review_exam_media m WHERE m.review_id=r.id)
                  AND (p.task_id IS NULL OR p.approval_notified_at IS NULL)
                ORDER BY r.week_no,p.user_id LIMIT %s;""",(max(1,min(500,int(limit))),))
            return cur.fetchall()
    return await run(op)


async def v54_mark_school_review_exam_notified(review_id,user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_school_review_progress SET approval_notified_at=CURRENT_TIMESTAMP
                WHERE review_id=%s AND user_id=%s RETURNING *;""",(int(review_id),int(user_id)))
            row=cur.fetchone(); conn.commit(); return row
    return await run(op)


def _v54_award_exam_speed_bonus_sync(task_id,user_id):
    with connect() as conn,conn.cursor() as cur:
        cur.execute("SELECT id,kind,exam_definition_id,school_review_id FROM biology_tasks WHERE id=%s;",(int(task_id),))
        task=cur.fetchone()
        if not task or task['kind']!='exam': return None
        if task.get('exam_definition_id'):
            cur.execute("SELECT id FROM biology_linked_exam_definitions WHERE id=%s FOR UPDATE;",(task['exam_definition_id'],))
            if not cur.fetchone(): return None
            race_key=f"definition:{int(task['exam_definition_id'])}"
        elif task.get('school_review_id'):
            cur.execute("SELECT id FROM biology_school_reviews WHERE id=%s FOR UPDATE;",(task['school_review_id'],))
            if not cur.fetchone(): return None
            race_key=f"school:{int(task['school_review_id'])}"
        else:
            cur.execute("SELECT id FROM biology_tasks WHERE id=%s FOR UPDATE;",(int(task_id),))
            if not cur.fetchone(): return None
            race_key=f"task:{int(task_id)}"
        cur.execute("SELECT * FROM biology_exam_speed_rewards WHERE task_id=%s AND user_id=%s;",(int(task_id),int(user_id)))
        existing=cur.fetchone()
        if existing: return existing
        cur.execute("SELECT COUNT(*)::INTEGER AS n FROM biology_exam_speed_rewards WHERE race_key=%s;",(race_key,))
        rank=int(cur.fetchone()['n'])+1
        bonus={1:10,2:5,3:3}.get(rank)
        if not bonus: return None
        cur.execute("""INSERT INTO biology_exam_speed_rewards(task_id,user_id,race_key,submission_rank,bonus_xp)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *;""",
            (int(task_id),int(user_id),race_key,rank,bonus))
        reward=cur.fetchone()
        if reward:
            _set_xp_event(cur,int(user_id),bonus,f'مكافأة سرعة الامتحان - المركز {rank}',
                f'exam_speed:{int(task_id)}:{int(user_id)}')
        conn.commit(); return reward


async def v54_exam_speed_reward(task_id,user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_exam_speed_rewards WHERE task_id=%s AND user_id=%s;",
                (int(task_id),int(user_id)))
            return cur.fetchone()
    return await run(op)


_v54_previous_record_submission=record_submission
async def record_submission(task_id,user_id,message_id,file_unique_id,media_group_id=None):
    result=await _v54_previous_record_submission(task_id,user_id,message_id,file_unique_id,media_group_id)
    if result=='added': await run(lambda: _v54_award_exam_speed_bonus_sync(task_id,user_id))
    return result

async def v29_ready_personal_exams():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.id definition_id,s.user_id,d.title FROM biology_linked_exam_definitions d
                JOIN biology_students s ON s.approved=TRUE AND s.onboarding_version>=19
                WHERE d.target_scope='chapter' AND s.study_track='chapter' AND s.current_chapter=d.chapter
                AND NOT EXISTS(SELECT 1 FROM biology_tasks t WHERE t.exam_definition_id=d.id AND t.target_scope='student:'||s.user_id);""")
            candidates=cur.fetchall(); ready=[]
            for c in candidates:
                required=set()
                cur.execute("SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s;",(c['definition_id'],))
                for r in cur.fetchall(): required.add((int(r['chapter']),int(r['lecture'])))
                cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s;",(c['definition_id'],))
                for p in cur.fetchall():
                    cur.execute("SELECT lectures FROM biology_personal_preparations WHERE user_id=%s AND chapter=%s AND prep_no=%s ORDER BY target_date DESC LIMIT 1;",(c['user_id'],p['chapter'],p['prep_no']))
                    x=cur.fetchone()
                    if not x:
                        cur.execute("SELECT lectures FROM biology_preparations WHERE chapter=%s AND chapter_prep_no=%s ORDER BY target_date DESC LIMIT 1;",(p['chapter'],p['prep_no']))
                        x=cur.fetchone()
                    if x:
                        for raw in (x['lectures'] or '').split(','):
                            if raw.strip().isdigit(): required.add((int(p['chapter']),int(raw)))
                if required:
                    missing=[]
                    for ch,lec in required:
                        cur.execute("SELECT 1 FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL;",(c['user_id'],ch,lec))
                        if not cur.fetchone(): missing.append((ch,lec))
                    if not missing: ready.append(c)
            return ready
    return await run(op)

async def v29_course_exam_release_at(definition_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position;",(definition_id,)); pairs=cur.fetchall()
            if not pairs: return None
            dates=[]
            for p in pairs:
                cur.execute("SELECT target_date FROM biology_preparations WHERE chapter=%s AND chapter_prep_no=%s ORDER BY target_date DESC LIMIT 1;",(p['chapter'],p['prep_no']))
                r=cur.fetchone()
                if r: dates.append(r['target_date'])
            if not dates: return None
            d=max(dates)+timedelta(days=1)
            cur.execute("SELECT (%s::date + INTERVAL '18 hours') AT TIME ZONE 'Asia/Baghdad' AS release_at;",(d,))
            return cur.fetchone()['release_at']
    return await run(op)


_v43_reliable_course_exam_release_at=v29_course_exam_release_at


_v42_previous_create_or_get_exam_task=v29_create_or_get_exam_task
async def v29_create_or_get_exam_task(definition_id,user_id,available_at=None,approval_required=False):
    """Create a task and resync old course snapshots to the corrected release time."""
    task=await _v42_previous_create_or_get_exam_task(definition_id,user_id,available_at,approval_required)
    if not task or available_at is None or approval_required: return task
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT target_scope FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL;",(int(definition_id),)); definition=cur.fetchone()
            if not definition or definition.get("target_scope")!="course": return task
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(task["id"],int(user_id)))
            if cur.fetchone(): return task
            cur.execute("SELECT CURRENT_TIMESTAMP AS now;"); now=cur.fetchone()["now"]; active=available_at<=now
            cur.execute("""UPDATE biology_tasks SET exam_available_at=%s,
                    deadline=%s+(exam_duration_hours||' hours')::INTERVAL,
                    exam_pending_activation=%s,closed=%s,published_at=CASE WHEN %s THEN COALESCE(published_at,%s) ELSE published_at END
                WHERE id=%s RETURNING *;""",(available_at,available_at,not active,not active,active,available_at,task["id"])); updated=cur.fetchone(); conn.commit(); return updated
    return await run(op)

async def v29_extend_exam(task_id,hours):
    def op():
        with connect() as conn, conn.cursor() as cur:
            h=max(1,min(168,int(hours)))
            cur.execute("UPDATE biology_tasks SET deadline=GREATEST(deadline,CURRENT_TIMESTAMP)+(%s||' hours')::interval,exam_extension_hours=COALESCE(exam_extension_hours,0)+%s,teacher_deadline_reminder_sent=FALSE WHERE id=%s AND kind='exam' RETURNING *;",(h,h,task_id))
            r=cur.fetchone(); conn.commit(); return r
    return await run(op)


async def v30_student_course_current_exams(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.*,d.target_scope,d.chapter,d.exam_type
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s AND d.target_scope='course' AND d.deleted_at IS NULL
                  AND GREATEST(t.deadline,COALESCE((SELECT e.extended_until FROM biology_task_extensions e WHERE e.task_id=t.id AND e.user_id=%s),t.deadline))>CURRENT_TIMESTAMP
                  AND t.closed=FALSE AND (t.exam_pending_activation=FALSE OR t.exam_pending_activation IS NULL)
                ORDER BY COALESCE(t.exam_available_at,t.deadline),t.id DESC;""",(user_id,user_id))
            return cur.fetchall()
    return await run(op)

async def v30_student_course_past_exams(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON (t.id) t.*,d.target_scope,d.chapter,d.exam_type,
                    sub.submitted_at AS student_submitted_at
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id AND d.target_scope='course' AND d.deleted_at IS NULL
                LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=%s
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s
                  AND (t.closed=TRUE OR t.deadline<=CURRENT_TIMESTAMP OR sub.submitted_at IS NOT NULL)
                ORDER BY t.id DESC;""",(user_id,user_id))
            return cur.fetchall()
    return await run(op)

async def v30_student_chapter_exam_catalog(user_id, chapter):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.*
                FROM biology_linked_exam_definitions d
                WHERE d.target_scope='chapter' AND d.chapter=%s AND d.deleted_at IS NULL
                ORDER BY d.id DESC;""",(chapter,))
            definitions=cur.fetchall(); result=[]
            for d in definitions:
                cur.execute("""SELECT t.*,sub.submitted_at AS student_submitted_at
                    FROM biology_tasks t
                    LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=%s
                    WHERE t.kind='exam' AND t.exam_definition_id=%s AND t.target_scope=%s
                    ORDER BY t.id DESC LIMIT 1;""",(user_id,d['id'],f'student:{user_id}'))
                task=cur.fetchone()
                # Resolve all lectures represented by the definition.
                required=set()
                cur.execute("SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s ORDER BY position;",(d['id'],))
                for r in cur.fetchall(): required.add((int(r['chapter']),int(r['lecture'])))
                cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position;",(d['id'],))
                for pair in cur.fetchall():
                    cur.execute("SELECT lectures FROM biology_personal_preparations WHERE user_id=%s AND chapter=%s AND prep_no=%s ORDER BY target_date DESC LIMIT 1;",(user_id,pair['chapter'],pair['prep_no']))
                    x=cur.fetchone()
                    if not x:
                        cur.execute("SELECT lectures FROM biology_preparations WHERE chapter=%s AND chapter_prep_no=%s ORDER BY target_date DESC LIMIT 1;",(pair['chapter'],pair['prep_no']))
                        x=cur.fetchone()
                    if x:
                        for raw in (x['lectures'] or '').split(','):
                            if raw.strip().isdigit(): required.add((int(pair['chapter']),int(raw)))
                completed=0
                for ch,lec in required:
                    cur.execute("SELECT 1 FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL LIMIT 1;",(user_id,ch,lec))
                    completed += 1 if cur.fetchone() else 0
                ready=bool(required) and completed==len(required)
                if task:
                    if task.get('student_submitted_at'): status='submitted'
                    elif task.get('exam_pending_activation'): status='pending_approval'
                    else:
                        cur.execute("SELECT GREATEST(%s,COALESCE((SELECT e.extended_until FROM biology_task_extensions e WHERE e.task_id=%s AND e.user_id=%s),%s)) AS effective_deadline;",(task['deadline'],task['id'],user_id,task['deadline']))
                        eff=cur.fetchone()['effective_deadline']
                        status='closed' if eff<=datetime_now(cur) else 'open'
                else:
                    status='ready_waiting_task' if ready else 'locked'
                result.append({**dict(d),'task_id':task['id'] if task else None,'task':task,'status':status,'ready':ready,'completed_lectures':completed,'required_lectures':len(required)})
            return result
    return await run(op)

async def v30_exam_task_for_student(user_id, task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.*,d.target_scope,d.chapter,d.exam_type
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.id=%s AND t.kind='exam' AND (d.id IS NULL OR d.deleted_at IS NULL);""",(user_id,task_id))
            return cur.fetchone()
    return await run(op)

# ===== v31: exam deletion + reliable per-student extensions =====
async def v31_init_exam_controls():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;")
            cur.execute("CREATE INDEX IF NOT EXISTS biology_exam_defs_active_idx ON biology_linked_exam_definitions(target_scope,chapter,id) WHERE deleted_at IS NULL;")
            conn.commit()
    await run(op)

async def v31_active_exam_definitions():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE deleted_at IS NULL ORDER BY id DESC;")
            return cur.fetchall()
    return await run(op)

async def v31_delete_exam_definition(definition_id, actor_id=0):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL FOR UPDATE;", (definition_id,))
            d=cur.fetchone()
            if not d: return None
            # Soft-delete only. Never delete task/submission rows: historical attempts are immutable
            # and must remain auditable. The scheduler and student UI use deleted_at to hide the exam.
            cur.execute("UPDATE biology_linked_exam_definitions SET deleted_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING *;", (definition_id,))
            row=cur.fetchone()
            cur.execute("UPDATE biology_tasks SET closed=TRUE WHERE exam_definition_id=%s AND closed=FALSE;",(definition_id,))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);", (actor_id or d["created_by"], "exam_soft_deleted", f"definition_id={definition_id}"))
            conn.commit(); return row
    return await run(op)

async def v31_exam_definition_for_admin(definition_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL;", (definition_id,))
            return cur.fetchone()
    return await run(op)

async def v31_extend_student_exam(task_id,user_id,hours):
    def op():
        h=max(1,min(168,int(hours)))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.*,s.full_name,s.parent_chat_id FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                JOIN biology_students s ON s.user_id=%s
                WHERE t.id=%s AND t.kind='exam' FOR UPDATE;""",(user_id,user_id,task_id))
            t=cur.fetchone()
            if not t: return {"status":"not_found"}
            cur.execute("SELECT submitted_at FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL LIMIT 1;",(task_id,user_id))
            if cur.fetchone(): return {"status":"submitted"}
            now=datetime_now(cur)
            # Extend from the student's currently effective deadline, not the global task deadline.
            cur.execute("SELECT extended_until FROM biology_task_extensions WHERE task_id=%s AND user_id=%s FOR UPDATE;",(task_id,user_id))
            ext=cur.fetchone()
            base=max(t["deadline"], ext["extended_until"] if ext else t["deadline"], now)
            until=base+timedelta(hours=h)
            cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,requested_at,extended_until)
                VALUES(%s,%s,CURRENT_TIMESTAMP,%s)
                ON CONFLICT(task_id,user_id) DO UPDATE SET requested_at=CURRENT_TIMESTAMP,extended_until=EXCLUDED.extended_until
                RETURNING *;""",(task_id,user_id,until))
            row=cur.fetchone(); conn.commit()
            row["student_name"]=t["full_name"]; row["deadline"]=t["deadline"]; return {"status":"ok","extension":row,"student":t}
    return await run(op)


# ========================= v32 CLEAN EXAM MANAGEMENT DB =========================
async def v32_admin_exam_tasks():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON (t.exam_definition_id)
                    t.id,t.exam_definition_id,t.title,t.deadline,t.closed,t.target_scope,d.chapter,d.exam_type,
                    (SELECT COUNT(*) FROM biology_task_students x WHERE x.task_id=t.id) AS student_count
                FROM biology_tasks t
                JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id AND d.deleted_at IS NULL
                WHERE t.kind='exam'
                ORDER BY t.exam_definition_id,t.id DESC;""")
            rows=cur.fetchall()
            rows.sort(key=lambda r: (r.get("deadline"),r.get("id")), reverse=True)
            return rows
    return await run(op)

async def v32_admin_exam_students(task_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.user_id,s.full_name,
                EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=%s AND sub.user_id=s.user_id AND sub.submitted_at IS NOT NULL) AS submitted,
                (SELECT e.extended_until FROM biology_task_extensions e WHERE e.task_id=%s AND e.user_id=s.user_id) AS extended_until
                FROM biology_task_students ts JOIN biology_students s ON s.user_id=ts.user_id
                WHERE ts.task_id=%s ORDER BY s.full_name,s.user_id;""",(task_id,task_id,task_id))
            return cur.fetchall()
    return await run(op)

async def v32_admin_extend_student_exam(task_id,user_id,hours,admin_id):
    def op():
        h=max(1,min(72,int(hours)))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.id,t.title,t.deadline,t.kind,s.full_name
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                JOIN biology_students s ON s.user_id=%s
                JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id AND d.deleted_at IS NULL
                WHERE t.id=%s AND t.kind='exam' FOR UPDATE;""",(user_id,user_id,task_id))
            task=cur.fetchone()
            if not task: return {"status":"not_found"}
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(task_id,user_id))
            if cur.fetchone(): return {"status":"submitted"}
            cur.execute("SELECT extended_until FROM biology_task_extensions WHERE task_id=%s AND user_id=%s FOR UPDATE;",(task_id,user_id))
            ext=cur.fetchone()
            base=max(task["deadline"],ext["extended_until"] if ext else task["deadline"],datetime_now(cur))
            until=base+timedelta(hours=h)
            cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,requested_at,extended_until)
                VALUES(%s,%s,CURRENT_TIMESTAMP,%s)
                ON CONFLICT(task_id,user_id) DO UPDATE SET requested_at=CURRENT_TIMESTAMP,extended_until=EXCLUDED.extended_until
                RETURNING *;""",(task_id,user_id,until))
            extension=cur.fetchone()
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);",(admin_id,"exam_student_extension",f"task_id={task_id};user_id={user_id};hours={h}"))
            conn.commit()
            return {"status":"ok","extended_until":extension["extended_until"],"title":task["title"],"student_name":task["full_name"]}
    return await run(op)

# ========================= v37 COMPLETE STUDY-PATH HARDENING =========================
# Track changes: 3 automatic changes, then admin approval.
async def v37_track_change_status(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track,current_chapter,track_change_count FROM biology_students WHERE user_id=%s;",(user_id,))
            student=cur.fetchone()
            cur.execute("""SELECT id,requested_track,requested_chapter,status,created_at,decided_at,decided_by
                           FROM biology_track_change_requests
                           WHERE user_id=%s AND status='pending' ORDER BY id DESC LIMIT 1;""",(user_id,))
            pending=cur.fetchone()
            return {"student":student,"pending":pending}
    return await run(op)

async def v37_request_track_change(user_id, study_track, chapter, start_date, plan_rows):
    if study_track not in ("course","chapter") or (study_track=="chapter" and chapter not in range(1,6)):
        return {"status":"invalid"}
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,))
            st=cur.fetchone()
            if not st: return {"status":"missing"}
            count=int(st.get("track_change_count") or 0)
            if count>=3:
                cur.execute("""INSERT INTO biology_track_change_requests
                    (user_id,requested_track,requested_chapter,requested_start_date,status)
                    VALUES(%s,%s,%s,%s,'pending')
                    ON CONFLICT (user_id) WHERE status='pending' DO UPDATE SET
                      requested_track=EXCLUDED.requested_track,requested_chapter=EXCLUDED.requested_chapter,
                      requested_start_date=EXCLUDED.requested_start_date,created_at=CURRENT_TIMESTAMP
                    RETURNING *;""",(user_id,study_track,chapter,start_date))
                req=cur.fetchone(); conn.commit()
                return {"status":"pending","request":req,"count":count}
            # Automatic change. Preserve history, replace only future personal schedule.
            cur.execute("""UPDATE biology_students SET study_track=%s,current_chapter=%s,
                track_started_on=%s,track_change_count=COALESCE(track_change_count,0)+1,
                last_seen=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;""",
                (study_track,chapter,start_date,user_id))
            student=cur.fetchone()
            cur.execute("DELETE FROM biology_personal_preparations WHERE user_id=%s AND target_date>=CURRENT_DATE;",(user_id,))
            cur.execute("DELETE FROM biology_scheduled_tasks WHERE published=FALSE AND linked_student_id=%s;",(user_id,))
            cur.execute("""DELETE FROM biology_task_students ts USING biology_tasks t
                WHERE ts.task_id=t.id AND ts.user_id=%s AND t.closed=FALSE
                AND NOT EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id AND sub.user_id=%s)
                AND ((%s='course' AND t.target_scope LIKE 'student:%%') OR (%s='chapter' AND t.target_scope='course'));""",
                (user_id,user_id,study_track,study_track))
            if study_track=="course":
                cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
                    SELECT t.id,%s FROM biology_tasks t
                    WHERE t.closed=FALSE AND t.deadline>CURRENT_TIMESTAMP
                      AND t.target_scope IN ('course','all') ON CONFLICT DO NOTHING;""",(user_id,))
            else:
                for target_date,ch,lectures,prep_no in plan_rows:
                    cur.execute("""INSERT INTO biology_personal_preparations
                        (user_id,target_date,chapter,lectures,prep_no)
                        VALUES(%s,%s,%s,%s,%s)
                        ON CONFLICT(user_id,target_date,chapter,prep_no) DO UPDATE SET
                        chapter=EXCLUDED.chapter,lectures=EXCLUDED.lectures,prep_no=EXCLUDED.prep_no,
                        notified=FALSE,notified_at=NULL;""",(user_id,target_date,ch,lectures,prep_no))
            cur.execute("""INSERT INTO biology_track_change_audit
                (user_id,old_track,old_chapter,new_track,new_chapter,changed_by,change_type)
                VALUES(%s,%s,%s,%s,%s,%s,'automatic');""",
                (user_id,st.get("study_track"),st.get("current_chapter"),study_track,chapter,user_id))
            conn.commit()
            return {"status":"ok","count":count+1,"student":student}
    return await run(op)

async def v37_admin_decide_track_change(request_id, approve, admin_id):
    def op():
        from data import CHAPTER_PREPARATION_DISTRIBUTION
        from data import CHAPTER_PREPARATION_DISTRIBUTION
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_track_change_requests WHERE id=%s AND status='pending' FOR UPDATE;",(request_id,))
            req=cur.fetchone()
            if not req: return {"status":"missing"}
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(req["user_id"],))
            st=cur.fetchone()
            if not st: return {"status":"missing"}
            status="approved" if approve else "denied"
            if approve:
                cur.execute("""UPDATE biology_students SET study_track=%s,current_chapter=%s,
                    track_started_on=%s,last_seen=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;""",
                    (req["requested_track"],req["requested_chapter"],req["requested_start_date"],req["user_id"]))
                student=cur.fetchone()
                cur.execute("DELETE FROM biology_personal_preparations WHERE user_id=%s AND target_date>=CURRENT_DATE;",(req["user_id"],))
                cur.execute("""DELETE FROM biology_task_students ts USING biology_tasks t
                    WHERE ts.task_id=t.id AND ts.user_id=%s AND t.closed=FALSE
                    AND NOT EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id AND sub.user_id=%s)
                    AND ((%s='course' AND t.target_scope LIKE 'student:%%') OR (%s='chapter' AND t.target_scope='course'));""",
                    (req["user_id"],req["user_id"],req["requested_track"],req["requested_track"]))
                if req["requested_track"]=="chapter":
                    # Build the complete plan from the requested chapter through chapter 9.
                    cursor=req["requested_start_date"]
                    for ch in range(int(req["requested_chapter"]),10):
                        groups=CHAPTER_PREPARATION_DISTRIBUTION.get(ch,[])
                        allowed={6,0,1,2,3} if ch==1 else ({6,0,1,3} if ch==2 else {6,1,3})
                        for prep_no,nums in enumerate(groups,1):
                            while cursor.weekday() not in allowed: cursor += timedelta(days=1)
                            cur.execute("""INSERT INTO biology_personal_preparations(user_id,target_date,chapter,lectures,prep_no)
                                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,target_date,chapter,prep_no) DO UPDATE SET
                                chapter=EXCLUDED.chapter,lectures=EXCLUDED.lectures,prep_no=EXCLUDED.prep_no;""",
                                (req["user_id"],cursor,ch,",".join(map(str,nums)),prep_no))
                            cursor += timedelta(days=1)
                else:
                    cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
                        SELECT t.id,%s FROM biology_tasks t WHERE t.closed=FALSE AND t.deadline>CURRENT_TIMESTAMP
                        AND t.target_scope IN ('course','all') ON CONFLICT DO NOTHING;""",(req["user_id"],))
                cur.execute("""INSERT INTO biology_track_change_audit
                    (user_id,old_track,old_chapter,new_track,new_chapter,changed_by,change_type)
                    VALUES(%s,%s,%s,%s,%s,%s,'admin_approved');""",
                    (req["user_id"],st.get("study_track"),st.get("current_chapter"),req["requested_track"],req["requested_chapter"],admin_id))
            else:
                student=st
            cur.execute("""UPDATE biology_track_change_requests SET status=%s,decided_at=CURRENT_TIMESTAMP,decided_by=%s
                           WHERE id=%s;""",(status,admin_id,request_id))
            conn.commit()
            return {"status":status,"request":req,"student":student}
    return await run(op)

async def v37_pending_track_requests():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT r.*,s.full_name FROM biology_track_change_requests r
                JOIN biology_students s ON s.user_id=r.user_id
                WHERE r.status='pending' ORDER BY r.created_at;""")
            return cur.fetchall()
    return await run(op)

async def v37_current_personal_preparation(user_id):
    """Return only the first incomplete preparation; future preparations are not directly accessible."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT pp.* FROM biology_personal_preparations pp
                WHERE pp.user_id=%s ORDER BY pp.target_date,pp.id;""",(user_id,))
            rows=cur.fetchall()
            for row in rows:
                nums=[int(x) for x in (row["lectures"] or "").split(",") if x.strip().isdigit()]
                if not nums: continue
                cur.execute("""SELECT COUNT(*) n FROM biology_lecture_progress
                    WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s) AND completed_at IS NOT NULL;""",
                    (user_id,row["chapter"],nums))
                if int(cur.fetchone()["n"]) < len(set(nums)):
                    return row
            return None
    return await run(op)

async def v37_preparation_access(user_id,chapter,lecture):
    row=await v37_current_personal_preparation(user_id)
    if not row: return {"allowed":False,"reason":"finished","row":None}
    nums=[int(x) for x in (row["lectures"] or "").split(",") if x.strip().isdigit()]
    if int(row["chapter"])!=int(chapter) or int(lecture) not in nums:
        return {"allowed":False,"reason":"next_only","row":row}
    return {"allowed":True,"reason":"ok","row":row}

async def v37_chapter_completion_plan(user_id):
    """Return chapter-by-chapter finish dates and the full-course finish date."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track,current_chapter,schedule_mode FROM biology_students WHERE user_id=%s;",(user_id,))
            st=cur.fetchone()
            if not st: return {"chapters":[],"full_finish":None}
            if st["study_track"]=="course" and st.get("schedule_mode")!="custom":
                cur.execute("""SELECT chapter,MAX(target_date) finish_date,COUNT(*) prep_count
                    FROM biology_preparations GROUP BY chapter ORDER BY chapter;""")
            else:
                cur.execute("""SELECT chapter,MAX(target_date) finish_date,COUNT(*) prep_count
                    FROM biology_personal_preparations WHERE user_id=%s
                    GROUP BY chapter ORDER BY chapter;""",(user_id,))
            rows=cur.fetchall()
            finish=max((r["finish_date"] for r in rows),default=None)
            return {"chapters":rows,"full_finish":finish}
    return await run(op)

async def v37_track_change_init():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS track_change_count INTEGER NOT NULL DEFAULT 0;")
            cur.execute("""CREATE TABLE IF NOT EXISTS biology_track_change_requests(
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
                requested_track TEXT NOT NULL CHECK(requested_track IN ('course','chapter')),
                requested_chapter INTEGER,
                requested_start_date DATE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TIMESTAMPTZ,
                decided_by BIGINT
            );""")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS biology_track_pending_uq ON biology_track_change_requests(user_id) WHERE status='pending';")
            cur.execute("""CREATE TABLE IF NOT EXISTS biology_track_change_audit(
                id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,
                old_track TEXT,old_chapter INTEGER,new_track TEXT,new_chapter INTEGER,
                changed_by BIGINT NOT NULL,change_type TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );""")
            cur.execute("CREATE INDEX IF NOT EXISTS biology_track_req_status_idx ON biology_track_change_requests(status,created_at);")
            conn.commit()
    await run(op)

# Preserve the original migration and extend it with v37 schema synchronously.
_v37_original_init_db=init_db
def init_db():
    _v37_original_init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS track_change_count INTEGER NOT NULL DEFAULT 0;")
        cur.execute("""CREATE TABLE IF NOT EXISTS biology_track_change_requests(
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            requested_track TEXT NOT NULL CHECK(requested_track IN ('course','chapter')),
            requested_chapter INTEGER,
            requested_start_date DATE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMPTZ,
            decided_by BIGINT
        );""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS biology_track_pending_uq ON biology_track_change_requests(user_id) WHERE status='pending';")
        cur.execute("""CREATE TABLE IF NOT EXISTS biology_track_change_audit(
            id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,
            old_track TEXT,old_chapter INTEGER,new_track TEXT,new_chapter INTEGER,
            changed_by BIGINT NOT NULL,change_type TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        );""")
        cur.execute("CREATE INDEX IF NOT EXISTS biology_track_req_status_idx ON biology_track_change_requests(status,created_at);")
        conn.commit()


# Restore the reliable scheduling implementations after all legacy layers have
# finished defining their compatibility versions.
v41_set_study_days=_v42_reliable_set_study_days
v37_chapter_completion_plan=_v42_reliable_completion_plan


# ========================= v43 EXAM RECOVERY AND WARNING CONTROL =========================

async def v29_course_exam_release_at(definition_id):
    """An exam cannot start before the teacher has actually published it.

    Normal publication starts 6 hours after the linked preparation. If the
    teacher publishes the questions later, the full exam duration starts from
    the definition creation time instead of an already expired timestamp.
    """
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.created_at,MAX(p.published_at) AS prep_published_at
                FROM biology_linked_exam_definitions d
                JOIN biology_linked_exam_preparations lp ON lp.definition_id=d.id
                JOIN biology_preparations p
                  ON p.chapter=lp.chapter AND p.chapter_prep_no=lp.prep_no
                WHERE d.id=%s AND d.deleted_at IS NULL AND p.published=TRUE
                GROUP BY d.id,d.created_at;""",(int(definition_id),))
            row=cur.fetchone()
            if not row or not row.get("prep_published_at"): return None
            scheduled=row["prep_published_at"]+timedelta(hours=6)
            return max(scheduled,row["created_at"])
    return await run(op)


_v43_reliable_course_exam_release_at=v29_course_exam_release_at


async def v43_clear_task_warnings(task_id,user_id,admin_id=0,waive=False):
    """Remove every warning tied to one exact task and keep totals consistent."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""DELETE FROM biology_warning_log
                WHERE task_id=%s AND user_id=%s RETURNING id;""",(int(task_id),int(user_id)))
            removed=len(cur.fetchall())
            if removed:
                cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-%s) WHERE user_id=%s;",(removed,int(user_id)))
            if waive:
                cur.execute("""INSERT INTO biology_exam_warning_waivers(task_id,user_id,waived_by)
                    VALUES(%s,%s,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET
                    waived_by=EXCLUDED.waived_by,created_at=CURRENT_TIMESTAMP;""",(int(task_id),int(user_id),int(admin_id or 0)))
            if removed or waive:
                cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'exam_warning_removed',%s);",
                            (int(admin_id or 0),f"task_id={task_id};user_id={user_id};removed={removed};waive={bool(waive)}"))
            cur.execute("SELECT warnings FROM biology_students WHERE user_id=%s;",(int(user_id),)); student=cur.fetchone()
            conn.commit(); return {"removed":removed,"warnings":int((student or {}).get("warnings") or 0)}
    return await run(op)


_v43_previous_create_or_get_exam_task=v29_create_or_get_exam_task
async def v29_create_or_get_exam_task(definition_id,user_id,available_at=None,approval_required=False):
    """Resync old snapshots and undo premature warnings after a late publish."""
    task=await _v43_previous_create_or_get_exam_task(definition_id,user_id,available_at,approval_required)
    if not task or available_at is None or approval_required: return task
    def status_op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.*,d.target_scope,CURRENT_TIMESTAMP AS now
                FROM biology_tasks t JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.id=%s;""",(task["id"],)); return cur.fetchone()
    current=await run(status_op)
    if current and current.get("target_scope")=="course" and current["deadline"]>current["now"]:
        await v43_clear_task_warnings(current["id"],user_id,0,False)
    return current or task


_v43_reliable_create_or_get_exam_task=v29_create_or_get_exam_task


async def missing_students(task_id):
    """Never recreate an exam warning explicitly removed by the admin."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT s.* FROM biology_task_students roster
                JOIN biology_students s ON s.user_id=roster.user_id
                JOIN biology_tasks t ON t.id=roster.task_id
                WHERE roster.task_id=%s AND s.approved=TRUE AND t.optional_practice=FALSE
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions x WHERE x.task_id=%s AND x.user_id=s.user_id AND x.submitted_at IS NOT NULL)
                  AND NOT EXISTS(SELECT 1 FROM biology_warning_log w WHERE w.task_id=%s AND w.user_id=s.user_id)
                  AND NOT EXISTS(SELECT 1 FROM biology_exam_warning_waivers w WHERE w.task_id=%s AND w.user_id=s.user_id)
                  AND NOT EXISTS(SELECT 1 FROM biology_leave_requests lr WHERE lr.user_id=s.user_id AND lr.leave_date=t.deadline::date AND lr.status='approved')
                  AND NOT EXISTS(SELECT 1 FROM biology_task_extensions e WHERE e.task_id=%s AND e.user_id=s.user_id AND e.extended_until>CURRENT_TIMESTAMP);""",
                (int(task_id),int(task_id),int(task_id),int(task_id),int(task_id)))
            return cur.fetchall()
    return await run(op)


async def v43_extend_exam_student(task_id,user_id,hours,admin_id):
    """Extend and reopen one student's exam even after it has closed."""
    def op():
        h=max(1,min(168,int(hours)))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.*,s.full_name,s.parent_chat_id,CURRENT_TIMESTAMP AS now
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                JOIN biology_students s ON s.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.id=%s AND t.kind='exam' AND (d.id IS NULL OR d.deleted_at IS NULL)
                FOR UPDATE OF t,s;""",(int(user_id),int(task_id))); task=cur.fetchone()
            if not task: return {"status":"not_found"}
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(int(task_id),int(user_id)))
            if cur.fetchone(): return {"status":"submitted"}
            cur.execute("SELECT extended_until FROM biology_task_extensions WHERE task_id=%s AND user_id=%s FOR UPDATE;",(int(task_id),int(user_id))); ext=cur.fetchone()
            base=max(task["deadline"],ext["extended_until"] if ext else task["deadline"],task["now"]); until=base+timedelta(hours=h)
            cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,requested_at,extended_until)
                VALUES(%s,%s,CURRENT_TIMESTAMP,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET
                requested_at=CURRENT_TIMESTAMP,extended_until=EXCLUDED.extended_until;""",(int(task_id),int(user_id),until))
            cur.execute("DELETE FROM biology_exam_warning_waivers WHERE task_id=%s AND user_id=%s;",(int(task_id),int(user_id)))
            cur.execute("DELETE FROM biology_warning_log WHERE task_id=%s AND user_id=%s RETURNING id;",(int(task_id),int(user_id))); removed=len(cur.fetchall())
            if removed: cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-%s) WHERE user_id=%s;",(removed,int(user_id)))
            cur.execute("""UPDATE biology_tasks SET closed=FALSE,warned=FALSE,
                teacher_deadline_reminder_sent=FALSE,champion_announced=FALSE WHERE id=%s;""",(int(task_id),))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'reopen_exam_student',%s);",
                        (int(admin_id),f"task_id={task_id};user_id={user_id};hours={h};warnings_removed={removed}"))
            cur.execute("SELECT warnings FROM biology_students WHERE user_id=%s;",(int(user_id),)); remaining=int(cur.fetchone()["warnings"])
            conn.commit(); return {"status":"ok","task_id":int(task_id),"definition_id":task.get("exam_definition_id"),"user_id":int(user_id),"title":task["title"],"student_name":task["full_name"],"parent_chat_id":task.get("parent_chat_id"),"extended_until":until,"warnings_removed":removed,"warnings":remaining}
    return await run(op)


async def v43_extend_exam_definition(definition_id,hours,admin_id):
    """Extend/reopen every unsubmitted student task under one exam definition."""
    def op():
        h=max(1,min(168,int(hours)))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL FOR UPDATE;",(int(definition_id),)); definition=cur.fetchone()
            if not definition: return {"status":"not_found","students":[]}
            cur.execute("""SELECT t.id AS task_id,t.title,t.deadline,ts.user_id,s.full_name,s.parent_chat_id,
                    e.extended_until,CURRENT_TIMESTAMP AS now
                FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students s ON s.user_id=ts.user_id
                LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=ts.user_id
                WHERE t.exam_definition_id=%s AND t.kind='exam'
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id AND sub.user_id=ts.user_id AND sub.submitted_at IS NOT NULL)
                ORDER BY t.id FOR UPDATE OF t,s;""",(int(definition_id),)); students=cur.fetchall(); results=[]
            for item in students:
                base=max(item["deadline"],item.get("extended_until") or item["deadline"],item["now"]); until=base+timedelta(hours=h)
                cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,requested_at,extended_until)
                    VALUES(%s,%s,CURRENT_TIMESTAMP,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET
                    requested_at=CURRENT_TIMESTAMP,extended_until=EXCLUDED.extended_until;""",(item["task_id"],item["user_id"],until))
                cur.execute("DELETE FROM biology_exam_warning_waivers WHERE task_id=%s AND user_id=%s;",(item["task_id"],item["user_id"]))
                cur.execute("DELETE FROM biology_warning_log WHERE task_id=%s AND user_id=%s RETURNING id;",(item["task_id"],item["user_id"])); removed=len(cur.fetchall())
                if removed: cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-%s) WHERE user_id=%s;",(removed,item["user_id"]))
                cur.execute("""UPDATE biology_tasks SET closed=FALSE,warned=FALSE,
                    teacher_deadline_reminder_sent=FALSE,champion_announced=FALSE WHERE id=%s;""",(item["task_id"],))
                cur.execute("SELECT warnings FROM biology_students WHERE user_id=%s;",(item["user_id"],)); remaining=int(cur.fetchone()["warnings"])
                results.append({**dict(item),"extended_until":until,"warnings_removed":removed,"warnings":remaining})
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'reopen_exam_all',%s);",
                        (int(admin_id),f"definition_id={definition_id};hours={h};students={len(results)}"))
            conn.commit(); return {"status":"ok","definition":definition,"students":results,"warnings_removed":sum(x["warnings_removed"] for x in results)}
    return await run(op)


async def v43_clear_exam_definition_warnings(definition_id,admin_id):
    """Remove and permanently waive the premature warnings for one exam."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT title FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL;",(int(definition_id),)); definition=cur.fetchone()
            if not definition: return {"status":"not_found","students":[],"removed":0}
            cur.execute("""SELECT DISTINCT ts.user_id,t.id AS task_id,s.full_name,s.parent_chat_id
                FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students s ON s.user_id=ts.user_id
                WHERE t.exam_definition_id=%s AND t.kind='exam';""",(int(definition_id),)); students=cur.fetchall(); removed_total=0
            for item in students:
                cur.execute("DELETE FROM biology_warning_log WHERE task_id=%s AND user_id=%s RETURNING id;",(item["task_id"],item["user_id"])); removed=len(cur.fetchall()); removed_total+=removed
                if removed: cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-%s) WHERE user_id=%s;",(removed,item["user_id"]))
                item["warnings_removed"]=removed
                cur.execute("SELECT warnings FROM biology_students WHERE user_id=%s;",(item["user_id"],)); item["warnings"]=int(cur.fetchone()["warnings"])
                cur.execute("""INSERT INTO biology_exam_warning_waivers(task_id,user_id,waived_by)
                    VALUES(%s,%s,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET
                    waived_by=EXCLUDED.waived_by,created_at=CURRENT_TIMESTAMP;""",(item["task_id"],item["user_id"],int(admin_id)))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'clear_exam_warnings',%s);",
                        (int(admin_id),f"definition_id={definition_id};removed={removed_total}"))
            conn.commit(); return {"status":"ok","title":definition["title"],"students":students,"removed":removed_total}
    return await run(op)


async def v43_warning_students():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT user_id,full_name,username,warnings,approved
                FROM biology_students WHERE warnings>0 ORDER BY warnings DESC,full_name,user_id;"""); return cur.fetchall()
    return await run(op)


async def v43_reconcile_warning_counts():
    """Make the displayed warning counter exactly match the warning ledger."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE biology_students s SET warnings=(
                    SELECT COUNT(*) FROM biology_warning_log w WHERE w.user_id=s.user_id)
                WHERE s.warnings<>(SELECT COUNT(*) FROM biology_warning_log w WHERE w.user_id=s.user_id);""")
            changed=cur.rowcount; conn.commit(); return changed
    return await run(op)


async def v43_remove_warning(user_id,warning_id,admin_id):
    """Remove any selected warning and waive task recovery when applicable."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT w.*,t.kind FROM biology_warning_log w
                LEFT JOIN biology_tasks t ON t.id=w.task_id
                WHERE w.id=%s AND w.user_id=%s FOR UPDATE OF w;""",(int(warning_id),int(user_id))); warning=cur.fetchone()
            if not warning: return {"status":"not_found"}
            cur.execute("DELETE FROM biology_warning_log WHERE id=%s;",(int(warning_id),))
            cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-1) WHERE user_id=%s RETURNING warnings,full_name;",(int(user_id),)); student=cur.fetchone()
            if warning.get("task_id") and warning.get("kind")=="exam":
                cur.execute("""INSERT INTO biology_exam_warning_waivers(task_id,user_id,waived_by)
                    VALUES(%s,%s,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET
                    waived_by=EXCLUDED.waived_by,created_at=CURRENT_TIMESTAMP;""",(warning["task_id"],int(user_id),int(admin_id)))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'remove_selected_warning',%s);",
                        (int(admin_id),f"user_id={user_id};warning_id={warning_id};task_id={warning.get('task_id')}"))
            conn.commit(); return {"status":"ok","warning":warning,"student":student}
    return await run(op)


async def v43_exam_definition_detail(definition_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.*,
                    COUNT(DISTINCT ts.user_id) AS student_count,
                    COUNT(DISTINCT t.id) AS internal_task_count,
                    COUNT(DISTINCT w.id) AS warning_count,
                    COUNT(DISTINCT sub.user_id) AS submitted_count,
                    BOOL_OR(t.closed=FALSE) AS has_open
                FROM biology_linked_exam_definitions d
                LEFT JOIN biology_tasks t ON t.exam_definition_id=d.id AND t.kind='exam'
                LEFT JOIN biology_task_students ts ON ts.task_id=t.id
                LEFT JOIN biology_warning_log w ON w.task_id=t.id
                LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.submitted_at IS NOT NULL
                WHERE d.id=%s AND d.deleted_at IS NULL GROUP BY d.id;""",(int(definition_id),)); return cur.fetchone()
    return await run(op)


async def v43_repair_premature_exam_warnings():
    """Remove warnings emitted before a late-published exam's real deadline."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT w.id AS warning_id,w.user_id,w.task_id,w.created_at AS warning_at,
                    t.exam_duration_hours,d.id AS definition_id,d.created_at AS definition_created_at,
                    MAX(p.published_at) AS prep_published_at
                FROM biology_warning_log w
                JOIN biology_tasks t ON t.id=w.task_id AND t.kind='exam'
                JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id AND d.target_scope='course'
                JOIN biology_linked_exam_preparations lp ON lp.definition_id=d.id
                JOIN biology_preparations p ON p.chapter=lp.chapter AND p.chapter_prep_no=lp.prep_no
                WHERE w.reason LIKE 'عدم إرسال%%' AND p.published_at IS NOT NULL
                GROUP BY w.id,w.user_id,w.task_id,w.created_at,t.exam_duration_hours,d.id,d.created_at
                ORDER BY w.id;"""); candidates=cur.fetchall(); repaired=[]
            cur.execute("SELECT CURRENT_TIMESTAMP AS now;"); now=cur.fetchone()["now"]
            for item in candidates:
                release_at=max(item["definition_created_at"],item["prep_published_at"]+timedelta(hours=6))
                correct_deadline=release_at+timedelta(hours=max(1,int(item.get("exam_duration_hours") or 1)))
                if item["warning_at"]>=correct_deadline: continue
                cur.execute("DELETE FROM biology_warning_log WHERE id=%s RETURNING id;",(item["warning_id"],))
                if not cur.fetchone(): continue
                cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-1) WHERE user_id=%s RETURNING warnings,full_name;",(item["user_id"],)); student=cur.fetchone()
                cur.execute("""INSERT INTO biology_exam_warning_waivers(task_id,user_id,waived_by)
                    VALUES(%s,%s,0) ON CONFLICT(task_id,user_id) DO UPDATE SET created_at=CURRENT_TIMESTAMP;""",(item["task_id"],item["user_id"]))
                if correct_deadline>now:
                    cur.execute("""UPDATE biology_tasks SET exam_available_at=%s,deadline=%s,
                        closed=FALSE,warned=FALSE,teacher_deadline_reminder_sent=FALSE,
                        champion_announced=FALSE WHERE id=%s;""",(release_at,correct_deadline,item["task_id"]))
                repaired.append({**dict(item),"correct_deadline":correct_deadline,"warnings":int(student["warnings"]),"full_name":student["full_name"]})
            if repaired:
                cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(0,'repair_premature_exam_warnings',%s);",(f"warnings={len(repaired)}",))
            conn.commit(); return repaired
    return await run(op)


_v43_previous_init_db=init_db
def init_db():
    _v43_previous_init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS biology_exam_warning_waivers(
            task_id INTEGER NOT NULL REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            waived_by BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(task_id,user_id)
        );
        CREATE INDEX IF NOT EXISTS biology_exam_warning_waivers_user_idx
            ON biology_exam_warning_waivers(user_id,task_id);""")
        conn.commit()

async def v37_set_student_schedule(user_id, days, mode="custom"):
    days=sorted({int(x) for x in days})
    if not 1<=len(days)<=7 or any(x<0 or x>6 for x in days):
        return {"status":"days"}
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,))
            st=cur.fetchone()
            if not st: return {"status":"missing"}
            cur.execute("UPDATE biology_students SET schedule_mode=%s,study_days=%s WHERE user_id=%s RETURNING *;",
                        (mode,days,user_id)); student=cur.fetchone()
            # Rebuild future personal slots onto the newly selected weekdays without changing order.
            cur.execute("""SELECT id FROM biology_personal_preparations
                WHERE user_id=%s AND target_date>=CURRENT_DATE ORDER BY target_date,id;""",(user_id,))
            future=cur.fetchall()
            cursor=None
            from datetime import date
            if future:
                cur.execute("""SELECT id,target_date FROM biology_personal_preparations
                    WHERE user_id=%s AND target_date>=CURRENT_DATE ORDER BY target_date,id FOR UPDATE;""",(user_id,))
                future=cur.fetchall()
                first=future[0]["target_date"]
                # Move rows out of the unique-date namespace before assigning new dates.
                cur.execute("UPDATE biology_personal_preparations SET target_date=target_date+10000 WHERE user_id=%s AND target_date>=CURRENT_DATE;",(user_id,))
                cursor=first-timedelta(days=1)
                for r in future:
                    while True:
                        cursor += timedelta(days=1)
                        if cursor.weekday() in days: break
                    cur.execute("UPDATE biology_personal_preparations SET target_date=%s WHERE id=%s;",(cursor,r["id"]))
            elif st.get("study_track")=="chapter":
                # Recreate the personal plan from the current chapter if no future slots exist.
                from data import CHAPTER_PREPARATION_DISTRIBUTION
                ch=int(st.get("current_chapter") or 1)
                groups=CHAPTER_PREPARATION_DISTRIBUTION.get(ch,[])
                cursor=date.today()-timedelta(days=1)
                for prep_no,nums in enumerate(groups,1):
                    while True:
                        cursor += timedelta(days=1)
                        if cursor.weekday() in days: break
                    cur.execute("""INSERT INTO biology_personal_preparations(user_id,target_date,chapter,lectures,prep_no)
                        VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,target_date,chapter,prep_no) DO UPDATE SET
                        chapter=EXCLUDED.chapter,lectures=EXCLUDED.lectures,prep_no=EXCLUDED.prep_no;""",
                        (user_id,cursor,ch,",".join(map(str,nums)),prep_no))
            conn.commit(); return {"status":"ok","student":student}
    return await run(op)

async def v37_reset_schedule_to_regular(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); st=cur.fetchone()
            if not st: return {"status":"missing"}
            cur.execute("UPDATE biology_students SET schedule_mode='regular',study_days=ARRAY[1,3,5,6] WHERE user_id=%s RETURNING *;",(user_id,))
            student=cur.fetchone()
            # For personal-track students, restore the approved chapter cadence by rebuilding future slots.
            cur.execute("DELETE FROM biology_personal_preparations WHERE user_id=%s AND target_date>=CURRENT_DATE;",(user_id,))
            if student.get("study_track")=="chapter":
                from data import CHAPTER_PREPARATION_DISTRIBUTION
                start=date_today=__import__("datetime").date.today()
                cursor=start-timedelta(days=1)
                for ch in range(int(student.get("current_chapter") or 1),10):
                    groups=CHAPTER_PREPARATION_DISTRIBUTION.get(ch,[])
                    allowed={6,0,1,2,3} if ch==1 else ({6,0,1,3} if ch==2 else {6,1,3})
                    for prep_no,nums in enumerate(groups,1):
                        while True:
                            cursor+=timedelta(days=1)
                            if cursor.weekday() in allowed: break
                        cur.execute("""INSERT INTO biology_personal_preparations(user_id,target_date,chapter,lectures,prep_no)
                            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,target_date,chapter,prep_no) DO UPDATE SET
                            chapter=EXCLUDED.chapter,lectures=EXCLUDED.lectures,prep_no=EXCLUDED.prep_no;""",
                            (user_id,cursor,ch,",".join(map(str,nums)),prep_no))
            conn.commit(); return {"status":"ok","student":student}
    return await run(op)

# ========================= v33 QUESTION BANK / RISK / NOTIFICATION QUEUE =========================
async def v37_calculate_student_risk(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(warnings,0) warnings,xp FROM biology_students WHERE user_id=%s;""",(user_id,))
            s=cur.fetchone()
            if not s: return None
            cur.execute("""SELECT COUNT(*) n FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=%s
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE ts.user_id=%s AND GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline))<CURRENT_TIMESTAMP
                AND (d.id IS NULL OR d.deleted_at IS NULL)
                AND NOT EXISTS (SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id AND sub.user_id=%s);""",(user_id,user_id,user_id))
            overdue=int(cur.fetchone()["n"])
            cur.execute("""SELECT AVG(grade) avg_grade,COUNT(*) n FROM biology_submissions
                WHERE user_id=%s AND grade IS NOT NULL AND submitted_at>=CURRENT_TIMESTAMP-INTERVAL '30 days';""",(user_id,))
            avg=cur.fetchone()
            warning=int(s["warnings"] or 0)
            score=overdue*25+warning*10
            level="مرتفع" if score>=60 else "متوسط" if score>=25 else "منخفض"
            return {"level":level,"score":score,"overdue":overdue,"warnings":warning,
                    "average":float(avg["avg_grade"]) if avg and avg["avg_grade"] is not None else None,"xp":int(s["xp"] or 0)}
    return await run(op)

async def v37_student_dashboard(user_id):
    risk=await v37_calculate_student_risk(user_id)
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT
                (SELECT COUNT(*) FROM biology_lecture_progress WHERE user_id=%s AND completed_at IS NOT NULL) lectures,
                (SELECT COUNT(*) FROM biology_submissions WHERE user_id=%s) submissions,
                (SELECT COALESCE(SUM(delta),0) FROM biology_xp_log WHERE user_id=%s) xp_earned;""",(user_id,user_id,user_id))
            r=cur.fetchone()
            return {"lectures":int(r["lectures"] or 0),"submissions":int(r["submissions"] or 0),
                    "xp_earned":int(r["xp_earned"] or 0),"risk":risk}
    return await run(op)

async def v37_add_question(chapter, question, answer="", difficulty="medium", source=""):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_question_bank(chapter,question,answer,difficulty,source)
                VALUES(%s,%s,%s,%s,%s) RETURNING *;""",(chapter,question,answer,difficulty,source))
            r=cur.fetchone(); conn.commit(); return r
    return await run(op)

async def v37_record_question_attempt(user_id,question_id,correct,seconds=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_question_attempts(user_id,question_id,correct,response_seconds)
                VALUES(%s,%s,%s,%s) RETURNING *;""",(user_id,question_id,bool(correct),seconds))
            r=cur.fetchone(); conn.commit(); return r
    return await run(op)

async def v37_enqueue_notification(user_id,kind,title,body,priority="normal",dedupe_key=None):
    row=await v28_notification(user_id,kind,title,body,priority=priority,dedupe_key=dedupe_key)
    if not row: return None
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_notification_queue(notification_id,user_id,status)
                VALUES(%s,%s,'pending') ON CONFLICT(notification_id) DO NOTHING RETURNING *;""",(row["id"],user_id))
            q=cur.fetchone(); conn.commit(); return q
    await run(op)
    return row

_v37_prev_init_db_2=init_db
def init_db():
    _v37_prev_init_db_2()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS biology_question_bank(
            id BIGSERIAL PRIMARY KEY,chapter INTEGER NOT NULL,question TEXT NOT NULL,answer TEXT NOT NULL DEFAULT '',
            difficulty TEXT NOT NULL DEFAULT 'medium',source TEXT NOT NULL DEFAULT '',active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);""")
        cur.execute("""CREATE TABLE IF NOT EXISTS biology_question_attempts(
            id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL,question_id BIGINT NOT NULL REFERENCES biology_question_bank(id) ON DELETE CASCADE,
            correct BOOLEAN NOT NULL,response_seconds INTEGER,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);""")
        cur.execute("""CREATE TABLE IF NOT EXISTS biology_student_risk(
            user_id BIGINT PRIMARY KEY,level TEXT,score INTEGER NOT NULL DEFAULT 0,overdue INTEGER NOT NULL DEFAULT 0,
            warnings INTEGER NOT NULL DEFAULT 0,average NUMERIC,updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);""")
        # CREATE TABLE IF NOT EXISTS does not repair a table created by an older
        # release.  Some production databases had a legacy queue without
        # notification_id, which made the delivery job fail on every run.  Keep
        # the legacy rows under a timestamped backup name, then create the
        # canonical outbox.  The notifications themselves remain untouched in
        # biology_notifications and are still visible in the user's inbox.
        cur.execute("""
        DO $migration$
        DECLARE
            backup_table TEXT;
        BEGIN
            IF to_regclass(current_schema() || '.biology_notification_queue') IS NOT NULL
               AND EXISTS (
                   SELECT 1
                   FROM (VALUES ('id'),('notification_id'),('user_id'),('status'),
                                ('attempts'),('last_error'),('created_at'),('sent_at')) AS required(column_name)
                   WHERE NOT EXISTS (
                       SELECT 1 FROM information_schema.columns c
                       WHERE c.table_schema=current_schema()
                         AND c.table_name='biology_notification_queue'
                         AND c.column_name=required.column_name
                   )
               ) THEN
                backup_table='biology_notification_queue_legacy_' ||
                             to_char(clock_timestamp(),'YYYYMMDDHH24MISSMS');
                EXECUTE format('ALTER TABLE %I.%I RENAME TO %I',
                               current_schema(),'biology_notification_queue',backup_table);
            END IF;
        END
        $migration$;

        CREATE TABLE IF NOT EXISTS biology_notification_queue(
            id BIGSERIAL PRIMARY KEY,
            notification_id BIGINT NOT NULL UNIQUE REFERENCES biology_notifications(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMPTZ
        );
        CREATE UNIQUE INDEX IF NOT EXISTS biology_notification_queue_notice_uq
            ON biology_notification_queue(notification_id);
        CREATE INDEX IF NOT EXISTS biology_notification_queue_due_idx
            ON biology_notification_queue(status,attempts,created_at);
        """)
        cur.execute("""CREATE TABLE IF NOT EXISTS biology_audit_v33(
            id BIGSERIAL PRIMARY KEY,actor_id BIGINT NOT NULL,action TEXT NOT NULL,entity_type TEXT,
            entity_id TEXT,details TEXT,created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);""")
        conn.commit()


# ========================= v39 PRODUCTION SERVICES =========================
async def v39_free_exam_extension(task_id,user_id,hours=24):
    """Grant one free extension per ISO week, atomically and per student."""
    def op():
        h=max(1,min(24,int(hours)))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS biology_free_exam_extensions(
                user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
                week_start DATE NOT NULL,task_id INTEGER NOT NULL REFERENCES biology_tasks(id) ON DELETE CASCADE,
                extended_until TIMESTAMPTZ NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id,week_start));""")
            cur.execute("""SELECT t.* FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.id=%s AND t.kind='exam' AND t.closed=FALSE
                  AND (d.id IS NULL OR d.deleted_at IS NULL) FOR UPDATE;""",(user_id,task_id)); task=cur.fetchone()
            if not task: return {"status":"not_found"}
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(task_id,user_id))
            if cur.fetchone(): return {"status":"submitted"}
            cur.execute("SELECT (CURRENT_DATE-(EXTRACT(ISODOW FROM CURRENT_DATE)::INTEGER-1))::DATE AS week_start,CURRENT_TIMESTAMP AS now;")
            clock=cur.fetchone(); week_start=clock["week_start"]
            cur.execute("SELECT 1 FROM biology_free_exam_extensions WHERE user_id=%s AND week_start=%s FOR UPDATE;",(user_id,week_start))
            if cur.fetchone(): return {"status":"used"}
            cur.execute("SELECT extended_until FROM biology_task_extensions WHERE task_id=%s AND user_id=%s FOR UPDATE;",(task_id,user_id)); ext=cur.fetchone()
            base=max(task["deadline"],ext["extended_until"] if ext else task["deadline"],clock["now"]); until=base+timedelta(hours=h)
            cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,requested_at,extended_until)
                VALUES(%s,%s,CURRENT_TIMESTAMP,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET
                requested_at=CURRENT_TIMESTAMP,extended_until=EXCLUDED.extended_until;""",(task_id,user_id,until))
            cur.execute("INSERT INTO biology_free_exam_extensions(user_id,week_start,task_id,extended_until) VALUES(%s,%s,%s,%s);",(user_id,week_start,task_id,until))
            conn.commit(); return {"status":"ok","extended_until":until,"task":task}
    return await run(op)


async def v39_student_calendar(user_id,start_date,end_date):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track,current_chapter,schedule_mode,study_days FROM biology_students WHERE user_id=%s;",(user_id,)); student=cur.fetchone()
            if not student: return {"student":None,"preparations":[]}
            cur.execute("""SELECT target_date,chapter,lectures,prep_no FROM biology_personal_preparations
                WHERE user_id=%s AND target_date BETWEEN %s AND %s ORDER BY target_date,prep_no,id;""",(user_id,start_date,end_date))
            return {"student":student,"preparations":cur.fetchall()}
    return await run(op)


async def v39_due_notification_queue(limit=100):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT q.id AS queue_id,q.attempts,n.* FROM biology_notification_queue q
                JOIN biology_notifications n ON n.id=q.notification_id
                WHERE q.status IN ('pending','failed') AND q.attempts<8
                ORDER BY CASE n.priority WHEN 'high' THEN 0 ELSE 1 END,q.created_at
                LIMIT %s FOR UPDATE OF q SKIP LOCKED;""",(max(1,min(500,int(limit))),))
            return cur.fetchall()
    return await run(op)


async def v39_mark_notification_delivery(queue_id,sent,error=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE biology_notification_queue SET status=%s,attempts=attempts+1,last_error=%s,
                sent_at=CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE sent_at END WHERE id=%s;""",
                ('sent' if sent else 'failed',None if sent else str(error or '')[:1000],bool(sent),queue_id)); conn.commit()
    await run(op)


async def v39_notifications(user_id,limit=30):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_notifications WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT %s;",(user_id,max(1,min(100,int(limit)))))
            return cur.fetchall()
    return await run(op)


async def v39_learning_mastery(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT chapter,COUNT(*) FILTER(WHERE completed_at IS NOT NULL) AS completed
                FROM biology_lecture_progress WHERE user_id=%s GROUP BY chapter ORDER BY chapter;""",(user_id,)); lectures={int(r['chapter']):int(r['completed']) for r in cur.fetchall()}
            cur.execute("""SELECT d.chapter,ROUND(AVG(s.grade),1) AS average
                FROM biology_submissions s JOIN biology_tasks t ON t.id=s.task_id
                JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE s.user_id=%s AND s.grade IS NOT NULL AND d.deleted_at IS NULL GROUP BY d.chapter;""",(user_id,)); grades={int(r['chapter']):float(r['average']) for r in cur.fetchall()}
            return {"lectures":lectures,"grades":grades}
    return await run(op)


async def v39_adaptive_question(user_id,chapter=None):
    def op():
        with connect() as conn, conn.cursor() as cur:
            chapter_filter=" AND q.chapter=%s" if chapter is not None else ""
            params=[user_id]+([int(chapter)] if chapter is not None else [])
            cur.execute("""SELECT q.*,COALESCE(a.wrong,0) AS wrong,COALESCE(a.attempts,0) AS attempts
                FROM biology_question_bank q LEFT JOIN LATERAL(
                    SELECT COUNT(*) AS attempts,COUNT(*) FILTER(WHERE correct=FALSE) AS wrong,
                           MAX(created_at) AS last_attempt FROM biology_question_attempts
                    WHERE user_id=%s AND question_id=q.id) a ON TRUE
                WHERE q.active=TRUE"""+chapter_filter+"""
                ORDER BY COALESCE(a.wrong,0) DESC,COALESCE(a.attempts,0) ASC,RANDOM() LIMIT 1;""",params)
            return cur.fetchone()
    return await run(op)


async def v39_question(question_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_question_bank WHERE id=%s AND active=TRUE;",(question_id,)); return cur.fetchone()
    return await run(op)


async def v39_mistake_notebook(user_id,limit=10):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON(q.id) q.*,a.created_at FROM biology_question_attempts a
                JOIN biology_question_bank q ON q.id=a.question_id WHERE a.user_id=%s AND a.correct=FALSE
                ORDER BY q.id,a.created_at DESC LIMIT %s;""",(user_id,max(1,min(30,int(limit)))))
            return cur.fetchall()
    return await run(op)


# ========================= v41 STUDY RESET / ROYAL REVIEW / WEAKNESSES =========================

def _v41_plan_rows(start_chapter,start_date,study_days=None):
    """Build a complete chapter plan from the selected chapter to chapter 9."""
    from data import CHAPTER_PREPARATION_DISTRIBUTION
    rows=[]; cursor=start_date
    for chapter in range(int(start_chapter),10):
        groups=CHAPTER_PREPARATION_DISTRIBUTION.get(chapter,[])
        allowed=set(study_days or ({6,0,1,2,3} if chapter==1 else ({6,0,1,3} if chapter==2 else {6,1,3})))
        for prep_no,nums in enumerate(groups,1):
            while cursor.weekday() not in allowed:
                cursor += timedelta(days=1)
            rows.append((cursor,chapter,",".join(map(str,nums)),prep_no))
            cursor += timedelta(days=1)
    return rows


def _v41_clear_student_specific_exam_tasks(cur,user_id,start_chapter=None):
    params=[f"student:{user_id}"]
    if start_chapter is None:
        chapter_filter=" AND d.target_scope='chapter' AND NOT EXISTS(SELECT 1 FROM biology_submissions keep_sub WHERE keep_sub.task_id=t.id AND keep_sub.user_id=%s)"
        params.append(user_id)
    else:
        chapter_filter=" AND d.target_scope='chapter' AND (d.chapter>=%s OR NOT EXISTS(SELECT 1 FROM biology_submissions keep_sub WHERE keep_sub.task_id=t.id AND keep_sub.user_id=%s))"
        params.append(int(start_chapter))
        params.append(user_id)
    cur.execute("""SELECT t.id FROM biology_tasks t
        JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
        WHERE t.target_scope=%s"""+chapter_filter+" FOR UPDATE;",params)
    task_ids=[int(r["id"]) for r in cur.fetchall()]
    if task_ids:
        cur.execute("DELETE FROM biology_notifications WHERE user_id=%s AND entity_type='exam' AND entity_id=ANY(%s);",(user_id,task_ids))
        cur.execute("DELETE FROM biology_tasks WHERE id=ANY(%s);",(task_ids,))
    return task_ids


def _v41_reset_chapter_state(cur,user_id,start_chapter,start_date,plan_rows=None):
    """Reset curriculum state, not merely future dates, for an intentional chapter restart."""
    chapter=int(start_chapter)
    cur.execute("DELETE FROM biology_personal_preparations WHERE user_id=%s;",(user_id,))
    cur.execute("DELETE FROM biology_scheduled_tasks WHERE published=FALSE AND linked_student_id=%s;",(user_id,))
    _v41_clear_student_specific_exam_tasks(cur,user_id,chapter)
    cur.execute("DELETE FROM biology_lecture_progress WHERE user_id=%s AND chapter>=%s;",(user_id,chapter))
    cur.execute("DELETE FROM biology_lecture_reviews WHERE user_id=%s AND chapter>=%s;",(user_id,chapter))
    cur.execute("DELETE FROM biology_task_students ts USING biology_tasks t WHERE ts.task_id=t.id AND ts.user_id=%s AND t.target_scope NOT LIKE 'student:%%';",(user_id,))
    cur.execute("UPDATE biology_tasks SET optional_practice=TRUE WHERE target_scope=%s;",(f'student:{user_id}',))
    rows=plan_rows or _v41_plan_rows(chapter,start_date)
    cur.execute("UPDATE biology_students SET start_chapter=%s,start_prep_no=%s WHERE user_id=%s;",(chapter,rows[0][3] if rows else 1,user_id))
    for target_date,ch,lectures,prep_no in rows:
        cur.execute("""INSERT INTO biology_personal_preparations
            (user_id,target_date,chapter,lectures,prep_no,notified,notified_at)
            VALUES(%s,%s,%s,%s,%s,FALSE,NULL);""",
            (user_id,target_date,ch,lectures,prep_no))


async def set_student_onboarding(user_id,study_track,current_chapter,start_date,plan_rows):
    """Save initial/migrated track; choosing a chapter means a real restart from lecture 1."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE biology_students SET onboarding_version=19,study_track=%s,
                current_chapter=%s,track_started_on=%s,last_seen=CURRENT_TIMESTAMP
                WHERE user_id=%s RETURNING *;""",(study_track,current_chapter,start_date,user_id)); student=cur.fetchone()
            if not student: return None
            _v48_activate_wallet(cur,user_id,study_track)
            if study_track=="chapter":
                _v41_reset_chapter_state(cur,user_id,current_chapter,start_date,plan_rows)
            else:
                cur.execute("DELETE FROM biology_personal_preparations WHERE user_id=%s;",(user_id,))
                cur.execute("DELETE FROM biology_scheduled_tasks WHERE published=FALSE AND linked_student_id=%s;",(user_id,))
                _v41_clear_student_specific_exam_tasks(cur,user_id)
                _v48_restore_course_state(cur,user_id)
                cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
                    SELECT t.id,%s FROM biology_tasks t WHERE t.closed=FALSE AND t.deadline>CURRENT_TIMESTAMP
                    AND t.target_scope IN ('course','all') ON CONFLICT DO NOTHING;""",(user_id,))
            conn.commit(); return student
    return await run(op)


async def v37_request_track_change(user_id,study_track,chapter,start_date,plan_rows):
    if study_track not in ("course","chapter") or (study_track=="chapter" and chapter not in range(1,6)):
        return {"status":"invalid"}
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); old=cur.fetchone()
            if not old: return {"status":"missing"}
            count=int(old.get("track_change_count") or 0)
            if count>=3:
                cur.execute("""INSERT INTO biology_track_change_requests
                    (user_id,requested_track,requested_chapter,requested_start_date,status)
                    VALUES(%s,%s,%s,%s,'pending')
                    ON CONFLICT (user_id) WHERE status='pending' DO UPDATE SET
                    requested_track=EXCLUDED.requested_track,requested_chapter=EXCLUDED.requested_chapter,
                    requested_start_date=EXCLUDED.requested_start_date,created_at=CURRENT_TIMESTAMP
                    RETURNING *;""",(user_id,study_track,chapter,start_date)); request=cur.fetchone()
                cur.execute("UPDATE biology_track_change_requests SET requested_start_prep=%s WHERE id=%s;",(plan_rows[0][3] if plan_rows else 1,request['id']))
                conn.commit()
                return {"status":"pending","request":request,"count":count}
            cur.execute("""UPDATE biology_students SET study_track=%s,current_chapter=%s,
                track_started_on=%s,track_change_count=COALESCE(track_change_count,0)+1,
                last_seen=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;""",
                (study_track,chapter,start_date,user_id)); student=cur.fetchone()
            _v48_activate_wallet(cur,user_id,study_track)
            if study_track=="chapter":
                _v41_reset_chapter_state(cur,user_id,chapter,start_date,plan_rows)
            else:
                cur.execute("DELETE FROM biology_personal_preparations WHERE user_id=%s;",(user_id,))
                cur.execute("DELETE FROM biology_scheduled_tasks WHERE published=FALSE AND linked_student_id=%s;",(user_id,))
                _v41_clear_student_specific_exam_tasks(cur,user_id)
                _v48_restore_course_state(cur,user_id)
                cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
                    SELECT t.id,%s FROM biology_tasks t WHERE t.closed=FALSE AND t.deadline>CURRENT_TIMESTAMP
                    AND t.target_scope IN ('course','all') ON CONFLICT DO NOTHING;""",(user_id,))
            cur.execute("""INSERT INTO biology_track_change_audit
                (user_id,old_track,old_chapter,new_track,new_chapter,changed_by,change_type)
                VALUES(%s,%s,%s,%s,%s,%s,'automatic');""",
                (user_id,old.get("study_track"),old.get("current_chapter"),study_track,chapter,user_id))
            conn.commit(); return {"status":"ok","count":count+1,"student":student,"reset":study_track=="chapter"}
    return await run(op)


async def v37_admin_decide_track_change(request_id,approve,admin_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_track_change_requests WHERE id=%s AND status='pending' FOR UPDATE;",(request_id,)); req=cur.fetchone()
            if not req: return {"status":"missing"}
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(req["user_id"],)); old=cur.fetchone()
            if not old: return {"status":"missing"}
            status="approved" if approve else "denied"; student=old
            if approve:
                cur.execute("""UPDATE biology_students SET study_track=%s,current_chapter=%s,
                    track_started_on=%s,last_seen=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;""",
                    (req["requested_track"],req["requested_chapter"],req["requested_start_date"],req["user_id"])); student=cur.fetchone()
                _v48_activate_wallet(cur,req["user_id"],req["requested_track"])
                if req["requested_track"]=="chapter":
                    _v41_reset_chapter_state(cur,req["user_id"],req["requested_chapter"],req["requested_start_date"],v47_plan_rows(req["requested_chapter"],req.get("requested_start_prep") or 1,req["requested_start_date"]))
                else:
                    cur.execute("DELETE FROM biology_personal_preparations WHERE user_id=%s;",(req["user_id"],))
                    cur.execute("DELETE FROM biology_scheduled_tasks WHERE published=FALSE AND linked_student_id=%s;",(req["user_id"],))
                    _v41_clear_student_specific_exam_tasks(cur,req["user_id"])
                    _v48_restore_course_state(cur,req["user_id"])
                    cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
                        SELECT t.id,%s FROM biology_tasks t WHERE t.closed=FALSE AND t.deadline>CURRENT_TIMESTAMP
                        AND t.target_scope IN ('course','all') ON CONFLICT DO NOTHING;""",(req["user_id"],))
                cur.execute("""INSERT INTO biology_track_change_audit
                    (user_id,old_track,old_chapter,new_track,new_chapter,changed_by,change_type)
                    VALUES(%s,%s,%s,%s,%s,%s,'admin_approved');""",
                    (req["user_id"],old.get("study_track"),old.get("current_chapter"),req["requested_track"],req["requested_chapter"],admin_id))
            cur.execute("""UPDATE biology_track_change_requests SET status=%s,decided_at=CURRENT_TIMESTAMP,decided_by=%s
                WHERE id=%s;""",(status,admin_id,request_id))
            conn.commit(); return {"status":status,"request":req,"student":student}
    return await run(op)


async def v41_set_study_days(user_id,days):
    """Chapter students may replace weekdays only; the required count is fixed by the chapter."""
    days=sorted({int(day) for day in days})
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(user_id,)); student=cur.fetchone()
            if not student: return {"status":"missing"}
            if student.get("study_track")!="chapter": return {"status":"course"}
            chapter=int(student.get("current_chapter") or 1)
            required=5 if chapter==1 else 4 if chapter==2 else 3
            if len(days)!=required or any(day<0 or day>6 for day in days):
                return {"status":"count","required":required}
            cur.execute("""SELECT * FROM biology_personal_preparations
                WHERE user_id=%s ORDER BY chapter,prep_no,target_date,id FOR UPDATE;""",(user_id,)); rows=cur.fetchall()
            pending=[]
            for row in rows:
                lectures=sorted({int(x) for x in (row.get("lectures") or "").split(",") if x.strip().isdigit()})
                if not lectures: continue
                cur.execute("""SELECT COUNT(*) AS n FROM biology_lecture_progress
                    WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s) AND completed_at IS NOT NULL;""",
                    (user_id,row["chapter"],lectures))
                if int(cur.fetchone()["n"])<len(lectures): pending.append(row)
            pending_ids=[int(row["id"]) for row in pending]
            occupied={row["target_date"] for row in rows if int(row["id"]) not in set(pending_ids)}
            if pending_ids:
                cur.execute("""UPDATE biology_personal_preparations
                    SET target_date=target_date+10000 WHERE id=ANY(%s);""",(pending_ids,))
                from datetime import date
                cursor=date.today()-timedelta(days=1)
                for row in pending:
                    while True:
                        cursor += timedelta(days=1)
                        if cursor.weekday() in days and cursor not in occupied: break
                    cur.execute("""UPDATE biology_personal_preparations SET target_date=%s,notified=FALSE,notified_at=NULL
                        WHERE id=%s;""",(cursor,row["id"]))
                    occupied.add(cursor)
            cur.execute("""UPDATE biology_students SET schedule_mode='custom',study_days=%s,
                schedule_change_count=COALESCE(schedule_change_count,0)+1 WHERE user_id=%s RETURNING *;""",(days,user_id)); student=cur.fetchone()
            conn.commit(); return {"status":"ok","required":required,"student":student,"pending":len(pending)}
    return await run(op)


async def v41_student_calendar(user_id,start_date,end_date):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track,current_chapter,schedule_mode,study_days FROM biology_students WHERE user_id=%s;",(user_id,)); student=cur.fetchone()
            if not student: return {"student":None,"preparations":[]}
            if student.get("study_track")=="course":
                cur.execute("""SELECT target_date,chapter,lectures,chapter_prep_no AS prep_no,published
                    FROM biology_preparations WHERE target_date BETWEEN %s AND %s
                    ORDER BY target_date,prep_no;""",(start_date,end_date))
            else:
                cur.execute("""SELECT target_date,chapter,lectures,prep_no,notified AS published
                    FROM biology_personal_preparations WHERE user_id=%s AND target_date BETWEEN %s AND %s
                    ORDER BY target_date,prep_no,id;""",(user_id,start_date,end_date))
            return {"student":student,"preparations":cur.fetchall()}
    return await run(op)


async def v41_ensure_review_plan(user_id,chapter,lecture):
    student=await get_student(user_id)
    if student and v47_before_start(student,chapter,lecture): return False
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT completed_at FROM biology_lecture_progress
                WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL;""",(user_id,chapter,lecture)); progress=cur.fetchone()
            if not progress: return False
            for stage,delay in ((1,"6 hours"),(2,"24 hours"),(3,"7 days"),(4,"30 days")):
                cur.execute("""INSERT INTO biology_lecture_reviews
                    (user_id,chapter,lecture,stage,lecture_completed_at,due_at)
                    VALUES(%s,%s,%s,%s,%s,%s::timestamptz+(%s)::interval)
                    ON CONFLICT(user_id,chapter,lecture,stage) DO NOTHING;""",
                    (user_id,chapter,lecture,stage,progress["completed_at"],progress["completed_at"],delay))
            conn.commit(); return True
    return await run(op)


_v41_previous_mark_lecture_progress=mark_lecture_progress
async def mark_lecture_progress(user_id,chapter,lecture,completed=False,completion_method="bot_lecture"):
    row=await _v41_previous_mark_lecture_progress(user_id,chapter,lecture,completed,completion_method)
    if completed and row:
        await v41_ensure_review_plan(user_id,chapter,lecture)
    return row


async def v41_review_dashboard(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT r.* FROM biology_lecture_reviews r
                WHERE r.user_id=%s AND r.completed_at IS NULL
                AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                    WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter AND earlier.lecture=r.lecture
                    AND earlier.stage<r.stage AND earlier.completed_at IS NULL)
                ORDER BY (r.due_at<=CURRENT_TIMESTAMP) DESC,r.due_at,r.chapter,r.lecture;""",(user_id,)); pending=cur.fetchall()
            cur.execute("SELECT COUNT(*) AS n FROM biology_lecture_reviews WHERE user_id=%s AND completed_at IS NOT NULL;",(user_id,)); completed=int(cur.fetchone()["n"])
            return {"pending":pending,"completed":completed}
    return await run(op)


async def v41_review_item(user_id,review_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT *,due_at<=CURRENT_TIMESTAMP AS due FROM biology_lecture_reviews WHERE id=%s AND user_id=%s;",(review_id,user_id)); return cur.fetchone()
    return await run(op)


async def v41_complete_review(user_id,review_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_lecture_reviews WHERE id=%s AND user_id=%s FOR UPDATE;",(review_id,user_id)); row=cur.fetchone()
            if not row: return {"status":"missing"}
            if row.get("completed_at"): return {"status":"done","review":row}
            if row["due_at"]>datetime_now(cur): return {"status":"early","review":row}
            cur.execute("""SELECT 1 FROM biology_lecture_reviews WHERE user_id=%s AND chapter=%s AND lecture=%s
                AND stage<%s AND completed_at IS NULL LIMIT 1;""",(user_id,row["chapter"],row["lecture"],row["stage"]))
            if cur.fetchone(): return {"status":"previous","review":row}
            cur.execute("UPDATE biology_lecture_reviews SET completed_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING *;",(review_id,)); updated=cur.fetchone()
            conn.commit(); return {"status":"ok","review":updated}
    return await run(op)


async def v41_due_review_reminders(limit=100):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON(user_id,chapter,lecture) * FROM biology_lecture_reviews r
                WHERE completed_at IS NULL AND reminded_at IS NULL AND due_at<=CURRENT_TIMESTAMP
                AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                    WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter AND earlier.lecture=r.lecture
                    AND earlier.stage<r.stage AND earlier.completed_at IS NULL)
                ORDER BY user_id,chapter,lecture,stage LIMIT %s;""",(max(1,min(500,int(limit))),)); return cur.fetchall()
    return await run(op)


async def v41_mark_review_reminded(review_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE biology_lecture_reviews SET reminded_at=CURRENT_TIMESTAMP WHERE id=%s AND reminded_at IS NULL;",(review_id,)); changed=cur.rowcount; conn.commit(); return changed==1
    return await run(op)


async def v41_weakness_counts(user_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT chapter,COUNT(*) AS n FROM biology_weakness_points
                WHERE user_id=%s AND resolved_at IS NULL GROUP BY chapter ORDER BY chapter;""",(user_id,)); return {int(r["chapter"]):int(r["n"]) for r in cur.fetchall()}
    return await run(op)


async def v41_weaknesses(user_id,chapter=None,lecture=None):
    def op():
        clauses=["user_id=%s","resolved_at IS NULL"]; params=[user_id]
        if chapter is not None: clauses.append("chapter=%s"); params.append(int(chapter))
        if lecture is not None: clauses.append("lecture=%s"); params.append(int(lecture))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_weakness_points WHERE "+" AND ".join(clauses)+" ORDER BY chapter,lecture,created_at,id;",params); return cur.fetchall()
    return await run(op)


async def v41_add_weakness(user_id,chapter,lecture,text):
    clean=" ".join(str(text or "").split()).strip()
    if not clean: return None
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_weakness_points(user_id,chapter,lecture,weakness_text)
                VALUES(%s,%s,%s,%s) RETURNING *;""",(user_id,int(chapter),int(lecture),clean[:1500])); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def v41_weakness_item(user_id,weakness_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_weakness_points WHERE id=%s AND user_id=%s;",(weakness_id,user_id)); return cur.fetchone()
    return await run(op)


async def v41_resolve_weakness(user_id,weakness_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_weakness_points WHERE id=%s AND user_id=%s FOR UPDATE;",(weakness_id,user_id)); row=cur.fetchone()
            if not row: return {"status":"missing"}
            if row.get("resolved_at"): return {"status":"done","weakness":row,"xp":0}
            cur.execute("UPDATE biology_weakness_points SET resolved_at=CURRENT_TIMESTAMP WHERE id=%s;",(weakness_id,))
            xp=_set_xp_event(cur,user_id,5,"حل نقطة ضعف",f"weakness:{weakness_id}:{user_id}")
            conn.commit(); return {"status":"ok","weakness":row,"xp":max(0,int(xp))}
    return await run(op)


_v41_previous_init_db=init_db
def init_db():
    _v41_previous_init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS biology_lecture_reviews(
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
            lecture INTEGER NOT NULL,
            stage INTEGER NOT NULL CHECK(stage BETWEEN 1 AND 4),
            lecture_completed_at TIMESTAMPTZ NOT NULL,
            due_at TIMESTAMPTZ NOT NULL,
            reminded_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id,chapter,lecture,stage)
        );
        CREATE INDEX IF NOT EXISTS biology_lecture_reviews_due_idx
            ON biology_lecture_reviews(completed_at,reminded_at,due_at);
        CREATE TABLE IF NOT EXISTS biology_weakness_points(
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
            lecture INTEGER NOT NULL,
            weakness_text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS biology_weakness_points_active_idx
            ON biology_weakness_points(user_id,chapter,lecture,created_at) WHERE resolved_at IS NULL;
        INSERT INTO biology_lecture_reviews
            (user_id,chapter,lecture,stage,lecture_completed_at,due_at,reminded_at)
        SELECT p.user_id,p.chapter,p.lecture,s.stage,p.completed_at,p.completed_at+s.delay,
               CASE WHEN p.completed_at+s.delay<=CURRENT_TIMESTAMP THEN CURRENT_TIMESTAMP ELSE NULL END
        FROM biology_lecture_progress p
        CROSS JOIN (VALUES (1,INTERVAL '6 hours'),(2,INTERVAL '24 hours'),
                           (3,INTERVAL '7 days'),(4,INTERVAL '30 days')) AS s(stage,delay)
        WHERE p.completed_at IS NOT NULL
        ON CONFLICT(user_id,chapter,lecture,stage) DO NOTHING;
        """)
        conn.commit()


# ========================= v42 RELIABLE TRACKS AND EXAMS =========================
# These definitions are intentionally last: Python imports the authoritative
# versions below and old compatibility layers can no longer shadow the fixes.

async def v41_weaknesses(user_id,chapter=None,lecture=None):
    """List open weaknesses without untyped nullable SQL parameters.

    PostgreSQL cannot infer the type of a placeholder used only by ``IS NULL``.
    Building the optional predicates explicitly fixes the production error
    ``could not determine data type of parameter $4``.
    """
    def op():
        clauses=["user_id=%s","resolved_at IS NULL"]
        params=[int(user_id)]
        if chapter is not None:
            clauses.append("chapter=%s"); params.append(int(chapter))
        if lecture is not None:
            clauses.append("lecture=%s"); params.append(int(lecture))
        sql="SELECT * FROM biology_weakness_points WHERE "+" AND ".join(clauses)+" ORDER BY chapter,lecture,created_at,id;"
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql,params); return cur.fetchall()
    return await run(op)


async def v29_course_exam_release_at(definition_id):
    """Release a course exam exactly 6 hours after its linked preparation(s).

    The relation is the immutable chapter/preparation selection made by the
    teacher, never whichever preparation happens to be current later.
    """
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT MAX(p.published_at) AS published_at
                FROM biology_linked_exam_preparations lp
                JOIN biology_preparations p
                  ON p.chapter=lp.chapter AND p.chapter_prep_no=lp.prep_no
                WHERE lp.definition_id=%s AND p.published=TRUE;""",(int(definition_id),))
            row=cur.fetchone(); published_at=row.get("published_at") if row else None
            return published_at+timedelta(hours=6) if published_at else None
    return await run(op)


async def student_exam_lock(user_id):
    """Return only a blocking exam that belongs to the student's current track."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.* FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students st ON st.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s AND t.closed=FALSE
                  AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub
                      WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL)
                  AND (
                    (d.target_scope='course' AND st.study_track='course') OR
                    (d.target_scope='chapter' AND st.study_track='chapter' AND d.chapter=st.current_chapter) OR
                    (d.id IS NULL AND (t.target_scope='all' OR
                       (st.study_track='course' AND t.target_scope='course') OR
                       (st.study_track='chapter' AND t.target_scope='chapter_'||st.current_chapter)))
                  )
                ORDER BY CASE WHEN t.exam_pending_activation THEN 0 ELSE 1 END,t.created_at,t.id LIMIT 1;""",
                (int(user_id),int(user_id)))
            return cur.fetchone()
    return await run(op)


async def v28_student_exam_tasks(user_id):
    """Open exams for the current track only; stale assignments stay archived."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.* FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students st ON st.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s
                  AND (t.closed=FALSE OR t.exam_pending_activation=TRUE)
                  AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND (
                    (d.target_scope='course' AND st.study_track='course') OR
                    (d.target_scope='chapter' AND st.study_track='chapter' AND d.chapter=st.current_chapter) OR
                    (d.id IS NULL AND (t.target_scope='all' OR
                       (st.study_track='course' AND t.target_scope='course') OR
                       (st.study_track='chapter' AND t.target_scope='chapter_'||st.current_chapter)))
                  )
                ORDER BY COALESCE(t.exam_available_at,t.deadline),t.id DESC;""",(int(user_id),))
            return cur.fetchall()
    return await run(op)


async def v39_free_exam_extension(task_id,user_id,hours=24):
    """Grant the weekly extension atomically without locking the nullable join."""
    def op():
        h=max(1,min(24,int(hours)))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.* FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.id=%s AND t.kind='exam' AND t.closed=FALSE
                  AND (d.id IS NULL OR d.deleted_at IS NULL) FOR UPDATE OF t;""",(int(user_id),int(task_id)))
            task=cur.fetchone()
            if not task: return {"status":"not_found"}
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(int(task_id),int(user_id)))
            if cur.fetchone(): return {"status":"submitted"}
            cur.execute("SELECT (CURRENT_DATE-(EXTRACT(ISODOW FROM CURRENT_DATE)::INTEGER-1))::DATE AS week_start,CURRENT_TIMESTAMP AS now;")
            clock=cur.fetchone(); week_start=clock["week_start"]
            cur.execute("SELECT 1 FROM biology_free_exam_extensions WHERE user_id=%s AND week_start=%s FOR UPDATE;",(int(user_id),week_start))
            if cur.fetchone(): return {"status":"used"}
            cur.execute("SELECT extended_until FROM biology_task_extensions WHERE task_id=%s AND user_id=%s FOR UPDATE;",(int(task_id),int(user_id)))
            ext=cur.fetchone(); base=max(task["deadline"],ext["extended_until"] if ext else task["deadline"],clock["now"]); until=base+timedelta(hours=h)
            cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,requested_at,extended_until)
                VALUES(%s,%s,CURRENT_TIMESTAMP,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET
                requested_at=CURRENT_TIMESTAMP,extended_until=EXCLUDED.extended_until;""",(int(task_id),int(user_id),until))
            cur.execute("INSERT INTO biology_free_exam_extensions(user_id,week_start,task_id,extended_until) VALUES(%s,%s,%s,%s);",(int(user_id),week_start,int(task_id),until))
            conn.commit(); return {"status":"ok","extended_until":until,"task":task}
    return await run(op)


async def v42_review_context(user_id):
    """Return reviews in curriculum order plus course preparation groups."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track,current_chapter FROM biology_students WHERE user_id=%s;",(int(user_id),)); student=cur.fetchone()
            if not student: return {"student":None,"pending":[],"preparations":[],"completed":0}
            params=[int(user_id)]
            chapter_filter=""
            if student.get("study_track")=="chapter":
                chapter_filter=" AND r.chapter=%s"; params.append(int(student.get("current_chapter") or 1))
            cur.execute("""SELECT r.*,r.due_at<=CURRENT_TIMESTAMP AS due FROM biology_lecture_reviews r
                WHERE r.user_id=%s AND r.completed_at IS NULL"""+chapter_filter+"""
                  AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                    WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter AND earlier.lecture=r.lecture
                      AND earlier.stage<r.stage AND earlier.completed_at IS NULL)
                ORDER BY r.chapter,r.lecture,r.stage;""",params)
            pending=cur.fetchall()
            cur.execute("SELECT COUNT(*) AS n FROM biology_lecture_reviews WHERE user_id=%s AND completed_at IS NOT NULL;",(int(user_id),)); completed=int(cur.fetchone()["n"])
            cur.execute("""SELECT prep_no,chapter,chapter_prep_no,lectures,target_date,published,published_at
                FROM biology_preparations ORDER BY target_date,prep_no;""")
            return {"student":student,"pending":pending,"preparations":cur.fetchall(),"completed":completed}
    return await run(op)


async def v42_exam_bank_catalog(user_id,chapter):
    """Chapter exam bank for course students; it never blocks their course prep."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track FROM biology_students WHERE user_id=%s AND approved=TRUE;",(int(user_id),)); student=cur.fetchone()
            if not student: return []
            cur.execute("""SELECT d.* FROM biology_linked_exam_definitions d
                WHERE d.chapter=%s AND d.target_scope='chapter' AND d.deleted_at IS NULL ORDER BY d.id;""",(int(chapter),)); definitions=cur.fetchall(); result=[]
            for definition in definitions:
                cur.execute("SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s ORDER BY position;",(definition["id"],)); required={(int(x["chapter"]),int(x["lecture"])) for x in cur.fetchall()}
                cur.execute("SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s ORDER BY position;",(definition["id"],))
                for pair in cur.fetchall():
                    cur.execute("SELECT lectures FROM biology_preparations WHERE chapter=%s AND chapter_prep_no=%s ORDER BY prep_no DESC LIMIT 1;",(pair["chapter"],pair["prep_no"])); prep=cur.fetchone()
                    for raw in str((prep or {}).get("lectures") or "").split(','):
                        if raw.strip().isdigit(): required.add((int(pair["chapter"]),int(raw)))
                done=0
                for ch,lecture in required:
                    cur.execute("SELECT 1 FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL;",(int(user_id),ch,lecture))
                    done+=1 if cur.fetchone() else 0
                cur.execute("""SELECT t.*,(SELECT submitted_at FROM biology_submissions s WHERE s.task_id=t.id AND s.user_id=%s) AS student_submitted_at
                    FROM biology_tasks t WHERE t.exam_definition_id=%s AND t.target_scope=%s ORDER BY t.id DESC LIMIT 1;""",(int(user_id),definition["id"],f"student:{int(user_id)}")); task=cur.fetchone()
                item=dict(definition); item.update(required_lectures=len(required),completed_lectures=done,ready=bool(required and done==len(required)),task=task); result.append(item)
            return result
    return await run(op)


async def v42_admin_exam_definitions():
    """One admin row per published definition, regardless of student count."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.*,
                    COUNT(DISTINCT ts.user_id) AS student_count,
                    COUNT(DISTINCT t.id) AS internal_task_count,
                    BOOL_OR(t.closed=FALSE) AS has_open
                FROM biology_linked_exam_definitions d
                LEFT JOIN biology_tasks t ON t.exam_definition_id=d.id AND t.kind='exam'
                LEFT JOIN biology_task_students ts ON ts.task_id=t.id
                WHERE d.deleted_at IS NULL
                GROUP BY d.id ORDER BY d.id DESC;"""); return cur.fetchall()
    return await run(op)


# ========================= v49 Neon-safe storage and answer cleanup =========================


async def v49_installation_status():
    """Record this database generation without ever deleting an existing installation."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT value FROM biology_settings WHERE key='v49_installation_started_at';")
            marker=cur.fetchone()
            cur.execute("""SELECT
                    (SELECT COUNT(*) FROM biology_students) AS students,
                    (SELECT COUNT(*) FROM biology_tasks) AS tasks,
                    (SELECT COUNT(*) FROM biology_submissions) AS submissions;""")
            counts=cur.fetchone()
            first_boot=marker is None
            empty=not any(int(counts[key] or 0) for key in ('students','tasks','submissions'))
            return {'first_boot':first_boot,'fresh':first_boot and empty,
                    'students':int(counts['students'] or 0),'tasks':int(counts['tasks'] or 0),
                    'submissions':int(counts['submissions'] or 0)}
    return await run(op)


async def v49_prepare_fresh_database():
    """Silently retire past preparation dates on a genuinely empty new database."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT
                    (SELECT COUNT(*) FROM biology_students) AS students,
                    (SELECT COUNT(*) FROM biology_tasks) AS tasks,
                    (SELECT COUNT(*) FROM biology_submissions) AS submissions;""")
            counts=cur.fetchone()
            if any(int(counts[key] or 0) for key in ('students','tasks','submissions')):
                return {'status':'not_empty','preparations':0}
            cur.execute("""UPDATE biology_preparations SET published=TRUE,
                    published_at=((target_date-1)+TIME '23:00') AT TIME ZONE 'Asia/Baghdad'
                WHERE published=FALSE
                  AND target_date<=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                RETURNING prep_no;""")
            changed=len(cur.fetchall())
            cur.execute("""INSERT INTO biology_settings(key,value)
                VALUES('v49_installation_started_at',clock_timestamp()::TEXT)
                ON CONFLICT(key) DO NOTHING;""")
            conn.commit()
            return {'status':'fresh','preparations':changed}
    return await run(op)


async def v49_mark_installation_initialized():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_settings(key,value)
                VALUES('v49_installation_started_at',clock_timestamp()::TEXT)
                ON CONFLICT(key) DO NOTHING RETURNING key;""")
            changed=bool(cur.fetchone()); conn.commit(); return changed
    return await run(op)


async def v49_answer_cleanup_preview(lookback_days=7):
    days=max(1,min(31,int(lookback_days)))
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT
                    COUNT(*) FILTER (WHERE status='delivered') AS delivered,
                    COUNT(*) FILTER (WHERE status='pending') AS pending_protected,
                    COUNT(*) FILTER (WHERE status='delivered' AND student_message_delete_status IN ('pending','failed')) AS delete_candidates,
                    COUNT(*) FILTER (WHERE status='delivered' AND student_message_delete_status='deleted') AS deleted,
                    COUNT(*) FILTER (WHERE status='delivered' AND student_message_delete_status='expired') AS expired,
                    COUNT(*) FILTER (WHERE status='delivered' AND payload_purged_at IS NULL AND file_id<>'') AS payloads_to_purge
                FROM biology_submission_delivery_outbox
                WHERE created_at>=CURRENT_TIMESTAMP-(%s::INTEGER*INTERVAL '1 day');""",(days,))
            row=cur.fetchone() or {}
            return {key:int(row.get(key) or 0) for key in
                    ('delivered','pending_protected','delete_candidates','deleted','expired','payloads_to_purge')}
    return await run(op)


async def v49_answer_cleanup_candidates(lookback_days=7,limit=500):
    days=max(1,min(31,int(lookback_days))); maximum=max(1,min(1000,int(limit)))
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT id,user_id,student_message_id,created_at
                FROM biology_submission_delivery_outbox
                WHERE status='delivered' AND student_message_delete_status IN ('pending','failed')
                  AND created_at>=CURRENT_TIMESTAMP-(%s::INTEGER*INTERVAL '1 day')
                ORDER BY created_at,id LIMIT %s::INTEGER;""",(days,maximum))
            return cur.fetchall()
    return await run(op)


async def v49_mark_answer_message_delete(delivery_id,status,error=None):
    if status not in ('deleted','expired','failed'): raise ValueError('invalid delete status')
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_submission_delivery_outbox SET
                    student_message_delete_status=%s::TEXT,
                    student_message_deleted_at=CASE WHEN %s::TEXT='deleted' THEN CURRENT_TIMESTAMP ELSE student_message_deleted_at END,
                    student_message_delete_error=%s::TEXT
                WHERE id=%s::BIGINT AND status='delivered' RETURNING id;""",
                (status,status,str(error)[:500] if error else None,int(delivery_id)))
            changed=bool(cur.fetchone()); conn.commit(); return changed
    return await run(op)


async def v49_finish_answer_cleanup(admin_id,lookback_days,candidates,deleted,expired,failed,pending_protected):
    """Purge only delivered Telegram file references; grades, XP, warnings and group copies remain."""
    days=max(1,min(31,int(lookback_days)))
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_submission_delivery_outbox SET file_id='',payload_purged_at=CURRENT_TIMESTAMP
                WHERE status='delivered' AND payload_purged_at IS NULL AND file_id<>''
                  AND created_at>=CURRENT_TIMESTAMP-(%s::INTEGER*INTERVAL '1 day')
                RETURNING id;""",(days,))
            purged=len(cur.fetchall())
            cur.execute("""INSERT INTO biology_answer_cleanup_runs
                    (admin_id,lookback_days,candidates,deleted_messages,expired_messages,failed_messages,
                     purged_payloads,pending_protected)
                VALUES(%s::BIGINT,%s::INTEGER,%s::INTEGER,%s::INTEGER,%s::INTEGER,%s::INTEGER,%s::INTEGER,%s::INTEGER)
                RETURNING id,created_at;""",
                (int(admin_id),days,int(candidates),int(deleted),int(expired),int(failed),purged,int(pending_protected)))
            audit=cur.fetchone(); conn.commit(); return {'purged':purged,'audit':audit}
    return await run(op)


async def v49_answer_cleanup_history(limit=10):
    maximum=max(1,min(50,int(limit)))
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_answer_cleanup_runs ORDER BY id DESC LIMIT %s::INTEGER;",(maximum,))
            return cur.fetchall()
    return await run(op)


# ========================= v48 reliable submission delivery =========================

async def v48_stage_submission_delivery(task_id,user_id,student_message_id,payload_type,file_id,
                                        file_unique_id,media_group_id,destination_chat_id,destination_thread_id=None):
    """Validate first and persist Telegram's reusable file id before network delivery."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;",(int(user_id),)); student=cur.fetchone()
            if not student or not student.get('approved') or student.get('reset_pending'): return {'status':'not_allowed'}
            cur.execute("""SELECT t.*,GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline)) AS effective_deadline,
                    e.extended_until,CURRENT_TIMESTAMP AS now
                FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=%s
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.id=%s AND (d.id IS NULL OR d.deleted_at IS NULL) FOR UPDATE OF t;""",
                (int(user_id),int(user_id),int(task_id))); task=cur.fetchone()
            if not task or task.get('exam_pending_activation') or task['effective_deadline']<=task['now']:
                return {'status':'expired' if task else 'not_allowed'}
            if task.get('closed') and not (task.get('extended_until') and task['extended_until']>task['now']): return {'status':'not_allowed'}
            if task.get('exam_available_at') and task['exam_available_at']>task['now']: return {'status':'not_allowed'}
            if task.get('exam_approval_required'):
                cur.execute("SELECT 1 FROM biology_exam_access WHERE task_id=%s AND user_id=%s AND status='approved';",(int(task_id),int(user_id)))
                if not cur.fetchone(): return {'status':'not_allowed'}
            cur.execute("SELECT user_id FROM biology_submission_files WHERE task_id=%s AND file_unique_id=%s;",(int(task_id),str(file_unique_id)))
            used=cur.fetchone()
            if used and int(used['user_id'])!=int(user_id): return {'status':'duplicate'}
            cur.execute("""INSERT INTO biology_submission_delivery_outbox
                    (task_id,user_id,student_message_id,payload_type,file_id,file_unique_id,media_group_id,
                     destination_chat_id,destination_thread_id,status,next_attempt_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',CURRENT_TIMESTAMP)
                ON CONFLICT(task_id,user_id,file_unique_id) DO UPDATE SET
                    student_message_id=EXCLUDED.student_message_id,payload_type=EXCLUDED.payload_type,
                    file_id=EXCLUDED.file_id,media_group_id=EXCLUDED.media_group_id,
                    destination_chat_id=EXCLUDED.destination_chat_id,destination_thread_id=EXCLUDED.destination_thread_id,
                    status=CASE WHEN biology_submission_delivery_outbox.status='delivered' THEN 'delivered' ELSE 'pending' END,
                    next_attempt_at=CURRENT_TIMESTAMP,last_error=NULL
                RETURNING *;""",(int(task_id),int(user_id),int(student_message_id),str(payload_type),str(file_id),
                    str(file_unique_id),media_group_id,int(destination_chat_id),destination_thread_id)); row=cur.fetchone()
            conn.commit(); return {'status':'delivered' if row['status']=='delivered' else 'ok','delivery':row,'task':task}
    return await run(op)


async def v48_mark_delivery_sent(delivery_id,message_id,thread_id=None):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_submission_delivery_outbox SET delivered_message_id=%s,
                destination_thread_id=COALESCE(%s::BIGINT,destination_thread_id),attempts=attempts+1,
                last_error=NULL,next_attempt_at=CURRENT_TIMESTAMP WHERE id=%s AND status='pending' RETURNING *;""",
                (int(message_id),thread_id,int(delivery_id))); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def v48_mark_delivery_retry(delivery_id,error,retry_seconds=60):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_submission_delivery_outbox SET attempts=attempts+1,last_error=%s,
                next_attempt_at=CURRENT_TIMESTAMP+(%s || ' seconds')::INTERVAL
                WHERE id=%s AND status='pending' RETURNING *;""",
                (str(error)[:1000],max(10,min(3600,int(retry_seconds))),int(delivery_id))); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def v48_finish_submission_delivery(delivery_id,status,error=None):
    if status not in ('delivered','rejected'): raise ValueError('Invalid delivery status')
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_submission_delivery_outbox SET status=%s,last_error=%s,
                delivered_at=CASE WHEN %s='delivered' THEN CURRENT_TIMESTAMP ELSE delivered_at END
                WHERE id=%s RETURNING *;""",(status,str(error)[:1000] if error else None,status,int(delivery_id)))
            row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def v48_pending_submission_deliveries(limit=50,max_attempts=20):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT o.*,t.title,t.kind,s.full_name,s.parent_chat_id FROM biology_submission_delivery_outbox o
                JOIN biology_tasks t ON t.id=o.task_id JOIN biology_students s ON s.user_id=o.user_id
                WHERE o.status='pending' AND o.next_attempt_at<=CURRENT_TIMESTAMP
                ORDER BY o.next_attempt_at,o.id LIMIT %s;""",(max(1,min(200,int(limit))),))
            return cur.fetchall()
    return await run(op)


async def v48_delivery_submission_registered(delivery_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT EXISTS(SELECT 1 FROM biology_submission_delivery_outbox o
                    JOIN biology_submissions s ON s.task_id=o.task_id AND s.user_id=o.user_id
                    WHERE o.id=%s AND s.submitted_at IS NOT NULL) AS registered;""",(int(delivery_id),))
            return bool(cur.fetchone()['registered'])
    return await run(op)


async def v48_mark_delivery_admin_alerted(delivery_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_submission_delivery_outbox SET admin_alerted_at=CURRENT_TIMESTAMP
                WHERE id=%s AND admin_alerted_at IS NULL RETURNING id;""",(int(delivery_id),))
            changed=bool(cur.fetchone()); conn.commit(); return changed
    return await run(op)


# ========================= v48 quick memory review bank =========================

async def v48_add_quick_review_question(chapter,prep_no,question_payload_type,question_file_id,question_text,
                                        answer_payload_type,answer_file_id,answer_text,created_by):
    if question_payload_type not in ('text','photo','document') or answer_payload_type not in ('text','photo','document'):
        return None
    if question_payload_type!='text' and not question_file_id: return None
    if answer_payload_type!='text' and not answer_file_id: return None
    if question_payload_type=='text' and not str(question_text or '').strip(): return None
    if answer_payload_type=='text' and not str(answer_text or '').strip(): return None
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_quick_review_questions
                    (chapter,prep_no,question_payload_type,question_file_id,question_text,
                     answer_payload_type,answer_file_id,answer_text,created_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *;""",
                (int(chapter),int(prep_no),question_payload_type,question_file_id,str(question_text or '')[:4000],
                 answer_payload_type,answer_file_id,str(answer_text or '')[:4000],int(created_by)))
            row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def v48_quick_review_catalog(user_id=None):
    def op():
        with connect() as conn,conn.cursor() as cur:
            student=None
            if user_id is not None:
                cur.execute("SELECT * FROM biology_students WHERE user_id=%s AND approved=TRUE AND reset_pending=FALSE;",(int(user_id),)); student=cur.fetchone()
                if not student: return []
            cur.execute("""SELECT chapter,prep_no,COUNT(*) AS question_count
                FROM biology_quick_review_questions WHERE active=TRUE
                GROUP BY chapter,prep_no ORDER BY chapter,prep_no;"""); rows=cur.fetchall()
            if student and student.get('study_track')=='chapter' and student.get('start_chapter'):
                boundary=(int(student['start_chapter']),int(student.get('start_prep_no') or 1))
                rows=[r for r in rows if (int(r['chapter']),int(r['prep_no']))>=boundary]
            return rows
    return await run(op)


async def v48_quick_review_questions(chapter,prep_no,include_inactive=False):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_quick_review_questions WHERE chapter=%s AND prep_no=%s
                AND (active=TRUE OR %s::BOOLEAN=TRUE) ORDER BY id;""",(int(chapter),int(prep_no),bool(include_inactive)))
            return cur.fetchall()
    return await run(op)


async def v48_quick_review_question(question_id,user_id=None):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_quick_review_questions WHERE id=%s AND active=TRUE;",(int(question_id),)); row=cur.fetchone()
            if not row: return None
            if user_id is not None:
                cur.execute("SELECT * FROM biology_students WHERE user_id=%s AND approved=TRUE AND reset_pending=FALSE;",(int(user_id),)); student=cur.fetchone()
                if not student: return None
                if student.get('study_track')=='chapter' and student.get('start_chapter') and (int(row['chapter']),int(row['prep_no']))<(int(student['start_chapter']),int(student.get('start_prep_no') or 1)):
                    return None
                cur.execute("""INSERT INTO biology_quick_review_attempts(question_id,user_id)
                    VALUES(%s,%s) ON CONFLICT(question_id,user_id) DO UPDATE SET opened_at=CURRENT_TIMESTAMP;""",
                    (int(question_id),int(user_id))); conn.commit()
            return row
    return await run(op)


async def v48_reveal_quick_review_answer(question_id,user_id):
    row=await v48_quick_review_question(question_id,user_id)
    if not row: return None
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_quick_review_attempts SET answer_revealed_at=CURRENT_TIMESTAMP
                WHERE question_id=%s AND user_id=%s;""",(int(question_id),int(user_id))); conn.commit()
    await run(op); return row


async def v48_retire_quick_review_question(question_id,admin_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_quick_review_questions SET active=FALSE,retired_at=CURRENT_TIMESTAMP
                WHERE id=%s AND active=TRUE RETURNING *;""",(int(question_id),)); row=cur.fetchone()
            if row:
                cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'retire_quick_review_question',%s);",
                    (int(admin_id),f"question_id={int(question_id)}"))
            conn.commit(); return row
    return await run(op)


async def v42_admin_exam_students(definition_id):
    """Resolve every student's real task under a deduplicated exam definition."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON(ts.user_id) ts.user_id,s.full_name,t.id AS task_id,t.deadline,t.closed,
                    sub.submitted_at,(SELECT e.extended_until FROM biology_task_extensions e
                        WHERE e.task_id=t.id AND e.user_id=ts.user_id) AS extended_until
                FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students s ON s.user_id=ts.user_id
                LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=ts.user_id
                WHERE t.exam_definition_id=%s AND t.kind='exam'
                ORDER BY ts.user_id,t.id DESC;""",(int(definition_id),)); return cur.fetchall()
    return await run(op)


async def v42_open_bank_exam(definition_id,user_id):
    """Open or safely reopen an unsubmitted optional chapter-bank exam."""
    task=await v29_create_or_get_exam_task(int(definition_id),int(user_id),None,False)
    if not task: return None
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(task["id"],int(user_id)))
            if cur.fetchone(): return task
            cur.execute("""UPDATE biology_tasks SET closed=FALSE,exam_pending_activation=FALSE,
                    exam_approval_required=FALSE,exam_available_at=CURRENT_TIMESTAMP,
                    published_at=COALESCE(published_at,CURRENT_TIMESTAMP),
                    deadline=CURRENT_TIMESTAMP+(exam_duration_hours||' hours')::INTERVAL
                WHERE id=%s RETURNING *;""",(task["id"],)); row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def v42_due_missing_course_exams(limit=20):
    """Preparations older than 12h that still have no linked course exam."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT p.* FROM biology_preparations p
                WHERE p.published=TRUE AND p.published_at IS NOT NULL
                  AND p.published_at<=CURRENT_TIMESTAMP-INTERVAL '12 hours'
                  AND p.published_at>=CURRENT_TIMESTAMP-INTERVAL '7 days'
                  AND NOT EXISTS(SELECT 1 FROM biology_linked_exam_preparations lp
                    JOIN biology_linked_exam_definitions d ON d.id=lp.definition_id
                    WHERE lp.chapter=p.chapter AND lp.prep_no=p.chapter_prep_no
                      AND d.target_scope='course' AND d.deleted_at IS NULL)
                  AND NOT EXISTS(SELECT 1 FROM biology_missing_exam_reminders r WHERE r.prep_no=p.prep_no)
                ORDER BY p.published_at LIMIT %s;""",(max(1,min(100,int(limit))),)); return cur.fetchall()
    return await run(op)


async def v42_mark_missing_exam_reminded(prep_no):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO biology_missing_exam_reminders(prep_no) VALUES(%s) ON CONFLICT DO NOTHING RETURNING prep_no;",(int(prep_no),)); changed=cur.fetchone() is not None; conn.commit(); return changed
    return await run(op)


_v42_previous_create_linked_exam_definition=create_linked_exam_definition
async def create_linked_exam_definition(selected_pairs,title,created_by,media,target_scope="chapter",selected_lectures=None,exam_type="normal",duration_hours=2):
    """Create an exam and snapshot the exact lectures selected by the teacher."""
    row=await _v42_previous_create_linked_exam_definition(selected_pairs,title,created_by,media,target_scope,selected_lectures,exam_type,duration_hours)
    pairs=sorted({(int(ch),int(prep)) for ch,prep in (selected_pairs or [])})
    if row and pairs:
        def op():
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT COALESCE(MAX(position),-1) AS pos FROM biology_linked_exam_lectures WHERE definition_id=%s;",(row["id"],)); pos=int(cur.fetchone()["pos"])+1
                for ch,prep_no in pairs:
                    cur.execute("SELECT lectures FROM biology_preparations WHERE chapter=%s AND chapter_prep_no=%s ORDER BY prep_no DESC LIMIT 1;",(ch,prep_no)); prep=cur.fetchone()
                    for raw in str((prep or {}).get("lectures") or "").split(','):
                        if not raw.strip().isdigit(): continue
                        cur.execute("""INSERT INTO biology_linked_exam_lectures(definition_id,chapter,lecture,position)
                            VALUES(%s,%s,%s,%s) ON CONFLICT(definition_id,chapter,lecture) DO NOTHING;""",(row["id"],ch,int(raw),pos))
                        if cur.rowcount: pos+=1
                conn.commit()
        await run(op)
    return row


_v42_previous_init_db=init_db
def init_db():
    _v42_previous_init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS biology_missing_exam_reminders(
            prep_no INTEGER PRIMARY KEY REFERENCES biology_preparations(prep_no) ON DELETE CASCADE,
            reminded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );""")
        # Snapshot old definitions once as well, so later schedule edits cannot
        # silently move an exam from lectures 11/12 to 13/14.
        cur.execute("""INSERT INTO biology_linked_exam_lectures(definition_id,chapter,lecture,position)
            SELECT lp.definition_id,lp.chapter,parsed.lecture,
                   (100000+lp.position*100+v.ordinality)::INTEGER
            FROM biology_linked_exam_preparations lp
            JOIN biology_preparations p ON p.chapter=lp.chapter AND p.chapter_prep_no=lp.prep_no
            CROSS JOIN LATERAL regexp_split_to_table(p.lectures,',') WITH ORDINALITY AS v(raw,ordinality)
            CROSS JOIN LATERAL (SELECT trim(v.raw)::INTEGER AS lecture) parsed
            WHERE trim(v.raw)~'^[0-9]+$'
              AND NOT EXISTS(SELECT 1 FROM biology_linked_exam_lectures snapshot WHERE snapshot.definition_id=lp.definition_id)
            ON CONFLICT(definition_id,chapter,lecture) DO NOTHING;""")
        conn.commit()


# ========================= v44 STUDY FLOW RELIABILITY =========================

async def v44_free_exam_extension(task_id,user_id,hours=24):
    """Use the weekly extension even when the exam already expired.

    The student row is locked first, so two fast button presses cannot consume
    the same weekly allowance twice. Reopening also clears only the warning
    generated by this exact exam and gives the student a genuinely fresh
    deadline.
    """
    def op():
        h=max(1,min(24,int(hours)))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS biology_free_exam_extensions(
                user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
                week_start DATE NOT NULL,
                task_id INTEGER NOT NULL REFERENCES biology_tasks(id) ON DELETE CASCADE,
                extended_until TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id,week_start));""")
            cur.execute("SELECT user_id FROM biology_students WHERE user_id=%s FOR UPDATE;",(int(user_id),))
            if not cur.fetchone(): return {"status":"not_found"}
            cur.execute("""SELECT t.*,s.full_name,s.parent_chat_id,CURRENT_TIMESTAMP AS now
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                JOIN biology_students s ON s.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.id=%s AND t.kind='exam' AND (d.id IS NULL OR d.deleted_at IS NULL)
                FOR UPDATE OF t;""",(int(user_id),int(task_id)))
            task=cur.fetchone()
            if not task: return {"status":"not_found"}
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(int(task_id),int(user_id)))
            if cur.fetchone(): return {"status":"submitted"}
            cur.execute("SELECT (CURRENT_DATE-(EXTRACT(ISODOW FROM CURRENT_DATE)::INTEGER-1))::DATE AS week_start;")
            week_start=cur.fetchone()["week_start"]
            cur.execute("SELECT 1 FROM biology_free_exam_extensions WHERE user_id=%s AND week_start=%s;",(int(user_id),week_start))
            if cur.fetchone(): return {"status":"used"}
            cur.execute("SELECT extended_until FROM biology_task_extensions WHERE task_id=%s AND user_id=%s FOR UPDATE;",(int(task_id),int(user_id)))
            extension=cur.fetchone()
            base=max(task["deadline"],extension["extended_until"] if extension else task["deadline"],task["now"])
            until=base+timedelta(hours=h)
            cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,requested_at,extended_until)
                VALUES(%s,%s,CURRENT_TIMESTAMP,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET
                requested_at=CURRENT_TIMESTAMP,extended_until=EXCLUDED.extended_until;""",(int(task_id),int(user_id),until))
            cur.execute("""INSERT INTO biology_free_exam_extensions(user_id,week_start,task_id,extended_until)
                VALUES(%s,%s,%s,%s);""",(int(user_id),week_start,int(task_id),until))
            cur.execute("DELETE FROM biology_exam_warning_waivers WHERE task_id=%s AND user_id=%s;",(int(task_id),int(user_id)))
            cur.execute("DELETE FROM biology_warning_log WHERE task_id=%s AND user_id=%s RETURNING id;",(int(task_id),int(user_id)))
            warnings_removed=len(cur.fetchall())
            if warnings_removed:
                cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-%s) WHERE user_id=%s;",(warnings_removed,int(user_id)))
            cur.execute("""UPDATE biology_tasks SET closed=FALSE,warned=FALSE,
                teacher_deadline_reminder_sent=FALSE,champion_announced=FALSE WHERE id=%s;""",(int(task_id),))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'weekly_free_exam_reopen',%s);",
                        (int(user_id),f"task_id={task_id};hours={h};warnings_removed={warnings_removed}"))
            conn.commit()
            return {"status":"ok","task_id":int(task_id),"title":task["title"],
                    "extended_until":until,"warnings_removed":warnings_removed,
                    "parent_chat_id":task.get("parent_chat_id"),"full_name":task.get("full_name")}
    return await run(op)


async def student_exam_lock(user_id):
    """Return an unsubmitted exam for the current track, including expired ones.

    Expiry closes submission, but it must not silently unlock the next
    preparation. The student can reopen it with the weekly extension or through
    the admin extension controls.
    """
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.* FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students st ON st.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s
                  AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub
                      WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL)
                  AND (
                    (d.target_scope='course' AND st.study_track='course') OR
                    (d.target_scope='chapter' AND st.study_track='chapter' AND d.chapter=st.current_chapter) OR
                    (d.id IS NULL AND (t.target_scope='all' OR
                       (st.study_track='course' AND t.target_scope='course') OR
                       (st.study_track='chapter' AND t.target_scope='chapter_'||st.current_chapter)))
                  )
                ORDER BY CASE WHEN t.exam_pending_activation THEN 0 WHEN t.closed THEN 1 ELSE 2 END,
                         t.created_at,t.id LIMIT 1;""",(int(user_id),int(user_id)))
            return cur.fetchone()
    return await run(op)


async def v44_queue_review_notification(review_id,title,body):
    """Persist one royal-review reminder and its outbox row atomically."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT r.* FROM biology_lecture_reviews r
                JOIN biology_students s ON s.user_id=r.user_id
                WHERE r.id=%s AND s.approved=TRUE AND r.completed_at IS NULL
                  AND r.due_at<=CURRENT_TIMESTAMP
                  AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                    WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter
                      AND earlier.lecture=r.lecture AND earlier.stage<r.stage
                      AND earlier.completed_at IS NULL)
                FOR UPDATE OF r;""",(int(review_id),))
            review=cur.fetchone()
            if not review: return {"status":"skip"}
            dedupe=f"royal-review:{int(review_id)}"
            cur.execute("""INSERT INTO biology_notifications
                    (user_id,kind,title,body,priority,entity_type,entity_id,dedupe_key)
                VALUES(%s,'royal_review',%s,%s,'high','royal_review',%s,%s)
                ON CONFLICT(dedupe_key) DO UPDATE SET dedupe_key=EXCLUDED.dedupe_key
                RETURNING id;""",(review["user_id"],str(title)[:250],str(body)[:3000],int(review_id),dedupe))
            notification_id=cur.fetchone()["id"]
            cur.execute("""INSERT INTO biology_notification_queue(notification_id,user_id,status)
                VALUES(%s,%s,'pending') ON CONFLICT(notification_id) DO NOTHING;""",(notification_id,review["user_id"]))
            cur.execute("UPDATE biology_lecture_reviews SET reminded_at=CURRENT_TIMESTAMP WHERE id=%s;",(int(review_id),))
            conn.commit(); return {"status":"ok","notification_id":notification_id,"user_id":review["user_id"]}
    return await run(op)


async def v44_rearm_legacy_review_reminders():
    """Make pre-v44 overdue reviews eligible for the new durable reminders once."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE biology_lecture_reviews r SET reminded_at=NULL
                WHERE r.completed_at IS NULL AND r.due_at<=CURRENT_TIMESTAMP
                  AND r.due_at>=CURRENT_TIMESTAMP-INTERVAL '35 days'
                  AND NOT EXISTS(SELECT 1 FROM biology_notifications n
                    WHERE n.dedupe_key='royal-review:'||r.id::TEXT);""")
            changed=cur.rowcount; conn.commit(); return changed
    return await run(op)


# ========================= v45 EXAM ENFORCEMENT CUTOVER =========================

_v45_previous_init_db=init_db
def init_db():
    _v45_previous_init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS biology_late_exam_requests(
            id BIGSERIAL PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied')),
            xp_cost INTEGER NOT NULL CHECK(xp_cost>=0),
            hours INTEGER NOT NULL CHECK(hours BETWEEN 1 AND 24),
            requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMPTZ,
            decided_by BIGINT
        );
        CREATE INDEX IF NOT EXISTS biology_late_exam_requests_status_idx
            ON biology_late_exam_requests(status,requested_at);
        CREATE UNIQUE INDEX IF NOT EXISTS biology_late_exam_requests_pending_uq
            ON biology_late_exam_requests(task_id,user_id) WHERE status='pending';""")
        conn.commit()


async def v45_configure_exam_policy(cutoff_date):
    """Persist the explicit first date whose exams can block study progress."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT %s::date AS cutoff;",(str(cutoff_date),)); cutoff=cur.fetchone()["cutoff"]
            cur.execute("""INSERT INTO biology_settings(key,value)
                VALUES('v45_exam_enforcement_cutoff',%s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;""",(cutoff.isoformat(),))
            conn.commit(); return cutoff
    return await run(op)


async def v45_retire_legacy_unsubmitted_exams(cutoff_date):
    """Retire pre-cutoff missed exams and permanently remove their warnings."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT %s::date AS cutoff;",(str(cutoff_date),)); cutoff=cur.fetchone()["cutoff"]
            cur.execute("""SELECT DISTINCT t.id AS task_id,ts.user_id
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam'
                  AND COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)<(%s::date AT TIME ZONE 'Asia/Baghdad')
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub
                    WHERE sub.task_id=t.id AND sub.user_id=ts.user_id AND sub.submitted_at IS NOT NULL);""",(cutoff,))
            retired=cur.fetchall()
            for item in retired:
                cur.execute("""INSERT INTO biology_exam_warning_waivers(task_id,user_id,waived_by)
                    VALUES(%s,%s,0) ON CONFLICT(task_id,user_id) DO UPDATE SET created_at=CURRENT_TIMESTAMP;""",
                    (item["task_id"],item["user_id"]))
            cur.execute("""DELETE FROM biology_warning_log w USING biology_tasks t
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE w.task_id=t.id AND t.kind='exam'
                  AND COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)<(%s::date AT TIME ZONE 'Asia/Baghdad')
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub
                    WHERE sub.task_id=t.id AND sub.user_id=w.user_id AND sub.submitted_at IS NOT NULL)
                RETURNING w.user_id;""",(cutoff,))
            warning_users={int(row["user_id"]) for row in cur.fetchall()}
            if warning_users:
                cur.execute("""UPDATE biology_students s SET warnings=(
                    SELECT COUNT(*) FROM biology_warning_log w WHERE w.user_id=s.user_id)
                    WHERE s.user_id=ANY(%s);""",(list(warning_users),))
            cur.execute("""UPDATE biology_tasks t SET closed=TRUE
                FROM biology_linked_exam_definitions d
                WHERE t.exam_definition_id=d.id AND t.kind='exam'
                  AND COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)<(%s::date AT TIME ZONE 'Asia/Baghdad');""",(cutoff,))
            cur.execute("""UPDATE biology_tasks t SET closed=TRUE
                WHERE t.exam_definition_id IS NULL AND t.kind='exam'
                  AND COALESCE(t.published_at,t.created_at)<(%s::date AT TIME ZONE 'Asia/Baghdad');""",(cutoff,))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(0,'retire_legacy_exams',%s);",
                        (f"cutoff={cutoff.isoformat()};assignments={len(retired)};warning_users={len(warning_users)}",))
            conn.commit(); return {"cutoff":cutoff,"retired":len(retired),"warning_users":len(warning_users)}
    return await run(op)


async def student_exam_lock(user_id):
    """Only exams published on or after the v45 cutover can block preparation."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.* FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students st ON st.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s
                  AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND (COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)>=(
                    COALESCE((SELECT value::date FROM biology_settings WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27')
                    AT TIME ZONE 'Asia/Baghdad') OR EXISTS(SELECT 1 FROM biology_task_extensions active_ext
                      WHERE active_ext.task_id=t.id AND active_ext.user_id=ts.user_id AND active_ext.extended_until>CURRENT_TIMESTAMP))
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub
                      WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL)
                  AND (
                    (d.target_scope='course' AND st.study_track='course') OR
                    (d.target_scope='chapter' AND st.study_track='chapter' AND d.chapter=st.current_chapter) OR
                    (d.id IS NULL AND (t.target_scope='all' OR
                       (st.study_track='course' AND t.target_scope='course') OR
                       (st.study_track='chapter' AND t.target_scope='chapter_'||st.current_chapter)))
                  )
                ORDER BY CASE WHEN t.exam_pending_activation THEN 0 WHEN t.closed THEN 1 ELSE 2 END,
                         COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at),t.id LIMIT 1;""",(int(user_id),int(user_id)))
            return cur.fetchone()
    return await run(op)


async def v28_student_exam_tasks(user_id):
    """List only current-policy unsubmitted exams, including expired requests."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.* FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students st ON st.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s
                  AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND (COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)>=(
                    COALESCE((SELECT value::date FROM biology_settings WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27')
                    AT TIME ZONE 'Asia/Baghdad') OR EXISTS(SELECT 1 FROM biology_task_extensions active_ext
                      WHERE active_ext.task_id=t.id AND active_ext.user_id=ts.user_id AND active_ext.extended_until>CURRENT_TIMESTAMP))
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub
                    WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL)
                  AND (
                    (d.target_scope='course' AND st.study_track='course') OR
                    (d.target_scope='chapter' AND st.study_track='chapter' AND d.chapter=st.current_chapter) OR
                    (d.id IS NULL AND (t.target_scope='all' OR
                       (st.study_track='course' AND t.target_scope='course') OR
                       (st.study_track='chapter' AND t.target_scope='chapter_'||st.current_chapter)))
                  )
                ORDER BY COALESCE(t.exam_available_at,t.deadline),t.id DESC;""",(int(user_id),int(user_id)))
            return cur.fetchall()
    return await run(op)


async def v45_exam_task_status(user_id,task_id):
    """Resolve one student's exam, its effective deadline, and cutover status."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.*,d.target_scope AS definition_scope,d.chapter AS definition_chapter,
                    (COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)>=(
                      COALESCE((SELECT value::date FROM biology_settings WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27')
                      AT TIME ZONE 'Asia/Baghdad') OR COALESCE(e.extended_until>CURRENT_TIMESTAMP,FALSE)) AS enforced,
                    ((d.target_scope='course' AND st.study_track='course') OR
                     (d.target_scope='chapter' AND st.study_track='chapter' AND d.chapter=st.current_chapter) OR
                     (d.id IS NULL AND (t.target_scope='all' OR
                       (st.study_track='course' AND t.target_scope='course') OR
                       (st.study_track='chapter' AND t.target_scope='chapter_'||st.current_chapter)))) AS track_allowed,
                    GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline)) AS effective_deadline,
                    EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL) AS submitted,
                    (SELECT r.status FROM biology_late_exam_requests r WHERE r.task_id=t.id AND r.user_id=%s ORDER BY r.id DESC LIMIT 1) AS late_request_status,
                    CURRENT_TIMESTAMP AS now
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                JOIN biology_students st ON st.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=%s
                WHERE t.id=%s AND t.kind='exam' AND (d.id IS NULL OR d.deleted_at IS NULL);""",
                (int(user_id),int(user_id),int(user_id),int(user_id),int(task_id)))
            return cur.fetchone()
    return await run(op)


async def v45_request_late_exam(task_id,user_id,xp_cost=150,hours=2):
    """Create an exact expired-exam request for teacher approval."""
    def op():
        cost=max(0,int(xp_cost)); requested_hours=max(1,min(24,int(hours)))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT t.*,s.full_name,s.xp,CURRENT_TIMESTAMP AS now,
                    GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline)) AS effective_deadline,
                    (COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)>=(
                      COALESCE((SELECT value::date FROM biology_settings WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27')
                      AT TIME ZONE 'Asia/Baghdad') OR COALESCE(e.extended_until>CURRENT_TIMESTAMP,FALSE)) AS enforced
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                JOIN biology_students s ON s.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=ts.user_id
                WHERE t.id=%s AND t.kind='exam' AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND ((d.target_scope='course' AND s.study_track='course') OR
                    (d.target_scope='chapter' AND s.study_track='chapter' AND d.chapter=s.current_chapter) OR
                    (d.id IS NULL AND (t.target_scope='all' OR
                      (s.study_track='course' AND t.target_scope='course') OR
                      (s.study_track='chapter' AND t.target_scope='chapter_'||s.current_chapter))))
                FOR UPDATE OF t,s;""",(int(user_id),int(task_id)))
            row=cur.fetchone()
            if not row or not row.get("enforced"): return {"status":"not_found"}
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(int(task_id),int(user_id)))
            if cur.fetchone(): return {"status":"submitted"}
            if not row.get("closed") and row["effective_deadline"]>row["now"]: return {"status":"open"}
            if int(row.get("xp") or 0)<cost: return {"status":"xp","required":cost,"current":int(row.get("xp") or 0)}
            cur.execute("SELECT 1 FROM biology_late_exam_requests WHERE task_id=%s AND user_id=%s AND status='pending';",(int(task_id),int(user_id)))
            if cur.fetchone(): return {"status":"exists"}
            cur.execute("""INSERT INTO biology_late_exam_requests(task_id,user_id,status,xp_cost,hours)
                VALUES(%s,%s,'pending',%s,%s) RETURNING *;""",(int(task_id),int(user_id),cost,requested_hours))
            request=cur.fetchone()
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'late_exam_requested',%s);",
                        (int(user_id),f"task_id={task_id};request_id={request['id']};xp_cost={cost}"))
            conn.commit(); return {"status":"ok","request":request,"task":row,"student_name":row["full_name"]}
    return await run(op)


async def v45_decide_late_exam_request(request_id,approved,admin_id):
    """Approve a late exam, deduct XP once, and open a fresh attempt window."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT r.*,t.title,t.exam_duration_hours,s.full_name,s.parent_chat_id,s.xp
                FROM biology_late_exam_requests r
                JOIN biology_tasks t ON t.id=r.task_id
                JOIN biology_students s ON s.user_id=r.user_id
                WHERE r.id=%s FOR UPDATE OF r,t,s;""",(int(request_id),))
            row=cur.fetchone()
            if not row or row["status"]!="pending": return {"status":"processed"}
            cur.execute("SELECT 1 FROM biology_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(row["task_id"],row["user_id"]))
            if cur.fetchone():
                cur.execute("UPDATE biology_late_exam_requests SET status='denied',decided_at=CURRENT_TIMESTAMP,decided_by=%s WHERE id=%s;",(int(admin_id),int(request_id)))
                conn.commit(); return {**dict(row),"status":"submitted"}
            if not approved:
                cur.execute("UPDATE biology_late_exam_requests SET status='denied',decided_at=CURRENT_TIMESTAMP,decided_by=%s WHERE id=%s;",(int(admin_id),int(request_id)))
                cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'late_exam_denied',%s);",(int(admin_id),f"request_id={request_id};task_id={row['task_id']}"))
                conn.commit(); return {**dict(row),"status":"denied"}
            if int(row.get("xp") or 0)<int(row["xp_cost"]): return {**dict(row),"status":"xp"}
            cur.execute("SELECT CURRENT_TIMESTAMP AS now;"); now=cur.fetchone()["now"]
            attempt_hours=max(1,min(24,int(row.get("hours") or row.get("exam_duration_hours") or 2)))
            until=now+timedelta(hours=attempt_hours)
            cur.execute("""INSERT INTO biology_task_extensions(task_id,user_id,requested_at,extended_until)
                VALUES(%s,%s,CURRENT_TIMESTAMP,%s) ON CONFLICT(task_id,user_id) DO UPDATE SET
                requested_at=CURRENT_TIMESTAMP,extended_until=EXCLUDED.extended_until;""",(row["task_id"],row["user_id"],until))
            cur.execute("""UPDATE biology_tasks SET closed=FALSE,warned=FALSE,exam_pending_activation=FALSE,
                exam_approval_required=FALSE,teacher_deadline_reminder_sent=FALSE,champion_announced=FALSE
                WHERE id=%s;""",(row["task_id"],))
            cur.execute("DELETE FROM biology_exam_warning_waivers WHERE task_id=%s AND user_id=%s;",(row["task_id"],row["user_id"]))
            cur.execute("DELETE FROM biology_warning_log WHERE task_id=%s AND user_id=%s RETURNING id;",(row["task_id"],row["user_id"]))
            removed=len(cur.fetchall())
            if removed: cur.execute("UPDATE biology_students SET warnings=GREATEST(0,warnings-%s) WHERE user_id=%s;",(removed,row["user_id"]))
            _set_xp_event(cur,row["user_id"],-int(row["xp_cost"]),"امتحان متاخر بموافقة الاستاذ",f"late-exam:{int(request_id)}")
            cur.execute("""UPDATE biology_late_exam_requests SET status='approved',decided_at=CURRENT_TIMESTAMP,
                decided_by=%s WHERE id=%s;""",(int(admin_id),int(request_id)))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'late_exam_approved',%s);",
                        (int(admin_id),f"request_id={request_id};task_id={row['task_id']};user_id={row['user_id']};xp={row['xp_cost']}"))
            conn.commit(); return {**dict(row),"status":"approved","extended_until":until,"warnings_removed":removed}
    return await run(op)


# ========================= v46 CURRENT EXAMS AND ROYAL REVIEW =========================

async def v29_course_exam_release_at(definition_id):
    """Release a course exam six hours after its exact preparation publication."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.created_at,MAX(p.published_at) AS prep_published_at
                FROM biology_linked_exam_definitions d
                JOIN biology_linked_exam_preparations lp ON lp.definition_id=d.id
                JOIN biology_preparations p
                  ON p.chapter=lp.chapter AND p.chapter_prep_no=lp.prep_no
                WHERE d.id=%s AND d.deleted_at IS NULL AND p.published=TRUE
                GROUP BY d.id,d.created_at;""",(int(definition_id),))
            row=cur.fetchone()
            if not row or not row.get("prep_published_at"): return None
            scheduled=row["prep_published_at"]+timedelta(hours=6)
            return max(scheduled,row["created_at"])
    return await run(op)


_v46_reliable_course_exam_release_at=v29_course_exam_release_at


async def v42_due_missing_course_exams(limit=20):
    """Preparations older than six hours that still have no linked course exam."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT p.* FROM biology_preparations p
                WHERE p.published=TRUE AND p.published_at IS NOT NULL
                  AND p.published_at<=CURRENT_TIMESTAMP-INTERVAL '12 hours'
                  AND p.published_at>=CURRENT_TIMESTAMP-INTERVAL '7 days'
                  AND NOT EXISTS(SELECT 1 FROM biology_linked_exam_preparations lp
                    JOIN biology_linked_exam_definitions d ON d.id=lp.definition_id
                    WHERE lp.chapter=p.chapter AND lp.prep_no=p.chapter_prep_no
                      AND d.target_scope='course' AND d.deleted_at IS NULL)
                  AND NOT EXISTS(SELECT 1 FROM biology_missing_exam_reminders r WHERE r.prep_no=p.prep_no)
                ORDER BY p.published_at LIMIT %s;""",(max(1,min(100,int(limit))),))
            return cur.fetchall()
    return await run(op)


async def v31_active_exam_definitions():
    """Dispatch only definitions retained by the Sep-7 exam policy."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.* FROM biology_linked_exam_definitions d
                WHERE d.deleted_at IS NULL AND (
                  d.created_at>=(COALESCE((SELECT value::date FROM biology_settings
                    WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27') AT TIME ZONE 'Asia/Baghdad')
                  OR EXISTS(SELECT 1 FROM biology_tasks t WHERE t.exam_definition_id=d.id
                    AND COALESCE(t.published_at,t.exam_available_at,t.created_at)>=(
                      COALESCE((SELECT value::date FROM biology_settings
                        WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27') AT TIME ZONE 'Asia/Baghdad')))
                ORDER BY d.id DESC;""")
            return cur.fetchall()
    return await run(op)


async def v42_admin_exam_definitions():
    """Hide retired definitions from admin lists unless an extension is active."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.*,
                    COUNT(DISTINCT ts.user_id) AS student_count,
                    COUNT(DISTINCT t.id) AS internal_task_count,
                    BOOL_OR(t.closed=FALSE OR COALESCE(e.extended_until>CURRENT_TIMESTAMP,FALSE)) AS has_open
                FROM biology_linked_exam_definitions d
                LEFT JOIN biology_tasks t ON t.exam_definition_id=d.id AND t.kind='exam'
                LEFT JOIN biology_task_students ts ON ts.task_id=t.id
                LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=ts.user_id
                WHERE d.deleted_at IS NULL AND (
                  d.created_at>=(COALESCE((SELECT value::date FROM biology_settings
                    WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27') AT TIME ZONE 'Asia/Baghdad')
                  OR COALESCE(t.published_at,t.exam_available_at,t.created_at)>=(
                    COALESCE((SELECT value::date FROM biology_settings
                      WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27') AT TIME ZONE 'Asia/Baghdad')
                  OR e.extended_until>CURRENT_TIMESTAMP)
                GROUP BY d.id ORDER BY d.id DESC;""")
            return cur.fetchall()
    return await run(op)


async def v42_review_context(user_id):
    """Course review starts at chapter 3 preparation 9; chapter tracks start at lecture 1."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track,current_chapter FROM biology_students WHERE user_id=%s;",(int(user_id),))
            student=cur.fetchone()
            if not student: return {"student":None,"pending":[],"preparations":[],"completed":0}
            if student.get("study_track")=="course":
                scope=" AND (r.chapter>3 OR (r.chapter=3 AND r.lecture>=11))"
                completed_scope=" AND (chapter>3 OR (chapter=3 AND lecture>=11))"
            else:
                chapter=int(student.get("current_chapter") or 1)
                scope=" AND r.chapter=%s"; completed_scope=" AND chapter=%s"
            pending_params=[int(user_id)]
            if student.get("study_track")!="course": pending_params.append(chapter)
            cur.execute("""SELECT r.*,r.due_at<=CURRENT_TIMESTAMP AS due FROM biology_lecture_reviews r
                WHERE r.user_id=%s AND r.completed_at IS NULL"""+scope+"""
                  AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                    WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter AND earlier.lecture=r.lecture
                      AND earlier.stage<r.stage AND earlier.completed_at IS NULL)
                ORDER BY r.chapter,r.lecture,r.stage;""",pending_params)
            pending=cur.fetchall()
            completed_params=[int(user_id)]
            if student.get("study_track")!="course": completed_params.append(chapter)
            cur.execute("SELECT COUNT(*) AS n FROM biology_lecture_reviews WHERE user_id=%s AND completed_at IS NOT NULL"+completed_scope+";",completed_params)
            completed=int(cur.fetchone()["n"])
            if student.get("study_track")=="course":
                cur.execute("""SELECT prep_no,chapter,chapter_prep_no,lectures,target_date,published,published_at
                    FROM biology_preparations
                    WHERE chapter>3 OR (chapter=3 AND chapter_prep_no>=11)
                    ORDER BY chapter,chapter_prep_no,prep_no;""")
            else:
                cur.execute("""SELECT prep_no,chapter,chapter_prep_no,lectures,target_date,published,published_at
                    FROM biology_preparations WHERE chapter=%s ORDER BY chapter_prep_no,prep_no;""",(chapter,))
            return {"student":student,"pending":pending,"preparations":cur.fetchall(),"completed":completed}
    return await run(op)


async def v41_due_review_reminders(limit=100):
    """Never remind course students about content before chapter 3 preparation 9."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON(r.user_id,r.chapter,r.lecture) r.*
                FROM biology_lecture_reviews r
                JOIN biology_students s ON s.user_id=r.user_id AND s.approved=TRUE
                WHERE r.completed_at IS NULL AND r.reminded_at IS NULL AND r.due_at<=CURRENT_TIMESTAMP
                  AND ((s.study_track='course' AND (r.chapter>3 OR (r.chapter=3 AND r.lecture>=11)))
                    OR (s.study_track='chapter' AND r.chapter=s.current_chapter))
                  AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                    WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter AND earlier.lecture=r.lecture
                      AND earlier.stage<r.stage AND earlier.completed_at IS NULL)
                ORDER BY r.user_id,r.chapter,r.lecture,r.stage LIMIT %s;""",(max(1,min(500,int(limit))),))
            return cur.fetchall()
    return await run(op)


async def v41_ensure_review_plan(user_id,chapter,lecture):
    """Create reviews from lecture one, except course content before chapter 3 prep 9."""
    student=await get_student(user_id)
    if student and v47_before_start(student,chapter,lecture): return False
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT study_track FROM biology_students WHERE user_id=%s;",(int(user_id),))
            student=cur.fetchone()
            if not student: return False
            if student.get("study_track")=="course" and (
                int(chapter)<3 or (int(chapter)==3 and int(lecture)<11)
            ):
                return False
            cur.execute("""SELECT completed_at FROM biology_lecture_progress
                WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL;""",
                (int(user_id),int(chapter),int(lecture)))
            progress=cur.fetchone()
            if not progress: return False
            for stage,delay in ((1,"6 hours"),(2,"24 hours"),(3,"7 days"),(4,"30 days")):
                cur.execute("""INSERT INTO biology_lecture_reviews
                    (user_id,chapter,lecture,stage,lecture_completed_at,due_at)
                    VALUES(%s,%s,%s,%s,%s,%s::timestamptz+(%s)::interval)
                    ON CONFLICT(user_id,chapter,lecture,stage) DO NOTHING;""",
                    (int(user_id),int(chapter),int(lecture),stage,
                     progress["completed_at"],progress["completed_at"],delay))
            conn.commit(); return True
    return await run(op)


async def v46_cleanup_course_review_history():
    """Remove pre-preparation-9 course reviews and their queued notifications."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""DELETE FROM biology_notifications n USING biology_lecture_reviews r,biology_students s
                WHERE n.entity_type='royal_review' AND n.entity_id=r.id
                  AND r.user_id=s.user_id AND s.study_track='course'
                  AND (r.chapter<3 OR (r.chapter=3 AND r.lecture<11));""")
            notifications=cur.rowcount
            cur.execute("""DELETE FROM biology_lecture_reviews r USING biology_students s
                WHERE r.user_id=s.user_id AND s.study_track='course'
                  AND (r.chapter<3 OR (r.chapter=3 AND r.lecture<11));""")
            reviews=cur.rowcount
            conn.commit(); return {"reviews":reviews,"notifications":notifications}
    return await run(op)


async def v46_repair_retained_exam_state(cutoff_date):
    """Undo the previous Sep-8 cutover for retained Sep-7 exams and extensions."""
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT %s::date AS cutoff;",(str(cutoff_date),)); cutoff=cur.fetchone()["cutoff"]
            cur.execute("""DELETE FROM biology_exam_warning_waivers w USING biology_tasks t
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE w.task_id=t.id AND w.waived_by=0
                  AND COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)>=(%s::date AT TIME ZONE 'Asia/Baghdad');""",(cutoff,))
            waivers=cur.rowcount
            cur.execute("""UPDATE biology_tasks t SET closed=FALSE,warned=FALSE,
                    teacher_deadline_reminder_sent=FALSE,champion_announced=FALSE
                WHERE t.kind='exam' AND EXISTS(SELECT 1 FROM biology_task_students ts
                    LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=ts.user_id
                    WHERE ts.task_id=t.id AND GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline))>CURRENT_TIMESTAMP)
                  AND (COALESCE(t.published_at,t.exam_available_at,
                        (SELECT d.created_at FROM biology_linked_exam_definitions d WHERE d.id=t.exam_definition_id),
                        t.created_at)>=(%s::date AT TIME ZONE 'Asia/Baghdad')
                    OR EXISTS(SELECT 1 FROM biology_task_extensions active_ext
                      WHERE active_ext.task_id=t.id AND active_ext.extended_until>CURRENT_TIMESTAMP));""",(cutoff,))
            reopened=cur.rowcount
            conn.commit(); return {"waivers":waivers,"reopened":reopened}
    return await run(op)


# Final scheduling aliases (must stay at the physical end of this module).
v41_set_study_days=_v42_reliable_set_study_days
v37_chapter_completion_plan=_v42_reliable_completion_plan
v29_course_exam_release_at=_v46_reliable_course_exam_release_at
v29_create_or_get_exam_task=_v42_previous_create_or_get_exam_task

# ========================= v47 explicit exam and enrollment policy =========================
_v47_previous_init_db = init_db
def init_db():
    _v47_previous_init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS reset_pending BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS restore_approval BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS start_chapter INTEGER;
            ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS start_prep_no INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS manual_publish_at TIMESTAMPTZ;
            ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS manual_deadline TIMESTAMPTZ;
            ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS window_notice_sent BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS policy_version INTEGER NOT NULL DEFAULT 46;
            ALTER TABLE biology_linked_exam_definitions ALTER COLUMN policy_version SET DEFAULT 47;
            ALTER TABLE biology_track_change_requests ADD COLUMN IF NOT EXISTS requested_start_prep INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS release_links_ready BOOLEAN NOT NULL DEFAULT FALSE;
            CREATE TABLE IF NOT EXISTS biology_exam_release_preparations(
                definition_id INTEGER NOT NULL REFERENCES biology_linked_exam_definitions(id) ON DELETE CASCADE,
                chapter INTEGER NOT NULL,prep_no INTEGER NOT NULL,
                PRIMARY KEY(definition_id,chapter,prep_no));
        """)
        conn.commit()


def v47_plan_rows(chapter,prep_no,start_date):
    from data import CHAPTER_PREPARATION_DISTRIBUTION
    chapter=int(chapter); prep_no=int(prep_no)
    groups=CHAPTER_PREPARATION_DISTRIBUTION.get(chapter,[])
    if not 1<=prep_no<=len(groups): raise ValueError('Invalid starting preparation')
    # Build only the selected and subsequent preparations, without consuming dates for skipped work.
    rows=[]; cursor=start_date
    for ch in range(chapter,10):
        allowed={6,0,1,2,3} if ch==1 else ({6,0,1,3} if ch==2 else {6,1,3})
        for number,nums in enumerate(CHAPTER_PREPARATION_DISTRIBUTION.get(ch,[]),1):
            if ch==chapter and number<prep_no: continue
            while cursor.weekday() not in allowed: cursor+=timedelta(days=1)
            rows.append((cursor,ch,','.join(map(str,nums)),number)); cursor+=timedelta(days=1)
    return rows


def _v47_required_lectures(cur,definition_id):
    from data import CHAPTER_PREPARATION_DISTRIBUTION
    cur.execute('SELECT chapter,lecture FROM biology_linked_exam_lectures WHERE definition_id=%s;', (definition_id,))
    required={(int(r['chapter']),int(r['lecture'])) for r in cur.fetchall()}
    # An explicit snapshot is authoritative: never add changed live catalog lectures to it.
    if required: return required
    cur.execute('SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s;', (definition_id,))
    for row in cur.fetchall():
        groups=CHAPTER_PREPARATION_DISTRIBUTION.get(int(row['chapter']),[])
        if 1<=int(row['prep_no'])<=len(groups):
            required.update((int(row['chapter']),int(n)) for n in groups[int(row['prep_no'])-1])
    return required


def v47_before_start(student,chapter,lecture):
    from data import CHAPTER_PREPARATION_DISTRIBUTION
    start=student.get('start_chapter')
    if student.get('study_track')!='chapter' or not start: return False
    groups=CHAPTER_PREPARATION_DISTRIBUTION.get(int(start),[])
    number=int(student.get('start_prep_no') or 1)
    if not 1<=number<=len(groups): return False
    return (int(chapter),int(lecture)) < (int(start),min(groups[number-1]))


async def v47_reset_student(user_id):
    """Delete and recreate enrollment atomically; retain only XP history, warnings and parents."""
    from psycopg.types.json import Json
    from psycopg import sql
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;', (int(user_id),))
            old=cur.fetchone()
            if not old: return False
            if old.get('reset_pending'): return True
            cur.execute('SELECT * FROM biology_parent_links WHERE student_id=%s;', (int(user_id),)); parents=cur.fetchall()
            cur.execute('SELECT * FROM biology_xp_log WHERE user_id=%s;', (int(user_id),)); xp_rows=cur.fetchall()
            # These tables deliberately have no FK to the enrollment record.
            for table,col in [('biology_notifications','user_id'),('biology_question_attempts','user_id'),
                              ('biology_student_risk','user_id'),('biology_observed_members','user_id'),
                              ('biology_student_topics','user_id'),('biology_communication_routes','student_id')]:
                cur.execute(sql.SQL('DELETE FROM {} WHERE {}=%s').format(sql.Identifier(table),sql.Identifier(col)),(int(user_id),))
            cur.execute('DELETE FROM biology_scheduled_tasks WHERE linked_student_id=%s;', (int(user_id),))
            cur.execute('DELETE FROM biology_tasks WHERE target_scope=%s;', (f'student:{int(user_id)}',))
            cur.execute('DELETE FROM biology_students WHERE user_id=%s;', (int(user_id),))
            course_xp=int(old['xp']) if old.get('study_track')!='chapter' else int(old.get('course_xp') or 0)
            chapter_xp=int(old['xp']) if old.get('study_track')=='chapter' else int(old.get('chapter_xp') or 0)
            cur.execute("""INSERT INTO biology_students(user_id,full_name,school,target_grade,xp,course_xp,chapter_xp,wallet_version,warnings,
                parent_chat_id,parent_username,parent_full_name,parent_approved,parent_link_code,
                reset_pending,restore_approval)
                VALUES(%s,'','','',%s,%s,%s,48,%s,%s,%s,%s,%s,%s,TRUE,%s);""",
                (int(user_id),old['xp'],course_xp,chapter_xp,old['warnings'],old.get('parent_chat_id'),old.get('parent_username'),
                 old.get('parent_full_name'),bool(old.get('parent_approved')),old.get('parent_link_code'),bool(old['approved'])))
            # Restore exact rows including XP event keys, so historical rewards cannot be claimed twice.
            import json
            for table,rows in [('biology_parent_links',parents),('biology_xp_log',xp_rows)]:
                if rows:
                    cur.execute(sql.SQL('INSERT INTO {} SELECT * FROM json_populate_recordset(NULL::{},%s)').format(
                        sql.Identifier(table),sql.Identifier(table)),(Json(rows,dumps=lambda x:json.dumps(x,default=str)),))
            conn.commit(); return True
    return await run(op)


async def v47_choose_track(user_id,track,chapter,prep_no,start_date):
    if track not in ('course','chapter'): raise ValueError('Invalid study track')
    plan=v47_plan_rows(chapter,prep_no,start_date) if track=='chapter' else []
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;', (int(user_id),)); old=cur.fetchone()
            if not old or not old['full_name'] or not old['school'] or not old['target_grade']: return None
            # Old keyboards cannot replay onboarding and wipe an active student's progress.
            if int(old.get('onboarding_version') or 0)>=19 and not old.get('reset_pending'): return None
            if track=='course':
                cur.execute('SELECT chapter FROM biology_preparations WHERE published=TRUE ORDER BY published_at DESC NULLS LAST,prep_no DESC LIMIT 1;')
                current=cur.fetchone(); actual_chapter=int((current or {}).get('chapter') or 3)
            else: actual_chapter=int(chapter)
            cur.execute("""UPDATE biology_students SET study_track=%s,current_chapter=%s,start_chapter=%s,
                start_prep_no=%s,track_started_on=%s,onboarding_version=47,reset_pending=FALSE,
                approved=CASE WHEN reset_pending THEN restore_approval ELSE approved END,restore_approval=FALSE
                WHERE user_id=%s RETURNING *;""",(track,actual_chapter,int(chapter) if track=='chapter' else None,
                int(prep_no),start_date,int(user_id)))
            student=cur.fetchone()
            _v48_activate_wallet(cur,int(user_id),track)
            cur.execute('SELECT * FROM biology_students WHERE user_id=%s;', (int(user_id),)); student=cur.fetchone()
            cur.execute('DELETE FROM biology_personal_preparations WHERE user_id=%s;', (int(user_id),))
            if track=='chapter':
                _v41_reset_chapter_state(cur,int(user_id),int(chapter),start_date,plan)
                # Do not fabricate completed lectures or review events for skipped preparations.
            else:
                cur.execute("""INSERT INTO biology_task_students(task_id,user_id)
                    SELECT id,%s FROM biology_tasks WHERE closed=FALSE AND deadline>CURRENT_TIMESTAMP
                    AND target_scope IN ('course','all') ON CONFLICT DO NOTHING;""",(int(user_id),))
            conn.commit(); return student
    return await run(op)


async def v47_course_window(definition_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL FOR UPDATE;",(int(definition_id),))
            result=_v48_course_window(cur,cur.fetchone())
            conn.commit();return result
    return await run(op)


async def v29_course_exam_release_at(definition_id):
    window=await v47_course_window(definition_id)
    return window.get('publish_at') if window['status']=='ready' else None


async def v47_set_exam_window(definition_id,publish_at,deadline):
    if publish_at.tzinfo is None or deadline.tzinfo is None or deadline<=publish_at: return False
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_linked_exam_definitions WHERE id=%s AND target_scope='course' AND deleted_at IS NULL FOR UPDATE;",(int(definition_id),))
            if not cur.fetchone() or publish_at<datetime_now(cur): return False
            cur.execute('SELECT 1 FROM biology_tasks WHERE exam_definition_id=%s LIMIT 1;', (int(definition_id),))
            if cur.fetchone(): return False  # Use existing extension controls once published.
            cur.execute("""UPDATE biology_linked_exam_definitions SET manual_publish_at=%s,manual_deadline=%s,
                actual_publish_at=NULL,actual_deadline=NULL,
                policy_version=47,window_notice_sent=FALSE WHERE id=%s;""",(publish_at,deadline,int(definition_id)))
            conn.commit(); return True
    return await run(op)


async def v47_late_exam_notices():
    definitions=await v31_active_exam_definitions(); result=[]
    for d in definitions:
        if d['target_scope']=='course' and int(d.get('policy_version') or 46)>=47 and not d.get('window_notice_sent'):
            if (await v47_course_window(d['id']))['status']=='needs_schedule': result.append(d)
    return result


async def v47_mark_window_notice(definition_id):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute('UPDATE biology_linked_exam_definitions SET window_notice_sent=TRUE WHERE id=%s;', (int(definition_id),)); conn.commit()
    await run(op)


# Keep the original unique-index-backed insertion, without the legacy deadline resync wrappers.
_v47_insert_exam_task=v29_create_or_get_exam_task
async def v29_create_or_get_exam_task(definition_id,user_id,available_at=None,approval_required=False):
    # Existing tasks keep their deadlines, close state, student extensions and submissions.
    existing=await task_for_linked_exam_student(int(definition_id),int(user_id))
    if existing: return existing
    definition=await v31_exam_definition_for_admin(int(definition_id))
    if not definition or definition.get('deleted_at'): return None
    student=await get_student(int(user_id))
    if not student or not student['approved'] or student.get('reset_pending'): return None
    if definition['target_scope']=='course':
        if student.get('study_track')!='course': return None
        window=await v47_course_window(int(definition_id))
        if window['status']!='ready': return None
        available_at=window['publish_at']
        deadline=window['deadline']
    else:
        from datetime import datetime,timezone
        available_at=available_at or datetime.now(timezone.utc)
        deadline=available_at+timedelta(hours=24)
    # The underlying insertion is atomic and idempotent. Duration is always 24 for new automatic exams.
    return await _v47_create_task_atomic(int(definition_id),int(user_id),available_at,deadline)


async def _v47_create_task_atomic(definition_id,user_id,publish_at,deadline,optional=False):
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;', (user_id,)); student=cur.fetchone()
            if not student or not student['approved'] or student.get('reset_pending'): return None
            cur.execute('SELECT * FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL FOR SHARE;', (definition_id,)); d=cur.fetchone()
            if not d: return None
            if d.get('obligation_retired_at') and not optional: return None
            now=datetime_now(cur)
            if d['target_scope']=='chapter' or optional:
                publish_at_local=now; deadline_local=now+timedelta(hours=24)
            else:
                window=_v48_course_window(cur,d)
                if window['status']!='ready': return None
                publish_at_local=window['publish_at']; deadline_local=window['deadline']
            if publish_at_local>now or deadline_local<=now: return None
            if d['target_scope']=='course' and student.get('study_track')!='course' and not optional: return None
            target=f'student:{user_id}'
            cur.execute('SELECT * FROM biology_tasks WHERE exam_definition_id=%s AND target_scope=%s;', (definition_id,target)); old=cur.fetchone()
            if old: return old
            required=_v47_required_lectures(cur,definition_id)
            if d['target_scope']=='chapter' and not optional:
                if student.get('study_track')!='chapter' or int(student['current_chapter'])!=int(d['chapter']): return None
                active={pair for pair in required if not v47_before_start(student,*pair)}
                if not active: return None
                for ch,lecture in active:
                    cur.execute('SELECT 1 FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL;', (user_id,ch,lecture))
                    if not cur.fetchone(): return None
            cur.execute('SELECT payload_type,file_id FROM biology_linked_exam_media WHERE definition_id=%s ORDER BY position,id;', (definition_id,)); media=cur.fetchall()
            if not media: return None
            duration_hours=max(1,int(((deadline_local-publish_at_local).total_seconds()+3599)//3600))
            synthetic=-(700000000000000000+(definition_id*1000000000000+user_id)%100000000000000000)
            cur.execute("""INSERT INTO biology_tasks(kind,title,chat_id,thread_id,source_message_id,payload_type,file_id,
                text_content,deadline,xp_reward,created_by,target_scope,linked_lectures,exam_pending_activation,
                exam_definition_id,exam_duration_hours,exam_available_at,exam_approval_required,closed,published_at,optional_practice)
                VALUES('exam',%s,%s,0,%s,%s,%s,%s,%s,20,%s,%s,%s,FALSE,%s,%s,%s,FALSE,FALSE,%s,%s)
                ON CONFLICT (exam_definition_id,target_scope) WHERE exam_definition_id IS NOT NULL AND target_scope LIKE 'student:%%'
                DO NOTHING RETURNING *;""",(d['title'],OWNER_CHAT_ID or d['created_by'],synthetic,media[0]['payload_type'],media[0]['file_id'],
                d['title'],deadline_local,d['created_by'],target,','.join(f'ف{ch}/م{lec}' for ch,lec in sorted(required)),definition_id,duration_hours,publish_at_local,publish_at_local,bool(optional)))
            task=cur.fetchone()
            if not task: return None
            cur.execute('INSERT INTO biology_task_students(task_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING;', (task['id'],user_id))
            for pos,item in enumerate(media):
                cur.execute('INSERT INTO biology_task_media(task_id,payload_type,file_id,source_message_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;', (task['id'],item['payload_type'],item['file_id'],synthetic-pos))
            conn.commit(); return task
    return await run(op)


async def v29_ready_personal_exams():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT d.id AS definition_id,s.* FROM biology_linked_exam_definitions d
                JOIN biology_students s ON s.current_chapter=d.chapter AND s.study_track='chapter'
                WHERE s.approved=TRUE AND s.reset_pending=FALSE AND s.onboarding_version>=19
                AND d.target_scope='chapter' AND d.deleted_at IS NULL
                AND NOT EXISTS(SELECT 1 FROM biology_tasks t WHERE t.exam_definition_id=d.id AND t.target_scope='student:'||s.user_id);""")
            ready=[]
            for student in cur.fetchall():
                required={p for p in _v47_required_lectures(cur,student['definition_id']) if not v47_before_start(student,*p)}
                if not required: continue
                for ch,lecture in required:
                    cur.execute('SELECT 1 FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL;', (student['user_id'],ch,lecture))
                    if not cur.fetchone(): break
                else: ready.append({'definition_id':student['definition_id'],'user_id':student['user_id']})
            return ready
    return await run(op)


async def v31_active_exam_definitions():
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM biology_linked_exam_definitions WHERE deleted_at IS NULL ORDER BY id;'); return cur.fetchall()
    return await run(op)


_v47_previous_bank_catalog=v42_exam_bank_catalog
async def v42_exam_bank_catalog(user_id,chapter):
    # All archived published definitions can be studied, including course exams from earlier chapters.
    def op():
        with connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM biology_students WHERE user_id=%s AND approved=TRUE AND reset_pending=FALSE;', (int(user_id),)); student=cur.fetchone()
            if not student: return []
            cur.execute('SELECT * FROM biology_linked_exam_definitions WHERE chapter=%s AND deleted_at IS NULL ORDER BY prep_no NULLS LAST,id;', (int(chapter),)); rows=cur.fetchall(); result=[]
            for d in rows:
                required=_v47_required_lectures(cur,d['id'])
                previous=bool(required) and all(v47_before_start(student,*p) for p in required)
                if d['target_scope']=='course' and not previous: continue
                completed=0
                for ch,lecture in required:
                    cur.execute('SELECT 1 FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL;', (int(user_id),ch,lecture))
                    if cur.fetchone() or v47_before_start(student,ch,lecture): completed+=1
                cur.execute("""SELECT t.*,(SELECT submitted_at FROM biology_submissions s WHERE s.task_id=t.id AND s.user_id=%s) AS student_submitted_at
                    FROM biology_tasks t WHERE t.exam_definition_id=%s AND t.target_scope=%s ORDER BY id DESC LIMIT 1;""",(int(user_id),d['id'],f'student:{int(user_id)}'))
                result.append({**dict(d),'required_lectures':len(required),'completed_lectures':completed,
                    'ready':bool(required) and completed==len(required),'previous':previous,'task':cur.fetchone()})
            return result
    return await run(op)


async def v42_open_bank_exam(definition_id,user_id):
    from datetime import datetime,timezone
    d=await v31_exam_definition_for_admin(int(definition_id))
    if not d: return None
    row=next((r for r in await v42_exam_bank_catalog(user_id,d['chapter']) if r['id']==int(definition_id)),None)
    if not row or not row['ready']: return None
    student=await get_student(user_id)
    optional=bool(row['previous'] or student.get('study_track')=='course' or int(student.get('current_chapter') or 0)!=int(d['chapter']))
    now=datetime.now(timezone.utc)
    return await _v47_create_task_atomic(int(definition_id),int(user_id),now,now+timedelta(hours=24),optional)

_v47_previous_review_context=v42_review_context
async def v42_review_context(user_id):
    result=await _v47_previous_review_context(user_id)
    student=await get_student(user_id)
    if student and student.get('study_track')=='chapter' and student.get('start_chapter'):
        result['pending']=[r for r in result['pending'] if not v47_before_start(student,r['chapter'],r['lecture'])]
        result['preparations']=[p for p in result['preparations'] if
            (int(p['chapter']),int(p.get('chapter_prep_no') or 0)) >=
            (int(student['start_chapter']),int(student.get('start_prep_no') or 1))]
    return result


async def v47_activate_legacy_ready_exams():
    """Transition already-prepared chapter exams from the old parent gate, once, when content is complete."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT t.id,s.* FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students s ON s.user_id=ts.user_id
                JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.exam_pending_activation=TRUE AND d.target_scope='chapter' AND d.deleted_at IS NULL
                AND s.approved=TRUE AND s.reset_pending=FALSE AND s.study_track='chapter' AND s.current_chapter=d.chapter
                FOR UPDATE OF t,s;""")
            candidates=cur.fetchall();activated=[]
            for student in candidates:
                cur.execute('SELECT exam_definition_id FROM biology_tasks WHERE id=%s;', (student['id'],)); definition_id=cur.fetchone()['exam_definition_id']
                required=_v47_required_lectures(cur,definition_id)
                active={p for p in required if not v47_before_start(student,*p)}
                if not active: continue
                for ch,lecture in active:
                    cur.execute('SELECT 1 FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=%s AND completed_at IS NOT NULL;', (student['user_id'],ch,lecture))
                    if not cur.fetchone(): break
                else:
                    cur.execute("""UPDATE biology_tasks SET exam_pending_activation=FALSE,exam_approval_required=FALSE,
                        closed=FALSE,exam_duration_hours=24,exam_available_at=CURRENT_TIMESTAMP,published_at=CURRENT_TIMESTAMP,
                        deadline=CURRENT_TIMESTAMP+INTERVAL '24 hours' WHERE id=%s RETURNING *;""",(student['id'],))
                    activated.append(cur.fetchone())
            conn.commit();return activated
    return await run(op)


# v47.1 review fixes: helpers shared by the active transaction paths above.
def _v48_restore_course_state(cur,user_id):
    cur.execute("""UPDATE biology_students SET schedule_mode='regular',start_chapter=NULL,start_prep_no=1,
        study_days=ARRAY[1,3,5,6],xp=COALESCE(course_xp,xp) WHERE user_id=%s;""",(user_id,))
    cur.execute("""UPDATE biology_tasks t SET optional_practice=FALSE
        FROM biology_linked_exam_definitions d WHERE t.exam_definition_id=d.id
        AND d.target_scope='course' AND d.deleted_at IS NULL AND t.target_scope=%s
        AND (t.deadline>CURRENT_TIMESTAMP OR EXISTS(SELECT 1 FROM biology_task_extensions e
            WHERE e.task_id=t.id AND e.user_id=%s AND e.extended_until>CURRENT_TIMESTAMP));""",(f'student:{user_id}',user_id))


def _v48_course_window(cur,d):
    if not d: return {'status':'missing'}
    if d.get('manual_publish_at') and d.get('manual_deadline'):
        return {'status':'ready','publish_at':d['manual_publish_at'],'deadline':d['manual_deadline']}
    # An automatic release gets one durable 24-hour window. A late worker or
    # restart must never shorten the student's submission time or move it again.
    if d.get('actual_publish_at') and d.get('actual_deadline'):
        return {'status':'ready','publish_at':d['actual_publish_at'],'deadline':d['actual_deadline']}
    if d.get('release_links_ready'):
        cur.execute('SELECT chapter,prep_no FROM biology_exam_release_preparations WHERE definition_id=%s;',(d['id'],))
        pairs={(int(r['chapter']),int(r['prep_no'])) for r in cur.fetchall()}
    else:
        cur.execute('SELECT chapter,prep_no FROM biology_linked_exam_preparations WHERE definition_id=%s;',(d['id'],))
        pairs={(int(r['chapter']),int(r['prep_no'])) for r in cur.fetchall()}
        # Explicitly selected lectures may belong to additional preparations.
        for ch,lecture in sorted(_v47_required_lectures(cur,d['id'])):
            cur.execute("""SELECT chapter,chapter_prep_no AS prep_no FROM biology_preparations
                WHERE chapter=%s AND %s=ANY(string_to_array(lectures,',')::integer[])
                ORDER BY target_date DESC LIMIT 1;""",(ch,lecture));row=cur.fetchone()
            if not row or row['prep_no'] is None: return {'status':'waiting_preparation'}
            pairs.add((int(row['chapter']),int(row['prep_no'])))
        if pairs:
            for chapter,prep_no in sorted(pairs):
                cur.execute("INSERT INTO biology_exam_release_preparations(definition_id,chapter,prep_no) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING;",(d['id'],chapter,prep_no))
            cur.execute("UPDATE biology_linked_exam_definitions SET release_links_ready=TRUE WHERE id=%s;",(d['id'],))
    if not pairs: return {'status':'waiting_preparation'}
    dates=[]
    for chapter,prep_no in sorted(pairs):
        cur.execute("""SELECT published,published_at FROM biology_preparations
            WHERE chapter=%s AND chapter_prep_no=%s ORDER BY target_date DESC LIMIT 1;""",(chapter,prep_no));row=cur.fetchone()
        if not row or not row['published'] or not row['published_at']: return {'status':'waiting_preparation'}
        dates.append(row['published_at'])
    publish=max(dates)+timedelta(hours=12)
    if d['created_at']>publish: return {'status':'needs_schedule'}
    now=datetime_now(cur)
    if now>=publish:
        # The first due scan is the real release anchor, shared by every
        # enrolled student and persisted across deployments.
        cur.execute("""UPDATE biology_linked_exam_definitions
            SET actual_publish_at=COALESCE(actual_publish_at,%s),
                actual_deadline=COALESCE(actual_deadline,%s)
            WHERE id=%s RETURNING actual_publish_at,actual_deadline;""",
            (now,now+timedelta(hours=24),d['id']))
        actual=cur.fetchone()
        return {'status':'ready','publish_at':actual['actual_publish_at'],'deadline':actual['actual_deadline']}
    return {'status':'ready','publish_at':publish,'deadline':publish+timedelta(hours=24)}


# ========================= v48 durable learning boundaries =========================

def _v48_activate_wallet(cur,user_id,track):
    """Mirror only the selected track wallet into the legacy xp column."""
    column='course_xp' if track=='course' else 'chapter_xp'
    cur.execute(f"UPDATE biology_students SET xp=COALESCE({column},0) WHERE user_id=%s;",(int(user_id),))


_v48_previous_init_db=init_db
def init_db():
    _v48_previous_init_db()
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS course_xp INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS chapter_xp INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS wallet_version INTEGER NOT NULL DEFAULT 48;
            ALTER TABLE biology_students ALTER COLUMN wallet_version SET DEFAULT 48;
            ALTER TABLE biology_xp_log ADD COLUMN IF NOT EXISTS wallet_scope TEXT NOT NULL DEFAULT 'course';
            ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS retired_obligation BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE biology_linked_exam_definitions ADD COLUMN IF NOT EXISTS obligation_retired_at TIMESTAMPTZ;
            ALTER TABLE biology_linked_exam_definitions ALTER COLUMN policy_version SET DEFAULT 48;

            CREATE TABLE IF NOT EXISTS biology_submission_delivery_outbox(
                id BIGSERIAL PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES biology_tasks(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
                student_message_id BIGINT NOT NULL,
                payload_type TEXT NOT NULL CHECK(payload_type IN ('photo','document','video')),
                file_id TEXT NOT NULL,file_unique_id TEXT NOT NULL,media_group_id TEXT,
                destination_chat_id BIGINT NOT NULL,destination_thread_id BIGINT,
                delivered_message_id BIGINT,status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','delivered','rejected')),
                attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,
                next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                delivered_at TIMESTAMPTZ,admin_alerted_at TIMESTAMPTZ,
                UNIQUE(task_id,user_id,file_unique_id));
            ALTER TABLE biology_submission_delivery_outbox ADD COLUMN IF NOT EXISTS admin_alerted_at TIMESTAMPTZ;
            CREATE INDEX IF NOT EXISTS biology_submission_delivery_pending_idx
                ON biology_submission_delivery_outbox(status,next_attempt_at,id);

            CREATE TABLE IF NOT EXISTS biology_quick_review_questions(
                id BIGSERIAL PRIMARY KEY,chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
                prep_no INTEGER NOT NULL CHECK(prep_no>0),
                question_payload_type TEXT NOT NULL CHECK(question_payload_type IN ('text','photo','document')),
                question_file_id TEXT,question_text TEXT NOT NULL DEFAULT '',
                answer_payload_type TEXT NOT NULL CHECK(answer_payload_type IN ('text','photo','document')),
                answer_file_id TEXT,answer_text TEXT NOT NULL DEFAULT '',
                active BOOLEAN NOT NULL DEFAULT TRUE,created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                retired_at TIMESTAMPTZ);
            CREATE INDEX IF NOT EXISTS biology_quick_review_question_catalog_idx
                ON biology_quick_review_questions(chapter,prep_no,id) WHERE active=TRUE;
            CREATE TABLE IF NOT EXISTS biology_quick_review_attempts(
                question_id BIGINT NOT NULL REFERENCES biology_quick_review_questions(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
                opened_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                answer_revealed_at TIMESTAMPTZ,
                PRIMARY KEY(question_id,user_id));""")
        cur.execute("""UPDATE biology_xp_log x SET wallet_scope=CASE
                WHEN s.study_track='chapter' THEN 'chapter' ELSE 'course' END
            FROM biology_students s WHERE x.user_id=s.user_id AND s.wallet_version<48;""")
        cur.execute("""UPDATE biology_students SET
                course_xp=CASE WHEN study_track='chapter' THEN 0 ELSE xp END,
                chapter_xp=CASE WHEN study_track='chapter' THEN xp ELSE 0 END,
                wallet_version=48
            WHERE wallet_version<48;""")
        cur.execute("""UPDATE biology_students SET xp=CASE WHEN study_track='chapter'
                THEN chapter_xp ELSE course_xp END WHERE wallet_version>=48;""")
        conn.commit()


def _set_xp_event(cur,user_id,delta,reason,event_key):
    """Apply an idempotent XP event to exactly one study-track wallet."""
    cur.execute("""SELECT xp,study_track,course_xp,chapter_xp FROM biology_students
        WHERE user_id=%s FOR UPDATE;""",(int(user_id),)); student=cur.fetchone()
    if not student: return 0
    cur.execute("SELECT delta,wallet_scope FROM biology_xp_log WHERE event_key=%s FOR UPDATE;",(event_key,)); old=cur.fetchone()
    scope=(old or {}).get('wallet_scope') or ('chapter' if student.get('study_track')=='chapter' else 'course')
    if scope not in ('course','chapter'): scope='course'
    column='chapter_xp' if scope=='chapter' else 'course_xp'
    old_delta=int((old or {}).get('delta') or 0)
    current=int(student.get(column) or 0)
    base=max(0,current-old_delta)
    applied=max(-base,int(delta)); difference=applied-old_delta
    if old:
        cur.execute("""UPDATE biology_xp_log SET delta=%s,reason=%s,wallet_scope=%s,
            created_at=CURRENT_TIMESTAMP WHERE event_key=%s;""",(applied,reason,scope,event_key))
    else:
        cur.execute("""INSERT INTO biology_xp_log(user_id,delta,reason,event_key,wallet_scope)
            VALUES(%s,%s,%s,%s,%s);""",(int(user_id),applied,reason,event_key,scope))
    if difference:
        balance=base+applied
        cur.execute(f"UPDATE biology_students SET {column}=%s WHERE user_id=%s;",(balance,int(user_id)))
        if ('chapter' if student.get('study_track')=='chapter' else 'course')==scope:
            cur.execute("UPDATE biology_students SET xp=%s WHERE user_id=%s;",(balance,int(user_id)))
    return difference


async def v48_student_wallets(user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT study_track,xp,course_xp,chapter_xp FROM biology_students
                WHERE user_id=%s;""",(int(user_id),)); return cur.fetchone()
    return await run(op)


def _v48_definition_before_start(cur,student,definition_id):
    if not definition_id or student.get('study_track')!='chapter': return False
    required=_v47_required_lectures(cur,int(definition_id))
    return bool(required) and all(v47_before_start(student,ch,lecture) for ch,lecture in required)


def _v48_task_track_allowed(task):
    """Keep course/chapter obligations isolated, including legacy unlinked tasks."""
    track=str(task.get('study_track') or 'course')
    definition_scope=task.get('definition_scope')
    if definition_scope=='course':
        return track=='course'
    if definition_scope=='chapter':
        return (track=='chapter' and
                int(task.get('definition_chapter') or 0)==int(task.get('current_chapter') or 0))
    scope=str(task.get('target_scope') or '')
    if scope=='all':
        return True
    if scope=='course':
        return track=='course'
    if scope.startswith('chapter_'):
        try: chapter=int(scope.split('_',1)[1])
        except (TypeError,ValueError): return False
        return track=='chapter' and chapter==int(task.get('current_chapter') or 0)
    if scope.startswith('student:'):
        try: return int(scope.split(':',1)[1])==int(task.get('user_id') or 0)
        except (TypeError,ValueError): return False
    return False


async def v48_retire_stale_exam_obligations(cutoff_date):
    """Permanently retire old obligations while preserving questions, grades and history."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT %s::date AS cutoff;",(str(cutoff_date),)); cutoff=cur.fetchone()['cutoff']
            cur.execute("""UPDATE biology_tasks t SET retired_obligation=TRUE,optional_practice=TRUE,
                    closed=TRUE,warned=TRUE,six_hour_reminder_sent=TRUE,
                    teacher_deadline_reminder_sent=TRUE,champion_announced=TRUE
                WHERE t.kind='exam' AND COALESCE(t.published_at,t.exam_available_at,t.created_at)<
                    (%s::date AT TIME ZONE 'Asia/Baghdad')
                RETURNING t.id;""",(cutoff,)); task_ids=[int(r['id']) for r in cur.fetchall()]
            if task_ids:
                cur.execute("""INSERT INTO biology_exam_warning_waivers(task_id,user_id,waived_by)
                    SELECT ts.task_id,ts.user_id,0 FROM biology_task_students ts
                    WHERE ts.task_id=ANY(%s) ON CONFLICT(task_id,user_id) DO NOTHING;""",(task_ids,))
                cur.execute("DELETE FROM biology_warning_log WHERE task_id=ANY(%s) RETURNING user_id;",(task_ids,))
                affected=list({int(r['user_id']) for r in cur.fetchall()})
                if affected:
                    cur.execute("""UPDATE biology_students s SET warnings=(SELECT COUNT(*)
                        FROM biology_warning_log w WHERE w.user_id=s.user_id)
                        WHERE s.user_id=ANY(%s);""",(affected,))
            cur.execute("""UPDATE biology_linked_exam_definitions d SET obligation_retired_at=CURRENT_TIMESTAMP
                WHERE d.deleted_at IS NULL AND d.obligation_retired_at IS NULL
                  AND d.created_at<(%s::date AT TIME ZONE 'Asia/Baghdad')
                  AND NOT EXISTS(SELECT 1 FROM biology_tasks t WHERE t.exam_definition_id=d.id
                    AND t.retired_obligation=FALSE)
                RETURNING d.id;""",(cutoff,)); definitions=len(cur.fetchall())
            conn.commit(); return {'tasks':len(task_ids),'definitions':definitions,'cutoff':cutoff}
    return await run(op)


async def student_exam_lock(user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT t.*,d.target_scope AS definition_scope,d.chapter AS definition_chapter,
                    d.obligation_retired_at,st.study_track,st.current_chapter,st.start_chapter,st.start_prep_no
                FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students st ON st.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s
                  AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND (t.retired_obligation=FALSE OR EXISTS(SELECT 1 FROM biology_task_extensions e
                    WHERE e.task_id=t.id AND e.user_id=ts.user_id AND e.extended_until>CURRENT_TIMESTAMP))
                  AND (COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)>=(
                    COALESCE((SELECT value::date FROM biology_settings WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27')
                    AT TIME ZONE 'Asia/Baghdad') OR EXISTS(SELECT 1 FROM biology_task_extensions e
                      WHERE e.task_id=t.id AND e.user_id=ts.user_id AND e.extended_until>CURRENT_TIMESTAMP))
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id
                    AND sub.user_id=ts.user_id AND sub.submitted_at IS NOT NULL)
                ORDER BY CASE WHEN t.exam_pending_activation THEN 0 WHEN t.closed THEN 1 ELSE 2 END,
                    COALESCE(t.published_at,t.exam_available_at,t.created_at),t.id;""",(int(user_id),))
            for task in cur.fetchall():
                if not _v48_task_track_allowed(task): continue
                if _v48_definition_before_start(cur,task,task.get('exam_definition_id')): continue
                return task
            return None
    return await run(op)


async def v28_student_exam_tasks(user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT t.*,d.target_scope AS definition_scope,d.chapter AS definition_chapter,
                    st.study_track,st.current_chapter,st.start_chapter,st.start_prep_no
                FROM biology_tasks t JOIN biology_task_students ts ON ts.task_id=t.id
                JOIN biology_students st ON st.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE t.kind='exam' AND t.optional_practice=FALSE AND ts.user_id=%s
                  AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND (t.retired_obligation=FALSE OR EXISTS(SELECT 1 FROM biology_task_extensions e
                    WHERE e.task_id=t.id AND e.user_id=ts.user_id AND e.extended_until>CURRENT_TIMESTAMP))
                  AND (COALESCE(t.published_at,t.exam_available_at,d.created_at,t.created_at)>=(
                    COALESCE((SELECT value::date FROM biology_settings WHERE key='v45_exam_enforcement_cutoff'),DATE '2026-09-27')
                    AT TIME ZONE 'Asia/Baghdad') OR EXISTS(SELECT 1 FROM biology_task_extensions e
                      WHERE e.task_id=t.id AND e.user_id=ts.user_id AND e.extended_until>CURRENT_TIMESTAMP))
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id
                    AND sub.user_id=ts.user_id AND sub.submitted_at IS NOT NULL)
                ORDER BY COALESCE(t.exam_available_at,t.deadline),t.id DESC;""",(int(user_id),))
            rows=[]
            for task in cur.fetchall():
                if not _v48_task_track_allowed(task): continue
                if _v48_definition_before_start(cur,task,task.get('exam_definition_id')): continue
                rows.append(task)
            return rows
    return await run(op)


_v48_previous_exam_task_status=v45_exam_task_status
async def v45_exam_task_status(user_id,task_id):
    row=await _v48_previous_exam_task_status(int(user_id),int(task_id))
    if not row: return None
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s;",(int(user_id),)); student=cur.fetchone()
            if not student: return row
            active_extension=bool(row.get('extended_until') and row['extended_until']>row['now'])
            if row.get('retired_obligation') and not active_extension: row['enforced']=False
            check={**dict(row),**dict(student)}
            if not _v48_task_track_allowed(check):
                row['track_allowed']=False; row['enforced']=False
            if _v48_definition_before_start(cur,student,row.get('exam_definition_id')):
                row['track_allowed']=False; row['enforced']=False
            return row
    return await run(op)


async def missing_students(task_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT s.*,t.exam_definition_id,t.target_scope,t.optional_practice,t.retired_obligation,
                    d.target_scope AS definition_scope,d.chapter AS definition_chapter
                FROM biology_task_students ts JOIN biology_students s ON s.user_id=ts.user_id
                JOIN biology_tasks t ON t.id=ts.task_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE ts.task_id=%s AND s.approved=TRUE AND t.optional_practice=FALSE
                  AND t.retired_obligation=FALSE AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id
                    AND sub.user_id=s.user_id AND sub.submitted_at IS NOT NULL)
                  AND NOT EXISTS(SELECT 1 FROM biology_warning_log w WHERE w.task_id=t.id AND w.user_id=s.user_id)
                  AND NOT EXISTS(SELECT 1 FROM biology_exam_warning_waivers w WHERE w.task_id=t.id AND w.user_id=s.user_id)
                  AND NOT EXISTS(SELECT 1 FROM biology_leave_requests l WHERE l.user_id=s.user_id
                    AND l.leave_date=t.deadline::date AND l.status='approved')
                  AND NOT EXISTS(SELECT 1 FROM biology_task_extensions e WHERE e.task_id=t.id
                    AND e.user_id=s.user_id AND e.extended_until>CURRENT_TIMESTAMP);""",(int(task_id),))
            result=[]
            for student in cur.fetchall():
                if not _v48_task_track_allowed(student): continue
                if _v48_definition_before_start(cur,student,student.get('exam_definition_id')): continue
                result.append(student)
            return result
    return await run(op)


async def students_pending_task(task_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT s.*,t.exam_definition_id,t.target_scope,t.optional_practice,t.retired_obligation,
                    d.target_scope AS definition_scope,d.chapter AS definition_chapter
                FROM biology_task_students ts JOIN biology_students s ON s.user_id=ts.user_id
                JOIN biology_tasks t ON t.id=ts.task_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                WHERE ts.task_id=%s AND s.approved=TRUE AND t.optional_practice=FALSE
                  AND t.retired_obligation=FALSE AND (d.id IS NULL OR d.deleted_at IS NULL)
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id
                    AND sub.user_id=s.user_id AND sub.submitted_at IS NOT NULL);""",(int(task_id),))
            rows=[]
            for student in cur.fetchall():
                if not _v48_task_track_allowed(student): continue
                if _v48_definition_before_start(cur,student,student.get('exam_definition_id')): continue
                rows.append(student)
            return rows
    return await run(op)


async def due_tasks():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE closed=FALSE AND retired_obligation=FALSE
                AND deadline<=CURRENT_TIMESTAMP ORDER BY deadline,id;"""); return cur.fetchall()
    return await run(op)


async def unreleased_closed_exams():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE kind='exam' AND closed=TRUE
                AND retired_obligation=FALSE AND optional_practice=FALSE AND questions_released=FALSE
                AND target_scope NOT LIKE 'student:%%' ORDER BY deadline;"""); return cur.fetchall()
    return await run(op)


async def recently_closed_tasks_for_warning_recovery(days=30):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE closed=TRUE AND retired_obligation=FALSE
                AND optional_practice=FALSE AND deadline<=CURRENT_TIMESTAMP
                AND deadline>=CURRENT_TIMESTAMP-(%s || ' days')::INTERVAL ORDER BY deadline,id;""",
                (max(1,min(90,int(days))),)); return cur.fetchall()
    return await run(op)


async def closed_exams_pending_champion(days=30):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE kind='exam' AND closed=TRUE
                AND retired_obligation=FALSE AND optional_practice=FALSE AND champion_announced=FALSE
                AND deadline>=CURRENT_TIMESTAMP-(%s || ' days')::INTERVAL ORDER BY deadline,id;""",
                (max(1,min(90,int(days))),)); return cur.fetchall()
    return await run(op)


async def due_teacher_exam_deadline_reminders():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE kind='exam' AND closed=FALSE
                AND retired_obligation=FALSE AND optional_practice=FALSE
                AND target_scope NOT LIKE 'student:%%' AND teacher_deadline_reminder_sent=FALSE
                AND deadline>CURRENT_TIMESTAMP AND deadline<=CURRENT_TIMESTAMP+INTERVAL '1 hour'
                ORDER BY deadline,id;"""); return cur.fetchall()
    return await run(op)


async def due_exam_reminders():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_tasks WHERE closed=FALSE AND retired_obligation=FALSE
                AND optional_practice=FALSE AND six_hour_reminder_sent=FALSE
                AND deadline>CURRENT_TIMESTAMP AND deadline<=CURRENT_TIMESTAMP+INTERVAL '6 hours'
                ORDER BY deadline,id;"""); return cur.fetchall()
    return await run(op)


async def v28_due_exam_notices():
    """Expire missed announcements instead of replaying them after every deployment."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_exam_notices SET sent=TRUE
                WHERE sent=FALSE AND exam_at<=CURRENT_TIMESTAMP;""")
            cur.execute("""SELECT * FROM biology_exam_notices WHERE sent=FALSE
                AND exam_at>CURRENT_TIMESTAMP
                AND exam_at<=CURRENT_TIMESTAMP+INTERVAL '24 hours'
                ORDER BY exam_at,id;""")
            rows=cur.fetchall(); conn.commit(); return rows
    return await run(op)


async def v31_active_exam_definitions():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_linked_exam_definitions WHERE deleted_at IS NULL
                AND obligation_retired_at IS NULL ORDER BY id;"""); return cur.fetchall()
    return await run(op)


async def v42_admin_exam_definitions():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT d.*,COUNT(DISTINCT ts.user_id) AS student_count,
                    COUNT(DISTINCT t.id) AS internal_task_count,
                    COALESCE(BOOL_OR(t.closed=FALSE AND t.retired_obligation=FALSE),FALSE) AS has_open
                FROM biology_linked_exam_definitions d
                LEFT JOIN biology_tasks t ON t.exam_definition_id=d.id AND t.kind='exam'
                LEFT JOIN biology_task_students ts ON ts.task_id=t.id
                WHERE d.deleted_at IS NULL AND d.obligation_retired_at IS NULL
                GROUP BY d.id ORDER BY d.id DESC;"""); return cur.fetchall()
    return await run(op)


# This wrapper must remain the last init_db definition: the v48 outbox table is
# created first, then v49 adds cleanup metadata and its audit log.
_v49_final_previous_init_db=init_db
def init_db():
    _v49_final_previous_init_db()
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""ALTER TABLE biology_submission_delivery_outbox
                ADD COLUMN IF NOT EXISTS student_message_deleted_at TIMESTAMPTZ;
            ALTER TABLE biology_submission_delivery_outbox
                ADD COLUMN IF NOT EXISTS student_message_delete_status TEXT NOT NULL DEFAULT 'pending';
            ALTER TABLE biology_submission_delivery_outbox
                ADD COLUMN IF NOT EXISTS student_message_delete_error TEXT;
            ALTER TABLE biology_submission_delivery_outbox
                ADD COLUMN IF NOT EXISTS payload_purged_at TIMESTAMPTZ;
            ALTER TABLE biology_linked_exam_definitions
                ADD COLUMN IF NOT EXISTS actual_publish_at TIMESTAMPTZ;
            ALTER TABLE biology_linked_exam_definitions
                ADD COLUMN IF NOT EXISTS actual_deadline TIMESTAMPTZ;
            CREATE INDEX IF NOT EXISTS biology_submission_cleanup_idx
                ON biology_submission_delivery_outbox(status,student_message_delete_status,created_at,id);
            CREATE TABLE IF NOT EXISTS biology_answer_cleanup_runs(
                id BIGSERIAL PRIMARY KEY,admin_id BIGINT NOT NULL,lookback_days INTEGER NOT NULL,
                candidates INTEGER NOT NULL DEFAULT 0,deleted_messages INTEGER NOT NULL DEFAULT 0,
                expired_messages INTEGER NOT NULL DEFAULT 0,failed_messages INTEGER NOT NULL DEFAULT 0,
                purged_payloads INTEGER NOT NULL DEFAULT 0,pending_protected INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);""")
        conn.commit()


async def v50_database_storage():
    """Return the live Neon/PostgreSQL footprint shown only to administrators."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT current_database() AS database_name,
                pg_database_size(current_database())::BIGINT AS database_bytes,
                pg_size_pretty(pg_database_size(current_database())) AS database_pretty;""")
            total=cur.fetchone()
            cur.execute("""SELECT COUNT(*)::INTEGER AS biology_tables,
                COALESCE(SUM(pg_total_relation_size(
                    format('%I.%I',schemaname,tablename)::REGCLASS)),0)::BIGINT AS biology_bytes
                FROM pg_tables WHERE schemaname=current_schema()
                  AND LEFT(tablename,8)='biology_';""")
            physics=cur.fetchone()
            cur.execute("SELECT pg_size_pretty(%s::BIGINT) AS biology_pretty;",(physics['biology_bytes'],))
            pretty=cur.fetchone()['biology_pretty']
            return {**dict(total),**dict(physics),'biology_pretty':pretty}
    return await run(op)


async def v31_delete_exam_definition(definition_id,actor_id=0):
    """Permanently erase one exam and every task-owned record in one transaction.

    Telegram group messages and students' already-earned XP are deliberately
    retained. All database exam media references, attempts, grades, extensions,
    delivery outbox rows and exam notifications are removed.
    """
    def op():
        identifier=int(definition_id)
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_linked_exam_definitions
                WHERE id=%s FOR UPDATE;""",(identifier,))
            definition=cur.fetchone()
            if not definition: return None
            cur.execute("SELECT id FROM biology_tasks WHERE exam_definition_id=%s FOR UPDATE;",(identifier,))
            task_ids=[int(row['id']) for row in cur.fetchall()]
            deleted_submissions=0; deleted_warnings=0
            if task_ids:
                cur.execute("SELECT COUNT(*)::INTEGER AS count FROM biology_submissions WHERE task_id=ANY(%s);",(task_ids,))
                deleted_submissions=int(cur.fetchone()['count'])
                cur.execute("""SELECT user_id,COUNT(*)::INTEGER AS count
                    FROM biology_warning_log WHERE task_id=ANY(%s)
                    GROUP BY user_id;""",(task_ids,))
                warning_counts=cur.fetchall()
                cur.execute("DELETE FROM biology_notifications WHERE entity_type='exam' AND entity_id=ANY(%s);",(task_ids,))
                cur.execute("DELETE FROM biology_warning_log WHERE task_id=ANY(%s);",(task_ids,))
                deleted_warnings=cur.rowcount
                for row in warning_counts:
                    cur.execute("""UPDATE biology_students
                        SET warnings=GREATEST(0,warnings-%s)
                        WHERE user_id=%s;""",(int(row['count']),int(row['user_id'])))
                # Child rows are protected by ON DELETE CASCADE.
                cur.execute("DELETE FROM biology_tasks WHERE id=ANY(%s);",(task_ids,))
            cur.execute("DELETE FROM biology_scheduled_tasks WHERE linked_definition_id=%s;",(identifier,))
            deleted_schedules=cur.rowcount
            # Linked media, preparation/lecture snapshots and release anchors
            # cascade from the definition.
            cur.execute("DELETE FROM biology_linked_exam_definitions WHERE id=%s;",(identifier,))
            if not cur.rowcount: return None
            title=str(definition.get('title') or '')[:180]
            cur.execute("""INSERT INTO biology_audit(actor_id,action,details)
                VALUES(%s,'exam_permanently_deleted',%s);""",
                (int(actor_id or definition['created_by']),
                 f"definition_id={identifier};tasks={len(task_ids)};submissions={deleted_submissions};title={title}"))
            conn.commit()
            return {**dict(definition),'deleted_tasks':len(task_ids),
                'deleted_submissions':deleted_submissions,'deleted_warnings':deleted_warnings,
                'deleted_schedules':deleted_schedules}
    return await run(op)


# ========================= v51 final learning experience =========================

_v51_previous_init_db=init_db
def init_db():
    """Install the final review, catalogue and hot-path indexes idempotently."""
    _v51_previous_init_db()
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""ALTER TABLE biology_students
                ADD COLUMN IF NOT EXISTS review_daily_goal INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE biology_students
                ADD COLUMN IF NOT EXISTS daily_prep_goal INTEGER NOT NULL DEFAULT 1;
            CREATE TABLE IF NOT EXISTS biology_preparation_catalog_overrides(
                chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
                prep_no INTEGER NOT NULL CHECK(prep_no>0),
                lectures TEXT NOT NULL,
                updated_by BIGINT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(chapter,prep_no));
            CREATE INDEX IF NOT EXISTS biology_preparations_today_idx
                ON biology_preparations(target_date,published,published_at);
            CREATE INDEX IF NOT EXISTS biology_personal_preparations_student_date_idx
                ON biology_personal_preparations(user_id,target_date,notified_at);
            CREATE INDEX IF NOT EXISTS biology_task_students_user_idx
                ON biology_task_students(user_id,task_id);
            CREATE INDEX IF NOT EXISTS biology_lecture_progress_completed_idx
                ON biology_lecture_progress(user_id,chapter,lecture)
                WHERE completed_at IS NOT NULL;
            CREATE INDEX IF NOT EXISTS biology_reviews_student_due_idx
                ON biology_lecture_reviews(user_id,due_at,chapter,lecture,stage)
                WHERE completed_at IS NULL;
            UPDATE biology_students SET review_daily_goal=1
                WHERE review_daily_goal<1 OR review_daily_goal>5;
            UPDATE biology_students SET daily_prep_goal=LEAST(5,GREATEST(1,review_daily_goal))
                WHERE daily_prep_goal<1 OR daily_prep_goal>5 OR
                      (daily_prep_goal=1 AND review_daily_goal<>1);
            UPDATE biology_students
                SET track_started_on=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                WHERE study_track='course' AND track_started_on IS NULL;""")
        cur.execute("""ALTER TABLE biology_personal_preparations
                DROP CONSTRAINT IF EXISTS biology_personal_preparations_user_id_target_date_key;
            WITH numbered AS (
                SELECT id,ROW_NUMBER() OVER(PARTITION BY user_id,chapter ORDER BY target_date,id) AS n
                FROM biology_personal_preparations WHERE prep_no IS NULL)
            UPDATE biology_personal_preparations p SET prep_no=numbered.n
                FROM numbered WHERE p.id=numbered.id;
            DELETE FROM biology_personal_preparations p
                USING biology_personal_preparations duplicate
                WHERE p.id>duplicate.id AND p.user_id=duplicate.user_id
                  AND p.target_date=duplicate.target_date AND p.chapter=duplicate.chapter
                  AND p.prep_no=duplicate.prep_no;
            ALTER TABLE biology_personal_preparations ALTER COLUMN prep_no SET NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS biology_personal_prep_slot_uq
                ON biology_personal_preparations(user_id,target_date,chapter,prep_no);""")
        conn.commit()


async def v51_preparation_catalog_overrides():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT chapter,prep_no,lectures,updated_by,updated_at
                FROM biology_preparation_catalog_overrides ORDER BY chapter,prep_no;""")
            return cur.fetchall()
    return await run(op)


async def v51_update_preparation_catalog(chapter,prep_no,lectures,actor_id):
    """Persist one preparation edit and propagate it to active/future schedules."""
    chapter=int(chapter); prep_no=int(prep_no)
    numbers=sorted({int(value) for value in lectures})
    if not numbers: return {"status":"lectures"}
    encoded=','.join(map(str,numbers))
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_preparation_catalog_overrides
                    (chapter,prep_no,lectures,updated_by,updated_at)
                VALUES(%s,%s,%s,%s,CURRENT_TIMESTAMP)
                ON CONFLICT(chapter,prep_no) DO UPDATE SET lectures=EXCLUDED.lectures,
                    updated_by=EXCLUDED.updated_by,updated_at=CURRENT_TIMESTAMP
                RETURNING *;""",(chapter,prep_no,encoded,int(actor_id)))
            override=cur.fetchone()
            cur.execute("""UPDATE biology_preparations SET lectures=%s
                WHERE chapter=%s AND chapter_prep_no=%s;""",(encoded,chapter,prep_no))
            course_rows=cur.rowcount
            cur.execute("""UPDATE biology_personal_preparations SET lectures=%s
                WHERE chapter=%s AND prep_no=%s
                  AND target_date>=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE;""",
                (encoded,chapter,prep_no))
            personal_rows=cur.rowcount
            cur.execute("""INSERT INTO biology_audit(actor_id,action,details)
                VALUES(%s,'preparation_catalog_edited',%s);""",
                (int(actor_id),f"chapter={chapter};prep={prep_no};lectures={encoded};course={course_rows};personal={personal_rows}"))
            conn.commit()
            return {"status":"ok","override":override,"course_rows":course_rows,
                "personal_rows":personal_rows,"lectures":numbers}
    return await run(op)


async def v51_set_review_daily_goal(user_id,goal):
    goal=int(goal)
    if goal not in range(1,6): return None
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_students SET review_daily_goal=%s,daily_prep_goal=%s
                WHERE user_id=%s AND approved=TRUE AND reset_pending=FALSE RETURNING *;""",
                (goal,goal,int(user_id)))
            row=cur.fetchone()
            if not row: return None
            # Keep today's already-released work stable. Starting with the next
            # study day, pack the selected number of preparations into each day.
            cur.execute("""SELECT id,target_date FROM biology_personal_preparations
                WHERE user_id=%s
                  AND target_date>(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                ORDER BY target_date,chapter,prep_no,id FOR UPDATE;""",(int(user_id),))
            future=cur.fetchall(); days={int(value) for value in (row.get('study_days') or [1,3,5,6])}
            if future:
                cur.execute("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE AS today;")
                cursor=max((cur.fetchone()['today']+timedelta(days=1)),future[0]['target_date'])
                while cursor.weekday() not in days: cursor+=timedelta(days=1)
                for index,item in enumerate(future):
                    if index and index%goal==0:
                        cursor+=timedelta(days=1)
                        while cursor.weekday() not in days: cursor+=timedelta(days=1)
                    cur.execute("UPDATE biology_personal_preparations SET target_date=%s WHERE id=%s;",(cursor,item['id']))
            conn.commit(); return row
    return await run(op)


async def v51_backfill_review_plans():
    """Create the four royal-review dates for every actually completed lecture."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""INSERT INTO biology_lecture_reviews
                    (user_id,chapter,lecture,stage,lecture_completed_at,due_at)
                SELECT p.user_id,p.chapter,p.lecture,v.stage,p.completed_at,
                       p.completed_at+v.delay
                FROM biology_lecture_progress p
                CROSS JOIN (VALUES
                    (1,INTERVAL '6 hours'),(2,INTERVAL '24 hours'),
                    (3,INTERVAL '7 days'),(4,INTERVAL '30 days')) AS v(stage,delay)
                WHERE p.completed_at IS NOT NULL
                ON CONFLICT(user_id,chapter,lecture,stage) DO NOTHING;""")
            inserted=cur.rowcount; conn.commit(); return inserted
    return await run(op)


async def v41_ensure_review_plan(user_id,chapter,lecture):
    """Royal review is curriculum-wide: any completed lecture, starting at chapter one."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT completed_at FROM biology_lecture_progress
                WHERE user_id=%s AND chapter=%s AND lecture=%s
                  AND completed_at IS NOT NULL;""",(int(user_id),int(chapter),int(lecture)))
            progress=cur.fetchone()
            if not progress: return False
            for stage,delay in ((1,'6 hours'),(2,'24 hours'),(3,'7 days'),(4,'30 days')):
                cur.execute("""INSERT INTO biology_lecture_reviews
                        (user_id,chapter,lecture,stage,lecture_completed_at,due_at)
                    VALUES(%s,%s,%s,%s,%s,%s::TIMESTAMPTZ+(%s)::INTERVAL)
                    ON CONFLICT(user_id,chapter,lecture,stage) DO NOTHING;""",
                    (int(user_id),int(chapter),int(lecture),stage,
                     progress['completed_at'],progress['completed_at'],delay))
            conn.commit(); return True
    return await run(op)


async def v42_review_context(user_id):
    """Return all seven chapters in order; only real lecture completions create reviews."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT study_track,current_chapter,review_daily_goal
                FROM biology_students WHERE user_id=%s;""",(int(user_id),))
            student=cur.fetchone()
            if not student: return {"student":None,"pending":[],"preparations":[],"completed":0}
            cur.execute("""SELECT r.*,r.due_at<=CURRENT_TIMESTAMP AS due
                FROM biology_lecture_reviews r
                WHERE r.user_id=%s AND r.completed_at IS NULL
                  AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                    WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter
                      AND earlier.lecture=r.lecture AND earlier.stage<r.stage
                      AND earlier.completed_at IS NULL)
                ORDER BY r.chapter,r.lecture,r.stage;""",(int(user_id),))
            pending=cur.fetchall()
            cur.execute("""SELECT COUNT(*)::INTEGER AS n FROM biology_lecture_reviews
                WHERE user_id=%s AND completed_at IS NOT NULL;""",(int(user_id),))
            completed=int(cur.fetchone()['n'])
            return {"student":student,"pending":pending,"preparations":[],"completed":completed}
    return await run(op)


async def v41_due_review_reminders(limit=100):
    """Queue due royal reviews for every track without the retired chapter-three cutoff."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""WITH eligible AS (
                    SELECT r.*,ROW_NUMBER() OVER(
                        PARTITION BY r.user_id ORDER BY r.due_at,r.chapter,r.lecture,r.stage) AS rn,
                        LEAST(5,GREATEST(1,s.review_daily_goal)) AS daily_goal
                    FROM biology_lecture_reviews r
                    JOIN biology_students s ON s.user_id=r.user_id
                    WHERE s.approved=TRUE AND s.reset_pending=FALSE
                      AND r.completed_at IS NULL AND r.reminded_at IS NULL
                      AND r.due_at<=CURRENT_TIMESTAMP
                      AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                        WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter
                          AND earlier.lecture=r.lecture AND earlier.stage<r.stage
                          AND earlier.completed_at IS NULL))
                SELECT * FROM eligible WHERE rn<=daily_goal
                ORDER BY due_at,user_id,chapter,lecture,stage LIMIT %s;""",
                (max(1,min(500,int(limit))),))
            return cur.fetchall()
    return await run(op)


async def incomplete_preparation_students(stage="overdue"):
    """Course reminders are restricted to today's preparation and the student's start."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            window=("p.published_at<=CURRENT_TIMESTAMP-INTERVAL '24 hours'" if stage=='overdue'
                else "p.published_at<=CURRENT_TIMESTAMP-INTERVAL '18 hours' AND p.published_at>CURRENT_TIMESTAMP-INTERVAL '24 hours'")
            cur.execute(f"""SELECT s.user_id,s.full_name,p.prep_no,p.chapter,p.lectures,p.published_at,p.target_date
                FROM biology_students s CROSS JOIN biology_preparations p
                WHERE s.approved=TRUE AND s.reset_pending=FALSE
                  AND s.onboarding_version>=19 AND s.study_track='course'
                  AND p.published=TRUE AND p.published_at IS NOT NULL AND {window}
                  AND p.target_date=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                  AND p.target_date>=COALESCE(s.track_started_on,
                      (s.registered_at AT TIME ZONE 'Asia/Baghdad')::DATE)
                  AND p.published_at>=s.registered_at
                  AND NOT EXISTS(SELECT 1 FROM biology_leave_requests lr
                      WHERE lr.user_id=s.user_id AND lr.leave_date=p.target_date AND lr.status='approved')
                  AND EXISTS(SELECT 1 FROM UNNEST(STRING_TO_ARRAY(p.lectures,',')) x
                    WHERE NOT EXISTS(SELECT 1 FROM biology_lecture_progress lp
                      WHERE lp.user_id=s.user_id AND lp.chapter=p.chapter
                        AND lp.lecture=x::INTEGER AND lp.completed_at IS NOT NULL));""")
            return cur.fetchall()
    return await run(op)


async def incomplete_personal_preparation_students(stage="overdue"):
    """Personal reminders also belong only to the preparation dated today."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            window=("pp.notified_at<=CURRENT_TIMESTAMP-INTERVAL '24 hours'" if stage=='overdue'
                else "pp.notified_at<=CURRENT_TIMESTAMP-INTERVAL '18 hours' AND pp.notified_at>CURRENT_TIMESTAMP-INTERVAL '24 hours'")
            cur.execute(f"""SELECT s.user_id,s.full_name,pp.id AS prep_id,pp.chapter,
                    pp.lectures,pp.target_date,pp.notified_at
                FROM biology_personal_preparations pp
                JOIN biology_students s ON s.user_id=pp.user_id
                WHERE s.approved=TRUE AND s.reset_pending=FALSE
                  AND pp.notified=TRUE AND pp.notified_at IS NOT NULL AND {window}
                  AND pp.target_date=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
                  AND pp.target_date>=COALESCE(s.track_started_on,pp.target_date)
                  AND NOT EXISTS(SELECT 1 FROM biology_leave_requests lr
                      WHERE lr.user_id=s.user_id AND lr.leave_date=pp.target_date AND lr.status='approved')
                  AND EXISTS(SELECT 1 FROM UNNEST(STRING_TO_ARRAY(pp.lectures,',')) x
                    WHERE NOT EXISTS(SELECT 1 FROM biology_lecture_progress lp
                      WHERE lp.user_id=s.user_id AND lp.chapter=pp.chapter
                        AND lp.lecture=x::INTEGER AND lp.completed_at IS NOT NULL));""")
            return cur.fetchall()
    return await run(op)


async def unwatched_lectures_for_student(user_id,now=None):
    """Return real overdue work only, never content before enrollment or today's prep."""
    from datetime import datetime,timezone
    instant=now or datetime.now(timezone.utc)
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT study_track,track_started_on,registered_at
                FROM biology_students WHERE user_id=%s AND approved=TRUE
                  AND reset_pending=FALSE;""",(int(user_id),))
            student=cur.fetchone()
            if not student: return []
            cutoff=instant-timedelta(hours=24)
            if student['study_track']=='course':
                cur.execute("""SELECT p.chapter,p.chapter_prep_no AS prep_no,p.target_date,p.lectures
                    FROM biology_preparations p
                    WHERE p.published=TRUE AND p.published_at IS NOT NULL
                      AND p.published_at<=%s
                      AND p.target_date>=COALESCE(%s::DATE,
                          (%s::TIMESTAMPTZ AT TIME ZONE 'Asia/Baghdad')::DATE)
                    ORDER BY p.target_date,p.chapter,p.chapter_prep_no;""",
                    (cutoff,student.get('track_started_on'),student['registered_at']))
            else:
                cur.execute("""SELECT pp.chapter,pp.prep_no,pp.target_date,pp.lectures
                    FROM biology_personal_preparations pp
                    WHERE pp.user_id=%s AND pp.notified_at IS NOT NULL
                      AND pp.notified_at<=%s
                      AND pp.target_date>=COALESCE(%s::DATE,pp.target_date)
                    ORDER BY pp.target_date,pp.chapter,pp.prep_no;""",
                    (int(user_id),cutoff,student.get('track_started_on')))
            preps=cur.fetchall(); keys=[]; seen=set()
            for prep in preps:
                for raw in str(prep['lectures'] or '').split(','):
                    if not raw.strip().isdigit(): continue
                    key=(int(prep['chapter']),int(raw))
                    if key not in seen:
                        seen.add(key); keys.append((key,prep))
            if not keys: return []
            cur.execute("""SELECT chapter,lecture FROM biology_lecture_progress
                WHERE user_id=%s AND completed_at IS NOT NULL;""",(int(user_id),))
            completed={(int(row['chapter']),int(row['lecture'])) for row in cur.fetchall()}
            return [{"chapter":key[0],"lecture":key[1],"prep_no":prep['prep_no'],
                    "target_date":prep['target_date']} for key,prep in keys if key not in completed]
    return await run(op)


async def personal_preparation_for_student(user_id,target_date):
    """Return the first unfinished preparation when several share one study day."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT pp.* FROM biology_personal_preparations pp
                WHERE pp.user_id=%s AND pp.target_date=%s
                ORDER BY CASE WHEN EXISTS(
                    SELECT 1 FROM UNNEST(STRING_TO_ARRAY(pp.lectures,',')) value
                    WHERE NOT EXISTS(SELECT 1 FROM biology_lecture_progress progress
                      WHERE progress.user_id=pp.user_id AND progress.chapter=pp.chapter
                        AND progress.lecture=value::INTEGER AND progress.completed_at IS NOT NULL)
                ) THEN 0 ELSE 1 END,pp.chapter,pp.prep_no,pp.id LIMIT 1;""",
                (int(user_id),target_date))
            return cur.fetchone()
    return await run(op)


async def v51_daily_tasks(user_id):
    """Fetch the complete daily-task dashboard in one database checkout."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s;",(int(user_id),))
            student=cur.fetchone()
            if not student: return {"student":None,"preparation":None,"tasks":[],"reviews":[],"weaknesses":0}
            today_sql="(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE"
            if student.get('study_track')=='course':
                cur.execute(f"""SELECT * FROM biology_preparations
                    WHERE published=TRUE AND target_date BETWEEN {today_sql} AND {today_sql}+1
                      AND target_date>=COALESCE(%s::DATE,target_date)
                    ORDER BY target_date DESC,published_at DESC NULLS LAST,prep_no DESC LIMIT 1;""",
                    (student.get('track_started_on'),))
            else:
                cur.execute(f"""SELECT pp.* FROM biology_personal_preparations pp
                    WHERE pp.user_id=%s AND pp.target_date={today_sql}
                    ORDER BY CASE WHEN EXISTS(
                        SELECT 1 FROM UNNEST(STRING_TO_ARRAY(pp.lectures,',')) value
                        WHERE NOT EXISTS(SELECT 1 FROM biology_lecture_progress progress
                          WHERE progress.user_id=pp.user_id AND progress.chapter=pp.chapter
                            AND progress.lecture=value::INTEGER AND progress.completed_at IS NOT NULL)
                    ) THEN 0 ELSE 1 END,pp.chapter,pp.prep_no,pp.id LIMIT 1;""",(int(user_id),))
            prep=cur.fetchone()
            if prep:
                lectures=[int(x) for x in str(prep.get('lectures') or '').split(',') if x.strip().isdigit()]
                cur.execute("""SELECT lecture FROM biology_lecture_progress
                    WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s)
                      AND completed_at IS NOT NULL;""",
                    (int(user_id),int(prep['chapter']),lectures or [0]))
                done={int(row['lecture']) for row in cur.fetchall()}
                prep={**dict(prep),'pending_lectures':[n for n in lectures if n not in done],
                    'completed_lectures':[n for n in lectures if n in done]}
            cur.execute("""SELECT DISTINCT ON(t.id) t.*,
                    GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline)) AS effective_deadline
                FROM biology_tasks t
                JOIN biology_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                JOIN biology_students st ON st.user_id=ts.user_id
                LEFT JOIN biology_linked_exam_definitions d ON d.id=t.exam_definition_id
                LEFT JOIN biology_task_extensions e ON e.task_id=t.id AND e.user_id=%s
                WHERE t.retired_obligation=FALSE AND t.optional_practice=FALSE
                  AND (d.id IS NULL OR d.obligation_retired_at IS NULL)
                  AND ((d.target_scope='course' AND st.study_track='course') OR
                       (d.target_scope='chapter' AND st.study_track='chapter'
                            AND d.chapter=st.current_chapter) OR
                       (d.id IS NULL AND (t.target_scope='all' OR t.target_scope=%s OR
                            (st.study_track='course' AND t.target_scope='course') OR
                            (st.study_track='chapter' AND t.target_scope='chapter_'||st.current_chapter))))
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions sub
                    WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL)
                  AND (t.closed=FALSE OR e.extended_until>CURRENT_TIMESTAMP)
                  AND GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline))>CURRENT_TIMESTAMP
                ORDER BY t.id,t.deadline;""",(int(user_id),int(user_id),f'student:{int(user_id)}',int(user_id)))
            tasks=cur.fetchall()
            cur.execute("""SELECT r.*,r.due_at<=CURRENT_TIMESTAMP AS due
                FROM biology_lecture_reviews r
                WHERE r.user_id=%s AND r.completed_at IS NULL AND r.due_at<=CURRENT_TIMESTAMP
                  AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                    WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter
                      AND earlier.lecture=r.lecture AND earlier.stage<r.stage
                      AND earlier.completed_at IS NULL)
                ORDER BY r.due_at,r.chapter,r.lecture,r.stage LIMIT 50;""",(int(user_id),))
            reviews=cur.fetchall()
            cur.execute("""SELECT COUNT(*)::INTEGER AS n FROM biology_weakness_points
                WHERE user_id=%s AND resolved_at IS NULL;""",(int(user_id),))
            weaknesses=int(cur.fetchone()['n'])
            return {"student":student,"preparation":prep,"tasks":tasks,
                "reviews":reviews,"weaknesses":weaknesses}
    return await run(op)


async def v46_cleanup_course_review_history():
    """Retired in v51: royal review now intentionally starts at chapter one."""
    return {"reviews":0,"notifications":0}


async def v51_admin_students():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT user_id,username,full_name,school,target_grade,approved,
                    xp,warnings,parent_chat_id,parent_full_name,parent_approved,registered_at,
                    study_track,current_chapter,start_chapter,start_prep_no,track_started_on
                FROM biology_students WHERE reset_pending=FALSE
                ORDER BY approved DESC,registered_at DESC,user_id;""")
            return cur.fetchall()
    return await run(op)


async def v51_admin_parents():
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT p.parent_chat_id,p.parent_username,p.parent_full_name,p.approved,
                    p.linked_at,s.user_id AS student_id,s.full_name AS student_name,
                    s.school,s.study_track,s.xp,s.warnings
                FROM biology_parent_links p
                JOIN biology_students s ON s.user_id=p.student_id
                WHERE s.reset_pending=FALSE
                ORDER BY p.approved DESC,p.linked_at DESC,p.parent_chat_id;""")
            return cur.fetchall()
    return await run(op)


# ========================= v52 final lecture-first learning flow =========================

_v52_previous_init_db=init_db
def init_db():
    """Install the lecture-first exam, early-study and model-answer schema."""
    _v52_previous_init_db()
    with connect() as conn,conn.cursor() as cur:
        cur.execute("""ALTER TABLE biology_linked_exam_definitions
                ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
            ALTER TABLE biology_submissions
                ADD COLUMN IF NOT EXISTS model_answer_due_at TIMESTAMPTZ;
            ALTER TABLE biology_submissions
                ADD COLUMN IF NOT EXISTS model_answer_sent_at TIMESTAMPTZ;
            CREATE TABLE IF NOT EXISTS biology_exam_model_answer_media(
                id BIGSERIAL PRIMARY KEY,
                definition_id INTEGER NOT NULL REFERENCES biology_linked_exam_definitions(id) ON DELETE CASCADE,
                payload_type TEXT NOT NULL CHECK(payload_type IN ('text','photo','document','video')),
                file_id TEXT,
                text_content TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(definition_id,position));
            CREATE TABLE IF NOT EXISTS biology_early_preparation_unlocks(
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
                study_track TEXT NOT NULL CHECK(study_track IN ('course','chapter')),
                source_prep_id BIGINT,
                chapter INTEGER NOT NULL CHECK(chapter BETWEEN 1 AND 5),
                prep_no INTEGER NOT NULL CHECK(prep_no>0),
                lectures TEXT NOT NULL,
                original_target_date DATE,
                unlocked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMPTZ,
                xp_bonus_awarded BOOLEAN NOT NULL DEFAULT FALSE,
                UNIQUE(user_id,chapter,prep_no));
            CREATE INDEX IF NOT EXISTS biology_model_answer_due_idx
                ON biology_submissions(model_answer_due_at,model_answer_sent_at)
                WHERE submitted_at IS NOT NULL AND model_answer_sent_at IS NULL;
            CREATE INDEX IF NOT EXISTS biology_early_unlock_active_idx
                ON biology_early_preparation_unlocks(user_id,completed_at,chapter,prep_no);
            UPDATE biology_submissions
                SET model_answer_due_at=submitted_at+INTERVAL '8 hours'
                WHERE submitted_at IS NOT NULL AND model_answer_due_at IS NULL;""")
        conn.commit()


def _v52_required_lectures(cur,definition_id):
    """Return the stable lecture set of an exam definition."""
    return sorted(_v47_required_lectures(cur,int(definition_id)))


async def v52_review_queue(user_id):
    """Only the next actionable review for each lecture, ordered by urgency."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s AND approved=TRUE AND reset_pending=FALSE;",(int(user_id),))
            student=cur.fetchone()
            if not student: return {"student":None,"due":[],"next":None,"completed":0}
            cur.execute("""SELECT r.*,r.due_at<=CURRENT_TIMESTAMP AS due
                FROM biology_lecture_reviews r
                WHERE r.user_id=%s AND r.completed_at IS NULL
                  AND NOT EXISTS(SELECT 1 FROM biology_lecture_reviews earlier
                    WHERE earlier.user_id=r.user_id AND earlier.chapter=r.chapter
                      AND earlier.lecture=r.lecture AND earlier.stage<r.stage
                      AND earlier.completed_at IS NULL)
                ORDER BY r.due_at,r.chapter,r.lecture,r.stage;""",(int(user_id),))
            pending=cur.fetchall(); due=[row for row in pending if row['due']]
            cur.execute("SELECT COUNT(*)::INTEGER AS n FROM biology_lecture_reviews WHERE user_id=%s AND completed_at IS NOT NULL;",(int(user_id),))
            return {"student":student,"due":due,"next":pending[0] if pending else None,
                    "completed":int(cur.fetchone()['n'])}
    return await run(op)


async def v52_progress_overview(user_id):
    """One consistent source for completed lectures and the real remaining finish date."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s AND approved=TRUE AND reset_pending=FALSE;",(int(user_id),))
            student=cur.fetchone()
            if not student: return {"student":None,"completed":{},"finish_date":None}
            cur.execute("""SELECT chapter,ARRAY_AGG(lecture ORDER BY lecture) AS lectures,
                    MAX(completed_at) AS last_completed_at
                FROM biology_lecture_progress
                WHERE user_id=%s AND completed_at IS NOT NULL
                GROUP BY chapter ORDER BY chapter;""",(int(user_id),))
            completed={int(row['chapter']):[int(value) for value in row['lectures']] for row in cur.fetchall()}
            if student.get('study_track')=='course':
                cur.execute("""SELECT MAX(p.target_date) AS finish_date
                    FROM biology_preparations p
                    WHERE p.target_date>=COALESCE(%s::DATE,p.target_date)
                      AND EXISTS(SELECT 1 FROM UNNEST(STRING_TO_ARRAY(p.lectures,',')) value
                        WHERE NOT EXISTS(SELECT 1 FROM biology_lecture_progress lp
                          WHERE lp.user_id=%s AND lp.chapter=p.chapter
                            AND lp.lecture=value::INTEGER AND lp.completed_at IS NOT NULL));""",
                    (student.get('track_started_on'),int(user_id)))
            else:
                cur.execute("""SELECT MAX(pp.target_date) AS finish_date
                    FROM biology_personal_preparations pp
                    WHERE pp.user_id=%s
                      AND EXISTS(SELECT 1 FROM UNNEST(STRING_TO_ARRAY(pp.lectures,',')) value
                        WHERE NOT EXISTS(SELECT 1 FROM biology_lecture_progress lp
                          WHERE lp.user_id=pp.user_id AND lp.chapter=pp.chapter
                            AND lp.lecture=value::INTEGER AND lp.completed_at IS NOT NULL));""",(int(user_id),))
            finish=cur.fetchone()['finish_date']
            return {"student":student,"completed":completed,"finish_date":finish}
    return await run(op)


async def v52_chapter_exam_bundle(user_id,chapter):
    """Return lecture completion plus every exam linked to each lecture."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s AND approved=TRUE AND reset_pending=FALSE;",(int(user_id),))
            student=cur.fetchone()
            if not student: return {"student":None,"completed":set(),"completed_pairs":set(),"exams":[]}
            cur.execute("SELECT lecture FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND completed_at IS NOT NULL;",(int(user_id),int(chapter)))
            completed={int(row['lecture']) for row in cur.fetchall()}
            cur.execute("SELECT chapter,lecture FROM biology_lecture_progress WHERE user_id=%s AND completed_at IS NOT NULL;",(int(user_id),))
            completed_pairs={(int(row['chapter']),int(row['lecture'])) for row in cur.fetchall()}
            cur.execute("""SELECT * FROM biology_linked_exam_definitions
                WHERE chapter=%s AND deleted_at IS NULL ORDER BY created_at,id;""",(int(chapter),))
            exams=[]
            for definition in cur.fetchall():
                required=_v52_required_lectures(cur,definition['id'])
                if not required: continue
                cur.execute("""SELECT t.*,sub.submitted_at,sub.model_answer_sent_at,
                        access.status AS approval_status
                    FROM biology_tasks t
                    LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=%s
                    LEFT JOIN biology_exam_access access ON access.task_id=t.id AND access.user_id=%s
                    WHERE t.exam_definition_id=%s AND t.target_scope=%s
                    ORDER BY t.id DESC LIMIT 1;""",
                    (int(user_id),int(user_id),int(definition['id']),f"student:{int(user_id)}"))
                task=cur.fetchone(); ready=all((int(chp),int(ch)) in completed_pairs for chp,ch in required)
                exams.append({**dict(definition),"required":required,"ready":ready,"task":task})
            return {"student":student,"completed":completed,"completed_pairs":completed_pairs,"exams":exams}
    return await run(op)


async def v52_prepare_chapter_exam(definition_id,user_id):
    """Prepare one optional chapter-bank attempt and enforce parent approval."""
    definition=await v31_exam_definition_for_admin(int(definition_id))
    if not definition: return {"status":"missing"}
    bundle=await v52_chapter_exam_bundle(int(user_id),int(definition['chapter']))
    exam=next((row for row in bundle['exams'] if int(row['id'])==int(definition_id)),None)
    if not exam or not exam['ready']: return {"status":"locked"}
    task=exam.get('task')
    if not task:
        now_holder={}
        def now_op():
            with connect() as conn,conn.cursor() as cur:
                now_holder['value']=datetime_now(cur)
        await run(now_op)
        now=now_holder['value']
        task=await _v47_create_task_atomic(int(definition_id),int(user_id),now,now+timedelta(hours=24),True)
    if not task: return {"status":"missing"}
    def gate_op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT submitted_at FROM biology_submissions WHERE task_id=%s AND user_id=%s;",(int(task['id']),int(user_id)))
            submitted=cur.fetchone()
            if submitted and submitted.get('submitted_at'): return {"status":"submitted","task":task}
            cur.execute("SELECT status FROM biology_exam_access WHERE task_id=%s AND user_id=%s;",(int(task['id']),int(user_id)))
            access=cur.fetchone(); approved=bool(access and access['status']=='approved'); notify=not bool(access)
            cur.execute("""UPDATE biology_tasks SET exam_approval_required=TRUE,
                    exam_pending_activation=%s,closed=FALSE
                WHERE id=%s RETURNING *;""",(not approved,int(task['id'])))
            updated=cur.fetchone()
            cur.execute("""INSERT INTO biology_exam_access(task_id,user_id,status)
                VALUES(%s,%s,%s) ON CONFLICT(task_id,user_id) DO NOTHING;""",
                (int(task['id']),int(user_id),'approved' if approved else 'pending'))
            conn.commit(); return {"status":"open" if approved else "approval","task":updated,"notify":notify}
    return await run(gate_op)


async def v52_exam_model_answer(definition_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_exam_model_answer_media
                WHERE definition_id=%s ORDER BY position,id;""",(int(definition_id),))
            return cur.fetchall()
    return await run(op)


async def v52_replace_exam_model_answer(definition_id,items,created_by):
    clean=[]
    for item in items:
        payload=str(item.get('payload_type') or '')
        if payload not in {'text','photo','document','video'}: continue
        file_id=item.get('file_id'); text=str(item.get('text_content') or '')[:4000]
        if payload=='text' and not text.strip(): continue
        if payload!='text' and not file_id: continue
        clean.append((payload,file_id,text))
    if not clean: return 0
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT 1 FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL;",(int(definition_id),))
            if not cur.fetchone(): return 0
            cur.execute("DELETE FROM biology_exam_model_answer_media WHERE definition_id=%s;",(int(definition_id),))
            for position,(payload,file_id,text) in enumerate(clean):
                cur.execute("""INSERT INTO biology_exam_model_answer_media
                    (definition_id,payload_type,file_id,text_content,position,created_by)
                    VALUES(%s,%s,%s,%s,%s,%s);""",
                    (int(definition_id),payload,file_id,text,position,int(created_by)))
            cur.execute("""INSERT INTO biology_audit(actor_id,action,details)
                VALUES(%s,'exam_model_answer_replaced',%s);""",
                (int(created_by),f"definition={int(definition_id)};items={len(clean)}"))
            conn.commit(); return len(clean)
    return await run(op)


async def v52_delete_exam_model_answer(definition_id,actor_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("DELETE FROM biology_exam_model_answer_media WHERE definition_id=%s;",(int(definition_id),))
            count=cur.rowcount
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'exam_model_answer_deleted',%s);",
                (int(actor_id),f"definition={int(definition_id)};items={count}"))
            conn.commit(); return count
    return await run(op)


async def v52_due_model_answers(limit=50):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT sub.task_id,sub.user_id,sub.submitted_at,t.title,t.exam_definition_id,
                    s.full_name,s.parent_chat_id
                FROM biology_submissions sub
                JOIN biology_tasks t ON t.id=sub.task_id AND t.kind='exam'
                JOIN biology_students s ON s.user_id=sub.user_id
                WHERE sub.submitted_at IS NOT NULL AND sub.model_answer_sent_at IS NULL
                  AND COALESCE(sub.model_answer_due_at,sub.submitted_at+INTERVAL '8 hours')<=CURRENT_TIMESTAMP
                  AND t.exam_definition_id IS NOT NULL
                  AND EXISTS(SELECT 1 FROM biology_exam_model_answer_media a
                    WHERE a.definition_id=t.exam_definition_id)
                ORDER BY COALESCE(sub.model_answer_due_at,sub.submitted_at+INTERVAL '8 hours'),sub.task_id
                LIMIT %s;""",(max(1,min(200,int(limit))),))
            return cur.fetchall()
    return await run(op)


async def v52_mark_model_answer_sent(task_id,user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""UPDATE biology_submissions SET model_answer_sent_at=CURRENT_TIMESTAMP
                WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL
                  AND model_answer_sent_at IS NULL RETURNING *;""",(int(task_id),int(user_id)))
            row=cur.fetchone(); conn.commit(); return row
    return await run(op)


async def v52_submission_state(task_id,user_id):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT submitted_at,model_answer_due_at,model_answer_sent_at,retry_count
                FROM biology_submissions WHERE task_id=%s AND user_id=%s;""",(int(task_id),int(user_id)))
            return cur.fetchone()
    return await run(op)


_v52_previous_prepare_submission_retry=prepare_submission_retry
async def prepare_submission_retry(task_id,user_id):
    state=await v52_submission_state(task_id,user_id)
    if state and state.get('model_answer_sent_at'):
        return {"status":"model_answer_locked","messages":[],"remaining":0}
    return await _v52_previous_prepare_submission_retry(task_id,user_id)


_v52_previous_record_submission=record_submission
async def record_submission(task_id,user_id,message_id,file_unique_id,media_group_id=None):
    state=await v52_submission_state(task_id,user_id)
    if state and state.get('model_answer_sent_at'): return "model_answer_locked"
    result=await _v52_previous_record_submission(task_id,user_id,message_id,file_unique_id,media_group_id)
    if result in ('added','replaced'):
        def op():
            with connect() as conn,conn.cursor() as cur:
                cur.execute("""UPDATE biology_submissions
                    SET model_answer_due_at=submitted_at+INTERVAL '8 hours',model_answer_sent_at=NULL
                    WHERE task_id=%s AND user_id=%s;""",(int(task_id),int(user_id)))
                conn.commit()
        await run(op)
    return result


_v52_previous_stage_submission=v48_stage_submission_delivery
async def v48_stage_submission_delivery(task_id,user_id,student_message_id,payload_type,file_id,
                                        file_unique_id,media_group_id,destination_chat_id,destination_thread_id=None):
    state=await v52_submission_state(task_id,user_id)
    if state and state.get('model_answer_sent_at'): return {'status':'model_answer_locked'}
    return await _v52_previous_stage_submission(task_id,user_id,student_message_id,payload_type,file_id,
        file_unique_id,media_group_id,destination_chat_id,destination_thread_id)


def _v52_prep_complete(cur,user_id,chapter,lectures):
    numbers=[int(value) for value in str(lectures or '').split(',') if value.strip().isdigit()]
    if not numbers: return False
    cur.execute("""SELECT COUNT(DISTINCT lecture)::INTEGER AS n
        FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s
          AND lecture=ANY(%s) AND completed_at IS NOT NULL;""",(int(user_id),int(chapter),numbers))
    return int(cur.fetchone()['n'])==len(set(numbers))


async def v52_current_preparation(user_id):
    """Prefer an unfinished early-unlocked lecture group, otherwise today's real item."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s;",(int(user_id),)); student=cur.fetchone()
            if not student: return None
            course=student.get('study_track')=='course'
            cur.execute("""SELECT e.*,'early'::TEXT AS source FROM biology_early_preparation_unlocks e
                WHERE e.user_id=%s AND e.completed_at IS NULL
                  AND (NOT %s OR e.chapter>3 OR (e.chapter=3 AND NOT EXISTS(
                    SELECT 1 FROM UNNEST(STRING_TO_ARRAY(e.lectures,',')) value
                    WHERE value::INTEGER<11)))
                ORDER BY e.unlocked_at,e.id LIMIT 1;""",(int(user_id),course))
            early=cur.fetchone()
            if early:
                lectures=[int(x) for x in early['lectures'].split(',') if x.strip().isdigit()]
                cur.execute("""SELECT lecture FROM biology_lecture_progress
                    WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s) AND completed_at IS NOT NULL;""",
                    (int(user_id),int(early['chapter']),lectures or [0]))
                done={int(row['lecture']) for row in cur.fetchall()}
                return {**dict(early),'target_date':early.get('original_target_date'),
                    'pending_lectures':[n for n in lectures if n not in done],
                    'completed_lectures':[n for n in lectures if n in done],'early':True}
            today_sql="(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE"
            if student.get('study_track')=='course':
                cur.execute(f"""SELECT p.*,FALSE AS early FROM biology_preparations p
                    WHERE p.published=TRUE AND p.target_date BETWEEN {today_sql} AND {today_sql}+1
                      AND p.target_date>=COALESCE(%s::DATE,p.target_date)
                      AND (p.chapter>3 OR (p.chapter=3 AND p.chapter_prep_no>=11))
                    ORDER BY p.target_date DESC,p.published_at DESC NULLS LAST,p.prep_no DESC LIMIT 1;""",
                    (student.get('track_started_on'),))
            else:
                cur.execute(f"""SELECT pp.*,FALSE AS early FROM biology_personal_preparations pp
                    WHERE pp.user_id=%s AND pp.target_date<={today_sql}
                    ORDER BY pp.target_date,pp.chapter,pp.prep_no,pp.id;""",(int(user_id),))
                rows=cur.fetchall(); base=next((row for row in rows if not _v52_prep_complete(cur,user_id,row['chapter'],row['lectures'])),None)
                if base:
                    lectures=[int(x) for x in base['lectures'].split(',') if x.strip().isdigit()]
                    cur.execute("SELECT lecture FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s) AND completed_at IS NOT NULL;",(int(user_id),int(base['chapter']),lectures or [0]))
                    done={int(row['lecture']) for row in cur.fetchall()}
                    return {**dict(base),'pending_lectures':[n for n in lectures if n not in done],
                        'completed_lectures':[n for n in lectures if n in done],'early':False}
                return None
            base=cur.fetchone()
            if not base: return None
            lectures=[int(x) for x in base['lectures'].split(',') if x.strip().isdigit()]
            cur.execute("SELECT lecture FROM biology_lecture_progress WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s) AND completed_at IS NOT NULL;",(int(user_id),int(base['chapter']),lectures or [0]))
            done={int(row['lecture']) for row in cur.fetchall()}
            return {**dict(base),'pending_lectures':[n for n in lectures if n not in done],
                'completed_lectures':[n for n in lectures if n in done],'early':False}
    return await run(op)


async def v52_unlock_next_preparation(user_id):
    """Unlock exactly the first unfinished future lecture group; never skip content."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM biology_students WHERE user_id=%s AND approved=TRUE AND reset_pending=FALSE FOR UPDATE;",(int(user_id),))
            student=cur.fetchone()
            if not student: return {"status":"student"}
            cur.execute("""SELECT * FROM biology_early_preparation_unlocks
                WHERE user_id=%s AND completed_at IS NULL ORDER BY unlocked_at,id LIMIT 1;""",(int(user_id),))
            active=cur.fetchone()
            if active: return {"status":"existing","row":active}
            if student.get('study_track')=='course':
                cur.execute("""SELECT p.prep_no AS source_prep_id,p.chapter,
                        COALESCE(p.chapter_prep_no,p.prep_no) AS prep_no,p.lectures,p.target_date
                    FROM biology_preparations p
                    WHERE p.target_date>=COALESCE(%s::DATE,p.target_date)
                    ORDER BY p.target_date,p.prep_no;""",(student.get('track_started_on'),))
            else:
                cur.execute("""SELECT pp.id AS source_prep_id,pp.chapter,pp.prep_no,pp.lectures,pp.target_date
                    FROM biology_personal_preparations pp WHERE pp.user_id=%s
                    ORDER BY pp.target_date,pp.chapter,pp.prep_no,pp.id;""",(int(user_id),))
            rows=cur.fetchall(); candidate=None
            for row in rows:
                if not _v52_prep_complete(cur,user_id,row['chapter'],row['lectures']):
                    candidate=row; break
            if not candidate: return {"status":"finished"}
            cur.execute("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE AS today;")
            today=cur.fetchone()['today']
            if candidate['target_date']<=today: return {"status":"current","row":candidate}
            cur.execute("""INSERT INTO biology_early_preparation_unlocks
                    (user_id,study_track,source_prep_id,chapter,prep_no,lectures,original_target_date)
                VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(user_id,chapter,prep_no) DO UPDATE SET
                    lectures=EXCLUDED.lectures,original_target_date=EXCLUDED.original_target_date,
                    completed_at=NULL,xp_bonus_awarded=FALSE,unlocked_at=CURRENT_TIMESTAMP
                RETURNING *;""",(int(user_id),student['study_track'],candidate['source_prep_id'],
                    int(candidate['chapter']),int(candidate['prep_no']),candidate['lectures'],candidate['target_date']))
            unlocked=cur.fetchone()
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'early_preparation_unlocked',%s);",
                (int(user_id),f"chapter={candidate['chapter']};prep={candidate['prep_no']};original={candidate['target_date']}"))
            conn.commit(); return {"status":"ok","row":unlocked}
    return await run(op)


_v52_previous_preparation_access=v37_preparation_access
async def v37_preparation_access(user_id,chapter,lecture):
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_early_preparation_unlocks
                WHERE user_id=%s AND chapter=%s AND completed_at IS NULL
                  AND %s=ANY(STRING_TO_ARRAY(lectures,',')::INTEGER[])
                ORDER BY unlocked_at DESC LIMIT 1;""",(int(user_id),int(chapter),int(lecture)))
            return cur.fetchone()
    early=await run(op)
    if early: return {"allowed":True,"reason":"early","row":early}
    return await _v52_previous_preparation_access(user_id,chapter,lecture)


_v52_previous_award_preparation=award_daily_preparation
async def award_daily_preparation(user_id,chapter,lecture):
    """Early lecture groups award 30 XP exactly once; normal groups keep 15 XP."""
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("""SELECT * FROM biology_early_preparation_unlocks
                WHERE user_id=%s AND chapter=%s
                  AND %s=ANY(STRING_TO_ARRAY(lectures,',')::INTEGER[])
                ORDER BY unlocked_at DESC LIMIT 1 FOR UPDATE;""",(int(user_id),int(chapter),int(lecture)))
            early=cur.fetchone()
            if not early: return None
            if early.get('completed_at'):
                return {"awarded":False,"early":True,"prep":early,"xp":0}
            if not _v52_prep_complete(cur,user_id,early['chapter'],early['lectures']):
                return {"awarded":False,"early":True,"prep":early,"xp":0}
            change=_set_xp_event(cur,int(user_id),30,"إكمال المحاضرات مبكرا - XP مضاعف",f"early_prep:{early['id']}:{int(user_id)}")
            cur.execute("""UPDATE biology_early_preparation_unlocks
                SET completed_at=CURRENT_TIMESTAMP,xp_bonus_awarded=TRUE WHERE id=%s RETURNING *;""",(early['id'],))
            done=cur.fetchone()
            if early.get('study_track')=='chapter':
                cur.execute("SELECT study_days,daily_prep_goal FROM biology_students WHERE user_id=%s;",(int(user_id),))
                student=cur.fetchone() or {}; days={int(value) for value in (student.get('study_days') or [1,3,5,6])}
                goal=max(1,min(5,int(student.get('daily_prep_goal') or 1)))
                cur.execute("""SELECT pp.* FROM biology_personal_preparations pp
                    WHERE pp.user_id=%s AND EXISTS(
                        SELECT 1 FROM UNNEST(STRING_TO_ARRAY(pp.lectures,',')) value
                        WHERE NOT EXISTS(SELECT 1 FROM biology_lecture_progress lp
                          WHERE lp.user_id=pp.user_id AND lp.chapter=pp.chapter
                            AND lp.lecture=value::INTEGER AND lp.completed_at IS NOT NULL))
                    ORDER BY pp.target_date,pp.chapter,pp.prep_no,pp.id FOR UPDATE;""",(int(user_id),))
                future=cur.fetchall()
                if future:
                    cur.execute("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE AS today;")
                    cursor=cur.fetchone()['today']+timedelta(days=1)
                    while cursor.weekday() not in days: cursor+=timedelta(days=1)
                    for index,item in enumerate(future):
                        if index and index%goal==0:
                            cursor+=timedelta(days=1)
                            while cursor.weekday() not in days: cursor+=timedelta(days=1)
                        cur.execute("UPDATE biology_personal_preparations SET target_date=%s WHERE id=%s;",(cursor,item['id']))
            conn.commit()
            return {"awarded":change>0,"early":True,"prep":done,"xp":30 if change>0 else 0}
    early_result=await run(op)
    if early_result is not None: return early_result
    result=await _v52_previous_award_preparation(user_id,chapter,lecture)
    if result is not None: result={**dict(result),"early":False,"xp":15 if result.get('awarded') else 0}
    return result


_v52_previous_create_exam_task=v29_create_or_get_exam_task
async def v29_create_or_get_exam_task(definition_id,user_id,available_at=None,approval_required=False):
    """All newly prepared exams require an explicit approved parent/admin gate."""
    task=await _v52_previous_create_exam_task(definition_id,user_id,available_at,True)
    if not task or task.get('legacy_scheduled'): return task
    def op():
        with connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT submitted_at FROM biology_submissions WHERE task_id=%s AND user_id=%s;",(int(task['id']),int(user_id)))
            submitted=cur.fetchone()
            if submitted and submitted.get('submitted_at'): return task
            cur.execute("SELECT status FROM biology_exam_access WHERE task_id=%s AND user_id=%s;",(int(task['id']),int(user_id)))
            access=cur.fetchone(); approved=bool(access and access['status']=='approved')
            cur.execute("""UPDATE biology_tasks SET exam_approval_required=TRUE,
                    exam_pending_activation=%s WHERE id=%s RETURNING *;""",(not approved,int(task['id'])))
            updated=cur.fetchone()
            cur.execute("""INSERT INTO biology_exam_access(task_id,user_id,status)
                VALUES(%s,%s,%s) ON CONFLICT(task_id,user_id) DO NOTHING;""",
                (int(task['id']),int(user_id),'approved' if approved else 'pending'))
            conn.commit(); return updated
    return await run(op)
