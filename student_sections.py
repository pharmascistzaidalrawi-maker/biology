"""Per-student visibility and obligations, always enabled by default."""
from contextvars import ContextVar
from functools import wraps
from student_sections_schema import SECTIONS,task_section

LABELS={'lectures':'🎬 المحاضرات والتحاضير','homework':'📚 الواجبات','exams':'📝 الامتحانات','school_review':'🏫 مراجعة المدرسة'}
_VIEW=ContextVar('student_section_view',default=None)

def callback_section(data):
    data=data or ''
    if data.startswith(('v54_school','pdfstudent|','school_review','schoolreview','schoolreview|')):return 'school_review'
    if data.startswith(('previous|homework|','coursework|homework|')):return 'homework'
    if data.startswith(('current_exams_menu','previous|exam|','coursework|exam|')):return 'exams'
    if data.startswith(('tasks|homework','homework|')):return 'homework'
    if data.startswith(('cohort|','today_prep','playlists','chapter|','lecture|','prepopen|','prepwatch|','prepprivate|','prepcomplete|','prepverify|',
        'backlog','royal_review','v42_review','v41_review','v52_unlock','v52_early','weakness','weak_',
        'v55_exam_watch|','v55_exam_study|','v55_exam_oath|','v55_exam_verify|',
        'pages|','ar6|early|','eng6|early|','math6|early|','phys6|early|','fr6|early|','gen|','is10|','preview_lecture','bio6|early|','chem6|early|','math6|early|','isl6|early|','v48_quick_reviews',
        'edu_plan','study_resources','v48_quick_student','schedules_menu','chapter_completion_schedule','personal_schedule','v37_personal_schedule')):return 'lectures'
    if data.startswith(('tasks|exam','exams_menu','examopen|','v52_exam','v42_bank','v45_',
        'examarchive|','past_exams','exam_menu','v42_exam_bank','course_current_exams','freeextend|',
        'resourcecategory|model_answer','v51_model','model_answers')):return 'exams'
    return None

def filter_keyboard(markup,flags):
    if not markup or not hasattr(markup,'inline_keyboard'):return markup
    from telegram import InlineKeyboardMarkup
    rows=[[b for b in row if not callback_section(b.callback_data) or flags.get(callback_section(b.callback_data),True)] for row in markup.inline_keyboard]
    return InlineKeyboardMarkup([row for row in rows if row])

def install_keyboard_filter():
    from telegram import Bot
    if getattr(Bot._post,'_student_sections',False):return
    old=Bot._post
    async def post(self,endpoint,data=None,*args,**kwargs):
        view=_VIEW.get()
        if view and data and data.get('reply_markup') and str(data.get('chat_id')) in {str(x) for x in view['recipients']}:
            data={**data,'reply_markup':filter_keyboard(data['reply_markup'],view['flags'])}
        return await old(self,endpoint,data,*args,**kwargs)
    post._student_sections=True;Bot._post=post

