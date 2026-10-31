"""Real completed work reports and period XP rankings, without polling jobs."""
import asyncio
from contextvars import ContextVar
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Baghdad')
PERIODS = {'day': 'اليومي', 'week': 'الأسبوعي', 'month': 'الشهري'}
LEARNING_KEYS = r'^(submission|grade|prep|personal_prep|early_prep|school_review|exam_speed|weakness|review|lecture|quick|genetics|homework_teacher):'


def period_window(period, now=None):
    if period not in PERIODS:
        raise ValueError('Invalid period')
    now = (now or datetime.now(ZONE)).astimezone(ZONE)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = (midnight if period == 'day' else midnight - timedelta(days=(midnight.weekday()+1) % 7)
             if period == 'week' else midnight.replace(day=1))
    return start, now


def migrate(db, p):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT value FROM {p}_settings WHERE key='learning_insights_v1';")
        if cur.fetchone():
            return
        cur.execute(f'''ALTER TABLE {p}_xp_log ADD COLUMN IF NOT EXISTS earned_at TIMESTAMPTZ;
            ALTER TABLE {p}_xp_log ADD COLUMN IF NOT EXISTS competition_eligible BOOLEAN NOT NULL DEFAULT FALSE;
        ''')
        cur.execute(f'UPDATE {p}_xp_log SET earned_at=created_at,competition_eligible=COALESCE(event_key ~ %s,FALSE);',(LEARNING_KEYS,))
        cur.execute(f'''CREATE OR REPLACE FUNCTION {p}_insight_xp_time() RETURNS TRIGGER LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP='UPDATE' THEN NEW.earned_at=COALESCE(OLD.earned_at,OLD.created_at,CURRENT_TIMESTAMP);
                ELSE NEW.earned_at=COALESCE(NEW.earned_at,NEW.created_at,CURRENT_TIMESTAMP); END IF;
                NEW.competition_eligible=COALESCE(NEW.event_key ~ '{LEARNING_KEYS}',FALSE);
                RETURN NEW;
            END; $$;
            CREATE TRIGGER {p}_insight_xp_time BEFORE INSERT OR UPDATE ON {p}_xp_log
                FOR EACH ROW EXECUTE FUNCTION {p}_insight_xp_time();
            CREATE OR REPLACE FUNCTION {p}_insight_completion_time() RETURNS TRIGGER LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT NULL THEN
                    NEW.completed_at=OLD.completed_at;
                END IF;
                RETURN NEW;
            END; $$;
            CREATE TRIGGER {p}_insight_lecture_time BEFORE UPDATE ON {p}_lecture_progress
                FOR EACH ROW EXECUTE FUNCTION {p}_insight_completion_time();
            CREATE TRIGGER {p}_insight_review_time BEFORE UPDATE ON {p}_lecture_reviews
                FOR EACH ROW EXECUTE FUNCTION {p}_insight_completion_time();
            CREATE INDEX IF NOT EXISTS {p}_insight_earned_period ON {p}_xp_log(earned_at,user_id)
                WHERE competition_eligible=TRUE;
            CREATE INDEX IF NOT EXISTS {p}_insight_completed_period ON {p}_lecture_progress(user_id,completed_at)
                WHERE completed_at IS NOT NULL;
            CREATE INDEX IF NOT EXISTS {p}_insight_submitted_period ON {p}_submissions(user_id,submitted_at)
                WHERE submitted_at IS NOT NULL;
            INSERT INTO {p}_settings(key,value) VALUES('learning_insights_v1','1') ON CONFLICT DO NOTHING;
        ''')
        conn.commit()


