"""Biology weekly exams, editable school reviews and private guardian messaging."""
import json
import time
from datetime import datetime, date, time as clock, timedelta
from zoneinfo import ZoneInfo

ZONE=ZoneInfo('Asia/Baghdad')

def thursday(day):
    return day+timedelta(days=(3-day.weekday())%7)

def week_start(day):
    return day-timedelta(days=(day.weekday()+1)%7)

def exam_window(day):
    if day.weekday()!=3:raise ValueError('اختر تاريخ يوم خميس.')
    start=datetime.combine(day,clock(),ZONE)
    return start,start+timedelta(days=1)

def parse_lectures(text,catalog):
    value=text.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩،؛','0123456789,;'))
    pairs=set()
    for part in value.split(';'):
        ch,nums=part.strip().split(':');ch=int(ch)
        valid={int(item[0]) for item in catalog.get(ch,[])}
        for number in nums.replace('+',',').split(','):
            no=int(number.strip())
            if no not in valid:raise ValueError('المحاضرة غير موجودة.')
            pairs.add((ch,no))
    if not pairs:raise ValueError('حدد المحاضرات.')
    return [list(pair) for pair in sorted(pairs)]

def lecture_text(pairs):
    chapters={}
    for ch,no in pairs or []:chapters.setdefault(int(ch),set()).add(int(no))
    return '\n'.join(f"الفصل {ch}: المحاضرات "+'+'.join(map(str,sorted(nums))) for ch,nums in sorted(chapters.items())) or 'لم تحدد المحاضرات بعد.'

def announcement(row):
    return (f"✦ {row['title']} ✦\n━━━━━━━━━━━━━━━━━━\n"
            f"🔸 امتحان يوم الخميس | {row['exam_date']:%d/%m/%Y}\n\n"
            f"{lecture_text(row['lectures'])}\n\n{row.get('topics') or ''}\n"
            '⏳ الامتحانات متاحة يوم الخميس من 00:00 حتى نهاية اليوم بتوقيت بغداد.\n━━━━━━━━━━━━━━━━━━')

async def sql(db,query,args=(),one=False,write=False):
    def op():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute(query,args)
            result=(cur.fetchone() if one else cur.fetchall()) if cur.description else None
            if write:conn.commit()
            return result
    return await db.run(op)

def migrate(db):
    with db.connect() as conn,conn.cursor() as cur:
        cur.execute('''ALTER TABLE biology_school_reviews
            ADD COLUMN IF NOT EXISTS selected_lectures JSONB NOT NULL DEFAULT '[]',
            ADD COLUMN IF NOT EXISTS content_revision INTEGER NOT NULL DEFAULT 1;
        CREATE TABLE IF NOT EXISTS biology_weekly_plans(
            id SERIAL PRIMARY KEY,exam_date DATE NOT NULL UNIQUE CHECK(EXTRACT(ISODOW FROM exam_date)=4),
            title TEXT NOT NULL DEFAULT 'امتحانات هذا الأسبوع',topics TEXT NOT NULL DEFAULT '',
            lectures JSONB NOT NULL DEFAULT '[]',published BOOLEAN NOT NULL DEFAULT FALSE,
            revision INTEGER NOT NULL DEFAULT 1,created_by BIGINT NOT NULL);
        CREATE TABLE IF NOT EXISTS biology_weekly_exam_links(
            definition_id INTEGER PRIMARY KEY REFERENCES biology_linked_exam_definitions(id) ON DELETE CASCADE,
            plan_id INTEGER NOT NULL REFERENCES biology_weekly_plans(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS biology_guardian_threads(
            id BIGSERIAL PRIMARY KEY,student_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            parent_id BIGINT NOT NULL,admin_id BIGINT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id,parent_id,admin_id));
        CREATE TABLE IF NOT EXISTS biology_guardian_messages(
            id BIGSERIAL PRIMARY KEY,thread_id BIGINT NOT NULL REFERENCES biology_guardian_threads(id) ON DELETE CASCADE,
            sender_id BIGINT NOT NULL,source_message_id BIGINT NOT NULL,body TEXT NOT NULL,
            sent BOOLEAN NOT NULL DEFAULT FALSE,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(thread_id,sender_id,source_message_id));
        CREATE INDEX IF NOT EXISTS biology_guardian_thread_history ON biology_guardian_messages(thread_id,id DESC);
        CREATE TABLE IF NOT EXISTS biology_education_deliveries(
            event_key TEXT NOT NULL,user_id BIGINT NOT NULL REFERENCES biology_students(user_id) ON DELETE CASCADE,
            sent BOOLEAN NOT NULL DEFAULT FALSE,lease_until TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(event_key,user_id));''')
        conn.commit()

