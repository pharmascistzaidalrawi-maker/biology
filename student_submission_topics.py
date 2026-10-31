"""Strict per-student Telegram forum topic routing for exam and homework deliveries.

Never silently falls back to the General topic. Durable outbox retries are retained
when Telegram topic creation or posting is unavailable.
"""
import asyncio
import logging

LOG = logging.getLogger(__name__)

class TopicRoutingError(RuntimeError):
    """A submission cannot be routed to its dedicated student topic."""


INVALID_TOPIC_ERRORS = (
    'message thread not found', 'message_thread_id is invalid',
    'message thread id is invalid', 'topic not found', 'thread not found',
    'topic was deleted', 'topic is closed',
)


def is_invalid_topic_error(exc):
    return any(fragment in str(exc).lower() for fragment in INVALID_TOPIC_ERRORS)


def topic_name(student):
    uid = int(student['user_id'])
    # Telegram topic names cannot be empty and must fit Telegram's length limit.
    name = ' '.join(str(student.get('full_name') or '').split()) or 'طالب'
    return f'{name} | {uid}'[:128]


async def check_destination(bot, chat_id):
    """Read-only launch check: do not create a topic or send a test answer."""
    try:
        cid=int(chat_id or 0)
    except (TypeError,ValueError):
        return False,'معرّف مجموعة الحلول غير صالح.'
    if cid>=0:
        return False,'اضبط معرّف Supergroup سالبًا ومفعّلاً فيها Topics.'
    try:
        chat=await bot.get_chat(cid)
        if getattr(chat,'type',None)!='supergroup' or not getattr(chat,'is_forum',False):
            return False,'مجموعة الحلول يجب أن تكون Supergroup مفعّلاً فيها Topics.'
        me=await bot.get_me()
        member=await bot.get_chat_member(cid,me.id)
        status=getattr(member,'status',None)
        if status not in ('creator','administrator'):
            return False,'اجعل البوت مشرفًا في مجموعة الحلول.'
        if status!='creator' and not getattr(member,'can_manage_topics',False):
            return False,'فعّل صلاحية إدارة المواضيع للبوت في مجموعة الحلول.'
        return True,'المجموعة منتدى، والبوت مشرف ولديه صلاحية إدارة المواضيع.'
    except Exception as exc:
        return False,'تعذر الوصول إلى مجموعة الحلول: '+str(exc)[:300]


class StudentTopicRouter:
    def __init__(self, database):
        self.db = database
        self._locks = {}

    async def ensure(self, bot, student, chat_id, *, force_new=False):
        if not student or not student.get('user_id'):
            raise TopicRoutingError('بيانات الطالب غير مكتملة.')
        if not chat_id or int(chat_id) >= 0:
            raise TopicRoutingError('وجهة الحلول يجب أن تكون مجموعة Supergroup مفعّلاً فيها Topics.')
        uid = int(student['user_id'])
        cid = int(chat_id)
        key = (cid, uid)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                chat = await bot.get_chat(cid)
            except Exception as exc:
                raise TopicRoutingError(f'تعذر فحص مجموعة الحلول: {exc}') from exc
            if getattr(chat, 'type', None) != 'supergroup' or not getattr(chat, 'is_forum', False):
                raise TopicRoutingError('مجموعة الحلول ليست Supergroup مفعّلاً فيها Topics.')
            existing = await self.db.student_topic(uid, cid)
            if force_new and existing:
                await self.db.clear_student_topic(uid, cid, int(existing))
                existing = None
            if existing:
                return int(existing)
            try:
                topic = await bot.create_forum_topic(cid, topic_name(student))
                thread_id = int(topic.message_thread_id)
                if thread_id <= 0:
                    raise TopicRoutingError('Telegram لم يرجع رقم موضوع صالحاً.')
                await self.db.save_student_topic(uid, cid, thread_id)
                return thread_id
            except Exception as exc:
                if isinstance(exc, TopicRoutingError):
                    raise
                raise TopicRoutingError(f'تعذر إنشاء توبك الطالب: {exc}') from exc

    async def send(self, bot, row, student, *, caption, parse_mode):
        """Send media only to a verified student topic, retrying once for a stale topic.

        The caller must mark the outbox as sent only after this returns.
        """
        cid = int(row['destination_chat_id'])
        thread_id = await self.ensure(bot, student, cid)
        kwargs = dict(chat_id=cid, message_thread_id=thread_id,
                      caption=caption, parse_mode=parse_mode)
        async def attempt():
            kind = row['payload_type']
            if kind == 'photo':
                return await bot.send_photo(photo=row['file_id'], **kwargs)
            if kind == 'document':
                return await bot.send_document(document=row['file_id'], **kwargs)
            if kind == 'video':
                return await bot.send_video(video=row['file_id'], **kwargs)
            raise TopicRoutingError('نوع الملف غير مدعوم.')
        try:
            sent = await attempt()
        except Exception as exc:
            if not is_invalid_topic_error(exc):
                raise
            LOG.warning('Stale student topic for user=%s chat=%s thread=%s', student['user_id'], cid, thread_id)
            thread_id = await self.ensure(bot, student, cid, force_new=True)
            kwargs['message_thread_id'] = thread_id
            sent = await attempt()
        return sent, thread_id
