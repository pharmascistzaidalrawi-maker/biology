"""PDF school-review publications: 48-hour exams tied to original publish time.

Install *after* the existing school-exam/admin modules. Uses the existing review,
exam-access, task, submission and audit tables; no independent grading subsystem.
"""
from datetime import datetime, timedelta
import logging
import time

DEFAULT_HOURS = 48
MAX_TITLE = 130


def pdf_title(caption, filename):
    """Return a short plain-text review title from a PDF message."""
    first = (caption or '').strip().splitlines()
    title = (first[0] if first else '').strip()
    if not title:
        title = (filename or 'مراجعة مدرسية').rsplit('.', 1)[0]
    title = ' '.join(title.split())[:MAX_TITLE]
    return title or 'مراجعة مدرسية'


def is_pdf_document(document):
    if document is None:
        return False
    name = (getattr(document, 'file_name', '') or '').lower()
    mime = (getattr(document, 'mime_type', '') or '').lower()
    return mime == 'application/pdf' or name.endswith('.pdf')


def validate_window(start, end, now):
    if not start or not end or start.tzinfo is None or end.tzinfo is None:
        raise ValueError('الوقت يجب أن يتضمن المنطقة الزمنية.')
    if end <= start or end <= now:
        raise ValueError('النهاية يجب أن تكون بعد البداية وفي المستقبل.')
    if end - start > timedelta(days=365):
        raise ValueError('لا يمكن إنشاء امتحان مدته أكثر من سنة.')


