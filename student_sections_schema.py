"""Shared authoritative section defaults and automatic-penalty guards."""
SECTIONS=('lectures','homework','exams','school_review')

def task_section(task):
    if not task:return None
    return 'school_review' if task.get('school_review_id') else 'exams' if task.get('kind')=='exam' else 'homework' if task.get('kind')=='homework' else None

def task_allowed(cur,uid,task,prefix,accountable=False):
    section=task_section(task)
    if not section:return True
    function='section_accountable' if accountable else 'section_enabled'
    cur.execute(f'SELECT {prefix}_{function}(%s,%s) AS allowed;',(int(uid),section))
    return bool(cur.fetchone()['allowed'])

def migrate(db,prefix):
    if prefix not in ('biology','islamic','chemistry'):raise ValueError('Invalid subject')
    p=prefix
    with db.connect() as conn,conn.cursor() as cur:
        cur.execute(f'''CREATE TABLE IF NOT EXISTS {p}_student_modules(
            user_id BIGINT PRIMARY KEY REFERENCES {p}_students(user_id) ON DELETE CASCADE,
            lectures BOOLEAN NOT NULL DEFAULT TRUE,homework BOOLEAN NOT NULL DEFAULT TRUE,
            exams BOOLEAN NOT NULL DEFAULT TRUE,school_review BOOLEAN NOT NULL DEFAULT TRUE,
            listener BOOLEAN NOT NULL DEFAULT FALSE,changed_by BIGINT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);
            ALTER TABLE {p}_student_modules ADD COLUMN IF NOT EXISTS lectures BOOLEAN NOT NULL DEFAULT TRUE;
            CREATE OR REPLACE FUNCTION {p}_section_enabled(uid BIGINT,section TEXT) RETURNS BOOLEAN
            LANGUAGE SQL VOLATILE AS $$
                SELECT COALESCE((SELECT CASE section WHEN 'lectures' THEN m.lectures
                    WHEN 'homework' THEN m.homework WHEN 'exams' THEN m.exams
                    WHEN 'school_review' THEN m.school_review ELSE FALSE END
                    FROM {p}_student_modules m WHERE m.user_id=uid),TRUE);
            $$;
            CREATE OR REPLACE FUNCTION {p}_section_accountable(uid BIGINT,section TEXT) RETURNS BOOLEAN
            LANGUAGE SQL VOLATILE AS $$
                SELECT {p}_section_enabled(uid,section)
                    AND NOT COALESCE((SELECT listener FROM {p}_student_modules WHERE user_id=uid),FALSE);
            $$;
            CREATE OR REPLACE FUNCTION {p}_section_warning_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE section TEXT;
            BEGIN
                IF NEW.task_id IS NOT NULL AND (COALESCE(NEW.issued_by,0)=0 OR NEW.policy_key IN ('failed','rejected')) THEN
                    PERFORM 1 FROM {p}_students WHERE user_id=NEW.user_id FOR UPDATE;
                    SELECT CASE WHEN school_review_id IS NOT NULL THEN 'school_review'
                        WHEN kind='exam' THEN 'exams' ELSE 'homework' END INTO section
                        FROM {p}_tasks WHERE id=NEW.task_id;
                    IF section IS NOT NULL AND NOT {p}_section_accountable(NEW.user_id,section) THEN RETURN NULL; END IF;
                END IF;
                RETURN NEW;
            END; $$;
            DROP TRIGGER IF EXISTS {p}_section_warning_guard ON {p}_warning_log;
            CREATE TRIGGER {p}_section_warning_guard BEFORE INSERT ON {p}_warning_log
                FOR EACH ROW EXECUTE FUNCTION {p}_section_warning_guard();
            CREATE OR REPLACE FUNCTION {p}_section_default_student() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
            INSERT INTO {p}_school_review_students(user_id,active,enabled_by)
                    VALUES(NEW.user_id,TRUE,0) ON CONFLICT DO NOTHING;
                RETURN NEW;
            END; $$;
            DROP TRIGGER IF EXISTS {p}_section_default_student ON {p}_students;
            CREATE TRIGGER {p}_section_default_student AFTER INSERT ON {p}_students
                FOR EACH ROW EXECUTE FUNCTION {p}_section_default_student();
                DELETE FROM {p}_warning_log w USING {p}_tasks t
                WHERE w.task_id=t.id AND (COALESCE(w.issued_by,0)=0 OR w.policy_key IN ('failed','rejected'))
                  AND NOT {p}_section_accountable(w.user_id,CASE WHEN t.school_review_id IS NOT NULL THEN 'school_review'
                    WHEN t.kind='exam' THEN 'exams' ELSE 'homework' END);
            UPDATE {p}_students s SET warnings=(SELECT COUNT(*) FROM {p}_warning_log w WHERE w.user_id=s.user_id);
            INSERT INTO {p}_school_review_students(user_id,active,enabled_by)
                SELECT s.user_id,COALESCE(m.school_review,TRUE),COALESCE(m.changed_by,0)
                FROM {p}_students s LEFT JOIN {p}_student_modules m ON m.user_id=s.user_id
                ON CONFLICT(user_id) DO UPDATE SET active=EXCLUDED.active;
        ''')
        conn.commit()
