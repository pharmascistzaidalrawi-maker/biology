"""Shared idle-friendly scheduling. Never trade accepted student data for quota.

All recurring maintenance is aligned to UTC half-hours across all subjects.
Known assessment/publication times use local timers, not database polling.
This reduces unattended compute; it cannot cap real users' database activity.
"""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from functools import wraps
from collections import OrderedDict

LOG = logging.getLogger(__name__)
SUBJECTS = ('english', 'chemistry', 'islamic', 'biology', 'mathematics', 'physics', 'arabic', 'french')
INTERVAL = 1800
BACKGROUND_INTERVAL = 21600


def next_boundary(now, interval=INTERVAL):
    return (math.floor(now / interval) + 1) * interval


class Registration:
    def __init__(self, callback, interval, name, data=None):
        self.callback, self.interval, self.name, self.data = callback, interval, name, data
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class ConsolidatedQueue:
    """Collect startup polling registrations, preserving ordinary one-shot jobs."""
    def __init__(self, queue):
        self.queue, self.registered = queue, []

    def __getattr__(self, name):
        return getattr(self.queue, name)

    def run_repeating(self, callback, interval, first=None, last=None, data=None, name=None, **kwargs):
        job = Registration(callback, interval, name or callback.__name__, data)
        self.registered.append(job)
        return job

    def get_jobs_by_name(self, name):
        return tuple(j for j in self.registered if j.name == name and not j.removed)

    def jobs(self):
        return tuple(j for j in self.registered if not j.removed)


class StartupApplication:
    def __init__(self, app, queue):
        self._app, self.job_queue = app, queue

    def __getattr__(self, name):
        return getattr(self._app, name)


