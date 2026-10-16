"""Strict per-student forum delivery (v10).

A submission must NEVER be sent to General or a private admin chat. Telegram
failures leave the durable outbox pending and produce actionable diagnostics.
"""
import asyncio
import logging

LOG = logging.getLogger(__name__)


class TopicRoutingError(RuntimeError):
    """A Telegram submission could not be delivered to the student's forum topic."""


STALE_TOPIC_ERRORS = (
    'message thread not found', 'message_thread_id is invalid',
    'message thread id is invalid', 'topic not found', 'thread not found',
    'topic was deleted', 'topic is deleted', 'message thread is not found',
    'message_thread_id_invalid',
)
CLOSED_TOPIC_ERRORS = ('topic is closed', 'topic closed', 'topic is hidden')


def is_invalid_topic_error(exc):
    error = str(exc).lower()
    return any(part in error for part in (*STALE_TOPIC_ERRORS, *CLOSED_TOPIC_ERRORS))


def topic_name(student):
    uid = int(student['user_id'])
    name = ' '.join(str(student.get('full_name') or '').split()) or 'طالب'
    return f'{name} | {uid}'[:128]


async def check_destination(bot, chat_id):
    """Read-only admin diagnostics: returns (ready, Arabic explanation)."""
    try:
        cid = int(chat_id or 0)
    except (ValueError, TypeError):
        cid = 0
    if cid >= 0:
        return False, 'اضبط آيدي مجموعة Supergroup سالباً، ولا تستخدم آيدي الأدمن أو General.'
    try:
        chat = await bot.get_chat(cid)
    except Exception as exc:
        return False, f'تعذر الوصول إلى المجموعة {cid}: {exc}'
    if getattr(chat, 'type', None) != 'supergroup':
        return False, 'الوجهة ليست Supergroup؛ حوّل المجموعة إلى Supergroup.'
    if not getattr(chat, 'is_forum', False):
        return False, 'خاصية Topics غير مفعلة بهذه المجموعة.'
    try:
        me = await bot.get_me()
        membership = await bot.get_chat_member(cid, me.id)
    except Exception as exc:
        return False, f'تعذر التأكد من عضوية وصلاحيات البوت: {exc}'
    status = getattr(membership, 'status', '')
    if status not in ('administrator', 'creator', 'owner'):
        return False, 'البوت ليس مشرفاً في مجموعة الحلول.'
    if status == 'administrator' and not getattr(membership, 'can_manage_topics', False):
        return False, 'البوت مشرف، لكن تنقصه صلاحية Manage Topics / إدارة المواضيع.'
    return True, f'المجموعة {cid}: Topics مفعلة والبوت مشرف يستطيع إدارة المواضيع.'


