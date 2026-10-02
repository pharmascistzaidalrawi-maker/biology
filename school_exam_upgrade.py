"""School exams use normal tasks, delivery and per-student late approvals."""
from datetime import datetime, time as dt_time, timedelta
from collections import deque
import time


def migrate(db, prefix):
    if prefix not in {'chemistry','physics','biology','french','mathematics'}:
        raise ValueError('Unknown bot')
    with db.connect() as conn,conn.cursor() as cur:
        cur.execute(f'''ALTER TABLE {prefix}_school_reviews
            ADD COLUMN IF NOT EXISTS exam_opens_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS exam_closes_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS exam_manual_closed BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS exam_deleted BOOLEAN NOT NULL DEFAULT FALSE;
            UPDATE {prefix}_school_reviews SET
              exam_opens_at=COALESCE(exam_opens_at,(exam_date+TIME '18:00') AT TIME ZONE 'Asia/Baghdad'),
              exam_closes_at=COALESCE(exam_closes_at,((exam_date+TIME '18:00') AT TIME ZONE 'Asia/Baghdad')+INTERVAL '24 hours');
            UPDATE {prefix}_tasks t SET deadline=r.exam_closes_at,
              exam_available_at=r.exam_opens_at,exam_duration_hours=24
              FROM {prefix}_school_reviews r WHERE t.school_review_id=r.id
              AND t.deadline>CURRENT_TIMESTAMP+INTERVAL '365 days';''')

