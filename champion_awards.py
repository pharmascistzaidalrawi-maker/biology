"""Highest-grade exam/homework awards with durable, recipient-level retries."""
import asyncio
import hashlib
import json
import logging
from champion_certificate import render_certificate
LOG=logging.getLogger(__name__)
COHORT_SQL="CASE WHEN t.kind='homework' THEN 'task:'||t.id::TEXT ELSE COALESCE('school:'||t.school_review_id::TEXT,'exam:'||t.exam_definition_id::TEXT,'task:'||t.id::TEXT) END"

def migrate(db,p):
    with db.connect() as conn,conn.cursor() as cur:
        cur.execute(f'''CREATE TABLE IF NOT EXISTS {p}_certificate_cycles(
            cohort TEXT PRIMARY KEY,claimed_at TIMESTAMPTZ,signature TEXT);
            CREATE TABLE IF NOT EXISTS {p}_certificate_deliveries(
                cohort TEXT NOT NULL,winner_id BIGINT NOT NULL,chat_id BIGINT NOT NULL,
                role TEXT NOT NULL,signature TEXT NOT NULL,message_id BIGINT,
                sent_signature TEXT,last_error TEXT,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(cohort,winner_id,chat_id));
            INSERT INTO {p}_settings(key,value) VALUES('certificate_v58_started_at',CURRENT_TIMESTAMP::TEXT) ON CONFLICT DO NOTHING;''')
        conn.commit()