def install(ns, db, prefix):
    if prefix not in {'chemistry', 'english', 'mathematics', 'physics', 'biology', 'french', 'islamic'}:
        raise ValueError('Unknown subject')
    B, K = ns['InlineKeyboardButton'], ns['InlineKeyboardMarkup']
    zone = ns['TIMEZONE']
    logger = ns.get('logger') or logging.getLogger(__name__)
    group_id = ns.get(f'{prefix.upper()}_GROUP_ID') or 0
    TelegramError = ns['TelegramError']
    def now(): return datetime.now(zone)
    def stamp(value): return value.astimezone(zone).strftime('%d/%m/%Y %H:%M')

    def migrate():
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(f'''ALTER TABLE {prefix}_school_reviews
                DROP CONSTRAINT IF EXISTS {prefix}_school_reviews_week_no_check;
                ALTER TABLE {prefix}_school_reviews
                    ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'weekly',
                    ADD COLUMN IF NOT EXISTS pdf_file_id TEXT,
                    ADD COLUMN IF NOT EXISTS pdf_file_unique_id TEXT,
                    ADD COLUMN IF NOT EXISTS pdf_file_name TEXT,
                    ADD COLUMN IF NOT EXISTS source_chat_id BIGINT,
                    ADD COLUMN IF NOT EXISTS source_message_id BIGINT;
                CREATE UNIQUE INDEX IF NOT EXISTS {prefix}_pdf_unique_file_idx
                    ON {prefix}_school_reviews(pdf_file_unique_id)
                    WHERE source_type='pdf' AND pdf_file_unique_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS {prefix}_pdf_source_message_idx
                    ON {prefix}_school_reviews(source_chat_id,source_message_id)
                    WHERE source_type='pdf' AND source_chat_id IS NOT NULL AND source_message_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS {prefix}_pdf_review_date_idx
                    ON {prefix}_school_reviews(exam_closes_at DESC) WHERE source_type='pdf';''')
            conn.commit()

    old_post_init = ns['post_init']
    async def post_init(app):
        await old_post_init(app)
        await db.run(migrate)
        logger.info('%s school PDF review (48h) migrations ready', prefix)
    ns['post_init'] = post_init

    def row_pdf(review):
        return bool(review and review.get('source_type') == 'pdf')

    async def sql(query, args=(), one=False):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(query, args)
                value = cur.fetchone() if one else cur.fetchall() if cur.description else None
                conn.commit()
                return value
        return await db.run(op)

    async def create_review(document, caption, actor, source_chat=None, source_message=None, published=None):
        """One atomic publication; repeated message / Telegram unique file is idempotent."""
        if not is_pdf_document(document):
            raise ValueError('الملف يجب أن يكون PDF فقط.')
        if not getattr(document, 'file_id', None):
            raise ValueError('لم يصل معرف ملف PDF من تيليغرام.')
        title = pdf_title(caption, getattr(document, 'file_name', ''))
        unique_id = getattr(document, 'file_unique_id', None) or None
        published = (published or now()).astimezone(zone)
        deadline = published + timedelta(hours=DEFAULT_HOURS)
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                # Lock only the sequence allocation, not unrelated review submissions.
                cur.execute('SELECT pg_advisory_xact_lock(49351, 7);')
                if unique_id:
                    cur.execute(f'''SELECT * FROM {prefix}_school_reviews
                        WHERE source_type='pdf' AND pdf_file_unique_id=%s;''', (unique_id,))
                    found = cur.fetchone()
                    if found: return {'status': 'duplicate', 'review': found}
                if source_chat is not None and source_message is not None:
                    cur.execute(f'''SELECT * FROM {prefix}_school_reviews WHERE source_type='pdf'
                        AND source_chat_id=%s AND source_message_id=%s;''', (source_chat, source_message))
                    found = cur.fetchone()
                    if found: return {'status': 'duplicate', 'review': found}
                cur.execute(f'SELECT COALESCE(MAX(week_no),0)+1 AS number FROM {prefix}_school_reviews;')
                week = int(cur.fetchone()['number'])
                cur.execute(f'''INSERT INTO {prefix}_school_reviews
                    (week_no,week_label,chapter_label,topics,publish_date,exam_date,
                     active,published_at,exam_opens_at,exam_closes_at,exam_manual_closed,
                     exam_deleted,source_type,pdf_file_id,pdf_file_unique_id,pdf_file_name,
                     source_chat_id,source_message_id,group_message_id)
                    VALUES(%s,%s,%s,%s,%s,%s,TRUE,%s,%s,%s,FALSE,FALSE,'pdf',%s,%s,%s,%s,%s,%s)
                    RETURNING *;''',
                    (week, f'PDF {week}', title, title, published.date(), deadline.date(),
                     published, published, deadline, document.file_id, unique_id,
                     getattr(document, 'file_name', '') or '', source_chat, source_message,
                     source_message if source_chat == group_id else None))
                review = cur.fetchone()
                cur.execute(f'''INSERT INTO {prefix}_school_review_exam_media
                    (review_id,payload_type,file_id,text_content,position,created_by)
                    VALUES(%s,'document',%s,%s,0,%s);''',
                    (review['id'], document.file_id, (caption or '')[:4000], int(actor)))
                cur.execute(f'''INSERT INTO {prefix}_audit(actor_id,action,details)
                    VALUES(%s,'school_pdf_published',%s);''',
                    (int(actor), f"review={review['id']};duration_hours=48;source={source_chat}:{source_message}"))
                conn.commit()
                return {'status': 'created', 'review': review}
        return await db.run(op)

    async def replace_pdf(review_id, document, caption, actor):
        """Atomic replacement, preserving task IDs, submissions, approvals and dates."""
        if not is_pdf_document(document):
            raise ValueError('الملف الجديد يجب أن يكون PDF.')
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''SELECT * FROM {prefix}_school_reviews WHERE id=%s
                    AND source_type='pdf' FOR UPDATE;''', (int(review_id),))
                review = cur.fetchone()
                if not review: return False
                unique_id = getattr(document, 'file_unique_id', None) or None
                if unique_id:
                    cur.execute(f'''SELECT id FROM {prefix}_school_reviews
                        WHERE source_type='pdf' AND pdf_file_unique_id=%s AND id<>%s;''',
                        (unique_id, int(review_id)))
                    if cur.fetchone(): raise ValueError('هذا الملف مربوط بامتحان PDF آخر بالفعل.')
                cur.execute(f'''UPDATE {prefix}_school_reviews SET pdf_file_id=%s,
                    pdf_file_unique_id=%s,pdf_file_name=%s,exam_deleted=FALSE
                    WHERE id=%s;''', (document.file_id, unique_id,
                    getattr(document, 'file_name', '') or '', int(review_id)))
                cur.execute(f'DELETE FROM {prefix}_school_review_exam_media WHERE review_id=%s;', (int(review_id),))
                cur.execute(f'''INSERT INTO {prefix}_school_review_exam_media
                    (review_id,payload_type,file_id,text_content,position,created_by)
                    VALUES(%s,'document',%s,%s,0,%s);''',
                    (int(review_id), document.file_id, (caption or '')[:4000], int(actor)))
                cur.execute(f'SELECT id,title FROM {prefix}_tasks WHERE school_review_id=%s FOR UPDATE;', (int(review_id),))
                for task in cur.fetchall():
                    cur.execute(f'''UPDATE {prefix}_tasks SET payload_type='document',file_id=%s,
                        text_content=%s WHERE id=%s;''', (document.file_id, task['title'], task['id']))
                    cur.execute(f'DELETE FROM {prefix}_task_media WHERE task_id=%s;', (task['id'],))
                    cur.execute(f'''INSERT INTO {prefix}_task_media(task_id,payload_type,file_id,source_message_id)
                        VALUES(%s,'document',%s,%s) ON CONFLICT DO NOTHING;''',
                        (task['id'], document.file_id, -(870000000000000000+int(task['id'])*1000)))
                cur.execute(f'''INSERT INTO {prefix}_audit(actor_id,action,details)
                    VALUES(%s,'school_pdf_replaced',%s);''', (int(actor), f'review={review_id}'))
                conn.commit()
                return True
        return await db.run(op)

    async def control(review_id, action, actor, start=None, end=None):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''SELECT * FROM {prefix}_school_reviews WHERE id=%s
                    AND source_type='pdf' FOR UPDATE;''', (int(review_id),))
                r = cur.fetchone()
                if not r: return False
                a, b = r['exam_opens_at'], r['exam_closes_at']
                if action == 'delete':
                    cur.execute(f'''UPDATE {prefix}_school_reviews SET exam_deleted=TRUE,
                        exam_manual_closed=TRUE WHERE id=%s;''', (review_id,))
                    cur.execute(f'DELETE FROM {prefix}_school_review_exam_media WHERE review_id=%s;', (review_id,))
                    cur.execute(f'''WITH removed AS (
                        DELETE FROM {prefix}_warning_log l USING {prefix}_tasks t
                        WHERE l.task_id=t.id AND t.school_review_id=%s
                        RETURNING l.user_id), counted AS (
                        SELECT user_id,COUNT(*)::INTEGER AS n FROM removed GROUP BY user_id)
                        UPDATE {prefix}_students s SET warnings=GREATEST(0,s.warnings-c.n)
                        FROM counted c WHERE s.user_id=c.user_id;''', (review_id,))
                    cur.execute(f'DELETE FROM {prefix}_tasks WHERE school_review_id=%s;', (review_id,))
                    cur.execute(f'''UPDATE {prefix}_school_review_progress SET task_id=NULL,
                        approval_notified_at=NULL WHERE review_id=%s;''', (review_id,))
                else:
                    if action == 'close':
                        closed = True
                    else:
                        closed = False
                        if action == 'reopen':
                            a = now(); b = a + timedelta(hours=DEFAULT_HOURS)
                        elif action == 'schedule':
                            validate_window(start, end, now()); a, b = start, end
                        elif action.startswith('extend:'):
                            hours = int(action.partition(':')[2])
                            if not 1 <= hours <= 720: raise ValueError('التمديد من ساعة إلى 720 ساعة.')
                            b = max(b, now()) + timedelta(hours=hours)
                        else:
                            raise ValueError('أمر غير صالح.')
                    if b <= a: raise ValueError('وقت النهاية غير صالح.')
                    cur.execute(f'''UPDATE {prefix}_school_reviews SET exam_opens_at=%s,
                        exam_closes_at=%s,exam_manual_closed=%s,exam_date=%s,
                        publish_date=LEAST(publish_date,%s) WHERE id=%s;''',
                        (a,b,closed,b.astimezone(zone).date(),a.astimezone(zone).date(),review_id))
                    cur.execute(f'''UPDATE {prefix}_tasks SET deadline=%s,exam_available_at=%s,
                        closed=%s,exam_duration_hours=%s,teacher_deadline_reminder_sent=FALSE
                        WHERE school_review_id=%s;''',
                        (b,a,closed,max(1,int((b-a).total_seconds() / 3600 + 0.999)),review_id))
                    if closed or action in ('schedule','reopen'):
                        cur.execute(f'''DELETE FROM {prefix}_task_extensions WHERE task_id IN
                            (SELECT id FROM {prefix}_tasks WHERE school_review_id=%s);''', (review_id,))
                    if action in ('schedule','reopen'):
                        cur.execute(f'''UPDATE {prefix}_late_exam_requests SET status='denied',
                            decided_by=%s,decided_at=CURRENT_TIMESTAMP WHERE status='pending'
                            AND task_id IN (SELECT id FROM {prefix}_tasks WHERE school_review_id=%s);''',
                            (int(actor), review_id))
                cur.execute(f'''INSERT INTO {prefix}_audit(actor_id,action,details)
                    VALUES(%s,%s,%s);''', (int(actor), 'school_pdf_' + action, f'review={review_id}'))
                conn.commit()
                return True
        return await db.run(op)

    async def pdf_list(query, page=0):
        page = max(0, int(page))
        rows = await sql(f'''SELECT id,chapter_label,exam_closes_at,exam_manual_closed,exam_deleted
            FROM {prefix}_school_reviews WHERE source_type='pdf' ORDER BY id DESC
            LIMIT 11 OFFSET %s;''', (page * 10,))
        kb = []
        for r in rows[:10]:
            status = '🗑' if r['exam_deleted'] else '🔒' if r['exam_manual_closed'] or r['exam_closes_at'] <= now() else '🟢'
            kb.append([B(f"{status} {r['chapter_label'][:38]}", callback_data=f"pdf|panel|{r['id']}")])
        if page: kb.append([B('◀️ السابق',callback_data=f'pdf|list|{page-1}')])
        if len(rows) > 10: kb.append([B('التالي ▶️',callback_data=f'pdf|list|{page+1}')])
        kb.append([B('➕ نشر PDF جديد',callback_data='pdf|new')])
        kb.append([B('◀️ إدارة المراجعة',callback_data='v54_school_admin')])
        await query.edit_message_text('📄 امتحانات PDF المنشورة\nاختر الملف للتحكم به.\n🟢 مفتوح | 🔒 مغلق | 🗑 محذوف',reply_markup=K(kb))

    async def panel(query, rid):
        r = await db.v54_school_review_by_id(int(rid))
        if not row_pdf(r):
            await query.answer('هذا الملف غير موجود.',show_alert=True); return
        count = int(r.get('media_count') or 0)
        status = '🗑 تم حذف الامتحان' if r['exam_deleted'] else '🔒 مغلق' if r['exam_manual_closed'] or r['exam_closes_at'] <= now() else '⏳ لم يبدأ' if r['exam_opens_at'] > now() else '🟢 مفتوح'
        txt = (f"📄 {r['chapter_label']}\n"
               f"📌 الحالة: {status}\n"
               f"📅 النشر: {stamp(r['published_at'])}\n"
               f"▶️ الفتح: {stamp(r['exam_opens_at'])}\n"
               f"⏰ الانتهاء: {stamp(r['exam_closes_at'])}\n"
               f"📎 عدد أجزاء الامتحان: {count}\n"
               '🔐 إكمال المراجعة وموافقة ولي الأمر مطلوبان حسب إعدادات البوت.')
        kb = [
            [B('📄 عرض PDF',callback_data=f'pdf|view|{rid}'),B('🔁 استبدال PDF',callback_data=f'pdf|replace|{rid}')],
            [B('🗓 تعديل البداية والنهاية',callback_data=f'sex|schedule|{rid}')],
            [B('⏳ تمديد ساعة',callback_data=f'sex|extend:1|{rid}'),B('⏳ تمديد يوم',callback_data=f'sex|extend:24|{rid}')],
            [B('⏳ تمديد مخصص',callback_data=f'pdf|custom|{rid}')],
            [B('🔓 إعادة الفتح 48 ساعة',callback_data=f'sex|reopen|{rid}'),B('🔒 إغلاق الآن',callback_data=f'sex|close|{rid}')],
            [B('👥 تسليمات الطلاب',callback_data=f'pdf|students|{rid}|0'),B('📜 سجل التعديلات',callback_data=f'pdf|audit|{rid}')],
            [B('🗑 حذف الامتحان نهائياً',callback_data=f'sex|delete_confirm|{rid}')],
            [B('◀️ ملفات PDF',callback_data='pdf|list|0')]]
        await query.edit_message_text(txt,reply_markup=K(kb))

    async def students(query, rid, page):
        page = max(0, int(page))
        rows = await sql(f'''SELECT s.user_id,s.full_name,p.completed_at,sub.submitted_at,
            t.id AS task_id FROM {prefix}_school_review_students e
            JOIN {prefix}_students s ON s.user_id=e.user_id AND e.active=TRUE
            LEFT JOIN {prefix}_school_review_progress p ON p.user_id=s.user_id AND p.review_id=%s
            LEFT JOIN {prefix}_tasks t ON t.id=p.task_id
            LEFT JOIN {prefix}_submissions sub ON sub.task_id=t.id AND sub.user_id=s.user_id
            WHERE s.approved=TRUE AND s.reset_pending=FALSE AND s.study_track='course'
            ORDER BY s.full_name,s.user_id LIMIT 11 OFFSET %s;''', (rid, page * 10))
        kb = []
        for r in rows[:10]:
            icon = '✅' if r['submitted_at'] else '📚' if r['completed_at'] else '⏳'
            kb.append([B(f"{icon} {r['full_name'][:35]}",callback_data=f"pdf|student|{rid}|{r['user_id']}")])
        if page: kb.append([B('◀️ السابق',callback_data=f'pdf|students|{rid}|{page-1}')])
        if len(rows)>10: kb.append([B('التالي ▶️',callback_data=f'pdf|students|{rid}|{page+1}')])
        kb.append([B('◀️ الامتحان',callback_data=f'pdf|panel|{rid}')])
        await query.edit_message_text('👥 حالة الطلاب\n✅ مسلّم | 📚 أكمل المراجعة | ⏳ لم يكمل',reply_markup=K(kb))

    async def student(query, rid, uid):
        r = await sql(f'''SELECT s.full_name,p.completed_at,t.id AS task_id,sub.submitted_at,
            t.deadline FROM {prefix}_students s
            LEFT JOIN {prefix}_school_review_progress p ON p.user_id=s.user_id AND p.review_id=%s
            LEFT JOIN {prefix}_tasks t ON t.id=p.task_id
            LEFT JOIN {prefix}_submissions sub ON sub.task_id=t.id AND sub.user_id=s.user_id
            WHERE s.user_id=%s;''', (rid,uid), True)
        if not r: await query.answer('الطالب غير موجود.',show_alert=True); return
        text = f"👤 {r['full_name']}\n📚 المراجعة: {'مكتملة' if r['completed_at'] else 'لم تكتمل'}\n📝 التسليم: {'تم' if r['submitted_at'] else 'لم يتم'}"
        kb = []
        if r['task_id'] and not r['submitted_at']:
            kb.append([B('⏳ تمديد 24 ساعة لهذا الطالب',callback_data=f'pdf|studentextend|{rid}|{uid}')])
        kb.append([B('◀️ الطلاب',callback_data=f'pdf|students|{rid}|0')])
        await query.edit_message_text(text,reply_markup=K(kb))

    async def extend_student(rid, uid, actor):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f'''SELECT t.id,t.deadline FROM {prefix}_tasks t
                    JOIN {prefix}_school_reviews r ON r.id=t.school_review_id AND r.source_type='pdf'
                    WHERE r.id=%s AND t.target_scope=%s FOR UPDATE OF t;''', (rid,f'student:{uid}'))
                t=cur.fetchone()
                if not t:return False
                cur.execute(f'''SELECT submitted_at FROM {prefix}_submissions WHERE task_id=%s AND user_id=%s;''',(t['id'],uid))
                sub=cur.fetchone()
                if sub and sub['submitted_at']:return False
                cur.execute(f'''SELECT extended_until FROM {prefix}_task_extensions WHERE task_id=%s AND user_id=%s;''',(t['id'],uid))
                ext=cur.fetchone()
                base=max(t['deadline'], (ext or {}).get('extended_until') or t['deadline'],now())
                cur.execute(f'''INSERT INTO {prefix}_task_extensions(task_id,user_id,requested_at,extended_until)
                    VALUES(%s,%s,CURRENT_TIMESTAMP,%s)
                    ON CONFLICT(task_id,user_id) DO UPDATE SET extended_until=EXCLUDED.extended_until,
                    requested_at=CURRENT_TIMESTAMP;''', (t['id'],uid,base+timedelta(hours=24)))
                cur.execute(f'''UPDATE {prefix}_tasks SET closed=FALSE,
                    exam_pending_activation=FALSE,exam_approval_required=FALSE,
                    teacher_deadline_reminder_sent=FALSE WHERE id=%s;''',(t['id'],))
                cur.execute(f'''UPDATE {prefix}_exam_access SET status='approved',
                    approved_by=%s,approved_at=CURRENT_TIMESTAMP
                    WHERE task_id=%s AND user_id=%s;''',(actor,t['id'],uid))
                cur.execute(f'''INSERT INTO {prefix}_audit(actor_id,action,details)
                    VALUES(%s,'school_pdf_student_extended',%s);''',(actor,f'review={rid};user={uid};hours=24'))
                conn.commit();return True
        return await db.run(op)

    async def audit(query, rid):
        rows=await sql(f'''SELECT actor_id,action,created_at FROM {prefix}_audit
            WHERE action LIKE 'school_pdf_%%' AND (details=%s OR details LIKE %s)
            ORDER BY id DESC LIMIT 10;''',(f'review={rid}',f'review={rid};%'))
        lines=['📜 آخر تعديلات الامتحان']
        for r in rows:
            lines.append(f"{stamp(r['created_at'])} | {r['actor_id']} | {r['action']}")
        if len(lines)==1:lines.append('لا توجد تعديلات مسجلة.')
        await query.edit_message_text('\n'.join(lines),reply_markup=K([[B('◀️ الامتحان',callback_data=f'pdf|panel|{rid}')]]))

    async def announce(context, r, limit=50):
        """Retry-safe best-effort fanout, with one row per successfully sent notification."""
        students = await sql(f'''SELECT s.user_id,s.full_name,s.parent_chat_id
            FROM {prefix}_school_review_students e
            JOIN {prefix}_students s ON s.user_id=e.user_id
            WHERE e.active=TRUE AND s.approved=TRUE AND s.reset_pending=FALSE
            AND s.study_track='course' AND NOT EXISTS(
              SELECT 1 FROM {prefix}_school_review_notifications n
              WHERE n.review_id=%s AND n.user_id=s.user_id AND n.kind='pdf_published')
            ORDER BY s.user_id LIMIT %s;''', (r['id'], limit))
        for s in students:
            try:
                await context.bot.send_message(s['user_id'],
                    f"📄 مراجعة مدرسة جديدة: {r['chapter_label']}\n"
                    f"📝 امتحان PDF مرتبط بالمراجعة\n"
                    f"⏰ النهاية: {stamp(r['exam_closes_at'])} (بتوقيت بغداد)\n"
                    'افتح المراجعة وأكملها لطلب موافقة ولي الأمر على الامتحان.',
                    reply_markup=K([[B('📚 فتح المراجعة',callback_data=f"v54_school_review_open|{r['id']}")]]))
                await db.v54_mark_school_review_notification(r['id'],s['user_id'],'pdf_published')
            except TelegramError:
                logger.warning('Could not send school PDF to student %s',s['user_id'])
            # Parent notifications are independently deduplicated.
            parent_ids = {int(s['parent_chat_id'])} if s.get('parent_chat_id') else set()
            try:
                for parent in await db.student_parents(s['user_id'],True):
                    if parent.get('parent_chat_id'): parent_ids.add(int(parent['parent_chat_id']))
            except Exception:
                logger.exception('Could not load linked parents for %s',s['user_id'])
            for pid in parent_ids:
                if pid == s['user_id']: continue
                kind=f'pdf_parent_{s["user_id"]}'
                already = await sql(f'''SELECT 1 AS yes FROM {prefix}_school_review_notifications
                    WHERE review_id=%s AND user_id=%s AND kind=%s;''',(r['id'],pid,kind),True)
                if already:continue
                try:
                    await context.bot.send_message(pid,
                        f"📄 مراجعة جديدة للطالب {s['full_name']}\n"
                        f"📚 {r['chapter_label']}\n⏰ انتهاء الامتحان: {stamp(r['exam_closes_at'])}")
                    await db.v54_mark_school_review_notification(r['id'],pid,kind)
                except TelegramError:
                    logger.warning('Could not notify parent %s',pid)

    async def pdf_job(context):
        # Includes retries for missed publication notifications after restarts.
        reviews = await sql(f'''SELECT r.* FROM {prefix}_school_reviews r WHERE r.source_type='pdf'
            AND r.active=TRUE AND r.exam_deleted=FALSE AND r.published_at IS NOT NULL
            AND r.exam_closes_at>CURRENT_TIMESTAMP
            AND EXISTS(SELECT 1 FROM {prefix}_school_review_students e
                JOIN {prefix}_students s ON s.user_id=e.user_id
                WHERE e.active=TRUE AND s.approved=TRUE AND s.reset_pending=FALSE
                AND s.study_track='course' AND NOT EXISTS(
                  SELECT 1 FROM {prefix}_school_review_notifications n
                  WHERE n.review_id=r.id AND n.user_id=s.user_id AND n.kind='pdf_published'))
            ORDER BY r.id DESC LIMIT 10;''')
        for r in reviews:
            await announce(context,r,limit=100)
        # Parent delivery is retried independently of the student's notification.
        parent_rows = await sql(f'''SELECT DISTINCT r.id AS review_id,r.chapter_label,
            r.exam_closes_at,s.user_id AS student_id,s.full_name,parent.pid AS parent_id
            FROM {prefix}_school_reviews r
            JOIN {prefix}_school_review_students e ON e.active=TRUE
            JOIN {prefix}_students s ON s.user_id=e.user_id AND s.approved=TRUE
                AND s.reset_pending=FALSE AND s.study_track='course'
            JOIN LATERAL (
                SELECT s.parent_chat_id AS pid WHERE s.parent_chat_id IS NOT NULL
                UNION
                SELECT l.parent_chat_id AS pid FROM {prefix}_parent_links l
                WHERE l.student_id=s.user_id AND l.approved=TRUE
            ) parent ON parent.pid IS NOT NULL AND parent.pid<>s.user_id
            WHERE r.source_type='pdf' AND r.active=TRUE AND r.exam_deleted=FALSE
              AND r.published_at IS NOT NULL AND r.exam_closes_at>CURRENT_TIMESTAMP
              AND NOT EXISTS(SELECT 1 FROM {prefix}_school_review_notifications n
                WHERE n.review_id=r.id AND n.user_id=parent.pid
                  AND n.kind=('pdf_parent_'||s.user_id::text))
            ORDER BY review_id DESC LIMIT 100;''')
        for r in parent_rows:
            try:
                await context.bot.send_message(r['parent_id'],
                    f"📄 مراجعة جديدة للطالب {r['full_name']}\n"
                    f"📚 {r['chapter_label']}\n⏰ انتهاء الامتحان: {stamp(r['exam_closes_at'])}")
                await db.v54_mark_school_review_notification(r['review_id'],r['parent_id'],
                    f"pdf_parent_{r['student_id']}")
            except TelegramError: pass
        for kind, hours in (('pdf_remind_24',24),('pdf_remind_6',6)):
            due = await sql(f'''SELECT r.id,r.chapter_label,r.exam_closes_at,s.user_id
                FROM {prefix}_school_reviews r
                JOIN {prefix}_school_review_students e ON e.active=TRUE
                JOIN {prefix}_students s ON s.user_id=e.user_id AND s.approved=TRUE
                    AND s.reset_pending=FALSE AND s.study_track='course'
                WHERE r.source_type='pdf' AND r.active=TRUE AND r.exam_deleted=FALSE
                  AND r.exam_manual_closed=FALSE AND r.exam_closes_at>CURRENT_TIMESTAMP
                  AND r.exam_closes_at<=CURRENT_TIMESTAMP+(%s * INTERVAL '1 hour')
                  AND NOT EXISTS(SELECT 1 FROM {prefix}_school_review_notifications n
                      WHERE n.review_id=r.id AND n.user_id=s.user_id AND n.kind=%s)
                ORDER BY r.exam_closes_at LIMIT 100;''',(hours,kind))
            for r in due:
                try:
                    await context.bot.send_message(r['user_id'],
                        f"⏰ تذكير امتحان PDF: {r['chapter_label']}\n"
                        f"موعد الانتهاء: {stamp(r['exam_closes_at'])}",
                        reply_markup=K([[B('📝 فتح الامتحان',callback_data=f"v54_school_exam_open|{r['id']}")]]))
                    await db.v54_mark_school_review_notification(r['id'],r['user_id'],kind)
                except TelegramError: pass
        ended = await sql(f'''SELECT r.id,r.chapter_label,s.user_id FROM {prefix}_school_reviews r
            JOIN {prefix}_school_review_students e ON e.active=TRUE
            JOIN {prefix}_students s ON s.user_id=e.user_id AND s.approved=TRUE
                AND s.reset_pending=FALSE AND s.study_track='course'
            WHERE r.source_type='pdf' AND r.active=TRUE AND r.exam_deleted=FALSE
              AND (r.exam_manual_closed=TRUE OR r.exam_closes_at<=CURRENT_TIMESTAMP)
              AND r.published_at>=CURRENT_TIMESTAMP-INTERVAL '7 days'
              AND NOT EXISTS(SELECT 1 FROM {prefix}_school_review_notifications n
                WHERE n.review_id=r.id AND n.user_id=s.user_id AND n.kind='pdf_ended')
            ORDER BY review_id DESC LIMIT 100;''')
        for r in ended:
            try:
                await context.bot.send_message(r['user_id'],f"🔒 انتهى امتحان PDF: {r['chapter_label']}")
                await db.v54_mark_school_review_notification(r['id'],r['user_id'],'pdf_ended')
            except TelegramError: pass

    old_job=ns['v54_school_review_job']
    async def v54_school_review_job(context):
        await old_job(context)
        try: await pdf_job(context)
        except Exception: logger.exception('School PDF notification job failed')
    ns['v54_school_review_job']=v54_school_review_job

    old_admin_menu=ns['v54_school_admin_menu']
    async def admin_menu(query):
        # Keep existing student activation and weekly-review management intact.
        rows = await db.v54_school_review_catalog()
        n = sum(1 for r in rows if row_pdf(r))
        kb=K([[B('➕ نشر PDF + امتحان 48 ساعة',callback_data='pdf|new')],
              [B(f'📄 ملفات PDF ({n})',callback_data='pdf|list|0')],
              [B('👥 تفعيل الطلاب المحددين',callback_data='v54_school_students|0')],
              [B('🗓 أسابيع المراجعة التقليدية',callback_data='v54_school_catalog')],
              [ns['back_menu']()]])
        await query.edit_message_text('🏫 إدارة مراجعة المدرسة\n\n'
            '📄 أي PDF تنشره من هنا يصبح امتحاناً لمدة 48 ساعة من وقت النشر.\n'
            'يمكنك التحكم بالبداية والنهاية والتمديد والإغلاق والاستبدال والتسليمات.\n'
            'الملفات المنشورة مباشرة في موضوع مراجعة المدرسة داخل المجموعة تُربط أيضاً تلقائياً.',reply_markup=kb)
    ns['v54_school_admin_menu']=admin_menu

    old_catalog=ns['v54_school_admin_catalog']
    async def admin_catalog(query):
        catalog = await db.v54_school_review_catalog()
        kb=[]
        for r in catalog:
            if row_pdf(r):continue
            count=int(r.get('media_count') or 0)
            kb.append([B(f"{'✅' if count else '📭'} الأسبوع {r['week_label']}",callback_data=f"v54_school_admin_review|{r['id']}")])
        kb.append([B('📄 امتحانات PDF',callback_data='pdf|list|0')])
        kb.append([B('◀️ إدارة المراجعة',callback_data='v54_school_admin')])
        await query.edit_message_text('🗓 أسابيع مراجعة المدرسة التقليدية',reply_markup=K(kb))
    ns['v54_school_admin_catalog']=admin_catalog

    old_admin_review=ns['v54_school_admin_review']
    async def admin_review(query,rid):
        r=await db.v54_school_review_by_id(rid)
        if row_pdf(r):return await panel(query,rid)
        return await old_admin_review(query,rid)
    ns['v54_school_admin_review']=admin_review

    old_review_menu=ns['v54_school_review_menu']
    async def review_menu(query):
        access=await db.v54_school_review_access(query.from_user.id)
        if not access:return await old_review_menu(query)
        catalog=await db.v54_school_review_catalog(query.from_user.id)
        pdfs=[r for r in catalog if row_pdf(r) and r['published_at'] and not r['exam_deleted']
              and r['exam_closes_at']>now() and not r['exam_manual_closed']]
        weekly=[r for r in catalog if not row_pdf(r) and r.get('published_at')
                and r['publish_date']<=now().date()<=r['exam_date']]
        if not pdfs:return await old_review_menu(query)
        kb=[]
        for r in sorted(pdfs,key=lambda x:x['id'],reverse=True)[:12]:
            kb.append([B(f"📄 {r['chapter_label'][:45]}",callback_data=f"v54_school_review_open|{r['id']}")])
        for r in weekly:
            kb.append([B(f"📚 الأسبوع {r['week_label']}",callback_data=f"v54_school_review_open|{r['id']}")])
        kb.extend([[B('📝 امتحانات المراجعة',callback_data='v54_school_exams')],[ns['back_menu']()]])
        await query.answer()
        await query.edit_message_text('🏫 مراجعة المدرسة\n📄 ملفات PDF الجديدة والامتحانات المرتبطة بها:',reply_markup=K(kb))
    ns['v54_school_review_menu']=review_menu

    old_review_detail=ns['v54_school_review_detail']
    async def review_detail(query,rid):
        r=await db.v54_school_review_by_id(rid,query.from_user.id)
        if not row_pdf(r):return await old_review_detail(query,rid)
        access=await db.v54_school_review_access(query.from_user.id)
        if not access or not r.get('published_at'):
            await query.answer('المراجعة غير متاحة لحسابك.',show_alert=True);return
        expired=r['exam_closes_at']<=now() or r['exam_manual_closed'] or r['exam_deleted']
        kb=[]
        if r.get('completed_at'):
            kb.append([B('📝 فتح الامتحان / طلب دخول',callback_data=f'v54_school_exam_open|{rid}')])
        elif not expired:
            kb.append([B('✅ أكملت مراجعة هذا الملف',callback_data=f'v54_school_complete|{rid}')])
        kb.append([B('📄 إعادة إرسال PDF',callback_data=f'pdfstudent|file|{rid}')])
        kb.extend([[B('◀️ مراجعة المدرسة',callback_data='v54_school_review')],
                   [ns['back_menu']()]])
        await query.answer()
        await query.edit_message_text(
            f"📄 {r['chapter_label']}\n▶️ البداية: {stamp(r['exam_opens_at'])}\n"
            f"⏰ النهاية: {stamp(r['exam_closes_at'])}\n"
            + ('🔒 الامتحان مغلق أو منتهي.' if expired else '🟢 الامتحان ضمن مدته.'),
            reply_markup=K(kb))
        if r.get('pdf_file_id'):
            try:
                await query.get_bot().send_document(query.from_user.id,r['pdf_file_id'],
                    caption=f"📄 {r['chapter_label']}\n⏰ نهاية الامتحان: {stamp(r['exam_closes_at'])}",
                    protect_content=True)
            except TelegramError:
                logger.exception('Could not send school PDF %s to student %s',rid,query.from_user.id)
    ns['v54_school_review_detail']=review_detail

    old_review_text=ns['_v54_school_review_text']
    def review_text(review, heading='🏫 مراجعة للمدرسة'):
        if row_pdf(review):
            return (f"{heading}\n📄 {review['chapter_label']}\n"
                    f"📝 امتحان PDF مرتبط بالمراجعة\n"
                    f"▶️ الفتح: {stamp(review['exam_opens_at'])}\n"
                    f"⏰ النهاية: {stamp(review['exam_closes_at'])}")
        return old_review_text(review,heading)
    ns['_v54_school_review_text']=review_text

    old_private=ns['private_messages']
    async def private_messages(update,context):
        uid=update.effective_user.id
        oath=context.user_data.get('awaiting_school_review_oath')
        if oath and not ns['is_admin'](uid):
            r=await db.v54_school_review_by_id(oath['review_id'],uid)
            if row_pdf(r) and (r['exam_deleted'] or r['exam_manual_closed'] or r['exam_closes_at']<=now()):
                context.user_data.pop('awaiting_school_review_oath',None)
                await update.effective_message.reply_text('🔒 انتهى أو أغلق امتحان PDF؛ لا يمكن إكمال المراجعة لبدء محاولة جديدة.')
                return
        state=context.user_data.get('school_pdf_input')
        if state and ns['is_admin'](uid):
            msg=update.effective_message
            if time.monotonic()-state['started']>600:
                context.user_data.pop('school_pdf_input',None)
                await msg.reply_text('⏳ انتهت مهلة الإدخال. ابدأ من جديد.');return
            action,rid=state['action'],state.get('rid')
            if action in ('new','replace'):
                if not is_pdf_document(msg.document):
                    await msg.reply_text('⚠️ أرسل ملف PDF فقط، وليس صورة أو نصاً.');return
                try:
                    if action=='new':
                        result=await create_review(msg.document,msg.caption,uid)
                    else:
                        ok=await replace_pdf(rid,msg.document,msg.caption,uid)
                        result={'status':'replaced' if ok else 'missing','review':await db.v54_school_review_by_id(rid)}
                except ValueError as exc:
                    await msg.reply_text(f'⚠️ {exc}');return
                except Exception:
                    logger.exception('Could not save school PDF from private admin chat')
                    await msg.reply_text('⚠️ تعذر تأكيد حفظ PDF في قاعدة البيانات. لم يُعلن نجاح النشر؛ حاول مجدداً بعد التحقق من الاتصال.')
                    return
                context.user_data.pop('school_pdf_input',None)
                r=result['review']
                if result['status']=='missing':await msg.reply_text('المراجعة لم تعد موجودة.');return
                label='تم النشر والربط' if result['status']=='created' else 'الملف موجود مسبقاً' if result['status']=='duplicate' else 'تم استبدال الملف'
                kb=K([[B('⚙️ التحكم بالامتحان',callback_data=f"pdf|panel|{r['id']}")]])
                await msg.reply_text(f"✅ {label}\n📄 {r['chapter_label']}\n"
                    f"⏰ نهاية الامتحان: {stamp(r['exam_closes_at'])}",reply_markup=kb)
                if result['status']=='created' and group_id:
                    try:
                        sent=await context.bot.send_document(group_id,msg.document.file_id,
                            caption=f"📄 مراجعة مدرسة جديدة\n{r['chapter_label']}\n"
                                    f"⏰ الامتحان ينتهي {stamp(r['exam_closes_at'])}",
                            message_thread_id=ns.get('SCHOOL_REVIEW_TOPIC_ID') or None)
                        await sql(f'''UPDATE {prefix}_school_reviews SET group_message_id=%s
                            WHERE id=%s AND group_message_id IS NULL;''',(sent.message_id,r['id']))
                    except TelegramError:
                        await msg.reply_text('⚠️ تم النشر داخل البوت، لكن تعذر إرساله إلى مجموعة مراجعة المدرسة. تحقق من صلاحيات البوت ومعرّف الموضوع.')
                return
            if action=='schedule':
                try:
                    pieces=(msg.text or '').split('|')
                    if len(pieces)!=2:raise ValueError
                    a,b=[datetime.strptime(x.strip(),'%d/%m/%Y %H:%M').replace(tzinfo=zone) for x in pieces]
                    validate_window(a,b,now())
                    await control(rid,'schedule',uid,a,b)
                except ValueError:
                    await msg.reply_text('أرسل الوقتين هكذا:\n11/10/2026 18:00 | 13/10/2026 18:00\nويجب أن تكون النهاية بالمستقبل.');return
                context.user_data.pop('school_pdf_input',None)
                await msg.reply_text('✅ تم تعديل وقت الامتحان لجميع الطلاب.',reply_markup=K([[B('⚙️ الامتحان',callback_data=f'pdf|panel|{rid}')]]));return
            if action=='custom':
                try:
                    hours=int((msg.text or '').strip())
                    if not 1<=hours<=720:raise ValueError
                    await control(rid,f'extend:{hours}',uid)
                except ValueError:
                    await msg.reply_text('أرسل عدداً صحيحاً من 1 إلى 720 ساعة.');return
                context.user_data.pop('school_pdf_input',None)
                await msg.reply_text(f'✅ تم تمديد الامتحان {hours} ساعة.',reply_markup=K([[B('⚙️ الامتحان',callback_data=f'pdf|panel|{rid}')]]));return
        return await old_private(update,context)
    ns['private_messages']=private_messages

    old_button=ns['button_handler']
    async def button_handler(update,context):
        q=update.callback_query;data=q.data or '';uid=q.from_user.id
        if data=='menu':context.user_data.pop('school_pdf_input',None)
        if data.startswith('v54_school_complete|') and not ns['is_admin'](uid):
            try: rid=int(data.split('|')[1])
            except (ValueError,IndexError):return await old_button(update,context)
            r=await db.v54_school_review_by_id(rid,uid)
            if row_pdf(r) and (r['exam_deleted'] or r['exam_manual_closed'] or r['exam_closes_at']<=now()):
                await q.answer('🔒 انتهى أو أغلق هذا الامتحان.',show_alert=True);return
        if data.startswith('pdfstudent|file|'):
            try: rid=int(data.split('|')[2])
            except (ValueError,IndexError):await q.answer('رابط غير صالح.',show_alert=True);return
            if not await db.v54_school_review_access(uid):
                await q.answer('هذه المراجعة غير مفعلة لك.',show_alert=True);return
            r=await db.v54_school_review_by_id(rid,uid)
            if not row_pdf(r) or not r.get('published_at') or not r.get('pdf_file_id'):
                await q.answer('الملف غير موجود.',show_alert=True);return
            await q.answer()
            try:await context.bot.send_document(uid,r['pdf_file_id'],caption=f"📄 {r['chapter_label']}",protect_content=True)
            except TelegramError:await q.message.reply_text('⚠️ تعذر إرسال الملف.')
            return
        if data.startswith('pdf|') or data.startswith('sex|'):
            if not ns['is_admin'](uid):
                await q.answer('للإدارة فقط.',show_alert=True);return
        if data.startswith('sex|'):
            parts=data.split('|')
            if len(parts)!=3 or not parts[2].isdigit():
                await q.answer('أمر غير صالح.',show_alert=True);return
            _,action,rid_str=parts;rid=int(rid_str)
            r=await db.v54_school_review_by_id(rid)
            if row_pdf(r):
                if action=='schedule':
                    context.user_data['school_pdf_input']={'action':'schedule','rid':rid,'started':time.monotonic()}
                    await q.answer()
                    await q.edit_message_text('🗓 أرسل وقت البداية والنهاية في الخاص بتوقيت بغداد:\n'
                        '11/10/2026 18:00 | 13/10/2026 18:00',
                        reply_markup=K([[B('إلغاء',callback_data=f'pdf|panel|{rid}')]]));return
                if action=='delete_confirm':
                    await q.answer()
                    await q.edit_message_text('⚠️ سيحذف الامتحان وتسليماته نهائياً، وتبقى مراجعة PDF. هل تؤكد؟',
                        reply_markup=K([[B('🗑 تأكيد الحذف',callback_data=f'sex|delete|{rid}')],
                                        [B('إلغاء',callback_data=f'pdf|panel|{rid}')]]));return
                if action not in {'delete','close','reopen','extend:1','extend:24'}:
                    await q.answer('خيار غير صالح.',show_alert=True);return
                await q.answer()
                try: await control(rid,action,uid)
                except ValueError as exc:
                    await q.edit_message_text(f'⚠️ {exc}');return
                await panel(q,rid);return
        if data.startswith('pdf|'):
            parts=data.split('|');action=parts[1]
            if action=='new':
                context.user_data['school_pdf_input']={'action':'new','started':time.monotonic()}
                await q.answer()
                await q.edit_message_text('📄 أرسل ملف PDF الآن في الخاص.\n'
                    'بمجرد إرساله ينشر كـمراجعة جديدة ويبدأ امتحان 48 ساعة تلقائياً.',
                    reply_markup=K([[B('إلغاء',callback_data='v54_school_admin')]]));return
            if action=='list':
                await q.answer();await pdf_list(q,int(parts[2]));return
            if len(parts)<3 or not parts[2].isdigit():
                await q.answer('رابط غير صالح.',show_alert=True);return
            rid=int(parts[2])
            if action=='panel':
                context.user_data.pop('school_pdf_input',None)
                await q.answer();await panel(q,rid);return
            if action=='replace':
                context.user_data['school_pdf_input']={'action':'replace','rid':rid,'started':time.monotonic()}
                await q.answer();await q.edit_message_text('🔁 أرسل ملف PDF البديل في الخاص.\n'
                    'لن تتغير مواعيد الامتحان ولا تسليمات الطلاب.',
                    reply_markup=K([[B('إلغاء',callback_data=f'pdf|panel|{rid}')]]));return
            if action in ('custom','audit','view','students','student','studentextend'):
                await q.answer()
                if action=='custom':
                    context.user_data['school_pdf_input']={'action':'custom','rid':rid,'started':time.monotonic()}
                    await q.edit_message_text('⏳ أرسل عدد ساعات التمديد (1 إلى 720) في الخاص.',
                        reply_markup=K([[B('إلغاء',callback_data=f'pdf|panel|{rid}')]]));return
                if action=='audit':await audit(q,rid);return
                if action=='students':await students(q,rid,int(parts[3]));return
                if action=='student':await student(q,rid,int(parts[3]));return
                if action=='studentextend':
                    ok=await extend_student(rid,int(parts[3]),uid)
                    await q.edit_message_text('✅ تم تمديد الطالب 24 ساعة.' if ok else 'لم يمكن التمديد (لا يوجد امتحان مفتوح للطالب أو تم التسليم).',
                        reply_markup=K([[B('◀️ الطلاب',callback_data=f'pdf|students|{rid}|0')]]));return
                r=await db.v54_school_review_by_id(rid)
                if r and r.get('pdf_file_id'):
                    try:await context.bot.send_document(uid,r['pdf_file_id'],caption=f"📄 {r['chapter_label']}")
                    except TelegramError:await q.message.reply_text('⚠️ تعذر إرسال الملف. تحقق من معرف الملف.')
                await panel(q,rid);return
        return await old_button(update,context)
    ns['button_handler']=button_handler

    old_capture=ns['capture_group_task']
    async def capture_group_task(update,context):
        msg=update.effective_message
        if (msg and ns.get('SCHOOL_REVIEW_TOPIC_ID') and group_id
            and update.effective_chat.id==group_id
            and (msg.message_thread_id or 0)==ns['SCHOOL_REVIEW_TOPIC_ID']
            and msg.document and is_pdf_document(msg.document)):
            actor=update.effective_user.id if update.effective_user else None
            anonymous_admin=bool(msg.sender_chat and msg.sender_chat.id==group_id)
            if (not actor or not ns['is_admin'](actor)) and anonymous_admin:
                actor=ns.get('OWNER_CHAT_ID') or group_id
            if not actor or (not ns['is_admin'](actor) and not anonymous_admin):return
            try:
                result=await create_review(msg.document,msg.caption,actor,
                    update.effective_chat.id,msg.message_id,msg.date)
                r=result['review']
                status='📄 تم ربط PDF بامتحان تلقائي' if result['status']=='created' else 'ℹ️ هذا الملف مربوط بامتحان مسبقاً'
                await msg.reply_text(f"{status}\n📚 {r['chapter_label']}\n"
                    f"▶️ البداية: {stamp(r['exam_opens_at'])}\n"
                    f"⏰ النهاية: {stamp(r['exam_closes_at'])}\n"
                    '⚙️ للتحكم بالموعد والتمديد افتح إدارة مراجعة المدرسة في خاص البوت.')
            except ValueError as exc:
                await msg.reply_text(f'⚠️ {exc}')
            except Exception:
                logger.exception('PDF school review publication failed')
                await msg.reply_text('⚠️ تعذر ربط PDF. لم يتم تأكيد النشر؛ تحقق من قاعدة البيانات وحاول مجدداً.')
            return
        return await old_capture(update,context)
    ns['capture_group_task']=capture_group_task

    # Public for regression tests and future integrations.
    ns['_school_pdf_create']=create_review
    ns['_school_pdf_control']=control
    ns['_school_pdf_replace']=replace_pdf
    ns['_school_pdf_migrate']=migrate
