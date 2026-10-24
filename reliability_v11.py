"""V11: atomic assessment policy and small UI extensions.

Installed last. Migrations preserve existing students, grades and submitted files.
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)
ZONE = ZoneInfo('Asia/Baghdad')
COURSE_DAYS = [0, 1, 6]  # Monday, Tuesday, Sunday.


def parent_exempt(student):
    return bool(student and student.get('approved') and student.get('admin_manual_active')
                and not student.get('admin_blocked') and not student.get('reset_pending')
                and not student.get('parent_chat_id'))


def retry_denial(row, teacher=False):
    if not row or not row.get('submitted_at'): return 'missing'
    if row.get('grade') is not None: return 'graded'
    if row.get('rejected'): return 'rejected'
    if row.get('revision_open'): return 'open'
    if not teacher and not row.get('teacher_change_pending') and row.get('model_answer_sent_at'): return 'model_answer_locked'
    if not teacher and not row.get('teacher_change_pending') and row.get('retry_count', 0) >= 1: return 'limit'
    return None


def cohort_key(task):
    if task.get('school_review_id'): return f"school:{task['school_review_id']}"
    if task.get('exam_definition_id'): return f"exam:{task['exam_definition_id']}"
    return f"task:{task['id']}"


def install(ns, db):
    B, K = ns['InlineKeyboardButton'], ns['InlineKeyboardMarkup']
    admin = ns['is_admin']

    async def sql(statement, args=(), one=False, commit=False):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(statement, args)
                result = (cur.fetchone() if one else cur.fetchall()) if cur.description else None
                if commit: conn.commit()
                return result
        return await db.run(op)

    def migrate():
        from assessment_schema import migrate as migrate_schema
        migrate_schema(db)

    old_init = ns['init_db']
    def init_db():
        old_init()
        migrate()
    ns['init_db'] = db.init_db = init_db

    # A single persistent schedule migration; later teacher edits always win.
    async def migrate_calendar():
        from course_schedule_v12 import migrate_future
        days=await migrate_future(db)
        if days:ns['_ADMIN_COURSE_DAYS']=days

    old_post = ns['post_init']
    async def post_init(app):
        await old_post(app)
        await migrate_calendar()
        ns['_V11_COURSE_NAME']=await db.setting_value('v11_course_name') or 'الدورة الحالية'
        app.job_queue.run_repeating(champion_recovery,60,first=40,name='v11_champion_recovery')
        if ns.get('recover_activation_requests'):
            app.job_queue.run_repeating(ns['recover_activation_requests'],300,first=50,name='activation_request_recovery')
    ns['post_init'] = post_init

    async def open_exempt(task_id, uid):
        if not parent_exempt(await db.get_student(uid)): return False
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute('''SELECT t.* FROM biology_tasks t JOIN biology_task_students r ON r.task_id=t.id
                    WHERE t.id=%s AND r.user_id=%s FOR UPDATE OF t;''', (task_id,uid))
                task = cur.fetchone()
                if not task: return False
                cur.execute('''INSERT INTO biology_exam_access(task_id,user_id,status,approved_by)
                    VALUES(%s,%s,'approved',0) ON CONFLICT(task_id,user_id) DO UPDATE SET status='approved',approved_by=0;''', (task_id,uid))
                # Only a personal task's global flags may be changed.
                if task.get('target_scope') == f'student:{uid}':
                    cur.execute('UPDATE biology_tasks SET exam_pending_activation=FALSE,exam_approval_required=FALSE WHERE id=%s;', (task_id,))
                conn.commit(); return True
        return await db.run(op)
    db.v11_open_exempt = open_exempt

    for name in ('v52_prepare_chapter_exam','v54_prepare_school_review_exam'):
        previous = getattr(db,name)
        def wrap(fn):
            async def prepared(identifier,uid):
                result = await fn(identifier,uid)
                if result and result.get('task') and result.get('status') in ('approval','pending'):
                    if await open_exempt(result['task']['id'],uid):
                        return {**result,'status':'open','notify':False,'task':await db.get_task(result['task']['id'])}
                return result
            return prepared
        setattr(db,name,wrap(previous))
    old_create = db.v29_create_or_get_exam_task
    async def create(definition_id,user_id,available_at=None,approval_required=False):
        task = await old_create(definition_id,user_id,available_at,approval_required)
        if task and not task.get('legacy_scheduled') and await open_exempt(task['id'],user_id):
            return await db.get_task(task['id'])
        return task
    db.v29_create_or_get_exam_task = create

    async def revision(task_id, uid, teacher=False, actor=0, reject=False):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute('SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;', (uid,))
                student = cur.fetchone()
                if not student or (not teacher and (not student['approved'] or student.get('admin_blocked') or student.get('reset_pending'))): return {'status':'not_allowed'}
                cur.execute('''SELECT sub.*,t.kind,GREATEST(t.deadline,COALESCE(e.extended_until,t.deadline)) AS until,
                    CURRENT_TIMESTAMP AS now FROM biology_submissions sub
                    JOIN biology_tasks t ON t.id=sub.task_id
                    LEFT JOIN biology_task_extensions e ON e.task_id=sub.task_id AND e.user_id=sub.user_id
                    WHERE sub.task_id=%s AND sub.user_id=%s FOR UPDATE OF sub,t;''', (task_id,uid))
                row = cur.fetchone()
                denial = retry_denial(row, teacher)
                if denial: return {'status':denial}
                if not teacher and row['kind']!='homework' and row['until']<=row['now']: return {'status':'expired'}
                cur.execute("SELECT 1 FROM biology_submission_delivery_outbox WHERE task_id=%s AND user_id=%s AND status='pending';", (task_id,uid))
                if cur.fetchone(): return {'status':'pending_delivery'}
                cur.execute('''INSERT INTO biology_v11_submission_history(task_id,user_id,actor_id,action,snapshot)
                    VALUES(%s,%s,%s,%s,%s::jsonb);''', (task_id,uid,actor or uid,'reject' if reject else 'request_change' if teacher else 'student_change',json.dumps(row,default=str)))
                if reject:
                    cur.execute('UPDATE biology_submissions SET rejected=TRUE WHERE task_id=%s AND user_id=%s;', (task_id,uid))
                    cur.execute('''INSERT INTO biology_warning_log(user_id,task_id,reason,issued_by,policy_key)
                        VALUES(%s,%s,'رفض الأستاذ الإجابة',%s,'rejected') ON CONFLICT DO NOTHING;''', (uid,task_id,actor))
                    cur.execute('UPDATE biology_students SET warnings=(SELECT COUNT(*) FROM biology_warning_log WHERE user_id=%s) WHERE user_id=%s;', (uid,uid))
                elif teacher:
                    cur.execute('UPDATE biology_submissions SET teacher_change_pending=TRUE WHERE task_id=%s AND user_id=%s;', (task_id,uid))
                    cur.execute('''INSERT INTO biology_task_extensions(task_id,user_id,extended_until)
                        VALUES(%s,%s,CURRENT_TIMESTAMP+INTERVAL '24 hours') ON CONFLICT(task_id,user_id)
                        DO UPDATE SET extended_until=GREATEST(biology_task_extensions.extended_until,EXCLUDED.extended_until);''', (task_id,uid))
                else:
                    cur.execute('''UPDATE biology_submissions SET revision_open=TRUE,
                        retry_count=retry_count+CASE WHEN teacher_change_pending THEN 0 ELSE 1 END
                        WHERE task_id=%s AND user_id=%s;''', (task_id,uid))
                conn.commit(); return {'status':'ok','remaining':0,'messages':[]}
        return await db.run(op)
    db.v11_revision = revision
    async def retry(task_id,uid): return await revision(task_id,uid)
    db.prepare_submission_retry = ns['prepare_submission_retry'] = retry

    # Grade writes and stale callbacks must not correct an answer being replaced.
    for name in ('set_submission_grade','save_exam_correction'):
        old = getattr(db,name)
        def guard(fn):
            async def save(task_id,uid,*args,**kwargs):
                row = await sql('SELECT * FROM biology_submissions WHERE task_id=%s AND user_id=%s;', (task_id,uid),True)
                if not row or row['revision_open'] or row['teacher_change_pending'] or row['rejected']: return None
                return await fn(task_id,uid,*args,**kwargs)
            return save
        setattr(db,name,guard(old)); ns[name]=getattr(db,name)

    async def champion(task_id):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute('SELECT * FROM biology_tasks WHERE id=%s AND kind=\'exam\';', (task_id,))
                task = cur.fetchone()
                if not task or task.get('retired_obligation'): return None
                key = cohort_key(task)
                # Serialize the whole exam, not each student's task copy.
                cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,11));', (key,))
                cur.execute('SELECT * FROM biology_v11_champions WHERE cohort=%s;', (key,))
                claim = cur.fetchone()
                field = 'school_review_id' if task.get('school_review_id') else 'exam_definition_id' if task.get('exam_definition_id') else 'id'
                cur.execute(f'''SELECT t.id,t.deadline,t.closed,MAX(e.extended_until) AS extension FROM biology_tasks t
                    LEFT JOIN biology_task_extensions e ON e.task_id=t.id
                    WHERE t.{field}=%s AND t.retired_obligation=FALSE
                    GROUP BY t.id ORDER BY t.id;''', (task[field],))
                tasks = cur.fetchall()
                cur.execute('SELECT CURRENT_TIMESTAMP AS now;'); now = cur.fetchone()['now']
                if not tasks: return None
                ids = [t['id'] for t in tasks]
                cur.execute("SELECT 1 FROM biology_submission_delivery_outbox WHERE task_id=ANY(%s) AND status='pending' LIMIT 1;", (ids,))
                if cur.fetchone(): return None
                cur.execute('''SELECT sub.*,s.full_name,s.parent_chat_id FROM biology_submissions sub
                    JOIN biology_students s ON s.user_id=sub.user_id
                    WHERE sub.task_id=ANY(%s) AND sub.submitted_at IS NOT NULL AND sub.rejected=FALSE
                    ORDER BY sub.grade DESC NULLS LAST,sub.submitted_at,sub.user_id;''', (ids,))
                entries = cur.fetchall()
                if not entries or any(s['grade'] is None or s['revision_open'] or s['teacher_change_pending'] for s in entries): return None
                signature=hashlib.sha256(json.dumps([(e['task_id'],e['user_id'],str(e['grade']),str(e['submitted_at'])) for e in entries],sort_keys=True).encode()).hexdigest()
                if claim and claim.get('sent_at') and claim.get('result_signature')==signature: return None
                cur.execute('''INSERT INTO biology_v11_champions(cohort,claimed_at) VALUES(%s,CURRENT_TIMESTAMP)
                    ON CONFLICT(cohort) DO UPDATE SET claimed_at=CURRENT_TIMESTAMP
                    WHERE
                    (biology_v11_champions.claimed_at IS NULL OR biology_v11_champions.claimed_at<CURRENT_TIMESTAMP-INTERVAL '5 minutes')
                    RETURNING cohort;''', (key,))
                if not cur.fetchone(): return None
                conn.commit(); return {**entries[0],'cohort':key,'title':task['title'],'task_ids':ids,
                    'signature':signature,'previous_message_id':(claim or {}).get('message_id')}
        return await db.run(op)
    db.v11_champion = champion

    async def announce(context,task_id):
        if not ns.get('BIOLOGY_GROUP_ID') or not ns.get('CHAMPIONS_TOPIC_ID'):
            LOG.warning('Champion destination missing; announcement retained for retry')
            return
        winner = await champion(task_id)
        if not winner: return
        try:
            from homework_v14 import champion_message
            text=champion_message(winner,ns.get('SUBJECT_NAME','الأحياء'))
            message_id=winner.get('previous_message_id')
            if message_id:
                try:
                    await context.bot.edit_message_text(text,chat_id=ns['BIOLOGY_GROUP_ID'],message_id=message_id)
                except ns['TelegramError'] as exc:
                    if 'message is not modified' not in str(exc).lower(): raise
            else:
                sent = await context.bot.send_message(ns['BIOLOGY_GROUP_ID'],text,
                    message_thread_id=ns['CHAMPIONS_TOPIC_ID'])
                message_id=sent.message_id
            await sql('''UPDATE biology_v11_champions SET sent_at=CURRENT_TIMESTAMP,claimed_at=NULL,
                message_id=%s,winner_id=%s,result_signature=%s WHERE cohort=%s;''',
                (message_id,winner['user_id'],winner['signature'],winner['cohort']),commit=True)
            await sql('UPDATE biology_tasks SET champion_announced=TRUE WHERE id=ANY(%s);', (winner['task_ids'],),commit=True)
        except ns['TelegramError']:
            await sql('UPDATE biology_v11_champions SET claimed_at=NULL WHERE cohort=%s;', (winner['cohort'],),commit=True)
            LOG.exception('Champion send failed; retry enabled')
    async def champion_recovery(context):
        for task in await db.closed_exams_pending_champion():
            try: await ns['announce_champions'](context,task['id'])
            except Exception: LOG.exception('Champion recovery failed for task %s',task['id'])
    ns['announce_champions'] = announce
    async def pending_champions(days=30):
        return await sql('''SELECT DISTINCT ON (COALESCE('school:'||t.school_review_id::TEXT,
                'exam:'||t.exam_definition_id::TEXT,'task:'||t.id::TEXT)) t.*
            FROM biology_tasks t
            LEFT JOIN biology_v11_champions c ON c.cohort=COALESCE('school:'||t.school_review_id::TEXT,
                'exam:'||t.exam_definition_id::TEXT,'task:'||t.id::TEXT)
            WHERE t.kind='exam' AND t.retired_obligation=FALSE
            AND EXISTS(SELECT 1 FROM biology_submissions sub WHERE sub.task_id=t.id AND sub.grade IS NOT NULL
                AND (c.sent_at IS NULL OR c.result_signature IS NULL OR sub.graded_at>c.sent_at
                    OR sub.submitted_at>c.sent_at))
            ORDER BY COALESCE('school:'||t.school_review_id::TEXT,
                'exam:'||t.exam_definition_id::TEXT,'task:'||t.id::TEXT),t.id;''')
    db.closed_exams_pending_champion = ns['closed_exams_pending_champion'] = pending_champions

    # Keep every distinct menu action, with the student's daily work first.
    old_menu = ns['main_menu']
    def menu(admin=False):
        rows = []; seen = set()
        original = old_menu(admin)
        for row in original.inline_keyboard:
            clean = []
            for button in row:
                key = (button.callback_data,button.url) if button.callback_data or button.url else ('other',button.text)
                if key in seen: continue
                seen.add(key); clean.append(button)
            if clean: rows.append(clean)
        if admin:
            rows.insert(0,[B('🗓 '+ns.get('_V11_COURSE_NAME','اسم وجدول الدورة'),callback_data='v11|course')])
        else:
            priority = {'daily_learning_session':0,'exams_menu':1,'tasks|homework':2}
            rows.sort(key=lambda row:min(priority.get(b.callback_data,10) for b in row))
        return K(rows)
    ns['main_menu']=menu
    old_parent = ns['parent_menu']
    def parent_menu(students):
        rows = [list(r) for r in old_parent(students).inline_keyboard]
        for student in students:
            rows.insert(0,[B(f"💬 مراسلة الأستاذ — {student['full_name']}",callback_data=f"v11|contact|{student['user_id']}")])
        return K(rows)
    ns['parent_menu']=parent_menu

    old_schedule=ns['v37_schedule_menu']
    async def schedule(q,*args,**kwargs):
        class NamedQuery:
            def __getattr__(self,name): return getattr(q,name)
            async def edit_message_text(self,text,*a,**kw):
                title=ns.get('_V11_COURSE_NAME','الدورة الحالية')
                # Existing messages use HTML; escape the teacher's custom title.
                from html import escape
                return await q.edit_message_text(escape(title)+'\n'+text,*a,**kw)
        return await old_schedule(NamedQuery(),*args,**kwargs)
    ns['v37_schedule_menu']=schedule

    old_start = ns['start']
    async def start(update,context):
        if not update.effective_user: return ns['ConversationHandler'].END
        for key in ('v11_contact','v11_enroll','v11_course_name'): context.user_data.pop(key,None)
        result = await old_start(update,context)
        uid=update.effective_user.id
        student=await db.get_student(uid)
        if not admin(uid) and (student or context.user_data.get('start_role')=='student') and not (student or {}).get('approved') and not await db.students_by_parent(uid,False):
            await update.effective_message.reply_text('إذا واجهتك صعوبة بالتسجيل، الأستاذ يكمل معلوماتك:',
                reply_markup=K([[B('👨‍🏫 التفعيل بواسطة الأستاذ',callback_data='v11|help')]]))
        return result
    ns['start']=start

    old_show=ns['show_task']
    async def show_task(q,context,task_id):
        if not admin(q.from_user.id):
            student=await db.get_student(q.from_user.id)
            if not student or not student.get('approved') or student.get('admin_blocked') or student.get('reset_pending'):
                await q.answer('حسابك غير مفعل.',show_alert=True); return
        await open_exempt(task_id,q.from_user.id)
        row=await sql('SELECT * FROM biology_submissions WHERE task_id=%s AND user_id=%s;', (task_id,q.from_user.id),True)
        if row and row.get('submitted_at') and not admin(q.from_user.id):
            rows=[]
            denial=retry_denial(row)
            if denial in (None,'open'):
                rows.append([B('✏️ تغيير الإجابة' if denial is None else '📤 إكمال تغيير الإجابة',callback_data=f'retrysubmission|{task_id}')])
            rows.append([ns['back_menu']()])
            status='مرفوضة — احتُسب إنذار' if row['rejected'] else f"مصححة: {row['grade']}/100" if row['grade'] is not None else 'بانتظار التصحيح'
            await q.edit_message_text(f'📄 إجابتك {status}\nالتغيير الشخصي متاح مرة واحدة قبل التصحيح. طلب الأستاذ لا يستهلك هذه المحاولة.',reply_markup=K(rows)); return
        return await old_show(q,context,task_id)
    ns['show_task']=show_task

    old_deliver=ns['_v48_deliver_submission_row']
    async def deliver(bot,row):
        result=await old_deliver(bot,row)
        if result in ('added','replaced','album_part','delivered'):
            delivery=await sql('SELECT * FROM biology_submission_delivery_outbox WHERE id=%s;', (row['id'],),True)
            if delivery and delivery.get('delivered_message_id'):
                # Attach controls to the answer itself; no duplicate control messages.
                try:
                    await bot.edit_message_reply_markup(chat_id=delivery['destination_chat_id'],message_id=delivery['delivered_message_id'],
                        reply_markup=K([[B('❌ رفض + إنذار',callback_data=f"v11|reject|{row['task_id']}|{row['user_id']}|{row['id']}"),
                                         B('✏️ طلب تغيير',callback_data=f"v11|change|{row['task_id']}|{row['user_id']}|{row['id']}")]]))
                except ns['TelegramError']: LOG.warning('Could not attach answer controls',exc_info=True)
        return result
    ns['_v48_deliver_submission_row']=deliver

    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query; data=str(q.data or ''); uid=q.from_user.id
        if data.startswith('retrysubmission|'):
            try: tid=int(data.split('|')[1])
            except ValueError: await q.answer('رابط غير صالح.'); return
            result=await revision(tid,uid)
            status=result['status']
            if status not in ('ok','open'):
                labels={'graded':'تم تصحيح الإجابة؛ لا يمكن تغييرها.','limit':'استخدمت فرصة التغيير الواحدة.',
                    'expired':'انتهت المهلة. اطلب من الأستاذ طلب تغيير الإجابة.','rejected':'رفض الأستاذ الإجابة.',
                    'model_answer_locked':'وصل الجواب النموذجي؛ لا يمكن التغيير الشخصي.', 'pending_delivery':'انتظر وصول الإجابة المحفوظة أولاً.'}
                await q.answer(labels.get(status,'التغيير غير متاح.'),show_alert=True); return
            context.user_data['waiting_submission']=tid
            context.user_data.pop('submission_album',None)
            await q.answer(); await q.edit_message_text('📤 أرسل الإجابة الجديدة كاملة الآن. تبقى إجابتك القديمة محفوظة حتى وصول البديلة.'); return
        if data.startswith(('task|','examopen|','submit|')):
            try: await open_exempt(int(data.split('|')[1]),uid)
            except (ValueError,IndexError): await q.answer('رابط غير صالح.'); return
        if (data in ('tasks|exam|normal','course_current_exams') or data.startswith('v11|current|')) and not admin(uid):
            student=await db.get_student(uid)
            if not student or not student.get('approved') or student.get('admin_blocked'): return await old_button(update,context)
            if not (await db.bio6_flags(uid)).get('exams',True): return await old_button(update,context)
            tasks=await sql('''SELECT t.id,t.title,sub.submitted_at,sub.grade FROM biology_tasks t
                JOIN biology_task_students r ON r.task_id=t.id AND r.user_id=%s
                LEFT JOIN biology_submissions sub ON sub.task_id=t.id AND sub.user_id=r.user_id
                WHERE t.kind='exam' AND t.retired_obligation=FALSE
                AND (t.closed=FALSE OR (sub.submitted_at IS NOT NULL AND sub.grade IS NULL))
                ORDER BY t.deadline,t.id;''',(uid,))
            try: page=max(0,int(data.split('|')[2])) if data.startswith('v11|current|') else 0
            except (ValueError,IndexError): await q.answer('صفحة غير صالحة.'); return
            page=min(page,max(0,(len(tasks)-1)//15))
            rows=[[B(('📤 ' if r['submitted_at'] else '📝 ')+r['title'][:65],callback_data=f"task|{r['id']}")] for r in tasks[page*15:(page+1)*15]]
            nav=[]
            if page: nav.append(B('السابق',callback_data=f'v11|current|{page-1}'))
            if (page+1)*15<len(tasks): nav.append(B('التالي',callback_data=f'v11|current|{page+1}'))
            if nav: rows.append(nav)
            await q.answer(); await q.edit_message_text('📝 امتحاناتي الحالية\nاختر المحاضرة/الامتحان لعرض الإجابة أو تغييرها.' if rows else 'لا توجد امتحانات حالية.',reply_markup=K(rows+[[ns['back_menu']()]])); return
        if not data.startswith('v11|'): return await old_button(update,context)
        parts=data.split('|'); action=parts[1]
        if action in ('enroll','reject','change','course','course_name') and not admin(uid):
            await q.answer('للأستاذ فقط.',show_alert=True); return
        if admin(uid) and context.user_data.get('preview_student'):
            await q.answer('اخرج من المعاينة أولاً.',show_alert=True); return
        try:
            if action=='help':
                if await db.students_by_parent(uid,False): await q.answer('هذا حساب ولي أمر.'); return
                if (await db.get_student(uid) or {}).get('approved'): await q.answer('حسابك مفعل بالفعل.'); return
                last=context.user_data.get('v11_help_at')
                now=datetime.now(ZONE)
                if last and now-last<timedelta(minutes=5): await q.answer('طلبك وصل؛ انتظر الأستاذ.'); return
                destination=int(ns.get('ACTIVATION_GROUP_ID') or 0)
                if destination>=0: await q.answer('مجموعة التفعيل غير مضبوطة.',show_alert=True); return
                await context.bot.send_message(destination,f'👨‍🏫 طلب إكمال تسجيل بواسطة الأستاذ\n👤 {q.from_user.full_name}\n🆔 {uid}',
                    reply_markup=K([[B('✍️ إكمال معلومات الطالب',callback_data=f'v11|enroll|{uid}')]]))
                context.user_data['v11_help_at']=now
                context.user_data.pop('registration',None)
                await q.answer('تم إرسال طلبك بنجاح.',show_alert=True); return
            if action=='enroll':
                target=int(parts[2]); context.user_data['v11_enroll']={'uid':target,'at':datetime.now(ZONE),'chat':q.message.chat_id}
                await q.answer(); await q.message.reply_text('أرسل معلومات الطالب بثلاثة أسطر:\nالاسم الثلاثي أو الرباعي\nاسم المدرسة\nالمعدل المطلوب (0–100)\n\nبعد الحفظ يظهر كود ولي الأمر ليربط حسابه بالأمر /parent.'); return
            if action in ('reject','change'):
                tid,target,delivery_id=map(int,parts[2:5])
                ref=await db.submission_by_review(q.message.chat_id,q.message.message_id)
                delivery=await sql('SELECT * FROM biology_submission_delivery_outbox WHERE id=%s;', (delivery_id,),True)
                if not ref or ref['task_id']!=tid or ref['user_id']!=target or not delivery or delivery['delivered_message_id']!=q.message.message_id:
                    await q.answer('هذه نسخة قديمة من الإجابة.',show_alert=True); return
                result=await revision(tid,target,teacher=True,actor=uid,reject=action=='reject')
                if result['status']!='ok': await q.answer('الإجابة مصححة أو سبق اتخاذ الإجراء عليها.',show_alert=True); return
                text='❌ رفض الأستاذ إجابتك، واحتُسب إنذار.' if action=='reject' else '✏️ طلب الأستاذ تغيير إجابتك. لديك 24 ساعة على الأقل، بلا خصم نقاط ولا استهلاك محاولتك الشخصية.'
                markup=None if action=='reject' else K([[B('📤 تغيير إجابتي',callback_data=f'retrysubmission|{tid}')]])
                try:
                    await context.bot.send_message(target,text,reply_markup=markup)
                    await q.answer('تم حفظ القرار وإبلاغ الطالب.',show_alert=True)
                except ns['TelegramError']:
                    await q.answer('حُفظ القرار لكن تعذر إبلاغ الطالب. اطلب منه فتح الامتحان.',show_alert=True)
                return
            if action=='contact':
                target=int(parts[2]); students=await db.students_by_parent(uid,True)
                if not any(s['user_id']==target for s in students): await q.answer('غير مسموح.',show_alert=True); return
                context.user_data['v11_contact']={'uid':target,'at':datetime.now(ZONE)}
                await q.answer(); await q.edit_message_text('اكتب رسالتك للأستاذ أو أرسل صورة/ملف. سيصلك رده هنا.',reply_markup=K([[B('إلغاء',callback_data='v11|cancel')]])); return
            if action=='cancel':
                for key in ('v11_contact','v11_enroll','v11_course_name'): context.user_data.pop(key,None)
                await q.answer(); await q.edit_message_text('تم الإلغاء. أرسل /start للقائمة.'); return
            if action=='course':
                title=await db.setting_value('v11_course_name') or 'الدورة الحالية'
                await q.answer(); await q.edit_message_text(f'🗓 {title}\nالأيام الافتراضية: الاثنين والثلاثاء والخميس؛ بنفس مجموعات المحاضرات الأصلية.\nيمكن تعديل كل محاضرة واسمها وموعدها، وإضافة أو حذف محاضرات من محرر الجدول.',reply_markup=K([
                    [B('✏️ تغيير اسم الدورة',callback_data='v11|course_name')],
                    [B('🗓 تحرير الجدول والمحاضرات',callback_data='ctl_calendar')],[ns['back_menu']()]])); return
            if action=='course_name':
                context.user_data['v11_course_name']={'at':datetime.now(ZONE)}
                await q.answer(); await q.edit_message_text('أرسل الاسم الجديد للدورة (حتى 100 حرف).'); return
        except (ValueError,IndexError):
            await q.answer('بيانات غير صالحة. افتح القائمة مجدداً.',show_alert=True)
    ns['button_handler']=button

    async def input_message(update,context):
        msg=update.effective_message
        if not msg or not update.effective_user: return False
        uid=update.effective_user.id
        state=context.user_data.get('v11_enroll') if admin(uid) else None
        if state:
            if datetime.now(ZONE)-state['at']>timedelta(minutes=20):
                context.user_data.pop('v11_enroll',None); await msg.reply_text('انتهت مهلة إكمال التسجيل. افتح الطلب مجدداً.'); return True
            if msg.chat_id!=state['chat']: return False
            values=(msg.text or '').strip().splitlines()
            if len(values)!=3 or not 3<=len(values[0].split())<=8 or len(values[0])>120 or not 2<=len(values[1])<=160:
                await msg.reply_text('أرسل الاسم الثلاثي/الرباعي ثم المدرسة ثم المعدل بثلاثة أسطر.'); return True
            try: grade=int(values[2]); assert 0<=grade<=100
            except (ValueError,AssertionError): await msg.reply_text('المعدل من 0 إلى 100.'); return True
            target=state['uid']
            if admin(target): await msg.reply_text('لا يمكن تسجيل الأستاذ كطالب.'); return True
            student=await db.register_student(target,None,values[0],values[1],str(grade))
            if student.get('status')=='parent_account': await msg.reply_text('هذا الحساب ولي أمر ولا يمكن تحويله.'); return True
            from admin_student_access import change_access
            await change_access(db,target,'activate',uid)
            code,_=await db.v7_ensure_parent_code(target)
            context.user_data.pop('v11_enroll',None)
            text=f'✅ تم تسجيل وتفعيل الطالب: {values[0]}\n🔑 ربط ولي الأمر: /parent {code}\nولي الأمر يفتح البوت ويرسل هذا الأمر من حسابه.\nالطالب يفتح /start لاختيار المسار والمحاضرة.'
            await msg.reply_text(text)
            try: await context.bot.send_message(target,text)
            except ns['TelegramError']: await msg.reply_text('تعذر إرسال إشعار للطالب؛ معلوماته محفوظة.')
            return True
        state=context.user_data.get('v11_course_name') if admin(uid) else None
        if state:
            if datetime.now(ZONE)-state['at']>timedelta(minutes=20): context.user_data.pop('v11_course_name',None); return False
            name=(msg.text or '').strip()
            if not 1<=len(name)<=100: await msg.reply_text('أرسل اسماً من 1 إلى 100 حرف.'); return True
            await db.set_setting_value('v11_course_name',name)
            ns['_V11_COURSE_NAME']=name
            context.user_data.pop('v11_course_name',None)
            await msg.reply_text('✅ تم تغيير اسم الدورة.'); return True
        state=context.user_data.get('v11_contact')
        if state:
            if datetime.now(ZONE)-state['at']>timedelta(minutes=20): context.user_data.pop('v11_contact',None); return False
            if not any(s['user_id']==state['uid'] for s in await db.students_by_parent(uid,True)):
                context.user_data.pop('v11_contact',None); return True
            dest=int(ns.get('OWNER_CHAT_ID') or 0)
            if not dest: await msg.reply_text('وجهة مراسلة الأستاذ غير مضبوطة.'); return True
            try:
                sent=await ns['send_message_payload'](context.bot,dest,msg,
                    f"💬 رسالة ولي أمر الطالب {state['uid']}\nمن: {update.effective_user.full_name}\n\n{msg.text or msg.caption or ''}")
                await sql('INSERT INTO biology_v11_parent_messages(chat_id,message_id,parent_id,student_id) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING;',
                    (dest,sent.message_id,uid,state['uid']),commit=True)
            except ns['TelegramError']:
                await msg.reply_text('تعذر إرسال الرسالة. حاول مجدداً.'); return True
            context.user_data.pop('v11_contact',None)
            await msg.reply_text('✅ تم إرسال رسالتك بنجاح.'); return True
        if admin(uid) and msg.reply_to_message:
            command=(msg.text or '').strip()
            if command in ('رفض الإجابة','طلب تغيير الإجابة'):
                ref=await db.submission_by_review(msg.chat_id,msg.reply_to_message.message_id)
                if ref:
                    rejected=command=='رفض الإجابة'
                    result=await revision(ref['task_id'],ref['user_id'],teacher=True,actor=uid,reject=rejected)
                    if result['status']!='ok':
                        await msg.reply_text('الإجابة مصححة أو الإجراء غير متاح حالياً.'); return True
                    text='❌ رفض الأستاذ إجابتك واحتُسب إنذار.' if rejected else '✏️ طلب الأستاذ تغيير إجابتك، بلا خصم. لديك 24 ساعة على الأقل.'
                    markup=None if rejected else K([[B('📤 تغيير إجابتي',callback_data=f"retrysubmission|{ref['task_id']}")]])
                    try:
                        await context.bot.send_message(ref['user_id'],text,reply_markup=markup)
                        await msg.reply_text('✅ حُفظ القرار وأُبلغ الطالب.')
                    except ns['TelegramError']:
                        await msg.reply_text('حُفظ القرار لكن تعذر إبلاغ الطالب. يمكنه فتح الامتحان من البوت.')
                    return True
            route=await sql('SELECT * FROM biology_v11_parent_messages WHERE chat_id=%s AND message_id=%s;', (msg.chat_id,msg.reply_to_message.message_id),True)
            if route:
                if not any(s['user_id']==route['student_id'] for s in await db.students_by_parent(route['parent_id'],True)):
                    await msg.reply_text('لم يعد ولي الأمر مربوطاً بهذا الطالب.'); return True
                try:
                    await ns['send_message_payload'](context.bot,route['parent_id'],msg,'💬 رد الأستاذ\n\n'+(msg.text or msg.caption or ''))
                    await msg.reply_text('✅ تم إرسال رسالتك بنجاح.')
                except ns['TelegramError']: await msg.reply_text('تعذر إرسال الرد. حاول مجدداً.')
                return True
        return False
    ns['v11_input_message']=input_message
    old_private=ns['private_messages']
    async def private(update,context):
        if await input_message(update,context): return
        return await old_private(update,context)
    ns['private_messages']=private
    old_group=ns.get('route_group_messages')
    if old_group is not None:
        async def group(update,context):
            if await input_message(update,context): return
            return await old_group(update,context)
        ns['route_group_messages']=group
