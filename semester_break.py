"""One durable 14-day midyear break; never change real completed work."""
from datetime import timedelta

def install(ns,db):
    old_init=ns['init_db']
    def init():
        old_init()
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS biology_midyear_break(
                id INTEGER PRIMARY KEY CHECK(id=1),actor_id BIGINT NOT NULL,
                used_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                days INTEGER NOT NULL DEFAULT 14 CHECK(days=14),snapshot JSONB NOT NULL);''')
            # Remove only fabricated completion; preserve watched/manual completions and XP.
            cur.execute('''DELETE FROM biology_lecture_reviews r WHERE r.chapter=4
                AND EXISTS(SELECT 1 FROM biology_lecture_progress lp WHERE lp.user_id=r.user_id
                    AND lp.chapter=r.chapter AND lp.lecture=r.lecture
                    AND lp.completion_method='prior_learning');''')
            cur.execute("DELETE FROM biology_lecture_progress WHERE chapter=4 AND completion_method='prior_learning';")
            conn.commit()
    ns['init_db']=db.init_db=init
    async def apply(actor):
        if not ns['is_admin'](actor):return {'status':'forbidden'}
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_xact_lock(110914);')
                cur.execute('SELECT * FROM biology_midyear_break WHERE id=1 FOR UPDATE;')
                used=cur.fetchone()
                if used:return {'status':'already','used_at':used['used_at']}
                cur.execute("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Baghdad')::DATE AS today;")
                today=cur.fetchone()['today']
                cur.execute('LOCK TABLE biology_preparations IN EXCLUSIVE MODE;')
                cur.execute('''SELECT * FROM biology_preparations WHERE target_date>%s
                    OR (target_date=%s AND published=FALSE) ORDER BY target_date,prep_no FOR UPDATE;''',(today,today))
                rows=cur.fetchall()
                if not rows:return {'status':'empty'}
                cur.execute('''INSERT INTO biology_midyear_break(id,actor_id,snapshot)
                    SELECT 1,%s,jsonb_build_object(
                        'course',COALESCE((SELECT jsonb_agg(to_jsonb(p)) FROM biology_preparations p),'[]'::jsonb),
                        'personal',COALESCE((SELECT jsonb_agg(to_jsonb(p)) FROM biology_personal_preparations p),'[]'::jsonb),
                        'foundations',COALESCE((SELECT jsonb_agg(to_jsonb(g)) FROM biology_genetics_foundations g),'[]'::jsonb));''',(actor,))
                ids=[r['prep_no'] for r in rows]
                park=max(r['target_date'] for r in rows)+timedelta(days=10000)
                cur.execute('''UPDATE biology_preparations p SET target_date=%s::DATE+m.n
                    FROM unnest(%s::INTEGER[],%s::INTEGER[]) AS m(id,n) WHERE p.prep_no=m.id;''',(park,ids,list(range(len(ids)))))
                cur.execute('''UPDATE biology_preparations p SET target_date=m.day,published=FALSE,published_at=NULL
                    FROM unnest(%s::INTEGER[],%s::DATE[]) AS m(id,day) WHERE p.prep_no=m.id;''',
                    (ids,[r['target_date']+timedelta(days=14) for r in rows]))
                cur.execute('''UPDATE biology_personal_preparations SET target_date=target_date+14,notified=FALSE,notified_at=NULL
                    WHERE target_date>%s OR (target_date=%s AND notified=FALSE);''',(today,today))
                cur.execute('UPDATE biology_genetics_foundations SET target_date=target_date+14 WHERE target_date>=%s;',(today,))
                cur.execute('''UPDATE biology_early_preparation_unlocks SET original_target_date=original_target_date+14
                    WHERE original_target_date>%s AND completed_at IS NULL;''',(today,))
                cur.execute("SELECT to_regclass('biology_early_unlock_schedule_snapshots') AS name;")
                if cur.fetchone()['name']:
                    cur.execute("UPDATE biology_midyear_break SET snapshot=snapshot||jsonb_build_object('early_snapshots',COALESCE((SELECT jsonb_agg(to_jsonb(s)) FROM biology_early_unlock_schedule_snapshots s),'[]'::jsonb)) WHERE id=1;")
                    cur.execute('UPDATE biology_early_unlock_schedule_snapshots SET target_date=target_date+14 WHERE target_date>%s;',(today,))
                cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'midyear_break_14','once; days=14');",(actor,))
                conn.commit()
                return {'status':'applied','count':len(rows),'next_date':rows[0]['target_date']+timedelta(days=14)}
        return await db.run(op)
    db.apply_midyear_break=apply
    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query
        if q.data!='prep_holiday':return await old_button(update,context)
        result=await apply(q.from_user.id)
        text={'forbidden':'للإدارة فقط.','already':'استُخدمت عطلة نصف السنة سابقاً؛ لا يمكن تكرارها.',
              'empty':'لا توجد محاضرات قادمة لتأجيلها؛ لم تُستهلك العطلة.'}.get(result['status'])
        if text:await q.answer(text,show_alert=True);return
        await q.answer('تم اعتماد عطلة نصف السنة لمدة 14 يوماً.')
        await q.edit_message_text(ns['bold']('🏖 عطلة نصف السنة — 14 يوماً\n'
            f"تم تأجيل المحاضرات القادمة أسبوعين.\nأقرب تحضير: {result['next_date']:%d/%m/%Y}\n"
            'استُخدمت العطلة الوحيدة، وتبقى الإنجازات الحقيقية والحلول والنقاط محفوظة.'),parse_mode='HTML',
            reply_markup=ns['InlineKeyboardMarkup']([[ns['back_menu']()]]))
    ns['button_handler']=button
