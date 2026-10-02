"""Lecture exam difficulty: required easy exams, optional hard exams, no study lock."""


def migrate(db):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("""ALTER TABLE biology_linked_exam_definitions
            ADD COLUMN IF NOT EXISTS difficulty TEXT NOT NULL DEFAULT 'easy'
                CHECK(difficulty IN ('easy','hard'));
            ALTER TABLE biology_tasks ADD COLUMN IF NOT EXISTS exam_difficulty TEXT
                CHECK(exam_difficulty IN ('easy','hard'));""")


async def set_difficulty(db, definition_id, difficulty, actor):
    if difficulty not in {'easy', 'hard'}:
        raise ValueError('Invalid difficulty')
    def op():
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE biology_linked_exam_definitions
                SET difficulty=%s,updated_at=CURRENT_TIMESTAMP
                WHERE id=%s AND deleted_at IS NULL RETURNING id;''', (difficulty,definition_id))
            if not cur.fetchone(): return False
            # Submitted attempts keep their awarded XP and historical classification.
            cur.execute('''UPDATE biology_tasks t SET exam_difficulty=%s,
                    optional_practice=%s,xp_reward=%s
                WHERE t.exam_definition_id=%s AND t.retired_obligation=FALSE
                  AND NOT EXISTS(SELECT 1 FROM biology_submissions s
                    WHERE s.task_id=t.id AND s.submitted_at IS NOT NULL);''',
                (difficulty,difficulty=='hard',60 if difficulty=='hard' else 20,definition_id))
            cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,'exam_difficulty',%s);",
                        (actor,f'definition={definition_id};difficulty={difficulty}'))
            return True
    return await db.run(op)


async def task_is_hard(db, task_id):
    task=await db.get_task(task_id)
    return bool(task and task.get('exam_difficulty')=='hard')


def install(ns, db):
    B,K=ns['InlineKeyboardButton'],ns['InlineKeyboardMarkup']
    previous=ns['button_handler']
    async def button_handler(update, context):
        q=update.callback_query; data=q.data or ''; state=context.user_data.get('linked_exam') or {}
        if data.startswith('difficulty_') or (data=='v53_admin_exam_done' and not state.get('difficulty')):
            if not ns['is_admin'](q.from_user.id):
                await q.answer('للإدارة فقط.',show_alert=True); return
            if data=='v53_admin_exam_done':
                if not state.get('selected_lectures'):
                    await q.answer('اختر المحاضرات أولا.',show_alert=True); return
                await q.answer()
                await q.edit_message_text('اختر مستوى الامتحان:\n🟢 السهل: إلزامي، مهلة 24 ساعة من نزوله، ومكافأة 20 XP.\n🔴 الصعب: اختياري، بلا إنذار، ومكافأة 60 XP.',
                    reply_markup=K([[B('🟢 سهل — إلزامي',callback_data='difficulty_new|easy')],
                                    [B('🔴 صعب — اختياري ×3',callback_data='difficulty_new|hard')],
                                    [ns['back_menu']()]]));return
            if data.startswith('difficulty_new|'):
                value=data.split('|')[1]
                if value not in {'easy','hard'} or not state.get('selected_lectures'):
                    await q.answer('ابدأ اختيار الامتحان مجددا.',show_alert=True);return
                state['difficulty']=value
                # Delegate through the existing lecture picker / reuse workflow.
                class QueryProxy:
                    data='v53_admin_exam_done'
                    def __getattr__(self,name):return getattr(q,name)
                class UpdateProxy:
                    callback_query=QueryProxy()
                    def __getattr__(self,name):return getattr(update,name)
                return await previous(UpdateProxy(),context)
            if data.startswith('difficulty_edit|'):
                definition_id=int(data.split('|')[1])
                definition=await db.v31_exam_definition_for_admin(definition_id)
                if not definition:await q.answer('الامتحان غير موجود.',show_alert=True);return
                await q.answer()
                await q.edit_message_text(f"تصنيف: {definition['title']}\nالتغيير يشمل المحاولات غير المسلّمة. تبقى المكافآت والإنذارات السابقة كما هي.",
                    reply_markup=K([[B('🟢 سهل — إلزامي',callback_data=f'difficulty_save|{definition_id}|easy')],
                                    [B('🔴 صعب — اختياري ×3',callback_data=f'difficulty_save|{definition_id}|hard')],
                                    [ns['back_menu']()]]));return
            if data.startswith('difficulty_save|'):
                _,number,value=data.split('|')
                if value not in {'easy','hard'}:await q.answer('تصنيف غير صالح.',show_alert=True);return
                saved=await set_difficulty(db,int(number),value,q.from_user.id)
                await q.answer('تم حفظ التصنيف' if saved else 'الامتحان غير موجود.')
                if saved:await ns['v52_admin_exam_students'](q,int(number))
                return
        return await previous(update,context)
    ns['button_handler']=button_handler
