"""Parallel foundation lectures, stable progress IDs, and teacher-paced Tuesdays."""
import logging
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo
from html import escape
from urllib.parse import urlparse
from biology_data import GENETICS_FOUNDATIONS
from course_schedule_v12 import pack_lessons, migrate_future

ZONE=ZoneInfo('Asia/Baghdad')
LOG=logging.getLogger(__name__)


def valid_video_url(value):
    u=urlparse(value.strip())
    return u.scheme=='https' and u.hostname in {'youtube.com','www.youtube.com','youtu.be'} and bool(u.path and u.path!='/')


def parallel_plan(rows, first, occupied=(), fixed=None, choices=None):
    fixed=dict(fixed or {}); choices=choices or {}; planned=dict(fixed)
    remaining=[n for n in range(1,8) if n not in fixed]
    def capacity(day,chapter):
        if day.weekday()!=1:return 1
        if chapter in (3,4) and remaining:
            planned[remaining.pop(0)]=day
            return 1
        return choices.get(day,1)
    bundles=pack_lessons(rows,first,occupied=occupied,capacity_fn=capacity)
    # If the teacher's current course is already past chapters 3/4, keep the
    # remaining foundations visible and scheduled weekly, rather than dropping them.
    if remaining:
        day=first
        while day.weekday()!=1:day+=timedelta(days=1)
        for n in remaining:
            while day in planned.values():day+=timedelta(days=7)
            planned[n]=day;day+=timedelta(days=7)
    return bundles,planned


