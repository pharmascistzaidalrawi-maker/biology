"""Future-only course regrouping. Student progress is never rewritten."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json

DAYS = [0, 1, 6]
ZONE = ZoneInfo('Asia/Baghdad')


def pack_lessons(rows, first_date, days=DAYS, counts=None, occupied=(), capacity_fn=None):
    """Return daily same-chapter bundles and source IDs, keeping lecture order.

    A chapter's final single lecture stays alone: chapters are never mixed in
    a preparation, because the bot's preparation schema has one chapter.
    """
    days=set(days); occupied=set(occupied)
    if not days or not days<=set(range(7)): raise ValueError('أيام دراسة غير صالحة.')
    counts=counts or {day:2 if day==1 else 1 for day in days}
    if any(not 1<=int(counts.get(day,1))<=10 for day in days): raise ValueError('عدد المحاضرات من 1 إلى 10.')
    lessons=[]; seen=set()
    for row in rows:
        for raw in str(row['lectures']).split(','):
            lecture=int(raw.strip()); pair=(int(row['chapter']),lecture)
            if pair in seen: raise ValueError('محاضرة مكررة في الجدول؛ صححها قبل إعادة التوزيع.')
            seen.add(pair); lessons.append((row,lecture))
    result=[]; day=first_date; index=0
    while index<len(lessons):
        while day.weekday() not in days or day in occupied: day+=timedelta(days=1)
        chapter=lessons[index][0]['chapter']; items=[]; sources=[]; titles=[]
        capacity=int(capacity_fn(day,chapter) if capacity_fn else counts.get(day.weekday(),1))
        if not 1<=capacity<=10: raise ValueError('عدد المحاضرات غير صالح.')
        while index<len(lessons) and lessons[index][0]['chapter']==chapter and len(items)<capacity:
            row,lecture=lessons[index]; items.append(lecture)
            if row['prep_no'] not in sources:sources.append(row['prep_no'])
            if row.get('title') and row['title'] not in titles:titles.append(row['title'])
            index+=1
        result.append(dict(chapter=chapter,lectures=','.join(map(str,items)),target_date=day,sources=sources,title=' / '.join(titles)))
        day+=timedelta(days=1)
    return result


async def migrate_future(db, marker="v12_double_tuesday_done", planner=None, force=False):
    def op():
        with db.connect() as conn,conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(110011);')
            cur.execute("SELECT value FROM biology_settings WHERE key=%s;",(marker,))
            if cur.fetchone() and not force:return None
            cur.execute('LOCK TABLE biology_preparations IN EXCLUSIVE MODE;')
            cur.execute('SELECT * FROM biology_preparations WHERE published=FALSE ORDER BY target_date,prep_no FOR UPDATE;')
            rows=cur.fetchall()
            cur.execute('SELECT target_date FROM biology_preparations WHERE published=TRUE;')
            occupied={r['target_date'] for r in cur.fetchall()}
            today=datetime.now(ZONE).date()
            bundles=planner(cur,rows,today,occupied) if planner else pack_lessons(rows,today+timedelta(days=1),occupied=occupied)
            cur.execute("INSERT INTO biology_course_schedule_history(actor_id,rows) SELECT 0,COALESCE(jsonb_agg(to_jsonb(p)),'[]'::jsonb) FROM biology_preparations p;")
            # Freeze prep-based COURSE exam links as explicit lecture links before regrouping.
            ids=[r['prep_no'] for r in rows]
            cur.execute('''SELECT lp.definition_id,lp.chapter,lp.prep_no,p.lectures
                FROM biology_linked_exam_preparations lp
                JOIN biology_linked_exam_definitions d ON d.id=lp.definition_id AND d.target_scope='course'
                JOIN biology_preparations p ON p.chapter=lp.chapter AND p.chapter_prep_no=lp.prep_no
                WHERE p.prep_no=ANY(%s) ORDER BY lp.definition_id,lp.position;''',(ids,))
            links=cur.fetchall()
            for definition_id in sorted({link['definition_id'] for link in links}):
                cur.execute('SELECT id FROM biology_linked_exam_definitions WHERE id=%s FOR UPDATE;',(definition_id,))
                # Existing explicit snapshots are authoritative; otherwise freeze ALL
                # linked preparations, including those outside the pending schedule.
                required=db._v47_required_lectures(cur,definition_id)
                for chapter,lecture in sorted(required):
                    cur.execute('''INSERT INTO biology_linked_exam_lectures(definition_id,chapter,lecture,position)
                        SELECT %s,%s,%s,COALESCE(MAX(position),-1)+1 FROM biology_linked_exam_lectures WHERE definition_id=%s
                        ON CONFLICT(definition_id,chapter,lecture) DO NOTHING;''',
                        (definition_id,chapter,lecture,definition_id))
            for link in links:
                # Prevent legacy init_db from restoring this obsolete prep link.
                cur.execute('UPDATE biology_linked_exam_definitions SET prep_no=NULL WHERE id=%s AND chapter=%s AND prep_no=%s;',
                    (link['definition_id'],link['chapter'],link['prep_no']))
                cur.execute('DELETE FROM biology_linked_exam_preparations WHERE definition_id=%s AND chapter=%s AND prep_no=%s;',
                    (link['definition_id'],link['chapter'],link['prep_no']))
            # Keep representatives in the same chapter; archive absorbed rows in the snapshot.
            by_chapter={}
            for row in rows:by_chapter.setdefault(row['chapter'],[]).append(row)
            used=[]; targets={}
            cur.execute('SELECT COALESCE(MAX(target_date),CURRENT_DATE) AS last,COALESCE(MAX(prep_no),0) AS max_id FROM biology_preparations;')
            maxima=cur.fetchone();next_id=maxima['max_id']
            cur.execute('SELECT chapter,MAX(chapter_prep_no) AS pno FROM biology_preparations GROUP BY chapter;')
            chapter_numbers={r['chapter']:r['pno'] or 0 for r in cur.fetchall()}
            park=max(maxima['last'],today)+timedelta(days=10000)
            for i,row in enumerate(rows):
                cur.execute('UPDATE biology_preparations SET target_date=%s WHERE prep_no=%s;',(park+timedelta(days=i),row['prep_no']))
            for bundle in bundles:
                ch=bundle['chapter']
                if by_chapter[ch]:tid=by_chapter[ch].pop(0)['prep_no']
                else:
                    next_id+=1;tid=next_id;chapter_numbers[ch]=chapter_numbers.get(ch,0)+1
                    cur.execute('INSERT INTO biology_preparations(prep_no,chapter,chapter_prep_no,lectures,target_date,published) VALUES(%s,%s,%s,%s,%s,FALSE);',
                        (tid,ch,chapter_numbers[ch],bundle['lectures'],park+timedelta(days=len(rows)+len(used)+1)))
                used.append(tid)
                for source in bundle['sources']:targets.setdefault(source,[]).append(tid)
                cur.execute('UPDATE biology_preparations SET lectures=%s,target_date=%s,title=%s WHERE prep_no=%s;',
                    (bundle['lectures'],bundle['target_date'],bundle['title'],tid))
            # Snapshot manual links before changing them; one source may be split across days.
            cur.execute('SELECT * FROM biology_manual_exam_links WHERE prep_no=ANY(%s);',(ids,))
            manual=cur.fetchall()
            cur.execute('DELETE FROM biology_manual_exam_links WHERE prep_no=ANY(%s);',(ids,))
            for link in manual:
                for tid in targets.get(link['prep_no'],[]):
                    cur.execute('''INSERT INTO biology_manual_exam_links(prep_no,task_id,linked_by)
                        VALUES(%s,%s,%s) ON CONFLICT DO NOTHING;''',(tid,link['task_id'],link['linked_by']))
            removed=[pid for pid in ids if pid not in used]
            cur.execute("DELETE FROM biology_early_preparation_unlocks WHERE study_track='course' AND source_prep_id=ANY(%s) AND completed_at IS NULL;",(ids,))
            cur.execute('DELETE FROM biology_preparations WHERE prep_no=ANY(%s);',(removed,))
            cur.execute("UPDATE biology_students SET study_days=%s WHERE study_track='course';",(DAYS,))
            for key,value in [('admin_course_days',json.dumps(DAYS)),('admin_course_calendar','1'),('v11_calendar_done','1'),(marker,'1')]:
                cur.execute('INSERT INTO biology_settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value;',(key,value))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(0,'v12_double_tuesday',%s);",(f'old_rows={len(rows)};new_rows={len(bundles)}',))
            conn.commit();return DAYS
    return await db.run(op)
