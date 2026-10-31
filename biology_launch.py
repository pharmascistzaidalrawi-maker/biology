"""Biology launch diagnostics and recoverable service-error responses."""
import time
from collections import OrderedDict
from psycopg import OperationalError, InterfaceError
from psycopg_pool import PoolTimeout


def install(ns, db):
    B,K=ns['InlineKeyboardButton'],ns['InlineKeyboardMarkup']
    old_menu=ns['main_menu']
    def menu(admin=False):
        markup=old_menu(admin)
        if not admin:return markup
        rows=[list(row) for row in markup.inline_keyboard]
        rows.append([B('🟢 حالة التشغيل والتوفير',callback_data='launch|status',style='primary')])
        return K(rows)
    ns['main_menu']=menu

    def status(context):
        app=getattr(context,'application',None)
        queue=getattr(app,'job_queue',None) or getattr(context,'job_queue',None)
        jobs=queue.jobs() if queue else ()
        recurring=[j for j in jobs if type(getattr(getattr(j,'job',None),'trigger',None)).__name__=='IntervalTrigger']
        names=('BOT_TOKEN','DATABASE_URL','BIOLOGY_GROUP_ID','ACTIVATION_GROUP_ID',
               'HOMEWORK_SUBMISSIONS_CHAT_ID','EXAM_SUBMISSIONS_CHAT_ID','CHAMPIONS_TOPIC_ID',
               'PREPARATION_TOPIC_ID','HOMEWORK_TOPIC_ID','EXAM_TOPIC_ID','SCHOOL_REVIEW_TOPIC_ID','RESOURCE_TOPIC_ID')
        lines=['🟢 حالة بوت الأحياء',f"الإصدار: {ns['BUILD_VERSION']}",
               '♻️ التوفير: فحص خلفي موحد كل 30 دقيقة ومؤقتات للمواعيد المعروفة.',
               f'المهام الدورية الفعلية: {len(recurring)}',
               f'الاتصالات: خامل {db.DB_POOL_MIN}، أقصى {db.DB_POOL_MAX}.',
               '⚙️ الإعدادات:']
        lines += [('✅ ' if ns.get(key) else '⚠️ ')+key+(' مضبوط' if ns.get(key) else ' غير مضبوط') for key in names]
        lines += ['هذه الصفحة لا تفحص القاعدة ولا تبقيها فعالة.',
                  'ضبط 0.25 CU وعدم نوم خدمة الاستضافة إعدادان من لوحة Neon وRender، ولا يغيّرهما هذا البوت.',
                  'المعرّفات المضبوطة تحتاج صلاحيات صحيحة؛ استخدم فحص وجهات الحلول أدناه.']
        return ns['bold']('\n'.join(lines))
    def keyboard():
        return K([[B('🔌 اختبار اتصال القاعدة',callback_data='launch|database',style='primary')],
                  [B('🧵 فحص وجهات الحلول',callback_data='launch|topics',style='primary')],
                  [B('🔄 تحديث الحالة',callback_data='launch|status',style='primary'),ns['back_menu']()]])
    def leave_input(context):
        data=getattr(context,'user_data',None)
        if data is not None:
            from reliability_oct30 import clear_flows
            clear_flows(data)
            for key in ('preview_student','control_session','control_input'):data.pop(key,None)
    async def command(update,context):
        if not ns['is_admin'](update.effective_user.id):return
        leave_input(context)
        await update.effective_message.reply_text(status(context),parse_mode='HTML',reply_markup=keyboard())
    ns['launchcheck_command']=command
    old_button=ns['button_handler']
    async def button(update,context):
        q=update.callback_query
        if not str(q.data or '').startswith('launch|'):return await old_button(update,context)
        if not ns['is_admin'](q.from_user.id):
            await q.answer('للإدارة فقط.',show_alert=True);return
        leave_input(context)
        if q.data=='launch|database':
            await q.answer()
            def check():
                with db.connect() as conn,conn.cursor() as cur:
                    cur.execute('SELECT 1 AS connected;');return cur.fetchone()['connected']==1
            ok=await db.run(check)
            await q.edit_message_text(ns['bold']('✅ اتصال قاعدة البيانات يعمل الآن.' if ok else '⚠️ تعذر تأكيد الاتصال.'),parse_mode='HTML',reply_markup=keyboard());return
        if q.data=='launch|topics':
            await q.answer();await ns['submission_topics_check_command'](update,context);return
        if q.data!='launch|status':
            await q.answer('افتح حالة التشغيل من القائمة مجددًا.',show_alert=True);return
        await q.answer();await q.edit_message_text(status(context),parse_mode='HTML',reply_markup=keyboard())
    ns['button_handler']=button

    old_error=ns['error_handler']
    notified=OrderedDict()
    async def error_handler(update,context):
        if not isinstance(context.error,(OperationalError,InterfaceError,PoolTimeout)):
            return await old_error(update,context)
        # Preserve diagnostics for the owner, and acknowledge the student's request.
        await old_error(update,context)
        q=getattr(update,'callback_query',None) if update else None
        notice='تعذر الاتصال بقاعدة البيانات حاليًا. حاول بعد قليل؛ لا تعتبر الحل مسلّمًا حتى تظهر رسالة التأكيد.'
        answered=False
        if q is not None:
            try:await q.answer(notice,show_alert=True);answered=True
            except ns['TelegramError']:pass
        user=getattr(update,'effective_user',None) if update else None
        chat=getattr(update,'effective_chat',None) if update else None
        message=getattr(update,'effective_message',None) if update else None
        if not answered and user and chat and chat.type=='private' and message:
            now=time.monotonic()
            if now-notified.get(user.id,-60)>=30:
                try:await message.reply_text(ns['bold'](notice),parse_mode='HTML')
                except ns['TelegramError']:pass
                else:
                    notified[user.id]=now;notified.move_to_end(user.id)
                    if len(notified)>512:notified.popitem(last=False)
    ns['error_handler']=error_handler
