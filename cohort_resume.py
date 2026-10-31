"""The 9 November cohort: 5/27..39, seven days, 5/1..26, fourteen days, 4."""
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
import json

RESUME=date(2026,11,9)
MARKER='biology_resume_20261109'
PHASES=((1,5,tuple(range(27,40)),7),(2,5,tuple(range(1,27)),14),(3,4,tuple(range(1,10)),0))
ZONE=ZoneInfo('Asia/Baghdad')

def build(first=RESUME,days=(0,1,6),occupied=()):
    """The original biology cadence: Monday, Tuesday, Sunday; double Tuesday in 5."""
    allowed=set(days);occupied=set(occupied)
    if not allowed or not allowed<=set(range(7)):raise ValueError('أيام الدراسة غير صالحة.')
    out=[];cursor=first
    for phase,ch,lectures,rest in PHASES:
        pending=list(lectures)
        while pending:
            while cursor.weekday() not in allowed or cursor in occupied:cursor+=timedelta(days=1)
            count=2 if cursor.weekday()==1 and ch==5 else 1
            group=pending[:count];pending=pending[count:]
            out.append(dict(cohort_phase=phase,chapter=ch,chapter_prep_no=group[0],lectures=','.join(map(str,group)),target_date=cursor))
            cursor+=timedelta(days=1)
        # cursor is the day after the last lecture: reserve full calendar rest days.
        cursor+=timedelta(days=rest)
    return out

def schema(db):
    with db.connect() as conn,conn.cursor() as cur:
        cur.execute('''ALTER TABLE biology_preparations ADD COLUMN IF NOT EXISTS cohort_phase INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE biology_students ADD COLUMN IF NOT EXISTS biology_reordered BOOLEAN NOT NULL DEFAULT FALSE;
            CREATE TABLE IF NOT EXISTS biology_manual_exam_links(
                prep_no INTEGER NOT NULL REFERENCES biology_preparations(prep_no) ON DELETE CASCADE,
                task_id INTEGER NOT NULL REFERENCES biology_tasks(id) ON DELETE CASCADE,
                linked_by BIGINT NOT NULL,linked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(prep_no,task_id));
            CREATE TABLE IF NOT EXISTS biology_cohort_plan_history(
                id BIGSERIAL PRIMARY KEY,created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                snapshot JSONB NOT NULL);''')
        conn.commit()

