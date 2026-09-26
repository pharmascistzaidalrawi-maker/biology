"""Exam dispatch: one immutable window per student, no deadline resynchronization."""
from datetime import datetime
from zoneinfo import ZoneInfo
import database as db
TIMEZONE=ZoneInfo('Asia/Baghdad')

def track_scope(student):
    if student.get('study_track')=='course': return 'course'
    ch=student.get('current_chapter')
    return f'chapter_{ch}' if ch else None

def weekly_study_day_count(chapter):
    return 5 if chapter==1 else 4 if chapter==2 else 3

async def dispatch_exams(now=None):
    now=now or datetime.now(TIMEZONE)
    created=await db.v47_activate_legacy_ready_exams()
    for definition in await db.v31_active_exam_definitions():
        if definition.get('target_scope')!='course': continue
        window=await db.v47_course_window(definition['id'])
        # v47_course_window may persist the actual release anchor during this
        # call, a few microseconds after the sweep timestamp was captured.
        current=max(now,datetime.now(TIMEZONE))
        if window['status']!='ready' or not window['publish_at']<=current<window['deadline']: continue
        for student in await db.active_students_for_linked_exam(definition['id']):
            if await db.task_for_linked_exam_student(definition['id'],student['user_id']): continue
            task=await db.v29_create_or_get_exam_task(definition['id'],student['user_id'],window['publish_at'],False)
            if task: created.append(task)
    for item in await db.v29_ready_personal_exams():
        task=await db.v29_create_or_get_exam_task(item['definition_id'],item['user_id'],now,False)
        if task: created.append(task)
    return created
