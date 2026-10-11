"""Homework administration and idempotent teacher-awarded submission XP."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import secrets
ZONE=ZoneInfo('Asia/Baghdad')

def install(ns,db,prefix):
    if prefix not in {'chemistry','english','mathematics','physics','biology','french','islamic'}:raise ValueError('Invalid subject')
    p=prefix;B=ns['InlineKeyboardButton'];K=ns['InlineKeyboardMarkup']
    async def sql(query,args=(),one=False,write=False):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(query,args)
                result=(cur.fetchone() if one else cur.fetchall()) if cur.description else None
                if write:conn.commit()
                return result
        return await db.run(op)
    old_init=db.init_db
    def init_db():
        old_init()
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute(f'''ALTER TABLE {p}_tasks ADD COLUMN IF NOT EXISTS homework_opens_at TIMESTAMPTZ;
                ALTER TABLE {p}_tasks ADD COLUMN IF NOT EXISTS homework_manual_closed BOOLEAN NOT NULL DEFAULT FALSE;''')
            # Restore historical homework waivers from explicit admin-removal audit evidence.
            cur.execute(f"""INSERT INTO {p}_exam_warning_waivers(task_id,user_id,waived_by)
                SELECT t.id,s.user_id,a.actor_id FROM {p}_audit a
                JOIN {p}_tasks t ON t.id=(substring(a.details from 'task_id=([0-9]+)'))::INTEGER AND t.kind='homework'
                JOIN {p}_students s ON s.user_id=(substring(a.details from 'user_id=([0-9]+)'))::BIGINT
                WHERE a.action='remove_selected_warning' ON CONFLICT(task_id,user_id) DO NOTHING;""")
            cur.execute(f"""WITH removed AS (
                DELETE FROM {p}_warning_log w USING {p}_tasks t WHERE w.task_id=t.id AND t.kind='homework'
                AND COALESCE(w.issued_by,0)=0 AND (t.homework_manual_closed OR t.retired_obligation OR EXISTS(
                    SELECT 1 FROM {p}_exam_warning_waivers v WHERE v.task_id=w.task_id AND v.user_id=w.user_id))
                RETURNING w.user_id), counts AS (SELECT user_id,COUNT(*) n FROM removed GROUP BY user_id)
                UPDATE {p}_students s SET warnings=GREATEST(0,s.warnings-counts.n::INTEGER) FROM counts WHERE s.user_id=counts.user_id;""")
            conn.commit()
    db.init_db=init_db;ns['init_db']=init_db
    async def task(tid):return await sql(f"SELECT * FROM {p}_tasks WHERE id=%s AND kind='homework';",(tid,),True)
    old_effective=db.effective_task_deadline
    async def effective(tid,uid):
        row=await old_effective(tid,uid)
        if not row:return row
        t=await task(tid)
        if t and (t.get('retired_obligation') or t.get('homework_manual_closed') or (t.get('homework_opens_at') and t['homework_opens_at']>datetime.now(ZONE))):
            return {**row,'assigned':False,'closed':True}
        return row
    db.effective_task_deadline=effective;ns['effective_task_deadline']=effective
    async def clear_cancelled(tid):
        await sql(f"""WITH removed AS (
            DELETE FROM {p}_warning_log WHERE task_id=%s AND COALESCE(issued_by,0)=0 RETURNING user_id,task_id),
            waivers AS (INSERT INTO {p}_exam_warning_waivers(task_id,user_id,waived_by)
                SELECT DISTINCT task_id,user_id,0 FROM removed ON CONFLICT(task_id,user_id) DO NOTHING),
            counts AS (SELECT user_id,COUNT(*) n FROM removed GROUP BY user_id)
            UPDATE {p}_students s SET warnings=GREATEST(0,s.warnings-counts.n::INTEGER)
            FROM counts WHERE s.user_id=counts.user_id;""",(tid,),write=True)
    async def bonus(tid,uid,value,actor):
        if not 0<=value<=1000:raise ValueError('XP بين 0 و1000.')
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f"SELECT id FROM {p}_tasks WHERE id=%s AND kind='homework' FOR UPDATE;",(tid,))
                if not cur.fetchone():raise ValueError('الواجب غير موجود.')
                cur.execute(f"SELECT user_id FROM {p}_submissions WHERE task_id=%s AND user_id=%s AND submitted_at IS NOT NULL;",(tid,uid))
                if not cur.fetchone():raise ValueError('لم يسلم الطالب هذا الواجب.')
                delta=db._set_xp_event(cur,uid,value,'مكافأة الأستاذ على الواجب',f'homework_teacher:{tid}:{uid}')
                cur.execute(f"INSERT INTO {p}_audit(actor_id,action,details) VALUES(%s,'homework_teacher_xp',%s);",(actor,f'task={tid};student={uid};value={value};delta={delta}'))
                conn.commit();return delta
        return await db.run(op)
    # Exposed for focused regression tests and future subject ports.
    ns['_homework_bonus']=bonus
    def controls(tid,uid):
        return K([[B(f'⭐ {v} XP',callback_data=f'hw|xp|{tid}|{uid}|{v}') for v in (0,10,20)],
                  [B(f'⭐ {v} XP',callback_data=f'hw|xp|{tid}|{uid}|{v}') for v in (30,50,100)],
                  [B('⚙️ إدارة الواجب',callback_data=f'hw|open|{tid}')]])
    old_delivery=ns['_v48_deliver_submission_row']
    async def delivery(bot,row):
        result=await old_delivery(bot,row)
        if result in ('added','replaced','album_part','delivered') and await task(row['task_id']):
            latest=await sql(f'SELECT delivered_message_id FROM {p}_submission_delivery_outbox WHERE id=%s;',(row['id'],),True)
            mid=(latest or {}).get('delivered_message_id') or row.get('delivered_message_id')
            if mid:
                try:await bot.edit_message_reply_markup(chat_id=row['destination_chat_id'],message_id=mid,reply_markup=controls(row['task_id'],row['user_id']))
                except ns['TelegramError']:ns['logger'].exception('Homework review keyboard could not be attached')
        return result
    ns['_v48_deliver_submission_row']=delivery
    old_menu=ns['main_menu']
    def main_menu(admin=False):
        m=old_menu(admin)
        return m
    ns['main_menu']=main_menu
    def back(tid=None):return [B('◀️ الواجب',callback_data=f'hw|open|{tid}')] if tid else [B('🏠 القائمة',callback_data='menu')]
    async def listing(q,page):
        page=max(0,page);rows=await sql(f"SELECT id,title,closed FROM {p}_tasks WHERE kind='homework' ORDER BY id DESC LIMIT 11 OFFSET %s;",(page*10,))
        kb=[[B(('🔒 ' if r['closed'] else '📚 ')+r['title'][:45],callback_data=f"hw|open|{r['id']}")] for r in rows[:10]]
        if page:kb.append([B('السابق',callback_data=f'hw|list|{page-1}')])
        if len(rows)>10:kb.append([B('التالي',callback_data=f'hw|list|{page+1}')])
        kb.append([B('➕ نشر واجب',callback_data='v29_pub|homework')]);kb.append(back())
        await ns['safe_homework_panel'](q,'📚 إدارة الواجبات\nاختر الواجب للتحكم به.',K(kb))
    async def panel(q,tid):
        t=await task(tid)
        if not t:await q.answer('الواجب محذوف.',show_alert=True);return
        text=f"📚 {t['title']}\n⏰ النهاية: {t['deadline'].astimezone(ZONE):%d/%m/%Y %H:%M}\n⭐ مكافأة التسليم: {t['xp_reward']} XP\n"+('🔒 مغلق يدوياً' if t.get('homework_manual_closed') else '✅ يخضع للموعد والتمديدات')
        if t.get('homework_opens_at'):text+=f"\nالبداية: {t['homework_opens_at'].astimezone(ZONE):%d/%m/%Y %H:%M}"
        kb=[[B('✏️ العنوان',callback_data=f'hw|field|{tid}|title'),B('⭐ مكافأة التسليم',callback_data=f'hw|field|{tid}|xp_reward')],
            [B('🗓 وقت البداية والنهاية',callback_data=f'hw|field|{tid}|window')],
            [B('⏳ تمديد الجميع 24 ساعة',callback_data=f'hw|extendall|{tid}|24'),B('⏳ 48 ساعة',callback_data=f'hw|extendall|{tid}|48')],
            [B('📜 سجل التعديلات',callback_data=f'hw|audit|{tid}')],
            [B('🔒 إغلاق',callback_data=f'hw|close|{tid}'),B('🔓 فتح 24 ساعة',callback_data=f'hw|reopen|{tid}')],
            [B('👥 الطلاب والتسليمات',callback_data=f'hw|students|{tid}|0'),B('➕ إضافة طالب',callback_data=f'hw|field|{tid}|add')],
            [B('🖼 استبدال مرفق الواجب',callback_data=f'hw|field|{tid}|media')],
            [B('📄 عرض الأسئلة',callback_data=f'hw|content|{tid}'),B('✏️ تعديل نص الواجب',callback_data=f'hw|field|{tid}|text_content')],
            [B('🚫 إلغاء الواجب وإسقاط إنذاراته',callback_data=f'hw|cancel|{tid}')],
            [B('🗑 حذف الواجب نهائياً',callback_data=f'hw|delete|{tid}')],[B('◀️ الواجبات',callback_data='hw|list|0')]]
        await ns['safe_homework_panel'](q,text,K(kb))
    async def safe_panel(q,text,kb):
        # Group submissions are media: never replace their caption or attached answer.
        if q.message.chat.type!='private' or not getattr(q.message,'text',None):
            await q.message.reply_text(text,reply_markup=kb)
        else:await q.edit_message_text(text,reply_markup=kb)
    ns['safe_homework_panel']=safe_panel
    async def students(q,tid,page):
        rows=await sql(f'''SELECT s.user_id,s.full_name,sub.submitted_at,sub.grade FROM {p}_task_students r
            JOIN {p}_students s ON s.user_id=r.user_id LEFT JOIN {p}_submissions sub ON sub.task_id=r.task_id AND sub.user_id=r.user_id
            WHERE r.task_id=%s ORDER BY s.user_id LIMIT 16 OFFSET %s;''',(tid,max(0,page)*15))
        kb=[[B(('✅ ' if r['submitted_at'] else '⏳ ')+r['full_name'][:35],callback_data=f"hw|student|{tid}|{r['user_id']}")] for r in rows[:15]]
        if page:kb.append([B('السابق',callback_data=f'hw|students|{tid}|{page-1}')])
        if len(rows)>15:kb.append([B('التالي',callback_data=f'hw|students|{tid}|{page+1}')])
        await safe_panel(q,'👥 طلاب الواجب — ✅ مسلّم / ⏳ لم يسلّم',K(kb+[back(tid)]))
    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query;data=q.data or '';actor=q.from_user.id
        if data.startswith('tasks|homework') and ns['is_admin'](actor):await q.answer();await listing(q,0);return
        if not data.startswith('hw|'):
            context.user_data.pop('hw_edit',None)
            return await old_button(update,context)
        if not ns['is_admin'](actor):await q.answer('للإدارة فقط.',show_alert=True);return
        bits=data.split('|');action=bits[1]
        try:
            tid=int(bits[2]);await q.answer()
            if action=='list':await listing(q,tid);return
            t=await task(tid)
            if not t:await q.message.reply_text('الواجب غير موجود.');return
            if action=='open':await panel(q,tid)
            elif action=='content':await ns['send_task_content'](context.bot,q.message.chat_id,t)
            elif action=='xp':
                uid,value=map(int,bits[3:5]);delta=await bonus(tid,uid,value,actor)
                await q.message.reply_text(f'✅ مكافأة الأستاذ للطالب {uid}: {value} XP. التغيير: {delta:+d}.\nاختيار قيمة أخرى يعدّل المكافأة نفسها ولا يكررها.')
            elif action=='students':await students(q,tid,int(bits[3]))
            elif action=='student':
                uid=int(bits[3]);r=await sql(f'''SELECT s.full_name,sub.grade,sub.submitted_at FROM {p}_students s
                    JOIN {p}_task_students a ON a.user_id=s.user_id AND a.task_id=%s
                    LEFT JOIN {p}_submissions sub ON sub.user_id=s.user_id AND sub.task_id=a.task_id WHERE s.user_id=%s;''',(tid,uid),True)
                if not r:raise ValueError('الطالب غير مكلّف بالواجب.')
                kb=[[B(f'تمديد {h} ساعة',callback_data=f'hw|extend|{tid}|{uid}|{h}') for h in (6,24,48)],
                    [B('⭐ XP مخصص',callback_data=f'hw|field|{tid}|bonus|{uid}'),B('📊 الدرجة',callback_data=f'hw|field|{tid}|grade|{uid}')],
                    [B('🛡 إعفاء من إنذار هذا الواجب',callback_data=f'hw|waive|{tid}|{uid}')],
                    [B('🚫 إلغاء تكليفه',callback_data=f'hw|remove|{tid}|{uid}')],back(tid)]
                await safe_panel(q,f"👤 {r['full_name']}\nالتسليم: {'تم' if r['submitted_at'] else 'لم يسلم'}\nالدرجة: {r.get('grade')}",K(kb))
            elif action=='extendall':
                h=int(bits[3])
                if h not in (24,48):raise ValueError('مدة غير صالحة.')
                await sql(f'''UPDATE {p}_tasks SET deadline=GREATEST(deadline,CURRENT_TIMESTAMP)+(%s||' hours')::INTERVAL,
                    closed=FALSE,homework_manual_closed=FALSE,retired_obligation=FALSE,
                    teacher_deadline_reminder_sent=FALSE WHERE id=%s AND kind='homework';''',(h,tid),write=True)
                await sql(f"INSERT INTO {p}_audit(actor_id,action,details) VALUES(%s,'homework_extend_all',%s);",
                    (actor,f'task={tid};hours={h}'),write=True)
                await panel(q,tid)
            elif action=='audit':
                rows=await sql(f'''SELECT actor_id,action,details,created_at FROM {p}_audit
                    WHERE details LIKE %s ORDER BY id DESC LIMIT 15;''',(f'%task={tid}%',))
                lines=['📜 آخر تعديلات الواجب']+[f"{r['created_at'].astimezone(ZONE):%d/%m %H:%M} | {r['action']} | {r['actor_id']}" for r in rows]
                await safe_panel(q,'\n'.join(lines),K([back(tid)]))
            elif action=='waive':
                student_id=int(bits[3])
                result=await db.v43_clear_task_warnings(tid,student_id,actor,True)
                await q.message.reply_text(f"🛡 تم إعفاء الطالب من إنذار هذا الواجب. الإنذارات المتبقية: {result['warnings']}.")
            elif action=='extend':
                uid,h=map(int,bits[3:5])
                if h not in (6,24,48):raise ValueError('مدة غير صالحة.')
                # Extension is cumulative from the later of task deadline, prior
                # individual extension or current time. Never shorten a student's window.
                await sql(f'''INSERT INTO {p}_task_extensions(task_id,user_id,extended_until)
                    SELECT %s,ts.user_id,GREATEST(t.deadline,CURRENT_TIMESTAMP,
                        COALESCE(e.extended_until,t.deadline))+(%s||' hours')::INTERVAL
                    FROM {p}_task_students ts JOIN {p}_tasks t ON t.id=ts.task_id
                    LEFT JOIN {p}_task_extensions e ON e.task_id=ts.task_id AND e.user_id=ts.user_id
                    WHERE ts.task_id=%s AND ts.user_id=%s
                    ON CONFLICT(task_id,user_id) DO UPDATE SET extended_until=EXCLUDED.extended_until;''',(tid,h,tid,uid),write=True)
                await q.message.reply_text('✅ تم تمديد الطالب من موعده الحالي، دون تقصير تمديد سابق. إذا كان الواجب مغلقاً يدوياً افتحه أولاً.')
            elif action=='remove':
                token=secrets.token_hex(8);context.user_data['hw_confirm']=(token,tid,int(bits[3]),'remove')
                await safe_panel(q,'تأكيد إلغاء تكليف الطالب؟ تبقى إجابته ونقاطه محفوظة.',K([[B('تأكيد',callback_data=f'hw|confirm|{tid}|{token}')],back(tid)]))
            elif action=='cancel':
                token=secrets.token_hex(8);context.user_data['hw_confirm']=(token,tid,0,'cancel')
                await safe_panel(q,'إلغاء الواجب للجميع وإزالة إنذارات عدم تسليمه؟ تبقى التسليمات محفوظة.',K([[B('تأكيد الإلغاء',callback_data=f'hw|confirm|{tid}|{token}')],back(tid)]))
            elif action=='delete':
                token=secrets.token_hex(8);context.user_data['hw_confirm']=(token,tid,0,'delete')
                await safe_panel(q,'حذف الواجب وتسليماته وبياناته من قاعدة البيانات نهائياً؟ لا تُحذف رسائل المجموعة.',K([[B('تأكيد الحذف',callback_data=f'hw|confirm|{tid}|{token}')],back(tid)]))
            elif action=='confirm':
                state=context.user_data.pop('hw_confirm',None)
                if not state or state[0]!=bits[3] or state[1]!=tid:raise ValueError('أعد فتح نافذة التأكيد.')
                if state[3]=='delete':await db.delete_task(tid);await listing(q,0)
                elif state[3]=='cancel':
                    await sql(f'UPDATE {p}_tasks SET retired_obligation=TRUE,homework_manual_closed=TRUE,closed=TRUE WHERE id=%s;',(tid,),write=True)
                    await clear_cancelled(tid);await panel(q,tid)
                else:await sql(f'DELETE FROM {p}_task_students WHERE task_id=%s AND user_id=%s;',(tid,state[2]),write=True);await students(q,tid,0)
            elif action=='close':
                await sql(f'UPDATE {p}_tasks SET homework_manual_closed=TRUE,closed=TRUE WHERE id=%s;',(tid,),write=True);await panel(q,tid)
            elif action=='reopen':
                now=datetime.now(ZONE);await sql(f'UPDATE {p}_tasks SET retired_obligation=FALSE,homework_manual_closed=FALSE,closed=FALSE,homework_opens_at=%s,deadline=%s WHERE id=%s;',(now,now+timedelta(days=1),tid),write=True);await panel(q,tid)
            elif action=='field':
                if q.message.chat.type!='private':raise ValueError('افتح إدارة الواجبات في خاص البوت لتعديل الحقول.')
                field=bits[3];prompts={'media':'أرسل صورة أو PDF أو فيديو بديل. سيحل محل مرفقات الواجب الحالية داخل البوت.', 'title':'أرسل عنوان الواجب.','text_content':'أرسل النص الجديد؛ المرفقات الحالية تبقى كما هي.','xp_reward':'أرسل مكافأة التسليم من 0 إلى 1000. تخص التسليمات اللاحقة فقط.','window':'أرسل البداية والنهاية بتوقيت بغداد:\n08/10/2026 18:00 | 09/10/2026 18:00','add':'أرسل معرف الطالب ID.','bonus':'أرسل مكافأة الأستاذ من 0 إلى 1000. تعدّل المكافأة السابقة ولا تتكرر.','grade':'أرسل الدرجة من 0 إلى 100.'}
                if field not in prompts:raise ValueError('حقل غير صالح.')
                context.user_data['hw_edit']={'task':tid,'field':field,'uid':int(bits[4]) if len(bits)>4 else None,'at':datetime.now(ZONE)}
                await safe_panel(q,prompts[field]+'\nللإلغاء اضغط العودة.',K([back(tid)]))
            else:raise ValueError('زر غير صالح.')
            if action!='field':context.user_data.pop('hw_edit',None)
        except (ValueError,IndexError) as exc:await q.message.reply_text(str(exc) or 'قيمة غير صالحة.')
    ns['button_handler']=button
    old_private=ns['private_messages']
    async def private(update,context):
        state=context.user_data.get('hw_edit')
        if not state:return await old_private(update,context)
        if not ns['is_admin'](update.effective_user.id):context.user_data.pop('hw_edit',None);return
        if datetime.now(ZONE)-state['at']>timedelta(minutes=15):context.user_data.pop('hw_edit',None);await update.effective_message.reply_text('انتهت المهلة، أعد فتح التعديل.');return
        msg=update.effective_message;text=(msg.text or '').strip();tid=state['task'];field=state['field'];uid=state['uid']
        try:
            if not await task(tid):raise ValueError('الواجب محذوف.')
            if field=='media':
                kind,file_id,caption=ns['message_payload'](msg)
                if not file_id or kind not in ('photo','document','video'):raise ValueError('أرسل صورة أو مستند أو فيديو.')
                def replace_media():
                    with db.connect() as conn,conn.cursor() as cur:
                        cur.execute(f"SELECT id FROM {p}_tasks WHERE id=%s AND kind='homework' FOR UPDATE;",(tid,))
                        if not cur.fetchone():raise ValueError('الواجب محذوف.')
                        cur.execute(f'DELETE FROM {p}_task_media WHERE task_id=%s;',(tid,))
                        cur.execute(f'INSERT INTO {p}_task_media(task_id,payload_type,file_id,source_message_id) VALUES(%s,%s,%s,%s);',(tid,kind,file_id,msg.message_id))
                        cur.execute(f'UPDATE {p}_tasks SET payload_type=%s,file_id=%s,text_content=%s WHERE id=%s;',(kind,file_id,caption or '',tid))
                        conn.commit()
                await db.run(replace_media)
            elif field in ('title','text_content'):
                if not text or len(text)>(200 if field=='title' else 3500):raise ValueError('النص فارغ أو طويل جداً.')
                await sql(f'UPDATE {p}_tasks SET {field}=%s WHERE id=%s;',(text,tid),write=True)
            elif field=='window':
                parts=text.split('|')
                if len(parts)!=2:raise ValueError('أرسل البداية | النهاية.')
                a,b=[datetime.strptime(v.strip(),'%d/%m/%Y %H:%M').replace(tzinfo=ZONE) for v in parts]
                if b<=a or b<=datetime.now(ZONE):raise ValueError('النهاية يجب أن تكون بعد البداية وفي المستقبل.')
                await sql(f'UPDATE {p}_tasks SET homework_opens_at=%s,deadline=%s,closed=FALSE,homework_manual_closed=FALSE,retired_obligation=FALSE WHERE id=%s;',(a,b,tid),write=True)
            else:
                value=int(text)
                if field=='add':
                    row=await sql(f'SELECT user_id FROM {p}_students WHERE user_id=%s;',(value,),True)
                    if not row:raise ValueError('الطالب غير مسجل.')
                    await sql(f'INSERT INTO {p}_task_students(task_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING;',(tid,value),write=True)
                elif field=='bonus':await bonus(tid,uid,value,update.effective_user.id)
                elif field=='grade':
                    if not 0<=value<=100:raise ValueError('الدرجة بين 0 و100.')
                    if not await db.set_submission_grade(tid,uid,value,update.effective_user.id):raise ValueError('لا يوجد تسليم لتصحيحه.')
                elif field=='xp_reward':
                    if not 0<=value<=1000:raise ValueError('XP بين 0 و1000.')
                    await sql(f'UPDATE {p}_tasks SET xp_reward=%s WHERE id=%s;',(value,tid),write=True)
            context.user_data.pop('hw_edit',None)
            await msg.reply_text('✅ تم حفظ التعديل.',reply_markup=K([back(tid)]))
        except ValueError as exc:await msg.reply_text(str(exc) or 'أرسل قيمة صحيحة.')
    ns['private_messages']=private

    old_start=ns['start']
    async def start(update,context):
        context.user_data.pop('hw_edit',None);context.user_data.pop('hw_confirm',None)
        return await old_start(update,context)
    ns['start']=start
