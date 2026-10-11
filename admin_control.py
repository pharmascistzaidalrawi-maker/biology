"""Persistent course editing and explicitly scoped administrator student sessions."""
import json
import secrets
from collections import ChainMap
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from education_update import ZONE, sql, MarkupProxy, UpdateProxy


def numbers(text):
    text=text.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩،','0123456789,'))
    return sorted({int(v.strip()) for v in text.replace('+',',').split(',') if v.strip()})


def course_plan(catalog, chapter, lecture, first_date, days):
    days=set(days)
    if not days or not days<=set(range(7)):raise ValueError('اختر يوماً واحداً على الأقل.')
    if first_date.weekday() not in days:raise ValueError('تاريخ البداية يجب أن يكون ضمن أيام الدراسة المحددة.')
    groups=catalog.get(chapter,[])
    start=next((i for i,group in enumerate(groups) if lecture in group),None)
    if start is None:raise ValueError('المحاضرة ليست ضمن التحاضير؛ عدل مجموعات المحاضرات أولاً.')
    result=[];cursor=first_date
    for ch in sorted(catalog):
        if ch<chapter:continue
        for index,group in enumerate(catalog[ch]):
            if ch==chapter and index<start:continue
            selected=[n for n in group if n>=lecture] if ch==chapter and index==start else list(group)
            if not selected:continue
            result.append((ch,index+1,','.join(map(str,selected)),cursor))
            cursor+=timedelta(days=1)
            while cursor.weekday() not in days:cursor+=timedelta(days=1)
    if not result:raise ValueError('الجدول فارغ.')
    return result


def migrate(db):
    with db.connect() as conn,conn.cursor() as cur:
        cur.execute('''ALTER TABLE biology_preparations ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS needs_announcement BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE biology_preparation_catalog_overrides ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '';
        CREATE TABLE IF NOT EXISTS biology_course_schedule_history(
            id BIGSERIAL PRIMARY KEY,actor_id BIGINT NOT NULL,rows JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);''')
        conn.commit()


async def rebuild_course(db,rows,days,actor):
    """Atomic replacement of the course calendar, with a retained historical snapshot."""
    if not rows:raise ValueError('الجدول فارغ.')
    def op():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute('LOCK TABLE biology_preparations IN EXCLUSIVE MODE;')
            cur.execute('SELECT COALESCE(MAX(prep_no),0) AS n FROM biology_preparations;');last=cur.fetchone()['n']
            cur.execute('INSERT INTO biology_course_schedule_history(actor_id,rows) SELECT %s,COALESCE(jsonb_agg(to_jsonb(p)),\'[]\'::jsonb) FROM biology_preparations p;',(actor,))
            cur.execute('SELECT chapter,prep_no,title FROM biology_preparation_catalog_overrides;')
            titles={(r['chapter'],r['prep_no']):r['title'] for r in cur.fetchall()}
            cur.execute('DELETE FROM biology_preparations;')
            today=datetime.now(ZONE).date()
            for offset,(ch,pno,lectures,day) in enumerate(rows,1):
                cur.execute('''INSERT INTO biology_preparations(prep_no,chapter,chapter_prep_no,lectures,target_date,title,published,published_at,needs_announcement)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s);''',
                    (last+offset,ch,pno,lectures,day,titles.get((ch,pno),''),day<=today,
                     datetime.combine(day,datetime.min.time(),ZONE) if day<=today else None,day<=today))
            # Old early-unlock pointers must not override the replacement calendar.
            cur.execute("DELETE FROM biology_early_preparation_unlocks WHERE study_track='course' AND completed_at IS NULL;")
            cur.execute('''UPDATE biology_students SET current_chapter=%s,start_chapter=%s,start_prep_no=%s,
                track_started_on=GREATEST(COALESCE(track_started_on,%s),%s),study_days=%s
                WHERE study_track='course';''',(rows[0][0],rows[0][0],rows[0][1],rows[0][3],rows[0][3],sorted(days)))
            for key,value in [('admin_course_calendar','1'),('admin_course_days',json.dumps(sorted(days)))]:
                cur.execute('INSERT INTO biology_settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;',(key,value))
            cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',(actor,'course_calendar_rebuilt',f'start={rows[0]};count={len(rows)}'))
            conn.commit()
    return await db.run(op)