def install(ns,db,p):
    assert p in ('english','chemistry','islamic','biology','mathematics','physics','arabic','french')
    ns.setdefault('SUBJECT_NAME',{'chemistry':'الكيمياء','islamic':'التربية الإسلامية','english':'اللغة الإنجليزية','biology':'الأحياء','mathematics':'الرياضيات','physics':'الفيزياء','arabic':'اللغة العربية','french':'اللغة الفرنسية'}[p])
    old_init=ns['init_db']
    def init():old_init();migrate(db,p)
    ns['init_db']=db.init_db=init
    async def sql(statement,args=(),one=False,commit=False):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(statement,args)
                result=(cur.fetchone() if one else cur.fetchall()) if cur.description else None
                if commit:conn.commit()
                return result
        return await db.run(op)
    def cohort(task):
        if task['kind']=='homework':return 'task:'+str(task['id'])
        return ('school:'+str(task['school_review_id']) if task.get('school_review_id') else
                'exam:'+str(task['exam_definition_id']) if task.get('exam_definition_id') else 'task:'+str(task['id']))
    async def snapshot(task_id):
        def op():
            with db.connect() as conn,conn.cursor() as cur:
                cur.execute(f'SELECT * FROM {p}_tasks WHERE id=%s;',(task_id,));t=cur.fetchone()
                if not t or t['retired_obligation'] or (t['optional_practice'] and t.get('exam_difficulty')!='hard') or t.get('catchup_source_id'):return None
                key=cohort(t)
                cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,58));',(key,))
                cur.execute(f'SELECT * FROM {p}_certificate_cycles WHERE cohort=%s;',(key,));cycle=cur.fetchone()
                field='id' if t['kind']=='homework' else 'school_review_id' if t.get('school_review_id') else 'exam_definition_id' if t.get('exam_definition_id') else 'id'
                cur.execute(f'''SELECT t.id,t.closed,GREATEST(t.deadline,COALESCE(MAX(e.extended_until),t.deadline)) AS until,
                    CURRENT_TIMESTAMP AS now FROM {p}_tasks t LEFT JOIN {p}_task_extensions e ON e.task_id=t.id
                    WHERE t.{field}=%s AND t.kind=%s AND t.retired_obligation=FALSE AND (t.optional_practice=FALSE OR to_jsonb(t)->>'exam_difficulty'='hard') AND t.catchup_source_id IS NULL
                    GROUP BY t.id;''',(t[field],t['kind']));tasks=cur.fetchall()
                # Finish the submission window (including teacher extensions) first.
                finished=[r for r in tasks if r['until']<=r['now'] or r['closed']]
                if not finished or not any(r['id']==t['id'] for r in finished):return None
                ids=[r['id'] for r in finished]
                cur.execute(f'''SELECT sub.*,s.full_name,s.parent_chat_id,s.parent_approved FROM {p}_submissions sub
                    JOIN {p}_students s ON s.user_id=sub.user_id
                    WHERE sub.task_id=ANY(%s) AND sub.submitted_at IS NOT NULL AND sub.rejected=FALSE AND sub.grade IS NOT NULL AND sub.revision_open=FALSE AND sub.teacher_change_pending=FALSE
                    ORDER BY sub.grade DESC NULLS LAST,sub.submitted_at,sub.user_id;''',(ids,));entries=cur.fetchall()
                if not entries:return None
                top=entries[0]['grade'];winners=[];seen=set()
                for e in entries:
                    if e['grade']==top and e['user_id'] not in seen:winners.append(e);seen.add(e['user_id'])
                group=int(ns.get(p.upper()+'_GROUP_ID') or 0);topic=int(ns.get('CHAMPIONS_TOPIC_ID') or 0)
                if not group or not topic:return None
                recipients=[]
                for winner in winners:
                    uid=winner['user_id']
                    targets=[(group,'group'),(uid,'student')]
                    cur.execute(f'SELECT parent_chat_id FROM {p}_parent_links WHERE student_id=%s AND approved=TRUE;',(uid,))
                    parents=cur.fetchall()
                    targets += [(r['parent_chat_id'],'parent') for r in parents]
                    if winner.get('parent_chat_id') and winner.get('parent_approved'):
                        cur.execute(f'SELECT approved FROM {p}_parent_links WHERE student_id=%s AND parent_chat_id=%s;',(uid,winner['parent_chat_id']))
                        explicit=cur.fetchone()
                        if explicit is None or explicit['approved']:targets.append((winner['parent_chat_id'],'parent'))
                    used=set()
                    for chat,role in targets:
                        if not chat or chat in used:continue
                        used.add(chat);recipients.append((uid,int(chat),role))
                signature=hashlib.sha256(json.dumps([t['title'],t['kind'],[(e['user_id'],e['full_name'],str(e['grade'])) for e in winners],recipients],sort_keys=True).encode()).hexdigest()
                cur.execute(f'''INSERT INTO {p}_certificate_cycles(cohort,claimed_at,signature)
                    VALUES(%s,CURRENT_TIMESTAMP,%s) ON CONFLICT(cohort) DO UPDATE
                    SET claimed_at=CURRENT_TIMESTAMP,signature=EXCLUDED.signature
                    WHERE {p}_certificate_cycles.claimed_at IS NULL OR
                        {p}_certificate_cycles.claimed_at<CURRENT_TIMESTAMP-INTERVAL '10 minutes'
                    RETURNING cohort;''',(key,signature))
                if not cur.fetchone():return None
                for uid,chat,role in recipients:
                    cur.execute(f'''INSERT INTO {p}_certificate_deliveries(cohort,winner_id,chat_id,role,signature)
                        VALUES(%s,%s,%s,%s,%s) ON CONFLICT(cohort,winner_id,chat_id)
                        DO UPDATE SET signature=EXCLUDED.signature,role=EXCLUDED.role;''',(key,uid,chat,role,signature))
                conn.commit()
                return dict(task=t,key=key,ids=ids,winners=winners,signature=signature,group=group,topic=topic)
        return await db.run(op)
    async def announce(context,task_id):
        result=await snapshot(task_id)
        if not result:return
        key=result['key'];signature=result['signature']
        try:
            deliveries=await sql(f'SELECT * FROM {p}_certificate_deliveries WHERE cohort=%s;',(key,))
            byid={w['user_id']:w for w in result['winners']}
            from telegram import InputMediaPhoto
            for delivery in deliveries:
                try:
                    if delivery['signature']!=signature:
                        if delivery['message_id']:
                            await context.bot.delete_message(chat_id=delivery['chat_id'],message_id=delivery['message_id'])
                        await sql(f'DELETE FROM {p}_certificate_deliveries WHERE cohort=%s AND winner_id=%s AND chat_id=%s;',
                            (key,delivery['winner_id'],delivery['chat_id']),commit=True)
                        continue
                    if delivery['sent_signature']==signature:continue
                    w=byid[delivery['winner_id']];t=result['task']
                    image=await asyncio.to_thread(render_certificate,w['full_name'],w['grade'],t['kind'],t['title'])
                    noun='الواجب' if t['kind']=='homework' else 'الامتحان'
                    caption=ns['bold'](f"🏆 بطل {noun}\n👤 {w['full_name']}\n📚 {ns.get('SUBJECT_NAME','')}\n📝 {t['title']}\n⭐ الدرجة: {float(w['grade']):g}/100\nمبارك هذا الإنجاز!")
                    mid=delivery['message_id']
                    if mid:
                        try:await context.bot.edit_message_media(chat_id=delivery['chat_id'],message_id=mid,
                            media=InputMediaPhoto(image,caption=caption,parse_mode='HTML'))
                        except ns['BadRequest'] as exc:
                            if 'not modified' not in str(exc).lower():raise
                    else:
                        kwargs={'message_thread_id':result['topic']} if delivery['role']=='group' else {}
                        sent=await context.bot.send_photo(chat_id=delivery['chat_id'],photo=image,caption=caption,parse_mode='HTML',**kwargs)
                        mid=sent.message_id
                    await sql(f'''UPDATE {p}_certificate_deliveries SET message_id=%s,sent_signature=%s,
                        last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE cohort=%s AND winner_id=%s AND chat_id=%s;''',
                        (mid,signature,key,delivery['winner_id'],delivery['chat_id']),commit=True)
                except Exception as exc:
                    # A missing old Telegram message is already withdrawn.
                    if delivery['signature']!=signature and 'not found' in str(exc).lower():
                        await sql(f'DELETE FROM {p}_certificate_deliveries WHERE cohort=%s AND winner_id=%s AND chat_id=%s;',
                            (key,delivery['winner_id'],delivery['chat_id']),commit=True)
                    else:
                        await sql(f'''UPDATE {p}_certificate_deliveries SET last_error=%s WHERE cohort=%s AND winner_id=%s AND chat_id=%s;''',
                            (str(exc)[:1500],key,delivery['winner_id'],delivery['chat_id']),commit=True)
                        LOG.exception('Certificate delivery pending: %s',delivery['chat_id'])
            pending=await sql(f'''SELECT 1 FROM {p}_certificate_deliveries WHERE cohort=%s
                AND (signature<>%s OR sent_signature IS DISTINCT FROM signature) LIMIT 1;''',(key,signature),one=True)
            if not pending:await sql(f'UPDATE {p}_tasks SET champion_announced=TRUE WHERE id=ANY(%s);',(result['ids'],),commit=True)
        finally:
            await sql(f'UPDATE {p}_certificate_cycles SET claimed_at=NULL WHERE cohort=%s;',(key,),commit=True)
    async def candidates(days=30):
        # Select a finished attempt per assessment, not the earliest still-open copy.
        return await sql(f'''SELECT DISTINCT ON ({COHORT_SQL}) t.* FROM {p}_tasks t
            WHERE t.retired_obligation=FALSE AND t.kind IN ('exam','homework')
                AND (t.optional_practice=FALSE OR to_jsonb(t)->>'exam_difficulty'='hard')
                AND t.catchup_source_id IS NULL
                AND (t.closed OR GREATEST(t.deadline,COALESCE((SELECT MAX(e.extended_until)
                    FROM {p}_task_extensions e WHERE e.task_id=t.id),t.deadline))<=CURRENT_TIMESTAMP)
                AND (t.champion_announced=FALSE OR EXISTS(SELECT 1 FROM {p}_certificate_deliveries cd
                    WHERE cd.cohort=({COHORT_SQL}) AND cd.sent_signature IS DISTINCT FROM cd.signature)
                  OR EXISTS(SELECT 1 FROM {p}_submissions changed WHERE changed.task_id=t.id
                    AND changed.graded_at>COALESCE((SELECT MAX(cd.updated_at) FROM {p}_certificate_deliveries cd
                      WHERE cd.cohort=({COHORT_SQL})),
                      (SELECT value::timestamptz FROM {p}_settings WHERE key='certificate_v58_started_at'))))
                AND EXISTS(SELECT 1 FROM {p}_submissions sub WHERE sub.task_id=t.id
                    AND sub.submitted_at IS NOT NULL AND sub.grade IS NOT NULL AND sub.rejected=FALSE
                    AND sub.revision_open=FALSE AND sub.teacher_change_pending=FALSE)
            ORDER BY ({COHORT_SQL}),t.id;''')
    ns['announce_champions']=announce
    db.closed_exams_pending_champion=ns['closed_exams_pending_champion']=candidates
    db.certificate_snapshot=snapshot
