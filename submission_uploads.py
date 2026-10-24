"""Up to 25 answer files across individual messages and multiple albums."""
from uuid import uuid4

MAX_SUBMISSION_FILES = 25

def upload_group(user_data, task_id):
    state = user_data.get('submission_upload')
    if not state or state['task_id'] != task_id:
        state = {'task_id': task_id, 'group_id': 'upload_' + uuid4().hex}
        user_data['submission_upload'] = state
    return state['group_id']

def upload_keyboard(ns, task_id):
    return ns['InlineKeyboardMarkup']([[ns['InlineKeyboardButton'](
        '✅ إنهاء التسليم', callback_data=f'uploadfinish|{task_id}')]])

async def upload_summary(db, prefix, task_id, user_id, group_id):
    assert prefix in ('biology', 'islamic')
    def op():
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(f'''SELECT COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status='delivered') AS delivered,
                COUNT(*) FILTER (WHERE status='pending') AS pending
                FROM {prefix}_submission_delivery_outbox
                WHERE task_id=%s AND user_id=%s AND media_group_id=%s
                  AND status IN ('pending','delivered');''', (task_id,user_id,group_id))
            return dict(cur.fetchone())
    return await db.run(op)

def install(ns, db, prefix):
    old_button = ns['button_handler']
    async def button(update, context):
        q = update.callback_query
        data = str(q.data or '')
        if data.startswith('uploadfinish|'):
            try: tid = int(data.split('|')[1])
            except (ValueError, IndexError): return
            state = context.user_data.get('submission_upload')
            if not state or state['task_id'] != tid or context.user_data.get('waiting_submission') != tid:
                await q.answer('التسليم مغلق أو انتهت الجلسة. افتح المهمة للتحقق من إجابتك.',show_alert=True)
                return
            status = await upload_summary(db,prefix,tid,q.from_user.id,state['group_id'])
            if not status['total']:
                await q.answer('أرسل صور الحل أولاً.',show_alert=True)
                return
            context.user_data.pop('submission_upload',None)
            context.user_data.pop('submission_album',None)
            context.user_data.pop('waiting_submission',None)
            await q.answer('تم إنهاء رفع الصور')
            note = (f"⏳ توجد {status['pending']} ملفات محفوظة بانتظار وصولها للأستاذ. سيعيد البوت إرسالها تلقائياً."
                    if status['pending'] else '✅ وصلت جميع ملفات الحل إلى الأستاذ وتم تسجيل التسليم.')
            await q.edit_message_text(ns['bold'](f"📤 انتهى رفع الحل: {status['total']} من {MAX_SUBMISSION_FILES} ملفاً.\n{note}"),
                parse_mode='HTML', reply_markup=ns['InlineKeyboardMarkup']([
                    [ns['InlineKeyboardButton']('🔄 تغيير الإجابة',callback_data=f'retrysubmission|{tid}')],
                    [ns['InlineKeyboardButton']('🎬 فتح المحاضرات التالية',callback_data='today_prep')],
                    [ns['back_menu']()]]))
            return
        if data.startswith('submissionok|'):
            state=context.user_data.get('submission_upload')
            if state and data==f"submissionok|{state['task_id']}":
                context.user_data.pop('submission_upload',None)
        result = await old_button(update,context)
        # A successful explicit revision starts a fresh 25-file answer.
        if data.startswith('retrysubmission|') and context.user_data.get('waiting_submission'):
            state=await db.v52_submission_state(context.user_data['waiting_submission'],q.from_user.id)
            if state and state.get('revision_open'):
                context.user_data.pop('submission_upload',None)
        return result
    ns['button_handler'] = button
