"""Private, penalty-free copies of course work published before enrollment."""

def migrate(db,p):
    with db.connect() as conn,conn.cursor() as cur:
        cur.execute(f'''ALTER TABLE {p}_tasks ADD COLUMN IF NOT EXISTS catchup_source_id INTEGER
                REFERENCES {p}_tasks(id) ON DELETE CASCADE;
            ALTER TABLE {p}_tasks ADD COLUMN IF NOT EXISTS catchup_owner BIGINT
                REFERENCES {p}_students(user_id) ON DELETE CASCADE;
            CREATE UNIQUE INDEX IF NOT EXISTS {p}_catchup_owner_source
                ON {p}_tasks(catchup_owner,catchup_source_id) WHERE catchup_source_id IS NOT NULL;
            CREATE OR REPLACE FUNCTION {p}_catchup_no_penalty() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.task_id IS NOT NULL AND
                   (COALESCE(NEW.issued_by,0)=0 OR NEW.policy_key IN ('failed','rejected')) AND
                   EXISTS(SELECT 1 FROM {p}_tasks t WHERE t.id=NEW.task_id
                    AND t.catchup_owner=NEW.user_id AND t.catchup_source_id IS NOT NULL)
                THEN RETURN NULL; END IF;
                RETURN NEW;
            END; $$;
            DROP TRIGGER IF EXISTS {p}_catchup_no_penalty ON {p}_warning_log;
            CREATE TRIGGER {p}_catchup_no_penalty BEFORE INSERT ON {p}_warning_log
                FOR EACH ROW EXECUTE FUNCTION {p}_catchup_no_penalty();''')
        conn.commit()

