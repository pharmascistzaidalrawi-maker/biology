"""Idempotent assessment schema shared by CLI/database and bot startup."""
def migrate(db):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute('''ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS homework_manual_closed BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS homework_opens_at TIMESTAMPTZ;
            ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS homework_number INTEGER;
            ALTER TABLE biology_pending_tasks ADD COLUMN IF NOT EXISTS homework_number INTEGER;
            ALTER TABLE biology_pending_tasks ADD COLUMN IF NOT EXISTS homework_published_at TIMESTAMPTZ;
            ALTER TABLE biology_school_reviews ADD COLUMN IF NOT EXISTS exam_deleted BOOLEAN NOT NULL DEFAULT FALSE;
            CREATE TABLE IF NOT EXISTS biology_v9_deleted_task_warnings(
            task_id INTEGER NOT NULL REFERENCES biology_tasks(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            deleted_by BIGINT NOT NULL DEFAULT 0,
            deleted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(task_id,user_id));
            ALTER TABLE biology_submissions
            ADD COLUMN IF NOT EXISTS revision_open BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS teacher_change_pending BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS rejected BOOLEAN NOT NULL DEFAULT FALSE;
            CREATE TABLE IF NOT EXISTS biology_v11_champions (
                cohort TEXT PRIMARY KEY, claimed_at TIMESTAMPTZ,
                sent_at TIMESTAMPTZ, message_id BIGINT, winner_id BIGINT);
            ALTER TABLE biology_v11_champions ADD COLUMN IF NOT EXISTS result_signature TEXT;
            ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS activation_delivered_at TIMESTAMPTZ;
            CREATE TABLE IF NOT EXISTS biology_v11_submission_history (
                id BIGSERIAL PRIMARY KEY, task_id INTEGER NOT NULL, user_id BIGINT NOT NULL,
                actor_id BIGINT NOT NULL, action TEXT NOT NULL, snapshot JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS biology_v11_parent_messages (
                chat_id BIGINT NOT NULL,message_id BIGINT NOT NULL,parent_id BIGINT NOT NULL,
                student_id BIGINT NOT NULL,PRIMARY KEY(chat_id,message_id));
            ALTER TABLE biology_warning_log ADD COLUMN IF NOT EXISTS policy_key TEXT;
            CREATE UNIQUE INDEX IF NOT EXISTS biology_v11_warning_policy
                ON biology_warning_log(user_id,task_id,policy_key) WHERE policy_key IS NOT NULL;
            UPDATE biology_warning_log w SET policy_key='failed'
                WHERE w.policy_key IS NULL AND w.reason LIKE 'رسوب%%'
                AND w.id=(SELECT MIN(w2.id) FROM biology_warning_log w2
                    WHERE w2.user_id=w.user_id AND w2.task_id=w.task_id AND w2.reason LIKE 'رسوب%%')
                AND NOT EXISTS(SELECT 1 FROM biology_warning_log w3 WHERE w3.user_id=w.user_id
                    AND w3.task_id=w.task_id AND w3.policy_key='failed');
            CREATE OR REPLACE FUNCTION biology_v11_grade_policy() RETURNS trigger AS $$
            BEGIN
                IF NEW.grade IS NOT NULL AND (NEW.grade<0 OR NEW.grade>100) THEN
                    RAISE EXCEPTION 'Grade must be between 0 and 100';
                END IF;
                IF NEW.grade IS NOT NULL AND (NEW.revision_open OR NEW.teacher_change_pending OR NEW.rejected) THEN
                    RAISE EXCEPTION 'Answer awaiting replacement or rejected';
                END IF;
                IF NEW.grade IS NOT NULL AND EXISTS(SELECT 1 FROM biology_tasks WHERE id=NEW.task_id AND kind='exam') THEN
                    PERFORM 1 FROM biology_students WHERE user_id=NEW.user_id FOR UPDATE;
                    IF NEW.grade<80 THEN
                        INSERT INTO biology_warning_log(user_id,task_id,reason,issued_by,policy_key)
                            VALUES(NEW.user_id,NEW.task_id,'رسوب: الدرجة أقل من 80',COALESCE(NEW.graded_by,0),'failed')
                            ON CONFLICT DO NOTHING;
                    ELSE
                        DELETE FROM biology_warning_log WHERE user_id=NEW.user_id AND task_id=NEW.task_id AND policy_key='failed';
                    END IF;
                    UPDATE biology_students SET warnings=(SELECT COUNT(*) FROM biology_warning_log WHERE user_id=NEW.user_id)
                        WHERE user_id=NEW.user_id;
                END IF;
                RETURN NEW;
            END; $$ LANGUAGE plpgsql;
            DROP TRIGGER IF EXISTS biology_v11_grade_policy ON biology_submissions;
            CREATE TRIGGER biology_v11_grade_policy BEFORE INSERT OR UPDATE OF grade ON biology_submissions
                FOR EACH ROW EXECUTE FUNCTION biology_v11_grade_policy();''')
        conn.commit()

    from student_sections_schema import migrate as migrate_sections
    migrate_sections(db,'biology')