def migrate(db):
    with db.connect() as conn,conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS biology_genetics_foundations(
            number INTEGER PRIMARY KEY CHECK(number BETWEEN 1 AND 7),url TEXT,target_date DATE);
            CREATE TABLE IF NOT EXISTS biology_tuesday_choices(
            target_date DATE PRIMARY KEY,lecture_count INTEGER NOT NULL CHECK(lecture_count IN(1,2)),actor_id BIGINT NOT NULL);
            CREATE TABLE IF NOT EXISTS biology_genetics_notices(key TEXT PRIMARY KEY,sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);''')
        for n,url in GENETICS_FOUNDATIONS:
            cur.execute("INSERT INTO biology_genetics_foundations(number,url) VALUES(%s,%s) ON CONFLICT(number) DO UPDATE SET url=EXCLUDED.url WHERE biology_genetics_foundations.url IS NULL OR biology_genetics_foundations.url='';",(n,url))
        conn.commit()


async def replan(db,force=False,choice=None):
    def planner(cur,rows,today,occupied):
        if choice:
            day,count,actor=choice
            cur.execute('INSERT INTO biology_tuesday_choices(target_date,lecture_count,actor_id) VALUES(%s,%s,%s) ON CONFLICT(target_date) DO UPDATE SET lecture_count=EXCLUDED.lecture_count,actor_id=EXCLUDED.actor_id;',(day,count,actor))
        cur.execute('SELECT number,target_date FROM biology_genetics_foundations WHERE target_date<=%s;',(today,))
        fixed={r['number']:r['target_date'] for r in cur.fetchall()}
        cur.execute('SELECT * FROM biology_tuesday_choices;')
        choices={r['target_date']:r['lecture_count'] for r in cur.fetchall()}
        first=max(today+timedelta(days=1),min((r['target_date'] for r in rows),default=today+timedelta(days=1)))
        bundles,plan=parallel_plan(rows,first,occupied,fixed,choices)
        for n,day in plan.items():
            cur.execute('UPDATE biology_genetics_foundations SET target_date=%s WHERE number=%s;',(day,n))
        return bundles
    return await migrate_future(db,marker='v13_genetics_schedule',planner=planner,force=force)


def install(ns,db):
    B,K=ns['InlineKeyboardButton'],ns['InlineKeyboardMarkup']
    async def sql(statement,args=(),one=False,write=False):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(statement,args)
                result=(cur.fetchone() if one else cur.fetchall()) if cur.description else None
                if write:conn.commit()
                return result
        return await db.run(op)
    old_init=db.init_db
    def init_db():old_init();migrate(db)
    db.init_db=ns['init_db']=init_db
    old_progress=db.v52_progress_overview
    async def progress(uid):
        result=await old_progress(uid)
        if not result.get('student'):return result
        done=set(result.get('completed',{}).get(5,[]))
        rows=await sql('SELECT * FROM biology_genetics_foundations ORDER BY number;')
        pending=[r for r in rows if 39+r['number'] not in done]
        dates=[r['target_date'] for r in pending if r['target_date']]
        if result.get('finish_date'):dates.append(result['finish_date'])
        return {**result,'finish_date':max(dates) if dates else None,'foundation_pending':pending}
    db.v52_progress_overview=progress
    old_finish=db.student_finish_date
    async def finish(uid):return (await progress(uid)).get('finish_date')
    db.student_finish_date=ns['student_finish_date']=finish

    async def listing(q):
        rows=await sql('SELECT * FROM biology_genetics_foundations ORDER BY number;')
        data=await progress(q.from_user.id);done=set(data.get('completed',{}).get(5,[]))
        text=['🧬 الفصل الخامس — أساسيات الوراثة','الأستاذ سالم آل منصور 2026','محاضرة أسبوعياً يوم الثلاثاء مع الفصلين الثالث والرابع.']
        buttons=[]
        for r in rows:
            status='✅' if r['number']+39 in done else ('▶️' if r['url'] else '⏳')
            when=r['target_date'].strftime('%d/%m/%Y') if r['target_date'] else 'لم يحدد بعد'
            text.append(f"{status} أساسيات {r['number']} — {when}"+(' — الرابط غير متوفر' if not r['url'] else ''))
            buttons.append([B(f"{status} أساسيات الوراثة {r['number']}",callback_data=f"gen|open|{r['number']}")])
        if ns['is_admin'](q.from_user.id):
            buttons.append([B('🔗 تعديل رابط محاضرة',callback_data='gen|link')])
        buttons.append([ns['back_menu']()])
        await q.edit_message_text('\n'.join(text),reply_markup=K(buttons))

    async def open_lesson(q,n,complete=False):
        r=await sql('SELECT * FROM biology_genetics_foundations WHERE number=%s;',(n,),True)
        if not r:await q.answer('المحاضرة غير موجودة.');return
        student=await db.get_student(q.from_user.id)
        active=bool(student and student.get('approved') and not student.get('admin_blocked') and not student.get('reset_pending'))
        due=bool(r['target_date'] and r['target_date']<=datetime.now(ZONE).date())
        available=active and (due or student.get('study_track')=='chapter' and student.get('current_chapter')==5)
        if complete:
            if not available or not r['url']:
                await q.answer('لا يمكن اعتماد المحاضرة قبل موعدها أو قبل إضافة رابطها.',show_alert=True);return
            await db.mark_lecture_progress(q.from_user.id,5,39+n,True,'genetics_foundations')
            await q.answer('تم حفظ الإنجاز وتحديث نسبة المنهج.')
        else:await q.answer()
        buttons=[]
        if r['url']:buttons.append([B('▶️ مشاهدة المحاضرة',url=r['url'])])
        if available and r['url']:buttons.append([B('✅ أكملت دراسة المحاضرة',callback_data=f'gen|done|{n}')])
        buttons.append([B('◀️ أساسيات الوراثة',callback_data='gen|list')])
        await q.edit_message_text(f'🧬 أساسيات الوراثة — المحاضرة {n}\n'+('الرابط غير متوفر حالياً؛ تبقى غير مكتملة في نسبة الإنجاز.' if not r['url'] else 'شاهد المحاضرة ثم اعتمد إكمالها بعد دراستها.')+('' if available else '\nيمكن تصفحها الآن؛ اعتماد الإنجاز بعد التفعيل وحلول الموعد.'),reply_markup=K(buttons))

    class ExtraQuery:
        def __init__(self,q,footer=''):self.q=q;self.footer=footer
        def __getattr__(self,k):return getattr(self.q,k)
        async def edit_message_text(self,text,*args,**kw):
            rows=[list(r) for r in getattr(kw.get('reply_markup'),'inline_keyboard',[])]
            rows.insert(0,[B('🧬 أساسيات الوراثة — الثلاثاء',callback_data='gen|list')])
            kw['reply_markup']=K(rows)
            return await self.q.edit_message_text(text+self.footer,*args,**kw)
    for name in ('v51_today_preparation','v51_daily_tasks_menu','v37_schedule_menu'):
        old=ns[name]
        def wrap(fn):
            async def show(q,*a,**kw):
                return await fn(ExtraQuery(q,'\n\n🧬 أساسيات الوراثة: محاضرة كل ثلاثاء، مستقلة عن محاضرة الفصل الحالي.'),*a,**kw)
            return show
        ns[name]=wrap(old)
    async def overview(q):
        result=await progress(q.from_user.id)
        if not result.get('student'):
            await q.edit_message_text('أكمل التسجيل أولاً.');return
        completed=result.get('completed',{});done_total=0;lines=['📊 تقدم المنهج — 107 محاضرات']
        for ch in range(1,6):
            total=39 if ch==5 else len(ns['PLAYLISTS'][ch])
            done=len(set(completed.get(ch,[])) & set(range(1,total+1)))
            done_total+=done
            lines.append(f'الفصل {ch}'+(' — الوراثة' if ch==5 else '')+f': {done}/{total} — {round(done*100/total)}%')
        base_done=len(set(completed.get(5,[])) & set(range(40,47)));done_total+=base_done
        lines.append(f'الفصل الخامس — أساسيات الوراثة: {base_done}/7 — {round(base_done*100/7)}%')
        lines.append(f'🎯 الإنجاز الكلي: {done_total}/107 — {round(done_total*100/107)}%')
        finish=result.get('finish_date')
        lines.append('🏁 الإكمال المتوقع: '+(finish.strftime('%d/%m/%Y') if finish else 'اكتمل المسار المجدول'))
        missing=[r['number'] for r in result.get('foundation_pending',[]) if not r['url']]
        if missing:lines.append('⏳ أساسيات بلا رابط، تبقى غير مكتملة: '+', '.join(map(str,missing)))
        lines.append('الموعد تقديري، يتحدث حسب جدول الأستاذ وإنجاز الطالب؛ الأيام الماضية غير المكتملة تبقى متأخرات.')
        await q.edit_message_text('\n'.join(lines),reply_markup=K([[B('🔄 تحديث',callback_data='chapter_completion_schedule')],[B('🧬 الأساسيات',callback_data='gen|list')],[ns['back_menu']()]]))
    ns['v52_progress_overview']=overview
    old_menu=ns['main_menu']
    def menu(admin=False):
        rows=[list(r) for r in old_menu(admin).inline_keyboard]
        rows.insert(0,[B('🧬 أساسيات الوراثة وجدول الثلاثاء',callback_data='gen|list')])
        return K(rows)
    ns['main_menu']=menu

    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query;data=str(q.data or '');uid=q.from_user.id
        if data=='chapter|5':
            await q.answer();await q.edit_message_text('🧬 الفصل الخامس\nاختر القسم:',reply_markup=K([[B('أساسيات الوراثة — 7 محاضرات',callback_data='gen|list')],[B('الوراثة — 39 محاضرة',callback_data='gen|genetics')],[ns['back_menu']()]]));return
        if data=='gen|genetics':
            rows=[[B(f'{n} — {title[:42]}',callback_data=f'lecture|5|{n}')] for n,title,url in ns['PLAYLISTS'][5] if n<=39]
            await q.answer();await q.edit_message_text('🧬 الفصل الخامس — الوراثة',reply_markup=K(rows+[[ns['back_menu']()]]));return
        # Every legacy route to an appended foundation uses the same availability gate.
        pieces=data.split('|')
        if len(pieces)==3 and pieces[1]=='5' and pieces[2].isdigit() and 40<=int(pieces[2])<=46 and pieces[0] in {'lecture','prepopen','lecture_done','v55_exam_watch','v55_exam_done','v55_exam_verify','v55_exam_study','v55_exam_oath','v55_exam_oath_confirm','v52_exam_lecture'}:
            await open_lesson(q,int(pieces[2])-39);return
        if not data.startswith('gen|'):
            context.user_data.pop('gen_link',None)
            return await old_button(update,context)
        try:
            action=pieces[1]
            if action=='list':context.user_data.pop('gen_link',None);await q.answer();await listing(q);return
            if action in ('open','done'):await open_lesson(q,int(pieces[2]),action=='done');return
            if not ns['is_admin'](uid) or context.user_data.get('preview_student'):
                await q.answer('للأستاذ فقط.',show_alert=True);return
            if action=='link':
                context.user_data['gen_link']=datetime.now(ZONE)
                await q.answer();await q.edit_message_text('أرسل للبوت في الخاص رقم المحاضرة ثم رابط YouTube، مثال:\n2 https://youtu.be/VIDEO_ID');return
            if action=='count':
                day=date.fromisoformat(pieces[2]);count=int(pieces[3]);today=datetime.now(ZONE).date()
                if day<=today or day.weekday()!=1 or count not in (1,2):raise ValueError()
                if await sql('SELECT 1 FROM biology_genetics_foundations WHERE target_date=%s;',(day,),True):
                    await q.answer('هذا الثلاثاء مخصص لمحاضرة فصل + أساسيات.',show_alert=True);return
                # Commit choice and repack under one transaction in the planner path.
                await replan(db,True,(day,count,uid))
                await q.answer('تم تحديث الجدول وموعد إكمال المنهج.');await q.edit_message_text(f'✅ الثلاثاء {day:%d/%m/%Y}: {count} محاضرة من الفصل الحالي.');return
        except (ValueError,IndexError):await q.answer('الخيار غير صالح أو انتهى موعده.',show_alert=True)
    ns['button_handler']=button

    async def input_message(update,context):
        started=context.user_data.get('gen_link')
        if not started:return False
        if not ns['is_admin'](update.effective_user.id) or context.user_data.get('preview_student'):context.user_data.pop('gen_link',None);return False
        if datetime.now(ZONE)-started>timedelta(minutes=15):context.user_data.pop('gen_link',None);return False
        try:
            number,url=(update.effective_message.text or '').split(maxsplit=1);n=int(number)
            if n not in range(1,8) or not valid_video_url(url):raise ValueError()
        except ValueError:
            await update.effective_message.reply_text('أرسل رقماً من 1 إلى 7 ثم رابط YouTube آمن يبدأ بـ https://.');return True
        await sql('UPDATE biology_genetics_foundations SET url=%s WHERE number=%s;',(url.strip(),n),write=True)
        ns['PLAYLISTS'][5][38+n]=(39+n,f'أساسيات الوراثة — المحاضرة {n}',url.strip())
        context.user_data.pop('gen_link',None)
        await update.effective_message.reply_text('✅ تم تحديث رابط المحاضرة.');return True
    ns['genetics_input_message']=input_message
    old_start=ns['start']
    async def start(update,context):
        context.user_data.pop('gen_link',None)
        return await old_start(update,context)
    ns['start']=start

    async def notify_once(bot,key,chat,text,markup=None,thread=None):
        if not chat or await sql('SELECT 1 FROM biology_genetics_notices WHERE key=%s;',(key,),True):return
        await bot.send_message(chat,text,reply_markup=markup,message_thread_id=thread)
        await sql('INSERT INTO biology_genetics_notices(key) VALUES(%s) ON CONFLICT DO NOTHING;',(key,),write=True)
    async def job(context):
        now=datetime.now(ZONE);today=now.date()
        rows=await sql('SELECT * FROM biology_genetics_foundations ORDER BY number;')
        for r in rows:
            if not r['target_date']:continue
            release=datetime.combine(r['target_date']-timedelta(days=1),time(23),ZONE)
            if release<=now:
                try:
                    await notify_once(context.bot,f"foundation:{r['number']}",ns.get('BIOLOGY_GROUP_ID'),f"🧬 تحضير الثلاثاء {r['target_date']:%d/%m}: أساسيات الوراثة {r['number']} مع محاضرة الفصل الحالي."+(' الرابط غير متوفر بعد.' if not r['url'] else ''),K([[B('🧬 فتح الأساسيات',callback_data='gen|list')]]),ns.get('PREPARATION_TOPIC_ID') or None)
                except ns['TelegramError']:
                    LOG.warning('Foundation notification failed; retry next sweep',exc_info=True)
        dates=[r['target_date'] for r in rows if r['target_date']]
        if len(dates)==7 and today.weekday()==1 and today>=max(dates):
            target=today+timedelta(days=7)
            if not await sql('SELECT 1 FROM biology_preparations WHERE target_date>=%s LIMIT 1;',(target,),True):return
            markup=K([[B('محاضرة واحدة',callback_data=f'gen|count|{target}|1'),B('محاضرتان',callback_data=f'gen|count|{target}|2')]])
            await notify_once(context.bot,f'choice:{today}',ns.get('OWNER_CHAT_ID'),f'انتهى توزيع الأساسيات الأسبوعي. حدد عدد محاضرات الفصل الحالي للثلاثاء القادم {target:%d/%m/%Y}. الافتراضي محاضرة واحدة إلى أن تختار. إكمال الطلاب للأساسيات يبقى حسب سجل كل طالب.',markup)
    old_post=ns['post_init']
    async def post(app):
        await old_post(app)
        await replan(db)
        for r in await sql('SELECT * FROM biology_genetics_foundations;'):
            ns['PLAYLISTS'][5][38+r['number']]=(39+r['number'],f"أساسيات الوراثة — المحاضرة {r['number']}",r['url'])
        app.job_queue.run_repeating(job,300,first=15,name='genetics_tuesdays')
    ns['post_init']=post
