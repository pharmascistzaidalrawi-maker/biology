"""Unified, compact administrator navigation for Biology and Biology bots.

Installed last, after legacy UI decorators. Preserves existing callback routes,
while consolidating entry points into one unambiguous administration hub.
Historical quick-review records are preserved, but their UI is retired.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger(__name__)


def install(ns, db, subject):
    if subject not in {'biology', 'biology'}:
        raise ValueError('Unsupported bot')
    B, K = ns['InlineKeyboardButton'], ns['InlineKeyboardMarkup']
    is_admin = ns['is_admin']
    label = 'الأحياء' if subject == 'biology' else 'الأحياء'

    def kb(rows):
        return K(rows)

    def b(title, route, **kw):
        return B(title, callback_data=route, **kw)

    def home():
        # One entry point for each action; no separate duplicate "advanced hub".
        return kb([
            [b('👥 إدارة الدورة', 'v7|course')],
            [b('📝 الامتحانات', 'v7|exams'), b('📚 الواجبات', 'hw|list|0')],
            [b('🏫 مراجعة المدرسة', 'v7|school')],
            [b('📖 الملازم والملخصات', 'respub|menu')],
            [b('🎬 التحاضير والمحاضرات', 'prep_schedule')],
            [b('⚙️ الإعدادات والصيانة', 'v7|settings')],
            [b('🔔 الإشعارات', 'notifications')],
        ])

    old_menu = ns['main_menu']
    def main_menu(admin=False):
        return home() if admin else old_menu(False)
    ns['main_menu'] = main_menu

    async def show(q, title, rows):
        await q.edit_message_text(f'{title}\n━━━━━━━━━━━━━━━━━━\nكل إجراء موجود في قسم واحد فقط.', reply_markup=kb(rows + [[b('🏠 الرئيسية', 'menu')]]))

    def course():
        return [
            [b('👥 الطلاب والتفعيل', 'v7|students'), b('👪 أولياء الأمور', 'admin_parents')],
            [b('🗓 تعديل جدول الدورة', 'ctl_calendar')],
            [b('⚠️ الإنذارات والعقوبات', 'v43_warnings')],
            [b('👁 معاينة حساب طالب', 'admin_preview_list|0')],
        ]

    def students():
        return [
            [b('👥 قائمة الطلاب', 'admin_students'), b('🆕 طلبات Start', 'bio6|leads|0')],
            [b('🎯 صلاحيات طالب / وضع مستمع', 'bio6|choose')],
        ]

    def exams():
        return [
            [b('📝 الامتحانات المنشورة وإدارتها', 'v42_admin_exams')],
            [b('➕ امتحان الدورة', 'v53_course_exam_start'), b('📘 امتحان الفصول', 'v53_chapter_exam_start')],
            [b('🏆 امتحان تراكمي', 'v53_course_cumulative_start')],
            [b('📅 الامتحانات الأسبوعية', 'edu_weeks')],
            [b('🗑 حذف امتحان نهائياً', 'admin_exam_delete_menu')],
            [b('🔗 ربط الامتحان بالتحضير', 'bio6|linkpreps|0')],
            [b('🗓 المنشورات المجدولة', 'scheduled_list')],
        ]

    def school():
        return [
            [b('🏫 إدارة مراجعة المدرسة', 'v54_school_admin')],
            [b('📄 ملفات PDF وامتحاناتها', 'pdf|list|0')],
            [b('📌 تحديد توبك المراجعة', 'bio6|topic')],
        ]

    def settings():
        return [
            [b('🔄 طلبات تغيير المسارات', 'track_change_requests')],
            [b('📦 حجم قاعدة البيانات', 'v50_db_storage')],
            [b('🧹 تنظيف الإجابات', 'v49_cleanup')],
        ]

    # Keep students' old menu but remove the retired quick review actions even
    # if a nested renderer or an older cache still inserts them.
    QUICK_CALLBACKS = {'v48_quick_admin', 'v48_quick_reviews', 'v48qadd'}
    def is_quick(data):
        return data in QUICK_CALLBACKS or data.startswith('v48q')

    def without_quick(markup):
        if markup is None or not hasattr(markup, 'inline_keyboard'):
            return markup
        rows = []
        for row in markup.inline_keyboard:
            cleaned = [btn for btn in row if not is_quick(str(getattr(btn, 'callback_data', '') or ''))]
            if cleaned:
                rows.append(cleaned)
        return kb(rows) if rows != [list(r) for r in markup.inline_keyboard] else markup

    # Remove retired review questions from student screens as well. A wrapper
    # applies to all old UI renderers, not just the top-level menu.
    old_review = ns.get('v51_review_menu')
    if old_review:
        class FilterQuery:
            def __init__(self, original):
                self.original = original
            async def edit_message_text(self, text, *args, **kwargs):
                kwargs['reply_markup'] = without_quick(kwargs.get('reply_markup'))
                return await self.original.edit_message_text(text, *args, **kwargs)
            def __getattr__(self, key):
                return getattr(self.original, key)
        async def review(q, *args, **kwargs):
            return await old_review(FilterQuery(q), *args, **kwargs)
        ns['v51_review_menu'] = review

    # Store codes for /start leads without creating a partially registered
    # student, which would interrupt the existing onboarding conversation.
    old_post = ns['post_init']
    async def post_init(app):
        await old_post(app)
        def migrate():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''ALTER TABLE {subject}_start_leads
                    ADD COLUMN IF NOT EXISTS reserved_parent_code TEXT;
                    CREATE UNIQUE INDEX IF NOT EXISTS {subject}_lead_parent_code_uq
                    ON {subject}_start_leads(reserved_parent_code)
                    WHERE reserved_parent_code IS NOT NULL;
                    CREATE TABLE IF NOT EXISTS {subject}_v7_start_notices(
                    user_id BIGINT PRIMARY KEY, last_sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);''')
                conn.commit()
        await db.run(migrate)
    ns['post_init'] = post_init

    async def ensure_parent_code(uid):
        def work():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'SELECT parent_link_code FROM {subject}_students WHERE user_id=%s;', (uid,))
                student = cur.fetchone()
                if student and student.get('parent_link_code'):
                    return student['parent_link_code'], True
                cur.execute(f'''INSERT INTO {subject}_start_leads(user_id,display_name)
                    VALUES(%s,'') ON CONFLICT(user_id) DO NOTHING;''', (uid,))
                cur.execute(f'SELECT reserved_parent_code FROM {subject}_start_leads WHERE user_id=%s FOR UPDATE;', (uid,))
                lead = cur.fetchone()
                if lead and lead.get('reserved_parent_code'):
                    return lead['reserved_parent_code'], False
                for _ in range(12):
                    code = secrets.token_hex(5).upper()
                    cur.execute(f'''SELECT 1 FROM {subject}_students WHERE parent_link_code=%s
                        UNION SELECT 1 FROM {subject}_start_leads WHERE reserved_parent_code=%s LIMIT 1;''', (code, code))
                    if cur.fetchone():
                        continue
                    cur.execute(f'UPDATE {subject}_start_leads SET reserved_parent_code=%s WHERE user_id=%s;', (code, uid))
                    conn.commit()
                    return code, False
                raise RuntimeError('Could not reserve a unique parent code')
        return await db.run(work)
    db.v7_ensure_parent_code = ensure_parent_code

    old_register = ns['register_student']
    async def register_student(*args, **kwargs):
        uid = int(args[0] if args else kwargs['user_id'])
        previous = await db.get_student(uid)
        student = await old_register(*args, **kwargs)
        if not student or student.get('status') == 'parent_account' or previous:
            return student
        def work():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'SELECT reserved_parent_code FROM {subject}_start_leads WHERE user_id=%s;', (uid,))
                lead = cur.fetchone()
                code = (lead or {}).get('reserved_parent_code')
                if not code:
                    return None
                cur.execute(f'''UPDATE {subject}_students SET parent_link_code=%s
                    WHERE user_id=%s AND NOT EXISTS(SELECT 1 FROM {subject}_students
                    WHERE parent_link_code=%s AND user_id<>%s) RETURNING *;''', (code, uid, code, uid))
                row = cur.fetchone()
                conn.commit()
                return row
        updated = await db.run(work)
        return updated or student
    db.register_student = register_student
    ns['register_student'] = register_student

    # Student /start notices are now handled only by start_role_routing.py,
    # and are delivered exclusively to the activation group. No private alerts.
    old_start = ns['start']
    async def start(update, context):
        return await old_start(update, context)
    ns['start'] = start

    old_button = ns['button_handler']
    async def button(update, context):
        q = update.callback_query
        data = str(q.data or '')
        if is_admin(q.from_user.id) and (data in ('menu', 'bio6|hub', 'v7|home') or data.startswith('v7|')):
            # Leaving a real/read-only student session must not carry its
            # identity or pending text edit into a new administrator action.
            for key in ('control_session', 'control_input', 'control_rebuild',
                        'preview_student', 'bio6_input', 'hw_edit', 'resource_publish_draft'):
                context.user_data.pop(key, None)
        if is_quick(data):
            context.user_data.pop('v48_quick_add', None)
            await q.answer('تم إيقاف نظام أسئلة المراجعة القديم.', show_alert=True)
            return
        if data in ('bio6|hub', 'v7|home'):
            if not is_admin(q.from_user.id):
                await q.answer('للإدارة فقط.', show_alert=True)
                return
            await q.answer()
            await q.edit_message_text(f'👑 مركز إدارة {label}\n━━━━━━━━━━━━━━━━━━\nواجهة مختصرة، بدون أقسام مكررة.', reply_markup=home())
            return
        if data.startswith('v7|'):
            if not is_admin(q.from_user.id):
                await q.answer('للإدارة فقط.', show_alert=True)
                return
            await q.answer()
            if data == 'v7|course':
                return await show(q, '👥 إدارة الدورة', course())
            if data == 'v7|students':
                return await show(q, '👥 إدارة الطلاب والتفعيل', students())
            if data == 'v7|exams':
                return await show(q, '📝 إدارة الامتحانات', exams())
            if data == 'v7|school':
                return await show(q, '🏫 إدارة مراجعة المدرسة', school())
            if data == 'v7|settings':
                return await show(q, '⚙️ الإعدادات والصيانة', settings())
            return await q.answer('قسم غير معروف.', show_alert=True)
        if data == 'menu' and is_admin(q.from_user.id):
            await q.answer()
            await q.edit_message_text(f'👑 مركز إدارة {label}\n━━━━━━━━━━━━━━━━━━\nاختر القسم المطلوب:', reply_markup=home())
            return
        return await old_button(update, context)
    ns['button_handler'] = button

    old_private = ns['private_messages']
    async def private_messages(update, context):
        context.user_data.pop('v48_quick_add', None)
        return await old_private(update, context)
    ns['private_messages'] = private_messages
    ns['_v7_admin_menu'] = home
    ns['_v7_admin_sections'] = {'course': course, 'students': students, 'exams': exams,
                                'school': school, 'settings': settings}
