"""V14: numbered publication, 48-hour warnings and unrestricted late homework."""
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo
import re

ZONE = ZoneInfo('Asia/Baghdad')
ORDINALS = ['الأول','الثاني','الثالث','الرابع','الخامس','السادس','السابع','الثامن',
            'التاسع','العاشر','الحادي عشر','الثاني عشر','الثالث عشر','الرابع عشر',
            'الخامس عشر','السادس عشر','السابع عشر','الثامن عشر','التاسع عشر','العشرون']

def homework_title(number):
    return 'الواجب '+(ORDINALS[number-1] if 1 <= number <= len(ORDINALS) else str(number))

def title_number(title):
    normalized=str(title).replace('الاول','الأول').strip(' 📚')
    for n, word in reversed(list(enumerate(ORDINALS,1))):
        if normalized == 'الواجب '+word: return n
    match=re.fullmatch(r'الواجب\s+(\d+)',normalized)
    return int(match[1]) if match else None

def reserve_number(cur, pending, number=None, title=None):
    """Serialize numbering across admins, pending previews and direct publishers."""
    cur.execute('SELECT pg_advisory_xact_lock(140048);')
    cur.execute('SELECT * FROM biology_pending_tasks WHERE id=%s FOR UPDATE;', (pending['id'],))
    pending=cur.fetchone()
    if not pending: return None
    if pending.get('homework_number') and number is None: return pending
    if number is None:
        cur.execute('''SELECT GREATEST(7,COALESCE(MAX(n),0))+1 AS number FROM (
            SELECT homework_number n FROM biology_tasks WHERE kind='homework'
            UNION ALL SELECT homework_number FROM biology_pending_tasks WHERE kind='homework') x;''')
        number=int(cur.fetchone()['number'])
    if not 1 <= number <= 100000: raise ValueError('رقم الواجب يجب أن يكون بين 1 و100000.')
    cur.execute('''SELECT 1 FROM biology_tasks WHERE kind='homework' AND homework_number=%s
        UNION ALL SELECT 1 FROM biology_pending_tasks WHERE homework_number=%s AND id<>%s;''',
        (number,number,pending['id']))
    if cur.fetchone(): raise ValueError('هذا الرقم مستخدم لواجب آخر؛ اختر رقماً مختلفاً.')
    cur.execute('''UPDATE biology_pending_tasks SET homework_number=%s,title=%s
        WHERE id=%s RETURNING *;''',(number,title or homework_title(number),pending['id']))
    return cur.fetchone()

def finalize_homework(cur, row, number=None, published_at=None):
    cur.execute('SELECT pg_advisory_xact_lock(140048);')
    explicit_number=number is not None
    if number is None: number=row.get('homework_number')
    if number is None:
        cur.execute('''SELECT GREATEST(7,COALESCE(MAX(n),0))+1 AS number FROM (
            SELECT homework_number n FROM biology_tasks WHERE kind='homework'
            UNION ALL SELECT homework_number FROM biology_pending_tasks WHERE kind='homework') x;''')
        number=int(cur.fetchone()['number'])
    published_at=published_at or row['created_at']
    cur.execute('''UPDATE biology_tasks SET homework_number=%s,created_at=%s,deadline=%s,
        title=CASE WHEN homework_number IS NULL THEN %s ELSE title END WHERE id=%s RETURNING *;''',
        (number,published_at,published_at+timedelta(hours=48),row['title'] if explicit_number or title_number(row['title'])==number else homework_title(number),row['id']))
    return cur.fetchone()

