"""Explicit session intents, atomic parent approvals, and authorized account controls."""
from datetime import datetime,timedelta,timezone
from functools import wraps
import asyncio

OATH_KEYS=('awaiting_private_study_oath','awaiting_exam_bank_oath','awaiting_school_review_oath','v41_review_oath_id','v41_weakness_oath_id','awaiting_review_oath','awaiting_weakness_oath')
UPLOAD_KEYS=('waiting_submission','submission_upload','submission_album')
def clear_flows(data,upload=True):
    for key in list(data):
        if key in OATH_KEYS or key.startswith('awaiting_') or key.endswith('_input') or key in ('profile_refresh','gen_link','registration','registration_started_at','edu_input','v11_contact','v11_enroll','v11_course_name','manual_task','linked_exam','v52_model_answer','v29_publish','v41_study_days','v41_study_required','_flow_stamp','_repair_input') or (upload and key in (*UPLOAD_KEYS,'_upload_last_activity','_upload_task')):data.pop(key,None)

def install(ns,db,p):
    if p not in ('english','chemistry','islamic','biology','mathematics','physics','arabic','french'):raise ValueError(p)
    B,K=ns['InlineKeyboardButton'],ns['InlineKeyboardMarkup']
    async def sql(statement,args=(),one=False,write=False):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(statement.replace('BOT_',p+'_'),args)
                value=(cur.fetchone() if one else cur.fetchall()) if cur.description else None
                if write:conn.commit()
                return value
        return await db.run(op)
    async def audit(actor,action,details):
        await sql("INSERT INTO BOT_audit(actor_id,action,details) VALUES(%s,%s,%s);",(actor,action,details),write=True)
    def stamp(data):
        if any(key in data for key in OATH_KEYS):data['_flow_stamp']=datetime.now(timezone.utc).timestamp()
        if data.get('waiting_submission'):
            data['_upload_last_activity']=datetime.now(timezone.utc).timestamp();data['_upload_task']=data['waiting_submission']
    def expire(data):
        if data.get('waiting_submission') and datetime.now(timezone.utc).timestamp()-data.get('_upload_last_activity',datetime.now(timezone.utc).timestamp())>7200:
            clear_flows(data);return True
        if any(key in data for key in OATH_KEYS) and datetime.now(timezone.utc).timestamp()-data.get('_flow_stamp',0)>1200:
            clear_flows(data,upload=False)

    # This is deliberately one transaction: a failed activation cannot leave an
    # approval receipt behind; repeated parent clicks preserve the same window.
    async def approve(tid,uid,actor,allow=True):
        admin=ns['is_admin'](actor)
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f'SELECT * FROM {p}_students WHERE user_id=%s FOR UPDATE;',(uid,));student=cur.fetchone()
                if not student:return {'status':'missing'}
                if not admin:
                    cur.execute(f'SELECT approved FROM {p}_parent_links WHERE student_id=%s AND parent_chat_id=%s;',(uid,actor));link=cur.fetchone()
                    authorized=bool(link and link['approved']) or bool(not link and student.get('parent_approved') and student.get('parent_chat_id')==actor)
                    if not authorized:return {'status':'forbidden'}
                cur.execute(f'''SELECT t.* FROM {p}_tasks t JOIN {p}_task_students ts ON ts.task_id=t.id AND ts.user_id=%s
                    LEFT JOIN {p}_linked_exam_definitions d ON d.id=t.exam_definition_id
                    WHERE t.id=%s AND t.kind='exam' AND t.retired_obligation=FALSE AND (d.id IS NULL OR d.deleted_at IS NULL) FOR UPDATE OF t;''',(uid,tid));task=cur.fetchone()
                if not task or student.get('reset_pending') or student.get('admin_blocked') or not student.get('approved'):return {'status':'unavailable'}
                section='school_review' if task.get('school_review_id') else 'exams'
                cur.execute(f'SELECT {p}_section_enabled(%s,%s) AS enabled;',(uid,section))
                if not admin and not cur.fetchone()['enabled']:return {'status':'disabled'}
                if admin:cur.fetchone()
                cur.execute(f'SELECT 1 FROM {p}_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;',(tid,uid))
                if cur.fetchone():return {'status':'submitted','task':task}
                cur.execute(f'SELECT * FROM {p}_exam_access WHERE task_id=%s AND user_id=%s FOR UPDATE;',(tid,uid));access=cur.fetchone()
                cur.execute('SELECT CURRENT_TIMESTAMP AS now;');now=cur.fetchone()['now']
                cur.execute(f'SELECT extended_until FROM {p}_task_extensions WHERE task_id=%s AND user_id=%s;',(tid,uid));extension=cur.fetchone()
                deadline=max(task['deadline'],extension['extended_until']) if extension else task['deadline']
                available=task.get('exam_available_at') or now
                legacy_pending=bool(task.get('exam_pending_activation'))
                fixed=False
                if task.get('school_review_id'):
                    cur.execute(f'SELECT * FROM {p}_school_reviews WHERE id=%s FOR SHARE;',(task['school_review_id'],));review=cur.fetchone()
                    if not review or review.get('exam_deleted') or review.get('exam_manual_closed'):return {'status':'closed'}
                    deadline=max(review.get('exam_closes_at') or deadline,extension['extended_until']) if extension else review.get('exam_closes_at') or deadline
                    available=review.get('exam_opens_at') or available;fixed=True
                elif task.get('exam_definition_id'):
                    cur.execute(f'SELECT target_scope FROM {p}_linked_exam_definitions WHERE id=%s;',(task['exam_definition_id'],));definition=cur.fetchone()
                    fixed=bool(definition and definition['target_scope']=='course' and not task.get('optional_practice'))
                if not legacy_pending and not task.get('closed') and deadline>now and access and access['status']=='approved':return {'status':'already_open','task':{**task,'deadline':deadline}}
                if not allow:
                    if access and access['status']=='approved' and not legacy_pending:return {'status':'already_open','task':task}
                    cur.execute(f'''INSERT INTO {p}_exam_access(task_id,user_id,status,approved_by,approved_at) VALUES(%s,%s,'denied',%s,CURRENT_TIMESTAMP)
                        ON CONFLICT(task_id,user_id) DO UPDATE SET status='denied',approved_by=EXCLUDED.approved_by,approved_at=CURRENT_TIMESTAMP;''',(tid,uid,actor))
                    conn.commit();return {'status':'denied','task':task}
                if legacy_pending and not fixed:
                    available=now;deadline=now+timedelta(hours=max(1,int(task.get('exam_duration_hours') or 24)))
                elif deadline<=now:return {'status':'expired','task':task}
                elif task['closed'] and not legacy_pending:return {'status':'closed','task':task}
                cur.execute(f'''UPDATE {p}_tasks SET exam_pending_activation=FALSE,exam_approval_required=(exam_approval_required OR exam_pending_activation),
                    closed=FALSE,deadline=%s,exam_available_at=%s,published_at=COALESCE(published_at,%s) WHERE id=%s RETURNING *;''',(deadline if legacy_pending else task['deadline'],available,now,tid));task=cur.fetchone()
                task={**task,'deadline':deadline}
                cur.execute(f'''INSERT INTO {p}_exam_access(task_id,user_id,status,approved_by,approved_at) VALUES(%s,%s,'approved',%s,CURRENT_TIMESTAMP)
                    ON CONFLICT(task_id,user_id) DO UPDATE SET status='approved',approved_by=EXCLUDED.approved_by,approved_at=CURRENT_TIMESTAMP;''',(tid,uid,actor))
                cur.execute(f"INSERT INTO {p}_audit(actor_id,action,details) VALUES(%s,'atomic_exam_approval',%s);",(actor,f'task={tid};student={uid}'))
                conn.commit();return {'status':'scheduled' if available>now else 'opened','task':task}
        return await db.run(op)
    db.approve_exam_atomically=approve

    async def panel(q,uid):
        student=await db.get_student(uid)
        if not student:await q.answer('الطالب غير موجود.',show_alert=True);return
        rows=[[B('⚙️ الأقسام المفعّلة',callback_data=f'sections|student|{uid}'),B('👁 معاينة',callback_data=f'preview_student|{uid}')],
              [B('📝 واجبات وامتحانات الطالب',callback_data=f'repair|tasks|{uid}|0')],
              [B('✏️ تعديل بيانات الطالب',callback_data=f'repair|profile|{uid}'),B('⭐ تعديل XP',callback_data=f'repair|xp|{uid}')],
              [B('🧹 إنهاء جلسة الطالب القديمة',callback_data=f'repair|session|{uid}')],
              [B('⚠️ إنذارات الطالب',callback_data=f'repair|warnings|{uid}'),B('🎬 تعديل إكمال محاضرة',callback_data=f'repair|progress|{uid}')],
              [B('🔓 فك حظر الطالب من المجموعة',callback_data=f'repair|unban|{uid}')],
              [B('🔓 تفعيل الحساب',callback_data=f'admin_access|activate|{uid}'),B('⛔ إيقاف الحساب',callback_data=f'admin_access|block|{uid}')],
              [B('👥 إدارة الطلبة',callback_data='admin_students'),ns['back_menu']()]]
        await q.answer();await q.edit_message_text(ns['bold'](f"🛠 إدارة حساب الطالب\n{student['full_name']}\n🆔 {uid}\n⭐ {student['xp']} XP | ⚠️ {student['warnings']}/10\nالتعديلات الإدارية محفوظة بسجل المتابعة."),parse_mode='HTML',reply_markup=K(rows))
    async def task_panel(q,uid,tid):
        task=await db.get_task(tid)
        assigned=await sql('SELECT 1 FROM BOT_task_students WHERE task_id=%s AND user_id=%s;',(tid,uid),one=True)
        if not task or not assigned:await q.answer('المهمة ليست ضمن حساب الطالب.',show_alert=True);return
        sub=await sql('SELECT * FROM BOT_submissions WHERE task_id=%s AND user_id=%s;',(tid,uid),one=True)
        rows=[[B('🔓 فتح/تمديد للطالب',callback_data=f'repair|reopen|{uid}|{tid}')]]
        if sub and sub.get('submitted_at'):
            rows.append([B('💯 تعديل الدرجة',callback_data=f'repair|grade|{uid}|{tid}'),B('🔄 طلب حل بديل',callback_data=f'repair|revision|{uid}|{tid}')])
        rows.append([B('◀️ حساب الطالب',callback_data=f'admin_student|{uid}')])
        await q.answer();await q.edit_message_text(ns['bold'](f"{task['title']}\nالطالب: {uid}\nالحالة: "+('مسلّم' if sub and sub.get('submitted_at') else 'لم يسلّم')+f"\nالدرجة: {sub.get('grade') if sub else '-'}"),parse_mode='HTML',reply_markup=K(rows))
    async def reopen(uid,tid,actor):
        if not ns['is_admin'](actor):return False
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f'SELECT user_id FROM {p}_students WHERE user_id=%s FOR UPDATE;',(uid,))
                if not cur.fetchone():return False
                cur.execute(f'SELECT t.* FROM {p}_tasks t JOIN {p}_task_students a ON a.task_id=t.id WHERE t.id=%s AND a.user_id=%s FOR UPDATE OF t;',(tid,uid));task=cur.fetchone()
                if not task:return False
                hours=48 if task['kind']=='homework' else 24
                cur.execute(f'''INSERT INTO {p}_task_extensions(task_id,user_id,extended_until) VALUES(%s,%s,CURRENT_TIMESTAMP+(%s*INTERVAL '1 hour'))
                    ON CONFLICT(task_id,user_id) DO UPDATE SET extended_until=GREATEST({p}_task_extensions.extended_until,EXCLUDED.extended_until);''',(tid,uid,hours))
                # Activation flags are shared, approval remains per student.
                cur.execute(f'UPDATE {p}_tasks SET closed=FALSE,retired_obligation=FALSE,exam_pending_activation=FALSE WHERE id=%s;',(tid,))
                cur.execute(f'''INSERT INTO {p}_exam_access(task_id,user_id,status,approved_by,approved_at) VALUES(%s,%s,'approved',%s,CURRENT_TIMESTAMP)
                    ON CONFLICT(task_id,user_id) DO UPDATE SET status='approved',approved_by=EXCLUDED.approved_by,approved_at=CURRENT_TIMESTAMP;''',(tid,uid,actor))
                cur.execute(f"INSERT INTO {p}_audit(actor_id,action,details) VALUES(%s,'admin_reopen_student_task',%s);",(actor,f'student={uid};task={tid};hours={hours}'))
                conn.commit();return True
        return await db.run(op)
    db.admin_reopen_student_task=reopen
    async def admin_grade(uid,tid,grade,actor,revision=False):
        if not ns['is_admin'](actor) or (grade is not None and not 0<=grade<=100):return False
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f'SELECT user_id FROM {p}_students WHERE user_id=%s FOR UPDATE;',(uid,))
                if not cur.fetchone():return False
                cur.execute(f'SELECT * FROM {p}_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL FOR UPDATE;',(tid,uid));sub=cur.fetchone()
                if not sub:return False
                cur.execute(f"INSERT INTO {p}_v11_submission_history(task_id,user_id,actor_id,action,snapshot) SELECT task_id,user_id,%s,%s,to_jsonb(s) FROM {p}_submissions s WHERE task_id=%s AND user_id=%s;",(actor,'admin_revision' if revision else 'admin_grade',tid,uid))
                cur.execute(f'UPDATE {p}_submissions SET grade=%s,graded_by=%s,graded_at=CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP END,revision_open=%s,teacher_change_pending=%s,rejected=FALSE WHERE task_id=%s AND user_id=%s;',(grade,None if revision else actor,revision,revision,revision,tid,uid))
                cur.execute(f'SELECT kind FROM {p}_tasks WHERE id=%s;',(tid,));task=cur.fetchone()
                if task and task['kind']=='exam':db._set_xp_event(cur,uid,0 if revision else grade-80,'تعديل إداري للدرجة',f'grade:{tid}:{uid}')
                if revision:cur.execute(f"DELETE FROM {p}_warning_log WHERE task_id=%s AND user_id=%s AND policy_key IN ('failed','rejected');",(tid,uid))
                cur.execute(f'UPDATE {p}_students SET warnings=(SELECT COUNT(*) FROM {p}_warning_log WHERE user_id=%s) WHERE user_id=%s;',(uid,uid))
                conn.commit();return True
        return await db.run(op)
    db.admin_update_grade=admin_grade

    awards_lock=asyncio.Lock()
    async def recover(context):
        if awards_lock.locked():return
        async with awards_lock:
            for task in await db.closed_exams_pending_champion():
                try:await ns['announce_champions'](context,task['id'])
                except Exception:ns['logger'].exception('Award recovery failed: task=%s',task['id'])
    ns['recover_all_awards']=recover
    old_menu=ns['main_menu']
    def menu(admin=False):
        result=old_menu(admin)
        if not admin:return result
        return K([[B('🛠 إدارة الحسابات والأبطال',callback_data='repair|students|0|0')]]+[list(r) for r in result.inline_keyboard])
    ns['main_menu']=menu

    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query;data=str(q.data or '');actor=q.from_user.id;admin=ns['is_admin'](actor)
        if admin and data.startswith(('repair|','admin_student|','admin_access|','admin_access_confirm|')):
            clear_flows(context.user_data)
        if admin and (data.startswith(('admin_','sections|','repair|','adminexam','v11|reject|','v11|change|','v43_','v42_admin','v53_admin','v54_school_admin')) or data in ('admin_students','admin_publish')):
            context.user_data.pop('preview_student',None)
        if data.startswith(('examallow|','examdeny|','adminexamallow|','adminexamdeny|')):
            try:action,raw,student=data.split('|');tid,uid=int(raw),int(student)
            except ValueError:await q.answer('رابط غير صالح.',show_alert=True);return
            if action.startswith('admin') and not admin:await q.answer('للإدارة فقط.',show_alert=True);return
            result=await approve(tid,uid,actor,allow='deny' not in action);status=result['status']
            labels={'opened':'✅ تم تفعيل الامتحان.','scheduled':'✅ حُفظت الموافقة؛ يفتح الامتحان بموعده المحدد.','already_open':'✅ الامتحان مفعّل مسبقاً؛ الموعد ثابت.','denied':'تم رفض الطلب.','expired':'انتهت مهلة الامتحان. اطلب من الأستاذ تمديداً من إدارة حساب الطالب.','submitted':'الطالب سلّم هذا الامتحان مسبقاً.','forbidden':'هذا الطلب ليس لحسابك.','disabled':'قسم الامتحان معطّل لهذا الطالب.','closed':'الامتحان مغلق إدارياً؛ يستطيع الأستاذ فتحه من إدارة الطالب.'}
            await q.answer(labels.get(status,'الامتحان غير متاح أو حُذف؛ لم تُغيّر بياناته.'),show_alert=True)
            if status in ('opened','scheduled'):
                task=result['task'];until=task['deadline'].astimezone(ns['TIMEZONE']).strftime('%d/%m/%Y %H:%M')
                try:await context.bot.send_message(uid,ns['bold'](f"✅ وافق ولي الأمر/الإدارة على الامتحان\n{task['title']}\n⏰ نهاية المهلة: {until}"),parse_mode='HTML',reply_markup=K([[B('📝 فتح الامتحان',callback_data=f'task|{tid}')]]))
                except ns['TelegramError']:ns['logger'].warning('Exam approval notice failed: %s',uid)
            return
        if admin and data.startswith('admin_student|'):
            context.user_data.pop('_repair_input',None)
            try:uid=int(data.split('|')[1])
            except ValueError:await q.answer('معرّف غير صالح.',show_alert=True);return
            return await panel(q,uid)
        if data.startswith('repair|'):
            if not admin:await q.answer('للإدارة فقط.',show_alert=True);return
            try:
                _,action,raw,*extra=data.split('|');uid=int(raw);tid=int(extra[0]) if extra else 0
            except (ValueError,IndexError):await q.answer('رابط غير صالح.',show_alert=True);return
            if action=='students':
                students=await sql('SELECT user_id,full_name FROM BOT_students ORDER BY full_name LIMIT 500;')
                page=max(0,tid);rows=[[B(s['full_name'],callback_data=f'admin_student|{s["user_id"]}')] for s in students[page*12:(page+1)*12]]
                if page:rows.append([B('السابق',callback_data=f'repair|students|0|{page-1}')])
                if len(students)>(page+1)*12:rows.append([B('التالي',callback_data=f'repair|students|0|{page+1}')])
                rows += [[B('🏆 فحص ونشر الأبطال المستحقين',callback_data='repair|awards|0')],[ns['back_menu']()]]
                await q.answer();await q.edit_message_text(ns['bold']('🛠 اختر الطالب لإدارة حسابه.'),parse_mode='HTML',reply_markup=K(rows));return
            if action=='awards':
                if not ns.get(p.upper()+'_GROUP_ID') or not ns.get('CHAMPIONS_TOPIC_ID'):
                    await q.answer('اضبط آيدي مجموعة المادة وCHAMPIONS_TOPIC_ID في إعدادات الاستضافة أولاً.',show_alert=True);return
                await q.answer('جارٍ فحص الأبطال المستحقين.');await recover(context)
                await q.edit_message_text(ns['bold']('✅ اكتمل فحص الأبطال. ينشر أعلى المصحّحين بعد انتهاء مهلة المهمة، وتُحدّث النتيجة عند تصحيح درجة أعلى.'),parse_mode='HTML',reply_markup=ns['main_menu'](True));return
            if not await db.get_student(uid):await q.answer('الطالب غير موجود.',show_alert=True);return
            if action=='warnings':
                warnings=await sql('SELECT id,reason FROM BOT_warning_log WHERE user_id=%s ORDER BY id DESC LIMIT 50;',(uid,))
                rows=[[B('🗑 '+w['reason'][:45],callback_data=f'repair|unwarn|{uid}|{w["id"]}')] for w in warnings]
                rows += [[B('➕ إضافة إنذار',callback_data=f'repair|warn|{uid}')],[B('حساب الطالب',callback_data=f'admin_student|{uid}')]]
                await q.answer();await q.edit_message_text(ns['bold']('⚠️ إنذارات الطالب — اضغط الإنذار لحذفه.'),parse_mode='HTML',reply_markup=K(rows));return
            if action=='unwarn':
                def remove():
                    with db.connect() as conn,conn.cursor() as cur:
                        cur.execute(f'SELECT user_id FROM {p}_students WHERE user_id=%s FOR UPDATE;',(uid,))
                        cur.execute(f'DELETE FROM {p}_warning_log WHERE id=%s AND user_id=%s;',(tid,uid))
                        cur.execute(f'UPDATE {p}_students SET warnings=(SELECT COUNT(*) FROM {p}_warning_log WHERE user_id=%s) WHERE user_id=%s;',(uid,uid));conn.commit()
                await db.run(remove);await audit(actor,'admin_remove_warning',f'student={uid};warning={tid}')
                await q.answer('تم حذف الإنذار.',show_alert=True);return
            if action=='unban':
                group=ns.get(p.upper()+'_GROUP_ID')
                if not group:await q.answer('مجموعة المادة غير مضبوطة.',show_alert=True);return
                try:await context.bot.unban_chat_member(group,uid,only_if_banned=True)
                except ns['TelegramError']:
                    await q.answer('تعذر فك الحظر؛ تحقق من صلاحية البوت بالمجموعة.',show_alert=True);return
                await audit(actor,'admin_unban_student',f'student={uid}');await q.answer('تم فك حظر الطالب من المجموعة.',show_alert=True);return
            if action=='session':
                app=getattr(context,'application',None)
                if app is not None:
                    student_data=app.user_data.get(uid)
                    if student_data is not None:clear_flows(student_data);student_data.pop('_repair_input',None)
                await audit(actor,'admin_clear_student_session',f'student={uid}')
                await q.answer('انتهت جلسة الطالب القديمة؛ بياناته وحلوله محفوظة.',show_alert=True);return
            if action=='tasks':
                tasks=await sql('SELECT t.* FROM BOT_tasks t JOIN BOT_task_students a ON a.task_id=t.id WHERE a.user_id=%s ORDER BY t.id DESC LIMIT 300;',(uid,))
                page=max(0,tid);rows=[[B(t['title'],callback_data=f'repair|task|{uid}|{t["id"]}')] for t in tasks[12*page:12*(page+1)]]
                if page:rows.append([B('السابق',callback_data=f'repair|tasks|{uid}|{page-1}')])
                if len(tasks)>12*(page+1):rows.append([B('التالي',callback_data=f'repair|tasks|{uid}|{page+1}')])
                rows.append([B('حساب الطالب',callback_data=f'admin_student|{uid}')]);await q.answer();await q.edit_message_text(ns['bold']('📝 اختر مهمة الطالب.'),parse_mode='HTML',reply_markup=K(rows));return
            if action=='task':return await task_panel(q,uid,tid)
            if action=='reopen':
                ok=await reopen(uid,tid,actor);await q.answer('تم فتح المهلة للطالب.' if ok else 'المهمة غير متاحة.',show_alert=True);return
            if action in ('grade','revision'):
                if not await sql('SELECT 1 FROM BOT_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;',(tid,uid),one=True):
                    await q.answer('لا توجد إجابة مسلّمة.',show_alert=True);return
                if action=='revision':
                    await admin_grade(uid,tid,None,actor,revision=True)
                    await reopen(uid,tid,actor);await audit(actor,'admin_request_answer_replacement',f'student={uid};task={tid}')
                    try:await context.bot.send_message(uid,'✏️ طلب الأستاذ إعادة إرسال إجابتك. افتح المهمة وأرسل الحل البديل.',reply_markup=K([[B('📤 إرسال الحل البديل',callback_data=f'task|{tid}')]]))
                    except ns['TelegramError']:pass
                    await q.answer('تم فتح تغيير الإجابة.',show_alert=True);return
            if action in ('profile','xp','grade','warn','progress'):
                context.user_data['_repair_input']=dict(kind=action,uid=uid,tid=tid,at=datetime.now(timezone.utc).timestamp())
                labels={'profile':'أرسل الاسم، المدرسة، والمعدل المطلوب بثلاثة أسطر.','xp':'أرسل مقدار تعديل XP؛ مثال 20 أو -10.','grade':'أرسل الدرجة الجديدة من 0 إلى 100.','warn':'أرسل سبب الإنذار الإداري.','progress':'أرسل: رقم الفصل ثم رقم المحاضرة ثم 1 للإكمال أو 0 لإلغاء الإكمال. مثال: 1 2 1'}
                await q.answer();await q.edit_message_text(ns['bold'](labels[action]),parse_mode='HTML',reply_markup=K([[B('إلغاء',callback_data=f'admin_student|{uid}')]]));return
            await q.answer('خيار غير صالح.',show_alert=True);return
        from student_sections import callback_section
        new_section=callback_section(data)
        if data in ('menu','preview_home','exams_menu','tasks|homework') or new_section=='lectures' or data.startswith(('previous|','coursework|')):
            clear_flows(context.user_data)
        elif data.startswith(('task|','submit|','retrysubmission|')):
            try:target=int(data.split('|')[1])
            except (ValueError,IndexError):target=None
            clear_flows(context.user_data,upload=target!=context.user_data.get('waiting_submission'))
        expire(context.user_data)
        for key in (*OATH_KEYS,'_flow_stamp'):context.user_data.pop(key,None)
        if not data.startswith(('uploadfinish|','submissionok|','task|','submit|','retrysubmission|')):
            for key in (*UPLOAD_KEYS,'_upload_last_activity','_upload_task'):context.user_data.pop(key,None)
        result=await old_button(update,context);stamp(context.user_data);return result
    ns['button_handler']=button

    old_input=ns['v11_input_message'];old_private=ns['private_messages']
    async def dispatch(update,context,source):
        msg=update.effective_message;user=update.effective_user
        if not msg or not user:return False
        data=context.user_data;expired=expire(data)
        if expired:
            await msg.reply_text('انتهت جلسة رفع الحل القديمة. افتح المهمة واضغط إرسال الحل لتحديد الواجب أو الامتحان الصحيح.');return True
        marker=(getattr(msg,'chat_id',user.id),getattr(msg,'message_id',None))
        if marker[1] is not None and data.get('_handled_upload_message')==marker:return True
        pending=data.get('_repair_input')
        if pending and ns['is_admin'](user.id):
            if datetime.now(timezone.utc).timestamp()-pending['at']>1200:
                data.pop('_repair_input',None);await msg.reply_text('انتهت مهلة التعديل. افتح حساب الطالب مجدداً.');return True
            text=(msg.text or '').strip();uid=pending['uid'];kind=pending['kind']
            try:
                if kind=='profile':
                    parts=text.splitlines()
                    if len(parts)!=3 or not 2<=len(parts[0])<=120 or not 2<=len(parts[1])<=160 or not 0<=int(parts[2])<=100:raise ValueError('أرسل الاسم والمدرسة والمعدل بثلاثة أسطر صحيحة.')
                    await sql('UPDATE BOT_students SET full_name=%s,school=%s,target_grade=%s WHERE user_id=%s;',(*parts,uid),write=True)
                elif kind=='xp':
                    delta=int(text)
                    if not -100000<=delta<=100000:raise ValueError('مقدار التعديل خارج المدى.')
                    await db.adjust_xp(uid,delta,'تعديل إداري',actor_id=user.id)
                elif kind=='warn':
                    if not 2<=len(text)<=500:raise ValueError('أرسل سبباً من 2 إلى 500 حرف.')
                    count=await db.add_warning(uid,text,user.id)
                    if count>=ns['MAX_WARNINGS'] and ns.get(p.upper()+'_GROUP_ID'):
                        try:await context.bot.ban_chat_member(ns[p.upper()+'_GROUP_ID'],uid)
                        except ns['TelegramError']:pass
                elif kind=='progress':
                    values=list(map(int,text.split()))
                    if len(values)!=3:raise ValueError('أرسل ثلاثة أرقام: الفصل المحاضرة 1 أو 0.')
                    chapter,lecture,done=values
                    if done not in (0,1) or not any(int(item[0])==lecture for item in ns['PLAYLISTS'].get(chapter,[])):raise ValueError('الفصل أو المحاضرة غير موجودة.')
                    if done:await db.mark_lecture_progress(uid,chapter,lecture,True,'admin_override')
                    else:await sql('UPDATE BOT_lecture_progress SET completed_at=NULL,completion_method=%s WHERE user_id=%s AND chapter=%s AND lecture=%s;',('admin_override',uid,chapter,lecture),write=True)
                else:
                    grade=int(text)
                    if not 0<=grade<=100:raise ValueError('الدرجة من 0 إلى 100.')
                    await admin_grade(uid,pending['tid'],grade,user.id)
                    await ns['announce_champions'](context,pending['tid'])
                await audit(user.id,'admin_student_'+kind,f'student={uid};value={text}')
            except ValueError as exc:await msg.reply_text(str(exc) or 'أرسل قيمة صحيحة.');return True
            data.pop('_repair_input',None);await msg.reply_text('✅ تم حفظ التعديل.',reply_markup=K([[B('حساب الطالب',callback_data=f'admin_student|{uid}')]]));return True
        if data.get('waiting_submission'):
            clear_flows(data,upload=False)
            data['_upload_last_activity']=datetime.now(timezone.utc).timestamp()
            handled=await ns['receive_submission'](update,context)
            if handled and marker[1] is not None:data['_handled_upload_message']=marker
            return bool(handled)
        if any(key in data for key in OATH_KEYS):
            await old_private(update,context)
            if marker[1] is not None:data['_handled_upload_message']=marker
            return True
        return None
    async def input_message(update,context):
        handled=await dispatch(update,context,'input')
        return handled if handled is not None else await old_input(update,context)
    async def private(update,context):
        handled=await dispatch(update,context,'private')
        if handled is not None:return
        return await old_private(update,context)
    ns['v11_input_message']=input_message;ns['private_messages']=private
    old_start=ns['start'];old_cancel=ns.get('cancel_registration');old_menu_command=ns.get('menu_command')
    def reset_command(fn):
        @wraps(fn)
        async def wrapped(update,context,*a,**kw):
            clear_flows(context.user_data);context.user_data.pop('_repair_input',None)
            return await fn(update,context,*a,**kw)
        return wrapped
    ns['start']=reset_command(old_start)
    if old_cancel:ns['cancel_registration']=reset_command(old_cancel)
    if old_menu_command:ns['menu_command']=reset_command(old_menu_command)
    old_post=ns['post_init']
    async def post(app):
        await old_post(app)
        app.job_queue.run_repeating(recover,60,first=10,name='oct30_award_recovery')
    ns['post_init']=post
