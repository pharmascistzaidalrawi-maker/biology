"""Shared subject menus, section visibility and historical-work preview."""

def install(ns,db,p):
    if p not in ('english','chemistry','islamic','biology'):raise ValueError('Invalid subject')
    legacy={'english':'eng6','chemistry':'chem6','islamic':'isl6','biology':'bio6'}[p]
    B,K=ns['InlineKeyboardButton'],ns['InlineKeyboardMarkup']
    async def sql(text,args=(),one=False):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(text.replace("subject_",p+"_"),args)
                return cur.fetchone() if one else cur.fetchall()
        return await db.run(op)
    def add_controls(markup,uid=None):
        rows=[list(r) for r in getattr(markup,'inline_keyboard',())]
        data=f'sections|student|{uid}' if uid else 'sections|list|0'
        if not any(b.callback_data==data for row in rows for b in row):
            rows.insert(0,[B('⚙️ تفعيل خانات هذا الطالب' if uid else '⚙️ خانات الطلاب المفعّلة',callback_data=data)])
        return K(rows)
    old_menu=ns['main_menu']
    def menu(admin=False):
        markup=old_menu(admin)
        if admin:return add_controls(markup)
        from student_sections import _VIEW,filter_keyboard
        # Filter after all installers have added their buttons, then pack by role.
        view=_VIEW.get()
        if view:markup=filter_keyboard(markup,view['flags'])
        buttons=[];seen=set()
        for row in markup.inline_keyboard:
            for button in row:
                key=button.callback_data or button.url or button.text
                if str(button.callback_data or '').startswith(('catchup|','previous|')) or key in seen:continue
                seen.add(key);buttons.append(button)
        from student_sections import callback_section
        rows=[]
        def take(predicate,width=2):
            group=[b for b in buttons if predicate(b)]
            buttons[:]=[b for b in buttons if not predicate(b)]
            rows.extend([group[i:i+width] for i in range(0,len(group),width)])
        take(lambda b:b.callback_data=='daily_learning_session',1)
        take(lambda b:b.callback_data in ('tasks|homework','exams_menu'))
        take(lambda b:b.callback_data in ('today_prep','playlists'))
        take(lambda b:b.callback_data in ('royal_review_menu','backlog_auto'))
        take(lambda b:b.callback_data in ('edu_plan','schedules_menu'))
        for section in ('lectures','homework','exams','school_review'):
            take(lambda b:callback_section(b.callback_data)==section)
        take(lambda b:b.callback_data in ('v51_progress','xp_store'))
        take(lambda b:b.callback_data not in ('notifications','account_settings'))
        take(lambda b:True)
        return K(rows)
    ns['main_menu']=menu
    async def chooser(q,page):
        students=await sql('SELECT user_id,full_name FROM subject_students ORDER BY full_name,user_id;')
        page=min(max(0,page),max(0,(len(students)-1)//12))
        keyboard=[[B(f"⚙️ {r['full_name']} — {r['user_id']}",callback_data=f"sections|student|{r['user_id']}")] for r in students[12*page:12*(page+1)]]
        nav=[]
        if page:nav.append(B('السابق',callback_data=f'sections|list|{page-1}'))
        if (page+1)*12<len(students):nav.append(B('التالي',callback_data=f'sections|list|{page+1}'))
        if nav:keyboard.append(nav)
        keyboard.append([B('إدارة الطلاب',callback_data='admin_students'),ns['back_menu']()])
        await q.answer();await q.edit_message_text(ns['bold']('⚙️ تفعيل أقسام الطلاب\nاختر الطالب لتفعيل أو تعطيل المحاضرات والواجبات والامتحانات ومراجعة المدرسة.'+('' if students else '\nلا توجد حسابات مسجلة بعد.')),parse_mode='HTML',reply_markup=K(keyboard))
    async def task_list(q,context,kind,page=0,previous_only=False):
        student=await db.get_student(q.from_user.id)
        if not student or not student.get('approved') or student.get('reset_pending') or student.get('admin_blocked'):return False
        section='homework' if kind=='homework' else 'exams'
        if not await db.section_enabled(q.from_user.id,section):
            await q.answer('هذه الخانة معطّلة لحسابك من الإدارة.',show_alert=True);return True
        if student.get('study_track')=='course':await db.sync_course_catchup(q.from_user.id)
        current=await (db.v28_student_homeworks(q.from_user.id) if kind=='homework' else db.v28_student_exam_tasks(q.from_user.id))
        previous=await sql('''SELECT t.*,sub.grade,sub.submitted_at FROM subject_tasks t
            LEFT JOIN subject_submissions sub ON sub.task_id=t.id AND sub.user_id=%s
            WHERE t.catchup_owner=%s AND t.catchup_source_id IS NOT NULL AND t.kind=%s
            ORDER BY t.catchup_source_id;''',(q.from_user.id,q.from_user.id,kind))
        current=[t for t in current if not t.get('catchup_source_id')]
        data=[(t,True) for t in previous] if previous_only else [(t,False) for t in current]
        page=min(max(0,page),max(0,(len(data)-1)//12))
        keyboard=[]
        for t,practice in data[page*12:(page+1)*12]:
            label=('✅' if t.get('submitted_at') else '📖')+' سابق بلا إنذار' if practice else '📤 مطلوب حالي'
            keyboard.append([B(f"{label} — {t['title']}",callback_data=f"task|{t['id']}")])
        nav=[]
        route='previous' if previous_only else 'coursework'
        if page:nav.append(B('السابق',callback_data=f'{route}|{kind}|{page-1}'))
        if (page+1)*12<len(data):nav.append(B('التالي',callback_data=f'{route}|{kind}|{page+1}'))
        if nav:keyboard.append(nav)
        keyboard.append([B('◀️ '+('الواجبات' if kind=='homework' else 'الامتحانات'),callback_data='tasks|homework' if kind=='homework' else 'exams_menu'),ns['back_menu']()])
        noun='الواجبات' if kind=='homework' else 'الامتحانات'
        text=f'📚 {noun} الحالية\nعدد العناصر: {len(current)}\nتخضع المطلوبات الحالية لمواعيدها المعتادة.'
        if previous_only:text=f'📚 {noun} السابقة لتسجيلك\nعدد العناصر: {len(previous)}\nمتاحة للحل مع XP مرة واحدة، بلا إنذارات.'
        if not data:text+='\nلا توجد عناصر متاحة حالياً.'
        await q.answer();await q.edit_message_text(ns['bold'](text),parse_mode='HTML',reply_markup=K(keyboard));return True
    class QueryControls:
        def __init__(self,q,uid=None):self.q=q;self.uid=uid
        def __getattr__(self,name):return getattr(self.q,name)
        async def edit_message_text(self,text,*args,**kw):
            kw['reply_markup']=add_controls(kw.get('reply_markup'),self.uid)
            return await self.q.edit_message_text(text,*args,**kw)
    class UpdateControls:
        def __init__(self,update,q):self.update=update;self.callback_query=q
        def __getattr__(self,name):return getattr(self.update,name)
    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query;data=str(q.data or '');admin=ns['is_admin'](q.from_user.id)
        if admin and data=='preview_exit':
            context.user_data.pop('preview_student',None)
            await q.answer();await q.edit_message_text(ns['bold']('لوحة الإدارة'),parse_mode='HTML',reply_markup=ns['main_menu'](True));return
        if admin and data.startswith('preview_student|'):
            try:target=int(data.split('|')[1])
            except (ValueError,IndexError):
                await q.answer('معرّف الطالب غير صالح.',show_alert=True);return
            if not await db.get_student(target):
                await q.answer('الطالب غير موجود.',show_alert=True);return
            context.user_data['preview_student']=target
        target=context.user_data.get('preview_student') if admin else None
        if target and (data in ('menu','preview_home') or data.startswith('preview_student|')):
            student=await db.get_student(int(target))
            if not student:
                await q.answer('الطالب غير موجود.',show_alert=True);return
            from student_experience import PreviewQuery
            await q.answer()
            await PreviewQuery(q,int(target),ns).edit_message_text(ns['bold']('👁 معاينة واجهة الطالب: '+student['full_name']),parse_mode='HTML',reply_markup=ns['main_menu'](False));return
        if data.startswith('sections|list|') or data==legacy+'|choose':
            if not admin or context.user_data.get('preview_student'):
                await q.answer('للإدارة فقط؛ اخرج من المعاينة أولاً.',show_alert=True);return
            try:page=int(data.rsplit('|',1)[1]) if data.startswith('sections|list|') else 0
            except ValueError:page=0
            await chooser(q,page);return
        if data.startswith(legacy+'|student|'):
            # Keep bookmarks and older permission menus on the canonical panel.
            class AliasQuery:
                def __getattr__(self,k):return getattr(q,k)
                data='sections|student|'+str(q.data).split('|')[2]
            return await old_button(UpdateControls(update,AliasQuery()),context)
        preview=context.user_data.get('preview_student') if admin else None
        if preview and data.startswith('task|'):
            try:tid=int(data.split('|')[1])
            except (ValueError,IndexError):
                await q.answer('رقم المهمة غير صالح.',show_alert=True);return
            task=await db.get_task(tid)
            if task and task.get('catchup_source_id'):
                uid=int(preview)
                section='homework' if task['kind']=='homework' else 'exams'
                student=await db.get_student(uid)
                if not student or not student.get('approved') or student.get('reset_pending') or student.get('admin_blocked') or task.get('catchup_owner')!=uid or not await db.section_enabled(uid,section):
                    await q.answer('هذا العنصر غير متاح للطالبة المحددة في المعاينة.',show_alert=True);return
                submission=await sql('SELECT grade,submitted_at FROM subject_submissions WHERE task_id=%s AND user_id=%s;',(tid,uid),one=True)
                await q.answer()
                media=await db.get_task_media(tid)
                if not media and task.get('file_id'):media=[task]
                caption=ns['bold'](('👁 معاينة للإدارة — '+task['title'])[:900])
                media_failed=False
                for i,item in enumerate(media):
                    send={'photo':context.bot.send_photo,'document':context.bot.send_document,'video':context.bot.send_video}.get(item['payload_type'])
                    if send:
                        try:
                            await send(q.from_user.id,item['file_id'],caption=caption if i==0 else None,parse_mode='HTML' if i==0 else None,protect_content=True)
                        except ns['TelegramError']:
                            ns['logger'].exception('Preview media failed: student=%s task=%s',uid,tid)
                            media_failed=True
                text='👁 معاينة '+task['title']+'\nمطلوب سابق بلا إنذارات.\n'
                if submission and submission.get('submitted_at'):
                    text+='✅ تم التسليم.\n'+(f"الدرجة: {submission['grade']}/100" if submission.get('grade') is not None else 'بانتظار التصحيح.')
                else:text+='📤 لم يتم التسليم بعد.'
                if task.get('text_content') and task['text_content']!=task['title']:text+='\n\n'+task['text_content']
                if media_failed:text+='\n⚠️ تعذّر عرض بعض مرفقات المهمة؛ راجع سجل البوت.'
                text=text[:3500]+'\n\nهذه معاينة للإدارة؛ إرسال الحل متاح من حساب الطالب.'
                from student_experience import PreviewQuery
                await PreviewQuery(q,uid,ns).edit_message_text(ns['bold'](text),parse_mode='HTML',reply_markup=K([[B('العودة للسابق',callback_data=f"previous|{task['kind']}|0")]]));return
        if (not admin or preview) and data in ('tasks|homework','exams_menu','current_exams_menu'):
            uid=int(preview or q.from_user.id)
            student=await db.get_student(uid)
            kind='homework' if data=='tasks|homework' else 'exam'
            section='homework' if kind=='homework' else 'exams'
            if not student or not student.get('approved') or student.get('reset_pending') or student.get('admin_blocked'):
                await q.answer('أكمل تفعيل حسابك أولاً.',show_alert=True);return
            if not await db.section_enabled(uid,section):
                await q.answer('هذه الخانة معطّلة لحسابك من الإدارة.',show_alert=True);return
            task_query=q
            if preview:
                from student_experience import PreviewQuery
                task_query=PreviewQuery(q,uid,ns)
            await q.answer()
            if data=='current_exams_menu':
                return await ns['v51_exams_menu'](task_query)
            noun='الواجبات' if kind=='homework' else 'الامتحانات'
            current_route='coursework|homework|0' if kind=='homework' else 'current_exams_menu'
            rows=[[B('📤 '+noun,callback_data=current_route,style='primary')],
                  [B('📚 '+noun+' السابقة',callback_data=f'previous|{kind}|0',style='primary')],
                  [ns['back_menu']()]]
            await task_query.edit_message_text(ns['bold']('📚 '+noun+'\nاختر المطلوبات الحالية أو السابقة لتسجيلك.'),parse_mode='HTML',reply_markup=K(rows));return
        if (not admin or preview) and data.startswith('catchup|'):
            # Old messages keep working after the combined button is replaced.
            uid=int(preview or q.from_user.id)
            student=await db.get_student(uid)
            if not student or not student.get('approved') or student.get('reset_pending') or student.get('admin_blocked'):
                await q.answer('هذه القائمة متاحة للحسابات المفعّلة.',show_alert=True);return
            flags=await db.section_flags(uid)
            rows=[]
            if flags['homework']:rows.append([B('📚 الواجبات السابقة',callback_data='previous|homework|0')])
            if flags['exams']:rows.append([B('📝 الامتحانات السابقة',callback_data='previous|exam|0')])
            text='📚 المطلوبات السابقة\nاختر القسم لعرض المطلوبات السابقة لتسجيلك، مع XP بلا إنذارات.'
            if not rows:text+='\nالواجبات والامتحانات معطّلة لحسابك حالياً.'
            rows.append([ns['back_menu']()])
            await q.answer();await q.edit_message_text(ns['bold'](text),parse_mode='HTML',reply_markup=K(rows));return
        if (not admin or preview) and data.startswith(('coursework|','previous|')):
            parts=data.split('|');kind='homework' if data=='tasks|homework' else parts[1]
            if kind not in ('homework','exam'):return
            try:page=int(parts[2]) if len(parts)>2 else 0
            except ValueError:page=0
            task_query=q
            if preview:
                from student_experience import PreviewQuery
                task_query=PreviewQuery(q,int(preview),ns)
            if await task_list(task_query,context,kind,page,previous_only=data.startswith('previous|')):return
        if admin and not context.user_data.get('preview_student'):
            uid=None
            if data.startswith('admin_student|'):
                try:uid=int(data.split('|')[1])
                except ValueError:pass
            elif data.startswith(('admin_access|','admin_access_confirm|','approve|')):
                try:uid=int(data.rsplit('|',1)[1])
                except ValueError:pass
            if uid or data in ('admin_students','v8|course','v7|students') or data.startswith('v51_students|'):
                return await old_button(UpdateControls(update,QueryControls(q,uid)),context)
        return await old_button(update,context)
    async def scoped_button(update,context):
        from student_sections import _VIEW,callback_section
        q=update.callback_query;uid=q.from_user.id
        preview=context.user_data.get('preview_student') if ns['is_admin'](uid) else None
        if ns['is_admin'](uid) and str(q.data or '').startswith('preview_student|'):
            try:preview=int(q.data.split('|')[1])
            except (ValueError,IndexError):preview=None
        if str(q.data or '')=='preview_exit':preview=None
        view=None
        if not ns['is_admin'](uid) or preview:
            target=int(preview or uid)
            view=dict(flags=await db.section_flags(target),uid=target,recipients=(uid,target))
        token=_VIEW.set(view)
        try:
            field=callback_section(str(q.data or ''))
            if view and field and not view['flags'].get(field,True):
                await q.answer('هذه الخانة معطّلة لحسابك من الإدارة.',show_alert=True);return
            return await button(update,context)
        finally:_VIEW.reset(token)
    ns['button_handler']=scoped_button