def migrate(db):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute('''ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS homework_number INTEGER;
            ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS homework_opens_at TIMESTAMPTZ;
            ALTER TABLE biology_pending_tasks ADD COLUMN IF NOT EXISTS homework_number INTEGER;
            ALTER TABLE biology_pending_tasks ADD COLUMN IF NOT EXISTS homework_published_at TIMESTAMPTZ;
            CREATE TABLE IF NOT EXISTS biology_v14_migrations(key TEXT PRIMARY KEY);''')
        cur.execute("SELECT pg_advisory_xact_lock(140048);")
        cur.execute("SELECT 1 FROM biology_v14_migrations WHERE key='48h';")
        if not cur.fetchone():
            cur.execute("SELECT id,title FROM biology_tasks WHERE kind='homework' ORDER BY id;")
            seen=set()
            for row in cur.fetchall():
                n=title_number(row['title'])
                if n and n not in seen:
                    cur.execute('UPDATE biology_tasks SET homework_number=%s WHERE id=%s;',(n,row['id']))
                    seen.add(n)
            cur.execute('''UPDATE biology_tasks SET deadline=created_at+INTERVAL '48 hours',warned=FALSE,
                closed=(created_at+INTERVAL '48 hours'<=CURRENT_TIMESTAMP)
                WHERE kind='homework' AND retired_obligation=FALSE AND homework_manual_closed=FALSE;
                UPDATE biology_pending_tasks SET homework_published_at=created_at WHERE kind='homework';
                DELETE FROM biology_warning_log w USING biology_tasks t
                WHERE w.task_id=t.id AND t.kind='homework' AND COALESCE(w.issued_by,0)=0
                  AND w.reason LIKE 'عدم إرسال%'
                  AND (t.deadline>CURRENT_TIMESTAMP OR EXISTS(SELECT 1 FROM biology_submissions s
                    WHERE s.task_id=t.id AND s.user_id=w.user_id AND s.submitted_at IS NOT NULL));
                UPDATE biology_students s SET warnings=(SELECT COUNT(*) FROM biology_warning_log w WHERE w.user_id=s.user_id);
                INSERT INTO biology_v14_migrations VALUES('48h');''')
        cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS biology_homework_number_unique
            ON biology_tasks(homework_number) WHERE homework_number IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS biology_pending_homework_number_unique
            ON biology_pending_tasks(homework_number) WHERE homework_number IS NOT NULL;''')
        conn.commit()

def champion_message(winner, subject='الأحياء'):
    grade=winner['grade']
    # Grades in this application are percentages (0..100), including stored history.
    if isinstance(grade,(float,int)): grade=f'{grade:g}'
    return ("تتقدم إدارة منصة المجتهد التعليمية بالشكر والتقدير للطالب/ة :\n\n"
            f"( {winner['full_name']} )\n\n"
            "على التفوق والإبداع في الامتحان لمادة :\n\n"
            f"( {subject} ) / ( {winner['title']} )\n\n"
            "وذلك لحصول الطالب/ة على درجة:\n\n"
            f"( {grade}/100 )\n\n"
            "كل التوفيق الكم يا رب نشوفكم بأعلى المراتب")

def bold_payload(data):
    """Format Telegram message bodies at the common API boundary, preserving links."""
    data=dict(data)
    for field, entities in (('text','entities'),('caption','caption_entities')):
        value=data.get(field)
        if not isinstance(value,str) or not value: continue
        mode=data.get('parse_mode')
        if str(mode).lower()=='html':
            value=re.sub(r'</?(?:b|strong|code|pre)(?:\s[^>]*)?>','',value,flags=re.I)
            data[field]='<b>'+value+'</b>'
        else:
            # Entity offsets use UTF-16 code units, including emoji pairs.
            from telegram import MessageEntity
            length=len(value.encode('utf-16-le'))//2
            existing=list(data.get(entities) or [])
            existing=[e for e in existing if (e.get('type') if isinstance(e,dict) else e.type) not in ('bold','code','pre')]
            data[entities]=existing+[MessageEntity('bold',0,length)]
            data.pop('parse_mode',None)
    return data

def install_bold():
    from telegram import Bot
    if getattr(Bot._post,'_v14_bold',False): return
    old=Bot._post
    async def post(self, endpoint, data=None, *args, **kwargs):
        if data and endpoint not in ('answerCallbackQuery','setChatTitle','setChatDescription','sendChatAction'):
            data=bold_payload(data)
            # Albums and edited media carry their own captions and parse modes.
            from telegram import InputMedia
            import copy
            def media_bold(item):
                if isinstance(item,InputMedia) and item.caption:
                    item=copy.copy(item)
                    changed=bold_payload({'caption':item.caption,'parse_mode':item.parse_mode,
                                          'caption_entities':item.caption_entities})
                    with item._unfrozen():
                        item.caption=changed['caption']; item.parse_mode=changed.get('parse_mode')
                        item.caption_entities=tuple(changed.get('caption_entities') or ())
                return item
            if isinstance(data.get('media'),(list,tuple)):
                data['media']=[media_bold(item) for item in data['media']]
            elif data.get('media') is not None: data['media']=media_bold(data['media'])
        return await old(self,endpoint,data,*args,**kwargs)
    post._v14_bold=True
    Bot._post=post

def install(ns,db):
    install_bold()
    B,K=ns['InlineKeyboardButton'],ns['InlineKeyboardMarkup']
    old_init=ns['init_db']
    def init_db():
        old_init(); migrate(db)
    ns['init_db']=db.init_db=init_db

    async def sql(statement,args=(),one=False):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(statement,args)
                result=(cur.fetchone() if one else cur.fetchall()) if cur.description else None
                conn.commit(); return result
        return await db.run(op)

    async def reserve(pending,number=None,title=None):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                result=reserve_number(cur,pending,number,title); conn.commit(); return result
        return await db.run(op)

    async def preview(q,pending):
        deadline=(pending.get('homework_published_at') or pending['created_at'])+timedelta(hours=48)
        text=(f"📚 تأكيد ربط الواجب\n\nالرقم: {pending['homework_number']}\nالاسم: {pending['title']}\n"
              f"⏰ مهلة التسليم: {deadline.astimezone(ZONE):%d/%m/%Y %H:%M}\n"
              "المدة 48 ساعة من النشر. يبقى التسليم متاحاً بعدها ويُلغى إنذار عدم التسليم عند تسجيل الحل.")
        await q.edit_message_text(text,reply_markup=K([
            [B('✅ تأكيد',callback_data=f"hw14|confirm|{pending['id']}")],
            [B('✏️ تعديل الاسم والرقم',callback_data=f"hw14|edit|{pending['id']}")],
            [B('❌ إلغاء',callback_data=f"cancelbind|{pending['id']}")]]))

    # Route the configured homework topic even when it shares a destination ID
    # with submissions; retain the existing exam and PDF routing elsewhere.
    old_capture=ns.get('capture_group_task')
    original_capture=ns.get('_old_capture_group_task_v30')
    if old_capture and original_capture:
        async def capture(update,context):
            msg=update.effective_message
            if (msg and update.effective_chat.id==ns.get('BIOLOGY_GROUP_ID')
                    and (msg.message_thread_id or 0)==ns.get('HOMEWORK_TOPIC_ID')):
                return await original_capture(update,context)
            return await old_capture(update,context)
        ns['capture_group_task']=capture

    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query; data=q.data or ''
        if data.startswith(('bind|','deadline|','customdeadline|','hw14|')):
            bits=data.split('|'); pid=int(bits[2] if bits[0]=='hw14' else bits[1])
            pending=await db.get_pending_task(pid)
            if (pending and pending['kind']=='homework') or data.startswith('hw14|'):
                if not ns['is_admin'](q.from_user.id):
                    await q.answer('للإدارة فقط.',show_alert=True); return
                if not pending:
                    await q.answer('تم التعامل مع المنشور مسبقاً.',show_alert=True);return
                if q.message.chat_id!=pending['chat_id'] and q.message.chat.type!='private':
                    await q.answer('استخدم رسالة الربط الأصلية.',show_alert=True);return
                action=bits[1] if bits[0]=='hw14' else 'preview'
                if action=='confirm':
                    task=await db.confirm_pending_task(pid,datetime.now(ZONE))
                    if not task:
                        await q.answer('تم التعامل مع المنشور مسبقاً.',show_alert=True);return
                    await q.answer('تم الربط')
                    await q.edit_message_text(f"✅ تم ربط {task['title']} بالبوت.\n📚 رقم الواجب: {task['homework_number']}\n⏰ نهاية المهلة: {task['deadline'].astimezone(ZONE):%d/%m/%Y %H:%M}\nالتسليم يبقى متاحاً بعد المهلة.")
                    await ns['notify_task_assignment'](context.bot,task);return
                await q.answer()
                pending=await reserve(pending)
                if not pending: return
                if action=='edit':
                    await q.edit_message_text(f"✏️ أرسل رداً على هذه الرسالة في المجموعة:\n/hwname {pid} 8 | الواجب الثامن\nغيّر الرقم والاسم كما تريد.",reply_markup=K([[B('◀️ التأكيد',callback_data=f'hw14|preview|{pid}')],[B('❌ إلغاء',callback_data=f'cancelbind|{pid}')]]));return
                await preview(q,pending);return
        # Homework uses the real deadline for warnings, but never for submission access.
        if data.startswith(('task|','submit|')):
            tid=int(data.split('|')[1]); task=await db.get_task(tid)
            if task and task['kind']=='homework':
                if data.startswith('task|'): return await show_homework(q,context,task)
                if not await allowed(q,context,task): return
                context.user_data['waiting_submission']=tid
                await q.answer();await context.bot.send_message(q.from_user.id,'📤 أرسل حتى 25 صورة أو ملف للحل، ثم اضغط «إنهاء التسليم». يُلغى إنذار عدم تسليم هذا الواجب بعد تسجيله.');return
        return await old_button(update,context)
    ns['button_handler']=button

    async def allowed(q,context,task):
        if ns['is_admin'](q.from_user.id): return True
        student=await db.get_student(q.from_user.id)
        effective=await db.effective_task_deadline(task['id'],q.from_user.id)
        if not student or not student.get('approved') or student.get('admin_blocked') or student.get('reset_pending') or not effective or not effective.get('assigned'):
            await q.answer('الواجب غير متاح لحسابك.',show_alert=True);return False
        if not student.get('admin_manual_active') and not await ns['is_channel_member'](context.bot,q.from_user.id):
            await q.answer('اشترك بالقناة المطلوبة أولاً.',show_alert=True);return False
        return True

    async def show_homework(q,context,task):
        if not await allowed(q,context,task): return
        await ns['send_task_content'](context.bot,q.from_user.id,task)
        await context.bot.send_message(q.from_user.id,
            f"📚 {task['title']}\n⏰ مهلة الإنذار: {task['deadline'].astimezone(ZONE):%d/%m/%Y %H:%M}\nالتسليم متاح حتى بعد المهلة؛ يُلغى إنذار عدم التسليم عند تسجيل الحل.",
            reply_markup=K([[B('📤 إرسال الواجب',callback_data=f"submit|{task['id']}")],
                [B('🔄 تغيير إجابتي',callback_data=f"retrysubmission|{task['id']}")],[ns['back_menu']()]]))
    old_show=ns['show_task']
    async def show(q,context,tid):
        task=await db.get_task(tid)
        if task and task['kind']=='homework' and not ns['is_admin'](q.from_user.id):return await show_homework(q,context,task)
        return await old_show(q,context,tid)
    ns['show_task']=show

    async def route(update,context):
        msg=update.effective_message; raw=(msg.text or '').strip()
        if raw.split(' ',1)[0].split('@')[0]=='/hwname':
            if not ns['is_admin'](update.effective_user.id):return
            try:
                match=re.fullmatch(r'/hwname(?:@\w+)?\s+(\d+)\s+(\d+)\s*\|\s*(.{1,100})',raw)
                if not match:raise ValueError('الصيغة: /hwname رقم_الطلب رقم_الواجب | الاسم')
                pending=await db.get_pending_task(int(match[1]))
                if not pending or pending['kind']!='homework' or msg.chat_id!=pending['chat_id'] or (msg.message_thread_id or 0)!=(pending.get('thread_id') or 0):raise ValueError('استخدم مجموعة وموضوع منشور الواجب الأصلي، وتأكد أنه لم يُربط بعد.')
                pending=await reserve(pending,int(match[2]),match[3].strip())
                await msg.reply_text(f"✅ الاسم: {pending['title']}\nرقم الواجب: {pending['homework_number']}\nارجع لرسالة التأكيد واضغط «التأكيد» لمراجعة البيانات.",reply_markup=K([[B('مراجعة وتأكيد',callback_data=f"hw14|preview|{pending['id']}")],[B('❌ إلغاء',callback_data=f"cancelbind|{pending['id']}")]]))
            except ValueError as exc:await msg.reply_text(str(exc))
            return
        return None
    ns['hwname_command']=route