def synchronize(cur,p,uid):
    cur.execute(f'SELECT * FROM {p}_students WHERE user_id=%s FOR UPDATE;',(int(uid),))
    s=cur.fetchone()
    if not s or s.get('study_track')!='course' or s.get('reset_pending') or not s.get('approved'):return
    # The immutable registration timestamp is the cutoff: route changes cannot
    # turn work published during the student's enrollment into free practice.
    cur.execute(f'''SELECT DISTINCT ON (COALESCE('school:'||t.school_review_id::TEXT,
                'exam:'||t.exam_definition_id::TEXT,'task:'||t.id::TEXT)) t.*
        FROM {p}_tasks t LEFT JOIN {p}_linked_exam_definitions d ON d.id=t.exam_definition_id
        WHERE t.catchup_source_id IS NULL AND t.school_review_id IS NULL
          AND (t.target_scope IN ('course','all') OR t.target_scope LIKE 'chapter_%%' OR (d.target_scope IN ('course','chapter') AND t.kind='exam'))
          AND (d.id IS NULL OR d.deleted_at IS NULL)
          AND (t.published_at IS NULL OR t.published_at<=CURRENT_TIMESTAMP)
          AND COALESCE(LEAST(t.published_at,t.created_at),t.created_at)<%s
          AND (t.exam_available_at IS NULL OR t.exam_available_at<%s)
          AND NOT EXISTS(SELECT 1 FROM {p}_submissions done
              WHERE done.user_id=%s AND done.task_id=t.id AND done.submitted_at IS NOT NULL)
        ORDER BY COALESCE('school:'||t.school_review_id::TEXT,
                'exam:'||t.exam_definition_id::TEXT,'task:'||t.id::TEXT),t.created_at,t.id;''',
        (s['registered_at'],s['registered_at'],int(uid)))
    for task in cur.fetchall():
        # Never duplicate an already completed attempt from the same definition.
        if task.get('exam_definition_id'):
            cur.execute(f'''SELECT 1 FROM {p}_submissions sub JOIN {p}_tasks old ON old.id=sub.task_id
                WHERE sub.user_id=%s AND old.exam_definition_id=%s AND sub.submitted_at IS NOT NULL;''',
                (int(uid),task['exam_definition_id']))
            if cur.fetchone():continue
        cur.execute(f'''SELECT id FROM {p}_tasks WHERE catchup_owner=%s AND catchup_source_id=%s;''',(int(uid),task['id']))
        existing=cur.fetchone()
        if existing:clone=existing['id']
        else:
            # Allocate a unique source key from the serial sequence without using
            # Telegram message ids, which can collide across private copies.
            cur.execute(f"SELECT nextval(pg_get_serial_sequence('{p}_tasks','id')) AS id;")
            clone=cur.fetchone()['id']
            cur.execute(f'''INSERT INTO {p}_tasks(id,kind,title,chat_id,thread_id,source_message_id,
                payload_type,file_id,text_content,deadline,xp_reward,created_by,target_scope,
                linked_lectures,closed,optional_practice,questions_released,published_at,
                catchup_source_id,catchup_owner)
                VALUES(%s,%s,%s,%s,NULL,%s,%s,%s,%s,TIMESTAMPTZ '2099-12-31 23:59:59+03',
                    %s,%s,%s,%s,FALSE,TRUE,TRUE,CURRENT_TIMESTAMP,%s,%s);''',
                (clone,task['kind'],task['title'],int(uid),-clone,task['payload_type'],task.get('file_id'),
                 task.get('text_content') or '',task.get('xp_reward') or 10,task.get('created_by'),
                 f'student:{uid}',task.get('linked_lectures') or '',task['id'],int(uid)))
            cur.execute(f'''INSERT INTO {p}_task_media(task_id,payload_type,file_id,source_message_id)
                SELECT %s,payload_type,file_id,source_message_id FROM {p}_task_media WHERE task_id=%s
                ON CONFLICT DO NOTHING;''',(clone,task['id']))
        cur.execute(f'INSERT INTO {p}_task_students(task_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING;',(clone,int(uid)))
        # Remove only unsubmitted pre-enrollment assignments mistakenly added by
        # older onboarding, and their automatic warnings. Original work survives.
        cur.execute(f'''DELETE FROM {p}_task_students WHERE user_id=%s AND task_id=%s
            AND NOT EXISTS(SELECT 1 FROM {p}_submissions WHERE user_id=%s AND task_id=%s AND submitted_at IS NOT NULL);''',
            (int(uid),task['id'],int(uid),task['id']))
        cur.execute(f'''DELETE FROM {p}_warning_log WHERE user_id=%s AND task_id=%s
            AND (COALESCE(issued_by,0)=0 OR policy_key IN ('failed','rejected'));''',(int(uid),task['id']))
    # Also clear duplicate legacy per-student copies of the same old exam;
    # DISTINCT source selection above must not leave a second warning path.
    cur.execute(f'''SELECT t.id FROM {p}_tasks t
        LEFT JOIN {p}_linked_exam_definitions d ON d.id=t.exam_definition_id
        WHERE t.catchup_source_id IS NULL AND t.school_review_id IS NULL
          AND (t.target_scope IN ('course','all') OR t.target_scope LIKE 'chapter_%%' OR d.target_scope IN ('course','chapter'))
          AND (t.published_at IS NULL OR t.published_at<=CURRENT_TIMESTAMP)
          AND COALESCE(LEAST(t.published_at,t.created_at),t.created_at)<%s
          AND (t.exam_available_at IS NULL OR t.exam_available_at<%s)
          AND NOT EXISTS(SELECT 1 FROM {p}_submissions sub WHERE sub.task_id=t.id AND sub.user_id=%s AND sub.submitted_at IS NOT NULL);''',
        (s['registered_at'],s['registered_at'],int(uid)))
    old_ids=[r['id'] for r in cur.fetchall()]
    if old_ids:
        cur.execute(f'DELETE FROM {p}_task_students WHERE user_id=%s AND task_id=ANY(%s);',(int(uid),old_ids))
        cur.execute(f'''DELETE FROM {p}_warning_log WHERE user_id=%s AND task_id=ANY(%s)
            AND (COALESCE(issued_by,0)=0 OR policy_key IN ('failed','rejected'));''',(int(uid),old_ids))
    cur.execute(f'UPDATE {p}_students SET warnings=(SELECT COUNT(*) FROM {p}_warning_log WHERE user_id=%s) WHERE user_id=%s;',(int(uid),int(uid)))