def install(ns, db, prefix):
    if prefix not in {'chemistry','physics','biology','french','mathematics'}:
        raise ValueError('Unknown bot')
    B=ns['InlineKeyboardButton']; K=ns['InlineKeyboardMarkup']
    bold=ns['bold']; HTML=ns['ParseMode'].HTML; zone=ns['TIMEZONE']
    def window(row):
        start=row.get('exam_opens_at') or datetime.combine(row['exam_date'],dt_time(18),tzinfo=zone)
        return start,row.get('exam_closes_at') or start+timedelta(hours=24)

    async def review_control(review_id,action,actor,start=None,end=None):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f'SELECT * FROM {prefix}_school_reviews WHERE id=%s FOR UPDATE;', (review_id,))
                row=cur.fetchone()
                if not row: return False
                a,b=window(row)
                if action=='delete':
                    cur.execute(f'UPDATE {prefix}_school_reviews SET exam_deleted=TRUE,exam_manual_closed=TRUE WHERE id=%s;', (review_id,))
                    cur.execute(f'DELETE FROM {prefix}_school_review_exam_media WHERE review_id=%s;', (review_id,))
                    cur.execute(f'''UPDATE {prefix}_students s SET warnings=GREATEST(0,s.warnings-w.n)
                        FROM (SELECT l.user_id,COUNT(*)::INTEGER AS n FROM {prefix}_warning_log l
                          JOIN {prefix}_tasks t ON t.id=l.task_id WHERE t.school_review_id=%s GROUP BY l.user_id) w
                        WHERE s.user_id=w.user_id;''',(review_id,))
                    cur.execute(f'DELETE FROM {prefix}_tasks WHERE school_review_id=%s;', (review_id,))
                    cur.execute(f'UPDATE {prefix}_school_review_progress SET approval_notified_at=NULL WHERE review_id=%s;', (review_id,))
                else:
                    closed=action=='close'
                    if action=='reopen': a=datetime.now(zone); b=a+timedelta(hours=24)
                    elif action=='schedule': a,b=start,end
                    elif action.startswith('extend:'): b=max(b,datetime.now(zone))+timedelta(hours=int(action.split(':')[1]))
                    if b<=a: raise ValueError('End must follow start')
                    cur.execute(f'''UPDATE {prefix}_school_reviews SET exam_opens_at=%s,exam_closes_at=%s,
                        exam_manual_closed=%s WHERE id=%s;''',(a,b,closed,review_id))
                    cur.execute(f'''UPDATE {prefix}_tasks SET deadline=%s,exam_available_at=%s,closed=%s,
                        teacher_deadline_reminder_sent=FALSE WHERE school_review_id=%s;''',(b,a,closed,review_id))
                    # Manual closure cancels personal extensions as well.
                    if closed or action in {'schedule','reopen'}:
                        cur.execute(f'DELETE FROM {prefix}_task_extensions WHERE task_id IN (SELECT id FROM {prefix}_tasks WHERE school_review_id=%s);',(review_id,))
                    if action in {'schedule','reopen'}:
                        cur.execute(f'''UPDATE {prefix}_late_exam_requests SET status='denied',
                            decided_by=%s,decided_at=CURRENT_TIMESTAMP WHERE status='pending'
                            AND task_id IN (SELECT id FROM {prefix}_tasks WHERE school_review_id=%s);''',(actor,review_id))
                cur.execute(f'INSERT INTO {prefix}_audit(actor_id,action,details) VALUES(%s,%s,%s);',
                    (actor,'school_exam_'+action,f'review={review_id}'))
                return True
        return await db.run(op)

    old_admin=ns['v54_school_admin_review']
    async def admin_review(query,review_id):
        row=await db.v54_school_review_by_id(review_id)
        if not row: return await old_admin(query,review_id)
        a,b=window(row)
        text=ns['_v54_school_review_text'](row,'⚙️ إعداد امتحان مراجعة المدرسة')
        text+=f"\nالفتح: {a.astimezone(zone):%d/%m/%Y %H:%M}\nالانتهاء: {b.astimezone(zone):%d/%m/%Y %H:%M}"
        text+=f"\nأجزاء الأسئلة: {row.get('media_count',0)}"
        if row.get('exam_deleted'):text+='\n🗑 حذفت الأسئلة؛ يمكنك إضافة امتحان جديد.'
        elif row.get('exam_manual_closed') or b<=datetime.now(zone):text+='\n⏰ مغلق — حدد موعداً أو أعد الفتح.'
        rows=[[B('➕ إضافة/استبدال الأسئلة',callback_data=f'v54_school_exam_add|{review_id}',style='success')],
            [B('🗓 تحديد الفتح والانتهاء',callback_data=f'sex|schedule|{review_id}',style='primary')],
            [B('⏳ تمديد ساعة',callback_data=f'sex|extend:1|{review_id}'),B('⏳ تمديد 24 ساعة',callback_data=f'sex|extend:24|{review_id}')],
            [B('🔓 إعادة الفتح 24 ساعة',callback_data=f'sex|reopen|{review_id}',style='success'),B('🔒 إغلاق الآن',callback_data=f'sex|close|{review_id}',style='danger')],
            [B('🗑 حذف الامتحان',callback_data=f'sex|delete_confirm|{review_id}',style='danger')],
            [B('◀️ الأسابيع',callback_data='v54_school_catalog'),ns['back_menu']()]]
        await query.edit_message_text(bold(text),parse_mode=HTML,reply_markup=K(rows))
    ns['v54_school_admin_review']=admin_review

    old_private=ns['private_messages']
    async def private_messages(update,context):
        pending=context.user_data.get('school_exam_schedule')
        if pending and ns['is_admin'](update.effective_user.id):
            if time.monotonic()-pending['at']>600:
                context.user_data.pop('school_exam_schedule',None)
                await update.effective_message.reply_text('انتهت المهلة. افتح تحديد الموعد من جديد.');return
            try:
                parts=(update.effective_message.text or '').strip().split('|')
                if len(parts)!=2:raise ValueError
                a,b=[datetime.strptime(p.strip(),'%d/%m/%Y %H:%M').replace(tzinfo=zone) for p in parts]
                if b<=a or b<=datetime.now(zone):raise ValueError
            except ValueError:
                await update.effective_message.reply_text('أرسل البداية والنهاية هكذا:\n01/10/2026 18:00 | 02/10/2026 18:00\nالنهاية يجب أن تكون بعد البداية وفي المستقبل.');return
            saved=await review_control(pending['id'],'schedule',update.effective_user.id,a,b)
            context.user_data.pop('school_exam_schedule',None)
            await update.effective_message.reply_text('✅ حفظ الموعد لجميع الطلاب.' if saved else 'المراجعة غير موجودة.');return
        return await old_private(update,context)
    ns['private_messages']=private_messages

    old_show=ns['show_task']
    async def show_task(query,context,task_id):
        task=await ns['get_task'](task_id)
        if task and task.get('school_review_id') and not ns['is_admin'](query.from_user.id):
            status=await db.v45_exam_task_status(query.from_user.id,task_id)
            if not status or not status.get('track_allowed'):
                await query.answer('هذا الامتحان غير مخصص لحسابك.',show_alert=True);return
            if not status.get('submitted') and (status.get('closed') or status['effective_deadline']<=status['now']):
                rows=[]
                pending=status.get('late_request_status')=='pending'
                text='⏳ طلبك ينتظر قرار الأستاذ.' if pending else 'انتهى أو أغلق الامتحان. يمكن طلب الدخول بموافقة الإدارة.'
                if not pending:
                    rows.append([B(f"👨‍🏫 طلب دخول — {ns['LATE_EXAM_XP_COST']} XP",callback_data=f'v45_late_request|{task_id}')])
                rows.append([ns['back_menu']()])
                await query.edit_message_text(bold(text),parse_mode=HTML,reply_markup=K(rows));return
        return await old_show(query,context,task_id)
    ns['show_task']=show_task

    old_button=ns['button_handler']
    async def button_handler(update,context):
        q=update.callback_query; data=q.data or ''; uid=q.from_user.id
        if data=='menu':context.user_data.pop('school_exam_schedule',None)
        if data.startswith('sex|'):
            if not ns['is_admin'](uid):await q.answer('للإدارة فقط.',show_alert=True);return
            _,action,number=data.split('|'); rid=int(number)
            if action=='schedule':
                context.user_data['school_exam_schedule']={'id':rid,'at':time.monotonic()}
                await q.answer();await q.edit_message_text('أرسل وقت الفتح ووقت الانتهاء بتوقيت بغداد، مثلاً:\n01/10/2026 18:00 | 02/10/2026 18:00',reply_markup=K([[ns['back_menu']()]]));return
            if action=='delete_confirm':
                await q.answer();await q.edit_message_text('تأكيد حذف أسئلة الامتحان وتسليماته من قاعدة البوت؟ تبقى مراجعة الأسبوع وإنجاز إكمالها.',reply_markup=K([[B('🗑 تأكيد الحذف',callback_data=f'sex|delete|{rid}',style='danger')],[B('إلغاء',callback_data=f'v54_school_admin_review|{rid}')]]));return
            if action not in {'close','reopen','delete','extend:1','extend:24'}:
                await q.answer('خيار غير صالح.',show_alert=True);return
            await q.answer()
            await review_control(rid,action,uid)
            await admin_review(q,rid);return
        if data.startswith('v54_school_exam_open|') and not ns['is_admin'](uid):
            rid=int(data.split('|')[1])
            result=await db.v54_prepare_school_review_exam(rid,uid)
            if result.get('status')=='closed':
                await q.answer();await ns['show_task'](q,context,result['task']['id']);return
            if result.get('status')=='scheduled':
                await q.answer('لم يحن وقت فتح الامتحان بعد.',show_alert=True);return
        return await old_button(update,context)
    ns['button_handler']=button_handler

    # Shared parent approval must preserve the school's fixed window.
    old_decide=db.v45_decide_late_exam_request
    async def decide(request_id,approved,actor):
        result=await old_decide(request_id,approved,actor)
        if result.get('status')=='approved':
            await db.decide_exam_access(result['task_id'],result['user_id'],'approved',actor)
        return result
    db.v45_decide_late_exam_request=decide

    # A compact in-memory guard stops floods before database work; no permanent ban.
    buckets={}; mute={}; last_notice={}; checks=[0]
    async def spam_guard(update,context):
        user=update.effective_user
        if not user or ns['is_admin'](user.id):return
        q=update.callback_query
        if not q and (not update.effective_chat or update.effective_chat.type!='private'):return
        now=time.monotonic(); uid=user.id; key=(uid,bool(q))
        checks[0]+=1
        if checks[0]%256==0:
            for k in list(buckets):
                if not buckets[k] or now-buckets[k][-1]>60:buckets.pop(k,None)
            for table in (mute,last_notice):
                for k in list(table):
                    if now-table[k]>60:table.pop(k,None)
        events=buckets.setdefault(key,deque())
        while events and now-events[0]>10:events.popleft()
        blocked=now<mute.get(uid,0)
        if not blocked:
            events.append(now)
            blocked=len(events)>(12 if q else 30)
            if blocked:mute[uid]=now+10
        if not blocked:return
        if q:
            try:await q.answer('انتظر 10 ثوانٍ قبل المحاولة مجدداً.',show_alert=False)
            except ns['TelegramError']:pass
        elif now-last_notice.get(uid,-100)>10:
            last_notice[uid]=now
            try:await update.effective_message.reply_text('⏳ إرسال سريع جداً. انتظر 10 ثوانٍ ثم حاول.')
            except ns['TelegramError']:pass
        raise ns['ApplicationHandlerStop']
    ns['spam_guard']=spam_guard
