"""Activation delivery, account lifecycle, flexible calendars and admin preview."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

PROFILE_FIELDS = (
    'user_id','username','full_name','school','target_grade','approved',
    'parent_chat_id','parent_username','parent_full_name','parent_approved','parent_link_code',
    'admin_manual_active','admin_blocked','admin_access_previous_approval',
)
ORPHAN_TABLES = (
    ('biology_notifications','user_id'),('biology_question_attempts','user_id'),
    ('biology_student_risk','user_id'),('biology_observed_members','user_id'),
    ('biology_student_topics','user_id'),('biology_communication_routes','student_id'),
)

async def erase_student(db, uid, preserve_profile=False):
    """One transaction: cascade study history; optionally reinsert basic profile/parents."""
    from psycopg import sql
    from psycopg.types.json import Json
    import json
    def op():
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s);',(int(uid),))
            cur.execute('SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;',(int(uid),))
            old=cur.fetchone()
            if not old:return False
            parents=[]
            if preserve_profile:
                cur.execute('SELECT * FROM biology_parent_links WHERE student_id=%s;',(int(uid),))
                parents=cur.fetchall()
            for table,column in ORPHAN_TABLES:
                cur.execute(sql.SQL('DELETE FROM {} WHERE {}=%s').format(sql.Identifier(table),sql.Identifier(column)),(int(uid),))
            cur.execute('DELETE FROM biology_scheduled_tasks WHERE linked_student_id=%s;',(int(uid),))
            cur.execute('DELETE FROM biology_tasks WHERE target_scope=%s;',(f'student:{int(uid)}',))
            cur.execute('DELETE FROM biology_students WHERE user_id=%s;',(int(uid),))
            if preserve_profile:
                profile={key:old[key] for key in PROFILE_FIELDS if key in old}
                profile.update(reset_pending=True,restore_approval=bool(old.get('approved')))
                cur.execute(sql.SQL('INSERT INTO biology_students ({}) VALUES ({})').format(
                    sql.SQL(',').join(map(sql.Identifier,profile)),
                    sql.SQL(',').join(sql.Placeholder() for _ in profile)),tuple(profile.values()))
                if parents:
                    cur.execute('INSERT INTO biology_parent_links SELECT * FROM json_populate_recordset(NULL::biology_parent_links,%s);',
                        (Json(parents,dumps=lambda x:json.dumps(x,default=str)),))
            conn.commit();return True
    return await db.run(op)

async def claim_activation(db,uid):
    def op():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute('''UPDATE biology_students SET activation_requested_at=CURRENT_TIMESTAMP
                WHERE user_id=%s AND approved=FALSE AND admin_blocked=FALSE
                AND (activation_requested_at IS NULL OR activation_requested_at<CURRENT_TIMESTAMP-INTERVAL '5 minutes')
                RETURNING user_id;''',(int(uid),))
            claimed=bool(cur.fetchone());conn.commit();return claimed
    return await db.run(op)

async def release_activation(db,uid):
    def op():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute('UPDATE biology_students SET activation_requested_at=NULL WHERE user_id=%s AND approved=FALSE;',(int(uid),));conn.commit()
    await db.run(op)

def activation_destinations(ns):
    """Activation requests belong in the activation group, never admin DMs."""
    group=int(ns.get('ACTIVATION_GROUP_ID') or 0)
    return [group] if group < 0 else []

class PreviewQuery:
    """Render a real student's read views into the administrator's original chat."""
    def __init__(self,q,uid,ns):
        self._q=q;self.from_user=SimpleNamespace(id=uid);self._ns=ns
    def __getattr__(self,key):return getattr(self._q,key)
    async def answer(self,*args,**kwargs):return await self._q.answer(*args,**kwargs)
    async def edit_message_text(self,text,*args,**kwargs):
        B=self._ns['InlineKeyboardButton'];K=self._ns['InlineKeyboardMarkup']
        markup=kwargs.get('reply_markup');rows=[list(row) for row in (markup.inline_keyboard if markup else [])]
        rows.append([B('👁 قائمة الطالب',callback_data='preview_home',style='primary'),
                     B('🔙 العودة لحساب الأدمن',callback_data='preview_exit',style='danger')])
        kwargs['reply_markup']=K(rows)
        return await self._q.edit_message_text(text,*args,**kwargs)


def install(ns,db):
    B=ns['InlineKeyboardButton'];K=ns['InlineKeyboardMarkup']
    original_menu=ns['main_menu']
    def main_menu(admin=False):
        menu=original_menu(admin)
        if not admin:return menu
        rows=[list(row) for row in menu.inline_keyboard]
        rows.insert(0,[B('👁 معاينة واجهة الطالب',callback_data='admin_preview_list|0',style='primary')])
        return K(rows)
    ns['main_menu']=main_menu
    def clear_cache(uid):
        cache=ns['_V51_STUDENT_CACHE'].get()
        if cache is not None:cache.pop(uid,None)
    def pending_menu(student):
        rows=[[B('🔔 إعادة إرسال طلب التفعيل',callback_data='activation_resend',style='primary')],
              [B('🗑 حذف حسابي',callback_data='self_delete',style='danger')]]
        if not student.get('parent_chat_id'):
            rows.insert(0,[B('📋 نسخ كود ولي الأمر',copy_text=ns['CopyTextButton'](f"/parent {student['parent_link_code']}"),style='primary')])
        return K(rows)

    async def send_activation(bot,student):
        uid=int(student['user_id'])
        if not await claim_activation(db,uid):return {'status':'cooldown','sent':0}
        buttons=K([[B('✅ قبول وتفعيل',callback_data=f'approve|{uid}',style='success'),
                    B('❌ رفض',callback_data=f'reject|{uid}',style='danger')]])
        text=ns['bold'](f"🔔 طلب تفعيل\n👤 {student.get('full_name') or '-'}\n🆔 {uid}\n🏫 {student.get('school') or '-'}\n👨‍👩‍👦 ولي الأمر: {student.get('parent_chat_id') or 'غير مربوط'}")
        async def deliver(destination):
            try:
                await bot.send_message(destination,text,parse_mode=ns['ParseMode'].HTML,reply_markup=buttons)
                return True
            except ns['TelegramError']:
                ns['logger'].warning('Activation delivery failed for destination %s, student %s',destination,uid,exc_info=True)
                return False
        destinations=activation_destinations(ns)
        sent=sum(await asyncio.gather(*(deliver(chat) for chat in destinations)))
        if not sent:await release_activation(db,uid)
        else:
            def mark_delivered():
                with db.connect() as conn,conn.cursor() as cur:
                    cur.execute('UPDATE biology_students SET activation_delivered_at=CURRENT_TIMESTAMP WHERE user_id=%s;',(uid,))
                    conn.commit()
            await db.run(mark_delivered)
        try:
            await bot.send_message(uid,('⏳ تم إرسال طلب تفعيلك إلى مجموعة التفعيل. يمكنك إعادة إرساله من الزر أدناه.' if sent else '⚠️ تعذر إيصال طلبك إلى مجموعة التفعيل. أخبر الإدارة أو حاول مجدداً.'),reply_markup=pending_menu(student))
        except ns['TelegramError']:
            ns['logger'].warning('Student activation status notice failed for %s',uid)
        return {'status':'sent' if sent else 'failed','sent':sent,'total':len(destinations)}
    ns['send_activation_request']=send_activation

    async def recover_activation(context):
        def pending():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute('''SELECT * FROM biology_students WHERE approved=FALSE AND admin_blocked=FALSE
                    AND reset_pending=FALSE AND activation_delivered_at IS NULL
                    AND (activation_requested_at IS NULL OR activation_requested_at<CURRENT_TIMESTAMP-INTERVAL '5 minutes')
                    ORDER BY activation_requested_at NULLS FIRST,user_id LIMIT 30;''')
                return cur.fetchall()
        for student in await db.run(pending):
            try: await send_activation(context.bot,student)
            except Exception: ns['logger'].exception('Activation recovery failed for %s',student['user_id'])
    ns['recover_activation_requests']=recover_activation
    previous_start=ns['start']
    async def start(update,context):
        if ns['is_admin'](update.effective_user.id):
            context.user_data.pop('preview_student',None)
            return await previous_start(update,context)
        student=await db.get_student(update.effective_user.id)
        if student and student.get('reset_pending') and all(student.get(key) for key in ('full_name','school','target_grade')):
            await update.effective_message.reply_text('اختر مسار الدراسة وأول محاضرة. معلوماتك الأساسية محفوظة.',reply_markup=ns['onboarding_track_keyboard']())
            return ns['ConversationHandler'].END
        if student and (not student.get('approved') or student.get('admin_blocked')) and not student.get('reset_pending'):
            context.user_data.pop('registration',None)
            label='⛔ حسابك موقوف بقرار الإدارة.' if student.get('admin_blocked') else '⏳ تسجيلك محفوظ وبانتظار موافقة الإدارة.'
            if not student.get('admin_blocked') and not student.get('activation_requested_at'):
                await send_activation(context.bot,student)
            await update.effective_message.reply_text(label,reply_markup=pending_menu(student))
            return ns['ConversationHandler'].END
        return await previous_start(update,context)
    ns['start']=start

    previous_button=ns['button_handler']
    async def schedule(q,context):
        student=await db.get_student(q.from_user.id)
        if not student or not student.get('approved') or student.get('study_track')!='chapter':
            await q.edit_message_text('تعديل الأيام لطلاب الدراسة حسب الفصول. للدورة جدول الأستاذ.',reply_markup=K([[ns['back_menu']()]]));return
        days=context.user_data.setdefault('flex_days',list(student.get('study_days') or [6,1,3]))
        names=['الاثنين','الثلاثاء','الأربعاء','الخميس','الجمعة','السبت','الأحد']
        rows=[[B(('✅ ' if i in days else '▫️ ')+name,callback_data=f'flex_day|{i}',style='success' if i in days else 'primary')] for i,name in enumerate(names)]
        rows.extend([[B('💾 حفظ الجدول',callback_data='flex_save',style='success')],[ns['back_menu']()]])
        await q.edit_message_text(f'🗓 اختر أيام دراستك بحرية (من يوم إلى 7 أيام).\nعدد الأيام المحددة: {len(days)}\nيعاد توزيع المحاضرات غير المكتملة وتحديث موعد إكمال المنهج.',reply_markup=K(rows))

    async def preview_home(q,uid):
        student=await db.get_student(uid)
        if not student:await q.edit_message_text('الحساب غير موجود.');return
        await PreviewQuery(q,uid,ns).edit_message_text(f"👁 معاينة واجهة الطالب: {student.get('full_name') or uid}\nالمعاينة للعرض؛ تقدم الطالب لا يتغير.",reply_markup=ns['main_menu'](False))

    async def button(update,context):
        q=update.callback_query;data=str(q.data or '');uid=q.from_user.id
        admin=ns['is_admin'](uid)
        if data.startswith('admin_preview_list|'):
            if not admin:await q.answer('للإدارة فقط.',show_alert=True);return
            try:page=max(0,int(data.split('|')[1]))
            except ValueError:await q.answer('صفحة غير صالحة.');return
            context.user_data.pop('preview_student',None)
            students=[s for s in await db.v51_admin_students() if not ns['is_admin'](int(s['user_id']))]
            size=10;page=min(page,max(0,(len(students)-1)//size))
            rows=[[B(f"👁 {s.get('full_name') or s['user_id']}",callback_data=f"preview_student|{s['user_id']}",style='primary')] for s in students[page*size:(page+1)*size]]
            nav=[]
            if page:nav.append(B('السابق',callback_data=f'admin_preview_list|{page-1}',style='primary'))
            if (page+1)*size<len(students):nav.append(B('التالي',callback_data=f'admin_preview_list|{page+1}',style='primary'))
            if nav:rows.append(nav)
            rows.append([B('لوحة الإدارة',callback_data='preview_exit',style='danger')])
            await q.answer();await q.edit_message_text('👁 اختر الطالب لمعاينة واجهته:' if students else 'لا يوجد طلاب للمعاينة بعد.',reply_markup=K(rows));return
        if data=='preview_exit':
            if not admin:await q.answer('للإدارة فقط.',show_alert=True);return
            context.user_data.pop('preview_student',None);await q.answer()
            await q.edit_message_text('لوحة الإدارة',reply_markup=ns['main_menu'](True));return
        if data.startswith('preview_student|'):
            if not admin:await q.answer('للإدارة فقط.',show_alert=True);return
            try:target=int(data.split('|')[1])
            except ValueError:await q.answer('معرف غير صالح.');return
            if not await db.get_student(target):await q.answer('الحساب غير موجود.',show_alert=True);return
            context.user_data['preview_student']=target;await q.answer();await preview_home(q,target);return
        target=context.user_data.get('preview_student') if admin else None
        if target:
            await q.answer()
            if data in {'preview_home','menu'}:await preview_home(q,target);return
            pq=PreviewQuery(q,target,ns)
            # Explicit read-only entry points. No submission/completion/approval callbacks are delegated.
            if data in {'playlists','all_lectures'}:
                await pq.edit_message_text('🎬 جميع المحاضرات',reply_markup=K([[B(f'📘 الفصل {ch}',callback_data=f'preview_lectures|{ch}',style='primary')] for ch in ns['PLAYLISTS']]));return
            if data.startswith(('preview_lectures|','chapter|')):
                ch=int(data.split('|')[1]);items=ns['PLAYLISTS'].get(ch,[])
                rows=[[B(f'المحاضرة {item[0]}',callback_data=f'preview_lecture|{ch}|{item[0]}',style='primary') for item in items[i:i+4]] for i in range(0,len(items),4)]
                await pq.edit_message_text(f'🎬 الفصل {ch}',reply_markup=K(rows));return
            if data.startswith('preview_lecture|'):
                _,ch,no=data.split('|');item=next((r for r in ns['PLAYLISTS'].get(int(ch),[]) if int(r[0])==int(no)),None)
                if item:await pq.edit_message_text(f'🎬 المحاضرة {item[0]}\n{item[1]}\n{item[2]}')
                return
            functions={'exams_menu':('v51_exams_menu',()),'v42_exam_bank':('v52_exam_bank',()),
                       'account_settings':('v51_account_settings',()),'progress':('v51_progress_menu',()),
                       'v51_progress':('v51_progress_menu',()),
                       'v52_progress_overview':('v52_progress_overview',()),
                       'mastery_map':('v39_mastery_menu',()),
                       'weaknesses_menu':('v41_weakness_menu',()),
                       'notifications':('v44_notifications_menu',()),
                       'backlog_auto':('v51_backlog_menu',()),
                       'royal_review_menu':('v51_review_menu',()),
                       'v54_school_review':('v54_school_review_menu',()),
                       'v54_school_exams':('v54_school_review_exams',()),
                       'daily_learning_session':('v51_daily_tasks_menu',()),
                       'chapter_completion_schedule':('v52_progress_overview',()),'schedules_menu':('v37_schedule_menu',())}
            if data.startswith('v52_exam_chapter|'):functions[data]=('v52_exam_chapter',(int(data.split('|')[1]),))
            if data.startswith('v52_exam_lecture|'):functions[data]=('v52_exam_lecture',tuple(map(int,data.split('|')[1:])))
            if data.startswith('weak_chapter|'):functions[data]=('v41_weakness_chapter',(int(data.split('|')[1]),))
            if data.startswith('weak_lecture|'):functions[data]=('v41_weakness_lecture',tuple(map(int,data.split('|')[1:])))
            if data.startswith('v54_school_detail|'):functions[data]=('v54_school_review_detail',(int(data.split('|')[1]),))
            if data.startswith('student_calendar|'):functions[data]=('v41_calendar_menu',(data.split('|')[1],))
            if data.startswith('tasks|'):
                parts=data.split('|');functions[data]=('show_tasks',(parts[1],parts[2] if len(parts)>2 else 'all'))
            if data in functions:
                name,args=functions[data];await ns[name](pq,*args);return
            if data in {'profile','parent_link','achievement_menu','academic_dashboard'}:
                student=await db.get_student(target)
                if data=='parent_link':
                    await pq.edit_message_text(f"👨‍👩‍👦 ولي الأمر: {student.get('parent_full_name') or student.get('parent_chat_id') or 'غير مربوط'}\nرمز الربط محفوظ؛ لا يرسل أثناء المعاينة.")
                elif data=='profile':
                    await pq.edit_message_text(f"👤 {student.get('full_name')}\n🏫 {student.get('school')}\n🎯 {student.get('target_grade')}\n⭐ XP: {student.get('xp',0)}\n⚠️ الإنذارات: {student.get('warnings',0)}\n📚 المسار: {student.get('study_track')} — الفصل {student.get('current_chapter')}")
                else:
                    await ns['v52_progress_overview'](pq)
                return
            if data=='today_prep':
                prep=await db.v52_current_preparation(target)
                if not prep:await pq.edit_message_text('لا توجد محاضرات مستحقة.');return
                ch=int(prep['chapter']);numbers=prep.get('pending_lectures') or []
                rows=[[B(f'المحاضرة {no}',callback_data=f'preview_lecture|{ch}|{no}',style='primary')] for no in numbers]
                await pq.edit_message_text(f"🎬 محاضراتي الحالية\nالفصل {ch} — المحاضرات {'+'.join(map(str,numbers))}",reply_markup=K(rows));return
            await pq.edit_message_text('👁 هذا الإجراء يغيّر حساب الطالب؛ استخدم حسابك الإداري لتنفيذه. المعاينة للعرض فقط.');return

        if data in {'self_delete','self_delete_confirm'}:
            if admin:await q.answer('لا يُحذف حساب الإدارة من واجهة الطالب.',show_alert=True);return
            student=await db.get_student(uid)
            if not student:return await previous_button(update,context)
            if data=='self_delete':
                context.user_data['self_delete_at']=datetime.now(ZoneInfo('UTC'))
                await q.answer();await q.edit_message_text('⚠️ حذف حسابي نهائياً\nسيُحذف تسجيلك وتقدمك وتسليماتك وXP وإنذاراتك وربط ولي الأمر من قاعدة البيانات.\nملفات الإجابات الموجودة في المجموعة تبقى فيها. هل تؤكد؟',reply_markup=K([[B('🗑 تأكيد حذف حسابي',callback_data='self_delete_confirm',style='danger')],[B('تراجع',callback_data='menu',style='primary')]]));return
            stamp=context.user_data.pop('self_delete_at',None)
            if not stamp or datetime.now(ZoneInfo('UTC'))-stamp>timedelta(minutes=10):await q.answer('أكد الحذف من جديد.',show_alert=True);return
            if not await erase_student(db,uid):await q.answer('الحساب غير موجود.');return
            clear_cache(uid);context.user_data.clear();await q.answer()
            await q.edit_message_text('✅ حُذف حسابك بالكامل. ابدأ التسجيل من جديد.',reply_markup=K([[B('🆕 البدء من جديد',callback_data='parent_restart',style='success')]]));return

        if data=='activation_resend':
            student=await db.get_student(uid)
            if not student or student.get('approved') or student.get('admin_blocked'):
                await q.answer('حسابك مفعل أو موقوف بقرار الإدارة.',show_alert=True);return
            await q.answer()
            result=await send_activation(context.bot,student)
            messages={'sent':'✅ أُعيد إرسال طلبك إلى الوجهات المتاحة. يظهر أيضاً في قائمة انتظار التفعيل.',
                      'cooldown':'⏳ طلبك موجود. يمكنك إعادة الإرسال بعد 5 دقائق من آخر إرسال.',
                      'failed':'⚠️ تعذر إرسال إشعار التفعيل. طلبك محفوظ في انتظار التفعيل. أبلغ الأستاذ ليتحقق من صلاحيات البوت ومعرف الكروب، ثم أعد المحاولة.'}
            await q.edit_message_text(messages[result['status']],reply_markup=pending_menu(student));return

        if data in {'personal_schedule','v37_personal_schedule','flex_schedule'}:
            context.user_data.pop('flex_days',None);await q.answer();await schedule(q,context);return
        if data.startswith('flex_day|'):
            try:day=int(data.split('|')[1])
            except ValueError:await q.answer('يوم غير صالح.');return
            if not 0<=day<=6:await q.answer('يوم غير صالح.');return
            student=await db.get_student(uid)
            days=context.user_data.setdefault('flex_days',list((student or {}).get('study_days') or [6,1,3]))
            if day in days:days.remove(day)
            else:days.append(day)
            await q.answer();await schedule(q,context);return
        if data=='flex_save':
            days=context.user_data.get('flex_days',[])
            if not days:await q.answer('اختر يوماً واحداً على الأقل.',show_alert=True);return
            result=await db.v41_set_study_days(uid,days)
            if result.get('status')!='ok':await q.answer('تعذر حفظ الجدول؛ راجع مسارك.',show_alert=True);return
            clear_cache(uid);await q.answer()
            plan=await db.v37_chapter_completion_plan(uid);finish=plan.get('full_finish')
            await q.edit_message_text(f"✅ تم تحديث جدولك والمحاضرات المستقبلية.\nموعد إكمال المنهج: {finish or 'أكملت المحاضرات المتاحة'}",reply_markup=K([[B('🗓 تعديل الجدول',callback_data='personal_schedule',style='primary')],[ns['back_menu']()]]));return

        if data=='change_study_track' or data.startswith(('v41_track_confirm|','v47_change_confirm|')):
            if admin:return await previous_button(update,context)
            context.user_data['track_clear_at']=datetime.now(ZoneInfo('UTC'))
            await q.answer();await q.edit_message_text('🔄 تغيير المسار من جديد\nيُحذف التقدم والامتحانات والتسليمات والمراجعات وXP والإنذارات القديمة.\nيبقى الاسم والمدرسة والمعدل وولي الأمر والتفعيل محفوظاً.\nبعد التأكيد تختار المسار وأول محاضرة.',reply_markup=K([[B('✅ تأكيد والبدء من جديد',callback_data='track_clear_confirm',style='danger')],[B('تراجع',callback_data='account_settings',style='primary')]]));return
        if data=='track_clear_confirm':
            stamp=context.user_data.pop('track_clear_at',None)
            if admin or not stamp or datetime.now(ZoneInfo('UTC'))-stamp>timedelta(minutes=10):await q.answer('أكد تغيير المسار من جديد.',show_alert=True);return
            if not await erase_student(db,uid,True):await q.answer('الحساب غير موجود.');return
            clear_cache(uid);context.user_data.clear();await q.answer()
            await q.edit_message_text('✅ حُذف سجل الدراسة السابق، وبقيت معلوماتك الأساسية وولي الأمر. اختر مسارك:',reply_markup=ns['onboarding_track_keyboard']());return
        return await previous_button(update,context)
    ns['button_handler']=button