async def edit_course_row(db,pid,field,value,actor):
    if field not in {'title','lectures','target_date','chapter','delete'}:raise ValueError('حقل غير صالح.')
    def op():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute('LOCK TABLE biology_preparations IN EXCLUSIVE MODE;')
            cur.execute('SELECT * FROM biology_preparations WHERE prep_no=%s FOR UPDATE;',(pid,));row=cur.fetchone()
            if not row:raise ValueError('التحضير غير موجود.')
            cur.execute('INSERT INTO biology_course_schedule_history(actor_id,rows) VALUES(%s,%s::jsonb);',(actor,json.dumps([row],default=str)))
            if field=='delete':cur.execute('DELETE FROM biology_preparations WHERE prep_no=%s;',(pid,))
            elif field=='target_date':
                cur.execute('SELECT prep_no FROM biology_preparations WHERE target_date=%s AND prep_no<>%s;',(value,pid))
                if cur.fetchone():raise ValueError('هذا اليوم يحتوي تحضيراً آخر. غيّر موعده أولاً أو أعد تنظيم الجدول.')
                cur.execute('UPDATE biology_preparations SET target_date=%s,published=%s,published_at=%s,needs_announcement=%s WHERE prep_no=%s;',(value,value<=datetime.now(ZONE).date(),datetime.now(ZONE) if value<=datetime.now(ZONE).date() else None,value<=datetime.now(ZONE).date(),pid))
            elif field=='chapter':
                ch,pno,encoded=value
                cur.execute('UPDATE biology_preparations SET chapter=%s,chapter_prep_no=%s,lectures=%s WHERE prep_no=%s;',(ch,pno,encoded,pid))
            else:cur.execute(f'UPDATE biology_preparations SET {field}=%s WHERE prep_no=%s;',(value,pid))
            if field in {'delete','lectures','target_date','chapter'}:
                cur.execute("DELETE FROM biology_early_preparation_unlocks WHERE study_track='course' AND source_prep_id=%s AND completed_at IS NULL;",(pid,))
            cur.execute("INSERT INTO biology_settings(key,value) VALUES('admin_course_calendar','1') ON CONFLICT(key) DO UPDATE SET value='1';")
            cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',(actor,'course_preparation_edit',f'prep={pid};field={field}'))
            conn.commit()
    return await db.run(op)


async def edit_profile(db,uid,field,value,actor):
    if field not in {'full_name','school','target_grade','xp','warnings'}:raise ValueError('حقل غير صالح.')
    def op():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute('SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;',(uid,));old=cur.fetchone()
            if not old:raise ValueError('الطالب غير موجود.')
            if field=='warnings':
                # Editing the displayed counter alone used to resurrect removed
                # warnings during the nightly ledger reconciliation.
                wanted=int(value)
                cur.execute('SELECT id,task_id FROM biology_warning_log WHERE user_id=%s ORDER BY created_at DESC,id DESC FOR UPDATE;',(uid,))
                entries=cur.fetchall()
                if wanted<len(entries):
                    for warning in entries[:len(entries)-wanted]:
                        if warning.get('task_id'):
                            cur.execute("""INSERT INTO biology_v9_deleted_task_warnings(task_id,user_id,deleted_by)
                                SELECT id,%s,%s FROM biology_tasks WHERE id=%s
                                ON CONFLICT(task_id,user_id) DO NOTHING;""",(uid,actor,warning['task_id']))
                        cur.execute('DELETE FROM biology_warning_log WHERE id=%s AND user_id=%s;',(warning['id'],uid))
                elif wanted>len(entries):
                    if wanted-len(entries)>20:
                        raise ValueError('لا يمكن إضافة أكثر من 20 إنذاراً دفعة واحدة؛ استخدم إدارة الإنذارات.')
                    for _ in range(wanted-len(entries)):
                        cur.execute("""INSERT INTO biology_warning_log(user_id,task_id,reason,issued_by)
                            VALUES(%s,NULL,'تعديل إداري لعدد الإنذارات',%s);""",(uid,actor))
                cur.execute("""UPDATE biology_students SET warnings=(SELECT COUNT(*) FROM biology_warning_log
                    WHERE user_id=%s) WHERE user_id=%s;""",(uid,uid))
            else:
                cur.execute(f'UPDATE biology_students SET {field}=%s WHERE user_id=%s;',(value,uid))
            if field=='xp':
                wallet='chapter_xp' if old.get('study_track')=='chapter' else 'course_xp'
                cur.execute(f'UPDATE biology_students SET {wallet}=%s WHERE user_id=%s;',(value,uid))
            cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',(actor,'admin_profile_edit',f'user={uid};field={field};old={old.get(field)};new={value}'))
            conn.commit()
    return await db.run(op)


class ContextProxy:
    def __init__(self,base,state,bot,target=None):
        self.base=base;self.user_data=state;self.bot=bot
        if target is not None and getattr(base,'application',None):
            original=base.application
            class App:
                user_data=ChainMap({target:state},original.user_data)
                def __getattr__(self,key):return getattr(original,key)
            self.application=App()
        if target is not None and getattr(base,'job_queue',None):
            original_queue=base.job_queue
            class Queue:
                def __getattr__(self,key):return getattr(original_queue,key)
                def run_once(self,callback,*args,**kwargs):
                    async def scoped(job_context):return await callback(ContextProxy(job_context,state,bot,target))
                    return original_queue.run_once(scoped,*args,**kwargs)
            self.job_queue=Queue()
    def __getattr__(self,key):return getattr(self.base,key)