async def school_edit(db,rid,field,value,actor):
    allowed={'week_label','chapter_label','topics','selected_lectures'}
    if field not in allowed:raise ValueError('حقل غير صالح')
    column='selected_lectures' if field=='selected_lectures' else field
    stored=json.dumps(value) if field=='selected_lectures' else value
    cast='::jsonb' if field=='selected_lectures' else ''
    def op():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute(f'UPDATE biology_school_reviews SET {column}=%s{cast},content_revision=content_revision+1 WHERE id=%s RETURNING *;',(stored,rid))
            row=cur.fetchone()
            if not row:return None
            cur.execute('UPDATE biology_tasks SET title=%s WHERE school_review_id=%s;',('مراجعة المدرسة — '+row['week_label']+' — '+row['chapter_label'],rid))
            cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',(actor,'school_content_edit',f'review={rid};field={field}'))
            conn.commit();return row
    return await db.run(op)

async def save_week(db,pid,field,value,actor):
    if field not in {'title','topics','lectures'}:raise ValueError('حقل غير صالح')
    return await sql(db,f"UPDATE biology_weekly_plans SET {field}=%s"+('::jsonb' if field=='lectures' else '')+",published=FALSE,revision=revision+1 WHERE id=%s RETURNING *;",
        (json.dumps(value) if field=='lectures' else value,pid),True,True)

async def publish_week(db,pid,actor):
    def op():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute('SELECT * FROM biology_weekly_plans WHERE id=%s FOR UPDATE;',(pid,));row=cur.fetchone()
            if not row or not row['lectures']:raise ValueError('حدد المحاضرات قبل النشر.')
            start,end=exam_window(row['exam_date'])
            if end<=datetime.now(ZONE):raise ValueError('انتهى هذا الخميس. اختر أسبوعاً قادماً.')
            cur.execute('SELECT definition_id FROM biology_weekly_exam_links WHERE plan_id=%s;',(pid,));ids=[r['definition_id'] for r in cur.fetchall()]
            covered={tuple(x) for x in row['lectures']}
            for did in ids:
                required=set(db._v52_required_lectures(cur,did))
                if not required or not required<=covered:raise ValueError('محاضرات أحد الامتحانات المرتبطة غير موجودة في التبليغ. عدل المحاضرات أو افصل الامتحان.')
                cur.execute('''SELECT 1 FROM biology_tasks WHERE exam_definition_id=%s
                    AND (exam_available_at<=CURRENT_TIMESTAMP OR EXISTS(SELECT 1 FROM biology_submissions WHERE task_id=biology_tasks.id)) LIMIT 1;''',(did,))
                if cur.fetchone():raise ValueError('أحد الامتحانات بدأ بالفعل. لا يمكن نقل موعده من هنا.')
            for did in ids:
                cur.execute('UPDATE biology_linked_exam_definitions SET manual_publish_at=%s,manual_deadline=%s WHERE id=%s;',(start,end,did))
                cur.execute('UPDATE biology_tasks SET exam_available_at=%s,deadline=%s WHERE exam_definition_id=%s;',(start,end,did))
            cur.execute('UPDATE biology_weekly_plans SET published=TRUE WHERE id=%s;',(pid,))
            cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',(actor,'weekly_plan_published',str(pid)))
            conn.commit();return row
    return await db.run(op)

async def thread_access(db,tid,uid,is_admin):
    row=await sql(db,'''SELECT t.*,s.full_name FROM biology_guardian_threads t
        JOIN biology_students s ON s.user_id=t.student_id
        JOIN biology_parent_links p ON p.student_id=t.student_id AND p.parent_chat_id=t.parent_id AND p.approved=TRUE
        WHERE t.id=%s;''',(tid,),True)
    if not row:return None
    if uid==row['admin_id'] and is_admin(uid):return row
    if uid==row['parent_id'] and is_admin(row['admin_id']):return row
    return None