def install(ns,db,p):
    assert p in ('chemistry','islamic','english','biology')
    old_init=ns['init_db']
    def init():old_init();migrate(db,p)
    db.init_db=ns['init_db']=init
    async def sync(uid=None):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                ids=[int(uid)] if uid is not None else None
                if ids is None:
                    cur.execute(f"SELECT user_id FROM {p}_students WHERE study_track='course' AND reset_pending=FALSE;")
                    ids=[r['user_id'] for r in cur.fetchall()]
                for item in ids:synchronize(cur,p,item)
                conn.commit()
        return await db.run(op)
    db.sync_course_catchup=sync
    for name in ('v47_choose_track','set_student_onboarding'):
        old=getattr(db,name)
        def wrap(fn):
            async def changed(uid,*a,**kw):
                result=await fn(uid,*a,**kw)
                await sync(uid)
                return result
            return changed
        setattr(db,name,wrap(old))
        if name in ns:ns[name]=getattr(db,name)
    old_speed=db._v54_award_exam_speed_bonus_sync
    def speed(tid,uid):
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute(f'SELECT 1 FROM {p}_tasks WHERE id=%s AND catchup_source_id IS NOT NULL;',(tid,))
            if cur.fetchone():return None
        return old_speed(tid,uid)
    db._v54_award_exam_speed_bonus_sync=speed
    old_xp=db._set_xp_event
    def xp(cur,uid,delta,reason,event_key):
        if str(event_key).startswith('grade:'):
            try:tid=int(str(event_key).split(':')[1])
            except (ValueError,IndexError):tid=None
            if tid:
                cur.execute(f'SELECT 1 FROM {p}_tasks WHERE id=%s AND catchup_owner=%s AND catchup_source_id IS NOT NULL;',(tid,uid))
                if cur.fetchone():delta=max(0,delta)
        return old_xp(cur,uid,delta,reason,event_key)
    db._set_xp_event=xp
    # Keep optional historical practice out of daily required-work lists.
    for name in ('v28_student_homeworks','open_tasks','open_exam_tasks','v51_daily_tasks'):
        old_list=getattr(db,name,None)
        if old_list is None:continue
        def filter_list(fn):
            async def current(*a,**kw):
                result=await fn(*a,**kw)
                return [row for row in result if not row.get('catchup_source_id')] if isinstance(result,list) else result
            return current
        setattr(db,name,filter_list(old_list))
        if name in ns:ns[name]=getattr(db,name)
    old_report=db.parent_report_bundle
    async def report(uid,*a,**kw):
        bundle=await old_report(uid,*a,**kw)
        def practice_ids():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f'SELECT id FROM {p}_tasks WHERE catchup_owner=%s AND catchup_source_id IS NOT NULL;',(uid,))
                return {r['id'] for r in cur.fetchall()}
        optional=await db.run(practice_ids)
        return {**bundle,**{period:[t for t in bundle[period] if t['id'] not in optional] for period in ('current','previous')}}
    db.parent_report_bundle=ns['parent_report_bundle']=report
    old_post=ns['post_init']
    async def post(app):
        await old_post(app)
        await sync()
    ns['post_init']=post
    async def rows(uid):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f'''SELECT t.*,sub.grade,sub.submitted_at FROM {p}_tasks t
                    LEFT JOIN {p}_submissions sub ON sub.task_id=t.id AND sub.user_id=%s
                    WHERE t.catchup_owner=%s AND t.catchup_source_id IS NOT NULL
                      AND {p}_section_enabled(%s,CASE WHEN t.kind='exam' THEN 'exams' ELSE 'homework' END)
                    ORDER BY t.kind,t.catchup_source_id;''',(uid,uid,uid))
                return cur.fetchall()
        return await db.run(op)
    old_menu=ns['main_menu'];B=ns['InlineKeyboardButton'];K=ns['InlineKeyboardMarkup']
    def menu(admin=False):
        result=old_menu(admin)
        if admin:return result
        from student_sections import _VIEW
        view=_VIEW.get()
        if view and not (view['flags'].get('homework',True) or view['flags'].get('exams',True)):return result
        return K([list(r) for r in result.inline_keyboard]+[[B('📚 واجبات وامتحانات سابقة — XP بلا إنذارات',callback_data='catchup|0')]])
    ns['main_menu']=menu
    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query;data=str(q.data or '')
        if data.startswith('task|'):
            try:tid=int(data.split('|')[1])
            except (ValueError,IndexError):return
            task=await db.get_task(tid)
            if task and task.get('catchup_source_id'):
                student=await db.get_student(q.from_user.id)
                section='exams' if task['kind']=='exam' else 'homework'
                if not student or not student.get('approved') or student.get('admin_blocked') or student.get('reset_pending') or task.get('catchup_owner')!=q.from_user.id or not await db.section_enabled(q.from_user.id,section):
                    await q.answer('هذا العنصر غير متاح لحسابك.',show_alert=True);return
                if not student.get('admin_manual_active') and not await ns['is_channel_member'](context.bot,q.from_user.id):
                    await q.answer('اشترك بالقناة أولاً.',show_alert=True);return
                def submitted():
                    with db.connect() as conn,conn.cursor() as cur:
                        cur.execute(f'SELECT * FROM {p}_submissions WHERE task_id=%s AND user_id=%s;',(tid,q.from_user.id))
                        return cur.fetchone()
                submission=await db.run(submitted)
                await q.answer()
                if submission and submission.get('submitted_at'):
                    grade=f"\nالدرجة: {submission['grade']}/100" if submission['grade'] is not None else '\nبانتظار التصحيح.'
                    await q.edit_message_text(ns['bold']('✅ سبق تسليم هذه المهمة التدريبية.'+grade),parse_mode='HTML',reply_markup=K([[B('🔄 تغيير الإجابة',callback_data=f'retrysubmission|{tid}')],[B('العودة للمطلوبات السابقة',callback_data='catchup|0')]]));return
                media=await db.get_task_media(tid)
                if not media and task.get('file_id'):media=[task]
                caption=ns['bold'](f"📚 {task['title']}\nمطلوب سابق: بلا إنذارات وبلا موعد إغلاق، مع XP عند الحل.")
                for i,item in enumerate(media):
                    fn={'photo':context.bot.send_photo,'document':context.bot.send_document,'video':context.bot.send_video}.get(item['payload_type'])
                    if fn:await fn(q.from_user.id,item['file_id'],caption=caption if i==0 else None,parse_mode='HTML' if i==0 else None,protect_content=True)
                if task.get('text_content') and task['text_content']!=task['title']:
                    await context.bot.send_message(q.from_user.id,ns['bold'](task['text_content']),parse_mode='HTML',protect_content=True)
                context.user_data['waiting_submission']=tid
                await q.edit_message_text(ns['bold'](f"📤 {task['title']}\nأرسل حتى 25 صورة أو ملفاً، ثم اضغط إنهاء التسليم.\nلا توجد إنذارات لهذه المهمة."),parse_mode='HTML',reply_markup=K([[B('العودة للمطلوبات السابقة',callback_data='catchup|0')]]));return
        if data.startswith('catchup|'):
            student=await db.get_student(q.from_user.id)
            if not student or not student.get('approved') or student.get('admin_blocked') or student.get('reset_pending') or student.get('study_track')!='course':
                await q.answer('متاح لطلاب مسار الدورة المفعّلين.',show_alert=True);return
            await sync(q.from_user.id)
            all_rows=await rows(q.from_user.id)
            try:page=max(0,int(data.split('|')[1]))
            except ValueError:page=0
            page=min(page,max(0,(len(all_rows)-1)//12))
            keyboard=[]
            for t in all_rows[page*12:page*12+12]:
                status='✅' if t['submitted_at'] else '📤'
                keyboard.append([B(f"{status} {t['title']}",callback_data=f"task|{t['id']}")])
            nav=[]
            if page:nav.append(B('السابق',callback_data=f'catchup|{page-1}'))
            if (page+1)*12<len(all_rows):nav.append(B('التالي',callback_data=f'catchup|{page+1}'))
            if nav:keyboard.append(nav)
            keyboard.append([ns['back_menu']()])
            await q.answer();await q.edit_message_text(ns['bold']('📚 المطلوبات السابقة لتسجيلك\nيمكنك حلها بأي وقت والحصول على XP مرة واحدة لكل مهمة، بدون إنذارات تأخير أو رسوب.'+('' if all_rows else '\nلا توجد مطلوبات سابقة متاحة.')),parse_mode='HTML',reply_markup=K(keyboard));return
        return await old_button(update,context)
    ns['button_handler']=button
