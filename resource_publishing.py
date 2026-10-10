"""Reliable admin booklet/summary publication with atomic multi-file save.

All uploads are staged in memory; nothing is visible to students until commit.
Existing resource tables and student browsing are reused unchanged.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import secrets

CATEGORIES = {'booklet': 'الملازم', 'summary': 'الملخصات',
              'model_answer': 'الإجابات النموذجية', 'ministerial': 'الأسئلة الوزارية'}
MAX_ITEMS = 25
MAX_FILE_BYTES = 50 * 1024 * 1024
DRAFT_TTL = timedelta(minutes=30)


def extract_media(msg):
    """Validate Telegram media, keeping file_id rather than downloading it."""
    if getattr(msg, 'document', None):
        doc = msg.document
        name = (getattr(doc, 'file_name', '') or '').lower()
        mime = (getattr(doc, 'mime_type', '') or '').lower()
        if not (mime == 'application/pdf' or name.endswith('.pdf')):
            raise ValueError('الملف المرفوع يجب أن يكون PDF. يمكنك أيضاً إرسال صورة أو فيديو.')
        if getattr(doc, 'file_size', 0) and doc.file_size > MAX_FILE_BYTES:
            raise ValueError('حجم PDF أكبر من 50 MB؛ قسّمه إلى ملفات أصغر.')
        return 'document', doc.file_id
    if getattr(msg, 'photo', None):
        return 'photo', msg.photo[-1].file_id
    if getattr(msg, 'video', None):
        video = msg.video
        if getattr(video, 'file_size', 0) and video.file_size > MAX_FILE_BYTES:
            raise ValueError('حجم الفيديو أكبر من 50 MB.')
        return 'video', video.file_id
    raise ValueError('أرسل PDF أو صورة أو فيديو. اضغط إنهاء وحفظ بعد اكتمال الرفع.')


def draft_expired(draft, now):
    return not draft or now - draft['started'] > DRAFT_TTL


def install(ns, db, prefix):
    if prefix not in {'biology', 'french', 'islamic'}:
        raise ValueError('Unknown subject')
    B, K = ns['InlineKeyboardButton'], ns['InlineKeyboardMarkup']
    zone = ns['TIMEZONE']
    logger = ns.get('logger') or logging.getLogger(__name__)
    group_id = ns.get(f'{prefix.upper()}_GROUP_ID') or 0
    topic_id = ns.get('RESOURCE_TOPIC_ID') or 0
    TelegramError = ns['TelegramError']
    def now(): return datetime.now(zone)
    def kb(rows): return K(rows)
    def back(): return B('◀️ إدارة الملفات', callback_data='respub|menu')
    def draft_buttons():
        return kb([[B('✅ إنهاء وحفظ ونشر', callback_data='respub|finish')],
                   [B('❌ إلغاء بدون نشر', callback_data='respub|cancel')]])

    def migrate():
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(f'''ALTER TABLE {prefix}_resources
                ADD COLUMN IF NOT EXISTS publication_key TEXT;
                CREATE UNIQUE INDEX IF NOT EXISTS {prefix}_resource_publication_key_idx
                ON {prefix}_resources(publication_key) WHERE publication_key IS NOT NULL;''')
            conn.commit()

    previous_post = ns['post_init']
    async def post_init(app):
        await previous_post(app)
        await db.run(migrate)
        logger.info('%s resource publishing ready', prefix)
    ns['post_init'] = post_init

    async def save_draft(draft, actor):
        """One DB transaction for metadata + ALL media; retries are idempotent."""
        if draft['step'] != 'media' or not draft.get('media'):
            raise ValueError('أضف ملفاً واحداً على الأقل قبل النشر.')
        if not draft.get('title'):
            raise ValueError('اسم الملف مطلوب.')
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'''INSERT INTO {prefix}_resources
                    (category,chapter,title,created_by,publication_key)
                    VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING RETURNING *;''',
                    (draft['category'], draft['chapter'], draft['title'], int(actor), draft['key']))
                row = cur.fetchone()
                created = bool(row)
                if not created:
                    cur.execute(f'SELECT * FROM {prefix}_resources WHERE publication_key=%s;', (draft['key'],))
                    row = cur.fetchone()
                    if not row: raise RuntimeError('Failed to retrieve publication')
                if created:
                    for kind, file_id in draft['media']:
                        cur.execute(f'''INSERT INTO {prefix}_resource_media
                            (resource_id,payload_type,file_id) VALUES(%s,%s,%s)
                            ON CONFLICT DO NOTHING;''', (row['id'], kind, file_id))
                    cur.execute(f'''INSERT INTO {prefix}_audit(actor_id,action,details)
                        VALUES(%s,'resource_published',%s);''',
                        (int(actor), f"resource={row['id']};category={draft['category']};files={len(draft['media'])}"))
                conn.commit()
                return row, created
        return await db.run(op)

    async def delete_resource(rid, actor):
        def op():
            with db.connect() as conn, conn.cursor() as cur:
                cur.execute(f'DELETE FROM {prefix}_resources WHERE id=%s RETURNING id;', (int(rid),))
                found = cur.fetchone()
                if found:
                    cur.execute(f'''INSERT INTO {prefix}_audit(actor_id,action,details)
                        VALUES(%s,'resource_deleted',%s);''', (int(actor), f'resource={rid}'))
                conn.commit()
                return bool(found)
        return await db.run(op)

    async def send_to_group(bot, row, media):
        if not group_id or not topic_id:
            return None
        failed = 0
        for index, (kind, file_id) in enumerate(media):
            args = {'chat_id': group_id, 'message_thread_id': topic_id}
            if index == 0:
                args['caption'] = f"📚 {CATEGORIES[row['category']]} | الفصل {row['chapter']}\n📄 {row['title']}"[:1024]
            try:
                if kind == 'document': await bot.send_document(document=file_id, **args)
                elif kind == 'photo': await bot.send_photo(photo=file_id, **args)
                else: await bot.send_video(video=file_id, **args)
            except TelegramError:
                failed += 1
                logger.exception('Resource %s could not be delivered to group', row['id'])
        return failed

    def chapters():
        # The curriculum is the source of truth, not a hardcoded five-chapter assumption.
        from data import PLAYLISTS
        return sorted(int(ch) for ch in PLAYLISTS)

    async def menu(q):
        rows = [[B('📚 نشر ملزمة جديدة', callback_data='respub|category|booklet')],
                [B('📝 نشر ملخص جديد', callback_data='respub|category|summary')],
                [B('📚 تصفح الملازم المنشورة', callback_data='resourcecategory|booklet')],
                [B('📝 تصفح الملخصات المنشورة', callback_data='resourcecategory|summary')],
                [B('🏠 القائمة الرئيسية', callback_data='menu')]]
        await q.edit_message_text('📖 إدارة الملازم والملخصات\n'
            'ارفع PDF أو صوراً من خاص البوت؛ لا يظهر المنشور للطلاب حتى تضغط إنهاء وحفظ.\n'
            'يمكنك نشر أكثر من ملف للعنوان نفسه وحذف منشور قديم.', reply_markup=kb(rows))

    async def chapter_menu(q, category):
        rows = [[B(f'📘 الفصل {ch}', callback_data=f'respub|begin|{category}|{ch}')]
                for ch in chapters()]
        rows.append([back()])
        await q.edit_message_text(f'📖 نشر {CATEGORIES[category]}\nاختر الفصل:', reply_markup=kb(rows))

    previous_menu = ns['main_menu']
    def main_menu(admin=False):
        original = previous_menu(admin)
        if not admin: return original
        rows = [list(r) for r in original.inline_keyboard]
        rows.insert(0, [B('📖 إدارة الملازم والملخصات', callback_data='respub|menu')])
        return kb(rows)
    ns['main_menu'] = main_menu

    previous_private = ns['private_messages']
    async def private_messages(update, context):
        draft = context.user_data.get('resource_publish_draft')
        if draft and ns['is_admin'](update.effective_user.id):
            msg = update.effective_message
            if draft_expired(draft, now()):
                context.user_data.pop('resource_publish_draft', None)
                await msg.reply_text('⏳ انتهت مهلة رفع الملازم والملخصات. ابدأ منشوراً جديداً.')
                return
            if draft['step'] == 'title':
                title = (msg.text or '').strip()
                if not title or len(title) > 140:
                    await msg.reply_text('⚠️ أرسل اسم الملزمة أو الملخص كنص، من 1 إلى 140 حرفاً.')
                    return
                draft.update(title=title, step='media')
                await msg.reply_text('📎 أرسل PDF أو صوراً أو فيديو، ويمكنك إرسال عدة ملفات.\n'
                    'عند الانتهاء اضغط «إنهاء وحفظ ونشر».', reply_markup=draft_buttons())
                return
            try:
                kind, file_id = extract_media(msg)
            except ValueError as exc:
                await msg.reply_text(f'⚠️ {exc}', reply_markup=draft_buttons())
                return
            if (kind, file_id) in draft['media']:
                await msg.reply_text('ℹ️ هذا المرفق مضاف بالفعل. أرسل ملفاً آخر أو اضغط إنهاء.',
                                     reply_markup=draft_buttons())
                return
            if len(draft['media']) >= MAX_ITEMS:
                await msg.reply_text(f'⚠️ الحد الأعلى {MAX_ITEMS} ملفاً للمنشور الواحد. انشر الموجود أولاً.',
                                     reply_markup=draft_buttons())
                return
            draft['media'].append((kind, file_id))
            await msg.reply_text(f"✅ تم استلام الملف {len(draft['media'])} من {MAX_ITEMS}.\n"
                'أرسل ملفاً آخر أو اضغط إنهاء وحفظ ونشر.', reply_markup=draft_buttons())
            return
        return await previous_private(update, context)
    ns['private_messages'] = private_messages

    previous_button = ns['button_handler']
    async def button_handler(update, context):
        q = update.callback_query
        data = q.data or ''
        uid = q.from_user.id
        if (data == 'menu' or data in {'v54_school_admin','admin_publish'}
                or data.startswith(('pdf|', 'hw|', 'v29_pub|'))):
            context.user_data.pop('resource_publish_draft', None)
        if data.startswith(('respub|','addresource|','finishresource|')):
            if not ns['is_admin'](uid):
                await q.answer('هذا القسم للإدارة فقط.', show_alert=True)
                return
            if q.message.chat.type != 'private':
                await q.answer('افتح خاص البوت لنشر الملازم والملخصات.', show_alert=True)
                return
            parts = data.split('|')
            action = parts[1] if parts[0] == 'respub' and len(parts)>1 else 'beginlegacy' if parts[0]=='addresource' else 'finishlegacy'
            if action in {'menu','cancel'}:
                context.user_data.pop('resource_publish_draft', None)
                context.user_data.pop('resource_awaiting_title', None)
                context.user_data.pop('waiting_resource_id', None)
                await q.answer()
                await menu(q)
                return
            if action == 'category' and len(parts)==3 and parts[2] in CATEGORIES:
                await q.answer(); await chapter_menu(q, parts[2]); return
            if action in {'begin','beginlegacy'} and len(parts)==4:
                category, ch_raw = parts[2:]
                try: chapter=int(ch_raw)
                except ValueError: chapter=-1
                if category not in CATEGORIES or chapter not in chapters():
                    await q.answer('الفصل أو القسم غير صالح.', show_alert=True); return
                # Cancel stale admin input sessions so a later message cannot be
                # interpreted as an unrelated exam, homework, or school PDF.
                for stale in ('resource_awaiting_title', 'waiting_resource_id',
                              'school_pdf_input', 'school_exam_schedule',
                              'manual_task', 'linked_exam', 'v29_publish',
                              'v54_school_exam_media', 'archive_awaiting',
                              'waiting_archive_id'):
                    context.user_data.pop(stale, None)
                context.user_data['resource_publish_draft'] = {
                    'key': secrets.token_hex(16), 'category': category, 'chapter': chapter,
                    'title': None, 'media': [], 'step': 'title', 'started': now()}
                await q.answer()
                await q.edit_message_text(f'✍️ أرسل الآن اسم {CATEGORIES[category]} للفصل {chapter} في الخاص.\n'
                    'يمكنك إلغاء العملية دون نشر.', reply_markup=kb([[B('❌ إلغاء', callback_data='respub|cancel')]]))
                return
            if action in {'finish','finishlegacy'}:
                draft = context.user_data.get('resource_publish_draft')
                if not draft or draft_expired(draft, now()):
                    context.user_data.pop('resource_publish_draft', None)
                    await q.answer('لا يوجد منشور قيد الرفع أو انتهت المهلة.', show_alert=True); return
                if not draft.get('media'):
                    await q.answer('أرسل ملف PDF أو صورة واحدة على الأقل.', show_alert=True); return
                await q.answer()
                try:
                    row, created = await save_draft(draft, uid)
                except Exception:
                    logger.exception('Resource publication transaction failed')
                    await q.message.reply_text('⚠️ تعذر تأكيد حفظ الملف في قاعدة البيانات. بقيت الملفات في المسودة؛ حاول إنهاء النشر مجدداً.')
                    return
                context.user_data.pop('resource_publish_draft', None)
                await q.edit_message_text(f"✅ {'نُشر بنجاح' if created else 'كان محفوظاً مسبقاً'} داخل البوت\n"
                    f"📖 {CATEGORIES[row['category']]} | الفصل {row['chapter']}\n"
                    f"📄 {row['title']}\n📎 عدد الملفات: {len(draft['media'])}\n"
                    'يستطيع الطلاب الوصول إليه من قسم الملازم والملخصات.',
                    reply_markup=kb([[B('📄 فتح المنشور',callback_data=f"resourceitem|{row['id']}")],
                                     [back()]]))
                if created:
                    failed = await send_to_group(context.bot, row, draft['media'])
                    if failed:
                        await q.message.reply_text(f'⚠️ نُشر داخل البوت، لكن تعذر إرسال {failed} مرفق للمجموعة. تحقق من صلاحيات البوت ومعرّف الموضوع.')
                return
            if action == 'deleteask' and len(parts)==3 and parts[2].isdigit():
                rid=int(parts[2]); row=await db.get_resource(rid)
                if not row:await q.answer('الملف غير موجود.',show_alert=True);return
                await q.answer()
                await q.edit_message_text(f"⚠️ تأكيد حذف «{row['title']}» وجميع مرفقاته من البوت؟",
                    reply_markup=kb([[B('🗑 تأكيد الحذف',callback_data=f'respub|delete|{rid}')],
                                     [B('إلغاء',callback_data=f'resourceitem|{rid}')]]))
                return
            if action == 'delete' and len(parts)==3 and parts[2].isdigit():
                rid=int(parts[2]);await q.answer()
                try:deleted=await delete_resource(rid,uid)
                except Exception:
                    logger.exception('Resource delete failed')
                    await q.message.reply_text('⚠️ تعذر الحذف. حاول لاحقاً.');return
                await q.edit_message_text('🗑 تم حذف المنشور.' if deleted else 'المنشور غير موجود.',
                    reply_markup=kb([[back()]]));return
            await q.answer('أمر غير صالح.',show_alert=True);return
        # Existing student resource browser stays intact. Admin gets a safe delete link.
        if data.startswith('resourceitem|') and ns['is_admin'](uid):
            try: rid=int(data.split('|')[1])
            except (IndexError,ValueError):return await previous_button(update,context)
            resource=await db.get_resource(rid)
            if not resource:return await q.answer('الملف غير موجود.',show_alert=True)
            media=await db.get_resource_media(rid)
            await q.answer()
            await q.edit_message_text(f"📄 {resource['title']}\n📘 الفصل {resource['chapter']}\n📎 عدد الملفات: {len(media)}",
                reply_markup=kb([[B('🗑 حذف المنشور',callback_data=f'respub|deleteask|{rid}')],
                                 [B('◀️ الملفات',callback_data=f"resourcechapter|{resource['category']}|{resource['chapter']}")],
                                 [back()]]))
            for item in media:
                try:
                    if item['payload_type']=='document':await context.bot.send_document(uid,item['file_id'])
                    elif item['payload_type']=='photo':await context.bot.send_photo(uid,item['file_id'])
                    elif item['payload_type']=='video':await context.bot.send_video(uid,item['file_id'])
                except TelegramError:
                    logger.exception('Could not resend resource %s media',rid)
                    await q.message.reply_text('⚠️ تعذر إرسال أحد المرفقات. ربما انتهت صلاحية معرف الملف.')
            return
        return await previous_button(update, context)
    ns['button_handler'] = button_handler
    ns['_resource_save_draft'] = save_draft
    ns['_resource_migrate'] = migrate