def install(ns, db, subject):
    if subject not in SUBJECTS:
        raise ValueError('Invalid subject')
    ns['NEON_ECO_MODE'] = True
    ns['NEON_ECO_INTERVAL_SECONDS'] = INTERVAL
    ns['NEON_BACKGROUND_INTERVAL_SECONDS'] = BACKGROUND_INTERVAL
    ns['NEON_ACTIVITY_SWEEP_SECONDS'] = 300
    # A persistent advisory-lock connection defeats a completely idle pool.
    ns['USE_DATABASE_INSTANCE_LOCK'] = False
    p = subject
    state = dict(lock=asyncio.Lock(), jobs=[], app=None, background_slot=None,
                 last_sweep=0.0, refresh_pending=False, event_due=None, event_job=None,
                 last_batch_slot=None, pending_event=False, db_calls=0)
    ns['_EFFICIENT_RUNTIME'] = state

    def migrate():
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT value FROM {p}_settings WHERE key='efficient_runtime_v1';")
            if cur.fetchone():
                return
            cur.execute(f'''ALTER TABLE {p}_students ADD COLUMN IF NOT EXISTS activation_delivered_at TIMESTAMPTZ;
                CREATE INDEX IF NOT EXISTS {p}_eco_open_deadline
                ON {p}_tasks(deadline) WHERE closed=FALSE AND retired_obligation=FALSE;
                CREATE INDEX IF NOT EXISTS {p}_eco_scheduled_publish
                ON {p}_scheduled_tasks(publish_at) WHERE published=FALSE;
                CREATE INDEX IF NOT EXISTS {p}_eco_model_due
                ON {p}_submissions(model_answer_due_at) WHERE model_answer_sent_at IS NULL;
                CREATE INDEX IF NOT EXISTS {p}_eco_grade_changes
                ON {p}_submissions(task_id,graded_at) WHERE grade IS NOT NULL;
                INSERT INTO {p}_settings(key,value) VALUES('efficient_runtime_v1','1')
                    ON CONFLICT DO NOTHING;''')
            conn.commit()

    async def next_event():
        """A single bounded query, only after real activity or a maintenance batch."""
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''WITH dates AS (
                    SELECT deadline AS at FROM {p}_tasks
                        WHERE closed=FALSE AND retired_obligation=FALSE AND optional_practice=FALSE
                    UNION ALL SELECT e.extended_until FROM {p}_task_extensions e
                        JOIN {p}_tasks t ON t.id=e.task_id WHERE t.retired_obligation=FALSE
                    UNION ALL SELECT deadline-INTERVAL '6 hours' FROM {p}_tasks
                        WHERE kind='exam' AND closed=FALSE AND retired_obligation=FALSE AND optional_practice=FALSE
                    UNION ALL SELECT publish_at FROM {p}_scheduled_tasks WHERE published=FALSE
                    UNION ALL SELECT exam_available_at FROM {p}_tasks
                        WHERE closed=FALSE AND retired_obligation=FALSE
                    UNION ALL SELECT ((target_date-1)+TIME '23:00') AT TIME ZONE 'Asia/Baghdad'
                        FROM {p}_preparations WHERE published=FALSE
                    UNION ALL SELECT ((target_date-1)+TIME '23:00') AT TIME ZONE 'Asia/Baghdad'
                        FROM {p}_personal_preparations WHERE notified=FALSE
                    UNION ALL SELECT COALESCE(sub.model_answer_due_at,sub.submitted_at+INTERVAL '8 hours')
                        FROM {p}_submissions sub JOIN {p}_tasks t ON t.id=sub.task_id AND t.kind='exam'
                        WHERE sub.submitted_at IS NOT NULL AND sub.model_answer_sent_at IS NULL
                          AND t.exam_definition_id IS NOT NULL AND EXISTS(SELECT 1 FROM {p}_exam_model_answer_media a
                              WHERE a.definition_id=t.exam_definition_id)
                ) SELECT MIN(at) AS due FROM dates
                    WHERE at>CURRENT_TIMESTAMP AND at<CURRENT_TIMESTAMP+INTERVAL '30 minutes';''')
                return cur.fetchone()['due']
        state['db_calls'] += 1
        return await db.run(op)

    def cancel_event():
        job = state['event_job']
        if job is not None:
            job.schedule_removal()
        state['event_job'], state['event_due'] = None, None

    async def refresh(context):
        state['refresh_pending'] = False
        try:
            due = await next_event()
            # Coalesce simultaneous dates; run just after the true deadline.
            stamp = math.ceil(due.timestamp()) + 1 if due else None
            if stamp == state['event_due']:
                return
            cancel_event()
            if stamp is not None:
                state['event_due'] = stamp
                state['event_job'] = context.job_queue.run_once(
                    event, datetime.fromtimestamp(stamp, timezone.utc), name='efficient_due_event')
        except Exception:
            LOG.exception('Could not refresh local timers; aligned recovery remains scheduled')

    async def call(context, name, callback, data=None):
        # Preserve job.data for future recurring workers without changing the caller.
        from types import SimpleNamespace
        class Context:
            job = SimpleNamespace(name=name, data=data)
            def __getattr__(self, attr):
                return getattr(context, attr)
        try:
            await callback(Context())
        except Exception:
            LOG.exception('Consolidated maintenance failed: %s', name)

    async def work(context, background=False):
        # No queuing a second copy behind a long-running batch.
        if state['lock'].locked():
            return
        async with state['lock']:
            slot = int(time.time() // BACKGROUND_INTERVAL)
            for job in state['jobs']:
                slow = job.name == 'v49_neon_eco_background'
                if slow and (not background or state['background_slot'] == slot):
                    continue
                await call(context, job.name, job.callback, job.data)
            if background:
                state['background_slot'] = slot
            await refresh(context)
        if state['pending_event']:
            state['pending_event'] = False
            await work(context)

    async def batch(context):
        # A repeated tick in the same half-hour must not duplicate scans.
        slot = int(time.time() // INTERVAL)
        if state['last_batch_slot'] == slot:
            return
        state['last_batch_slot'] = slot
        await work(context, background=True)

    async def event(context):
        state['event_job'], state['event_due'] = None, None
        if state['lock'].locked():
            state['pending_event'] = True
            return
        await work(context)

    async def activity_sweep(context):
        # On-demand refresh runs while a user has already awakened the database.
        now = time.monotonic()
        if now - state['last_sweep'] < 300 or state['lock'].locked():
            return
        state['last_sweep'] = now
        for name in ('publish_preparations_job', 'scheduled_tasks_job', 'linked_exam_dispatch_job'):
            if name in ns:
                await call(context, name, ns[name])
    ns['v49_activity_sweep'] = activity_sweep

    def request_refresh(context):
        queue = getattr(context, 'job_queue', None)
        if state['app'] is None or queue is None or state['refresh_pending']:
            return
        state['refresh_pending'] = True
        queue.run_once(refresh, 2, name='efficient_refresh_after_action')

    # Changes to dates, activations, publication, or answers re-arm the timer.
    # Nothing is cached across requests: grade and permission changes stay immediate.
    handlers = {'button_handler', 'private_messages', 'v11_input_message', 'route_group_messages', 'start'}
    handlers.update(name for name in ns if name.endswith('_command'))
    for name in handlers:
        old = ns.get(name)
        if old is None:
            continue
        def wrap(fn, handler_name):
            @wraps(fn)
            async def wrapped(update, context, *args, **kwargs):
                chat = getattr(update, 'effective_chat', None)
                user = getattr(update, 'effective_user', None)
                if (handler_name == 'v11_input_message' and chat and chat.type != 'private'
                        and user and not ns['is_admin'](user.id)):
                    return False
                result = await fn(update, context, *args, **kwargs)
                if handler_name != 'v11_input_message' or result:
                    request_refresh(context)
                return result
            return wrapped
        ns[name] = wrap(old, name)

    # Ordinary course-chat chatter must not perform an account lookup on every message.
    # This throttle is only for recording group membership; private access checks are live.
    observed = OrderedDict()
    old_observe = ns.get('observe_group_activity')
    if old_observe:
        async def observe(update, context):
            user, chat = update.effective_user, update.effective_chat
            if not user or not chat or user.is_bot or ns['is_admin'](user.id):
                return
            if chat.id != ns.get(subject.upper() + '_GROUP_ID'):
                return
            key = (chat.id, user.id)
            now = time.monotonic()
            if now - observed.get(key, -INTERVAL) < INTERVAL:
                return
            student = await ns['get_student'](user.id)
            if (student and student.get('approved') and not student.get('admin_blocked')
                    and not student.get('reset_pending')
                    and (student.get('parent_chat_id') or student.get('admin_manual_active'))):
                await ns['mark_member_compliant'](user.id)
            else:
                await ns['observe_group_member'](user.id, ns['ACTIVATION_GRACE_HOURS'])
            observed[key] = now
            observed.move_to_end(key)
            if len(observed) > 4096:
                observed.popitem(last=False)
        ns['observe_group_activity'] = observe
    old_guard = ns.get('admin_access_guard')
    if old_guard:
        async def access_guard(update, context):
            chat = getattr(update, 'effective_chat', None)
            message = getattr(update, 'effective_message', None)
            if (chat and chat.type != 'private' and not getattr(update, 'callback_query', None)
                    and not str(getattr(message, 'text', '') or '').startswith('/')):
                return
            return await old_guard(update, context)
        ns['admin_access_guard'] = access_guard

    old_post = ns['post_init']
    async def post_init(app):
        collector = ConsolidatedQueue(app.job_queue)
        await old_post(StartupApplication(app, collector))
        await db.run(migrate)
        # All old installers have finished: no later plugin can re-add minute polls.
        active = list(collector.jobs())
        seen = set()
        chosen = []
        for job in active:
            if job.name in ('v11_champion_recovery', 'oct30_award_recovery'):
                continue
            if job.name in seen:
                continue
            seen.add(job.name)
            chosen.append(job)
        chosen.append(Registration(ns['recover_all_awards'], INTERVAL, 'awards'))
        state['jobs'], state['app'] = chosen, app
        state['last_batch_slot'] = None
        now = time.time()
        app.job_queue.run_repeating(batch, INTERVAL,
            first=datetime.fromtimestamp(next_boundary(now), timezone.utc), name='efficient_maintenance',
            job_kwargs={'max_instances': 1, 'coalesce': True, 'misfire_grace_time': 300})
        # Catch up once after restart, not once per legacy worker.
        app.job_queue.run_once(batch, 3, name='efficient_startup_recovery')
        LOG.info('Efficient runtime: %s recurring registrations -> one aligned queue; workers=%s',
                 len(collector.registered), [j.name for j in chosen])
    ns['post_init'] = post_init
    ns['efficient_refresh_timers'] = refresh
    ns['efficient_maintenance'] = batch