class MarkupProxy:
    def __init__(self,q,rows,K):self.q=q;self.rows=rows;self.K=K
    def __getattr__(self,key):return getattr(self.q,key)
    async def edit_message_text(self,text,*args,**kw):
        markup=kw.get('reply_markup');rows=[list(r) for r in markup.inline_keyboard] if markup else []
        kw['reply_markup']=self.K(self.rows+rows)
        return await self.q.edit_message_text(text,*args,**kw)

class UpdateProxy:
    def __init__(self,update,q):self.update=update;self.callback_query=q
    def __getattr__(self,key):return getattr(self.update,key)

def install(ns,db):
    B=ns['InlineKeyboardButton'];K=ns['InlineKeyboardMarkup'];END=ns['ConversationHandler'].END
    def kb(rows):return K(rows+[[ns['back_menu']()]])
    old_init=db.init_db
    def init_db():old_init();migrate(db)
    db.init_db=init_db;ns['init_db']=init_db
    old_window=db._v48_course_window
    def course_window(cur,d):
        result=old_window(cur,d)
        if not d or d.get('target_scope')!='course' or result.get('status')!='ready':return result
        # Existing attempts retain their saved deadlines. Newly scheduled course exams use Thursday.
        saved=d.get('actual_publish_at') or d.get('manual_publish_at')
        if saved and saved<=datetime.now(ZONE):return result
        start,end=exam_window(thursday(result['publish_at'].astimezone(ZONE).date()))
        if d.get('manual_publish_at'):
            cur.execute('UPDATE biology_linked_exam_definitions SET manual_publish_at=%s,manual_deadline=%s WHERE id=%s;',(start,end,d['id']))
        else:
            cur.execute('UPDATE biology_linked_exam_definitions SET actual_publish_at=%s,actual_deadline=%s WHERE id=%s;',(start,end,d['id']))
        return {'status':'ready','publish_at':start,'deadline':end}
    db._v48_course_window=course_window

    old_menu=ns['main_menu']
    def main_menu(admin=False):
        rows=[list(r) for r in old_menu(admin).inline_keyboard]
        if admin:rows.insert(1,[B('📅 إدارة امتحانات الخميس',callback_data='edu_weeks',style='primary')])
        else:rows.insert(0,[B('📚 خطتي الدراسية',callback_data='edu_plan',style='success')])
        return K(rows)
    ns['main_menu']=main_menu
    old_exams=ns['v51_exams_menu']
    async def exams(q):return await old_exams(MarkupProxy(q,[[B('📅 امتحانات هذا الأسبوع',callback_data='edu_weekly',style='primary')]],K))
    ns['v51_exams_menu']=exams
    old_school_text=ns['_v54_school_review_text']
    def school_text(row,prefix='🏫 مراجعة للمدرسة'):
        text=old_school_text(row,prefix)
        if row.get('selected_lectures'):text+='\n'+lecture_text(row['selected_lectures'])
        return text
    ns['_v54_school_review_text']=school_text
    old_school_admin=ns['v54_school_admin_review']
    async def school_admin(q,rid):
        rows=[[B('✏️ اسم الأسبوع',callback_data=f'edu_school|{rid}|week_label'),B('✏️ الفصول الداخلة',callback_data=f'edu_school|{rid}|chapter_label')],
              [B('✏️ تعديل الموضوعات',callback_data=f'edu_school|{rid}|topics'),B('🎬 تعديل المحاضرات',callback_data=f'edu_school|{rid}|selected_lectures')]]
        return await old_school_admin(MarkupProxy(q,rows,K),rid)
    ns['v54_school_admin_review']=school_admin

    async def week_panel(q,pid):
        row=await sql(db,'SELECT * FROM biology_weekly_plans WHERE id=%s;',(pid,),True)
        if not row:await q.edit_message_text('الأسبوع غير موجود.');return
        linked=await sql(db,'SELECT definition_id FROM biology_weekly_exam_links WHERE plan_id=%s;',(pid,))
        rows=[[B('✏️ العنوان',callback_data=f'edu_weekfield|{pid}|title'),B('✏️ الموضوعات',callback_data=f'edu_weekfield|{pid}|topics')],
              [B('🎬 تحديد المحاضرات',callback_data=f'edu_weekfield|{pid}|lectures')],
              [B(f'📝 ربط الامتحانات ({len(linked)})',callback_data=f'edu_linkmenu|{pid}|0')],
              [B('📢 نشر التبليغ وحفظ موعد الخميس',callback_data=f'edu_publish|{pid}',style='success')],
              [B('🗑 سحب التبليغ',callback_data=f'edu_unpublish|{pid}',style='danger')]]
        await q.edit_message_text(announcement(row)+('\n✅ منشور' if row['published'] else '\n✏️ مسودة'),reply_markup=kb(rows))

    async def current_week(uid):
        student=await db.get_student(uid)
        if not student or not student.get('approved') or student.get('admin_blocked') or student.get('reset_pending'):return []
        if student.get('study_track')!='course':return []
        today=datetime.now(ZONE).date();start=week_start(today)
        return await sql(db,'SELECT * FROM biology_weekly_plans WHERE published=TRUE AND exam_date BETWEEN %s AND %s ORDER BY exam_date;',(start,start+timedelta(days=6)))

    old_daily=ns['v51_daily_tasks_menu']
    async def daily(q):
        if hasattr(db,'section_enabled') and not await db.section_enabled(q.from_user.id,'lectures'):
            return await old_daily(q)
        rows=await current_week(q.from_user.id)
        progress=await db.v52_progress_overview(q.from_user.id)
        if not progress.get('student'):return await old_daily(q)
        completed=progress.get('completed') or {};total=sum(len(v) for v in ns['PLAYLISTS'].values())
        done=sum(len({int(x) for x in completed.get(ch,[])} & {int(v[0]) for v in vals}) for ch,vals in ns['PLAYLISTS'].items())
        footer=f'\n\n📊 إنجاز المنهج: {round(done*100/total) if total else 0}% — المتبقي {max(0,total-done)} محاضرة.'
        if progress.get('finish_date'):footer+=f"\n🏁 الإكمال المتوقع: {progress['finish_date']:%d/%m/%Y}"
        if rows:footer+='\n\n📅 امتحان الخميس القادم:\n'+lecture_text(rows[0]['lectures'])
        footer+='\n\n💪 ابدأ بالمحاضرة الحالية، ثم امتحانها والواجب، ثم المراجعة. كل خطوة مكتملة تقرّبك من هدفك.'
        class DailyProxy(MarkupProxy):
            async def edit_message_text(self,text,*args,**kwargs):
                # Existing renderer uses HTML; escape the added dynamic footer using the same formatter.
                return await super().edit_message_text(text+ns['bold'](footer),*args,**kwargs)
        return await old_daily(DailyProxy(q,[[B('📅 امتحانات هذا الأسبوع',callback_data='edu_weekly',style='primary')]],K))
    ns['v51_daily_tasks_menu']=daily

    async def compose_thread(q,context,tid):
        row=await thread_access(db,tid,q.from_user.id,ns['is_admin'])
        if not row:await q.answer('لا تملك صلاحية هذه المحادثة أو أُلغي ربط ولي الأمر.',show_alert=True);return
        context.user_data['edu_input']={'kind':'message','id':tid,'at':time.monotonic()}
        await q.answer();await q.edit_message_text(f"✉️ محادثة بخصوص الطالب: {row['full_name']}\nأرسل نصاً أو صورة أو ملفاً. سيصل الرد للطرف الآخر عبر البوت.",reply_markup=kb([[B('📜 آخر الرسائل',callback_data=f'edu_history|{tid}')],[B('إلغاء الكتابة',callback_data='edu_cancel')]]))

    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query;data=str(q.data or '');uid=q.from_user.id;admin=ns['is_admin'](uid)
        if context.user_data.get('preview_student') and admin:
            if data=='edu_plan':
                from student_experience import PreviewQuery
                return await daily(PreviewQuery(q,int(context.user_data['preview_student']),ns))
            if data=='edu_weekly':
                from student_experience import PreviewQuery
                rows=await current_week(int(context.user_data['preview_student']))
                await q.answer();return await PreviewQuery(q,int(context.user_data['preview_student']),ns).edit_message_text('\n\n'.join(announcement(r) for r in rows) or 'لا يوجد تبليغ منشور لهذا الأسبوع.')
            return await old_button(update,context)
        if not data.startswith('edu_'):context.user_data.pop('edu_input',None)
        if data.startswith('admin_student|') and admin:
            sid=int(data.split('|')[1]);proxy=MarkupProxy(q,[[B('📞 مراسلة ولي الأمر',callback_data=f'edu_parents|{sid}',style='primary')]],K)
            return await old_button(UpdateProxy(update,proxy),context)
        if not data.startswith('edu_'):return await old_button(update,context)
        if data=='edu_cancel':context.user_data.pop('edu_input',None);await q.answer();await q.edit_message_text('تم إلغاء الكتابة.',reply_markup=ns['main_menu'](admin));return
        if data.startswith(('edu_reply|','edu_history|')):
            tid=int(data.split('|')[1]);row=await thread_access(db,tid,uid,ns['is_admin'])
            if not row:await q.answer('المحادثة غير متاحة لهذا الحساب.',show_alert=True);return
            if data.startswith('edu_reply|'):return await compose_thread(q,context,tid)
            messages=await sql(db,'SELECT sender_id,body,sent,created_at FROM biology_guardian_messages WHERE thread_id=%s ORDER BY id DESC LIMIT 20;',(tid,))
            lines=[f"📜 محادثة الطالب: {row['full_name']}"]
            for m in reversed(messages):lines.append(('👨‍🏫 الإدارة: ' if m['sender_id']==row['admin_id'] else '👨‍👩‍👦 ولي الأمر: ')+m['body'][:130]+('' if m['sent'] else ' [لم يرسل]'))
            await q.answer();await q.edit_message_text('\n\n'.join(lines)[:3800],reply_markup=kb([[B('✉️ كتابة رسالة',callback_data=f'edu_reply|{tid}')]]));return
        if data=='edu_plan':await q.answer();return await daily(q)
        if data=='edu_weekly':
            rows=await current_week(uid)
            await q.answer();await q.edit_message_text('\n\n'.join(announcement(r) for r in rows) or 'لا يوجد تبليغ منشور لهذا الأسبوع في مسارك حالياً.',reply_markup=kb([[B('📝 الامتحانات',callback_data='exams_menu')]]));return
        if not admin:await q.answer('للإدارة فقط.',show_alert=True);return
        if data.startswith('edu_parents|'):
            sid=int(data.split('|')[1]);parents=await db.student_parents(sid,True)
            rows=[[B('✉️ '+str(p.get('parent_full_name') or p['parent_chat_id']),callback_data=f"edu_thread|{sid}|{p['parent_chat_id']}")] for p in parents]
            await q.answer();await q.edit_message_text('اختر ولي الأمر:' if rows else 'لا يوجد ولي أمر مرتبط بهذا الطالب.',reply_markup=kb(rows));return
        if data.startswith('edu_thread|'):
            _,sid,pid=data.split('|');sid=int(sid);pid=int(pid)
            if pid not in {int(p['parent_chat_id']) for p in await db.student_parents(sid,True)}:await q.answer('ولي الأمر لم يعد مرتبطاً.');return
            row=await sql(db,'''INSERT INTO biology_guardian_threads(student_id,parent_id,admin_id) VALUES(%s,%s,%s)
                ON CONFLICT(student_id,parent_id,admin_id) DO UPDATE SET admin_id=EXCLUDED.admin_id RETURNING id;''',(sid,pid,uid),True,True)
            return await compose_thread(q,context,row['id'])
        if data.startswith(('edu_school|','edu_weekfield|')):
            action,rid,field=data.split('|');rid=int(rid)
            allowed={'week_label','chapter_label','topics','selected_lectures'} if action=='edu_school' else {'title','topics','lectures'}
            if field not in allowed:await q.answer('حقل غير صالح.');return
            context.user_data['edu_input']={'kind':'school' if action=='edu_school' else 'week','id':rid,'field':field,'at':time.monotonic()}
            hint='أرسل المحاضرات بهذه الصيغة: 4:1,2,3;5:1\nأي الفصل الرابع محاضرات 1+2+3 والخامس محاضرة 1.' if field in {'lectures','selected_lectures'} else 'أرسل النص الجديد كاملاً (سيستبدل النص القديم):'
            await q.answer();await q.edit_message_text(hint,reply_markup=kb([[B('إلغاء',callback_data='edu_cancel')]]));return
        if data=='edu_weeks':
            today=datetime.now(ZONE).date();day=thursday(today)
            rows=[[B(f'📅 الخميس {d:%d/%m/%Y}',callback_data=f'edu_newweek|{d.isoformat()}')] for d in [day,day+timedelta(days=7),day+timedelta(days=14)]]
            existing=await sql(db,'SELECT id,title,exam_date FROM biology_weekly_plans ORDER BY exam_date DESC LIMIT 12;')
            rows.extend([[B(f"✏️ {r['title']} — {r['exam_date']:%d/%m}",callback_data=f"edu_week|{r['id']}")] for r in existing])
            await q.answer();await q.edit_message_text('📅 إدارة امتحانات الخميس\nاختر الأسبوع، ثم حدد المحاضرات واربط الامتحانات وانشر التبليغ.',reply_markup=kb(rows));return
        if data.startswith('edu_newweek|'):
            day=date.fromisoformat(data.split('|')[1]);exam_window(day)
            row=await sql(db,'''INSERT INTO biology_weekly_plans(exam_date,created_by) VALUES(%s,%s)
                ON CONFLICT(exam_date) DO UPDATE SET exam_date=EXCLUDED.exam_date RETURNING id;''',(day,uid),True,True)
            await q.answer();return await week_panel(q,row['id'])
        if data.startswith('edu_week|'):await q.answer();return await week_panel(q,int(data.split('|')[1]))
        if data.startswith('edu_publish|'):
            pid=int(data.split('|')[1])
            try:await publish_week(db,pid,uid)
            except ValueError as e:await q.answer(str(e),show_alert=True);return
            await q.answer('تم النشر. الإشعار يرسل بداية الأسبوع أو في فحص الإرسال القادم.',show_alert=True);return await week_panel(q,pid)
        if data.startswith('edu_unpublish|'):
            pid=int(data.split('|')[1]);await sql(db,'UPDATE biology_weekly_plans SET published=FALSE WHERE id=%s;',(pid,),write=True)
            await q.answer('تم سحب التبليغ. مواعيد الامتحانات المحفوظة تبقى حتى تعديلها.');return await week_panel(q,pid)
        if data.startswith('edu_linkmenu|'):
            _,pid,page=data.split('|');pid=int(pid);page=max(0,int(page))
            definitions=await sql(db,"SELECT id,title FROM biology_linked_exam_definitions WHERE deleted_at IS NULL AND target_scope='course' ORDER BY id DESC LIMIT 9 OFFSET %s;",(page*8,))
            linked={r['definition_id'] for r in await sql(db,'SELECT definition_id FROM biology_weekly_exam_links WHERE plan_id=%s;',(pid,))}
            rows=[[B(('✅ ' if r['id'] in linked else '▫️ ')+r['title'][:40],callback_data=f"edu_link|{pid}|{r['id']}|{page}")] for r in definitions[:8]]
            if page:rows.append([B('السابق',callback_data=f'edu_linkmenu|{pid}|{page-1}')])
            if len(definitions)>8:rows.append([B('التالي',callback_data=f'edu_linkmenu|{pid}|{page+1}')])
            rows.append([B('العودة للأسبوع',callback_data=f'edu_week|{pid}')])
            await q.answer();await q.edit_message_text('اختر امتحانات الدورة المنشأة مسبقاً. المحاضرات الداخلة يجب أن تكون ضمن التبليغ. بعد الاختيار انشر الأسبوع لحفظ الموعد.',reply_markup=kb(rows));return
        if data.startswith('edu_link|'):
            _,pid,did,page=data.split('|');pid=int(pid);did=int(did)
            def toggle():
                with db.connect() as conn,conn.cursor() as cur:
                    cur.execute("SELECT id FROM biology_linked_exam_definitions WHERE id=%s AND deleted_at IS NULL AND target_scope='course' FOR UPDATE;",(did,))
                    if not cur.fetchone():return False
                    cur.execute('SELECT plan_id FROM biology_weekly_exam_links WHERE definition_id=%s;',(did,));old=cur.fetchone()
                    if old and old['plan_id']!=pid:raise ValueError('الامتحان مربوط بأسبوع آخر. افصله من ذلك الأسبوع أولاً.')
                    if old:cur.execute('DELETE FROM biology_weekly_exam_links WHERE definition_id=%s;',(did,))
                    else:cur.execute('INSERT INTO biology_weekly_exam_links(definition_id,plan_id) VALUES(%s,%s);',(did,pid))
                    cur.execute('UPDATE biology_weekly_plans SET published=FALSE,revision=revision+1 WHERE id=%s;',(pid,));conn.commit();return True
            try:await db.run(toggle)
            except ValueError as e:await q.answer(str(e),show_alert=True);return
            await q.answer('تم تغيير الربط. انشر الأسبوع لحفظ الموعد.');return await week_panel(q,pid)
    ns['button_handler']=button

    old_private=ns['private_messages']
    async def private(update,context):
        state=context.user_data.get('edu_input')
        if not state:return await old_private(update,context)
        msg=update.effective_message;uid=update.effective_user.id
        if time.monotonic()-state['at']>1800:
            context.user_data.pop('edu_input',None);await msg.reply_text('انتهت مهلة الكتابة. افتح الزر مرة ثانية.');return
        if state['kind']=='message':
            row=await thread_access(db,state['id'],uid,ns['is_admin'])
            if not row:context.user_data.pop('edu_input',None);await msg.reply_text('ألغيت المحادثة لأن الصلاحية أو ربط ولي الأمر تغير.');return
            body=(msg.text or msg.caption or '[مرفق]')[:3500]
            record=await sql(db,'''INSERT INTO biology_guardian_messages(thread_id,sender_id,source_message_id,body)
                VALUES(%s,%s,%s,%s) ON CONFLICT(thread_id,sender_id,source_message_id) DO UPDATE SET body=EXCLUDED.body RETURNING *;''',(row['id'],uid,msg.message_id,body),True,True)
            if record['sent']:await msg.reply_text('هذه الرسالة أرسلت سابقاً.');return
            destination=row['parent_id'] if uid==row['admin_id'] else row['admin_id']
            label='👨‍🏫 رسالة من الإدارة' if uid==row['admin_id'] else '👨‍👩‍👦 رد ولي الأمر'
            markup=kb([[B('✉️ رد على الرسالة',callback_data=f"edu_reply|{row['id']}"),B('📜 سجل المحادثة',callback_data=f"edu_history|{row['id']}")]])
            try:
                if msg.text:
                    await context.bot.send_message(destination,f"{label}\nالطالب: {row['full_name']}\n\n{body}",reply_markup=markup)
                else:
                    await context.bot.copy_message(destination,msg.chat_id,msg.message_id)
                    await context.bot.send_message(destination,f"{label}\nالطالب: {row['full_name']}\nالمرفق أعلاه.",reply_markup=markup)
            except ns['TelegramError']:
                await msg.reply_text('تعذر إرسال الرسالة. ربما الطرف الآخر لم يبدأ البوت أو حظره. أعد إرسالها لاحقاً.');return
            await sql(db,'UPDATE biology_guardian_messages SET sent=TRUE WHERE id=%s;',(record['id'],),write=True)
            context.user_data.pop('edu_input',None);await msg.reply_text('✅ وصلت رسالتك.',reply_markup=markup);return
        if not ns['is_admin'](uid):context.user_data.pop('edu_input',None);return
        text=(msg.text or '').strip()
        limit=80 if state['field'] in {'title','week_label'} else 200 if state['field']=='chapter_label' else 2000
        if not text or len(text)>limit:await msg.reply_text(f'أرسل نصاً من 1 إلى {limit} حرف.');return
        try:
            value=parse_lectures(text,ns['PLAYLISTS']) if state['field'] in {'lectures','selected_lectures'} else text
            if state['kind']=='school':result=await school_edit(db,state['id'],state['field'],value,uid)
            else:result=await save_week(db,state['id'],state['field'],value,uid)
        except (ValueError,TypeError):await msg.reply_text('القيمة غير صالحة. للمحاضرات استخدم: 4:1,2,3;5:1');return
        context.user_data.pop('edu_input',None)
        if not result:await msg.reply_text('لم يعد العنصر موجوداً. افتح القائمة من جديد.');return
        if state['kind']=='school' and result.get('group_message_id') and ns.get('BIOLOGY_GROUP_ID'):
            try:await context.bot.edit_message_text(chat_id=ns['BIOLOGY_GROUP_ID'],message_id=result['group_message_id'],text=ns['bold'](school_text(result)),parse_mode=ns['ParseMode'].HTML)
            except ns['TelegramError']:ns['logger'].warning('School announcement edit failed for %s',result['id'])
        target=f"v54_school_admin_review|{state['id']}" if state['kind']=='school' else f"edu_week|{state['id']}"
        await msg.reply_text('✅ حفظ التعديل. '+('انشر الأسبوع مجدداً لاعتماد التبليغ.' if state['kind']=='week' else 'تظهر البيانات الجديدة للطلاب فوراً.'),reply_markup=kb([[B('العودة للإعدادات',callback_data=target)]]))
    ns['private_messages']=private

    async def deliver_once(context,uid,key,text,markup):
        row=await sql(db,'''INSERT INTO biology_education_deliveries(event_key,user_id,lease_until) VALUES(%s,%s,CURRENT_TIMESTAMP+INTERVAL '10 minutes')
            ON CONFLICT(event_key,user_id) DO UPDATE SET lease_until=EXCLUDED.lease_until
            WHERE biology_education_deliveries.sent=FALSE AND biology_education_deliveries.lease_until<CURRENT_TIMESTAMP RETURNING user_id;''',(key,uid),True,True)
        if not row:return
        try:await context.bot.send_message(uid,text,reply_markup=markup)
        except ns['TelegramError']:
            ns['logger'].warning('Education notice delivery failed for %s',uid);return
        await sql(db,'UPDATE biology_education_deliveries SET sent=TRUE WHERE event_key=%s AND user_id=%s;',(key,uid),write=True)

    async def job(context):
        now=datetime.now(ZONE);today=now.date();start=week_start(today)
        plans=await sql(db,'SELECT * FROM biology_weekly_plans WHERE published=TRUE AND exam_date BETWEEN %s AND %s;',(today,start+timedelta(days=6)))
        students=await sql(db,"SELECT user_id,study_track FROM biology_students WHERE approved=TRUE AND reset_pending=FALSE AND admin_blocked=FALSE;")
        keys=[f"week:{p['id']}:{p['revision']}" for p in plans]+[f'plan:{today.isoformat()}']
        sent={(r['event_key'],r['user_id']) for r in await sql(db,'SELECT event_key,user_id FROM biology_education_deliveries WHERE event_key=ANY(%s) AND sent=TRUE;',(keys,))}
        for student in students:
            uid=student['user_id']
            if student['study_track']=='course' and (not hasattr(db,'section_enabled') or await db.section_enabled(uid,'exams')):
                for plan in plans:
                    key=f"week:{plan['id']}:{plan['revision']}"
                    if (key,uid) not in sent:await deliver_once(context,uid,key,announcement(plan),kb([[B('📅 امتحانات هذا الأسبوع',callback_data='edu_weekly')]]))
            if 18<=now.hour<21 and (f'plan:{today.isoformat()}',uid) not in sent:
                prep=await db.v52_current_preparation(uid)
                if prep and prep.get('pending_lectures'):
                    text=f"📚 خطوة اليوم تقرّبك من هدفك\nالفصل {prep['chapter']} — المحاضرات "+'+'.join(map(str,prep['pending_lectures']))+'\nابدأ بمحاضرة واحدة، ثم افتح خطتك لتكمل البقية.'
                    await deliver_once(context,uid,f'plan:{today.isoformat()}',text,kb([[B('📚 خطتي الدراسية',callback_data='edu_plan')]]))
    old_post=ns['post_init']
    async def post_init(app):
        await old_post(app)
        app.job_queue.run_repeating(job,1800,first=50,name='biology_weekly_and_daily_v4')
    ns['post_init']=post_init