def activate(db):
    with db.connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(110914);')
        cur.execute('SELECT value FROM biology_settings WHERE key=%s FOR UPDATE;',(MARKER,))
        if cur.fetchone():return False
        cur.execute('LOCK TABLE biology_preparations IN EXCLUSIVE MODE;')
        cur.execute('''INSERT INTO biology_cohort_plan_history(snapshot) SELECT jsonb_build_object(
            'preparations',COALESCE((SELECT jsonb_agg(to_jsonb(p)) FROM biology_preparations p),'[]'::jsonb),
            'manual_links',COALESCE((SELECT jsonb_agg(to_jsonb(m)) FROM biology_manual_exam_links m),'[]'::jsonb),
            'foundations',COALESCE((SELECT jsonb_agg(to_jsonb(g)) FROM biology_genetics_foundations g),'[]'::jsonb));''')
        cur.execute('''SELECT * FROM biology_preparations WHERE published=FALSE OR chapter IN(4,5)
            OR target_date>=(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE
            ORDER BY target_date,prep_no FOR UPDATE;''');old=cur.fetchall();ids=[r['prep_no'] for r in old]
        # Old exam requirements keep their original lectures when prep rows are reused.
        cur.execute('''SELECT DISTINCT lp.definition_id FROM biology_linked_exam_preparations lp
            JOIN biology_linked_exam_definitions d ON d.id=lp.definition_id AND d.target_scope='course'
            JOIN biology_preparations p ON p.chapter=lp.chapter AND p.chapter_prep_no=lp.prep_no
            WHERE p.prep_no=ANY(%s);''',(ids,))
        for definition in cur.fetchall():
            did=definition['definition_id']
            for position,(ch,lec) in enumerate(sorted(db._v47_required_lectures(cur,did))):
                cur.execute('''INSERT INTO biology_linked_exam_lectures(definition_id,chapter,lecture,position)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(definition_id,chapter,lecture) DO NOTHING;''',(did,ch,lec,position))
            cur.execute('DELETE FROM biology_linked_exam_preparations WHERE definition_id=%s;',(did,))
            cur.execute('UPDATE biology_linked_exam_definitions SET prep_no=NULL WHERE id=%s;',(did,))
        cur.execute('DELETE FROM biology_manual_exam_links WHERE prep_no=ANY(%s);',(ids,))
        cur.execute('SELECT COALESCE(MAX(prep_no),0) AS n,COALESCE(MAX(target_date),CURRENT_DATE) AS last,COALESCE(MIN(target_date),CURRENT_DATE) AS first FROM biology_preparations;');limits=cur.fetchone()
        park=limits['last']+timedelta(days=10000)
        if ids:
            cur.execute('''UPDATE biology_preparations p SET target_date=%s::DATE+m.n
                FROM unnest(%s::INTEGER[],%s::INTEGER[]) m(id,n) WHERE p.prep_no=m.id;''',(park,ids,list(range(len(ids)))))
        cur.execute('SELECT target_date FROM biology_preparations WHERE NOT(prep_no=ANY(%s));',(ids,));occupied={r['target_date'] for r in cur.fetchall()}
        cur.execute("SELECT value FROM biology_settings WHERE key='admin_course_days';");saved=cur.fetchone()
        days=json.loads(saved['value']) if saved and saved['value'] else [0,1,6]
        cur.execute('SELECT days FROM biology_midyear_break WHERE id=1;');holiday=cur.fetchone()
        rows=build(RESUME+timedelta(days=holiday['days'] if holiday else 0),days,occupied)
        next_id=limits['n'];used=[]
        for i,row in enumerate(rows):
            if i<len(old):pid=old[i]['prep_no']
            else:
                next_id+=1;pid=next_id
                cur.execute('INSERT INTO biology_preparations(prep_no,target_date,lectures,chapter,chapter_prep_no) VALUES(%s,%s,%s,%s,%s);',
                    (pid,park+timedelta(days=len(old)+i+1),row['lectures'],row['chapter'],row['chapter_prep_no']))
            used.append(pid)
            cur.execute('''UPDATE biology_preparations SET target_date=%s,lectures=%s,chapter=%s,chapter_prep_no=%s,
                cohort_phase=%s,published=FALSE,published_at=NULL,title=%s WHERE prep_no=%s;''',
                (row['target_date'],row['lectures'],row['chapter'],row['chapter_prep_no'],row['cohort_phase'],f"خطة الدورة — المرحلة {row['cohort_phase']}",pid))
        # Keep unused historical IDs and their references; put them outside the live calendar.
        removed=[pid for pid in ids if pid not in used]
        if removed:
            archive=limits['first']-timedelta(days=len(removed)+100)
            cur.execute('''UPDATE biology_preparations p SET target_date=%s::DATE+m.n,published=TRUE,
                cohort_phase=0,published_at=COALESCE(published_at,CURRENT_TIMESTAMP)
                FROM unnest(%s::INTEGER[],%s::INTEGER[]) m(id,n) WHERE p.prep_no=m.id;''',(archive,removed,list(range(len(removed)))))
        # Foundations retain their IDs and weekly Tuesday delivery, outside both rests.
        position_foundations(cur,rows)
        cur.execute("UPDATE biology_early_preparation_unlocks SET completed_at=CURRENT_TIMESTAMP WHERE study_track='course' AND completed_at IS NULL;")
        cur.execute('''DELETE FROM biology_lecture_reviews r USING biology_students s
            WHERE r.user_id=s.user_id AND s.study_track='course' AND r.chapter=5
            AND EXISTS(SELECT 1 FROM biology_lecture_progress lp WHERE lp.user_id=r.user_id AND lp.chapter=r.chapter
                AND lp.lecture=r.lecture AND lp.completion_method='prior_learning');''')
        cur.execute("DELETE FROM biology_lecture_progress lp USING biology_students s WHERE lp.user_id=s.user_id AND s.study_track='course' AND lp.chapter=5 AND lp.completion_method='prior_learning';")
        cur.execute('UPDATE biology_students SET biology_reordered=TRUE;')
        cur.execute("UPDATE biology_students SET current_chapter=5 WHERE study_track='course';")
        cur.execute('ALTER TABLE biology_students ALTER COLUMN biology_reordered SET DEFAULT TRUE;')
        for key,value in [(MARKER,'1'),('admin_course_calendar','1'),('biology_v11_calendar_done','1'),('v12_double_tuesday_done','1'),('v13_genetics_schedule','1'),('admin_course_days',json.dumps(days))]:
            cur.execute('INSERT INTO biology_settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;',(key,value))
        cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(0,'resume_20261109','5:27-39; rest7; 5:1-26; rest14; 4; finals manual');")
        conn.commit();return True

