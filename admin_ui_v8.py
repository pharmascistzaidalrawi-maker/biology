"""V8: single administrator navigation, routed to the existing working features.

Do not replace the legacy exam/homework implementations. Only centralize their
entry points, repair old V7 links, and expose read-only deployment diagnostics.
"""
import logging

LOG = logging.getLogger(__name__)


# One canonical destination per operation. Subject-specific links are built in
# make_sections(), rather than copying Biology callback IDs to English.
def make_sections(subject):
    if subject not in {'biology', 'english'}:
        raise ValueError('Unsupported subject')
    prefix = 'bio6' if subject == 'biology' else 'eng6'
    chapter = 'الفصول' if subject == 'biology' else 'اليونتات'
    sections = {
        'home': [
            [('👥 الدورة والطلاب', 'v8|course'), ('📝 الامتحانات', 'v8|exams')],
            [('📚 الواجبات', 'hw|list|0'), ('🏫 مراجعة المدرسة', 'v8|school')],
            [('📖 الملازم والملخصات', 'respub|menu'), ('🎬 التحاضير', 'prep_schedule')],
            [('⚙️ الإعدادات والفحص', 'v8|settings')],
        ],
        'course': [
            [('👥 إدارة الطلاب', 'v8|students'), ('👪 أولياء الأمور', 'admin_parents')],
            [('🗓 جدول الدورة', 'ctl_calendar'), ('⚠️ الإنذارات', 'v43_warnings')],
            [('👁 معاينة حساب طالب', 'admin_preview_list|0')],
        ],
        'students': [
            [('📋 الطلاب المسجلون', 'admin_students')],
            [('🆕 طلبات Start', f'{prefix}|leads|0')],
            [('🎯 الصلاحيات ووضع المستمع', f'{prefix}|choose')],
            [('➕ إضافة طالب بالآيدي', 'v9|add'), ('⏳ تمديد مهلة التفعيل', 'v9|extend')],
        ],
        'exams': [
            [('📋 الامتحانات المنشورة', 'v42_admin_exams')],
            [('➕ نشر امتحان جديد', 'v8|exam_publish')],
            [('📅 الامتحانات الأسبوعية', 'edu_weeks'), ('🗓 المجدولة', 'scheduled_list')],
            [('🔗 ربط الامتحان بالتحضير', f'{prefix}|linkpreps|0')],
            [('🗑 حذف امتحان نهائياً', 'admin_exam_delete_menu')],
        ],
        'exam_publish': [
            [('👥 امتحان الدورة الحالية', 'v53_course_exam_start')],
            [(f'📘 امتحان {chapter}', 'v53_chapter_exam_start')],
            [('🏆 امتحان تراكمي', 'v53_course_cumulative_start')],
        ],
        'school': [
            [('🏫 إدارة مراجعة المدرسة', 'v54_school_admin')],
            [('📄 ملفات PDF وامتحانات 48 ساعة', 'pdf|list|0')],
            [('📌 تحديد توبك المراجعة', f'{prefix}|topic')],
        ],
        'settings': [
            [('🔄 طلبات تغيير المسارات', 'track_change_requests'), ('🔔 الإشعارات', 'notifications')],
            [('📦 حجم قاعدة البيانات', 'v50_db_storage'), ('🧹 تنظيف الإجابات', 'v49_cleanup')],
            [('🩺 فحص جاهزية النظام', 'v8|health')],
        ],
    }
    if subject == 'english':
        sections['settings'].insert(2, [('🧵 فحص توبكات حلول الطلاب', 'eng6|topiccheck')])
    return sections


