"""Explicit administrator activation and durable account suspension."""
async def change_access(db,user_id,action,actor):
 if action not in {'activate','block','unblock'}:raise ValueError('Invalid action')
 def op():
  with db.connect() as conn,conn.cursor() as cur:
   cur.execute('SELECT * FROM biology_students WHERE user_id=%s FOR UPDATE;',(int(user_id),));student=cur.fetchone()
   if not student:return None
   if action=='activate':
    cur.execute('''UPDATE biology_students SET approved=TRUE,admin_manual_active=TRUE,
      admin_blocked=FALSE,admin_access_previous_approval=TRUE WHERE user_id=%s RETURNING *;''',(int(user_id),))
   elif action=='block':
    cur.execute('''UPDATE biology_students SET admin_access_previous_approval=
      CASE WHEN admin_blocked THEN admin_access_previous_approval ELSE approved END,
      admin_blocked=TRUE,approved=FALSE WHERE user_id=%s RETURNING *;''',(int(user_id),))
   else:
    cur.execute('''UPDATE biology_students SET approved=
      CASE WHEN admin_blocked THEN admin_access_previous_approval ELSE approved END,
      admin_blocked=FALSE WHERE user_id=%s RETURNING *;''',(int(user_id),))
   result=cur.fetchone()
   cur.execute("INSERT INTO biology_audit(actor_id,action,details) VALUES(%s,%s,%s);",(int(actor),'admin_student_'+action,f'user_id={int(user_id)}'))
   conn.commit();return result
 return await db.run(op)