def position_foundations(cur,rows):
    phases={p:(min(r['target_date'] for r in rows if r['cohort_phase']==p),max(r['target_date'] for r in rows if r['cohort_phase']==p)) for p in (1,2,3) if any(r['cohort_phase']==p for r in rows)}
    gaps=[(phases[p][1],phases[p+1][0]) for p in (1,2) if p in phases and p+1 in phases]
    cur.execute("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE AS today;");today=cur.fetchone()['today']
    cur.execute('SELECT number FROM biology_genetics_foundations WHERE target_date IS NULL OR target_date>%s ORDER BY number;',(today,))
    pending=cur.fetchall();day=max(today+timedelta(days=1),min(r['target_date'] for r in rows))
    for r in pending:
        while day.weekday()!=1 or any(end<day<start for end,start in gaps):day+=timedelta(days=1)
        cur.execute('UPDATE biology_genetics_foundations SET target_date=%s WHERE number=%s;',(day,r['number']));day+=timedelta(days=7)

def install(ns,db):
    old_init=ns['init_db']
    def init():old_init();schema(db)
    ns['init_db']=db.init_db=init
    old_post=ns['post_init']
    async def post(app):
        ns['init_db']()
        await db.run(lambda:activate(db))
        await old_post(app)
    ns['post_init']=post
    async def status():
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute('''SELECT cohort_phase,chapter,MIN(target_date) AS first,MAX(target_date) AS last,
                    COUNT(*) AS groups FROM biology_preparations WHERE cohort_phase>0
                    GROUP BY cohort_phase,chapter ORDER BY cohort_phase;''');phases=cur.fetchall()
                cur.execute('SELECT used_at FROM biology_midyear_break WHERE id=1;');used=cur.fetchone()
                return phases,used
        return await db.run(op)
    db.cohort_plan_status=status
    B,K=ns['InlineKeyboardButton'],ns['InlineKeyboardMarkup']
    old_menu=ns['main_menu']
    def menu(admin=False):
        old=old_menu(admin);markup=K([*old.inline_keyboard,[B('🗓 خطة الأحياء والاستراحات',callback_data='cohort|status')]])
        from student_sections import _VIEW,filter_keyboard
        view=_VIEW.get()
        return filter_keyboard(markup,view['flags']) if view and not admin else markup
    ns['main_menu']=menu
    async def calendar_context(uid):
        student=await db.get_student(uid)
        if not student or student.get('study_track')!='course' or not student.get('biology_reordered'):return None
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE AS today;");today=cur.fetchone()['today']
                cur.execute('SELECT * FROM biology_preparations WHERE cohort_phase>0 ORDER BY target_date;');rows=cur.fetchall()
                cur.execute('SELECT used_at FROM biology_midyear_break WHERE id=1;');used=cur.fetchone()
                cur.execute('SELECT chapter,lecture FROM biology_lecture_progress WHERE user_id=%s AND completed_at IS NOT NULL;',(uid,))
                done={(r['chapter'],r['lecture']) for r in cur.fetchall()}
                return rows,today,used,done
        return await db.run(op)
    def pause(data):
        if not data:return None
        rows,today,used,done=data
        if not rows:return None
        phases={p:(min(r['target_date'] for r in rows if r['cohort_phase']==p),max(r['target_date'] for r in rows if r['cohort_phase']==p)) for p in (1,2,3) if any(r['cohort_phase']==p for r in rows)}
        if today<rows[0]['target_date']:return rows[0]['target_date']
        for p in (1,2):
            if p in phases and p+1 in phases and phases[p][1]<today<phases[p+1][0]:return phases[p+1][0]
        if used and used['used_at'].astimezone(ZONE).date()<=today<used['used_at'].astimezone(ZONE).date()+timedelta(days=14):
            if not any(r['target_date']==today and r['published'] for r in rows):
                return min((r['target_date'] for r in rows if r['target_date']>today),default=None)
        return None
    old_today=ns['v51_today_preparation']
    async def today_screen(query):
        until=pause(await calendar_context(query.from_user.id))
        if until:
            await query.answer()
            await query.edit_message_text(ns['bold'](f'🏖 الدورة في استراحة حالياً.\nتُستأنف المحاضرات يوم {until:%d/%m/%Y}.\nمواعيد الفاينل يعلنها الأستاذ.'),parse_mode='HTML',reply_markup=K([[B('🗓 عرض خطة الدورة',callback_data='cohort|status')],[ns['back_menu']()]]));return
        return await old_today(query)
    ns['v51_today_preparation']=today_screen
    old_unlock=db.v52_unlock_next_preparation
    async def unlock(uid):
        context=await calendar_context(uid);until=pause(context)
        if context and not until:
            rows,today,used,done=context
            candidate=next((r for r in rows if any((r['chapter'],int(n)) not in done for n in r['lectures'].split(','))),None)
            if candidate:
                phase_start=min(r['target_date'] for r in rows if r['cohort_phase']==candidate['cohort_phase'])
                if today<phase_start:until=phase_start
        if until:return {'status':'cohort_pause','available_on':until}
        return await old_unlock(uid)
    db.v52_unlock_next_preparation=unlock
    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query;data=q.data or ''
        if data.startswith('gen|count|'):
            await q.answer('الخطة الجديدة تحافظ على ترتيب المراحل والاستراحات؛ عدّل مواعيدها من إدارة التحاضير.',show_alert=True);return
        if data!='cohort|status':return await old_button(update,context)
        if not ns['is_admin'](q.from_user.id) and not await db.section_enabled(q.from_user.id,'lectures'):
            await q.answer('خانة المحاضرات معطّلة لحسابك.',show_alert=True);return
        phases,used=await status();lines=['🗓 خطة الأحياء','الفاينلات يحدد الأستاذ مواعيدها وينشرها يدوياً.']
        titles={1:'الخامس — المحاضرات 27–39',2:'الخامس — المحاضرات 1–26',3:'الفصل الرابع كاملاً'}
        for r in phases:
            lines.append(f"\n{titles[r['cohort_phase']]}\n{r['first']:%d/%m/%Y} إلى {r['last']:%d/%m/%Y}")
            if r['cohort_phase']<3:lines.append('ثم استراحة '+('7 أيام' if r['cohort_phase']==1 else '14 يوماً'))
        lines.append('\nعطلة نصف السنة: '+('استُخدمت' if used else '14 يوماً، مرة واحدة بقرار الأستاذ'))
        buttons=[]
        if ns['is_admin'](q.from_user.id) and not used:buttons.append([B('🏖 منح عطلة نصف السنة — 14 يوماً',callback_data='prep_holiday')])
        buttons.append([ns['back_menu']()]);await q.answer();await q.edit_message_text(ns['bold']('\n'.join(lines)),parse_mode='HTML',reply_markup=K(buttons))
    ns['button_handler']=button
    old_publish=ns['mark_preparation_published']
    async def published(pid):
        result=await old_publish(pid)
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute('''UPDATE biology_students SET current_chapter=p.chapter FROM biology_preparations p
                    WHERE p.prep_no=%s AND p.cohort_phase>0 AND biology_students.study_track='course';''',(pid,));conn.commit()
        await db.run(op);return result
    ns['mark_preparation_published']=db.mark_preparation_published=published
    for name in ('v47_choose_track','set_student_onboarding'):
        old=getattr(db,name)
        def wrap(fn):
            async def choose(uid,track,*args,**kwargs):
                result=await fn(uid,track,*args,**kwargs)
                if track!='course' or not result:return result
                def op():
                    with db.connect() as conn,conn.cursor() as cur:
                        cur.execute('''SELECT chapter FROM biology_preparations WHERE cohort_phase>0
                            ORDER BY published DESC,CASE WHEN published THEN target_date END DESC,target_date LIMIT 1;''')
                        row=cur.fetchone()
                        if row:
                            cur.execute("UPDATE biology_students SET current_chapter=%s WHERE user_id=%s;",(row['chapter'],uid));conn.commit()
                await db.run(op);return await db.get_student(uid)
            return choose
        setattr(db,name,wrap(old))
        if name in ns:ns[name]=getattr(db,name)
    # A teacher date adjustment must retain the two rest windows and phase order.
    old_reschedule=db._reschedule_biology_calendar
    def reschedule(cur,first_date=None,direction=None,earliest_date=None):
        cur.execute('SELECT * FROM biology_preparations WHERE published=FALSE ORDER BY target_date,prep_no FOR UPDATE;');rows=cur.fetchall()
        if not rows or not all(r.get('cohort_phase') for r in rows):
            return old_reschedule(cur,first_date,direction,earliest_date)
        cur.execute("SELECT value FROM biology_settings WHERE key='admin_course_days';");saved=cur.fetchone();days=set(json.loads(saved['value']) if saved and saved['value'] else [0,1,6])
        cur.execute('SELECT target_date FROM biology_preparations WHERE published=TRUE;');occupied={r['target_date'] for r in cur.fetchall()}
        if direction:
            step=timedelta(days=1 if direction=='delay' else -1);first_date=rows[0]['target_date']+step
            while first_date.weekday() not in days or first_date in occupied:first_date+=step
            if earliest_date and first_date<=earliest_date:return 'too_early'
        first_phase=rows[0]['cohort_phase']
        if first_phase>1:
            cur.execute('SELECT MAX(target_date) AS last FROM biology_preparations WHERE cohort_phase=%s;',(first_phase-1,))
            last=cur.fetchone()['last']
            if last:
                floor=last+timedelta(days=8 if first_phase==2 else 15)
                if direction=='advance' and first_date<floor:return 'too_early'
                first_date=max(first_date,floor)
        planned=[];day=first_date;previous=first_phase
        for r in rows:
            if r['cohort_phase']!=previous:day+=timedelta(days=7 if previous==1 else 14)
            while day.weekday() not in days or day in occupied:day+=timedelta(days=1)
            planned.append(day);day+=timedelta(days=1);previous=r['cohort_phase']
        park=max([r['target_date'] for r in rows]+list(occupied)+planned)+timedelta(days=10000)
        ids=[r['prep_no'] for r in rows]
        cur.execute('''UPDATE biology_preparations p SET target_date=%s::DATE+m.n
            FROM unnest(%s::INTEGER[],%s::INTEGER[]) m(id,n) WHERE p.prep_no=m.id;''',(park,ids,list(range(len(ids)))))
        cur.execute('''UPDATE biology_preparations p SET target_date=m.day
            FROM unnest(%s::INTEGER[],%s::DATE[]) m(id,day) WHERE p.prep_no=m.id;''',(ids,planned))
        cur.execute('SELECT * FROM biology_preparations WHERE cohort_phase>0 ORDER BY target_date;');position_foundations(cur,cur.fetchall())
        cur.execute('SELECT * FROM biology_preparations WHERE prep_no=%s;',(ids[0],));return cur.fetchone()
    db._reschedule_biology_calendar=reschedule
