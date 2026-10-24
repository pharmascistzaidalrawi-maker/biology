"""Record prior study at enrollment boundaries without XP or review side effects."""
from data import PLAYLISTS, CHAPTER_PREPARATION_DISTRIBUTION
METHOD='prior_learning'

def before_boundary(boundary):
    return sorted((int(ch),int(item[0])) for ch,items in PLAYLISTS.items()
                  for item in items if (int(ch),int(item[0]))<boundary)

def boundary_for(cur,p,student):
    if student.get('reset_pending') or int(student.get('onboarding_version') or 0)<19:
        return None
    uid=int(student['user_id'])
    if student.get('study_track')=='chapter':
        # The saved plan retains the exact first lecture, including split groups.
        cur.execute(f'SELECT chapter,lectures FROM {p}_personal_preparations WHERE user_id=%s ORDER BY target_date,chapter,prep_no,id LIMIT 1;',(uid,))
        row=cur.fetchone()
        if row:return int(row['chapter']),min(int(n) for n in row['lectures'].split(',') if n.strip())
        ch=int(student.get('start_chapter') or 1);number=int(student.get('start_prep_no') or 1)
        groups=CHAPTER_PREPARATION_DISTRIBUTION.get(ch,[])
        return (ch,min(groups[number-1])) if 1<=number<=len(groups) else None
    if student.get('study_track')=='course':
        # Use the enrollment date, never today's advancing published lecture.
        cur.execute(f'''SELECT chapter,lectures FROM {p}_preparations
            ORDER BY CASE WHEN target_date<=%s::DATE THEN 0 ELSE 1 END,
            CASE WHEN target_date<=%s::DATE THEN target_date END DESC,
            target_date ASC,prep_no ASC LIMIT 1;''',(student.get('track_started_on'),student.get('track_started_on')))
        row=cur.fetchone()
        if row:return int(row['chapter']),min(int(n) for n in row['lectures'].split(',') if n.strip())
    return None

def synchronize(cur,p,uid):
    cur.execute(f'SELECT * FROM {p}_students WHERE user_id=%s FOR UPDATE;',(int(uid),))
    student=cur.fetchone()
    if not student:return
    boundary=boundary_for(cur,p,student)
    if boundary is None:return
    pairs=before_boundary(boundary)
    # Remove only inferred completion if the teacher moves the start backwards.
    cur.execute(f'''DELETE FROM {p}_lecture_progress WHERE user_id=%s
        AND completion_method=%s AND (chapter,lecture)>=(%s,%s);''',
        (int(uid),METHOD,*boundary))
    if pairs:
        cur.execute(f'''INSERT INTO {p}_lecture_progress(user_id,chapter,lecture,opened_at,completed_at,completion_method)
            SELECT %s,ch,lec,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,%s
            FROM UNNEST(%s::INTEGER[],%s::INTEGER[]) AS prior(ch,lec)
            ON CONFLICT(user_id,chapter,lecture) DO UPDATE
            SET completed_at=EXCLUDED.completed_at,completion_method=EXCLUDED.completion_method
            WHERE {p}_lecture_progress.completed_at IS NULL;''',
            (int(uid),METHOD,[ch for ch,n in pairs],[n for ch,n in pairs]))
    # Earlier lessons are complete *including their royal review cycle*.
    # No review plan or XP is generated for these inferred completions.
    cur.execute(f"""UPDATE {p}_lecture_reviews r
        SET completed_at=COALESCE(r.completed_at,CURRENT_TIMESTAMP),
            reminded_at=COALESCE(r.reminded_at,CURRENT_TIMESTAMP)
        WHERE r.user_id=%s AND (r.chapter,r.lecture)<(%s,%s);""",
        (int(uid),*boundary))
    # Remove stale inferred review rows if a student's start boundary moves back.
    cur.execute(f"""DELETE FROM {p}_lecture_reviews r
        WHERE r.user_id=%s AND (r.chapter,r.lecture)>=(%s,%s)
        AND NOT EXISTS(SELECT 1 FROM {p}_lecture_progress lp
          WHERE lp.user_id=r.user_id AND lp.chapter=r.chapter AND lp.lecture=r.lecture
          AND lp.completed_at IS NOT NULL);""",(int(uid),*boundary))

def install(ns,db,p):
    if p not in {'biology','biology','mathematics','physics','biology','french','islamic'}:raise ValueError('Invalid subject')
    def sync(uid=None):
        with db.connect() as conn,conn.cursor() as cur:
            if uid is None:
                cur.execute(f'SELECT user_id FROM {p}_students WHERE reset_pending=FALSE ORDER BY user_id;')
                ids=[r['user_id'] for r in cur.fetchall()]
            else:ids=[uid]
            for item in ids:synchronize(cur,p,item)
            conn.commit()
    async def sync_async(uid=None):return await db.run(lambda:sync(uid))
    db.sync_prior_learning=sync_async
    # Existing users migrate after the normal startup seed has populated the plan.
    old_post=ns['post_init']
    async def post(app):
        await old_post(app)
        await sync_async()
    ns['post_init']=post
    # All registration and route-selection entry points, including admin preview.
    for name in ('v47_choose_track','set_student_onboarding'):
        previous=getattr(db,name)
        def wrap(old):
            async def changed(uid,*a,**kw):
                result=await old(uid,*a,**kw)
                await sync_async(uid)
                return result
            return changed
        replacement=wrap(previous);setattr(db,name,replacement)
        if name in ns:ns[name]=replacement
    import admin_control
    for name in ('rebuild_course','edit_course_row'):
        previous=getattr(admin_control,name,None)
        if previous is None:continue
        def wrap_edit(old):
            async def edited(*a,**kw):
                result=await old(*a,**kw)
                await sync_async()
                return result
            return edited
        setattr(admin_control,name,wrap_edit(previous))
