"""Biology v6: teacher-controlled scope, safe early prep rollback and topic-only reviews.

Non-destructive migrations. Existing task, submission, grade and XP history retained.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import time

ZONE = ZoneInfo('Asia/Baghdad')
MODULES = ('lectures','exams','homework','school_review')
LABELS = {'lectures':'🎬 المحاضرات والتحاضير','exams':'📝 الامتحانات','homework':'📚 الواجبات','school_review':'🏫 مراجعة المدرسة'}
LOG = logging.getLogger(__name__)


def parse_topic(value):
    try: n=int(str(value).strip())
    except (ValueError,TypeError): raise ValueError('أرسل رقم Topic ID فقط.')
    if not 1<=n<=2147483647: raise ValueError('معرّف الموضوع غير صالح.')
    return n


def module_for_callback(data):
    """Only assessment routes; normal learning and admin routes remain available."""
    if data.startswith(('v54_school_', 'pdfstudent|','school_review|','schoolreview|')) or data in ('v54_school_review','v54_school_exams'):
        return 'school_review'
    if data.startswith(('tasks|homework','homework|')):return 'homework'
    if data.startswith(('tasks|exam','exams_menu','examopen|','v52_exam_','v55_exam_','v42_bank_','examarchive|','past_exams')) or data in ('exam_menu','v52_exam_bank','v42_exam_bank','past_exams'):
        return 'exams'
    return None


def install(ns,db):
    B,K=ns['InlineKeyboardButton'],ns['InlineKeyboardMarkup']
    is_admin=ns['is_admin']
    group=int(ns.get('BIOLOGY_GROUP_ID') or 0)
    original_topic=int(ns.get('SCHOOL_REVIEW_TOPIC_ID') or 0)
    def query(sql,args=(),one=False,commit=False):
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute(sql,args)
            result=(cur.fetchone() if one else cur.fetchall()) if cur.description else None
            if commit:conn.commit()
            return result
    async def sql(sql_text,args=(),one=False,commit=False):
        return await db.run(lambda:query(sql_text,args,one,commit))

    def migrate():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS biology_student_modules (
                user_id BIGINT PRIMARY KEY REFERENCES biology_students(user_id) ON DELETE CASCADE,
                exams BOOLEAN NOT NULL DEFAULT TRUE,
                homework BOOLEAN NOT NULL DEFAULT TRUE,
                school_review BOOLEAN NOT NULL DEFAULT TRUE,
                listener BOOLEAN NOT NULL DEFAULT FALSE,
                changed_by BIGINT,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS biology_manual_exam_links (
                    prep_no INTEGER NOT NULL REFERENCES biology_preparations(prep_no) ON DELETE CASCADE,
                    task_id INTEGER NOT NULL REFERENCES biology_tasks(id) ON DELETE CASCADE,
                    linked_by BIGINT NOT NULL,linked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(prep_no,task_id));
                CREATE TABLE IF NOT EXISTS biology_early_unlock_schedule_snapshots (
                    unlock_id BIGINT NOT NULL REFERENCES biology_early_preparation_unlocks(id) ON DELETE CASCADE,
                    personal_prep_id BIGINT NOT NULL REFERENCES biology_personal_preparations(id) ON DELETE CASCADE,
                    target_date DATE NOT NULL,PRIMARY KEY(unlock_id,personal_prep_id));
                CREATE TABLE IF NOT EXISTS biology_start_leads (
                user_id BIGINT PRIMARY KEY, username TEXT, display_name TEXT NOT NULL DEFAULT '',
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                registered_at TIMESTAMPTZ);
                ALTER TABLE biology_school_reviews ADD COLUMN IF NOT EXISTS source_thread_id BIGINT;
                -- Retire the old weekly workflow without deleting grades, submissions,
                -- warnings or the historical review records.
                UPDATE biology_school_reviews SET active=FALSE WHERE source_type<>'pdf';
                UPDATE biology_tasks SET retired_obligation=TRUE,closed=TRUE
                    WHERE school_review_id IN (SELECT id FROM biology_school_reviews WHERE source_type<>'pdf');
                CREATE INDEX IF NOT EXISTS biology_school_topic_idx
                    ON biology_school_reviews(source_type,source_thread_id,active);
            ''')
            cur.execute("INSERT INTO biology_settings(key,value) VALUES('school_topic_id',%s) ON CONFLICT(key) DO NOTHING;",(str(original_topic),))
            conn.commit()
    old_post=ns['post_init']
    async def post_init(app):
        await old_post(app)
        await db.run(migrate)
        topic=await sql("SELECT value FROM biology_settings WHERE key='school_topic_id';",one=True)
        ns['SCHOOL_REVIEW_TOPIC_ID']=int((topic or {}).get('value') or 0)
        # Mark inferred pre-enrollment royal reviews as finished, not due.
        await db.sync_prior_learning()
    ns['post_init']=post_init

    async def flags(uid):
        row=await sql('SELECT * FROM biology_student_modules WHERE user_id=%s;',(int(uid),),True)
        return row or dict(lectures=True,exams=True,homework=True,school_review=True,listener=False)
    db.bio6_flags=flags
    async def allowed(uid,module):
        if module not in MODULES:return True
        return bool((await flags(uid)).get(module,True))
    async def listener(uid):return bool((await flags(uid)).get('listener',False))
    db.bio6_listener=listener
    async def set_flag(uid,field,actor):
        if field not in (*MODULES,'listener'):raise ValueError('صلاحية غير معروفة')
        def work():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute('SELECT user_id FROM biology_students WHERE user_id=%s FOR UPDATE;',(uid,))
                if not cur.fetchone():raise ValueError('الطالب غير مسجل. افتح طلب /start أولاً.')
                cur.execute('INSERT INTO biology_student_modules(user_id) VALUES(%s) ON CONFLICT DO NOTHING;',(uid,))
                cur.execute(f'''UPDATE biology_student_modules SET {field}=NOT {field},changed_by=%s,
                    updated_at=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;''',(actor,uid))
                result=cur.fetchone()
                if field=='school_review':
                    cur.execute('''INSERT INTO biology_school_review_students(user_id,active,enabled_by,enabled_at)
                        VALUES(%s,%s,%s,CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET
                        active=EXCLUDED.active,enabled_by=EXCLUDED.enabled_by,enabled_at=CURRENT_TIMESTAMP;''',
                        (uid,bool(result[field]),actor))
                cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',
                    (actor,'student_module_change',f'user={uid};field={field};enabled={result[field]}'))
                conn.commit();return result
        return await db.run(work)

    async def record_lead(user):
        return await sql('''INSERT INTO biology_start_leads(user_id,username,display_name)
            VALUES(%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET
            username=EXCLUDED.username,display_name=EXCLUDED.display_name,
            last_seen_at=CURRENT_TIMESTAMP RETURNING *;''',
            (user.id,user.username or '',user.full_name or ''),True,True)
    async def leads(page=0):
        return await sql('''SELECT l.*,s.approved,s.full_name FROM biology_start_leads l
            LEFT JOIN biology_students s ON s.user_id=l.user_id
            WHERE l.registered_at IS NULL AND (s.user_id IS NULL OR s.approved=FALSE)
            ORDER BY l.last_seen_at DESC LIMIT 11 OFFSET %s;''',(max(0,page)*10,))

    # Inferred pre-enrollment lectures must never acquire new review plans.
    old_plan=db.v41_ensure_review_plan
    async def review_plan(uid,ch,lec):
        r=await sql('''SELECT completion_method FROM biology_lecture_progress
            WHERE user_id=%s AND chapter=%s AND lecture=%s;''',(uid,ch,lec),True)
        if r and r.get('completion_method')=='prior_learning':return False
        return await old_plan(uid,ch,lec)
    db.v41_ensure_review_plan=review_plan;ns['v41_ensure_review_plan']=review_plan

    # Detect linked course exams by exact prep OR its lectures. A manual course exam
    # with no mapping is ambiguous: suppress a false alarm if posted in the window.
    async def due_missing(limit=20):
        return await sql('''SELECT p.* FROM biology_preparations p
            WHERE p.published=TRUE AND p.published_at IS NOT NULL
              AND p.published_at BETWEEN CURRENT_TIMESTAMP-INTERVAL '7 days'
                  AND CURRENT_TIMESTAMP-INTERVAL '12 hours'
              AND NOT EXISTS (
                SELECT 1 FROM biology_linked_exam_definitions d
                WHERE d.target_scope='course' AND d.deleted_at IS NULL
                AND (EXISTS (SELECT 1 FROM biology_linked_exam_preparations lp
                    WHERE lp.definition_id=d.id AND lp.chapter=p.chapter
                      AND lp.prep_no=p.chapter_prep_no)
                  OR EXISTS (SELECT 1 FROM biology_linked_exam_lectures ll
                    WHERE ll.definition_id=d.id AND ll.chapter=p.chapter
                      AND ll.lecture=ANY(STRING_TO_ARRAY(p.lectures,',')::INTEGER[]))))
              AND NOT EXISTS (SELECT 1 FROM biology_manual_exam_links ml
                  JOIN biology_tasks linked ON linked.id=ml.task_id
                  WHERE ml.prep_no=p.prep_no AND linked.kind='exam'
                    AND linked.retired_obligation=FALSE)
              AND NOT EXISTS (
                SELECT 1 FROM biology_tasks t
                WHERE t.kind='exam' AND t.target_scope IN ('course','all')
                  AND t.retired_obligation=FALSE
                  AND COALESCE(t.published_at,t.exam_available_at,t.created_at)
                      BETWEEN p.published_at-INTERVAL '3 hours'
                      AND p.published_at+INTERVAL '36 hours')
              AND NOT EXISTS(SELECT 1 FROM biology_missing_exam_reminders m
                  WHERE m.prep_no=p.prep_no)
            ORDER BY p.published_at LIMIT %s;''',(max(1,min(100,int(limit))),))
    db.v42_due_missing_course_exams=due_missing;ns['v42_due_missing_course_exams']=due_missing

    # Student scoping at assignment, reminders, submission and penalty boundaries.
    old_effective=db.effective_task_deadline
    async def effective(tid,uid):
        row=await old_effective(tid,uid)
        if not row:return row
        task=await sql('SELECT kind,school_review_id FROM biology_tasks WHERE id=%s;',(tid,),True)
        module='school_review' if task and task.get('school_review_id') else (task or {}).get('kind')
        if module=='exam':module='exams'
        if module and not await allowed(uid,module):return {**row,'assigned':False,'closed':True}
        return row
    db.effective_task_deadline=effective;ns['effective_task_deadline']=effective
    old_assigned=db.assigned_students
    async def assigned(tid):
        task=await sql('SELECT kind,school_review_id FROM biology_tasks WHERE id=%s;',(tid,),True)
        module='school_review' if task and task.get('school_review_id') else (task or {}).get('kind')
        if module=='exam':module='exams'
        return [r for r in await old_assigned(tid) if await allowed(r['user_id'],module)]
    db.assigned_students=assigned;ns['assigned_students']=assigned
    for name in ('open_tasks','open_exam_tasks','v28_student_exam_tasks'):
        old=getattr(db,name)
        def factory(fn,source):
            async def scoped(*a,**kw):
                uid=kw.get('user_id')
                if source=='v28_student_exam_tasks':uid=a[0] if a else uid;module='exams'
                else:
                    uid=a[1] if len(a)>1 else uid
                    module='exams' if source=='open_exam_tasks' else (a[0] if a else kw.get('kind'))
                    if module=='exam':module='exams'
                if uid is not None and module and not await allowed(uid,module):return []
                return await fn(*a,**kw)
            return scoped
        fn=factory(old,name);setattr(db,name,fn);ns[name]=fn
    old_lock=db.student_exam_lock
    async def lock(uid):
        if await listener(uid) or not await allowed(uid,'exams'):return None
        return await old_lock(uid)
    db.student_exam_lock=lock;ns['student_exam_lock']=lock
    for name in ('missing_students','students_pending_task'):
        old=getattr(db,name)
        def factory(fn):
            async def scoped(tid):
                task=await sql('SELECT kind,school_review_id FROM biology_tasks WHERE id=%s;',(tid,),True)
                module='school_review' if task and task.get('school_review_id') else (task or {}).get('kind')
                if module=='exam':module='exams'
                return [r for r in await fn(tid) if not await listener(r['user_id']) and await allowed(r['user_id'],module)]
            return scoped
        fn=factory(old);setattr(db,name,fn);ns[name]=fn
    for name in ('incomplete_preparation_students','incomplete_personal_preparation_students'):
        old=getattr(db,name)
        def factory(fn):
            async def scoped(*a,**kw):return [r for r in await fn(*a,**kw) if not await listener(r['user_id'])]
            return scoped
        fn=factory(old);setattr(db,name,fn);ns[name]=fn
    for name in ('add_warning','add_warning_once'):
        old=getattr(db,name)
        def factory(fn,once):
            async def guarded(uid,*a,**kw):
                taskid=kw.get('task_id')
                if taskid is None and len(a)>=3:taskid=a[2]
                task=await sql('SELECT kind,school_review_id FROM biology_tasks WHERE id=%s;',(taskid,),True) if taskid else None
                module='school_review' if task and task.get('school_review_id') else (task or {}).get('kind')
                if module=='exam':module='exams'
                if await listener(uid) or (module and not await allowed(uid,module)):
                    student=await db.get_student(uid);count=int((student or {}).get('warnings') or 0)
                    return {'count':count,'created':False} if once else count
                return await fn(uid,*a,**kw)
            return guarded
        fn=factory(old,name=='add_warning_once');setattr(db,name,fn);ns[name]=fn
    old_school_access=db.v54_school_review_access
    async def school_access(uid):
        if not await allowed(uid,'school_review'):return None
        return await old_school_access(uid)
    db.v54_school_review_access=school_access
    # Listening accounts may optionally open an assessment without parent approval;
    # they still do not incur mandatory exam locks or warnings.
    async def approve_listener_task(uid,task):
        if not task:return
        await db.decide_exam_access(task['id'],uid,'approved',0)
        await sql('''UPDATE biology_tasks SET exam_pending_activation=FALSE,
            exam_approval_required=FALSE WHERE id=%s AND target_scope=%s;''',
            (task['id'],f'student:{uid}'),commit=True)
    for name in ('v52_prepare_chapter_exam','v54_prepare_school_review_exam'):
        old=getattr(db,name)
        def factory(fn):
            async def opened(definition_or_review,uid):
                result=await fn(definition_or_review,uid)
                if await listener(uid) and result and result.get('status') in ('approval','pending') and result.get('task'):
                    await approve_listener_task(uid,result['task'])
                    return {**result,'status':'open','notify':False}
                return result
            return opened
        setattr(db,name,factory(old))
    old_prep_access=db.v37_preparation_access
    async def prep_access(uid,ch,lec):
        if await listener(uid) and int(ch) in ns['PLAYLISTS'] and any(int(item[0])==int(lec) for item in ns['PLAYLISTS'][int(ch)]):
            return {'allowed':True,'reason':'listener','row':None}
        return await old_prep_access(uid,ch,lec)
    db.v37_preparation_access=prep_access

    # Retire automatic legacy weekly publication; do not delete historical records.
    async def no_weekly_publication(*a,**kw):return []
    db.v54_due_school_review_publications=no_weekly_publication
    old_catalog=db.v54_school_review_catalog
    def topic_row(r):
        topic=int(ns.get('SCHOOL_REVIEW_TOPIC_ID') or 0)
        return bool(topic and r.get('source_type')=='pdf' and r.get('active')
            and r.get('source_thread_id')==topic and (r.get('source_chat_id')==group or r.get('group_message_id')))
    async def catalog(uid=None):
        return [r for r in await old_catalog(uid) if topic_row(r)]
    db.v54_school_review_catalog=catalog
    async def review_by_id(rid,uid=None):
        if uid is None:
            return await sql('''SELECT r.*,(SELECT COUNT(*)::INTEGER FROM biology_school_review_exam_media m
                WHERE m.review_id=r.id) AS media_count FROM biology_school_reviews r WHERE r.id=%s;''',
                (int(rid),),True)
        return next((r for r in await catalog(uid) if int(r['id'])==int(rid)),None)
    db.v54_school_review_by_id=review_by_id
    for name in ('v54_due_school_review_reminders','v54_due_school_review_exams'):
        old=getattr(db,name)
        def factory(fn,source_name):
            async def filtered(*a,**kw):
                rows=await fn(*a,**kw)
                result=[]
                for r in rows:
                    rid=r.get('review_id') or r.get('id')
                    if rid and topic_row((await review_by_id(rid)) or {}) and (source_name!='v54_due_school_review_exams' or not await listener(r['user_id'])):result.append(r)
                return result
            return filtered
        setattr(db,name,factory(old,name))

    # Save the personal calendar before a possible early-completion reschedule.
    old_unlock=db.v52_unlock_next_preparation
    async def unlock(uid):
        result=await old_unlock(uid)
        if result.get('status') in {'ok','existing'}:
            row=result['row']
            await sql('''INSERT INTO biology_early_unlock_schedule_snapshots
                (unlock_id,personal_prep_id,target_date)
                SELECT %s,id,target_date FROM biology_personal_preparations WHERE user_id=%s
                ON CONFLICT(unlock_id,personal_prep_id) DO NOTHING;''',
                (row['id'],uid),commit=True)
        return result
    db.v52_unlock_next_preparation=unlock
    # Returning to the previous preparation is a database rollback, not a UI redirect.
    async def latest_early(uid):
        return await sql('''SELECT * FROM biology_early_preparation_unlocks WHERE user_id=%s
            AND (completed_at IS NULL OR completed_at>=CURRENT_TIMESTAMP-INTERVAL '24 hours')
            ORDER BY unlocked_at DESC,id DESC LIMIT 1;''',(uid,),True)
    async def undo_early(uid):
        def work():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute('SELECT user_id FROM biology_students WHERE user_id=%s FOR UPDATE;',(uid,))
                if not cur.fetchone():return {'status':'missing'}
                cur.execute('''SELECT * FROM biology_early_preparation_unlocks
                    WHERE user_id=%s AND (completed_at IS NULL OR completed_at>=CURRENT_TIMESTAMP-INTERVAL '24 hours')
                    ORDER BY unlocked_at DESC,id DESC LIMIT 1 FOR UPDATE;''',(uid,))
                r=cur.fetchone()
                if not r:return {'status':'none'}
                if r.get('study_track')=='chapter':
                    cur.execute('SELECT 1 FROM biology_early_unlock_schedule_snapshots WHERE unlock_id=%s LIMIT 1;',
                        (r['id'],))
                    if not cur.fetchone():return {'status':'legacy'}
                # If a later course preparation is already completed, rolling this one back
                # would invalidate the timeline. Require admin assistance in that case.
                nums=[int(v) for v in r['lectures'].split(',') if v.strip().isdigit()]
                cur.execute('''SELECT chapter,lecture FROM biology_lecture_progress
                    WHERE user_id=%s AND completed_at>%s AND
                    (chapter>%s OR (chapter=%s AND lecture>ALL(%s::INTEGER[])))
                    LIMIT 1;''',(uid,r['unlocked_at'],r['chapter'],r['chapter'],nums or [0]))
                if cur.fetchone():return {'status':'later'}
                cur.execute('''SELECT 1 FROM biology_tasks t
                    JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=%s
                    JOIN biology_linked_exam_lectures l ON l.definition_id=t.exam_definition_id
                    WHERE t.kind='exam' AND sub.submitted_at>= %s
                      AND l.chapter=%s AND l.lecture=ANY(%s) LIMIT 1;''',
                    (uid,r['unlocked_at'],r['chapter'],nums))
                if cur.fetchone():return {'status':'exam_submitted'}
                # Retire newly generated, unsubmitted individual exam obligations
                # for the cancelled early lectures, preserving other students' tasks.
                cur.execute('''UPDATE biology_tasks t SET retired_obligation=TRUE,closed=TRUE
                    WHERE t.target_scope=%s AND t.created_at>=%s
                      AND t.kind='exam' AND EXISTS(
                        SELECT 1 FROM biology_linked_exam_lectures l
                        WHERE l.definition_id=t.exam_definition_id
                          AND l.chapter=%s AND l.lecture=ANY(%s))
                      AND NOT EXISTS(SELECT 1 FROM biology_submissions sub
                        WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL);''',
                    (f'student:{uid}',r['unlocked_at'],r['chapter'],nums,uid))
                cur.execute('''SELECT lecture FROM biology_lecture_progress
                    WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s)
                    AND opened_at>= %s AND completion_method<>'prior_learning';''',
                    (uid,r['chapter'],nums,r['unlocked_at']))
                fresh=[int(v['lecture']) for v in cur.fetchall()]
                if fresh:
                    cur.execute('''DELETE FROM biology_lecture_reviews WHERE user_id=%s
                        AND chapter=%s AND lecture=ANY(%s);''',(uid,r['chapter'],fresh))
                    cur.execute('''DELETE FROM biology_lecture_progress WHERE user_id=%s
                        AND chapter=%s AND lecture=ANY(%s) AND opened_at>=%s
                        AND completion_method<>'prior_learning';''',(uid,r['chapter'],fresh,r['unlocked_at']))
                cur.execute('''UPDATE biology_personal_preparations p SET target_date=p.target_date+10000
                    FROM biology_early_unlock_schedule_snapshots s
                    WHERE s.unlock_id=%s AND p.id=s.personal_prep_id;''',(r['id'],))
                cur.execute('''UPDATE biology_personal_preparations p SET target_date=s.target_date
                    FROM biology_early_unlock_schedule_snapshots s
                    WHERE s.unlock_id=%s AND p.id=s.personal_prep_id;''',(r['id'],))
                # Correct the existing idempotent XP event, preserving the wallet scope.
                db._set_xp_event(cur,uid,0,'التراجع عن التحضير المبكر',f"early_prep:{r['id']}:{uid}")
                # Do not re-award XP for cancelled early unlock.
                cur.execute('DELETE FROM biology_early_preparation_unlocks WHERE id=%s;',(r['id'],))
                cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',
                    (uid,'early_preparation_undo',f"id={r['id']};lectures={','.join(map(str,fresh))}"))
                conn.commit();return {'status':'ok','lectures':fresh}
        return await db.run(work)
    db.bio6_undo_early=undo_early

    old_prep_menu=ns['v51_today_preparation']
    async def today(q):
        class PrepQuery:
            def __init__(self,base):self.base=base
            def __getattr__(self,key):return getattr(self.base,key)
            async def edit_message_text(self,text,*args,**kw):
                r=await latest_early(q.from_user.id)
                if r:
                    markup=kw.get('reply_markup');rows=[list(row) for row in markup.inline_keyboard] if markup else []
                    rows.insert(max(0,len(rows)-1),[B('↩️ العودة للتحضير السابق',callback_data='bio6|early|ask',style='danger')])
                    kw['reply_markup']=K(rows)
                return await self.base.edit_message_text(text,*args,**kw)
        return await old_prep_menu(PrepQuery(q))
    ns['v51_today_preparation']=today

    old_main_menu=ns['main_menu']
    scope_cache={}
    def main_menu(admin=False):
        original=old_main_menu(admin)
        rows=[list(r) for r in original.inline_keyboard]
        if admin:
            rows.insert(0,[B('⚙️ مركز الإدارة المطوّر',callback_data='bio6|hub',style='success')])
        # The synchronous menu uses a short-lived, refreshed per-student cache;
        # callback and database guards remain authoritative if the cache is cold.
        else:
            rows.insert(0,[B('🎯 أقسامي المفعّلة',callback_data='bio6|my_modules')])
        return K(rows)
    ns['main_menu']=main_menu

    async def hub(q):
        rows=[
            [B('👥 الطلاب والصلاحيات',callback_data='bio6|choose'),B('🆕 طلبات Start',callback_data='bio6|leads|0')],
            [B('📝 إدارة الامتحانات',callback_data='v42_admin_exams'),B('📚 إدارة الواجبات',callback_data='hw|list|0')],
            [B('🔗 مطابقة الامتحان مع التحضير',callback_data='bio6|linkpreps|0')],
            [B('🏫 مراجعة المدرسة',callback_data='v54_school_admin'),B('📄 امتحانات PDF',callback_data='pdf|list|0')],
            [B('📌 تحديد توبك المراجعة',callback_data='bio6|topic')],
            [B('🎬 إدارة التحاضير',callback_data='prep_schedule'),B('🗓 جدول الدورة',callback_data='ctl_calendar')],
            [B('📖 الملازم والملخصات',callback_data='study_resources')],
            [B('🏠 كل أزرار الإدارة',callback_data='menu')]]
        await q.edit_message_text('⚙️ مركز الإدارة — الأحياء\n━━━━━━━━━━━━━━━━━━\nكل الأزرار القديمة باقية بالقائمة الأصلية.\nاختر القسم المطلوب:',reply_markup=K(rows))
    async def link_preps(q,page):
        rows=await sql('''SELECT p.prep_no,p.chapter,p.chapter_prep_no,p.lectures,
            EXISTS(SELECT 1 FROM biology_manual_exam_links l WHERE l.prep_no=p.prep_no) AS linked
            FROM biology_preparations p WHERE p.published=TRUE
            ORDER BY p.target_date DESC,p.prep_no DESC LIMIT 11 OFFSET %s;''',(max(0,page)*10,))
        kb=[[B(f"{'🔗' if r['linked'] else '📘'} ف{r['chapter']} ت{r['chapter_prep_no']} | {r['lectures']}",callback_data=f"bio6|linkprep|{r['prep_no']}")]
            for r in rows[:10]]
        nav=[]
        if page:nav.append(B('◀️',callback_data=f'bio6|linkpreps|{page-1}'))
        if len(rows)>10:nav.append(B('▶️',callback_data=f'bio6|linkpreps|{page+1}'))
        if nav:kb.append(nav)
        kb.append([B('◀️ الإدارة',callback_data='bio6|hub')])
        await q.edit_message_text('🔗 اختر التحضير الذي تريد ربطه بامتحان منشور، حتى لا يصلك تنبيه كاذب.',reply_markup=K(kb))
    async def link_exam_picker(q,prep):
        rows=await sql('''SELECT id,title,published_at FROM biology_tasks
            WHERE kind='exam' AND target_scope IN ('course','all')
              AND retired_obligation=FALSE ORDER BY id DESC LIMIT 20;''')
        kb=[[B(f"📝 {r['title'][:44]}",callback_data=f"bio6|linksave|{prep}|{r['id']}")] for r in rows]
        kb.append([B('◀️ التحاضير',callback_data='bio6|linkpreps|0')])
        await q.edit_message_text(f'🔗 التحضير {prep}\nاختر الامتحان المنشور فعلياً المرتبط بهذا التحضير:',reply_markup=K(kb))
    async def student_panel(q,uid):
        student=await db.get_student(uid)
        if not student:
            await q.edit_message_text('هذا الطالب غير مسجل. افتح طلبات Start أولاً.',reply_markup=K([[B('🆕 الطلبات',callback_data='bio6|leads|0')],[B('◀️ الإدارة',callback_data='bio6|hub')]]));return
        f=await flags(uid)
        rows=[[B(('🟢 ' if f.get(field,True) else '🔴 ')+label,
                 callback_data=f'bio6|toggle|{uid}|{field}')]
              for field,label in LABELS.items()]
        rows += [[B(('🎧 مستمع بلا عقوبات' if f.get('listener') else '⚠️ طالب ملتزم بالعقوبات'),callback_data=f'bio6|toggle|{uid}|listener')],
                 [B('👁 إدارة ملف الطالب',callback_data=f'admin_student|{uid}')],
                 [B('◀️ الإدارة',callback_data='bio6|hub')]]
        await q.edit_message_text(f"👤 {student.get('full_name') or uid}\n🆔 {uid}\n🔑 كود ولي الأمر: {student.get('parent_link_code') or 'غير متوفر'}\n━━━━━━━━━━━━━━━━━━\n🟢 مفعّل / 🔴 معطّل\nالطالب يُحاسب فقط على الأقسام المفعّلة.\nالمستمع يمكنه الاطلاع دون إنذارات أو طرد آلي.",reply_markup=K(rows))
    async def lead_panel(q,uid):
        r=await sql('SELECT * FROM biology_start_leads WHERE user_id=%s;',(uid,),True)
        if not r:await q.answer('الطلب غير موجود.',show_alert=True);return
        code, active = await db.v7_ensure_parent_code(uid)
        status = 'جاهز' if active else 'يصبح قابلاً للربط بعد تسجيل الطالب'
        await q.edit_message_text(f"🆕 طلب بدء البوت\n👤 {r['display_name']}\n🆔 {uid}\n@{r['username'] or '-'}\n🔑 كود ولي الأمر: {code}\n{status}\nيمكن تسجيل الطالب وتفعيله مباشرة، ويختار مساره عند فتح البوت.",reply_markup=K([
            [B('✅ تسجيل وتفعيل',callback_data=f'bio6|activate|{uid}',style='success')],
            [B('◀️ طلبات Start',callback_data='bio6|leads|0')]]))
    async def lead_list(q,page):
        rows=await leads(page);buttons=[[B(f"👤 {r['display_name'][:28]} | {r['user_id']}",callback_data=f"bio6|lead|{r['user_id']}")] for r in rows[:10]]
        nav=[]
        if page:nav.append(B('◀️',callback_data=f'bio6|leads|{page-1}'))
        if len(rows)>10:nav.append(B('▶️',callback_data=f'bio6|leads|{page+1}'))
        if nav:buttons.append(nav)
        buttons.append([B('◀️ الإدارة',callback_data='bio6|hub')])
        await q.edit_message_text('🆕 الطلاب الذين ضغطوا Start ولم يكملوا التسجيل\nيمكنك تفعيلهم مباشرة من هنا.' if rows else 'لا توجد طلبات Start غير مسجلة.',reply_markup=K(buttons))
    async def activate(uid,actor,bot):
        r=await sql('SELECT * FROM biology_start_leads WHERE user_id=%s;',(uid,),True)
        if not r:return 'لم يعثر على الطلب.'
        name=(r.get('display_name') or '').strip()[:100] or f'طالب {uid}'
        student=await db.get_student(uid)
        if not student:
            student=await db.register_student(uid,r.get('username'),name,'تُحدّد لاحقاً',0)
        if student.get('status')=='parent_account':return 'الحساب مسجل كولي أمر؛ لا يمكن تحويله تلقائياً.'
        from admin_student_access import change_access
        await change_access(db,uid,'activate',actor)
        await sql('UPDATE biology_start_leads SET registered_at=CURRENT_TIMESTAMP WHERE user_id=%s;',(uid,),commit=True)
        try:await bot.send_message(uid,'✅ تم تفعيل حسابك من الإدارة. أرسل /start لاختيار مسار الدراسة، وأكمل معلوماتك مع الأستاذ.')
        except ns['TelegramError']:LOG.warning('Could not notify activated lead %s',uid)
        return '✅ تم إنشاء الحساب وتفعيله. بقي اختيار المسار واستكمال المعلومات.'

    old_start=ns['start']
    async def start(update,context):
        user=update.effective_user
        if user and not is_admin(user.id):
            scope_cache[user.id]=await flags(user.id)
            # Only student accounts are recorded as enrollment leads. Parents
            # must not appear as new students or trigger private admin alerts.
            if not await db.students_by_parent(user.id, False):
                student=await db.get_student(user.id)
                if not student or not student.get('approved'):
                    await record_lead(user)
        return await old_start(update,context)
    ns['start']=start

    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query;data=q.data or '';uid=q.from_user.id
        if not data.startswith('bio6|'):
            if not is_admin(uid):
                mod=module_for_callback(data)
                if data.startswith(('task|','submit|','retrysubmission|','extend|')):
                    try:tid=int(data.split('|')[1])
                    except (IndexError,ValueError):tid=None
                    if tid:
                        task=await sql('SELECT kind,school_review_id FROM biology_tasks WHERE id=%s;',(tid,),True)
                        mod='school_review' if task and task.get('school_review_id') else (task or {}).get('kind')
                        if mod=='exam':mod='exams'
                if mod and not await allowed(uid,mod):
                    await q.answer('🔒 هذا القسم غير مفعّل لحسابك. تواصل مع الأستاذ.',show_alert=True);return
            return await old_button(update,context)
        parts=data.split('|');action=parts[1]
        if action=='early':
            if is_admin(uid):await q.answer('للطلاب فقط.',show_alert=True);return
            sub=parts[2] if len(parts)>2 else ''
            if sub=='ask':
                r=await latest_early(uid)
                if not r:await q.answer('لا يوجد تحضير مبكر للتراجع عنه.',show_alert=True);return
                await q.answer();await q.edit_message_text('↩️ الرجوع للتحضير السابق\nسيتم إلغاء التحضير المبكر الأخير، وسحب مكافأة 30 XP إن كانت قد صُرفت، وإلغاء إنجاز المحاضرات الجديدة التي بدأت بعد فتحه.\n⚠️ هذا الإجراء لا يمكن التراجع عنه.',reply_markup=K([
                    [B('✅ تأكيد الرجوع وخصم XP',callback_data='bio6|early|undo',style='danger')],
                    [B('❌ لا، أكمل التحضير',callback_data='today_prep')]]));return
            if sub=='undo':
                result=await undo_early(uid);await q.answer()
                if result['status']=='ok':await ns['v51_today_preparation'](q)
                else:await q.edit_message_text('⚠️ لا يمكن الرجوع لأن هناك تحاضير أحدث مكتملة، أو لا يوجد تحضير مبكر. تواصل مع الإدارة.',reply_markup=K([[B('🎬 تحاضيري',callback_data='today_prep')]]))
                return
            if sub=='confirm':
                await ns['v52_unlock_next'](q);return
            await q.answer('زر غير صالح.',show_alert=True);return
        if action=='my_modules':
            if is_admin(uid):await q.answer('للطلاب فقط.',show_alert=True);return
            f=await flags(uid)
            lines=['🎯 الأقسام المفعّلة لحسابك','━━━━━━━━━━━━━━━━━━']
            lines.extend(('🟢 ' if f.get(field,True) else '🔴 ')+label for field,label in LABELS.items())
            lines.append('🎧 مستمع دون عقوبات' if f.get('listener') else '⚠️ يخضع للأقسام المفعّلة فقط')
            await q.answer();await q.edit_message_text('\n'.join(lines),reply_markup=K([[B('🏠 القائمة',callback_data='menu')]]));return
        if not is_admin(uid):await q.answer('للإدارة فقط.',show_alert=True);return
        if action=='hub':await q.answer();await hub(q);return
        if action=='choose':
            context.user_data['bio6_input']={'kind':'uid','at':time.monotonic()}
            await q.answer();await q.edit_message_text('👥 أرسل رقم ID الطالب في خاص البوت لفتح صلاحياته.',reply_markup=K([[B('◀️ الإدارة',callback_data='bio6|hub')]]));return
        if action=='linkpreps':await q.answer();await link_preps(q,int(parts[2]));return
        if action=='linkprep':await q.answer();await link_exam_picker(q,int(parts[2]));return
        if action=='linksave':
            prep,tid=int(parts[2]),int(parts[3])
            await sql('''INSERT INTO biology_manual_exam_links(prep_no,task_id,linked_by)
                SELECT %s,t.id,%s FROM biology_tasks t WHERE t.id=%s AND t.kind='exam'
                  AND t.target_scope IN ('course','all')
                ON CONFLICT(prep_no,task_id) DO NOTHING;''',(prep,uid,tid),commit=True)
            await q.answer('تم الربط.');await link_preps(q,0);return
        if action=='student':await q.answer();await student_panel(q,int(parts[2]));return
        if action=='toggle':
            scope_cache[int(parts[2])]=await set_flag(int(parts[2]),parts[3],uid)
            await q.answer('تم حفظ الصلاحية.');await student_panel(q,int(parts[2]));return
        if action=='leads':await q.answer();await lead_list(q,max(0,int(parts[2])));return
        if action=='lead':await q.answer();await lead_panel(q,int(parts[2]));return
        if action=='activate':
            msg=await activate(int(parts[2]),uid,context.bot)
            await q.answer();await q.edit_message_text(msg,reply_markup=K([[B('⚙️ صلاحيات الطالب',callback_data=f'bio6|student|{parts[2]}')],[B('◀️ الإدارة',callback_data='bio6|hub')]]));return
        if action=='topic':
            context.user_data['bio6_input']={'kind':'topic','at':time.monotonic()}
            await q.answer();await q.edit_message_text(f"📌 توبك مراجعة المدرسة\nالحالي: {ns.get('SCHOOL_REVIEW_TOPIC_ID') or 'غير محدد'}\nأرسل رقم Topic ID من مجموعة الأحياء.\nالملفات المنشورة في غير هذا الموضوع لن تُربط بمراجعة المدرسة.",reply_markup=K([[B('◀️ الإدارة',callback_data='bio6|hub')]]));return
        await q.answer('زر غير معروف.',show_alert=True)
    ns['button_handler']=button

    # Confirm before opening next prep, rather than immediate unlock on accidental tap.
    previous_button=ns['button_handler']
    async def confirm_early(update,context):
        q=update.callback_query
        if q.data=='v52_unlock_next' and not is_admin(q.from_user.id):
            await q.answer();await q.edit_message_text('⚡ فتح التحضير القادم مبكراً\nهل أنت متأكد؟\nيمكنك الرجوع لاحقاً، لكن تُسحب مكافأة XP المبكرة عند الرجوع.',reply_markup=K([
                [B('✅ تأكيد فتح التحضير',callback_data='bio6|early|confirm',style='success')],
                [B('↩️ إلغاء والعودة',callback_data='today_prep')]]));return
        return await previous_button(update,context)
    ns['button_handler']=confirm_early

    old_private=ns['private_messages']
    async def private(update,context):
        state=context.user_data.get('bio6_input')
        if not state or not is_admin(update.effective_user.id):return await old_private(update,context)
        if time.monotonic()-state['at']>600:
            context.user_data.pop('bio6_input',None)
            await update.effective_message.reply_text('انتهت مهلة الإدخال. افتح مركز الإدارة مجدداً.');return
        txt=(update.effective_message.text or '').strip()
        try:
            if state['kind']=='uid':
                uid=int(txt)
                if uid<=0:raise ValueError('معرف غير صالح')
                context.user_data.pop('bio6_input',None)
                await update.effective_message.reply_text('⚙️ افتح صلاحيات الطالب:',reply_markup=K([[B(f'👤 الطالب {uid}',callback_data=f'bio6|student|{uid}')]]))
            else:
                topic=parse_topic(txt)
                await sql("INSERT INTO biology_settings(key,value) VALUES('school_topic_id',%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;",(str(topic),),commit=True)
                ns['SCHOOL_REVIEW_TOPIC_ID']=topic
                context.user_data.pop('bio6_input',None)
                await update.effective_message.reply_text(f'✅ تم ربط مراجعة المدرسة بموضوع {topic}. المنشورات القديمة محفوظة لكنها لا تظهر إذا كانت من موضوع آخر.')
        except ValueError as exc:await update.effective_message.reply_text(f'⚠️ {exc}')
    ns['private_messages']=private

    # Existing /start leads can also be registered via the admin UI without forcing
    # a student to fill in all registration fields first.
    ns['_bio6_migrate']=migrate
    ns['_bio6_undo_early']=undo_early
    ns['_bio6_set_flag']=set_flag
