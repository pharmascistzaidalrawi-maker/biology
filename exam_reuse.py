"""Reuse published chapter questions for a separate, idempotent course exam."""
from data import PLAYLISTS


def lecture_set(values):
    pairs = {(int(ch), int(lecture)) for ch, lecture in values}
    if not pairs or any(ch not in PLAYLISTS or lecture not in {int(x[0]) for x in PLAYLISTS[ch]}
                        for ch, lecture in pairs):
        raise ValueError('Invalid lectures')
    return pairs


def migrate(db):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute('''ALTER TABLE biology_linked_exam_definitions
            ADD COLUMN IF NOT EXISTS source_exam_id INTEGER
            REFERENCES biology_linked_exam_definitions(id) ON DELETE SET NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS biology_reused_course_exam_idx
            ON biology_linked_exam_definitions(source_exam_id)
            WHERE source_exam_id IS NOT NULL AND target_scope='course'
              AND deleted_at IS NULL AND obligation_retired_at IS NULL;''')


async def matching_exams(db, selected, cumulative=False, difficulty=None):
    wanted = lecture_set(selected)
    def op():
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT d.* FROM biology_linked_exam_definitions d
                WHERE d.target_scope='chapter' AND d.deleted_at IS NULL
                  AND d.obligation_retired_at IS NULL AND d.exam_type=%s
                  AND EXISTS(SELECT 1 FROM biology_linked_exam_media m WHERE m.definition_id=d.id)
                ORDER BY d.id DESC;''', ('cumulative' if cumulative else 'normal',))
            return [row for row in cur.fetchall()
                    if db._v47_required_lectures(cur, row['id']) == wanted
                    and (difficulty is None or row['difficulty']==difficulty)]
    return await db.run(op)


async def reuse_exam(db, source_id, selected, actor, cumulative=False, difficulty=None):
    wanted = lecture_set(selected)
    def op():
        with db.connect() as conn, conn.cursor() as cur:
            # Serializes duplicate clicks and concurrent assignments of the same source.
            cur.execute('SELECT * FROM biology_linked_exam_definitions WHERE id=%s FOR UPDATE;', (source_id,))
            source = cur.fetchone()
            if (not source or source['target_scope'] != 'chapter' or source.get('deleted_at')
                    or source.get('obligation_retired_at')
                    or (difficulty is not None and source['difficulty']!=difficulty)
                    or source['exam_type'] != ('cumulative' if cumulative else 'normal')
                    or db._v47_required_lectures(cur, source_id) != wanted):
                return None
            cur.execute('''SELECT * FROM biology_linked_exam_definitions
                WHERE source_exam_id=%s AND target_scope='course'
                  AND deleted_at IS NULL AND obligation_retired_at IS NULL;''', (source_id,))
            existing = cur.fetchone()
            if existing:
                return {**existing,'difficulty_mismatch':existing['difficulty']!=source['difficulty']}
            cur.execute('SELECT 1 FROM biology_linked_exam_media WHERE definition_id=%s LIMIT 1;', (source_id,))
            if not cur.fetchone():
                return None
            cur.execute('''INSERT INTO biology_linked_exam_definitions
                (chapter,prep_no,title,created_by,target_scope,exam_type,duration_hours,
                 availability_mode,release_hour,release_next_day,source_exam_id,difficulty)
                VALUES(%s,NULL,%s,%s,'course',%s,24,'course_next_day',18,TRUE,%s,%s)
                RETURNING *;''', (source['chapter'], source['title'], actor, source['exam_type'], source_id,source['difficulty']))
            result = cur.fetchone()
            for pos, (ch, lecture) in enumerate(sorted(wanted)):
                cur.execute('''INSERT INTO biology_linked_exam_lectures
                    (definition_id,chapter,lecture,position) VALUES(%s,%s,%s,%s);''',
                    (result['id'], ch, lecture, pos))
            cur.execute('''INSERT INTO biology_linked_exam_media(definition_id,payload_type,file_id,position)
                SELECT %s,payload_type,file_id,position FROM biology_linked_exam_media
                WHERE definition_id=%s;''', (result['id'], source_id))
            cur.execute('''INSERT INTO biology_exam_model_answer_media
                (definition_id,payload_type,file_id,text_content,position,created_by)
                SELECT %s,payload_type,file_id,text_content,position,%s
                FROM biology_exam_model_answer_media WHERE definition_id=%s;''', (result['id'], actor, source_id))
            cur.execute('''INSERT INTO biology_audit(actor_id,action,details)
                VALUES(%s,'reuse_chapter_exam',%s);''', (actor, f"source={source_id};course={result['id']}"))
            return result
    return await db.run(op)


def install(ns, db):
    B, K = ns['InlineKeyboardButton'], ns['InlineKeyboardMarkup']
    previous = ns['button_handler']

    async def choose(query, context, page=0):
        state = context.user_data.get('linked_exam') or {}
        if state.get('audience') != 'course' or not state.get('selected_lectures'):
            await query.answer('حدد محاضرات الدورة أولا.', show_alert=True)
            return
        matches = await matching_exams(db, state['selected_lectures'], state.get('cumulative', False),state.get('difficulty'))
        page = max(0, min(page, (len(matches)-1)//8 if matches else 0))
        rows = [[B(f"📝 {row['title'][:65]} · #{row['id']}", callback_data=f"reuse_pick|{row['id']}")]
                for row in matches[page*8:(page+1)*8]]
        nav = []
        if page: nav.append(B('السابق', callback_data=f'reuse_page|{page-1}'))
        if (page+1)*8 < len(matches): nav.append(B('التالي', callback_data=f'reuse_page|{page+1}'))
        if nav: rows.append(nav)
        rows += [[B('➕ رفع امتحان جديد', callback_data='reuse_upload')],
                 [B('◀️ تعديل المحاضرات', callback_data='v53_admin_exam_chapters'), ns['back_menu']()]]
        labels = '، '.join(f'ف{ch}/م{lec}' for ch, lec in sorted(lecture_set(state['selected_lectures'])))
        text = (f'📚 امتحانات الفصول المطابقة\n{labels}\n\n'
                + ('اختر الامتحان المنشور لإسناده إلى طلاب الدورة.' if matches
                   else 'لا يوجد امتحان منشور يطابق جميع المحاضرات المختارة ونوع الامتحان. يمكنك تعديل الاختيار أو رفع امتحان جديد.'))
        await query.answer()
        await query.edit_message_text(text, reply_markup=K(rows))

    async def button_handler(update, context):
        q = update.callback_query
        data = q.data or ''
        state = context.user_data.get('linked_exam') or {}
        if data.startswith('reuse_') or (data == 'v53_admin_exam_done' and state.get('audience') == 'course'):
            if not ns['is_admin'](q.from_user.id):
                await q.answer('للإدارة فقط.', show_alert=True)
                return
            if state.get('audience') != 'course' or not state.get('selected_lectures'):
                await q.answer('انتهت جلسة الاختيار. ابدأ نشر الامتحان مجددا.', show_alert=True)
                return
            if data == 'v53_admin_exam_done' or data.startswith('reuse_page|'):
                page = int(data.split('|')[1]) if '|' in data else 0
                return await choose(q, context, page)
            if data == 'reuse_upload':
                state['step'] = 'title'
                await q.answer()
                await q.edit_message_text('✍️ أرسل اسم الامتحان الجديد.', reply_markup=K([[ns['back_menu']()]]))
                return
            if data.startswith('reuse_pick|'):
                source_id = int(data.split('|')[1])
                matches = await matching_exams(db, state['selected_lectures'], state.get('cumulative', False),state.get('difficulty'))
                source = next((row for row in matches if row['id'] == source_id), None)
                if not source:
                    await q.answer('الامتحان لم يعد متاحا أو لا يطابق المحاضرات.', show_alert=True)
                    return
                state['reuse_source_id'] = source_id
                await q.answer()
                await q.edit_message_text(
                    f"تأكيد إسناد «{source['title']}» لطلاب الدورة؟\n"
                    'تُنسخ الأسئلة والجواب النموذجي الموجود حاليا، مع سجل درجات وموعد مستقل للدورة. '
                    'أي تعديل لاحق على الأصل لا يغيّر نسخة الدورة.',
                    reply_markup=K([[B('✅ إسناد الامتحان', callback_data=f'reuse_confirm|{source_id}')],
                                    [B('◀️ الامتحانات المطابقة', callback_data='reuse_page|0')]]))
                return
            if data.startswith('reuse_confirm|'):
                source_id = int(data.split('|')[1])
                if state.get('reuse_source_id') != source_id:
                    await q.answer('اختر الامتحان وأكد الإسناد مجددا.', show_alert=True)
                    return
                result = await reuse_exam(db, source_id, state['selected_lectures'], q.from_user.id,
                                          state.get('cumulative', False),state.get('difficulty'))
                if not result:
                    await q.answer('تغير الامتحان أو حُذف؛ اختره مجددا.', show_alert=True)
                    return
                if result.get('difficulty_mismatch'):
                    await q.answer('نسخة الدورة الحالية لها تصنيف مختلف.',show_alert=True)
                    await q.edit_message_text('هذا الامتحان أُسند للدورة مسبقا بتصنيف مختلف. عدل تصنيف نسخة الدورة من الزر أدناه؛ لا يتغير تلقائيا بتعديل الأصل.',
                        reply_markup=K([[B('⚙️ تصنيف نسخة الدورة',callback_data=f"difficulty_edit|{result['id']}")],[ns['back_menu']()]]))
                    return
                context.user_data.pop('linked_exam', None)
                window = await db.v47_course_window(result['id'])
                message = ('سيُفتح حسب جدول الدورة، أو يمكنك تحديد الموعدين أدناه.'
                           if window['status'] == 'ready' else 'حدد وقت الفتح والانتهاء لتفعيل الامتحان.')
                await q.answer('تم إسناد الامتحان')
                await q.edit_message_text(f"✅ امتحان الدورة #{result['id']}: {result['title']}\n{message}",
                    reply_markup=K([[B('🗓 تحديد الفتح والانتهاء', callback_data=f"v47_window|{result['id']}")],
                                    [ns['back_menu']()]]))
                return
        return await previous(update, context)

    ns['button_handler'] = button_handler