class StudentTopicRouter:
    def __init__(self, database):
        self.db = database
        self._locks = {}

    async def ensure(self, bot, student, chat_id, *, force_new=False):
        if not student or not student.get('user_id'):
            raise TopicRoutingError('حساب الطالب غير موجود أو آيديه ناقص.')
        try:
            cid = int(chat_id)
        except (ValueError, TypeError) as exc:
            raise TopicRoutingError('آيدي مجموعة الحلول غير صحيح.') from exc
        if cid >= 0:
            raise TopicRoutingError('وجهة الحلول ليست مجموعة Supergroup؛ لا يمكن إرسال الحل إلى General.')
        uid = int(student['user_id'])
        lock = self._locks.setdefault((cid, uid), asyncio.Lock())
        async with lock:
            try:
                chat = await bot.get_chat(cid)
            except Exception as exc:
                raise TopicRoutingError(f'البوت لا يستطيع الوصول إلى مجموعة الحلول {cid}: {exc}') from exc
            if getattr(chat, 'type', None) != 'supergroup' or not getattr(chat, 'is_forum', False):
                raise TopicRoutingError('مجموعة الحلول ليست Supergroup مع Topics مفعلة.')
            existing = await self.db.student_topic(uid, cid)
            if existing and not force_new:
                if int(existing) > 1:
                    return int(existing)
                existing = None
            # Do not delete the previous mapping until a replacement was created
            # and saved successfully. Otherwise a transient Telegram failure can
            # strand submissions indefinitely.
            try:
                me = await bot.get_me()
                member = await bot.get_chat_member(cid, me.id)
            except Exception as exc:
                raise TopicRoutingError(f'تعذر فحص صلاحية إنشاء توبك: {exc}') from exc
            status = getattr(member, 'status', '')
            if status not in ('administrator', 'creator', 'owner'):
                raise TopicRoutingError('البوت ليس مشرفاً بمجموعة الحلول؛ عيّنه مشرفاً.')
            if status == 'administrator' and not getattr(member, 'can_manage_topics', False):
                raise TopicRoutingError('أعطِ البوت صلاحية إدارة المواضيع Manage Topics.')
            try:
                topic = await bot.create_forum_topic(cid, topic_name(student))
                thread_id = int(topic.message_thread_id)
                if thread_id <= 1:
                    raise TopicRoutingError('Telegram لم يرجع رقم توبك صالحاً.')
            except Exception as exc:
                if isinstance(exc, TopicRoutingError):
                    raise
                raise TopicRoutingError(f'فشل إنشاء توبك الطالب في المجموعة {cid}: {exc}') from exc
            try:
                await self.db.save_student_topic(uid, cid, thread_id)
            except Exception as exc:
                # Only the brand-new empty topic is removed. The previous topic
                # and mapping remain unchanged on database failure.
                try:
                    await bot.delete_forum_topic(cid, thread_id)
                except Exception:
                    LOG.exception('Could not clean up orphan topic %s in %s', thread_id, cid)
                raise TopicRoutingError(f'فشل حفظ رقم التوبك بقاعدة البيانات: {exc}') from exc
            return thread_id

    async def send(self, bot, row, student, *, caption, parse_mode):
        """Never pass message_thread_id=None. Repair closed/deleted topics once."""
        cid = int(row['destination_chat_id'])
        thread_id = await self.ensure(bot, student, cid)
        async def attempt(thread):
            if not thread or int(thread) <= 1:
                raise TopicRoutingError('لا يمكن إرسال حل دون توبك صالح.')
            kw = dict(chat_id=cid, message_thread_id=int(thread), caption=caption, parse_mode=parse_mode)
            kind = row['payload_type']
            if kind == 'photo':
                sent = await bot.send_photo(photo=row['file_id'], **kw)
            elif kind == 'document':
                sent = await bot.send_document(document=row['file_id'], **kw)
            elif kind == 'video':
                sent = await bot.send_video(video=row['file_id'], **kw)
            else:
                raise TopicRoutingError('نوع ملف التسليم غير مدعوم.')
            # Telegram must acknowledge the actual thread, not merely the chat.
            actual = getattr(sent, 'message_thread_id', None)
            if actual is None or int(actual) != int(thread):
                try:
                    await bot.delete_message(cid, sent.message_id)
                except Exception:
                    LOG.exception('Unable to remove wrongly threaded delivery %s', sent.message_id)
                raise TopicRoutingError('Telegram أرجع توبك مختلفاً عن توبك الطالب؛ تم رفض التسليم.')
            return sent
        try:
            sent = await attempt(thread_id)
        except Exception as exc:
            error = str(exc).lower()
            if any(part in error for part in CLOSED_TOPIC_ERRORS):
                try:
                    await bot.reopen_forum_topic(cid, thread_id)
                    sent = await attempt(thread_id)
                    return sent, thread_id
                except Exception as reopen_exc:
                    LOG.warning('Reopen topic failed; replacing student topic: %s', reopen_exc)
                    exc = reopen_exc
            if not is_invalid_topic_error(exc):
                raise
            LOG.warning('Replacing missing student topic user=%s chat=%s old_thread=%s',
                        student['user_id'], cid, thread_id)
            thread_id = await self.ensure(bot, student, cid, force_new=True)
            sent = await attempt(thread_id)
        return sent, thread_id
