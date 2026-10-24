"""Route Telegram /start by explicit student/parent role.

- Parents never create start leads or activation notifications.
- New students opt in before a notification is sent to ACTIVATION_GROUP_ID.
- One notification per student account, with database-backed deduplication.
- No fallback to an administrator's private messages or to General.

Install this module after the existing admin/start decorators.
"""
import logging
from datetime import datetime, timezone

LOG = logging.getLogger(__name__)


def install(ns, db, subject):
    if subject not in {'biology', 'biology', 'biology', 'mathematics', 'physics', 'french', 'islamic'}:
        raise ValueError('Unknown bot subject')
    B = ns['InlineKeyboardButton']
    K = ns['InlineKeyboardMarkup']
    END = ns['ConversationHandler'].END
    is_admin = ns['is_admin']
    old_start = ns['start']
    old_button = ns['button_handler']
    label = {'biology':'الأحياء', 'biology':'الأحياء', 'biology':'الأحياء',
             'mathematics':'الرياضيات', 'physics':'الفيزياء',
             'french':'الفرنسية', 'islamic':'التربية الإسلامية'}[subject]
    table = f'{subject}_start_role_notifications'

    def migrate():
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
                user_id BIGINT PRIMARY KEY,
                claimed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMPTZ
            );''')
            conn.commit()

    previous_post = ns['post_init']
    async def post_init(app):
        await previous_post(app)
        await db.run(migrate)
    ns['post_init'] = post_init

    async def linked_parent(uid):
        # Includes pending and approved parent links. Parent role is permanent
        # unless the user explicitly deletes the parent account.
        return bool(await db.students_by_parent(uid, False))

    async def claim(uid):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''INSERT INTO {table}(user_id) VALUES(%s)
                    ON CONFLICT(user_id) DO UPDATE SET claimed_at=CURRENT_TIMESTAMP
                    WHERE {table}.sent_at IS NULL
                    AND {table}.claimed_at<CURRENT_TIMESTAMP-INTERVAL '2 minutes'
                    RETURNING user_id;''', (uid,))
                got = bool(cur.fetchone())
                conn.commit()
                return got
        return await db.run(op)

    async def finish_claim(uid, sent):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                if sent:
                    cur.execute(f'''UPDATE {table} SET sent_at=CURRENT_TIMESTAMP
                        WHERE user_id=%s;''', (uid,))
                else:
                    # A failed Telegram send may be retried on the next /start.
                    cur.execute(f'''DELETE FROM {table} WHERE user_id=%s AND sent_at IS NULL;''', (uid,))
                conn.commit()
        await db.run(op)

    async def notify_student(update, context, student):
        uid = update.effective_user.id
        group_id = int(ns.get('ACTIVATION_GROUP_ID') or 0)
        if group_id >= 0:
            LOG.error('ACTIVATION_GROUP_ID must be a negative group chat ID for %s; no private fallback', subject)
            return False
        if not await claim(uid):
            return True  # Previously sent, or an in-flight attempt.
        sent = False
        try:
            code = (student or {}).get('parent_link_code')
            if not code and hasattr(db, 'v7_ensure_parent_code'):
                # Existing V7 feature reserves a parent code for new students.
                code, _active = await db.v7_ensure_parent_code(uid)
            user = update.effective_user
            lines = [f'🎓 طالب بدأ بوت {label}',
                     f'👤 {user.full_name or user.username or uid}',
                     f'🆔 {uid}',
                     f'📎 @{user.username}' if user.username else '📎 بدون اسم مستخدم',
                     f'🔑 كود ولي الأمر: {code}' if code else '🔑 كود ولي الأمر يظهر بعد التسجيل',
                     '📌 الإشعار من بوت المادة — الطالب فقط']
            # For V9 bots, the group moderator can open the existing lead panel.
            prefix = {'biology':'bio6', 'biology':'bio6', 'biology':'bio6',
                      'mathematics':'math6'}.get(subject)
            markup = K([[B('👤 عرض طلب الطالب', callback_data=f'{prefix}|lead|{uid}')]]) if prefix else None
            # No send to OWNER_CHAT_ID, ADMIN_IDS, or General.
            await context.bot.send_message(group_id, '\n'.join(lines), reply_markup=markup)
            sent = True
            return True
        except Exception:
            LOG.exception('Could not send student start notice to activation group (%s)', subject)
            return False
        finally:
            await finish_claim(uid, sent)

    async def start(update, context):
        user = update.effective_user
        if not user or is_admin(user.id):
            return await old_start(update, context)
        student = await db.get_student(user.id)
        if await linked_parent(user.id):
            context.user_data.pop('start_role', None)
            return await old_start(update, context)
        if not student and context.user_data.get('start_role') != 'student':
            # Unknown people cannot be distinguished as parents or students by
            # Telegram. Ask once; never label them students on /start alone.
            await update.effective_message.reply_text(
                f'أهلاً بك في بوت {label} 👋\nاختر نوع حسابك:',
                reply_markup=K([
                    [B('🎓 أنا طالب', callback_data='start_role|student')],
                    [B('👨‍👩‍👦 أنا ولي أمر', callback_data='start_role|parent')],
                ]))
            return END
        if not student or (not student.get('approved') and not student.get('reset_pending')):
            await notify_student(update, context, student)
        return await old_start(update, context)

    async def role_choice_entry(update, context):
        query = update.callback_query
        if not query:
            return END
        await query.answer()
        uid = query.from_user.id
        if is_admin(uid):
            return await old_start(update, context)
        if await linked_parent(uid):
            context.user_data.pop('start_role', None)
            return await old_start(update, context)
        role = (query.data or '').split('|')[-1]
        if role == 'parent':
            context.user_data['start_role'] = 'parent'
            # Remove a stale lead created by an earlier release. Only delete
            # unregistered leads; never touch student records or their codes.
            if subject in {'biology', 'biology', 'biology', 'mathematics'}:
                def drop_stale_lead():
                    with db.connect() as conn, conn.cursor() as cur:
                        cur.execute(f'''DELETE FROM {subject}_start_leads
                            WHERE user_id=%s AND registered_at IS NULL
                            AND NOT EXISTS (SELECT 1 FROM {subject}_students WHERE user_id=%s);''', (uid, uid))
                        conn.commit()
                await db.run(drop_stale_lead)
            await query.message.reply_text(
                '👨‍👩‍👦 أهلاً بولي الأمر. ما تحتاج تفعيل حساب طالب.\n'
                'حتى تربط حسابك بابنك، اطلب منه كود ولي الأمر وأرسله بهذا الشكل:\n'
                '/parent ABCD1234\n\n'
                'بعد الربط تقدر تضغط /start لفتح واجهة ولي الأمر.')
            return END
        if role != 'student':
            await query.message.reply_text('اختيار غير معروف. أرسل /start مرة أخرى.')
            return END
        context.user_data['start_role'] = 'student'
        # Run the normal start handler inside ConversationHandler so REG_NAME,
        # REG_SCHOOL, etc. continue to work after pressing the student button.
        return await ns['start'](update, context)

    async def button_handler(update, context):
        # ConversationHandler handles role buttons in group=0. Ignore the same
        # callback in group=1 to avoid duplicate messages and unexpected edits.
        if update.callback_query and (update.callback_query.data or '').startswith('start_role|'):
            return
        return await old_button(update, context)

    ns['start'] = start
    ns['role_choice_entry'] = role_choice_entry
    ns['button_handler'] = button_handler