class BotProxy:
    def __init__(self,base,target,admin,render=None):self.base=base;self.target=target;self.admin=admin;self.render=render
    def __getattr__(self,key):
        method=getattr(self.base,key)
        if not (key.startswith('send_') or key in {'copy_message','forward_message'}):return method
        async def call(*args,**kwargs):
            destination=args[0] if args else kwargs.get('chat_id')
            if destination==self.target and self.render and 'reply_markup' in kwargs:kwargs['reply_markup']=self.render(kwargs['reply_markup'])
            if args and args[0]==self.target:args=(self.admin,)+args[1:]
            if kwargs.get('chat_id')==self.target:kwargs['chat_id']=self.admin
            return await method(*args,**kwargs)
        return call


class ActingUpdate:
    def __init__(self,base,query,user,message):self.base=base;self.callback_query=query;self.effective_user=user;self.effective_message=message;self.message=message if getattr(base,'message',None) else None
    def __getattr__(self,key):return getattr(self.base,key)


def install(ns,db):
    B=ns['InlineKeyboardButton'];K=ns['InlineKeyboardMarkup'];old_init=db.init_db
    def init_db():
        old_init();migrate(db)
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT value FROM biology_settings WHERE key='admin_course_days';");row=cur.fetchone()
            ns['_ADMIN_COURSE_DAYS']=json.loads(row['value']) if row else [0,2,4]
    db.init_db=init_db;ns['init_db']=init_db
    def next_date(value,days=None):
        allowed=set(days or ns.get('_ADMIN_COURSE_DAYS') or [0,2,4]);value+=timedelta(days=1)
        while value.weekday() not in allowed:value+=timedelta(days=1)
        return value
    db._next_study_date=next_date
    def previous_date(value):
        allowed=set(ns.get('_ADMIN_COURSE_DAYS') or [0,2,4]);value-=timedelta(days=1)
        while value.weekday() not in allowed:value-=timedelta(days=1)
        return value
    db._previous_study_date=previous_date
    old_seed=db.seed_preparations
    async def seed(rows):
        if await db.setting_value('admin_course_calendar')=='1':return
        return await old_seed(rows)
    db.seed_preparations=seed;ns['seed_preparations']=seed
    old_due=db.due_preparations
    async def due(now):
        return await sql(db,"SELECT * FROM biology_preparations WHERE (published=FALSE OR needs_announcement=TRUE) AND ((target_date-1)+TIME '23:00')<=%s ORDER BY target_date,prep_no;",(now.astimezone(ZONE).replace(tzinfo=None),))
    db.due_preparations=due;ns['due_preparations']=due
    old_mark=db.mark_preparation_published
    async def mark(pid):
        await old_mark(pid);await sql(db,'UPDATE biology_preparations SET needs_announcement=FALSE WHERE prep_no=%s;',(pid,),write=True)
    db.mark_preparation_published=mark;ns['mark_preparation_published']=mark
    old_text=ns['preparation_text']
    def prep_text(row):return (ns['bold'](row['title'])+'\n' if row.get('title') else '')+old_text(row)
    ns['preparation_text']=prep_text
    old_current=db.v52_current_preparation
    async def current(uid):
        row=await old_current(uid)
        if row and not row.get('title'):
            meta=await sql(db,'SELECT title FROM biology_preparation_catalog_overrides WHERE chapter=%s AND prep_no=%s;',(row['chapter'],row.get('chapter_prep_no') or row.get('prep_no')),True)
            if meta:row={**row,'title':meta['title']}
        return row
    db.v52_current_preparation=current

    def back():return [B('لوحة الأدمن',callback_data='ctl_exit',style='danger')]
    def menu(rows):return K(rows+[back()])
    old_menu=ns['main_menu']
    def main_menu(admin=False):
        m=old_menu(admin)
        return K([[B('🗓 تحرير جدول الدورة بالكامل',callback_data='ctl_calendar',style='primary')]]+[list(r) for r in m.inline_keyboard]) if admin else m
    ns['main_menu']=main_menu
    old_prep_menu=ns['v51_preparation_admin_menu']
    async def prep_menu(q):return await old_prep_menu(MarkupProxy(q,[[B('🗓 تحرير جدول الدورة بالكامل',callback_data='ctl_calendar')]],K))
    ns['v51_preparation_admin_menu']=prep_menu
    old_group=ns['v51_preparation_chapter']
    async def group(q,ch):
        rows=await db.v51_preparation_catalog_overrides();titles={(r['chapter'],r['prep_no']):r.get('title') for r in rows}
        buttons=[[B(titles.get((ch,p)) or '+'.join(map(str,nums)),callback_data=f'ctl_group|{ch}|{p}')]
                 for p,nums in enumerate(ns['CHAPTER_PREPARATION_DISTRIBUTION'].get(ch,[]),1)]
        await q.edit_message_text('اختر التحضير لتعديل اسمه أو محاضراته:',reply_markup=menu(buttons))
    ns['v51_preparation_chapter']=group

    async def calendar(q,page=0):
        rows=await sql(db,'SELECT * FROM biology_preparations ORDER BY target_date,prep_no;');page=max(0,min(page,max(0,(len(rows)-1)//10)))
        buttons=[[B('🔄 إعادة التنظيم من أي محاضرة',callback_data='ctl_rebuild')],[B('➕ إضافة تحضير للدورة',callback_data='ctl_add')]]
        for r in rows[page*10:page*10+10]:buttons.append([B(f"{r['target_date']:%d/%m} | {r.get('title') or 'الفصل '+str(r['chapter'])+' م '+r['lectures'].replace(',','+')}",callback_data=f"ctl_row|{r['prep_no']}")])
        nav=[]
        if page:nav.append(B('السابق',callback_data=f'ctl_calendar|{page-1}'))
        if (page+1)*10<len(rows):nav.append(B('التالي',callback_data=f'ctl_calendar|{page+1}'))
        if nav:buttons.append(nav)
        await q.edit_message_text('🗓 الجدول الفعلي للدورة الحالية\nيمكن تغيير البداية إلى محاضرات أقدم أو أحدث. الإنجاز السابق يبقى محفوظاً.',reply_markup=menu(buttons))

    async def account(q,uid,s):
        r=await db.get_student(uid)
        if not r:await q.edit_message_text('الحساب غير موجود.',reply_markup=menu([]));return
        rows=[[B(label,callback_data=f'ctl_profile|{field}')] for field,label in [('full_name','✏️ الاسم'),('school','✏️ المدرسة'),('target_grade','✏️ المعدل المطلوب'),('xp','⭐ تعديل XP'),('warnings','⚠️ تعديل عدد الإنذارات')]]
        rows += [[B('🔄 تغيير المسار',callback_data='ctl_action|change_study_track'),B('🗓 جدول الطالب',callback_data='ctl_action|personal_schedule')],
                 [B('✅ تفعيل',callback_data=f'ctl_access|activate'),B('⛔ إيقاف',callback_data='ctl_access|block'),B('🔓 رفع الإيقاف',callback_data='ctl_access|unblock')],
                 [B('👨‍👩‍👦 إدارة ربط ولي الأمر',callback_data='ctl_parent')],
                 [B('🎬 تعديل إنجاز المحاضرات',callback_data='ctl_progress')],
                 [B('🗑 حذف حساب الطالب',callback_data='ctl_action|self_delete')],
                 [B('👁 واجهة الطالب',callback_data='ctl_home')]]
        await q.edit_message_text(f"⚙️ إدارة الطالب: {r.get('full_name')}\n🆔 {uid}\n⭐ {r.get('xp',0)} XP | ⚠️ {r.get('warnings',0)} إنذار\nكل تغيير هنا يطبق على هذا الطالب فعلياً.",reply_markup=managed_menu(rows,s))

    def session(context):
        s=context.user_data.get('control_session')
        if s and time.monotonic()-s['at']<1800:return s
        context.user_data.pop('control_session',None);return None

    def managed_menu(rows,s):
        result=[]
        for row in rows:
            new=[]
            for b in row:
                key=str(len(s['routes']));s['routes'][key]=b.callback_data
                kw=b.to_dict() if hasattr(b,'to_dict') else dict(b.__dict__)
                label=kw.pop('text');kw['callback_data']=f"managed|{s['nonce']}|{key}";new.append(B(label,**kw))
            result.append(new)
        return menu(result)

    def rendered(markup,s):
        rows=[]
        for row in (markup.inline_keyboard if markup else []):
            new=[]
            for b in row:
                raw=getattr(b,'callback_data',None)
                if not raw:new.append(b);continue
                key=str(len(s['routes']));s['routes'][key]=raw
                kw=b.to_dict() if hasattr(b,'to_dict') else dict(b.__dict__)
                kw['callback_data']=f"acting|{s['nonce']}|{key}"
                label=kw.pop('text');new.append(B(label,**kw))
            rows.append(new)
        rows += [[B('⚙️ إدارة هذا الطالب',callback_data='ctl_account'),B('👁 قائمة الطالب',callback_data='ctl_home')],back()]
        return K(rows)

    old_button=ns['button_handler'];old_private=ns['private_messages']
    async def acting(update,context,s,data=None):
        admin=update.effective_user.id if getattr(update,'effective_user',None) else update.callback_query.from_user.id
        uid=s['uid']
        student=await db.get_student(uid)
        if not ns['is_admin'](admin) or ns['is_admin'](uid) or not student:raise ValueError('جلسة الإدارة أو الطالب غير متاحة.')
        s['at']=time.monotonic();user=SimpleNamespace(id=uid,username=student.get('username'),full_name=student.get('full_name') or str(uid),first_name=student.get('full_name') or str(uid),last_name=None,is_bot=False)
        class Message:
            def __init__(self,m):self.base=m;self.from_user=user
            def __getattr__(self,k):return getattr(self.base,k)
            async def reply_text(self,text,*args,**kw):
                kw['reply_markup']=rendered(kw.get('reply_markup'),s)
                return await self.base.reply_text(text,*args,**kw)
        baseq=getattr(update,'callback_query',None)
        message=Message(update.effective_message) if getattr(update,'effective_message',None) else None
        class Query:
            from_user=user
            def __getattr__(self,k):return getattr(baseq,k)
            async def edit_message_text(self,text,*args,**kw):
                kw['reply_markup']=rendered(kw.get('reply_markup'),s)
                return await baseq.edit_message_text(text,*args,**kw)
        q=Query() if baseq else None
        if q:q.data=data
        proxy=ActingUpdate(update,q,user,message)
        child=ContextProxy(context,s['state'],BotProxy(context.bot,uid,admin,lambda markup:rendered(markup,s)),uid)
        await sql(db,'INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',(admin,'admin_student_action',f'user={uid};action={data or "message"}'),write=True)
        if q:await old_button(proxy,child)
        else:await old_private(proxy,child)
        if not await db.get_student(uid):context.user_data.pop('control_session',None)

    async def home(q,s):
        await q.edit_message_text(f"👁 تحكّم فعلي بحساب الطالب {s['uid']}\nالإجراءات تغيّر حساب الطالب المحدد.",reply_markup=rendered(ns['main_menu'](False),s))

    async def button(update,context):
        q=update.callback_query;data=str(q.data or '');uid=q.from_user.id
        handled=data.startswith(('ctl_','acting|','managed|','preview_student|')) or data=='preview_exit'
        if not handled:
            context.user_data.pop('control_session',None);context.user_data.pop('control_input',None)
            return await old_button(update,context)
        if not ns['is_admin'](uid):await q.answer('للإدارة فقط.',show_alert=True);return
        try:
            if data in {'ctl_exit','preview_exit'}:
                for key in ['control_session','control_input','preview_student','control_rebuild']:context.user_data.pop(key,None)
                await q.answer();await q.edit_message_text('لوحة الإدارة',reply_markup=ns['main_menu'](True));return
            if data.startswith('preview_student|'):
                target=int(data.split('|')[1])
                if ns['is_admin'](target) or not await db.get_student(target):raise ValueError('اختر حساب طالب موجود.')
                context.user_data.pop('preview_student',None);context.user_data.pop('control_input',None)
                s={'uid':target,'nonce':secrets.token_hex(6),'at':time.monotonic(),'state':{},'routes':{}}
                context.user_data['control_session']=s;await q.answer();await home(q,s);return
            s=session(context)
            if data.startswith('managed|'):
                _,nonce,key=data.split('|')
                if not s or nonce!=s['nonce'] or key not in s['routes']:raise ValueError('هذا الزر لجلسة قديمة.')
                data=s['routes'][key]
            if data.startswith('acting|'):
                _,nonce,key=data.split('|')
                if not s or nonce!=s['nonce'] or key not in s['routes']:raise ValueError('هذا الزر لجلسة قديمة. افتح واجهة الطالب مجدداً.')
                context.user_data.pop('control_input',None)
                return await acting(update,context,s,s['routes'][key])
            if data in {'ctl_home','ctl_account'} or data.startswith(('ctl_action|','ctl_profile|','ctl_access|')) or data in {'ctl_parent','ctl_progress'}:
                if not s:raise ValueError('اختر الطالب من المعاينة أولاً.')
                if ns['is_admin'](s['uid']):raise ValueError('لا يمكن إدارة حساب أدمن كطالب.')
                if data=='ctl_home':context.user_data.pop('control_input',None);await q.answer();await home(q,s);return
                if data=='ctl_account':context.user_data.pop('control_input',None);await q.answer();await account(q,s['uid'],s);return
                if data.startswith('ctl_action|'):return await acting(update,context,s,data.split('|',1)[1])
                if data.startswith('ctl_access|'):
                    from admin_student_access import change_access
                    action=data.split('|')[1]
                    await change_access(db,s['uid'],action,uid);await q.answer('تم حفظ الحالة.');await account(q,s['uid'],s);return
                if data.startswith('ctl_profile|'):state={'kind':'profile','field':data.split('|')[1],'target':s['uid'],'nonce':s['nonce']};prompt='أرسل القيمة الجديدة لهذا الحقل.'
                elif data=='ctl_parent':state={'kind':'parent','target':s['uid'],'nonce':s['nonce']};prompt='أرسل ID ولي الأمر لإضافته. لإزالة ربط محدد أرسل: حذف ثم ID. لا يمكن ربط حساب طالب كولي أمر.'
                else:state={'kind':'progress','target':s['uid'],'nonce':s['nonce']};prompt='أرسل: الفصل | المحاضرات | الحالة\nمثال: 3 | 1+2+3 | مكتمل\nأو: 3 | 1+2 | غير مكتمل\nهذا تعديل إداري للإنجاز دون منح XP تلقائي.'
            elif data.startswith('ctl_calendar'):
                context.user_data.pop('control_input',None);await q.answer();await calendar(q,int(data.split('|')[1]) if '|' in data else 0);return
            elif data=='ctl_rebuild':
                state={'kind':'rebuild'};prompt='أرسل: الفصل | أول محاضرة | تاريخ البداية | أيام الدراسة\nمثال: 3 | 5 | 09/10/2026 | الاثنين،الأربعاء،الجمعة\nيمكن اختيار محاضرات أقدم أو أحدث. سأعرض الجدول للتأكيد قبل الحفظ.'
            elif data=='ctl_rebuild_confirm':
                draft=context.user_data.pop('control_rebuild',None)
                if not draft or time.monotonic()-draft['at']>900:raise ValueError('انتهت صلاحية التأكيد؛ أعد تحديد الجدول.')
                await q.answer();await rebuild_course(db,draft['rows'],draft['days'],uid)
                ns['_ADMIN_COURSE_DAYS']=draft['days']
                await q.edit_message_text('✅ أعيد تنظيم جدول الدورة فعلياً. إنجازات الطلاب وامتحاناتهم السابقة محفوظة.',reply_markup=menu([[B('عرض الجدول',callback_data='ctl_calendar')]]));return
            elif data.startswith('ctl_group|'):
                _,ch,pno=data.split('|');await q.answer()
                await q.edit_message_text('تعديل اسم التحضير ومحاضراته في الدليل والجداول:',reply_markup=menu([[B('✏️ الاسم',callback_data=f'ctl_groupfield|{ch}|{pno}|title')],[B('🎬 المحاضرات',callback_data=f'v51_prep_edit|{ch}|{pno}')]]));return
            elif data.startswith('ctl_groupfield|'):
                _,ch,pno,field=data.split('|');state={'kind':'group_title','chapter':int(ch),'prep':int(pno)};prompt='أرسل الاسم الجديد للتحضير.'
            elif data=='ctl_add':state={'kind':'add'};prompt='أرسل: الفصل | المحاضرات | تاريخ التحضير | اسم التحضير\nمثال: 3 | 5+6 | 09/10/2026 | مراجعة المحاضرتين'
            elif data.startswith('ctl_row|'):
                pid=int(data.split('|')[1]);r=await sql(db,'SELECT * FROM biology_preparations WHERE prep_no=%s;',(pid,),True)
                if not r:raise ValueError('التحضير غير موجود.')
                await q.answer();await q.edit_message_text(f"{r['title'] or 'تحضير'}\nالفصل {r['chapter']} | {r['lectures']}\n{r['target_date']}",reply_markup=menu([[B(label,callback_data=f'ctl_rowfield|{pid}|{field}')] for field,label in [('title','✏️ الاسم'),('lectures','🎬 المحاضرات'),('chapter','📘 تغيير الفصل والمحاضرات'),('target_date','📅 التاريخ'),('delete','🗑 حذف التحضير')]]));return
            elif data.startswith('ctl_rowfield|'):
                _,pid,field=data.split('|');r=await sql(db,'SELECT * FROM biology_preparations WHERE prep_no=%s;',(int(pid),),True)
                if not r:raise ValueError('التحضير غير موجود.')
                if field=='delete':
                    context.user_data['control_input']={'kind':'row_delete','id':int(pid),'at':time.monotonic()}
                    await q.answer();await q.edit_message_text('هل تؤكد حذف هذا التحضير من جدول الدورة؟ يبقى تقدم الطلاب وامتحاناتهم محفوظاً.',reply_markup=menu([[B('تأكيد الحذف',callback_data=f'ctl_delete_confirm|{pid}',style='danger')]]));return
                state={'kind':'row','id':int(pid),'field':field,'chapter':r['chapter']};prompt='أرسل '+{'title':'الاسم الجديد.','lectures':'أرقام المحاضرات مثل 1+2+3.','target_date':'التاريخ بصيغة 09/10/2026.','chapter':'الفصل والمحاضرات بصيغة: 4 | 1+2+3'}[field]
            elif data.startswith('ctl_delete_confirm|'):
                state=context.user_data.pop('control_input',None);pid=int(data.split('|')[1])
                if not state or state['kind']!='row_delete' or state['id']!=pid or time.monotonic()-state['at']>900:raise ValueError('أعد تأكيد الحذف.')
                await edit_course_row(db,pid,'delete',None,uid);await q.answer();await calendar(q);return
            else:raise ValueError('خيار غير صالح.')
            for old_state in ['edu_input','v51_edit_prep','v54_school_exam_media','school_exam_schedule']:context.user_data.pop(old_state,None)
            context.user_data['control_input']={**state,'at':time.monotonic()}
            await q.answer();await q.edit_message_text(prompt,reply_markup=menu([]))
        except (ValueError,KeyError,IndexError) as exc:await q.answer(str(exc)[:180],show_alert=True)
    ns['button_handler']=button

    def valid_lectures(ch,raw):
        nums=numbers(raw);allowed={int(v[0]) for v in ns['PLAYLISTS'].get(ch,[])}
        if not nums or not set(nums)<=allowed:raise ValueError('أرقام المحاضرات غير صحيحة لهذا الفصل.')
        return nums
    def parse_date(value):return datetime.strptime(value.strip(),'%d/%m/%Y').date()
    async def private(update,context):
        uid=update.effective_user.id;state=context.user_data.get('control_input');s=session(context)
        if (state or s) and not ns['is_admin'](uid):context.user_data.pop('control_input',None);context.user_data.pop('control_session',None);return
        if not state:
            if s:return await acting(update,context,s)
            return await old_private(update,context)
        msg=update.effective_message
        try:
            if time.monotonic()-state['at']>900:context.user_data.pop('control_input',None);raise ValueError('انتهت مهلة الإدخال؛ افتح التعديل مجدداً.')
            text=(msg.text or '').strip()
            if not text:raise ValueError('أرسل نصاً لهذه العملية.')
            kind=state['kind']
            if 'target' in state and (not s or state['nonce']!=s['nonce'] or state['target']!=s['uid']):raise ValueError('تغير الطالب المحدد. افتح التعديل من جديد.')
            if kind=='rebuild':
                ch,lecture,day,rawdays=[v.strip() for v in text.split('|')];ch=int(ch);lecture=int(lecture);first=parse_date(day)
                if first<datetime.now(ZONE).date():raise ValueError('اختر اليوم أو تاريخاً قادماً لتجنب إنذارات بأثر رجعي.')
                names={'الاثنين':0,'الإثنين':0,'الثلاثاء':1,'الأربعاء':2,'الاربعاء':2,'الخميس':3,'الجمعة':4,'السبت':5,'الأحد':6,'الاحد':6}
                days=sorted({names[v.strip()] for v in rawdays.replace('،',',').split(',')})
                valid_lectures(ch,str(lecture));rows=course_plan(ns['CHAPTER_PREPARATION_DISTRIBUTION'],ch,lecture,first,days)
                context.user_data['control_rebuild']={'rows':rows,'days':days,'at':time.monotonic()}
                context.user_data.pop('control_input',None)
                sample='\n'.join(f'{d:%d/%m} — الفصل {c}: {n.replace(",","+")}' for c,p,n,d in rows[:5])
                await msg.reply_text(f'معاينة الجدول الجديد: {len(rows)} تحضير\n{sample}\nنهاية المنهج: {rows[-1][3]:%d/%m/%Y}\nسيستبدل جدول الدورة، مع حفظ نسخة تاريخية وإبقاء إنجاز الطلاب.',reply_markup=menu([[B('✅ اعتماد الجدول',callback_data='ctl_rebuild_confirm',style='success')]]));return
            if kind=='profile':
                field=state['field'];value=text
                if field in {'xp','warnings'}:
                    value=int(text)
                    if not 0<=value<=10000000:raise ValueError('استخدم عدداً غير سالب ضمن 10000000.')
                elif not 1<=len(text)<=150:raise ValueError('النص يجب أن يكون بين 1 و150 حرفاً.')
                await edit_profile(db,state['target'],field,value,uid)
            elif kind=='progress':
                ch,raw,status=[v.strip() for v in text.split('|')];ch=int(ch);nums=valid_lectures(ch,raw)
                if status not in {'مكتمل','غير مكتمل'}:raise ValueError('الحالة: مكتمل أو غير مكتمل.')
                def op():
                    with db.connect() as conn,conn.cursor() as cur:
                        for num in nums:
                            cur.execute('''INSERT INTO biology_lecture_progress(user_id,chapter,lecture,completed_at) VALUES(%s,%s,%s,%s)
                                ON CONFLICT(user_id,chapter,lecture) DO UPDATE SET completed_at=EXCLUDED.completed_at;''',(s['uid'],ch,num,datetime.now(ZONE) if status=='مكتمل' else None))
                        cur.execute('DELETE FROM biology_lecture_reviews WHERE user_id=%s AND chapter=%s AND lecture=ANY(%s);',(s['uid'],ch,nums))
                        cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',(uid,'admin_lecture_progress',f'user={s["uid"]};chapter={ch};lectures={nums};status={status}'));conn.commit()
                await db.run(op)
            elif kind=='parent':
                remove=text.startswith('حذف');parent=int(text.replace('حذف','').strip())
                if parent<=0 or (not remove and (ns['is_admin'](parent) or await db.get_student(parent))):raise ValueError('اختر ID ولي أمر صحيح لا يملك حساب طالب أو أدمن.')
                def op():
                    with db.connect() as conn,conn.cursor() as cur:
                        cur.execute('SELECT user_id FROM biology_students WHERE user_id=%s FOR UPDATE;',(s['uid'],))
                        if not cur.fetchone():raise ValueError('الطالب غير موجود.')
                        if remove:cur.execute('DELETE FROM biology_parent_links WHERE student_id=%s AND parent_chat_id=%s;',(s['uid'],parent))
                        else:cur.execute('INSERT INTO biology_parent_links(student_id,parent_chat_id,approved) VALUES(%s,%s,TRUE) ON CONFLICT(student_id,parent_chat_id) DO UPDATE SET approved=TRUE;',(s['uid'],parent))
                        cur.execute('SELECT * FROM biology_parent_links WHERE student_id=%s AND approved=TRUE ORDER BY linked_at LIMIT 1;',(s['uid'],));p=cur.fetchone() or {}
                        cur.execute('UPDATE biology_students SET parent_chat_id=%s,parent_approved=%s,parent_full_name=%s,parent_username=%s WHERE user_id=%s;',(p.get('parent_chat_id'),bool(p),p.get('parent_full_name'),p.get('parent_username'),s['uid']))
                        cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',(uid,'admin_parent_link',f'user={s["uid"]};parent={parent};remove={remove}'));conn.commit()
                await db.run(op)
            elif kind=='group_title':
                if len(text)>150:raise ValueError('الاسم أطول من 150 حرفاً.')
                ch,pno=state['chapter'],state['prep'];nums=ns['CHAPTER_PREPARATION_DISTRIBUTION'][ch][pno-1]
                def op():
                    with db.connect() as conn,conn.cursor() as cur:
                        cur.execute('''INSERT INTO biology_preparation_catalog_overrides(chapter,prep_no,lectures,title,updated_by)
                            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(chapter,prep_no) DO UPDATE SET title=EXCLUDED.title,updated_by=EXCLUDED.updated_by,updated_at=CURRENT_TIMESTAMP;''',(ch,pno,','.join(map(str,nums)),text,uid))
                        cur.execute('UPDATE biology_preparations SET title=%s WHERE chapter=%s AND chapter_prep_no=%s;',(text,ch,pno));conn.commit()
                await db.run(op)
            elif kind=='row':
                field=state['field'];value=text
                if field=='lectures':value=','.join(map(str,valid_lectures(state['chapter'],text)))
                if field=='chapter':
                    ch,raw=[x.strip() for x in text.split('|')];ch=int(ch);nums=valid_lectures(ch,raw)
                    pno=next((i for i,g in enumerate(ns['CHAPTER_PREPARATION_DISTRIBUTION'][ch],1) if nums[0] in g),1)
                    value=(ch,pno,','.join(map(str,nums)))
                if field=='title' and len(text)>150:raise ValueError('الاسم أطول من 150 حرفاً.')
                if field=='target_date':
                    value=parse_date(text)
                    if value<datetime.now(ZONE).date():raise ValueError('اختر اليوم أو تاريخاً قادماً.')
                await edit_course_row(db,state['id'],field,value,uid)
            elif kind=='add':
                ch,raw,day,title=[v.strip() for v in text.split('|')];ch=int(ch);nums=valid_lectures(ch,raw);day=parse_date(day)
                if day<datetime.now(ZONE).date() or not title or len(title)>150:raise ValueError('تأكد من التاريخ واسم التحضير.')
                def op():
                    with db.connect() as conn,conn.cursor() as cur:
                        cur.execute('LOCK TABLE biology_preparations IN EXCLUSIVE MODE;')
                        cur.execute('SELECT 1 FROM biology_preparations WHERE target_date=%s;',(day,))
                        if cur.fetchone():raise ValueError('اليوم مستخدم؛ عدل محاضرات تحضيره بدلاً من إضافة تحضير آخر.')
                        cur.execute('SELECT COALESCE(MAX(prep_no),0)+1 AS n FROM biology_preparations;');pid=cur.fetchone()['n']
                        pno=next((i for i,g in enumerate(ns['CHAPTER_PREPARATION_DISTRIBUTION'][ch],1) if nums[0] in g),1)
                        cur.execute('INSERT INTO biology_preparations(prep_no,chapter,chapter_prep_no,lectures,target_date,title,published,published_at,needs_announcement) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s);',(pid,ch,pno,','.join(map(str,nums)),day,title,day<=datetime.now(ZONE).date(),datetime.now(ZONE) if day<=datetime.now(ZONE).date() else None,day<=datetime.now(ZONE).date()))
                        cur.execute("INSERT INTO biology_settings(key,value) VALUES('admin_course_calendar','1') ON CONFLICT(key) DO UPDATE SET value='1';")
                        cur.execute('INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);',(uid,'admin_course_add',str(pid)));conn.commit()
                await db.run(op)
            else:raise ValueError('استخدم زر التأكيد.')
            context.user_data.pop('control_input',None)
            await msg.reply_text('✅ حُفظ التغيير فعلياً.',reply_markup=menu([[B('إدارة الطالب',callback_data='ctl_account')]] if s else [[B('جدول الدورة',callback_data='ctl_calendar')]]))
        except (ValueError,KeyError,IndexError) as exc:await msg.reply_text('تعذر الحفظ: '+str(exc)+'\nراجع الصيغة وأعد الإرسال.')
    ns['private_messages']=private
    old_start=ns['start']
    async def start(update,context):
        context.user_data.pop('control_session',None);context.user_data.pop('control_input',None)
        return await old_start(update,context)
    ns['start']=start