TITLES = {
    'home': ('👑 مركز الإدارة', 'اختر القسم المطلوب. كل أداة موجودة بمكان واحد.'),
    'course': ('👥 إدارة الدورة', 'الطلاب، أولياء الأمور، الجدول والإنذارات.'),
    'students': ('👤 إدارة الطلاب', 'التفعيل، طلبات Start، والصلاحيات الفردية.'),
    'exams': ('📝 الامتحانات', 'النشر، الإدارة، الامتحانات الأسبوعية والحذف.'),
    'exam_publish': ('➕ نشر امتحان', 'اختر نوع الامتحان أولاً.'),
    'school': ('🏫 مراجعة المدرسة', 'المراجعات وملفات PDF وتوبك النشر.'),
    'settings': ('⚙️ الإعدادات', 'الأدوات التقنية والإشعارات وفحص الجاهزية.'),
}
PARENTS = {'course': 'home', 'students': 'course', 'exams': 'home',
           'exam_publish': 'exams', 'school': 'home', 'settings': 'home'}
ALIASES = {
    'v7|home': 'home', 'v7|course': 'course', 'v7|students': 'students',
    'v7|exams': 'exams', 'v7|school': 'school', 'v7|settings': 'settings',
    'admin_publish': 'exam_publish',
}


def install(ns, db, subject):
    sections = make_sections(subject)
    B, K = ns['InlineKeyboardButton'], ns['InlineKeyboardMarkup']
    is_admin = ns['is_admin']
    subject_name = 'الأحياء' if subject == 'biology' else 'الإنكليزي'
    old_menu = ns['main_menu']
    old_button = ns['button_handler']

    def keyboard(section):
        rows = [[B(text, callback_data=route) for text, route in row]
                for row in sections[section]]
        if section != 'home':
            rows.append([B('◀️ رجوع', callback_data=f'v8|{PARENTS[section]}'),
                         B('🏠 الرئيسية', callback_data='v8|home')])
        return K(rows)

    def main_menu(admin=False):
        return keyboard('home') if admin else old_menu(False)
    ns['main_menu'] = main_menu

    async def render(q, section):
        title, description = TITLES[section]
        text = f'{title} | {subject_name}\n━━━━━━━━━━━━━━━━\n{description}'
        message = getattr(q, 'message', None)
        # Editing a media caption as text is not supported by Telegram.
        if message is not None and not getattr(message, 'text', None):
            await message.reply_text(text, reply_markup=keyboard(section))
        else:
            try:
                await q.edit_message_text(text, reply_markup=keyboard(section))
            except ns['TelegramError'] as exc:
                if 'message is not modified' in str(exc).lower():
                    return
                if message is None:
                    raise
                await message.reply_text(text, reply_markup=keyboard(section))

    async def health(q, context):
        notes = []
        required = ('button_handler', 'private_messages', 'post_init',
                    '_resource_save_draft', '_homework_clear_extended_warnings')
        missing = [name for name in required if not callable(ns.get(name))]
        notes.append('🟢 وحدات التحكم محمّلة' if not missing else
                     '🔴 وحدات غير محمّلة: ' + ', '.join(missing))
        def check_database():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute('SELECT to_regclass(%s) AS resources, to_regclass(%s) AS tasks, '
                            'to_regclass(%s) AS modules, to_regclass(%s) AS deleted_warnings;',
                            (f'{subject}_resources', f'{subject}_tasks', f'{subject}_student_modules',
                             f'{subject}_v9_deleted_task_warnings'))
                return cur.fetchone()
        try:
            result = await db.run(check_database)
            for name, value in (result or {}).items():
                notes.append(('🟢 ' if value else '🔴 ') + f'جدول {name}: ' +
                             ('جاهز' if value else 'غير موجود'))
        except Exception as exc:
            LOG.exception('V8 database health check failed (%s)', subject)
            notes.append(f'🔴 تعذّر الاتصال بقاعدة البيانات: {type(exc).__name__}')
        topic_id = int(ns.get('SCHOOL_REVIEW_TOPIC_ID') or 0)
        notes.append(f"{'🟢' if topic_id else '🟠'} توبك مراجعة المدرسة: " +
                     (str(topic_id) if topic_id else 'لم يُحدد'))
        if subject == 'english':
            for key, label in (('EXAM_SUBMISSIONS_CHAT_ID', 'الامتحانات'),
                               ('HOMEWORK_SUBMISSIONS_CHAT_ID', 'الواجبات')):
                cid = int(ns.get(key) or 0)
                if cid >= 0:
                    notes.append(f'🔴 توبكات {label}: اضبط {key} على مجموعة Forum')
                    continue
                try:
                    chat = await context.bot.get_chat(cid)
                    me = await context.bot.get_me()
                    member = await context.bot.get_chat_member(cid, me.id)
                    ready = (getattr(chat, 'type', None) == 'supergroup'
                             and bool(getattr(chat, 'is_forum', False))
                             and bool(getattr(member, 'can_manage_topics', False)))
                    notes.append(('🟢' if ready else '🔴') +
                                 f' توبكات {label}: ' +
                                 ('جاهزة' if ready else 'تحتاج Forum وصلاحية إدارة المواضيع'))
                except Exception as exc:
                    LOG.warning('Could not inspect %s topic destination: %s', key, exc)
                    notes.append(f'🔴 توبكات {label}: تعذّر فحص المجموعة')
        text = ('🩺 فحص جاهزية بوت ' + subject_name + '\n━━━━━━━━━━━━━━━━\n' +
                '\n'.join(notes) + '\n\nهذا فحص إعدادات واتصال، وليس بديلاً عن تجربة النشر والتسليم الفعلية.')
        await q.edit_message_text(text, reply_markup=K([
            [B('🔄 إعادة الفحص', callback_data='v8|health')],
            [B('◀️ الإعدادات', callback_data='v8|settings')],
            [B('🏠 الرئيسية', callback_data='v8|home')],
        ]))

    async def fail(q, data, exc):
        LOG.exception('Admin callback failed subject=%s callback=%s user=%s',
                      subject, data, getattr(getattr(q, 'from_user', None), 'id', '?'),
                      exc_info=exc)
        message = getattr(q, 'message', None)
        error = '⚠️ تعذّر تنفيذ الزر. لم أعتبر العملية ناجحة. راجع سجل التشغيل أو أعد المحاولة.'
        try:
            if message:
                await message.reply_text(error, reply_markup=K([
                    [B('🏠 مركز الإدارة', callback_data='v8|home')]]))
            else:
                await q.answer(error, show_alert=True)
        except Exception:
            LOG.exception('Could not deliver callback failure notice')

    async def button(update, context):
        q = update.callback_query
        data = str(q.data or '')
        uid = q.from_user.id
        alias = ALIASES.get(data)
        if data in ('menu', 'bio6|hub', 'eng6|hub') and is_admin(uid):
            alias = 'home'
        if data.startswith('v8|') or alias:
            if not is_admin(uid):
                await q.answer('هذه الواجهة للإدارة فقط.', show_alert=True)
                return
            section = data.split('|', 1)[1] if data.startswith('v8|') else alias
            if section not in sections and section != 'health':
                await q.answer('هذا القسم غير موجود. افتح القائمة الرئيسية.', show_alert=True)
                return
            # Only navigation cancels incomplete editing sessions; all legacy
            # callbacks continue through their own handlers unchanged.
            for state in ('control_session', 'control_input', 'preview_student',
                          'hw_edit', 'hw_confirm', 'bio6_input', 'eng6_input',
                          'resource_publish_draft', 'v29_publish', 'linked_exam'):
                context.user_data.pop(state, None)
            try:
                await q.answer()
                if section == 'health':
                    await health(q, context)
                else:
                    await render(q, section)
            except Exception as exc:
                await fail(q, data, exc)
            return
        try:
            return await old_button(update, context)
        except Exception as exc:
            await fail(q, data, exc)
    ns['button_handler'] = button
    ns['_v8_sections'] = sections
    ns['_v8_keyboard'] = keyboard
    ns['_v8_health'] = health