def install(ns,db,prefix):
    if prefix not in ('english','islamic','chemistry','biology','mathematics','physics','arabic','french'):raise ValueError('Invalid subject')
    p=prefix;legacy={'english':'eng6','chemistry':'chem6','islamic':'isl6','biology':'bio6','mathematics':'math6','physics':'phys6','arabic':'ar6','french':'fr6'}[p]
    B,K=ns['InlineKeyboardButton'],ns['InlineKeyboardMarkup']
    install_keyboard_filter()
    async def sql(query,args=(),one=False):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(query,args)
                result=(cur.fetchone() if one else cur.fetchall()) if cur.description else None
                conn.commit();return result
        return await db.run(op)
    async def flags(uid):
        row=await sql(f'SELECT * FROM {p}_student_modules WHERE user_id=%s;',(int(uid),),True)
        return {**dict.fromkeys(SECTIONS,True),'listener':False,**(row or {})}
    async def enabled(uid,section):return bool((await flags(uid)).get(section,True))
    db.section_flags=flags;db.section_enabled=enabled
    setattr(db,legacy+'_flags',flags)

    async def task_enabled(uid,tid,accountable=False):
        task=await db.get_task(int(tid))
        if not task:return False
        f=await flags(uid)
        return bool(f.get(task_section(task),True) and (not accountable or not f.get('listener')))

    async def set_flag(uid,field,actor):
        if field not in (*SECTIONS,'listener'):raise ValueError('خانة غير صالحة.')
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f'SELECT user_id FROM {p}_students WHERE user_id=%s FOR UPDATE;',(int(uid),))
                if not cur.fetchone():raise ValueError('الطالب غير مسجل.')
                cur.execute(f'INSERT INTO {p}_student_modules(user_id) VALUES(%s) ON CONFLICT DO NOTHING;',(int(uid),))
                cur.execute(f'''UPDATE {p}_student_modules SET {field}=NOT {field},changed_by=%s,
                    updated_at=CURRENT_TIMESTAMP WHERE user_id=%s RETURNING *;''',(int(actor),int(uid)))
                row=cur.fetchone()
                if field=='school_review':
                    cur.execute(f'''INSERT INTO {p}_school_review_students(user_id,active,enabled_by)
                        VALUES(%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET active=EXCLUDED.active,
                        enabled_by=EXCLUDED.enabled_by,enabled_at=CURRENT_TIMESTAMP;''',(int(uid),row[field],int(actor)))
                # Withdraw only policy warnings for the section being disabled.
                # A manual warning without an automatic policy remains intact.
                if (field in SECTIONS and not row[field]) or (field=='listener' and row[field]):
                    cur.execute(f'''DELETE FROM {p}_warning_log w USING {p}_tasks t
                        WHERE w.task_id=t.id AND w.user_id=%s
                        AND (COALESCE(w.issued_by,0)=0 OR w.policy_key IN ('failed','rejected'))
                        AND (%s='listener' OR CASE WHEN t.school_review_id IS NOT NULL THEN 'school_review'
                            WHEN t.kind='exam' THEN 'exams' ELSE 'homework' END=%s);''',(int(uid),field,field))
                    cur.execute(f'''UPDATE {p}_students SET warnings=(SELECT COUNT(*) FROM {p}_warning_log
                        WHERE user_id=%s) WHERE user_id=%s;''',(int(uid),int(uid)))
                cur.execute(f"INSERT INTO {p}_audit(actor_id,action,details) VALUES(%s,'student_section_toggle',%s);",(int(actor),f'user={uid};section={field};enabled={row[field]}'))
                conn.commit();return row
        return await db.run(op)
    db.set_student_section=set_flag;ns['_'+legacy+'_set_flag']=set_flag

    async def panel(q,uid):
        student=await db.get_student(uid)
        if not student:await q.answer('الطالب غير موجود.',show_alert=True);return
        f=await flags(uid)
        rows=[[B(('🟢 ' if f[field] else '🔴 ')+LABELS[field],callback_data=f'sections|toggle|{uid}|{field}')] for field in SECTIONS]
        rows.append([B(('🟢 ' if f.get('listener') else '🔴 ')+'مستمع بدون عقوبات',callback_data=f'sections|toggle|{uid}|listener')])
        rows.append([B('👤 ملف الطالب',callback_data=f'admin_student|{uid}'),B('👥 إدارة الطلاب',callback_data='admin_students')])
        await q.edit_message_text(f"⚙️ خانات الطالب: {student['full_name']}\n🆔 {uid}\n\nاضغط الخانة لتفعيلها أو تعطيلها.\nالطالب يرى ويُحاسب فقط على الخانات المفعّلة.\nكل الخانات مفعّلة افتراضياً للطلاب.",reply_markup=K(rows))

    # Every synchronous home menu runs inside an update-local scope. No shared
    # current-student cache can leak one student's choices into another's menu.
    old_menu=ns['main_menu']
    def menu(admin=False):
        markup=old_menu(admin)
        view=_VIEW.get()
        return filter_keyboard(markup,view['flags']) if view and not admin else markup
    ns['main_menu']=menu

    async def scope(update,context):
        user=getattr(update,'effective_user',None) or getattr(getattr(update,'callback_query',None),'from_user',None)
        if not user:return None
        uid=int(user.id)
        preview=context.user_data.get('preview_student') if ns['is_admin'](uid) else None
        if ns['is_admin'](uid) and not preview:return None
        target=int(preview or uid)
        return dict(flags=await flags(target),uid=target,recipients=(uid,target))

    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query;data=q.data or '';actor=q.from_user.id
        if data.startswith('sections|') or data.startswith(legacy+'|toggle|'):
            if not ns['is_admin'](actor):await q.answer('للإدارة فقط.',show_alert=True);return
            if context.user_data.get('preview_student'):
                await q.answer('اخرج من معاينة الطالب أولاً.',show_alert=True);return
            try:
                parts=data.split('|');action=parts[1];uid=int(parts[2])
                if action=='toggle':await set_flag(uid,parts[3],actor)
                elif action!='student':raise ValueError('خيار غير صالح.')
            except (ValueError,IndexError) as exc:await q.answer(str(exc),show_alert=True);return
            await q.answer('تم حفظ الخانات.' if action=='toggle' else '')
            await panel(q,uid);return
        view=await scope(update,context);token=_VIEW.set(view)
        try:
            if view:
                field=callback_section(data)
                if data.startswith(('task|','submit|','retrysubmission|','extend|','submissionok|','requestadminexam|')):
                    try:task=await db.get_task(int(data.split('|')[1]));field=task_section(task)
                    except (IndexError,ValueError):pass
                if field and not view['flags'][field]:
                    await q.answer('هذه الخانة معطّلة لحسابك من الإدارة.',show_alert=True);return
            return await old_button(update,context)
        finally:_VIEW.reset(token)
    ns['button_handler']=button

    # /start and all private input flows use the same scope, including states
    # opened before an administrator disabled the section.
    for name in ('start','private_messages','v11_input_message'):
        if name not in ns:continue
        old=ns[name]
        def factory(fn,source):
            @wraps(fn)
            async def scoped(update,context,*args,**kwargs):
                view=await scope(update,context);token=_VIEW.set(view)
                try:
                    if view and source!='start':
                        waiting=context.user_data.get('waiting_submission')
                        denied=waiting and not await task_enabled(view['uid'],waiting)
                        keys=('awaiting_exam_bank_oath','awaiting_private_study_oath','v41_review_oath_id','v41_weakness_oath_id')
                        if not view['flags']['lectures'] and any(context.user_data.get(k) for k in keys):
                            for k in keys:context.user_data.pop(k,None)
                            denied=True
                        if not view['flags']['school_review'] and context.user_data.pop('awaiting_school_review_oath',None):denied=True
                        if denied:
                            context.user_data.pop('waiting_submission',None)
                            await update.effective_message.reply_text('هذه الخانة معطّلة لحسابك من الإدارة.')
                            return True if source=='v11_input_message' else None
                    return await fn(update,context,*args,**kwargs)
                finally:_VIEW.reset(token)
            return scoped
        ns[name]=factory(old,name)

    # Authoritative data behind the daily requirements, not just button hiding.
    old_report=db.parent_report_bundle
    async def section_report(uid,*args,**kwargs):
        bundle=await old_report(uid,*args,**kwargs);f=await flags(uid)
        return {**bundle,**{key:[t for t in bundle[key] if f.get(task_section(t),True)] for key in ('current','previous')}}
    db.parent_report_bundle=ns['parent_report_bundle']=section_report

    old_daily=db.v51_daily_tasks
    async def daily(uid):
        bundle=await old_daily(uid);f=await flags(uid)
        bundle={**bundle,'tasks':[t for t in bundle.get('tasks',[]) if f[task_section(t)]]}
        if not f['lectures']:bundle.update(preparation=None,reviews=[],weaknesses=0)
        return bundle
    db.v51_daily_tasks=daily
    for name in ('v28_student_homeworks','v28_student_exam_tasks','v52_current_preparation'):
        old=getattr(db,name)
        field='homework' if name=='v28_student_homeworks' else 'lectures' if name=='v52_current_preparation' else 'exams'
        def factory(fn,field,none):
            async def scoped(uid,*args,**kwargs):
                if not await enabled(uid,field):return None if none else []
                return await fn(uid,*args,**kwargs)
            return scoped
        setattr(db,name,factory(old,field,name=='v52_current_preparation'))

    # Worker selections are rechecked at send/penalty boundaries below.
    selectors={'incomplete_preparation_students':'lectures','incomplete_personal_preparation_students':'lectures',
        'due_personal_preparation_notifications':'lectures','v41_due_review_reminders':'lectures',
        'v54_enabled_school_review_students':'school_review','v54_due_school_review_reminders':'school_review',
        'v54_due_school_review_exams':'school_review','active_students_for_linked_exam':'exams'}
    for name,field in selectors.items():
        old=getattr(db,name,None)
        if not old:continue
        def factory(fn,field):
            async def scoped(*args,**kwargs):
                rows=await fn(*args,**kwargs);result=[]
                for row in rows:
                    if await enabled(row['user_id'],field):result.append(row)
                return result
            return scoped
        fn=factory(old,field);setattr(db,name,fn)
        if name in ns:ns[name]=fn

    old_personal=db.v29_ready_personal_exams
    async def ready_personal(*args,**kwargs):
        return [row for row in await old_personal(*args,**kwargs) if await enabled(row['user_id'],'exams')]
    db.v29_ready_personal_exams=ready_personal
    old_models=db.v52_due_model_answers
    async def models(*args,**kwargs):
        return [row for row in await old_models(*args,**kwargs) if await task_enabled(row['user_id'],row['task_id'])]
    db.v52_due_model_answers=models

    # Direct APIs and inline/old buttons cannot bypass the same choices.
    for name in ('effective_task_deadline','student_exam_lock','v45_exam_task_status'):
        old=getattr(db,name)
        def factory(fn,name):
            async def scoped(*args,**kwargs):
                result=await fn(*args,**kwargs)
                if not result:return result
                uid=(args[1] if len(args)>1 else kwargs.get('user_id')) if name=='effective_task_deadline' else (args[0] if args else kwargs.get('user_id'))
                tid=(args[0] if args else kwargs.get('task_id')) if name=='effective_task_deadline' else (result.get('id') or result.get('task_id'))
                if name=='v45_exam_task_status':tid=args[1] if len(args)>1 else kwargs.get('task_id')
                if tid and not await task_enabled(uid,tid):
                    return {**result,'assigned':False,'closed':True} if name=='effective_task_deadline' else None
                return result
            return scoped
        fn=factory(old,name);setattr(db,name,fn);ns[name]=fn
    old_show=ns['show_task']
    async def show(q,context,tid):
        if not ns['is_admin'](q.from_user.id) and not await task_enabled(q.from_user.id,tid):
            await q.answer('هذه الخانة معطّلة لحسابك من الإدارة.',show_alert=True);return
        return await old_show(q,context,tid)
    ns['show_task']=show

    # Keep the older school-management switch consistent with Student Management.
    async def toggle_school(uid,actor):
        row=await set_flag(uid,'school_review',actor)
        return {'status':'ok','enabled':row['school_review'],'row':row}
    db.v54_toggle_school_review_student=toggle_school