def install(ns,db):
 B=ns['InlineKeyboardButton'];K=ns['InlineKeyboardMarkup'];END=ns['ConversationHandler'].END
 def migrate():
  with db.connect() as conn,conn.cursor() as cur:
   cur.execute('''ALTER TABLE biology_students
     ADD COLUMN IF NOT EXISTS admin_manual_active BOOLEAN NOT NULL DEFAULT FALSE,
     ADD COLUMN IF NOT EXISTS admin_blocked BOOLEAN NOT NULL DEFAULT FALSE,
     ADD COLUMN IF NOT EXISTS admin_access_previous_approval BOOLEAN NOT NULL DEFAULT FALSE;''');conn.commit()
 previous_init=ns['post_init']
 async def post_init(app):
  await previous_init(app);await db.run(migrate)
 ns['post_init']=post_init

 async def details(q,student_id):
  student=await db.get_student(student_id)
  if not student:await q.edit_message_text('الحساب غير موجود.',reply_markup=K([[ns['back_menu']()]]));return
  blocked=bool(student.get('admin_blocked'))
  status='⛔ موقوف بقرار الإدارة' if blocked else '✅ مفعل' if student.get('approved') else '⏳ بانتظار التفعيل'
  rows=[]
  if blocked:rows.append([B('🔓 إعادة فتح الحساب',callback_data=f'admin_access|unblock|{student_id}',style='success')])
  else:
   if not student.get('approved'):rows.append([B('✅ تفعيل يدوي — بدون شرط ولي الأمر',callback_data=f'admin_access|activate|{student_id}',style='success')])
   rows.append([B('⛔ إيقاف حساب الطالب',callback_data=f'admin_access|block|{student_id}',style='danger')])
  rows.append([B('⚙️ الخانات المفعّلة',callback_data=f'sections|student|{student_id}',style='primary')])
  rows.append([B('👁 عرض واجهة الطالب',callback_data=f'preview_student|{student_id}',style='primary')])
  rows.append([B('◀️ إدارة الطلبة',callback_data='admin_students'),ns['back_menu']()])
  await q.edit_message_text(f"👤 {student.get('full_name') or '-'}\n🆔 {student_id}\n{status}\n👨‍👩‍👦 ولي الأمر: {'مربوط' if student.get('parent_chat_id') else 'غير مربوط'}\n⭐ XP: {student.get('xp',0)}\n⚠️ الإنذارات: {student.get('warnings',0)}",reply_markup=K(rows))

 previous_button=ns['button_handler']
 async def button(update,context):
  q=update.callback_query;data=str(q.data or '')
  if data.startswith(('admin_student|','admin_access|','admin_access_confirm|','approve|')):
   if not ns['is_admin'](q.from_user.id):await q.answer('للإدارة فقط.',show_alert=True);return
   try:
    if data.startswith('admin_student|'):
     student_id=int(data.split('|')[1]);await q.answer();await details(q,student_id);return
    if data.startswith('approve|'):action='activate';student_id=int(data.split('|')[1])
    else:_,action,raw=data.split('|');student_id=int(raw)
    if action not in {'activate','block','unblock'}:raise ValueError()
   except (ValueError,IndexError):await q.answer('رابط غير صالح.',show_alert=True);return
   if ns['is_admin'](student_id):await q.answer('لا يمكن تغيير وصول الأدمن من إدارة الطلبة.',show_alert=True);return
   if action=='block' and not data.startswith('admin_access_confirm|'):
    await q.answer();await q.edit_message_text(f'تأكيد إيقاف حساب الطالب {student_id}؟ تبقى معلوماته وXP وإنذاراته محفوظة.',reply_markup=K([[B('⛔ تأكيد الإيقاف',callback_data=f'admin_access_confirm|block|{student_id}',style='danger')],[B('تراجع',callback_data=f'admin_student|{student_id}')]]));return
   student=await change_access(db,student_id,action,q.from_user.id)
   if not student:await q.answer('الحساب غير موجود.',show_alert=True);return
   cache=ns['_V51_STUDENT_CACHE'].get()
   if cache is not None:cache.pop(student_id,None)
   await q.answer('تم حفظ التغيير.',show_alert=True)
   try:
    text={'activate':'✅ فعّلت الإدارة حسابك يدوياً. يمكنك استعمال البوت بدون شرط ربط ولي الأمر. أرسل /start.','block':'⛔ أوقفت الإدارة وصولك إلى البوت. راجع الأستاذ.','unblock':'🔓 أعادت الإدارة فتح حسابك. أرسل /start.'}[action]
    await context.bot.send_message(student_id,text)
   except ns['TelegramError']:ns['logger'].warning('Student access notice failed for %s',student_id)
   await details(q,student_id);return
  return await previous_button(update,context)
 ns['button_handler']=button

 previous_start=ns['start']
 async def start(update,context):
  if ns['is_admin'](update.effective_user.id):return await previous_start(update,context)
  student=await db.get_student(update.effective_user.id)
  if student and student.get('admin_blocked'):
   await update.effective_message.reply_text('⛔ حسابك موقوف بقرار الإدارة. راجع الأستاذ.');return END
  if student and student.get('approved') and student.get('admin_manual_active') and not student.get('reset_pending'):
   context.user_data.pop('registration',None)
   keyboard=ns['onboarding_track_keyboard']() if not student.get('study_track') or int(student.get('onboarding_version') or 0)<19 else ns['main_menu']()
   await update.effective_message.reply_text('✅ حسابك مفعل يدوياً من الإدارة. اختر القسم المطلوب:',reply_markup=keyboard);return END
  return await previous_start(update,context)
 ns['start']=start

 async def access_guard(update,context):
  user=update.effective_user
  if not user or ns['is_admin'](user.id):return
  student=await db.get_student(user.id)
  if not student or not student.get('admin_blocked'):return
  if update.callback_query and str(getattr(update.callback_query,'data','') or '') in {'self_delete','self_delete_confirm'}:return
  if update.callback_query:await update.callback_query.answer('⛔ حسابك موقوف بقرار الإدارة.',show_alert=True)
  elif update.effective_message and update.effective_chat.type=='private':
   await update.effective_message.reply_text('⛔ حسابك موقوف بقرار الإدارة. راجع الأستاذ.',reply_markup=K([[B('🗑 حذف حسابي',callback_data='self_delete',style='danger')]]))
  raise ns['ApplicationHandlerStop']
 ns['admin_access_guard']=access_guard
