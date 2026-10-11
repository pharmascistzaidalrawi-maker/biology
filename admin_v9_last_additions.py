"""V9 shared fixes: durable warning removals, activation extensions, ID enrollment.

Installed after the V8 router; keeps the existing student/exam/homework flows.
"""
import logging
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger(__name__)
VALID_EXTENSION_HOURS = frozenset({24, 48, 72, 168})
SESSION_TTL = timedelta(minutes=10)


def parse_user_id(value):
    raw = str(value or '').strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789'))
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
        raise ValueError('أرسل آيدي الطالب الرقمي فقط، مثل 123456789.')
    number = int(raw)
    if not 1 <= number < 2**63:
        raise ValueError('آيدي الطالب غير صالح.')
    return number


def parse_hours(value):
    try:
        number = int(str(value).strip())
    except (ValueError, TypeError):
        raise ValueError('أرسل عدد الساعات بالأرقام.') from None
    if not 1 <= number <= 720:
        raise ValueError('المدة المسموحة من ساعة واحدة إلى 720 ساعة (30 يوماً).')
    return number


def install(ns, db, subject):
    if subject not in ('biology', 'english'):
        raise ValueError('Unsupported subject')
    B, K = ns['InlineKeyboardButton'], ns['InlineKeyboardMarkup']
    admin = ns['is_admin']
    label = 'الأحياء' if subject == 'biology' else 'الإنكليزي'
    T = subject
    TelegramError = ns['TelegramError']

    def kb(rows):
        return K(rows)

    def nav():
        return kb([[B('👥 إدارة الطلاب', callback_data='v8|students'),
                    B('🏠 مركز الإدارة', callback_data='v8|home')]])

    def migrate():
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(f'''CREATE TABLE IF NOT EXISTS {T}_v9_deleted_task_warnings (
                task_id INTEGER NOT NULL REFERENCES {T}_tasks(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES {T}_students(user_id) ON DELETE CASCADE,
                deleted_by BIGINT NOT NULL DEFAULT 0,
                deleted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(task_id,user_id));
                CREATE INDEX IF NOT EXISTS {T}_v9_deleted_warning_user_idx
                ON {T}_v9_deleted_task_warnings(user_id);''')
            conn.commit()

    previous_post = ns['post_init']

    async def post_init(app):
        await previous_post(app)
        await db.run(migrate)
        LOG.info('V9 %s warning tombstones and activation controls ready', T)

    ns['post_init'] = post_init

    async def activation_still_due(uid):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''SELECT EXISTS (
                    SELECT 1 FROM {T}_observed_members o
                    LEFT JOIN {T}_students s ON s.user_id=o.user_id
                    WHERE o.user_id=%s AND o.removed=FALSE
                    AND o.activation_deadline<=CURRENT_TIMESTAMP
                    AND (s.user_id IS NULL OR s.approved=FALSE OR s.parent_chat_id IS NULL)
                    AND NOT (COALESCE(s.approved,FALSE) AND COALESCE(s.admin_manual_active,FALSE))
                ) AS due;''', (int(uid),))
                return bool(cur.fetchone()['due'])
        return await db.run(op)

    db.v9_activation_still_due = activation_still_due

    async def extend_activation(uid, hours, actor):
        uid, hours, actor = parse_user_id(uid), parse_hours(hours), int(actor)
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                # Only extend people already known to this bot; do not create
                # arbitrary records for IDs that have never interacted.
                cur.execute(f'''SELECT EXISTS (
                    SELECT 1 FROM {T}_students WHERE user_id=%s
                    UNION ALL SELECT 1 FROM {T}_observed_members WHERE user_id=%s
                    UNION ALL SELECT 1 FROM {T}_start_leads WHERE user_id=%s
                ) AS known;''', (uid,uid,uid))
                if not cur.fetchone()['known']:
                    return {'status': 'unknown'}
                cur.execute(f'''INSERT INTO {T}_observed_members
                    (user_id,activation_deadline,removed,removed_at)
                    VALUES (%s,CURRENT_TIMESTAMP+(%s * INTERVAL '1 hour'),FALSE,NULL)
                    ON CONFLICT(user_id) DO UPDATE SET
                    activation_deadline=GREATEST(CURRENT_TIMESTAMP,{T}_observed_members.activation_deadline)
                        +(%s * INTERVAL '1 hour'),
                    removed=FALSE,removed_at=NULL
                    RETURNING activation_deadline;''', (uid,hours,hours))
                row = cur.fetchone()
                cur.execute(f'''INSERT INTO {T}_audit(actor_id,action,details)
                    VALUES(%s,'v9_activation_extended',%s);''',
                    (actor, f'user_id={uid};hours={hours}'))
                conn.commit()
                return {'status':'ok','deadline':row['activation_deadline']}
        return await db.run(op)

    db.v9_extend_activation = extend_activation

    async def enroll_by_id(uid, actor):
        uid = parse_user_id(uid)
        if admin(uid):
            return {'status': 'admin'}
        student = await db.get_student(uid)
        if student and student.get('status') == 'parent_account':
            return {'status': 'parent'}
        if not student:
            # An existing Start lead gives a useful display name. Never
            # overwrite a registered student's existing profile with placeholders.
            def lead_op():
                with db.connect() as conn, conn.cursor() as cur:
                    cur.execute(f'SELECT display_name,username FROM {T}_start_leads WHERE user_id=%s;', (uid,))
                    return cur.fetchone()
            lead = await db.run(lead_op)
            name = (lead or {}).get('display_name') or f'طالب {uid}'
            name = name[:120]
            student = await db.register_student(uid, (lead or {}).get('username'),
                                                name, 'يحدّدها الطالب لاحقاً', '0')
            if not student or student.get('status') == 'parent_account':
                return {'status': 'parent'}
        from admin_student_access import change_access
        updated = await change_access(db, uid, 'activate', actor)
        if not updated:
            return {'status':'missing'}
        # Prevent the group-expiry worker from treating manual activation as overdue.
        await db.mark_member_compliant(uid)
        cache_var = ns.get('_V51_STUDENT_CACHE')
        if cache_var:
            cache = cache_var.get()
            if cache is not None:
                cache.pop(uid, None)
        return {'status':'ok','student':updated}

    db.v9_enroll_by_id = enroll_by_id

    async def notify(bot, uid, text):
        try:
            await bot.send_message(uid, text)
            return True
        except TelegramError as exc:
            LOG.warning('V9 student notice not delivered subject=%s user=%s: %s',T,uid,exc)
            return False

    previous_button = ns['button_handler']

    async def button(update, context):
        q = update.callback_query
        data = str(q.data or '')
        if not data.startswith('v9|'):
            if data == 'menu' or data.startswith('v8|'):
                context.user_data.pop('v9_admin_input',None)
            return await previous_button(update, context)
        if not admin(q.from_user.id):
            await q.answer('هذا الزر للإدارة فقط.',show_alert=True)
            return
        if getattr(q.message.chat, 'type', 'private') != 'private':
            await q.answer('افتح خاص البوت لتنفيذ هذه العملية.',show_alert=True)
            return
        parts = data.split('|')
        action = parts[1] if len(parts)>1 else ''
        try:
            if action in ('add','extend') and len(parts)==2:
                context.user_data['v9_admin_input'] = {
                    'action':action, 'stage':'id', 'started':datetime.now(timezone.utc)}
                await q.answer()
                await q.edit_message_text(
                    '➕ إضافة طالب للدورة وتفعيل البوت\nأرسل آيدي الطالب الرقمي. لن تُحذف بياناته السابقة.'
                    if action=='add' else
                    '⏳ تمديد مهلة تفعيل الطالب\nأرسل آيدي الطالب الذي انتهت مهلته أو يحتاج وقتاً إضافياً.',
                    reply_markup=kb([[B('❌ إلغاء',callback_data='v9|cancel')]]))
                return
            if action=='cancel' and len(parts)==2:
                context.user_data.pop('v9_admin_input',None)
                await q.answer()
                await q.edit_message_text('تم إلغاء العملية.',reply_markup=nav())
                return
            if action=='add_do' and len(parts)==3:
                uid = parse_user_id(parts[2])
                result = await enroll_by_id(uid,q.from_user.id)
                if result['status']!='ok':
                    await q.answer('تعذر التفعيل: حساب أدمن أو ولي أمر أو معرّف غير صالح.',show_alert=True)
                    return
                sent = await notify(context.bot,uid,
                    f'✅ أضافتك الإدارة إلى دورة {label} وفعّلت حسابك. أرسل /start لاختيار المسار.')
                student=result['student']
                await q.answer('تم تفعيل حساب الطالب.',show_alert=True)
                await q.edit_message_text(
                    f'✅ حساب الطالب مفعل في بوت {label}\n'
                    f'👤 {student.get("full_name") or uid}\n🆔 {uid}\n'
                    f'🔑 كود ولي الأمر: {student.get("parent_link_code") or "غير متوفر"}\n'
                    + ('📩 وصل إشعار التفعيل للطالب.' if sent else
                       '⚠️ لم يصل الإشعار؛ لازم الطالب يفتح البوت ويضغط /start أولاً.') +
                    '\nℹ️ إضافة الحساب لا تعني إدخاله تلقائياً إلى مجموعة تيليغرام.',
                    reply_markup=nav())
                return
            if action=='extend_choose' and len(parts)==3:
                uid = parse_user_id(parts[2])
                rows = [[B('24 ساعة',callback_data=f'v9|extend_do|{uid}|24'),
                         B('48 ساعة',callback_data=f'v9|extend_do|{uid}|48')],
                        [B('72 ساعة',callback_data=f'v9|extend_do|{uid}|72'),
                         B('7 أيام',callback_data=f'v9|extend_do|{uid}|168')],
                        [B('✍️ مدة مخصصة بالساعات',callback_data=f'v9|custom|{uid}')],
                        [B('❌ إلغاء',callback_data='v9|cancel')]]
                await q.answer()
                await q.edit_message_text(f'⏳ الطالب: {uid}\nاختر مقدار الوقت الإضافي:',reply_markup=kb(rows))
                return
            if action=='custom' and len(parts)==3:
                uid = parse_user_id(parts[2])
                context.user_data['v9_admin_input']={
                    'action':'extend','stage':'hours','uid':uid,'started':datetime.now(timezone.utc)}
                await q.answer()
                await q.edit_message_text('✍️ أرسل عدد الساعات الإضافية (1 إلى 720).',
                    reply_markup=kb([[B('❌ إلغاء',callback_data='v9|cancel')]]))
                return
            if action=='extend_do' and len(parts)==4:
                uid, hours = parse_user_id(parts[2]), parse_hours(parts[3])
                result = await extend_activation(uid,hours,q.from_user.id)
                if result['status']=='unknown':
                    await q.answer('هذا الآيدي غير معروف للبوت. اطلب منه /start أو أضفه من الإدارة.',show_alert=True)
                    return
                when=result['deadline'].astimezone(ns['TIMEZONE']).strftime('%Y/%m/%d %H:%M')
                sent=await notify(context.bot,uid,
                    f'⏳ منحتك إدارة دورة {label} مهلة إضافية للتفعيل. الموعد الجديد: {when}. أرسل /start.')
                await q.answer('تم تمديد المهلة.',show_alert=True)
                await q.edit_message_text(f'✅ تم تمديد مهلة الطالب {uid} بمقدار {hours} ساعة.\n'
                    f'⏰ آخر موعد: {when}\n'
                    + ('📩 أُبلغ الطالب.' if sent else '⚠️ لم يصل إشعار خاص؛ اطلب من الطالب فتح البوت.')
                    + '\nإذا كان خارج مجموعة الدورة، يجب أن ينضم مجدداً عبر رابط المجموعة.',
                    reply_markup=nav())
                return
            await q.answer('زر غير معروف. ارجع إلى إدارة الطلاب.',show_alert=True)
        except (ValueError,IndexError) as exc:
            await q.answer(str(exc),show_alert=True)
        except Exception:
            LOG.exception('V9 admin callback failed: %s',data)
            await q.answer('تعذّر التنفيذ. لم أعتبر العملية ناجحة؛ راجع سجل التشغيل.',show_alert=True)

    ns['button_handler']=button
    previous_private=ns['private_messages']

    async def private_messages(update,context):
        state=context.user_data.get('v9_admin_input')
        if not state or not admin(update.effective_user.id):
            return await previous_private(update,context)
        msg=update.effective_message
        if getattr(update.effective_chat,'type','private')!='private':
            return await previous_private(update,context)
        if datetime.now(timezone.utc)-state.get('started',datetime.now(timezone.utc))>SESSION_TTL:
            context.user_data.pop('v9_admin_input',None)
            await msg.reply_text('⏳ انتهت مهلة الإدخال. افتح إدارة الطلاب وابدأ من جديد.',reply_markup=nav())
            return
        raw=(getattr(msg,'text',None) or '').strip()
        try:
            if state['stage']=='id':
                uid=parse_user_id(raw)
                context.user_data.pop('v9_admin_input',None)
                if admin(uid):
                    await msg.reply_text('لا يمكن إضافة أو تمديد حساب أدمن.',reply_markup=nav())
                    return
                if state['action']=='add':
                    student=await db.get_student(uid)
                    name=(student or {}).get('full_name') or 'طالب جديد'
                    await msg.reply_text(
                        f'➕ تأكيد تفعيل الطالب في دورة {label}\n👤 {name}\n🆔 {uid}\n'
                        'سيُفعّل الحساب، دون تغيير بياناته أو حذف سجلاته القديمة.\n'
                        'ملاحظة: لا يمكن للبوت إجبار الطالب على دخول مجموعة تيليغرام.',
                        reply_markup=kb([[B('✅ تأكيد الإضافة والتفعيل',callback_data=f'v9|add_do|{uid}')],
                                         [B('❌ إلغاء',callback_data='v9|cancel')]]))
                else:
                    await msg.reply_text(f'⏳ اختر مدة التمديد للطالب {uid}:',
                        reply_markup=kb([[B('⏳ اختيار المدة',callback_data=f'v9|extend_choose|{uid}')],
                                         [B('❌ إلغاء',callback_data='v9|cancel')]]))
                return
            if state['stage']=='hours':
                hours=parse_hours(raw)
                uid=parse_user_id(state['uid'])
                context.user_data.pop('v9_admin_input',None)
                await msg.reply_text(f'⏳ تأكيد تمديد الطالب {uid} بمقدار {hours} ساعة:',
                    reply_markup=kb([[B('✅ تأكيد التمديد',callback_data=f'v9|extend_do|{uid}|{hours}')],
                                     [B('❌ إلغاء',callback_data='v9|cancel')]]))
                return
        except ValueError as exc:
            await msg.reply_text(f'⚠️ {exc}')
            return
        return await previous_private(update,context)

    ns['private_messages']=private_messages