def install(ns, db, p):
    Button, K = ns['InlineKeyboardButton'], ns['InlineKeyboardMarkup']
    def B(*args,**kwargs):
        kwargs.setdefault('style','primary')
        return Button(*args,**kwargs)
    old_init = ns['init_db']
    def init():
        old_init()
        migrate(db, p)
    ns['init_db'] = db.init_db = init

    async def activity(uid, start, end):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''SELECT chapter,lecture,completed_at AS at,completion_method
                    FROM {p}_lecture_progress WHERE user_id=%s AND completed_at>=%s AND completed_at<%s
                        AND COALESCE(completion_method,'')<>'prior_learning'
                    ORDER BY completed_at,chapter,lecture;''', (uid,start,end))
                lectures = cur.fetchall()
                cur.execute(f'''SELECT t.id,t.title,t.kind,s.submitted_at AS at,s.grade,
                    (t.catchup_source_id IS NOT NULL) AS historical FROM {p}_submissions s
                    JOIN {p}_tasks t ON t.id=s.task_id
                    WHERE s.user_id=%s AND s.submitted_at>=%s AND s.submitted_at<%s
                        AND s.rejected=FALSE AND s.revision_open=FALSE AND s.teacher_change_pending=FALSE
                    ORDER BY s.submitted_at,t.id;''', (uid,start,end))
                tasks = cur.fetchall()
                cur.execute(f'''SELECT chapter,lecture,stage,completed_at AS at FROM {p}_lecture_reviews
                    WHERE user_id=%s AND completed_at>=%s AND completed_at<%s
                        AND NOT EXISTS(SELECT 1 FROM {p}_lecture_progress lp WHERE lp.user_id={p}_lecture_reviews.user_id
                            AND lp.chapter={p}_lecture_reviews.chapter AND lp.lecture={p}_lecture_reviews.lecture
                            AND lp.completion_method='prior_learning') ORDER BY completed_at,id;''', (uid,start,end))
                reviews = cur.fetchall()
                cur.execute(f'''SELECT chapter,lecture,weakness_text,resolved_at AS at FROM {p}_weakness_points
                    WHERE user_id=%s AND resolved_at>=%s AND resolved_at<%s ORDER BY resolved_at,id;''', (uid,start,end))
                weaknesses = cur.fetchall()
                cur.execute(f'''SELECT r.week_label AS title,sp.completed_at AS at FROM {p}_school_review_progress sp
                    JOIN {p}_school_reviews r ON r.id=sp.review_id
                    WHERE sp.user_id=%s AND sp.completed_at>=%s AND sp.completed_at<%s ORDER BY sp.completed_at,r.id;''',(uid,start,end))
                school_reviews = cur.fetchall()
                cur.execute(f'''SELECT COALESCE(SUM(delta),0)::BIGINT AS earned FROM {p}_xp_log
                    WHERE user_id=%s AND earned_at>=%s AND earned_at<%s AND competition_eligible=TRUE;''', (uid,start,end))
                earned = int(cur.fetchone()['earned'])
                return dict(lectures=lectures,tasks=tasks,reviews=reviews,weaknesses=weaknesses,school_reviews=school_reviews,
                            earned=earned,start=start,end=end)
        return await db.run(op)
    db.learning_activity = activity

    report_cache=ContextVar('learning_report_cache',default=None)
    old_bundle = db.parent_report_bundle
    async def bundle(uid, start, end, previous_start):
        cache=report_cache.get();key=(uid,start,end,previous_start)
        if cache is not None and key in cache:return cache[key]
        result = await old_bundle(uid,start,end,previous_start)
        result['activity'] = await activity(uid,start,end)
        if cache is not None:cache[key]=result
        return result
    db.parent_report_bundle = ns['parent_report_bundle'] = bundle
    old_weekly=ns.get('weekly_reports_job')
    if old_weekly:
        async def weekly(context):
            token=report_cache.set({})
            try:return await old_weekly(context)
            finally:report_cache.reset(token)
        ns['weekly_reports_job']=weekly

    async def leaders(period, uid=None, page=0, now=None):
        start, end = period_window(period, now)
        admins = sorted(set(ns.get('ADMIN_IDS',set())) | set(ns.get('FOUNDER_IDS',set())) | {int(ns.get('OWNER_CHAT_ID') or 0)})
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''WITH points AS (
                    SELECT x.user_id,SUM(x.delta)::BIGINT AS earned FROM {p}_xp_log x
                    WHERE x.earned_at>=%s AND x.earned_at<%s AND x.competition_eligible=TRUE
                    GROUP BY x.user_id HAVING SUM(x.delta)>0
                ), ranked AS (
                    SELECT s.user_id,s.full_name,points.earned,
                        DENSE_RANK() OVER(ORDER BY points.earned DESC) AS place,
                        ROW_NUMBER() OVER(ORDER BY points.earned DESC,s.user_id) AS position,
                        COUNT(*) OVER() AS total
                    FROM points JOIN {p}_students s ON s.user_id=points.user_id
                    WHERE s.approved=TRUE AND s.admin_blocked=FALSE AND s.reset_pending=FALSE
                        AND NOT(s.user_id=ANY(%s::BIGINT[]))
                ) SELECT *, (position BETWEEN %s AND %s) AS visible FROM ranked
                    WHERE position BETWEEN %s AND %s OR user_id=%s ORDER BY position;''',
                    (start,end,admins,page*10+1,page*10+10,page*10+1,page*10+10,uid or 0))
                rows = cur.fetchall()
                return dict(rows=[r for r in rows if r['visible']],mine=next((r for r in rows if r['user_id']==uid),None),
                            total=int(rows[0]['total']) if rows else 0,start=start,end=end)
        return await db.run(op)
    db.period_leaders = leaders

    old_menu = ns['main_menu']
    def menu(admin=False):
        original = old_menu(admin)
        rows = [list(r) for r in original.inline_keyboard]
        existing = {b.callback_data for row in rows for b in row}
        buttons = [B('📊 تقارير الطلبة' if admin else '📊 تقاريري',callback_data='insight|students|0' if admin else 'insight|reports'),
                   B('🥇 المتصدرون',callback_data='insight|leaders')]
        buttons = [b for b in buttons if b.callback_data not in existing]
        if buttons:
            index = next((i for i,r in enumerate(rows) if any(b.callback_data in ('notifications','account_settings') for b in r)),len(rows))
            rows.insert(index,buttons)
        return K(rows)
    ns['main_menu'] = menu
    if 'parent_menu' in ns:
        old_parent = ns['parent_menu']
        def parent_menu(students):
            markup = old_parent(students)
            rows = []
            for s in students:
                uid = s['user_id']
                rows.append([B('📊 تقرير '+s['full_name'],callback_data=f'insight|reports|{uid}')])
            return K(rows+[list(r) for r in markup.inline_keyboard])
        ns['parent_menu'] = parent_menu

    async def allowed(actor,uid):
        if not 0<uid<2**63:return None
        student = await db.get_student(uid)
        if not student:
            return None
        if ns['is_admin'](actor):
            return student
        if not student.get('approved') or student.get('admin_blocked') or student.get('reset_pending'):
            return None
        if uid == actor:
            return student
        # An explicit revoked parent link must never be resurrected by legacy fields.
        def check():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''SELECT approved FROM {p}_parent_links WHERE student_id=%s AND parent_chat_id=%s;''',(uid,actor))
                row = cur.fetchone()
                return bool(row['approved']) if row is not None else bool(student.get('parent_approved') and student.get('parent_chat_id')==actor)
        return student if await db.run(check) else None

    def entries(a):
        rows = []
        for r in a['lectures']:
            title = next((item[1] for item in ns['PLAYLISTS'].get(int(r['chapter']),[]) if int(item[0])==int(r['lecture'])),f"المحاضرة {r['lecture']}")
            rows.append((r['at'],f"🎬 الفصل {r['chapter']} — {title}"))
        for r in a['tasks']:
            grade = f" | {r['grade']}/100" if r['grade'] is not None else ' | بانتظار التصحيح'
            rows.append((r['at'],('📚 ' if r['kind']=='homework' else '📝 ')+r['title']+(' (سابق)' if r['historical'] else '')+grade))
        for r in a['reviews']:
            rows.append((r['at'],f"🔁 مراجعة مرحلة {r['stage']} — الفصل {r['chapter']}، المحاضرة {r['lecture']}"))
        for r in a.get('school_reviews',[]):
            rows.append((r['at'],'🏫 مراجعة المدرسة — '+r['title']))
        for r in a['weaknesses']:
            rows.append((r['at'],f"✅ عالجت نقطة ضعف — الفصل {r['chapter']}، المحاضرة {r['lecture']}: {r['weakness_text']}"))
        return sorted(rows,key=lambda item:item[0])

    async def report(q, context, uid, period, page=0, pdf=False):
        student = await allowed(q.from_user.id,uid)
        if student is None:
            await q.answer('هذا التقرير غير متاح لهذا الحساب.',show_alert=True)
            return
        start, end = period_window(period)
        a = await activity(uid,start,end)
        listing = entries(a)
        page = min(max(0,page),max(0,(len(listing)-1)//10))
        noun = 'اليوم' if period=='day' else 'هذا الأسبوع' if period=='week' else 'هذا الشهر'
        lines = [f'📊 تقريرك {PERIODS[period]}',f"👤 {student['full_name']}",f'📅 {start:%d/%m/%Y} — {end:%d/%m/%Y %H:%M}',
                 f"🎬 محاضرات مكتملة: {len(a['lectures'])}",f"📚 واجبات مسلّمة: {sum(r['kind']=='homework' for r in a['tasks'])}",
                 f"📝 امتحانات مسلّمة: {sum(r['kind']=='exam' for r in a['tasks'])}",f"🔁 مراجعات مكتملة: {len(a['reviews'])}",f"🏫 مراجعات مدرسة مكتملة: {len(a.get('school_reviews',[]))}",
                 f"✅ نقاط ضعف عالجتها: {len(a['weaknesses'])}",f"⭐ صافي XP الدراسة: {a['earned']}",f'\nإنجازك الفعلي خلال {noun}:']
        for at,text in listing[page*10:page*10+10]:
            lines.append(f"• {at.astimezone(ZONE):%d/%m %H:%M} — {text[:170]}")
        if not listing:
            lines.append('ما عندك إنجاز مكتمل مسجّل بهالفترة بعد. افتح مهامك وابدأ بخطوة 💪')
        lines.append('المحاضرة تُحسب بعد الإكمال، والحل بعد إنهاء التسليم؛ فتح المحتوى وحده ما يُحسب إنجازًا.')
        rows = []
        nav = []
        if page:
            nav.append(B('السابق',callback_data=f'insight|report|{period}|{uid}|{page-1}'))
        if len(listing)>(page+1)*10:
            nav.append(B('التالي',callback_data=f'insight|report|{period}|{uid}|{page+1}'))
        if nav:
            rows.append(nav)
        home=ns['back_menu']() if uid==q.from_user.id or ns['is_admin'](q.from_user.id) else B('👪 حساب ولي الأمر',callback_data='insight|parenthome')
        rows += [[B('📄 تحميل PDF كامل',callback_data=f'insight|pdf|{period}|{uid}')],
                 [B('◀️ التقارير',callback_data=f'insight|reports|{uid}'),home]]
        await q.answer()
        if pdf:
            from learning_report_pdf import build_activity_pdf
            number=1
            if period=='week':
                from weekly_report_design import week_number
                key=f'weekly_report_design_first_week_{uid}'
                first=await db.setting_value(key)
                if not first:
                    first=end.date().isoformat();await db.set_setting_value(key,first)
                number=week_number(first,end)
            document = await asyncio.to_thread(build_activity_pdf,student,a,period,entries(a),ns.get('SUBJECT_NAME',''),number)
            chat = getattr(q.message,'chat_id',q.from_user.id)
            await context.bot.send_document(chat,document,caption=ns['bold'](f"📊 تقرير {student['full_name']} {PERIODS[period]}"),parse_mode='HTML')
        else:
            await q.edit_message_text(ns['bold']('\n'.join(lines)),parse_mode='HTML',reply_markup=K(rows))

    old_button = ns['button_handler']
    async def button(update, context):
        q = update.callback_query
        data = str(q.data or '')
        actor = q.from_user.id
        # Old bookmarks now expose the same detailed report, not a separate counter.
        if data == 'achievement_menu':
            data = 'insight|reports'
        elif data.startswith('achievement|'):
            data = 'insight|report|'+data.split('|')[1]
        elif data.startswith('parentachievements|'):
            data = 'insight|report|week|'+data.split('|')[1]
        if not data.startswith('insight|'):
            return await old_button(update,context)
        chat=getattr(update,'effective_chat',None)
        if chat is not None and chat.type!='private':
            await q.answer('افتح التقارير والمتصدرين من محادثة البوت الخاصة.',show_alert=True)
            return
        from reliability_oct30 import clear_flows
        clear_flows(context.user_data)
        try:
            bits = data.split('|')
            action = bits[1]
            default = int(context.user_data.get('preview_student') or actor) if ns['is_admin'](actor) else actor
            if action=='parenthome':
                students=await db.students_by_parent(actor,True)
                await q.answer()
                await q.edit_message_text(ns['bold']('👪 حساب ولي الأمر\nاختر الطالب لمتابعة تقاريره.' if students else 'لا يوجد طالب مربوط بموافقة فعالة لهذا الحساب.'),parse_mode='HTML',reply_markup=ns['parent_menu'](students))
                return
            if action == 'students':
                if not ns['is_admin'](actor):
                    await q.answer('للإدارة فقط.',show_alert=True)
                    return
                page = min(100000,max(0,int(bits[2]))) if len(bits)>2 else 0
                def get():
                    with db.connect() as conn,conn.cursor() as cur:
                        cur.execute(f'SELECT user_id,full_name FROM {p}_students ORDER BY full_name,user_id LIMIT 13 OFFSET %s;',(page*12,))
                        return cur.fetchall()
                students = await db.run(get)
                rows = [[B(s['full_name'],callback_data=f"insight|reports|{s['user_id']}")] for s in students[:12]]
                nav = []
                if page:nav.append(B('السابق',callback_data=f'insight|students|{page-1}'))
                if len(students)>12:nav.append(B('التالي',callback_data=f'insight|students|{page+1}'))
                if nav:rows.append(nav)
                rows.append([ns['back_menu']()])
                await q.answer();await q.edit_message_text(ns['bold']('📊 اختر الطالب لعرض تقرير الإنجاز الفعلي.'),parse_mode='HTML',reply_markup=K(rows));return
            if action == 'reports':
                uid = int(bits[2]) if len(bits)>2 else default
                if await allowed(actor,uid) is None:
                    await q.answer('هذا التقرير غير متاح لهذا الحساب.',show_alert=True);return
                rows = [[B('☀️ تقريري اليومي',callback_data=f'insight|report|day|{uid}'),B('📅 تقريري الأسبوعي',callback_data=f'insight|report|week|{uid}')],
                        [B('🥇 المتصدرون',callback_data='insight|leaders'),ns['back_menu']()]]
                if uid!=actor and not ns['is_admin'](actor):rows[-1]=[B('👪 حساب ولي الأمر',callback_data='insight|parenthome')]
                await q.answer();await q.edit_message_text(ns['bold']('📊 تقارير الإنجاز\nاعرف شنو قريت وشنو سلّمت فعليًا، مع تفاصيل الوقت والدرجات.'),parse_mode='HTML',reply_markup=K(rows));return
            if action in ('report','pdf'):
                period = bits[2]
                if period not in PERIODS:raise ValueError('Invalid period')
                uid = int(bits[3]) if len(bits)>3 else default
                page = min(100000,max(0,int(bits[4]))) if len(bits)>4 else 0
                await report(q,context,uid,period,page,action=='pdf');return
            if action == 'leaders':
                if not ns['is_admin'](actor) and await allowed(actor,actor) is None:
                    await q.answer('المتصدرون متاحون للطلاب المفعّلين.',show_alert=True);return
                period = bits[2] if len(bits)>2 else 'day'
                page = min(100000,max(0,int(bits[3]))) if len(bits)>3 else 0
                if period not in PERIODS:raise ValueError('Invalid period')
                result = await leaders(period,default,page)
                if page and not result['rows']:
                    page=max(0,(result['total']-1)//10);result=await leaders(period,default,page)
                lines = [f'🥇 المتصدرون — الترتيب {PERIODS[period]}',f"📅 من {result['start']:%d/%m/%Y} بتوقيت بغداد",'']
                for r in result['rows']:
                    place=int(r['place']);icon={1:'🥇',2:'🥈',3:'🥉'}.get(place,'🏅')
                    lines.append(f"{icon} {place}. {r['full_name']} — {r['earned']} XP")
                if not result['rows']:lines.append('المنافسة بعدها ما بدأت بهالفترة. أول إنجاز ممكن يحطّك بالقائمة 🔥')
                mine=result['mine']
                lines += ['',f"مركزك: {mine['place']} | {mine['earned']} XP" if mine else 'مركزك: تحتاج XP دراسة موجبة خلال الفترة حتى تدخل الترتيب.',
                          'الترتيب حسب صافي XP الدراسة خلال الفترة؛ مشتريات المتجر والتعديلات اليدوية لا تغيّر المنافسة. التعادل يمنح نفس المركز.']
                if ns['is_admin'](actor) and default==actor:lines[-2]='حسابات الإدارة خارج المنافسة.'
                rows=[[B('اليوم',callback_data='insight|leaders|day|0'),B('الأسبوع',callback_data='insight|leaders|week|0')],
                      [B('الشهر',callback_data='insight|leaders|month|0')]]
                nav=[]
                if page:nav.append(B('السابق',callback_data=f'insight|leaders|{period}|{page-1}'))
                if result['total']>(page+1)*10:nav.append(B('التالي',callback_data=f'insight|leaders|{period}|{page+1}'))
                if nav:rows.append(nav)
                rows.append([B('🔄 تحديث',callback_data=f'insight|leaders|{period}|{page}'),ns['back_menu']()])
                await q.answer();await q.edit_message_text(ns['bold']('\n'.join(lines)),parse_mode='HTML',reply_markup=K(rows));return
            raise ValueError('Invalid action')
        except (ValueError,IndexError):
            await q.answer('رابط غير صالح. افتح التقارير من القائمة مجددًا.',show_alert=True)
    ns['button_handler'] = button
