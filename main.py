import asyncio
import logging
import hashlib
import os
import re
import secrets
import threading
import time
import psycopg
from collections import defaultdict, deque
from html import escape
from io import BytesIO
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

from telegram import BotCommand, CopyTextButton, InlineKeyboardButton as TelegramInlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, TelegramError, NetworkError, TimedOut
from telegram.ext import (
    AIORateLimiter, Application, ApplicationHandlerStop, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

from database import DATABASE_URL
from data import CHAPTER_COUNT, CHAPTER_PREPARATION_DISTRIBUTION, LECTURE_SUPPLEMENTS, NEXT_PREPARATIONS, PLAYLISTS, PREPARATIONS
from database import (
    add_archive_exam_media, add_pending_media_by_id, add_resource_media, add_submission_review_message, add_task_media_by_id, add_warning, add_warning_once, all_students_admin_view, all_preparations, append_pending_media,
    add_extra_preparation, adjust_xp, approved_students, approve_parent, archive_exams, archive_exams_by_lecture, archive_lectures, award_daily_preparation, buy_remove_warning, close_task, complete_backlog, confirm_pending_task, create_archive_exam, create_extension_request, create_leave_request, create_parent_leave, create_pending_task, decide_exam_access, decide_extension_request, decide_leave_request, due_exam_reminders,
    backlog_items, cancel_scheduled_task, communication_route, create_resource, create_scheduled_task, create_task, delete_pending_task, delete_student, delete_task, due_preparations, due_scheduled_tasks, due_tasks, due_unactivated_members, get_pending_task, unwatched_lectures_for_student,
    effective_task_deadline, exam_access, get_archive_exam, get_archive_exam_media, get_cumulative_exam, get_student, get_student_by_parent_code, get_task,
    exam_champions_if_ready, get_resource, get_resource_media, grade_submission_by_review, init_db, latest_preparation, link_parent,
    get_task_media, mark_preparation_published, missing_students, open_tasks, preparation_for_date, prepare_submission_retry,
    incomplete_preparation_students, incomplete_personal_preparation_students, lecture_progress, mark_lecture_progress, mark_member_compliant, mark_member_removed, next_unpublished_preparation, observe_group_member, observe_known_unactivated_members, open_exam_tasks, parent_report_bundle, pending_scheduled_tasks, plan_backlog, record_submission, register_student, remove_warning, reschedule_unpublished_preparations, resources_by_chapter,
    mark_exam_reminder_sent, mark_scheduled_task_published, save_communication_route, save_exam_correction, save_student_topic, scheduled_task_media, seed_preparations, set_cumulative_exam, set_setting_value, student_by_parent, student_topic, toggle_backlog, mark_champion_announced,
    request_exam_access, scheduled_exam_parent_reminders, set_backlog_deadline, set_student_approval, setting_value, shift_unpublished_preparations, submission_by_review, task_has_active_extensions, weekly_parent_reports, task_warning_audit,
    v31_init_exam_controls, v31_active_exam_definitions, v31_delete_exam_definition, v31_exam_definition_for_admin, v31_extend_student_exam,
    mark_questions_released, mark_scheduled_parent_reminder, mark_personal_preparation_notified, personal_preparation_for_student, personal_preparations_for_student_prep, set_student_onboarding, set_submission_grade, student_achievements, student_warning_history, students_by_parent, student_parents, students_pending_task, students_requiring_onboarding, students_for_scope, assigned_students, due_personal_preparation_notifications, preparation_catalog, backfill_personal_prep_numbers, personal_preparations_for_chapter, create_linked_exam_definition, linked_exam_definitions, linked_exam_media, linked_exam_preparations, linked_exam_lectures_text, active_students_for_linked_exam, student_finish_date, leave_month_usage, shift_personal_schedule_for_leave, set_pending_task_scope, swap_next_preparations, update_student_profile, weekly_schedule, weekly_top_student, all_parents_admin_view, due_teacher_exam_deadline_reminders, mark_teacher_exam_deadline_reminder, teacher_change_exam_deadline, latest_daily_exam, reopen_latest_daily_exam, decide_parent_link, unreleased_closed_exams, recently_closed_tasks_for_warning_recovery, closed_exams_pending_champion, student_schedule, set_student_schedule, reset_personal_schedule_to_regular, linked_exam_definition_status, task_for_linked_exam_student, create_linked_exam_task_for_student, activate_exam, student_exam_lock, add_linked_exam_lectures,
)

_INVALID_INTEGER_ENV=set()
def _env_int(name,default=0):
    value=os.getenv(name)
    if value is None or not str(value).strip(): return int(default)
    try: return int(str(value).strip())
    except (TypeError,ValueError):
        _INVALID_INTEGER_ENV.add(name); return int(default)

def _env_bool(name,default=False):
    value=os.getenv(name)
    if value is None or not str(value).strip(): return bool(default)
    return str(value).strip().lower() in {'1','true','yes','on','enabled'}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("biology_bot")

PRIVATE_STUDY_OATH = "اقسم بالله العظيم اني قد درست هذه المحاضرة على مصدري الخاص بالتمام والكمال وانا غير مبرئ الذمة في حال كذبت في قولي هذا"
ROYAL_REVIEW_OATH = "اقسم بالله العظيم اني قد راجعت هذه المحاضرة بالتمام والكمال واني غير مبرئ الذمة في حال كذبت في قولي"
WEAKNESS_RESOLUTION_OATH = "اقسم بالله العظيم اني قد تمكنت من حل نقطة ضعفي هذه واني غير مبرئ الذمة في حال كذبت في قولي"
SCHOOL_REVIEW_OATH = "اقسم بالله العظيم اني قد اكملت مراجعة المدرسة لهذا الاسبوع بالتمام والكمال وانا غير مبرئ الذمة في حال كذبت في قولي هذا"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Baghdad"))
BIOLOGY_GROUP_ID = _env_int("BIOLOGY_GROUP_ID")
PREPARATION_TOPIC_ID = _env_int("PREPARATION_TOPIC_ID")
HOMEWORK_TOPIC_ID = _env_int("HOMEWORK_TOPIC_ID")
EXAM_TOPIC_ID = _env_int("EXAM_TOPIC_ID")
SCHOOL_REVIEW_TOPIC_ID = _env_int("SCHOOL_REVIEW_TOPIC_ID")
HOMEWORK_GROUP_ID = _env_int("HOMEWORK_GROUP_ID")
EXAM_GROUP_ID = _env_int("EXAM_GROUP_ID")
HOMEWORK_SUBMISSIONS_CHAT_ID = _env_int("HOMEWORK_SUBMISSIONS_CHAT_ID",HOMEWORK_GROUP_ID)
EXAM_SUBMISSIONS_CHAT_ID = _env_int("EXAM_SUBMISSIONS_CHAT_ID",EXAM_GROUP_ID)
OWNER_CHAT_ID = _env_int("OWNER_CHAT_ID")
ACTIVATION_GROUP_ID = _env_int("ACTIVATION_GROUP_ID")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "").lstrip("@")
GROUP_INVITE_URL = os.getenv("GROUP_INVITE_URL", "")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@almujtahid_platform")
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/almujtahid_platform")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
FOUNDER_IDS = {int(x) for x in os.getenv("FOUNDER_IDS", "").split(",") if x.strip().isdigit()}
DEFAULT_HOMEWORK_HOURS = _env_int("DEFAULT_HOMEWORK_HOURS",24)
DEFAULT_EXAM_HOURS = 24
MAX_WARNINGS = 5
CHAMPIONS_TOPIC_ID=_env_int("CHAMPIONS_TOPIC_ID",11)
WARNINGS_TOPIC_ID=_env_int("WARNINGS_TOPIC_ID",9)
MIN_LECTURE_WATCH_MINUTES=max(30,_env_int("MIN_LECTURE_WATCH_MINUTES",30))
ACTIVATION_GRACE_HOURS=max(1,_env_int("ACTIVATION_GRACE_HOURS",24))
_spam_events=defaultdict(deque)
_OWNER_ERROR_ALERT_AT=0.0

REG_NAME, REG_SCHOOL, REG_GRADE, REG_JOIN = range(4)
DIV = "━━━━━━━━━━━━━━━━━━"
BUILD_VERSION = "v1.2-parent-roles"
NEON_ECO_MODE=_env_bool("NEON_ECO_MODE",True)
NEON_ECO_INTERVAL_SECONDS=max(900,_env_int("NEON_ECO_INTERVAL_SECONDS",1800))
NEON_BACKGROUND_INTERVAL_SECONDS=max(3600,_env_int("NEON_BACKGROUND_INTERVAL_SECONDS",21600))
NEON_ACTIVITY_SWEEP_SECONDS=max(60,_env_int("NEON_ACTIVITY_SWEEP_SECONDS",300))
USE_DATABASE_INSTANCE_LOCK=_env_bool("USE_DATABASE_INSTANCE_LOCK",not NEON_ECO_MODE)
DELETE_STUDENT_ANSWER_AFTER_DELIVERY=_env_bool("DELETE_STUDENT_ANSWER_AFTER_DELIVERY",True)
ANSWER_CLEANUP_LOOKBACK_DAYS=max(1,min(31,_env_int("ANSWER_CLEANUP_LOOKBACK_DAYS",7)))
TELEGRAM_DELETE_LIMIT_HOURS=max(1,min(47,_env_int("TELEGRAM_DELETE_LIMIT_HOURS",47)))
try:
    EXAM_ENFORCEMENT_START_DATE=date.fromisoformat(os.getenv("EXAM_ENFORCEMENT_START_DATE_V46","2026-09-27"))
except ValueError:
    EXAM_ENFORCEMENT_START_DATE=date(2026,9,7)
try:
    LATE_EXAM_XP_COST=max(0,int(os.getenv("LATE_EXAM_XP_COST","150") or 150))
except ValueError:
    LATE_EXAM_XP_COST=150
try:
    SUBMISSION_RETRY_SECONDS=max(15,min(3600,int(os.getenv("SUBMISSION_RETRY_SECONDS","60") or 60)))
except ValueError:
    SUBMISSION_RETRY_SECONDS=60
try:
    SUBMISSION_MAX_RETRIES=max(1,min(100,int(os.getenv("SUBMISSION_MAX_RETRIES","20") or 20)))
except ValueError:
    SUBMISSION_MAX_RETRIES=20


def _semantic_button_style(text,callback_data=None):
    """Use Telegram 9.4 semantic colours consistently across the whole UI."""
    label=str(text or '').strip().lower()
    action=str(callback_data or '').strip().lower()
    danger=('delete','cancel','reject','deny','close','remove_student','حذف','الغاء','إلغاء','رفض','اغلاق','إغلاق','تراجع')
    success=('confirm','finish','approve','submit','start','open','publish','save','verify','accept','تأكيد','تاكيد','حفظ','ابدأ','بدء','فتح','نشر','قبول')
    if any(token in action or token in label for token in danger) or label.startswith(('❌','🗑','🚫')):
        return 'danger'
    if any(token in action or token in label for token in success) or label.startswith(('✅','➕','▶️','🧪')):
        return 'success'
    return 'primary'


def InlineKeyboardButton(text,*args,**kwargs):
    """Project-wide button factory with Telegram-native semantic colours."""
    if kwargs.get('style') is None and not kwargs.get('pay') and not kwargs.get('callback_game'):
        kwargs['style']=_semantic_button_style(text,kwargs.get('callback_data'))
    return TelegramInlineKeyboardButton(text,*args,**kwargs)


def build_personal_plan(start_chapter,start_date):
    """Build the student's plan using the approved preparation distribution."""
    rows=[]; cursor=start_date
    for chapter in range(start_chapter,CHAPTER_COUNT+1):
        groups=CHAPTER_PREPARATION_DISTRIBUTION.get(chapter, [])
        if not groups:
            continue
        allowed={6,1,3,5}  # الأحد والثلاثاء والخميس والسبت
        for prep_no,nums in enumerate(groups,1):
            # Keep the previous weekday rules; only the lecture grouping is changed.
            while cursor.weekday() not in allowed:
                cursor += timedelta(days=1)
            rows.append((cursor,chapter,",".join(map(str,nums)),prep_no))
            cursor += timedelta(days=1)
    return rows

def onboarding_track_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📘 الفصل 1",callback_data="onboardtrack|1"),InlineKeyboardButton("📗 الفصل 2",callback_data="onboardtrack|2")],
        [InlineKeyboardButton("📙 الفصل 3",callback_data="onboardtrack|3"),InlineKeyboardButton("📕 الفصل 4",callback_data="onboardtrack|4")],
        [InlineKeyboardButton("📒 الفصل 5",callback_data="onboardtrack|5")],


        [InlineKeyboardButton("👥 أكمل مع الدورة الحالية",callback_data="onboardtrack|course")],
    ])


async def show_onboarding_track(message_or_query,edit=False):
    text=bold("📚 اختر مسار دراستك\n\nاختر الفصل الذي تريد أن تبدأ منه، أو اختر «أكمل مع الدورة» حتى تستمر مع تحاضير وواجبات وامتحانات الدورة الحالية كما كانت قبل التحديث.")
    if edit: await message_or_query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=onboarding_track_keyboard())
    else: await message_or_query.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=onboarding_track_keyboard())


def bold(text):
    return f"<b>{escape(str(text))}</b>"


def is_admin(user_id):
    return user_id==OWNER_CHAT_ID or user_id in ADMIN_IDS or user_id in FOUNDER_IDS


def main_menu(admin=False):
    rows=[
        [InlineKeyboardButton("🧪 تحاضير اليوم", callback_data="today_prep"),InlineKeyboardButton("📚 الواجبات", callback_data="tasks|homework")],
        [InlineKeyboardButton("📝 الامتحانات",callback_data="exams_menu"),InlineKeyboardButton("🎬 المحاضرات", callback_data="playlists")],
        [InlineKeyboardButton("✅ الأجوبة النموذجية",callback_data="resourcecategory|model_answer")],
        [InlineKeyboardButton("⚙️ إعدادات الحساب",callback_data="account_settings"),InlineKeyboardButton("⭐ متجر XP",callback_data="xp_store")],
        [InlineKeyboardButton("📚 المحاضرات المتراكمة",callback_data="backlog_auto")],
        [InlineKeyboardButton("📅 متى سوف ننهي المنهج؟",callback_data="chapter_completion_schedule")],
        [InlineKeyboardButton("🗓 الجداول الدراسية",callback_data="schedules_menu"),InlineKeyboardButton("📖 الملازم والملخصات",callback_data="study_resources")],
        [InlineKeyboardButton("🏅 إنجازاتي",callback_data="achievement_menu")],
    ]
    if admin: rows.append([InlineKeyboardButton("👥 إدارة الطلبة",callback_data="admin_students"),InlineKeyboardButton("👪 إدارة أولياء الأمور",callback_data="admin_parents")])
    if admin: rows.append([InlineKeyboardButton("🗓 إدارة جدول التحاضير",callback_data="prep_schedule")])
    if admin: rows.append([InlineKeyboardButton("➕ نشر واجب أو امتحان",callback_data="admin_publish")])
    if admin: rows.append([InlineKeyboardButton("⏳ إدارة تمديد الامتحانات",callback_data="admin_exam_extensions")])
    return InlineKeyboardMarkup(rows)


def parent_copy_markup(code, with_back=False):
    rows=[[InlineKeyboardButton('📋 نسخ /parent والرمز كاملاً',
            copy_text=CopyTextButton(text=f'/parent {code}'),style='primary')]]
    if with_back: rows.append([back_menu()])
    return InlineKeyboardMarkup(rows)


def parent_menu(students):
    rows=[]
    for s in students:
        sid=s["user_id"]
        rows.extend([
            [InlineKeyboardButton(f"📊 درجات وتقدم {s['full_name']}",callback_data=f"parentprogress|{sid}")],
            [InlineKeyboardButton("🏅 إنجازات هذا الأسبوع",callback_data=f"parentachievements|{sid}"),InlineKeyboardButton("⚠️ الإنذارات",callback_data=f"parentwarnings|{sid}")],
            [InlineKeyboardButton("🏖 طلب إجازة للطالب",callback_data=f"parentleave|{sid}")],
        ])
    rows.append([InlineKeyboardButton('🗑 حذف حساب ولي الأمر',callback_data='parent_delete',style='danger')])
    return InlineKeyboardMarkup(rows)


def guest_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 تحضير اليوم",callback_data="today_prep"),InlineKeyboardButton("🎬 المحاضرات",callback_data="playlists")],
        [InlineKeyboardButton("📖 الملازم والملخصات",callback_data="study_resources"),InlineKeyboardButton("🗂 امتحانات سابقة",callback_data="past_exams")],
        [InlineKeyboardButton("🎓 التسجيل في دورة الأحياء",callback_data="guest_enroll")],
    ])


def back_menu():
    return InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu")


async def is_group_member(bot, user_id):
    if not BIOLOGY_GROUP_ID:
        return True
    try:
        member = await bot.get_chat_member(BIOLOGY_GROUP_ID, user_id)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    except TelegramError:
        return False


async def is_channel_member(bot,user_id):
    try:
        member=await bot.get_chat_member(REQUIRED_CHANNEL,user_id)
        return member.status not in (ChatMemberStatus.LEFT,ChatMemberStatus.BANNED)
    except TelegramError:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(user.id):
        context.user_data.pop("registration",None)
        await update.effective_message.reply_text(bold("👑 مركز ادارة الأحياء\n━━━━━━━━━━━━━━━━━━\nالنشر • الامتحانات • المتابعة\n\nاختر القسم المطلوب من اللوحة المنظمة ادناه"),parse_mode=ParseMode.HTML,reply_markup=main_menu(True))
        return ConversationHandler.END
    existing=await get_student(user.id)
    if existing and existing["approved"] and int(existing.get("onboarding_version") or 0)<19:
        context.user_data.pop("registration",None)
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ البقاء على معلوماتي القديمة",callback_data="onboardkeep")],[InlineKeyboardButton("✏️ تغيير معلوماتي",callback_data="onboardedit")]])
        await update.effective_message.reply_text(bold(f"🆕 تحديث نظام الدراسة الجديد\n{DIV}\nبياناتك الحالية:\n👤 الاسم: {existing['full_name']}\n🏫 المدرسة: {existing['school']}\n🎯 المعدل المطلوب: {existing['target_grade']}\n\nهل تريد الاحتفاظ بهذه المعلومات أم تغييرها؟"),parse_mode=ParseMode.HTML,reply_markup=kb)
        return ConversationHandler.END
    parent_students=await students_by_parent(user.id,True)
    if parent_students and not existing:
        context.user_data.pop("registration",None)
        await update.effective_message.reply_text(bold("👪 واجهة ولي الأمر\n━━━━━━━━━━\nاختر ملف متابعة الطالب:"),parse_mode=ParseMode.HTML,reply_markup=parent_menu(parent_students))
        return ConversationHandler.END
    pending_parent_students=await students_by_parent(user.id,False)
    if pending_parent_students and not existing:
        context.user_data.pop("registration",None)
        names="، ".join(s["full_name"] for s in pending_parent_students)
        await update.effective_message.reply_text(bold(f"⏳ طلب حساب ولي الأمر قيد المراجعة.\nالطالب: {names}\nستعمل الواجهة فور ضغط الإدارة زر «تفعيل الحساب»."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 حذف حساب ولي الأمر",callback_data="parent_delete",style="danger")]]))
        return ConversationHandler.END
    if not await is_group_member(context.bot,user.id):
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم، أرغب بالتسجيل",callback_data="guest_enroll")],[InlineKeyboardButton("📚 لا، فتح المكتبة العامة",callback_data="guest_continue")]])
        await update.effective_message.reply_text(bold("🧪 أهلاً بك في أكاديمية الأحياء\n━━━━━━━━━━━━━━━━━━\nتعلّم • اختبر نفسك • تابع تقدمك\n━━━━━━━━━━━━━━━━━━\n\nهل ترغب بالانضمام إلى دورة الأحياء المجانية؟"),parse_mode=ParseMode.HTML,reply_markup=kb)
        return ConversationHandler.END
    if existing and existing["approved"]:
        context.user_data.pop("registration",None)
        if not existing.get("parent_chat_id"):
            await update.effective_message.reply_text(bold(f"🔒 تم إيقاف حسابك مؤقتاً\nيجب ربط ولي الأمر حتى تُفتح خدمات الدورة.\n\nرمز الربط: {existing['parent_link_code']}\nولي الأمر يفتح البوت وينسخ الأمر:")+f"\n<code>/parent {escape(existing['parent_link_code'])}</code>",parse_mode=ParseMode.HTML,reply_markup=parent_copy_markup(existing["parent_link_code"])); return ConversationHandler.END
        if not await is_channel_member(context.bot,user.id):
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("📢 الاشتراك بقناة منصة المجتهد",url=REQUIRED_CHANNEL_URL)]])
            await update.effective_message.reply_text(bold("🔒 خدمات البوت مقفولة. يجب الاشتراك بقناة منصة المجتهد أولاً."),parse_mode=ParseMode.HTML,reply_markup=kb)
            return ConversationHandler.END
        await update.effective_message.reply_text(bold(f"⚡ بوت الأحياء | منصة المجتهد التعليمية\n━━━━━━━━━━━━━━━━━━\nأهلا {existing['full_name']} 👋\n\n🎯 ابدأ من مهامي اليومية حتى يعرض لك البوت أهم خطوة دراسية الآن"), parse_mode=ParseMode.HTML, reply_markup=main_menu())
        return ConversationHandler.END
    if existing and not existing["approved"]:
        if not existing.get("parent_chat_id"):
            await update.effective_message.reply_text(bold(f"👨‍👩‍👦 تسجيلك محفوظ، لكن يجب ربط ولي الأمر أولاً.\nرمز الربط: {existing['parent_link_code']}\nولي الأمر يفتح البوت وينسخ الأمر:")+f"\n<code>/parent {escape(existing['parent_link_code'])}</code>",parse_mode=ParseMode.HTML,reply_markup=parent_copy_markup(existing["parent_link_code"]))
        else:
            await update.effective_message.reply_text(bold("⏳ تم ربط ولي الأمر وطلب تفعيل حسابك قيد مراجعة الإدارة."),parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    context.user_data["registration"] = {}
    context.user_data["registration_started_at"] = datetime.now(TIMEZONE)
    await update.effective_message.reply_text(bold("🧪 أهلاً بك في بوت مادة الأحياء\n\n✍️ أولاً: أرسل اسمك الرباعي:"), parse_mode=ParseMode.HTML)
    return REG_NAME


async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value=(update.message.text or "").strip()
    if len(value.split()) < 3 or len(value)>120:
        await update.effective_message.reply_text(bold("⚠️ أرسل اسمك الثلاثي أو الرباعي بصورة صحيحة."),parse_mode=ParseMode.HTML); return REG_NAME
    context.user_data["registration"]["full_name"]=value
    await update.effective_message.reply_text(bold("🏫 ثانياً: أرسل اسم المدرسة:"),parse_mode=ParseMode.HTML); return REG_SCHOOL


async def reg_school(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value=(update.message.text or "").strip()
    if len(value)<2 or len(value)>160: await update.effective_message.reply_text(bold("⚠️ أرسل اسم المدرسة بصورة صحيحة (160 حرفاً كحد أقصى)."),parse_mode=ParseMode.HTML); return REG_SCHOOL
    context.user_data["registration"]["school"]=value
    await update.effective_message.reply_text(bold("🎯 ثالثاً: ما المعدل الذي تود الحصول عليه؟\nمثال: 95 أو 100"),parse_mode=ParseMode.HTML); return REG_GRADE


async def reg_grade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value=(update.message.text or "").strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,2})?",value) or not 0<=float(value)<=100:
        await update.effective_message.reply_text(bold("⚠️ أرسل معدلاً رقمياً من 0 إلى 100."),parse_mode=ParseMode.HTML); return REG_GRADE
    context.user_data["registration"]["target_grade"]=value
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 الاشتراك بقناة منصة المجتهد",url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton("👥 الانضمام إلى كروب الأحياء",url=GROUP_INVITE_URL)],
        [InlineKeyboardButton("✅ تحقق وأرسل طلب التفعيل",callback_data="verify_join")],
    ])
    await update.effective_message.reply_text(bold("📌 رابعاً: اشترك بقناة منصة المجتهد وانضم إلى كروب الأحياء، ثم اضغط زر التحقق."),parse_mode=ParseMode.HTML,reply_markup=kb); return REG_JOIN


async def verify_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer()
    if not await is_channel_member(context.bot,update.effective_user.id):
        await query.answer("يجب الاشتراك بقناة منصة المجتهد أولاً.",show_alert=True); return REG_JOIN
    if not await is_group_member(context.bot,update.effective_user.id):
        await query.answer("لم يتم العثور عليك في كروب الأحياء بعد.",show_alert=True); return REG_JOIN
    reg=context.user_data.get("registration",{})
    student=await register_student(update.effective_user.id,update.effective_user.username,reg["full_name"],reg["school"],reg["target_grade"])
    context.user_data.pop("registration",None)
    if student.get('status')=='parent_account':
        await query.edit_message_text('حسابك مربوط كولي أمر. لا يمكن تسجيل الحساب نفسه كطالب؛ احذف حساب ولي الأمر أولاً من واجهته.')
        return ConversationHandler.END
    await query.edit_message_text(bold("✅ تم حفظ معلوماتك. بقي اختيار مسار الدراسة."),parse_mode=ParseMode.HTML,reply_markup=onboarding_track_keyboard())
    return ConversationHandler.END


async def send_activation_request(bot,student):
    buttons=InlineKeyboardMarkup([[InlineKeyboardButton("✅ قبول وتفعيل",callback_data=f"approve|{student['user_id']}"),InlineKeyboardButton("❌ رفض",callback_data=f"reject|{student['user_id']}")]])
    text=bold(f"🔔 طلب تفعيل مكتمل\n\n👤 {student['full_name']}\n🆔 {student['user_id']}\n🏫 {student['school']}\n🎯 {student['target_grade']}\n👨‍👩‍👦 ولي الأمر: {student['parent_chat_id']}")
    await bot.send_message(ACTIVATION_GROUP_ID,text,parse_mode=ParseMode.HTML,reply_markup=buttons)


def preparation_rows():
    # The live course starts from chapter 3, lecture 11, on Sunday 27/09/2026.
    # Earlier lectures remain available in the chapter library only.
    rows=[]; current=date(2026,9,27); global_no=0; allowed={6,1,3,5}
    for chapter in range(1,CHAPTER_COUNT+1):
        for chapter_prep_no,lectures in enumerate(CHAPTER_PREPARATION_DISTRIBUTION[chapter],1):
            global_no+=1
            if chapter<3 or (chapter==3 and chapter_prep_no<11):
                continue
            while current.weekday() not in allowed: current+=timedelta(days=1)
            rows.append((global_no,current,",".join(map(str,lectures)),chapter,chapter_prep_no))
            current+=timedelta(days=1)
    return rows


def biology_video_buttons(chapter,lecture):
    """Offer all video parts before marking a numbered lecture complete."""
    first=PLAYLISTS[chapter][lecture-1][2]
    supplements=LECTURE_SUPPLEMENTS.get((chapter,lecture),[])
    rows=[[InlineKeyboardButton('▶️ الجزء الأول' if supplements else '▶️ مشاهدة المحاضرة',url=first,style='success')]]
    rows += [[InlineKeyboardButton(f'▶️ {title}',url=url,style='success')] for title,url in supplements]
    return rows


AR_DAYS={0:"الاثنين",1:"الثلاثاء",2:"الأربعاء",3:"الخميس",4:"الجمعة",5:"السبت",6:"الأحد"}


def preparation_text(row):
    target=row["target_date"]; nums="  +  ".join(row["lectures"].split(","))
    return bold(f"✦ تحاضير يوم {AR_DAYS[target.weekday()]} المصادف {target.day}/{target.month}/{target.year} ✦\n\n{DIV}\n\n🔹 الفصل {row.get('chapter',3)} | المحاضرة :  {nums}\n\n{DIV}")


async def publish_preparations_job(context: ContextTypes.DEFAULT_TYPE):
    now=datetime.now(TIMEZONE)
    for row in await due_preparations(now):
        if not BIOLOGY_GROUP_ID: continue
        try:
            await context.bot.send_message(BIOLOGY_GROUP_ID,preparation_text(row),parse_mode=ParseMode.HTML,message_thread_id=PREPARATION_TOPIC_ID or None)
            await mark_preparation_published(row["prep_no"])
            for student in await students_for_scope("course"):
                if await student_exam_lock(student["user_id"]):
                    continue
                notice=bold(f"📚 نزل تحضير جديد لمسار الدورة\nالفصل {row['chapter']} | المحاضرات: {row['lectures']}\nافتح «تحاضير اليوم» داخل البوت.")
                try: await context.bot.send_message(student["user_id"],notice,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📚 تحاضير اليوم",callback_data="today_prep")]]))
                except TelegramError: pass
                for parent in await student_parents(student["user_id"],True):
                    try: await context.bot.send_message(parent["parent_chat_id"],bold(f"📚 نزل تحضير جديد للطالب {student['full_name']}\nالفصل {row['chapter']} | المحاضرات: {row['lectures']}"),parse_mode=ParseMode.HTML)
                    except TelegramError: pass
        except TelegramError as exc: logger.exception("Preparation publish failed: %s",exc)


async def personal_preparations_job(context: ContextTypes.DEFAULT_TYPE):
    rows=await due_personal_preparation_notifications(datetime.now(TIMEZONE))
    if rows: await linked_exam_dispatch_job(context)
    for row in rows:
        # A student with any unsubmitted exam must not receive/open the next preparation.
        if await student_exam_lock(row["user_id"]):
            continue
        text=bold(f"📚 تحضيرك الشخصي الجديد\nالفصل {row['chapter']} | المحاضرات: {row['lectures']}\n📅 {row['target_date'].strftime('%d/%m/%Y')}\n\nافتح «تحاضير اليوم» داخل البوت.")
        try:
            await context.bot.send_message(row["user_id"],text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📚 تحاضير اليوم",callback_data="today_prep")]]))
        except TelegramError as exc:
            logger.warning("Personal preparation notification failed for %s: %s",row["user_id"],exc); continue
        for parent in await student_parents(row["user_id"],True):
            try: await context.bot.send_message(parent["parent_chat_id"],bold(f"📚 تحضير جديد للطالب {row['full_name']}\nالفصل {row['chapter']} | المحاضرات: {row['lectures']}\n📅 {row['target_date'].strftime('%d/%m/%Y')}"),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        await mark_personal_preparation_notified(row["id"],row["user_id"],row["chapter"])


async def show_today_preparation(query):
    student=await get_student(query.from_user.id)
    if student and not is_admin(query.from_user.id):
        lock=await student_exam_lock(query.from_user.id)
        if lock:
            if lock.get("exam_pending_activation"):
                status="بانتظار موافقة ولي الأمر أو الإدارة"; target="exams_menu"
            else:
                effective=await effective_task_deadline(lock["id"],query.from_user.id)
                status="انتهى وقته ولم يتم تقديمه" if effective and datetime.now(TIMEZONE)>=effective["deadline"] else "مفعّل ويجب أداؤه قبل التحضير التالي"
                target=f"task|{lock['id']}"
            await query.edit_message_text(bold(f"🔒 لا يمكن فتح التحضير التالي الآن.\n\n📝 الامتحان الحاجز: {lock['title']}\n📌 الحالة: {status}\n\nبعد أداء الامتحان وتسجيل التسليم يُفتح التحضير التالي تلقائيًا."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 الذهاب إلى الامتحان",callback_data=target)],[back_menu()]])); return
    personal=student and (student.get("study_track")=="chapter" or student.get("schedule_mode")=="custom")
    row=await personal_preparation_for_student(query.from_user.id,datetime.now(TIMEZONE).date()) if personal else await preparation_for_date(datetime.now(TIMEZONE).date())
    if not row and not personal: row=await latest_preparation()
    text=preparation_text(row) if row else bold("📭 لا يوجد تحضير منشور حالياً.")
    kb=[]
    if row:
        for lecture in map(int,row["lectures"].split(",")): kb.append([InlineKeyboardButton(f"▶️ الذهاب إلى المحاضرة {lecture}",callback_data=f"prepopen|{row.get('chapter',3)}|{lecture}")])
    kb.append([back_menu()]); await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


def parse_deadline(text, kind):
    match=re.search(r"#موعد\s+(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s+(\d{1,2}):(\d{2})",text or "")
    if match:
        d,m,y,h,minute=map(int,match.groups()); return datetime(y,m,d,h,minute,tzinfo=TIMEZONE)
    hours=DEFAULT_HOMEWORK_HOURS if kind=="homework" else DEFAULT_EXAM_HOURS
    return datetime.now(TIMEZONE)+timedelta(hours=hours)


def message_payload(message):
    if message.document: return "document",message.document.file_id,message.caption or ""
    if message.photo: return "photo",message.photo[-1].file_id,message.caption or ""
    if message.video: return "video",message.video.file_id,message.caption or ""
    return "text",None,message.text or ""


async def capture_group_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    actor_id=update.effective_user.id if update.effective_user else 0
    chat_id=update.effective_chat.id
    anonymous_admin=bool(msg and msg.sender_chat and msg.sender_chat.id==chat_id)
    if not msg or not (is_admin(actor_id) or anonymous_admin): return
    if msg.reply_to_message and await submission_by_review(chat_id,msg.reply_to_message.message_id): return
    thread=msg.message_thread_id or 0
    if chat_id==BIOLOGY_GROUP_ID and thread==HOMEWORK_TOPIC_ID: kind="homework"
    elif chat_id==BIOLOGY_GROUP_ID and thread==EXAM_TOPIC_ID: kind="exam"
    else: return
    payload_type,file_id,content=message_payload(msg)
    if msg.media_group_id and not content:
        await append_pending_media(msg.media_group_id,payload_type,file_id,msg.message_id); return
    first_line=(content.strip().splitlines() or ["الواجب الجديد" if kind=="homework" else "الامتحان الجديد"])[0]
    title=re.sub(r"#موعد.*$","",first_line).strip() or ("واجب الأحياء" if kind=="homework" else "امتحان الأحياء")
    if kind=="exam" and ("#تراكمي" in (content or "") or "امتحان تراكمي" in title) and not title.startswith("[تراكمي]"):
        title="[تراكمي] "+title.replace("#تراكمي","").strip()
    pending=await create_pending_task(kind,title,msg.chat_id,thread,msg.message_id,payload_type,file_id,msg.media_group_id,content,actor_id)
    noun="الواجب" if kind=="homework" else "الامتحان"
    kb=InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ نعم، ربط {noun}",callback_data=f"bind|{pending['id']}"),
        InlineKeyboardButton("❌ لا",callback_data=f"cancelbind|{pending['id']}")
    ]])
    try: await msg.reply_text(bold(f"هل تريد ربط {noun} بالبوت وإرساله للطلاب؟"),parse_mode=ParseMode.HTML,reply_markup=kb)
    except TelegramError: pass


async def send_task_content(bot,chat_id,task):
    school_text=(f"\n\n{task.get('text_content')}" if task.get('school_review_id') and
        task.get('text_content') and task.get('text_content')!=task.get('title') else '')
    caption=bold(f"{task['title']}" + (f"\n🎬 المحاضرات الداخلة: {task.get('linked_lectures')}" if task.get('linked_lectures') else "") +
        school_text + f"\n\n⏰ آخر موعد: {task['deadline'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}")
    media=await get_task_media(task["id"])
    if media:
        for index,item in enumerate(media):
            cap=caption if index==0 else None
            if item["payload_type"]=="photo": await bot.send_photo(chat_id,item["file_id"],caption=cap,parse_mode=ParseMode.HTML if cap else None,protect_content=True)
            elif item["payload_type"]=="document": await bot.send_document(chat_id,item["file_id"],caption=cap,parse_mode=ParseMode.HTML if cap else None,protect_content=True)
            elif item["payload_type"]=="video": await bot.send_video(chat_id,item["file_id"],caption=cap,parse_mode=ParseMode.HTML if cap else None,protect_content=True)
    else: await bot.send_message(chat_id,bold(task["text_content"] or task["title"]),parse_mode=ParseMode.HTML,protect_content=True)


def scope_label(scope):
    if scope=="course": return "مسار الدورة"
    if scope=="all": return "جميع المسارات"
    if scope and scope.startswith("chapter_"): return f"الفصل {scope.split('_')[1]}"
    return "المسار الدراسي"


async def notify_task_assignment(bot,task):
    noun="الواجب" if task["kind"]=="homework" else "الامتحان"
    label=scope_label(task.get("target_scope"))
    deadline=task["deadline"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
    for student in await assigned_students(task["id"]):
        lecture_line=f"\n🎬 المحاضرات الداخلة: {task.get('linked_lectures')}" if task.get('linked_lectures') else ""
        try: await bot.send_message(student["user_id"],bold(f"🔔 نزل {noun} جديد\n📌 {task['title'].replace('[تراكمي ','').replace('] ','')}\n📚 {label}{lecture_line}\n⏰ آخر موعد: {deadline}\n\nافتح قسم {('الواجبات' if task['kind']=='homework' else 'الامتحانات')} داخل البوت."),parse_mode=ParseMode.HTML)
        except TelegramError: pass
        for parent in await student_parents(student["user_id"],True):
            try: await bot.send_message(parent["parent_chat_id"],bold(f"🔔 نزل {noun} جديد للطالب {student['full_name']}\n📌 {task['title'].replace('[تراكمي] ','')}\n📚 {label}\n⏰ آخر موعد: {deadline}"),parse_mode=ParseMode.HTML)
            except TelegramError: pass


async def show_tasks(query,kind,category=None):
    admin=is_admin(query.from_user.id); student_id=None if admin else query.from_user.id
    rows=await (open_exam_tasks(category=="cumulative",student_id) if kind=="exam" and category else open_tasks(kind,student_id))
    if not is_admin(query.from_user.id):
        visible=[]; now=datetime.now(TIMEZONE)
        for row in rows:
            effective=await effective_task_deadline(row["id"],query.from_user.id)
            if effective and (now<effective["deadline"] or (row["kind"]=="exam" and row.get("closed") and not effective.get("submitted"))): visible.append(row)
        rows=visible
    if not rows and kind=="exam" and not is_admin(query.from_user.id):
        # Explain an incomplete linked exam instead of making the student think the bot is broken.
        waiting=[]
        for definition in await linked_exam_definitions():
            status=await linked_exam_definition_status(definition["id"],query.from_user.id)
            expected_type='cumulative' if category=='cumulative' else 'normal'
            actual_type=definition.get('exam_type') or ('cumulative' if definition.get('title','').startswith('[تراكمي]') else 'normal')
            if actual_type!=expected_type:
                continue
            if status and status.get("missing"):
                missing=" + ".join(f"ف{x['chapter']}/م{x['lecture']}" for x in status["missing"])
                waiting.append(f"📝 {definition['title']}\n⏳ المتبقي: {missing}")
        text="📭 لا توجد عناصر مفتوحة حالياً."
        if waiting: text+="\n\n⚠️ توجد امتحانات لم تستوفِ محتواها بعد:\n\n"+"\n\n".join(waiting)
        await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if not rows:
        await query.edit_message_text(bold("📭 لا توجد عناصر مفتوحة حالياً."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    kb=[]
    for row in rows: kb.append([InlineKeyboardButton(f"{'📚' if kind=='homework' else '📝'} {row['title']}",callback_data=f"task|{row['id']}")])
    kb.append([back_menu()]); await query.edit_message_text(bold("اختر العنصر المطلوب:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def show_task(query,context,task_id):
    task=await get_task(task_id)
    if not task: await query.answer("العنصر غير موجود",show_alert=True); return
    effective=None
    if not is_admin(query.from_user.id):
        effective=await effective_task_deadline(task_id,query.from_user.id)
        late_exam = task["kind"]=="exam" and task.get("closed") and not effective.get("submitted")
        if (not effective or not effective.get("assigned") or (datetime.now(TIMEZONE)>=effective["deadline"] and not late_exam)):
            await query.answer("⏰ انتهى وقت هذا الامتحان أو الواجب.",show_alert=True); return
    if task["kind"]=="exam" and task.get("exam_approval_required") and not is_admin(query.from_user.id):
        access=await exam_access(task_id,query.from_user.id)
        if not access or access["status"]!="approved":
            student=await get_student(query.from_user.id)
            access=await request_exam_access(task_id,query.from_user.id)
            if student and student.get("parent_chat_id"):
                kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ الطالب موجود ومستعد",callback_data=f"examallow|{task_id}|{student['user_id']}"),InlineKeyboardButton("❌ غير مستعد",callback_data=f"examdeny|{task_id}|{student['user_id']}")]])
                try: await context.bot.send_message(student["parent_chat_id"],bold(f"📝 تفعيل امتحان\nالطالب {student['full_name']} يريد فتح: {task['title']}\nهل الطالب موجود الآن ومستعد للامتحان أمام أنظاركم؟"),parse_mode=ParseMode.HTML,reply_markup=kb)
                except TelegramError: pass
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("👨‍🏫 طلب التفعيل من الأدمن",callback_data=f"requestadminexam|{task_id}")],[back_menu()]])
            await context.bot.send_message(query.from_user.id,bold("🔒 أرسلنا طلب التفعيل إلى ولي أمرك. إذا احتجت تدخل الإدارة اضغط زر «طلب التفعيل من الأدمن»."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    await send_task_content(context.bot,query.from_user.id,task)
    noun="الواجب" if task["kind"]=="homework" else "الامتحان"
    rows=[]
    if is_admin(query.from_user.id): rows.append([InlineKeyboardButton(f"🗑 حذف {noun}",callback_data=f"deletetask|{task_id}")])
    else:
        rows.append([InlineKeyboardButton(f"📤 إرسال {noun}",callback_data=f"submit|{task_id}"),InlineKeyboardButton("🔄 تغيير إجاباتي",callback_data=f"retrysubmission|{task_id}")])
        if task["kind"]=="exam" and not (effective and effective.get("submitted")): rows.append([InlineKeyboardButton("⏳ طلب تمديد وقت الامتحان",callback_data=f"extend|{task_id}")])
    rows.extend([[InlineKeyboardButton("💬 فتح حساب الأستاذ",url=f"https://t.me/{OWNER_USERNAME}")],[back_menu()]])
    kb=InlineKeyboardMarkup(rows)
    await context.bot.send_message(query.from_user.id,bold(f"اختر «إرسال {noun} عبر البوت» حتى يُسجل تسليمك وتحصل على XP."),parse_mode=ParseMode.HTML,reply_markup=kb)


async def notify_student_and_parent(bot,student,text):
    parent_ids=[p["parent_chat_id"] for p in await student_parents(student["user_id"],True)]
    for chat_id in dict.fromkeys([student["user_id"],*parent_ids]):
        if not chat_id: continue
        try: await bot.send_message(chat_id,bold(text),parse_mode=ParseMode.HTML)
        except TelegramError: pass


async def ensure_student_topic(bot,student,chat_id):
    thread_id=await student_topic(student["user_id"],chat_id)
    if thread_id: return thread_id
    name=f"{student['full_name']} | {student['user_id']}"[:128]
    try:
        topic=await bot.create_forum_topic(chat_id,name)
        await save_student_topic(student["user_id"],chat_id,topic.message_thread_id)
        return topic.message_thread_id
    except TelegramError as exc:
        logger.warning("Could not create student topic in %s: %s",chat_id,exc); return None


async def receive_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_id=context.user_data.get("waiting_submission")
    if not task_id: return False
    student=await get_student(update.effective_user.id)
    if not student or not student["approved"] or not student.get("parent_chat_id") or not await is_channel_member(context.bot,update.effective_user.id):
        context.user_data.pop("waiting_submission",None); await update.effective_message.reply_text(bold("🔒 لا يمكنك التسليم: يجب تفعيل الحساب وربط ولي الأمر والاشتراك بالقناة."),parse_mode=ParseMode.HTML); return True
    task=await get_task(task_id)
    effective=await effective_task_deadline(task_id,update.effective_user.id) if task else None
    if not task or not effective or not effective.get("assigned") or (task["closed"] and datetime.now(TIMEZONE)>=effective["deadline"]) or datetime.now(TIMEZONE)>=effective["deadline"]:
        context.user_data.pop("waiting_submission",None); await update.effective_message.reply_text(bold("⏰ انتهى وقت التسليم."),parse_mode=ParseMode.HTML); return True
    msg=update.message; media=msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not media:
        await msg.reply_text(bold("⚠️ أرسل صورة أو ملف PDF أو فيديو كحل."),parse_mode=ParseMode.HTML); return True
    album_state=context.user_data.get("submission_album")
    same_album=bool(msg.media_group_id and album_state and album_state.get("id")==msg.media_group_id and album_state.get("task_id")==task_id)
    destination=(HOMEWORK_SUBMISSIONS_CHAT_ID if task["kind"]=="homework" else EXAM_SUBMISSIONS_CHAT_ID) or OWNER_CHAT_ID
    if not destination:
        await msg.reply_text(bold("⚠️ تعذر التسليم لأن وجهة تصحيح الحلول غير مضبوطة. تواصل مع الإدارة."),parse_mode=ParseMode.HTML); return True
    thread_id=await ensure_student_topic(context.bot,student,destination)
    if thread_id is None and destination==BIOLOGY_GROUP_ID:
        await msg.reply_text(bold("⚠️ تعذر إنشاء ملف خاص لتسليمك. لم يُسجل الحل، حاول لاحقاً أو تواصل مع الإدارة."),parse_mode=ParseMode.HTML); return True
    initial_caption=bold(f"📥 {task['title']}\n👤 الطالب: {update.effective_user.full_name}\n🆔 {update.effective_user.id}")
    try:
        if msg.photo: sent=await context.bot.send_photo(destination,msg.photo[-1].file_id,caption=initial_caption,parse_mode=ParseMode.HTML,message_thread_id=thread_id)
        elif msg.document: sent=await context.bot.send_document(destination,msg.document.file_id,caption=initial_caption,parse_mode=ParseMode.HTML,message_thread_id=thread_id)
        else: sent=await context.bot.send_video(destination,msg.video.file_id,caption=initial_caption,parse_mode=ParseMode.HTML,message_thread_id=thread_id)
    except TelegramError as exc:
        logger.warning("Submission delivery failed before recording: %s",exc)
        await msg.reply_text(bold("⚠️ لم يصل الحل إلى الأستاذ ولم يُسجل. أعد المحاولة بعد قليل."),parse_mode=ParseMode.HTML); return True
    try:
        result=await record_submission(task_id,update.effective_user.id,msg.message_id,media.file_unique_id,msg.media_group_id)
    except Exception as exc:
        logger.exception("Submission database recording failed: %s",exc)
        try: await context.bot.delete_message(destination,sent.message_id)
        except TelegramError: pass
        await msg.reply_text(bold("⚠️ لم يكتمل تسجيل الحل في قاعدة البيانات، لذلك لم يُحتسب. أعد المحاولة."),parse_mode=ParseMode.HTML); return True
    if result in ("expired","not_allowed"):
        try: await context.bot.delete_message(destination,sent.message_id)
        except TelegramError: pass
        context.user_data.pop("waiting_submission",None)
        await msg.reply_text(bold("⏰ انتهى وقت التسليم أثناء رفع الحل؛ لم يُسجل. اطلب تمديداً ثم أعد الإرسال." if result=="expired" else "🔒 تغيّرت صلاحية هذه المهمة؛ لم يُسجل الحل. افتح المهمة من جديد."),parse_mode=ParseMode.HTML)
        return True
    if result=="duplicate":
        try: await context.bot.delete_message(destination,sent.message_id)
        except TelegramError: pass
        await msg.reply_text(bold("🚫 هذا الملف مطابق لتسليم طالب آخر ولم يُحتسب."),parse_mode=ParseMode.HTML); return True
    if result=="exam_locked":
        try: await context.bot.delete_message(destination,sent.message_id)
        except TelegramError: pass
        noun="الواجب" if task["kind"]=="homework" else "الامتحان"
        context.user_data.pop("waiting_submission",None); await msg.reply_text(bold(f"🔒 سبق أن سلّمت {noun}. اضغط «حذف إجاباتي وإعادة الإرسال» إذا أردت استبدالها."),parse_mode=ParseMode.HTML); return True
    number="الأول" if result=="added" else ("تكملة الصور" if result=="album_part" else "المحدّث")
    caption=bold(f"📥 {task['title']} — التسليم {number}\n📚 المسار: {scope_label(task.get('target_scope'))}\n👤 الطالب: {update.effective_user.full_name}\n🆔 {update.effective_user.id}")
    try:
        await context.bot.edit_message_caption(destination,sent.message_id,caption=caption,parse_mode=ParseMode.HTML)
    except TelegramError:
        pass
    await add_submission_review_message(destination,sent.message_id,task_id,update.effective_user.id)
    if result=="added" and student.get("parent_chat_id"):
        noun="الواجب" if task["kind"]=="homework" else "الامتحان"
        try: await context.bot.send_message(student["parent_chat_id"],bold(f"✅ تم تسجيل تسليم {noun}\n👤 الطالب: {student['full_name']}\n📌 {task['title']}\n🕐 التسليم ضمن الوقت المحدد."),parse_mode=ParseMode.HTML)
        except TelegramError: pass
    if msg.media_group_id:
        token=time.monotonic()
        if not same_album:
            context.user_data["submission_album"]={"id":msg.media_group_id,"task_id":task_id,"added":result=="added","token":token}
        else:
            context.user_data["submission_album"]["token"]=token
        # Debounce finalization: every arriving album part renews the timer, so
        # slow Telegram delivery cannot cut off the final photos.
        context.job_queue.run_once(finalize_album_submission,8,data={"user_id":update.effective_user.id,"media_group_id":msg.media_group_id,"token":token})
    else:
        context.user_data.pop("waiting_submission",None)
        confirm_kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ لا، إجاباتي صحيحة",callback_data=f"submissionok|{task_id}"),InlineKeyboardButton("🔄 نعم، أريد تغييرها",callback_data=f"retrysubmission|{task_id}")],
            [InlineKeyboardButton("🎬 فتح المحاضرات التالية",callback_data="today_prep")]])
        await msg.reply_text(bold(f"✅ تم تسجيل وإرسال الحل للأستاذ بنجاح.\n⭐ {'أضيفت نقاط المهمة إلى حسابك.' if result=='added' else 'تم استبدال تسليمك السابق.'}\n\nهل تود تغيير الإجابات لوجود خطأ في الصورة؟\nيمكنك تغييرها مرتين فقط لكل امتحان أو واجب.\n\n📚 إذا كان هذا الامتحان حاجزًا أمام المحاضرات التالية، فسيُرفع الحظر تلقائيًا بعد تسجيل التسليم."),parse_mode=ParseMode.HTML,reply_markup=confirm_kb)
    return True


async def receive_exam_correction(update: Update,context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    if not msg or not is_admin(update.effective_user.id) or not msg.reply_to_message: return
    ref=await submission_by_review(update.effective_chat.id,msg.reply_to_message.message_id)
    if not ref: return
    payload_type,file_id,content=message_payload(msg)
    if not file_id: return
    token=str(msg.message_id)
    pending=context.chat_data.setdefault("pending_grade_files",{})
    pending[token]={"ref":ref,"payload_type":payload_type,"file_id":file_id,"content":content,"thread_id":msg.message_thread_id or 0}
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم، اعتماد كدرجة",callback_data=f"gradefile|yes|{token}"),InlineKeyboardButton("💬 لا، إرسال عادي",callback_data=f"gradefile|no|{token}")]])
    await msg.reply_text(bold("هل تريد اعتماد هذا الملف أو الصورة كتصحيح ودرجة؟"),parse_mode=ParseMode.HTML,reply_markup=kb)
    raise ApplicationHandlerStop


async def receive_grade_value(update: Update,context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    state=context.chat_data.get("awaiting_grade_value")
    if not msg or not state or not is_admin(update.effective_user.id): return
    if (msg.message_thread_id or 0)!=state["thread_id"]: return
    raw=(msg.text or "").strip()
    if not raw.isdigit() or not 0<=int(raw)<=100:
        await msg.reply_text(bold("⚠️ أرسل الدرجة رقماً من 0 إلى 100."),parse_mode=ParseMode.HTML); raise ApplicationHandlerStop
    grade=int(raw); ref=state["ref"]
    saved=await set_submission_grade(ref["task_id"],ref["user_id"],grade,update.effective_user.id)
    if not saved:
        await msg.reply_text(bold("⚠️ تعذر حفظ الدرجة."),parse_mode=ParseMode.HTML); raise ApplicationHandlerStop
    if state.get("file_id"):
        await save_exam_correction(ref["task_id"],ref["user_id"],state["payload_type"],state["file_id"],grade,update.effective_user.id)
    if ref["kind"]=="exam" and grade<60:
        count=await add_warning(ref["user_id"],f"رسوب في {ref['title']} بدرجة {grade}",update.effective_user.id,ref["task_id"])
        await notify_student_and_parent(context.bot,ref,f"⚠️ إنذار رسوب ({count}/{MAX_WARNINGS})\nالدرجة: {grade}/100 في {ref['title']}")
    caption=bold(f"📄 تصحيح {'الامتحان' if ref['kind']=='exam' else 'الواجب'}\n👤 الطالب: {ref['full_name']}\n📌 {ref['title']}\n📊 الدرجة: {grade}/100")
    delivered=[]
    for chat_id in (ref["user_id"],ref.get("parent_chat_id")):
        if not chat_id: continue
        try:
            if not state.get("file_id"): sent=await context.bot.send_message(chat_id,caption,parse_mode=ParseMode.HTML)
            elif state["payload_type"]=="photo": sent=await context.bot.send_photo(chat_id,state["file_id"],caption=caption,parse_mode=ParseMode.HTML)
            elif state["payload_type"]=="document": sent=await context.bot.send_document(chat_id,state["file_id"],caption=caption,parse_mode=ParseMode.HTML)
            else: sent=await context.bot.send_video(chat_id,state["file_id"],caption=caption,parse_mode=ParseMode.HTML)
            await save_communication_route(chat_id,sent.message_id,ref["user_id"],update.effective_chat.id,msg.message_thread_id or 0,"student" if chat_id==ref["user_id"] else "parent")
            delivered.append(chat_id)
        except TelegramError: pass
    context.chat_data.pop("awaiting_grade_value",None)
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ تغيير الدرجة",callback_data=f"changegrade|{ref['task_id']}|{ref['user_id']}")]])
    await msg.reply_text(bold(f"✅ حُفظت الدرجة {grade}/100 وأُرسلت للطالب وولي الأمر."),parse_mode=ParseMode.HTML,reply_markup=kb)
    if ref["kind"]=="exam": await announce_champions(context,ref["task_id"])
    raise ApplicationHandlerStop


async def send_message_payload(bot,chat_id,msg,caption_text,thread_id=None):
    payload_type,file_id,_=message_payload(msg)
    formatted=bold(caption_text) if caption_text else None
    if payload_type=="photo": return await bot.send_photo(chat_id,file_id,caption=formatted,parse_mode=ParseMode.HTML if formatted else None,message_thread_id=thread_id)
    if payload_type=="document": return await bot.send_document(chat_id,file_id,caption=formatted,parse_mode=ParseMode.HTML if formatted else None,message_thread_id=thread_id)
    if payload_type=="video": return await bot.send_video(chat_id,file_id,caption=formatted,parse_mode=ParseMode.HTML if formatted else None,message_thread_id=thread_id)
    return await bot.send_message(chat_id,bold(caption_text or "رسالة جديدة"),parse_mode=ParseMode.HTML,message_thread_id=thread_id)


async def receive_admin_communication(update: Update,context: ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    if not msg or not is_admin(update.effective_user.id) or not msg.reply_to_message: return
    ref=await submission_by_review(update.effective_chat.id,msg.reply_to_message.message_id)
    route=None if ref else await communication_route(update.effective_chat.id,msg.reply_to_message.message_id)
    if not ref and not route: return
    student_id=ref["user_id"] if ref else route["student_id"]
    student=await get_student(student_id)
    if not student: return
    _,_,content=message_payload(msg); content=(content or "").strip()
    mode="student"
    if content.startswith("#ولي"): mode="parent"; content=content[4:].strip()
    elif content.startswith("#الكل"): mode="both"; content=content[5:].strip()
    prefix="💬 رسالة من إدارة دورة الأحياء"
    recipients=[]
    if mode in ("student","both"): recipients.append((student["user_id"],"student"))
    if mode in ("parent","both") and student.get("parent_chat_id"): recipients.append((student["parent_chat_id"],"parent"))
    if not recipients:
        await msg.reply_text(bold("⚠️ لا يوجد حساب ولي أمر مربوط بهذا الطالب."),parse_mode=ParseMode.HTML); raise ApplicationHandlerStop
    for chat_id,role in recipients:
        try:
            sent=await send_message_payload(context.bot,chat_id,msg,f"{prefix}\n\n{content}" if content else prefix)
            await save_communication_route(chat_id,sent.message_id,student_id,update.effective_chat.id,msg.message_thread_id or 0,role)
        except TelegramError: pass
    await msg.reply_text(bold("✅ تم إرسال الرسالة بنجاح."),parse_mode=ParseMode.HTML)
    raise ApplicationHandlerStop


async def direct_message_command(update: Update,context: ContextTypes.DEFAULT_TYPE,role):
    if not is_admin(update.effective_user.id): return
    if len(context.args)<2:
        await update.effective_message.reply_text(bold(f"الاستخدام: /{'msg_parent' if role=='parent' else 'msg_student'} ID نص الرسالة"),parse_mode=ParseMode.HTML); return
    try: student_id=int(context.args[0])
    except ValueError: return
    student=await get_student(student_id)
    if not student:
        await update.effective_message.reply_text(bold("⚠️ الطالب غير موجود."),parse_mode=ParseMode.HTML); return
    chat_id=student.get("parent_chat_id") if role=="parent" else student["user_id"]
    if not chat_id:
        await update.effective_message.reply_text(bold("⚠️ ولي الأمر غير مربوط."),parse_mode=ParseMode.HTML); return
    text_value=" ".join(context.args[1:])
    try:
        sent=await context.bot.send_message(chat_id,bold(f"💬 رسالة من إدارة دورة الأحياء\n\n{text_value}"),parse_mode=ParseMode.HTML)
        await save_communication_route(chat_id,sent.message_id,student_id,update.effective_chat.id,update.effective_message.message_thread_id or 0,role)
        await update.effective_message.reply_text(bold("✅ تم إرسال الرسالة، ويمكنه الرد عليها مباشرة."),parse_mode=ParseMode.HTML)
    except TelegramError:
        await update.effective_message.reply_text(bold("⚠️ تعذر الإرسال؛ يجب أن يكون الحساب قد فتح البوت سابقاً."),parse_mode=ParseMode.HTML)


async def msg_student_command(update: Update,context: ContextTypes.DEFAULT_TYPE): await direct_message_command(update,context,"student")
async def msg_parent_command(update: Update,context: ContextTypes.DEFAULT_TYPE): await direct_message_command(update,context,"parent")


async def announce_champions(context,task_id):
    winners=await exam_champions_if_ready(task_id)
    if not winners:
        return
    names="\n".join(f"👤 {winner['full_name']} — {winner['grade']}/100" for winner in winners)
    text=f"🏆 بطل الامتحان\n\n📝 {winners[0]['title']}\n{names}\n\nمبارك هذا الإنجاز المتميز!"
    try:
        await context.bot.send_message(BIOLOGY_GROUP_ID,bold(text),parse_mode=ParseMode.HTML,message_thread_id=CHAMPIONS_TOPIC_ID or None)
    except TelegramError as exc:
        logger.warning("Champion announcement failed for task %s and will retry: %s",task_id,exc)
        return
    await mark_champion_announced(task_id)
    for winner in winners:
        if winner.get("parent_chat_id"):
            try: await context.bot.send_message(winner["parent_chat_id"],bold(f"🌟 نبارك لكم! حصل الطالب {winner['full_name']} على أعلى درجة في {winner['title']}: {winner['grade']}/100."),parse_mode=ParseMode.HTML)
            except TelegramError: pass


async def finalize_album_submission(context: ContextTypes.DEFAULT_TYPE):
    uid=context.job.data["user_id"]; user_data=context.application.user_data.get(uid,{})
    state=user_data.get("submission_album")
    if not state or state.get("id")!=context.job.data["media_group_id"] or state.get("token")!=context.job.data.get("token"): return
    user_data.pop("submission_album",None); user_data.pop("waiting_submission",None)
    task_id=state["task_id"]
    confirm_kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ لا، إجاباتي صحيحة",callback_data=f"submissionok|{task_id}"),InlineKeyboardButton("🔄 نعم، أريد تغييرها",callback_data=f"retrysubmission|{task_id}")],
        [InlineKeyboardButton("🎬 فتح المحاضرات التالية",callback_data="today_prep")]])
    try: await context.bot.send_message(uid,bold(f"✅ تم تسجيل وإرسال جميع صور الحل للأستاذ بنجاح.\n⭐ {'أضيفت نقاط المهمة إلى حسابك.' if state.get('added') else 'تم استبدال تسليمك السابق.'}\n\nهل تود تغيير الإجابات لوجود خطأ في إحدى الصور؟\nيمكنك تغييرها مرتين فقط لكل امتحان أو واجب."),parse_mode=ParseMode.HTML,reply_markup=confirm_kb)
    except TelegramError: pass


async def receive_archive_media(update: Update,context: ContextTypes.DEFAULT_TYPE):
    meta=context.user_data.get("archive_awaiting")
    if meta and meta.get("step")=="lecture":
        try:
            lecture=int((update.message.text or "").strip())
            if lecture<1 or lecture>len(PLAYLISTS[meta["chapter"]]): raise ValueError
        except ValueError:
            await update.effective_message.reply_text(bold("⚠️ أرسل رقم محاضرة صحيحاً."),parse_mode=ParseMode.HTML); return True
        meta["lecture"]=lecture; meta["step"]="title"
        await update.effective_message.reply_text(bold("✍️ الآن أرسل اسم الامتحان."),parse_mode=ParseMode.HTML); return True
    if meta and meta.get("step")=="title":
        title=(update.message.text or "").strip()
        if not title:
            await update.effective_message.reply_text(bold("⚠️ أرسل اسم الامتحان أولاً."),parse_mode=ParseMode.HTML); return True
        archive=await create_archive_exam(meta["chapter"],title,update.effective_user.id,meta["lecture"])
        context.user_data.pop("archive_awaiting",None); context.user_data["waiting_archive_id"]=archive["id"]
        await update.effective_message.reply_text(bold("✅ تم تسجيل الاسم. أرسل الآن صور الامتحان أو ملف PDF، ثم اضغط إنهاء وحفظ."),parse_mode=ParseMode.HTML); return True
    archive_id=context.user_data.get("waiting_archive_id")
    if not archive_id: return False
    msg=update.message
    payload_type,file_id,_=message_payload(msg)
    if not file_id:
        await msg.reply_text(bold("⚠️ أرسل صوراً أو ملف PDF للامتحان."),parse_mode=ParseMode.HTML); return True
    await add_archive_exam_media(archive_id,payload_type,file_id)
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ إنهاء وحفظ الامتحان",callback_data=f"finisharchive|{archive_id}")]])
    await msg.reply_text(bold("✅ تمت إضافة الملف. يمكنك إرسال ملفات أخرى أو الضغط على إنهاء."),parse_mode=ParseMode.HTML,reply_markup=kb)
    return True


RESOURCE_LABELS={"booklet":"الملازم","summary":"الملخصات","model_answer":"الإجابات النموذجية","ministerial":"الأسئلة الوزارية"}


async def receive_linked_exam(update: Update,context: ContextTypes.DEFAULT_TYPE):
    state=context.user_data.get("linked_exam")
    if not state or not is_admin(update.effective_user.id):
        return False
    msg=update.message
    if state["step"]=="title":
        title=(msg.text or "").strip()
        if not title:
            await msg.reply_text(bold("⚠️ أرسل اسم الامتحان كنص."),parse_mode=ParseMode.HTML); return True
        state["title"]=("[تراكمي] " if state.get("cumulative") and not title.startswith("[تراكمي]") else "")+title; state["step"]="media"; state["media"]=[]
        await msg.reply_text(bold("📎 أرسل أسئلة الامتحان الآن: صور أو PDF أو فيديو.\nيمكنك إرسال أكثر من صورة، ثم اضغط «إنهاء»."),parse_mode=ParseMode.HTML); return True
    payload_type,file_id,_=message_payload(msg)
    if not file_id:
        await msg.reply_text(bold("⚠️ أرسل صورة أو ملف PDF أو فيديو."),parse_mode=ParseMode.HTML); return True
    item=(payload_type,file_id)
    state.setdefault("media",[])
    if item not in state["media"]: state["media"].append(item)
    await msg.reply_text(bold(f"✅ تمت إضافة الملف رقم {len(state['media'])}.\nأرسل بقية الملفات أو اضغط «إنهاء»."),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ إنهاء وإضافة الامتحان",callback_data="linkedexamfinish")],[InlineKeyboardButton("❌ إلغاء",callback_data="linkedexamcancel")]]))
    return True


async def receive_manual_task(update: Update,context: ContextTypes.DEFAULT_TYPE):
    state=context.user_data.get("manual_task")
    if not state or not is_admin(update.effective_user.id): return False
    msg=update.message
    if state["step"]=="title":
        title=(msg.text or "").strip()
        if not title:
            await msg.reply_text(bold("⚠️ أرسل اسم المنشور كنص أولاً."),parse_mode=ParseMode.HTML); return True
        state["title"]=title; state["step"]="media"; state["media"]=[]
        await msg.reply_text(bold("📎 أرسل الآن صور المنشور أو ملفات PDF.\nيمكنك إرسال صورة واحدة أو صور متعددة، وعند الانتهاء اضغط «إنهاء إضافة الملفات»."),parse_mode=ParseMode.HTML); return True
    payload_type,file_id,_=message_payload(msg)
    if not file_id:
        await msg.reply_text(bold("⚠️ أرسل صورة أو ملف PDF أو فيديو."),parse_mode=ParseMode.HTML); return True
    item=(payload_type,file_id)
    state.setdefault("media",[])
    if item not in state["media"]: state["media"].append(item)
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ إنهاء إضافة الملفات",callback_data="manualmediafinish")],[InlineKeyboardButton("❌ إلغاء العملية",callback_data="manualcancel")]])
    await msg.reply_text(bold(f"✅ تمت إضافة الملف رقم {len(state['media'])}.\nأرسل ملفات أخرى أو اضغط «إنهاء إضافة الملفات»."),parse_mode=ParseMode.HTML,reply_markup=kb); return True


async def publish_manual_now(bot,state,admin_id):
    manual_kind=state["kind"]; kind="homework" if manual_kind=="homework" else "exam"
    if kind=="exam" and state.get("target_scope")!="course": state["target_scope"]="course"
    title=("[تراكمي] " if manual_kind=="cumulative" else "")+state["title"]
    thread=HOMEWORK_TOPIC_ID if kind=="homework" else EXAM_TOPIC_ID
    caption=bold(f"{'📚' if kind=='homework' else '🏆' if manual_kind=='cumulative' else '📝'} {state['title']}")
    sent_items=[]
    scope=state.get("target_scope","course")
    if scope!="course":
        synthetic_id=-int(time.time()*1000)
        first_type,first_file=state["media"][0]
        pending=await create_pending_task(kind,title,OWNER_CHAT_ID or admin_id,0,synthetic_id,first_type,first_file,None,state["title"],admin_id)
        await set_pending_task_scope(pending["id"],scope)
        for index,(payload_type,file_id) in enumerate(state["media"][1:],1): await add_pending_media_by_id(pending["id"],payload_type,file_id,synthetic_id-index)
        return pending
    try:
        for index,(payload_type,file_id) in enumerate(state["media"]):
            item_caption=caption if index==0 else None
            if payload_type=="photo": sent=await bot.send_photo(BIOLOGY_GROUP_ID,file_id,caption=item_caption,parse_mode=ParseMode.HTML if item_caption else None,message_thread_id=thread or None)
            elif payload_type=="document": sent=await bot.send_document(BIOLOGY_GROUP_ID,file_id,caption=item_caption,parse_mode=ParseMode.HTML if item_caption else None,message_thread_id=thread or None)
            else: sent=await bot.send_video(BIOLOGY_GROUP_ID,file_id,caption=item_caption,parse_mode=ParseMode.HTML if item_caption else None,message_thread_id=thread or None)
            sent_items.append((payload_type,file_id,sent.message_id))
    except TelegramError: return None
    first_type,first_file,first_message=sent_items[0]
    pending=await create_pending_task(kind,title,BIOLOGY_GROUP_ID,thread,first_message,first_type,first_file,None,state["title"],admin_id)
    await set_pending_task_scope(pending["id"],scope)
    for payload_type,file_id,message_id in sent_items[1:]: await add_pending_media_by_id(pending["id"],payload_type,file_id,message_id)
    return pending


async def receive_resource_input(update: Update,context: ContextTypes.DEFAULT_TYPE):
    meta=context.user_data.get("resource_awaiting_title")
    if meta:
        title=(update.message.text or "").strip()
        if not title:
            await update.effective_message.reply_text(bold("⚠️ أرسل اسم الملف أولاً."),parse_mode=ParseMode.HTML); return True
        resource=await create_resource(meta["category"],meta["chapter"],title,update.effective_user.id)
        context.user_data.pop("resource_awaiting_title",None); context.user_data["waiting_resource_id"]=resource["id"]
        await update.effective_message.reply_text(bold("✅ تم تسجيل الاسم. أرسل الآن الصور أو ملف PDF، وبعدها اضغط إنهاء وحفظ."),parse_mode=ParseMode.HTML); return True
    resource_id=context.user_data.get("waiting_resource_id")
    if not resource_id: return False
    payload_type,file_id,_=message_payload(update.message)
    if not file_id:
        await update.effective_message.reply_text(bold("⚠️ أرسل صورة أو ملف PDF."),parse_mode=ParseMode.HTML); return True
    await add_resource_media(resource_id,payload_type,file_id)
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ إنهاء وحفظ",callback_data=f"finishresource|{resource_id}")]])
    await update.effective_message.reply_text(bold("✅ تمت إضافة الملف. أرسل ملفات أخرى أو اضغط إنهاء وحفظ."),parse_mode=ParseMode.HTML,reply_markup=kb); return True


async def receive_private_reply(update: Update,context: ContextTypes.DEFAULT_TYPE):
    msg=update.message
    if not msg or not msg.reply_to_message: return False
    route=await communication_route(update.effective_chat.id,msg.reply_to_message.message_id)
    if not route: return False
    student=await get_student(route["student_id"])
    if not student: return False
    _,_,content=message_payload(msg)
    sender="ولي أمر الطالب" if route["role"]=="parent" else "الطالب"
    caption=f"📩 رد من {sender}\n👤 {student['full_name']}\n🆔 {student['user_id']}"
    if content: caption+=f"\n\n{content}"
    try:
        sent=await send_message_payload(context.bot,route["reply_group_id"],msg,caption,route["reply_thread_id"] or None)
        await save_communication_route(route["reply_group_id"],sent.message_id,student["user_id"],route["reply_group_id"],route["reply_thread_id"],route["role"])
        await msg.reply_text(bold("✅ وصل ردك إلى إدارة الدورة."),parse_mode=ParseMode.HTML)
    except TelegramError:
        await msg.reply_text(bold("⚠️ تعذر إرسال الرد حالياً، حاول لاحقاً."),parse_mode=ParseMode.HTML)
    return True


async def release_exam_questions(context,task):
    if task.get("target_scope")!="course":
        delivered=False
        for student in await assigned_students(task["id"]):
            try:
                await send_task_content(context.bot,student["user_id"],task)
                await context.bot.send_message(student["user_id"],bold(f"📝 انتهى وقت {task['title']} وهذه نسخة الأسئلة للمراجعة."),parse_mode=ParseMode.HTML)
                delivered=True
            except TelegramError as exc:
                logger.warning("Private exam question release failed for %s/%s: %s",task["id"],student["user_id"],exc)
        if not delivered:
            raise TelegramError("No private exam question copy was delivered")
        await mark_questions_released(task["id"])
        return
    media=await get_task_media(task["id"])
    if not media and task.get("file_id"): media=[{"payload_type":task["payload_type"],"file_id":task["file_id"]}]
    if not media and task.get("text_content"):
        await context.bot.send_message(BIOLOGY_GROUP_ID,bold(f"📝 أسئلة {task['title']} بعد انتهاء وقت الامتحان\n\n{task['text_content']}"),parse_mode=ParseMode.HTML,message_thread_id=EXAM_TOPIC_ID or None)
    else:
        for index,item in enumerate(media):
            cap=bold(f"📝 أسئلة {task['title']} بعد انتهاء وقت الامتحان") if index==0 else None
            if item["payload_type"]=="photo": await context.bot.send_photo(BIOLOGY_GROUP_ID,item["file_id"],caption=cap,parse_mode=ParseMode.HTML if cap else None,message_thread_id=EXAM_TOPIC_ID or None)
            elif item["payload_type"]=="document": await context.bot.send_document(BIOLOGY_GROUP_ID,item["file_id"],caption=cap,parse_mode=ParseMode.HTML if cap else None,message_thread_id=EXAM_TOPIC_ID or None)
            else: await context.bot.send_video(BIOLOGY_GROUP_ID,item["file_id"],caption=cap,parse_mode=ParseMode.HTML if cap else None,message_thread_id=EXAM_TOPIC_ID or None)
    await mark_questions_released(task["id"])


async def issue_missing_task_warnings(context,task):
    removed=0; warned=0
    for student in await missing_students(task["id"]):
        result=await add_warning_once(student["user_id"],f"عدم إرسال {task['title']}",0,task["id"])
        if not result["created"]:
            continue
        count=result["count"]; warned+=1
        text=f"⚠️ إنذار تلقائي ({count}/{MAX_WARNINGS})\nالسبب: عدم إرسال {task['title']}"
        if count==4: text+="\n🚨 هذا هو التحذير الأخير."
        if count>=MAX_WARNINGS and BIOLOGY_GROUP_ID:
            try:
                await context.bot.ban_chat_member(BIOLOGY_GROUP_ID,student["user_id"]); removed+=1; text+="\n🚫 تم حظرك من كروب الأحياء."
            except TelegramError as exc:
                logger.warning("Could not ban student %s after warnings: %s",student["user_id"],exc)
        await notify_student_and_parent(context.bot,student,text)
        try: await context.bot.send_message(BIOLOGY_GROUP_ID,bold(f"⚠️ {student['full_name']}\n{text}"),parse_mode=ParseMode.HTML,message_thread_id=WARNINGS_TOPIC_ID or None)
        except TelegramError as exc: logger.warning("Could not publish warning for %s: %s",student["user_id"],exc)
    return warned,removed


async def close_tasks_job(context: ContextTypes.DEFAULT_TYPE):
    for task in await due_tasks():
        warned,removed=await issue_missing_task_warnings(context,task)
        # Keep only the extended students' window open. Non-extended students
        # have already received their warning at the original deadline.
        if await task_has_active_extensions(task["id"]):
            continue
        if await close_task(task["id"]):
            try: await context.bot.send_message(task["chat_id"],bold(f"⏰ تم إغلاق {task['title']}.\n⚠️ الإنذارات: {warned}\n🚫 المحظورون: {removed}"),parse_mode=ParseMode.HTML,message_thread_id=task["thread_id"] or None)
            except TelegramError: pass
            if task["kind"]=="exam" and not task.get("questions_released"):
                try: await release_exam_questions(context,task)
                except TelegramError as exc: logger.warning("Exam question release failed for %s: %s",task["id"],exc)
            await announce_champions(context,task["id"])
    # Repair missing warnings from tasks that an older release closed before
    # finishing its warning loop (for example after a restart or API failure).
    for task in await recently_closed_tasks_for_warning_recovery():
        await issue_missing_task_warnings(context,task)
    for task in await unreleased_closed_exams():
        try: await release_exam_questions(context,task)
        except TelegramError as exc: logger.warning("Retry exam question release failed for %s: %s",task["id"],exc)
    for task in await closed_exams_pending_champion():
        await announce_champions(context,task["id"])


async def scheduled_tasks_job(context: ContextTypes.DEFAULT_TYPE):
    for row in await due_scheduled_tasks():
        thread=HOMEWORK_TOPIC_ID if row["kind"]=="homework" else EXAM_TOPIC_ID
        visible_title=row["title"].replace("[تراكمي] ","")
        icon="📚" if row["kind"]=="homework" else "🏆" if row["title"].startswith("[تراكمي]") else "📝"
        # If the worker wakes late, preserve the complete configured duration
        # from the real Telegram publication instead of an elapsed timestamp.
        actual_publish_at=max(row["publish_at"],datetime.now(TIMEZONE))
        deadline=actual_publish_at+timedelta(hours=row["submission_hours"])
        caption=bold(f"{icon} {visible_title}\n\n⏰ آخر موعد للتسليم: {deadline.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}")
        try:
            media=await scheduled_task_media(row["id"])
            if not media: media=[{"payload_type":row["payload_type"],"file_id":row["file_id"]}]
            sent_items=[]; scope=row.get("target_scope") or "course"
            if scope!="course":
                synthetic_message=-(10_000_000+row["id"])
                for item in media: sent_items.append((item["payload_type"],item["file_id"],synthetic_message))
                task_chat=OWNER_CHAT_ID or row["created_by"]
            elif row["kind"]=="exam":
                notice=await context.bot.send_message(BIOLOGY_GROUP_ID,bold(f"📝 بدأ {visible_title}\nالأسئلة متاحة داخل البوت بعد تفعيل ولي الأمر.\n⏰ ينتهي: {deadline.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}"),parse_mode=ParseMode.HTML,message_thread_id=thread or None)
                for item in media: sent_items.append((item["payload_type"],item["file_id"],notice.message_id))
                task_chat=BIOLOGY_GROUP_ID
            else:
                for index,item in enumerate(media):
                    item_caption=caption if index==0 else None
                    if item["payload_type"]=="photo": sent=await context.bot.send_photo(BIOLOGY_GROUP_ID,item["file_id"],caption=item_caption,parse_mode=ParseMode.HTML if item_caption else None,message_thread_id=thread or None)
                    elif item["payload_type"]=="document": sent=await context.bot.send_document(BIOLOGY_GROUP_ID,item["file_id"],caption=item_caption,parse_mode=ParseMode.HTML if item_caption else None,message_thread_id=thread or None)
                    else: sent=await context.bot.send_video(BIOLOGY_GROUP_ID,item["file_id"],caption=item_caption,parse_mode=ParseMode.HTML if item_caption else None,message_thread_id=thread or None)
                    sent_items.append((item["payload_type"],item["file_id"],sent.message_id))
                task_chat=BIOLOGY_GROUP_ID
            reward=30 if row["kind"]=="homework" else 20
            first_type,first_file,first_message=sent_items[0]
            linked_lectures=await linked_definition_lecture_text(row["linked_definition_id"]) if row.get("linked_definition_id") else ""
            task=await create_task(row["kind"],row["title"],task_chat,thread if scope=="course" else 0,first_message,first_type,first_file,None,visible_title,deadline,reward,row["created_by"],scope,linked_lectures)
            for payload_type,file_id,message_id in sent_items[1:]: await add_task_media_by_id(task["id"],payload_type,file_id,message_id)
            await mark_scheduled_task_published(row["id"],first_message)
            await notify_task_assignment(context.bot,task)
            try: await context.bot.send_message(row["created_by"],bold(f"✅ تم نشر المنشور المجدول الآن: {visible_title}\n⏰ آخر موعد للتسليم: {deadline.astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}"),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        except Exception as exc:
            logger.exception("Scheduled task publish failed for %s: %s",row["id"],exc)


async def linked_definition_lecture_text(definition_id):
    rows=await linked_exam_lectures_text(definition_id); nums=[]
    # Backward-compatible definitions: if explicit lecture rows are absent, derive from prep links.
    if rows and isinstance(rows[0],dict) and "lecture" in rows[0]:
        return " + ".join(f"ف{r['chapter']}/م{r['lecture']}" for r in rows)
    for r in rows:
        groups=CHAPTER_PREPARATION_DISTRIBUTION.get(r["chapter"],[])
        if 1<=r["prep_no"]<=len(groups): nums.extend((r["chapter"],n) for n in groups[r["prep_no"]-1])
    return " + ".join(f"ف{c}/م{n}" for c,n in sorted(set(nums)))


async def linked_exam_dispatch_job(context: ContextTypes.DEFAULT_TYPE):
    """V26 single eligibility engine: completion -> pending activation -> approval -> real exam window."""
    for definition in await linked_exam_definitions():
        media=[(x["payload_type"],x["file_id"]) for x in await linked_exam_media(definition["id"])]
        if not media: continue
        for student in await active_students_for_linked_exam(definition["id"]):
            uid=student["user_id"]
            existing=await task_for_linked_exam_student(definition["id"],uid)
            if existing: continue
            status=await linked_exam_definition_status(definition["id"],uid)
            if not status or not status["ready"]: continue
            lectures=" + ".join(f"ف{r['chapter']}/م{r['lecture']}" for r in status["lectures"])
            task=await create_linked_exam_task_for_student(definition["id"],uid,definition["title"],media,lectures,definition["created_by"],definition.get("duration_hours") or DEFAULT_EXAM_HOURS)
            if task:
                await request_exam_access(task["id"],uid)
                student_row=await get_student(uid)
                parents=await student_parents(uid,True)
                approval_kb_parent=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تفعيل الامتحان",callback_data=f"examallow|{task['id']}|{uid}"),InlineKeyboardButton("❌ رفض",callback_data=f"examdeny|{task['id']}|{uid}")]])
                approval_kb_admin=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تفعيل الامتحان",callback_data=f"adminexamallow|{task['id']}|{uid}"),InlineKeyboardButton("❌ رفض",callback_data=f"adminexamdeny|{task['id']}|{uid}")]])
                for parent in parents:
                    try:
                        await context.bot.send_message(parent["parent_chat_id"],bold(f"🔐 امتحان جاهز للتفعيل\n👤 الطالب: {student_row['full_name']}\n📝 {definition['title']}\n🎬 يشمل: {lectures}\n\nيرجى الموافقة لتفعيل الامتحان."),parse_mode=ParseMode.HTML,reply_markup=approval_kb_parent)
                    except TelegramError: pass
                if not parents:
                    try:
                        await context.bot.send_message(OWNER_CHAT_ID or definition['created_by'],bold(f"🔐 طلب تفعيل امتحان — لا يوجد ولي أمر مربوط\n👤 الطالب: {student_row['full_name']}\n📝 {definition['title']}\n🎬 يشمل: {lectures}"),parse_mode=ParseMode.HTML,reply_markup=approval_kb_admin)
                    except TelegramError: pass
                try: await context.bot.send_message(uid,bold(f"📝 تم تجهيز الامتحان: {definition['title']}\n🎬 يشمل: {lectures}\n\n🔐 الامتحان بانتظار موافقة ولي الأمر أو الإدارة قبل التفعيل."),parse_mode=ParseMode.HTML)
                except TelegramError: pass


async def exam_parent_readiness_job(context: ContextTypes.DEFAULT_TYPE):
    for row in await scheduled_exam_parent_reminders():
        when=row["publish_at"].astimezone(TIMEZONE).strftime("%H:%M")
        for student in await students_for_scope(row.get("target_scope") or "course"):
            if not student.get("parent_chat_id"): continue
            try: await context.bot.send_message(student["parent_chat_id"],bold(f"⏰ تنبيه قبل الامتحان\nبعد نحو نصف ساعة، الساعة {when}، ستُرسل أسئلة «{row['title'].replace('[تراكمي] ','')}». يرجى أن يكون الطالب أمام أنظاركم عند تفعيل الامتحان."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        await mark_scheduled_parent_reminder(row["id"])


async def track_chat_member(update: Update,context: ContextTypes.DEFAULT_TYPE):
    change=update.chat_member
    if not change or change.chat.id!=BIOLOGY_GROUP_ID: return
    user=change.new_chat_member.user
    if user.is_bot or is_admin(user.id): return
    status=change.new_chat_member.status
    if status in (ChatMemberStatus.MEMBER,ChatMemberStatus.RESTRICTED,ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.OWNER):
        student=await get_student(user.id)
        if student and student["approved"] and student.get("parent_chat_id"): await mark_member_compliant(user.id)
        else:
            await observe_group_member(user.id,ACTIVATION_GRACE_HOURS)
            try: await context.bot.send_message(user.id,bold(f"⏳ لديك {ACTIVATION_GRACE_HOURS} ساعة لتسجيل وتفعيل بوت الأحياء وربط ولي الأمر، وإلا ستُزال من كروب الدورة."),parse_mode=ParseMode.HTML)
            except TelegramError: pass


async def observe_group_activity(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id!=BIOLOGY_GROUP_ID or not update.effective_user or update.effective_user.is_bot or is_admin(update.effective_user.id): return
    student=await get_student(update.effective_user.id)
    if student and student["approved"] and student.get("parent_chat_id"): await mark_member_compliant(update.effective_user.id)
    else: await observe_group_member(update.effective_user.id,ACTIVATION_GRACE_HOURS)


async def activation_compliance_job(context: ContextTypes.DEFAULT_TYPE):
    for row in await due_unactivated_members():
        uid=row["user_id"]
        if is_admin(uid): await mark_member_compliant(uid); continue
        try:
            member=await context.bot.get_chat_member(BIOLOGY_GROUP_ID,uid)
            if member.status in (ChatMemberStatus.LEFT,ChatMemberStatus.BANNED): await mark_member_removed(uid); continue
            try: await context.bot.send_message(uid,bold("🚫 انتهت مهلة تفعيل بوت الأحياء وربط ولي الأمر، لذلك تمت إزالتك من كروب الدورة. يمكنك التواصل مع الإدارة وإكمال التفعيل ثم العودة."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
            await context.bot.ban_chat_member(BIOLOGY_GROUP_ID,uid)
            await context.bot.unban_chat_member(BIOLOGY_GROUP_ID,uid,only_if_banned=True)
            await mark_member_removed(uid)
        except TelegramError as exc: logger.warning("Could not remove unactivated member %s: %s",uid,exc)


async def exam_reminders_job(context: ContextTypes.DEFAULT_TYPE):
    for task in await due_exam_reminders():
        deadline=task["deadline"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
        noun="الواجب" if task["kind"]=="homework" else "الامتحان التراكمي" if task["title"].startswith("[تراكمي]") else "الامتحان"
        for student in await students_pending_task(task["id"]):
            try: await context.bot.send_message(student["user_id"],bold(f"⏰ تذكير مهم\nبقي أقل من 6 ساعات على انتهاء {noun}: {task['title'].replace('[تراكمي] ','')}.\nآخر موعد: {deadline}\nلا تؤجل إرسال إجابتك."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        await mark_exam_reminder_sent(task["id"])


async def teacher_exam_deadline_job(context: ContextTypes.DEFAULT_TYPE):
    for task in await due_teacher_exam_deadline_reminders():
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🔒 إغلاق الامتحان",callback_data=f"teacherclose|{task['id']}"),InlineKeyboardButton("⏳ تمديده",callback_data=f"teacherextendmenu|{task['id']}")]])
        try: await context.bot.send_message(task["chat_id"],bold(f"⏰ بقي أقل من ساعة على انتهاء امتحان «{task['title']}».\nهل تريد إغلاقه الآن أم تمديده؟\nإذا لم ترد، سيُغلق في موعده المحدد."),parse_mode=ParseMode.HTML,message_thread_id=task.get("thread_id") or None,reply_markup=kb)
        except TelegramError:
            if OWNER_CHAT_ID:
                try: await context.bot.send_message(OWNER_CHAT_ID,bold(f"⏰ بقي أقل من ساعة على {task['title']}."),parse_mode=ParseMode.HTML,reply_markup=kb)
                except TelegramError: pass
        await mark_teacher_exam_deadline_reminder(task["id"])


async def study_and_progress_job(context: ContextTypes.DEFAULT_TYPE):
    now=datetime.now(TIMEZONE)
    if now.hour==16:
        key=f"study_reminder_{now.date()}"
        if not await setting_value(key):
            for student in await approved_students():
                progress=await student_achievements(student["user_id"],now.replace(hour=0,minute=0,second=0,microsecond=0))
                if progress["lectures"] or progress["homeworks"] or progress["exams"]:
                    message=f"أنت بدأت اليوم فعلاً: {progress['lectures']} محاضرة و{progress['homeworks']} واجب. لا تقطع السلسلة؛ أكمل خطوة واحدة إضافية الآن."
                else: message="لحد الآن ما مسجل إنجاز اليوم. افتح محاضرة واحدة فقط وابدأ أول 15 دقيقة؛ البداية الصغيرة اليوم تمنع تراكم كبير غداً."
                try: await context.bot.send_message(student["user_id"],bold(f"🎯 دفعة اليوم\n{message}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ أبدأ الآن",callback_data="today_prep")]]))
                except TelegramError: pass
            await set_setting_value(key,"sent")
    for stage in ("reminder","overdue"):
        for row in await incomplete_preparation_students(stage):
            key=f"prep_{stage}_{row['prep_no']}_{row['user_id']}"
            if await setting_value(key): continue
            if stage=="reminder":
                try: await context.bot.send_message(row["user_id"],bold(f"⏰ تذكير التحضير\nبقي نحو 6 ساعات على انتهاء مهلة مشاهدة محاضرات الفصل {row['chapter']} ({row['lectures']})."),parse_mode=ParseMode.HTML)
                except TelegramError: pass
            else:
                student_row=await get_student(row["user_id"])
                for p in await student_parents(row["user_id"],True):
                    try: await context.bot.send_message(p["parent_chat_id"],bold(f"⚠️ انتهت مهلة التحضير\nلم يكمل الطالب {row['full_name']} محاضرات الفصل {row['chapter']} ({row['lectures']}) خلال 24 ساعة."),parse_mode=ParseMode.HTML)
                    except TelegramError: pass
                try: await context.bot.send_message(BIOLOGY_GROUP_ID,bold(f"⚠️ تنبيه متابعة\nالطالب: {row['full_name']}\nلم يكمل المحاضرات خلال 24 ساعة: {row['lectures']}"),parse_mode=ParseMode.HTML,message_thread_id=WARNINGS_TOPIC_ID or None)
                except TelegramError: pass
            await set_setting_value(key,"sent")
        for row in await incomplete_personal_preparation_students(stage):
            key=f"personal_prep_{stage}_{row['prep_id']}_{row['user_id']}"
            if await setting_value(key): continue
            if stage=="reminder":
                try: await context.bot.send_message(row["user_id"],bold(f"⏰ تذكير التحضير الشخصي\nبقي نحو 6 ساعات على انتهاء مهلة محاضرات الفصل {row['chapter']} ({row['lectures']})."),parse_mode=ParseMode.HTML)
                except TelegramError: pass
            else:
                for p in await student_parents(row["user_id"],True):
                    try: await context.bot.send_message(p["parent_chat_id"],bold(f"⚠️ انتهت مهلة التحضير\nلم يكمل الطالب {row['full_name']} محاضرات الفصل {row['chapter']} ({row['lectures']}) خلال 24 ساعة."),parse_mode=ParseMode.HTML)
                    except TelegramError: pass
            await set_setting_value(key,"sent")
    if now.weekday()==4 and now.hour==20:
        key=f"weekly_champion_{now.date()}"
        if not await setting_value(key):
            winner=await weekly_top_student(now-timedelta(days=7))
            if winner and winner["earned"]>0:
                try: await context.bot.send_message(BIOLOGY_GROUP_ID,bold(f"🏆 أفضل طالب هذا الأسبوع\n━━━━━━━━━━━━━━━━━━\n👤 {winner['full_name']}\n⭐ جمع {winner['earned']} XP خلال الأسبوع\n\nهذا التفوق نتيجة التزام يومي حقيقي. مبارك!"),parse_mode=ParseMode.HTML,message_thread_id=CHAMPIONS_TOPIC_ID or None)
                except TelegramError: pass
            await set_setting_value(key,"sent")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; data=query.data; uid=query.from_user.id
    if data=="verify_join": await query.answer(); return
    if data=="onboardkeep":
        await query.answer(); await show_onboarding_track(query,True); return
    if data=="onboardedit":
        context.user_data["profile_refresh"]={"step":"full_name"}
        await query.answer(); await query.edit_message_text(bold("✏️ أرسل اسمك الثلاثي أو الرباعي الجديد:"),parse_mode=ParseMode.HTML); return
    if data.startswith("onboardtrack|"):
        choice=data.split("|",1)[1]; student_row=await get_student(uid)
        if not student_row: await query.answer("سجّل معلوماتك أولاً.",show_alert=True); return
        start_date=datetime.now(TIMEZONE).date()
        if choice=="course":
            await set_student_onboarding(uid,"course",3,start_date,[])
            summary="👥 تم اختيار: أكمل مع الدورة الحالية. سيستمر حسابك مع تحاضير الدورة الحالية، والواجبات والامتحانات المفتوحة، وكل ما ينزل لاحقاً ضمن مسار الدورة."
        else:
            chapter=int(choice); plan=build_personal_plan(chapter,start_date)
            await set_student_onboarding(uid,"chapter",chapter,start_date,plan)
            weekly="5 أيام و6 محاضرات" if chapter==1 else "4 أيام و5 محاضرات" if chapter==2 else "3 أيام و3 محاضرات"
            summary=f"📘 تم اختيار البدء من الفصل {chapter}.\n📅 الجدول الأول: {weekly} أسبوعياً، ثم ينتقل تلقائياً لنظام الفصل التالي."
        await query.answer("تم حفظ المسار")
        student_row=await get_student(uid); command=f"/parent {student_row['parent_link_code']}"
        parent_note="" if student_row.get("parent_chat_id") else f"\n\n👨‍👩‍👦 يجب ربط ولي أمر حقيقي من حساب Telegram مختلف. انسخ الأمر التالي وأرسله لولي أمرك، ثم يرسله هو للبوت:\n<code>{escape(command)}</code>"
        await query.edit_message_text(bold(f"✅ اكتمل تحديث حسابك\n{summary}")+parent_note,parse_mode=ParseMode.HTML,reply_markup=main_menu() if student_row.get("parent_chat_id") and student_row.get("approved") else parent_copy_markup(student_row["parent_link_code"]))
        return
    if data.startswith("parentnotify|"):
        pending=context.user_data.pop("pending_parent_link",None)
        if not pending: await query.answer("انتهت صلاحية الطلب. أرسل رمز الربط مرة أخرى.",show_alert=True); return
        notify=data.endswith("|yes")
        linked=await link_parent(pending["code"],uid,query.from_user.username,query.from_user.full_name,notify)
        if not linked: await query.answer("رمز الربط غير صحيح.",show_alert=True); return
        if linked.get('status')=='student_account':
            await query.answer('حسابك مسجل كطالب ولا يمكن ربطه كولي أمر.',show_alert=True)
            await query.edit_message_text('🚫 تم رفض الربط: حساب الطالب لا يعمل كولي أمر لطالب آخر.')
            return
        if linked.get("status")=="self_parent_forbidden":
            await query.answer("لا يمكن استعمال حساب الطالب نفسه كولي أمر.",show_alert=True)
            await query.edit_message_text(bold("🚫 رُفض الربط\nيجب فتح البوت من حساب Telegram مختلف يعود لولي الأمر الحقيقي، ثم إرسال أمر الربط من ذلك الحساب."),parse_mode=ParseMode.HTML); return
        await query.answer("تم الربط والتفعيل"); await query.edit_message_text(bold(f"✅ تم ربط وتفعيل حسابك مباشرة كولي أمر للطالب {linked['full_name']}."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👪 فتح واجهة ولي الأمر",callback_data="parent_menu")]]))
        if linked.get("approved"): await mark_member_compliant(linked["user_id"])
        else:
            try: await send_activation_request(context.bot,linked)
            except TelegramError as exc: logger.warning("Activation request after parent link failed: %s",exc)
        if notify:
            try: await context.bot.send_message(linked["user_id"],bold("✅ تم تسجيل ولي أمر جديد لحسابك."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        return
    if data.startswith(("approveparent|","rejectparent|")):
        if not is_admin(uid): await query.answer("هذا الزر للإدارة فقط.",show_alert=True); return
        action,student_s,parent_s=data.split("|"); student_id,parent_id=int(student_s),int(parent_s); approved=action=="approveparent"
        result=await decide_parent_link(student_id,parent_id,approved)
        if not result: await query.answer("تمت معالجة الطلب مسبقاً.",show_alert=True); return
        student_row=result["student"]
        if approved:
            await query.answer("تم التفعيل",show_alert=True)
            await query.edit_message_text((query.message.text_html or bold("طلب ولي أمر"))+bold("\n\n✅ تم تفعيل حساب ولي الأمر."),parse_mode=ParseMode.HTML)
            try: await context.bot.send_message(parent_id,bold(f"✅ تم تفعيل حسابك كولي أمر للطالب {student_row['full_name']}.\nاضغط الزر لفتح واجهة ولي الأمر."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👪 فتح واجهة ولي الأمر",callback_data="parent_menu")]]))
            except TelegramError: pass
        else:
            await query.answer("تم الرفض",show_alert=True)
            await query.edit_message_text((query.message.text_html or bold("طلب ولي أمر"))+bold("\n\n❌ تم رفض حساب ولي الأمر."),parse_mode=ParseMode.HTML)
            try: await context.bot.send_message(parent_id,bold(f"❌ لم توافق الإدارة على ربط حسابك بالطالب {student_row['full_name']}."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        return
    if data.startswith(("teacherclose|","teacherextendmenu|","teacherextend|")):
        if not is_admin(uid): await query.answer("هذا الزر للأستاذ فقط.",show_alert=True); return
        parts=data.split("|"); task_id=int(parts[1])
        if data.startswith("teacherclose|"):
            row=await teacher_change_exam_deadline(task_id,close_now=True); await query.answer("تم الإغلاق",show_alert=True)
            await query.edit_message_text(bold(f"🔒 تم إغلاق امتحان «{row['title']}»."),parse_mode=ParseMode.HTML); return
        if data.startswith("teacherextendmenu|"):
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("ساعة",callback_data=f"teacherextend|{task_id}|1"),InlineKeyboardButton("ساعتان",callback_data=f"teacherextend|{task_id}|2")],[InlineKeyboardButton("6 ساعات",callback_data=f"teacherextend|{task_id}|6"),InlineKeyboardButton("12 ساعة",callback_data=f"teacherextend|{task_id}|12")],[InlineKeyboardButton("24 ساعة",callback_data=f"teacherextend|{task_id}|24")]])
            await query.answer(); await query.edit_message_text(bold("⏳ اختر مدة تمديد الامتحان:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
        hours=int(parts[2]); row=await teacher_change_exam_deadline(task_id,hours=hours)
        await query.answer("تم التمديد",show_alert=True); await query.edit_message_text(bold(f"✅ تم تمديد «{row['title']}» {hours} ساعة.\nالموعد الجديد: {row['deadline'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}"),parse_mode=ParseMode.HTML); return
    if data=="reopen_latest_exam" or data.startswith("reopenhours|"):
        if not is_admin(uid): await query.answer("هذا الخيار للأستاذ فقط.",show_alert=True); return
        exam=await latest_daily_exam()
        if not exam: await query.answer("لا يوجد امتحان يومي سابق.",show_alert=True); return
        if data=="reopen_latest_exam":
            context.user_data.pop("awaiting_reopen_hours",None)
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("ساعة",callback_data="reopenhours|1"),InlineKeyboardButton("🔓 3 ساعات",callback_data="reopenhours|3")],[InlineKeyboardButton("6 ساعات",callback_data="reopenhours|6"),InlineKeyboardButton("12 ساعة",callback_data="reopenhours|12")],[InlineKeyboardButton("24 ساعة",callback_data="reopenhours|24")],[InlineKeyboardButton("⌨️ عدد ساعات محدد",callback_data="reopencustomhelp")],[back_menu()]])
            await query.answer(); await query.edit_message_text(bold(f"🔓 إعادة فتح آخر امتحان يومي\n{DIV}\n📝 {exam['title']}\nالموعد السابق: {exam['deadline'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}\n\nاختر مدة الفتح الجديدة:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
        hours=int(data.split("|")[1]); row=await reopen_latest_daily_exam(hours)
        deadline=row["deadline"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
        for s in await students_pending_task(row["id"]):
            try: await context.bot.send_message(s["user_id"],bold(f"🔓 تمت إعادة فتح الامتحان\n📝 {row['title']}\n⏳ متاح لمدة {hours} ساعة\n🕐 يغلق: {deadline}"),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        await query.answer("تمت إعادة الفتح",show_alert=True); await query.edit_message_text(bold(f"✅ تمت إعادة فتح «{row['title']}» لمدة {hours} ساعة.\nينتهي: {deadline}\nتم إعلام الطلبة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data=="reopencustomhelp":
        if not is_admin(uid): await query.answer("هذا الخيار للأستاذ فقط.",show_alert=True); return
        context.user_data["awaiting_reopen_hours"]=True
        await query.answer(); await query.edit_message_text(bold("⌨️ أرسل الآن عدد ساعات إعادة الفتح في رسالة واحدة، من 1 إلى 72.\nمثال: 5"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ العودة",callback_data="admin_exam_extensions"),back_menu()]])); return
    if data=="parent_menu" or (data=="menu" and await students_by_parent(uid,True)):
        rows=await students_by_parent(uid,True)
        await query.answer(); await query.edit_message_text(bold("👪 واجهة ولي الأمر\n━━━━━━━━━━\nاختر ملف متابعة الطالب:"),parse_mode=ParseMode.HTML,reply_markup=parent_menu(rows)); return
    if data.startswith(("parentprogress|","parentachievements|","parentwarnings|","parentleave|","parentleaveday|")):
        parts=data.split("|"); student_id=int(parts[1]); allowed={s["user_id"] for s in await students_by_parent(uid,True)}
        if student_id not in allowed: await query.answer("هذا الطالب غير مربوط بحسابك.",show_alert=True); return
        student_row=await get_student(student_id); now=datetime.now(TIMEZONE); start_week=now-timedelta(days=7)
        back=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ واجهة ولي الأمر",callback_data="parent_menu")]])
        if data.startswith("parentprogress|"):
            bundle=await parent_report_bundle(student_id,start_week,now,start_week-timedelta(days=7)); current=bundle["current"]
            grades=[r for r in current if r["kind"]=="exam" and r["grade"] is not None]; previous=[r for r in bundle["previous"] if r["kind"]=="exam" and r["grade"] is not None]
            avg=round(sum(float(r["grade"]) for r in grades)/len(grades),1) if grades else None; old=round(sum(float(r["grade"]) for r in previous)/len(previous),1) if previous else None
            trend="لا توجد بيانات كافية" if avg is None or old is None else (f"⬆️ تطور {round(avg-old,1)} درجة" if avg>=old else f"⬇️ تراجع {round(old-avg,1)} درجة")
            lines=[f"📊 درجات وتقدم {student_row['full_name']}",DIV]+[f"📝 {r['title']}: {r['grade']}/100" for r in grades]
            if not grades: lines.append("لا توجد درجات امتحان هذا الأسبوع.")
            lines.extend([DIV,f"المعدل: {avg if avg is not None else '-'}",f"التطور: {trend}",f"⭐ XP: {student_row['xp']}"])
            await query.answer(); await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=back); return
        if data.startswith("parentachievements|"):
            a=await student_achievements(student_id,start_week)
            text=f"🏅 إنجازات {student_row['full_name']} هذا الأسبوع\n{DIV}\n🎬 محاضرات: {a['lectures']}\n📚 واجبات: {a['homeworks']}\n📝 امتحانات: {a['exams']}\n📊 معدل الامتحانات: {a['average'] if a['average'] is not None else '-'}\n⭐ صافي XP: {a['xp_earned']}"
            await query.answer(); await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=back); return
        if data.startswith("parentwarnings|"):
            warnings=await student_warning_history(student_id); lines=[f"⚠️ إنذارات {student_row['full_name']}",f"المجموع: {student_row['warnings']}",DIV]
            for i,w in enumerate(warnings,1): lines.append(f"{i}. {w['reason']}\n🕐 {w['created_at'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}")
            if not warnings: lines.append("لا توجد إنذارات.")
            await query.answer(); await query.edit_message_text(bold("\n\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=back); return
        if data.startswith("parentleave|"):
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("اليوم",callback_data=f"parentleaveday|{student_id}|0"),InlineKeyboardButton("غداً",callback_data=f"parentleaveday|{student_id}|1")],[InlineKeyboardButton("◀️ واجهة ولي الأمر",callback_data="parent_menu")]])
            await query.answer(); await query.edit_message_text(bold(f"🏖 طلب إجازة لـ {student_row['full_name']}\nتعفيه من جميع مطلوبات يوم كامل، ويُخصم 400 XP من رصيده.\nالرصيد الحالي: {student_row['xp']} XP"),parse_mode=ParseMode.HTML,reply_markup=kb); return
        leave_date=now.date()+timedelta(days=int(parts[2])); student_row=await get_student(student_id)
        if not student_row or not (student_row.get("study_track")=="chapter" or student_row.get("schedule_mode")=="custom"): await query.answer("نظام الإجازات متاح للطلاب على جدول شخصي أو مسار فصل مستقل.",show_alert=True); return
        if await leave_month_usage(student_id,leave_date)>=4: await query.answer("الطالب استنفد 4 إجازات لهذا الشهر.",show_alert=True); return
        result=await create_parent_leave(student_id,leave_date,uid)
        if result["status"]=="xp": await query.answer("رصيد الطالب أقل من 400 XP.",show_alert=True); return
        if result["status"]!="ok": await query.answer("يوجد طلب إجازة لهذا اليوم مسبقاً.",show_alert=True); return
        try: await context.bot.send_message(student_id,bold(f"🏖 تم اعتماد إجازتك بطلب ولي الأمر ليوم {leave_date.strftime('%d/%m/%Y')}. تم خصم 400 XP."),parse_mode=ParseMode.HTML)
        except TelegramError: pass
        await query.answer("تمت الإجازة",show_alert=True); await query.edit_message_text(bold(f"✅ تم اعتماد إجازة {student_row['full_name']} ليوم {leave_date.strftime('%d/%m/%Y')} وخصم 400 XP."),parse_mode=ParseMode.HTML,reply_markup=back); return
    if data.startswith("gradefile|"):
        if not is_admin(uid): await query.answer("هذا الزر للإدارة فقط.",show_alert=True); return
        _,choice,token=data.split("|"); state=context.chat_data.get("pending_grade_files",{}).pop(token,None)
        if not state: await query.answer("انتهت صلاحية هذا الطلب.",show_alert=True); return
        ref=state["ref"]
        if choice=="yes":
            context.chat_data["awaiting_grade_value"]=state
            await query.answer(); await query.edit_message_text(bold("📊 أرسل الآن الدرجة رقماً من 0 إلى 100 داخل نفس Topic."),parse_mode=ParseMode.HTML); return
        try:
            content=(state.get("content") or "").strip(); mode="student"
            if content.startswith("ولي#") or content.startswith("#ولي"): mode="parent"; content=content.replace("ولي#","",1).replace("#ولي","",1).strip()
            elif content.startswith("الكل#") or content.startswith("#الكل"): mode="both"; content=content.replace("الكل#","",1).replace("#الكل","",1).strip()
            recipients=[]
            if mode in ("student","both"): recipients.append((ref["user_id"],"student"))
            if mode in ("parent","both") and ref.get("parent_chat_id"): recipients.append((ref["parent_chat_id"],"parent"))
            if not recipients: await query.answer("لا يوجد ولي أمر مربوط.",show_alert=True); return
            caption=bold(content or "💬 رسالة من إدارة الدورة")
            for chat_id,role in recipients:
                if state["payload_type"]=="photo": sent=await context.bot.send_photo(chat_id,state["file_id"],caption=caption,parse_mode=ParseMode.HTML)
                elif state["payload_type"]=="document": sent=await context.bot.send_document(chat_id,state["file_id"],caption=caption,parse_mode=ParseMode.HTML)
                else: sent=await context.bot.send_video(chat_id,state["file_id"],caption=caption,parse_mode=ParseMode.HTML)
                await save_communication_route(chat_id,sent.message_id,ref["user_id"],query.message.chat_id,state["thread_id"],role)
            await query.answer("أُرسل كرسالة عادية"); await query.edit_message_text(bold("✅ أُرسل الملف كرسالة عادية، ولم تُسجل درجة."),parse_mode=ParseMode.HTML)
        except TelegramError: await query.answer("تعذر الإرسال.",show_alert=True)
        return
    if data.startswith("changegrade|"):
        if not is_admin(uid): await query.answer("هذا الزر للإدارة فقط.",show_alert=True); return
        _,task_s,student_s=data.split("|"); task=await get_task(int(task_s)); student_row=await get_student(int(student_s))
        if not task or not student_row: await query.answer("تعذر العثور على التسليم.",show_alert=True); return
        ref={"task_id":int(task_s),"user_id":int(student_s),"kind":task["kind"],"title":task["title"],"full_name":student_row["full_name"],"parent_chat_id":student_row.get("parent_chat_id")}
        context.chat_data["awaiting_grade_value"]={"ref":ref,"payload_type":"text","file_id":None,"thread_id":query.message.message_thread_id or 0}
        await query.answer(); await query.edit_message_text(bold("✏️ أرسل الدرجة الجديدة رقماً من 0 إلى 100 داخل نفس Topic."),parse_mode=ParseMode.HTML); return
    if data.startswith("retrysubmission|"):
        task_id=int(data.split("|")[1]); result=await prepare_submission_retry(task_id,uid)
        if result["status"]=="missing": await query.answer("ما عندك إجابة مسجلة لهذا العنصر.",show_alert=True); return
        if result["status"]=="limit": await query.answer("استخدمت محاولتي تغيير الإجابة المسموحتين.",show_alert=True); return
        for row in result["messages"]:
            try: await context.bot.delete_message(row["chat_id"],row["message_id"])
            except TelegramError: pass
        context.user_data["waiting_submission"]=task_id
        context.user_data.pop("submission_album",None)
        await query.answer("تم حذف الإجابة"); await query.edit_message_text(bold(f"✅ حُذفت إجابتك السابقة. أرسل جميع صور الإجابة الصحيحة الآن.\nمحاولات التغيير المتبقية: {result['remaining']} من 2."),parse_mode=ParseMode.HTML); return
    if data.startswith("submissionok|"):
        context.user_data.pop("waiting_submission",None); context.user_data.pop("submission_album",None)
        await query.answer("تم تثبيت التسليم",show_alert=True)
        await query.edit_message_text(bold("✅ تم تثبيت إجاباتك وإرسالها للأستاذ بنجاح."),parse_mode=ParseMode.HTML); return
    if data.startswith("requestadminexam|"):
        task_id=int(data.split("|")[1]); task=await get_task(task_id); student_row=await get_student(uid)
        if not task or not student_row: await query.answer("الطلب غير متاح.",show_alert=True); return
        destination=EXAM_SUBMISSIONS_CHAT_ID or OWNER_CHAT_ID
        try:
            thread_id=await ensure_student_topic(context.bot,student_row,destination)
            kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تفعيل الامتحان",callback_data=f"adminexamallow|{task_id}|{uid}")]])
            await context.bot.send_message(destination,bold(f"🔐 طلب تفعيل من الطالب\n👤 {student_row['full_name']}\n📝 {task['title']}"),parse_mode=ParseMode.HTML,message_thread_id=thread_id,reply_markup=kb)
            await query.answer("أُرسل الطلب للأدمن",show_alert=True)
        except TelegramError: await query.answer("تعذر إرسال الطلب حالياً.",show_alert=True)
        return
    if data=="guest_continue":
        await query.answer(); await query.edit_message_text(bold("📚 المكتبة العامة لبوت الأحياء\nيمكنك تصفح المحتوى، أما خدمات الدورة فتحتاج إلى التسجيل."),parse_mode=ParseMode.HTML,reply_markup=guest_menu()); return
    if data=="guest_enroll":
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("👥 الانضمام إلى كروب الأحياء",url=GROUP_INVITE_URL)],[InlineKeyboardButton("📩 التواصل مع الإدارة",url=f"https://t.me/{OWNER_USERNAME}")],[InlineKeyboardButton("📚 المكتبة العامة",callback_data="guest_continue")]])
        await query.answer(); await query.edit_message_text(bold("🎓 التسجيل في الدورة المجانية\n\n1. انضم إلى كروب الأحياء.\n2. تواصل مع الإدارة لتأكيد انضمامك.\n3. بعد إضافتك أرسل /start واملأ معلومات التسجيل."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("deletetask|"):
        if not is_admin(uid): await query.answer("الحذف متاح للإدارة فقط.",show_alert=True); return
        task_id=int(data.split("|")[1]); task=await get_task(task_id)
        if not task: await query.answer("المنشور غير موجود.",show_alert=True); return
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم، حذف نهائياً",callback_data=f"confirmdeletetask|{task_id}")],[InlineKeyboardButton("❌ تراجع",callback_data=f"task|{task_id}")]])
        await query.answer(); await query.edit_message_text(bold(f"⚠️ تأكيد الحذف\nهل تريد حذف «{task['title'].replace('[تراكمي] ','')}»؟\nسيختفي من البوت ومن كروب الأحياء، وتُحذف تسليماته ودرجاته المرتبطة."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("confirmdeletetask|"):
        if not is_admin(uid): await query.answer("الحذف متاح للإدارة فقط.",show_alert=True); return
        task_id=int(data.split("|")[1]); task=await get_task(task_id)
        if not task: await query.answer("تم حذفه مسبقاً.",show_alert=True); return
        media=await get_task_media(task_id); message_ids={task["source_message_id"]}|{item["source_message_id"] for item in media}
        for message_id in message_ids:
            try: await context.bot.delete_message(task["chat_id"],message_id)
            except TelegramError: pass
        await delete_task(task_id)
        await query.answer("تم الحذف"); await query.edit_message_text(bold("✅ تم حذف الواجب أو الامتحان نهائياً من البوت."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data.startswith("bind|") or data.startswith("cancelbind|") or data.startswith("deadline|") or data.startswith("customdeadline|"):
        if not is_admin(uid): await query.answer("هذا الزر للأدمن فقط.",show_alert=True); return
        parts=data.split("|"); pending_id=int(parts[1]); pending=await get_pending_task(pending_id)
        if not pending: await query.answer("تم التعامل مع هذا المنشور مسبقاً.",show_alert=True); return
        if data.startswith("cancelbind|"):
            await delete_pending_task(pending_id); await query.answer("تم الإلغاء"); await query.edit_message_text(bold("❌ لم يتم ربط المنشور بالبوت."),parse_mode=ParseMode.HTML); return
        if data.startswith("bind|"):
            kb=InlineKeyboardMarkup([
                [InlineKeyboardButton("ساعتان",callback_data=f"deadline|{pending_id}|2"),InlineKeyboardButton("6 ساعات",callback_data=f"deadline|{pending_id}|6")],
                [InlineKeyboardButton("12 ساعة",callback_data=f"deadline|{pending_id}|12"),InlineKeyboardButton("24 ساعة",callback_data=f"deadline|{pending_id}|24")],
                [InlineKeyboardButton("48 ساعة",callback_data=f"deadline|{pending_id}|48"),InlineKeyboardButton("72 ساعة",callback_data=f"deadline|{pending_id}|72")],
                [InlineKeyboardButton("🗓 موعد مخصص",callback_data=f"customdeadline|{pending_id}")],
            ])
            await query.answer(); await query.edit_message_text(bold("⏰ اختر مدة انتهاء التسليم من الآن:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
        if data.startswith("customdeadline|"):
            await query.answer(); await query.edit_message_text(bold(f"أرسل الأمر التالي في المجموعة مع تغيير التاريخ والساعة:\n/deadline {pending_id} 26/8/2026 23:00"),parse_mode=ParseMode.HTML); return
        hours=int(parts[2]); deadline=datetime.now(TIMEZONE)+timedelta(hours=hours); task=await confirm_pending_task(pending_id,deadline)
        if task: await notify_task_assignment(context.bot,task)
        noun="الواجب" if task["kind"]=="homework" else "الامتحان"
        await query.answer("تم الربط"); await query.edit_message_text(bold(f"✅ تم ربط {noun} بالبوت.\n🆔 الرقم: {task['id']}\n⏰ انتهاء التسليم: {deadline.strftime('%d/%m/%Y %H:%M')}"),parse_mode=ParseMode.HTML); return
    if data.startswith("finisharchive|"):
        if not is_admin(uid): await query.answer("هذا الزر للأدمن فقط.",show_alert=True); return
        archive_id=int(data.split("|")[1])
        if context.user_data.get("waiting_archive_id")==archive_id: context.user_data.pop("waiting_archive_id",None)
        await query.answer("تم الحفظ"); await query.edit_message_text(bold("✅ تم إنهاء وحفظ الامتحان في قسم الامتحانات السابقة."),parse_mode=ParseMode.HTML); return
    if data.startswith("addarchive|"):
        if not is_admin(uid): await query.answer("هذا الزر للأدمن فقط.",show_alert=True); return
        chapter=int(data.split("|")[1]); context.user_data["archive_awaiting"]={"chapter":chapter,"step":"lecture"}
        await query.answer(); await query.edit_message_text(bold(f"🎬 أرسل رقم المحاضرة في الفصل {chapter} أولاً، وبعدها اسم الامتحان ثم الصور أو PDF."),parse_mode=ParseMode.HTML); return
    if data.startswith("finishresource|"):
        if not is_admin(uid): await query.answer("هذا الزر للأدمن فقط.",show_alert=True); return
        resource_id=int(data.split("|")[1])
        if context.user_data.get("waiting_resource_id")==resource_id: context.user_data.pop("waiting_resource_id",None)
        await query.answer("تم الحفظ"); await query.edit_message_text(bold("✅ تم إنهاء وحفظ الملف بنجاح."),parse_mode=ParseMode.HTML); return
    if data.startswith("addresource|"):
        if not is_admin(uid): await query.answer("هذا الزر للأدمن فقط.",show_alert=True); return
        _,category,chapter_s=data.split("|"); context.user_data["resource_awaiting_title"]={"category":category,"chapter":int(chapter_s)}
        await query.answer(); await query.edit_message_text(bold(f"✍️ أرسل الآن اسم {RESOURCE_LABELS[category]} في رسالة خاصة، ثم أرسل الصور أو ملف PDF."),parse_mode=ParseMode.HTML); return
    if data=="admin_students":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        rows=await all_students_admin_view(); await query.answer(); await query.edit_message_text(bold(f"👥 عدد حسابات الطلبة: {len(rows)}\nسيتم إرسال التفاصيل لك الآن."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]]))
        for start_index in range(0,len(rows),15):
            lines=[]
            for s in rows[start_index:start_index+15]: lines.append(f"👤 {s['full_name']}\n🆔 {s['user_id']} | @{s['username'] or '-'}\n🏫 {s['school']} | 🎯 {s['target_grade']}\n👨‍👩‍👦 ولي الأمر: {s['parent_full_name'] or 'غير مربوط'} | @{s['parent_username'] or '-'} | {s['parent_chat_id'] or '-'} | {'✅ مفعل' if s['parent_approved'] else '⏳ بانتظار التفعيل'}\n⚠️ {s['warnings']} | ⭐ {s['xp']} | {'✅ مفعل' if s['approved'] else '⏳ غير مفعل'}")
            await context.bot.send_message(uid,bold("\n\n".join(lines)),parse_mode=ParseMode.HTML)
        return
    if data=="admin_parents":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        rows=await all_parents_admin_view(); await query.answer(); await query.edit_message_text(bold(f"👪 عدد حسابات أولياء الأمور: {len(rows)}\nستصلك التفاصيل الآن."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]]))
        for start_index in range(0,len(rows),15):
            lines=[]
            for p in rows[start_index:start_index+15]: lines.append(f"👪 {p['parent_full_name'] or '-'}\n🆔 {p['parent_chat_id']} | @{p['parent_username'] or '-'}\n👤 الطالب: {p['student_name']} ({p['student_id']})\n{'✅ مفعل' if p['approved'] else '⏳ بانتظار التفعيل'} | ⭐ رصيد الطالب {p['xp']} | ⚠️ {p['warnings']}")
            await context.bot.send_message(uid,bold("\n\n".join(lines)),parse_mode=ParseMode.HTML)
        return
    if data=="admin_publish":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 واجب",callback_data="manualkind|homework")],
            [InlineKeyboardButton("📝 امتحان الدورة الحالية",callback_data="courseexam_start")],
            [InlineKeyboardButton("🔗 امتحان مرتبط بفصل",callback_data="linkedexam_start")],
            [InlineKeyboardButton("🏆 امتحان تراكمي للدورة",callback_data="courseexam_start|cumulative")],
            [InlineKeyboardButton("🗓 عرض المنشورات المجدولة",callback_data="scheduled_list")],[back_menu()]])
        await query.answer(); await query.edit_message_text(bold("➕ اختر نوع المنشور الذي تريد نشره:\n\n📝 امتحان الدورة = للدورة الحالية فقط، ويرتبط بمجموعة محاضرات أو أكثر تلقائياً.\n🔗 امتحان الفصل = يرتبط بمحاضرة واحدة أو عدة محاضرات ويعمل لكل طالب حسب مساره."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data in {"linkedexam_start","courseexam_start","courseexam_start|cumulative"}:
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        course=data.startswith("courseexam_start")
        cumulative=data=="courseexam_start|cumulative"
        context.user_data["linked_exam"]={"step":"select_preps","selected_preps":[],"audience":"course" if course else "chapter","cumulative":cumulative}
        if course:
            rows=await all_preparations()
            buttons=[]
            for r in rows:
                label=f"{'✅ ' if r['prep_no'] in [] else ''}ف{r['chapter']} | المحاضرات {r['lectures'].replace(',', ' + ')}"
                buttons.append([InlineKeyboardButton(label,callback_data=f"courseprep|{r['chapter']}|{r['chapter_prep_no'] or r['prep_no']}")])
            buttons.append([InlineKeyboardButton("✅ إنهاء اختيار المحاضرات",callback_data="linkedprep_done")])
            buttons.append([back_menu()])
            text=("🏆 امتحان تراكمي للدورة الحالية" if cumulative else "📝 امتحان الدورة الحالية")+"\n\nاختر مجموعة محاضرات واحدة أو عدة مجموعات. يمكنك دمج أكثر من محاضرة في امتحان واحد. يصبح مستحقا فور إكمال الطالب جميع المحاضرات المطلوبة، ثم ينتظر موافقة ولي الأمر."
        else:
            buttons=[[InlineKeyboardButton(f"📘 الفصل {chapter}",callback_data=f"linkedexamchapter|{chapter}")
                      for chapter in range(start,min(start+2,6))] for start in range(1,6,2)]
            buttons.append([back_menu()])
            text="🔗 امتحان مرتبط بفصل\n\nاختر الفصل، ثم اختر المحاضرات التي تريد جمعها في الامتحان. يصبح الامتحان مستحقا فور إكمال جميع المحاضرات المطلوبة، ثم ينتظر موافقة ولي الأمر قبل الفتح."
        await query.answer(); await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith("linkedexamchapter|"):
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        state=context.user_data.get("linked_exam")
        if not state or state.get("audience")!="chapter": await query.answer("ابدأ العملية من جديد.",show_alert=True); return
        chapter=int(data.split("|")[1]); groups=CHAPTER_PREPARATION_DISTRIBUTION.get(chapter,[])
        state["chapter"]=chapter; state["selected_preps"]=[]
        rows=[]
        for i,nums in enumerate(groups,1): rows.append([InlineKeyboardButton(f"☐ المحاضرات {' + '.join(map(str,nums))}",callback_data=f"linkedexamprep|{chapter}|{i}")])
        rows.append([InlineKeyboardButton("✅ إنهاء اختيار المحاضرات",callback_data="linkedprep_done")])
        rows.append([InlineKeyboardButton("◀️ اختيار الفصل",callback_data="linkedexam_start")])
        await query.answer(); await query.edit_message_text(bold(f"📚 الفصل {chapter}\n\nاختر مجموعة محاضرات واحدة أو أكثر ثم اضغط إنهاء الاختيار."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith("coursepage|"):
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state or state.get("audience")!="course": await query.answer("ابدأ العملية من جديد.",show_alert=True); return
        page=max(0,int(data.split("|")[1])); rows=await all_preparations(); page_size=10; total=(len(rows)+page_size-1)//page_size
        if page>=total: page=0
        state["course_page"]=page; selected=state.setdefault("selected_preps",[]); kb=[]
        for r in rows[page*page_size:(page+1)*page_size]:
            p=(r["chapter"],r["chapter_prep_no"] or r["prep_no"]); mark="☑" if p in selected else "☐"
            kb.append([InlineKeyboardButton(f"{mark} ف{p[0]} | المحاضرات {r['lectures'].replace(',', ' + ')}",callback_data=f"courseprep|{p[0]}|{p[1]}")])
        nav=[]
        if page>0: nav.append(InlineKeyboardButton("◀️ السابق",callback_data=f"coursepage|{page-1}"))
        if page<total-1: nav.append(InlineKeyboardButton("التالي ▶️",callback_data=f"coursepage|{page+1}"))
        if nav: kb.append(nav)
        kb.append([InlineKeyboardButton("✅ إنهاء اختيار المحاضرات",callback_data="linkedprep_done")])
        await query.answer(); await query.edit_message_text(bold(f"📝 اختيار محاضرات امتحان الدورة — صفحة {page+1}/{total}\n\n📌 المجموعات المختارة: {len(selected)}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("linkedexamprep|") or data.startswith("courseprep|"):
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state: await query.answer("ابدأ العملية من جديد.",show_alert=True); return
        _,ch,prep=data.split("|"); chapter=int(ch); prep_no=int(prep)
        state["chapter"]=chapter
        pair=(chapter,prep_no)
        selected=state.setdefault("selected_preps",[])
        if pair in selected: selected.remove(pair); status="أزيل"
        else: selected.append(pair); status="أضيف"
        # Re-render selection list.
        if state.get("audience")=="course":
            rows=await all_preparations()
            kb=[]
            for r in rows:
                p=(r["chapter"],r["chapter_prep_no"] or r["prep_no"]); mark="☑" if p in selected else "☐"
                kb.append([InlineKeyboardButton(f"{mark} ف{r['chapter']} | المحاضرات {r['lectures'].replace(',', ' + ')}",callback_data=f"courseprep|{p[0]}|{p[1]}")])
        else:
            groups=CHAPTER_PREPARATION_DISTRIBUTION[chapter]; kb=[]
            for i,nums in enumerate(groups,1):
                mark="☑" if (chapter,i) in selected else "☐"
                kb.append([InlineKeyboardButton(f"{mark} المحاضرات {' + '.join(map(str,nums))}",callback_data=f"linkedexamprep|{chapter}|{i}")])
        kb.append([InlineKeyboardButton("✅ إنهاء اختيار المحاضرات",callback_data="linkedprep_done")])
        await query.answer(f"{status}ت مجموعة المحاضرات")
        await query.edit_message_text(bold(f"اختر المحاضرات المطلوبة:\n\n📌 المجموعات المختارة: {len(selected)}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data=="linkedprep_done":
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state or not state.get("selected_preps"): await query.answer("اختر مجموعة محاضرات واحدة على الأقل.",show_alert=True); return
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✍️ المتابعة",callback_data="linkedprep_title")],[InlineKeyboardButton("🎬 إضافة محاضرات منفردة يدويا",callback_data="linkedmanual_start")],[back_menu()]])
        await query.answer(); await query.edit_message_text(bold(f"✅ تم اختيار {len(state['selected_preps'])} مجموعة محاضرات.\n\nيمكنك المتابعة أو إضافة محاضرات منفردة."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="linkedprep_title":
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state or not state.get("selected_preps"): await query.answer("اختر محاضرات أولا.",show_alert=True); return
        state["step"]="title"
        await query.answer(); await query.edit_message_text(bold(f"📚 المجموعات المختارة: {len(state['selected_preps'])}\n\n✍️ أرسل الآن اسم الامتحان."),parse_mode=ParseMode.HTML); return
    if data=="linkedmanual_start":
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state: await query.answer("ابدأ العملية من جديد.",show_alert=True); return
        state["selected_lectures"]=state.get("selected_lectures",[])
        buttons=[[InlineKeyboardButton(f"📘 الفصل {chapter}",callback_data=f"linkedmanual_ch|{chapter}")
                  for chapter in range(start,min(start+3,10))] for start in range(1,10,3)]
        buttons.append([InlineKeyboardButton("❌ إلغاء",callback_data="linkedexamcancel",style='danger')])
        await query.answer(); await query.edit_message_text(bold("🎬 اختيار المحاضرات يدويًا\n\nاختر الفصل ثم حدد المحاضرات التي تريد إدخالها في الامتحان."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(buttons)); return
    if data.startswith("linkedmanual_ch|"):
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state: await query.answer("ابدأ العملية من جديد.",show_alert=True); return
        ch=int(data.split("|")[1]); state["manual_chapter"]=ch; selected=set(tuple(x) for x in state.get("selected_lectures",[])); ids=[x[0] for x in PLAYLISTS.get(ch,[])]
        kb=[]
        for i in range(0,len(ids),3): kb.append([InlineKeyboardButton(("☑️ " if (ch,n) in selected else "☐ ")+f"م{n}",callback_data=f"linkedmanual_lect|{ch}|{n}") for n in ids[i:i+3]])
        kb += [[InlineKeyboardButton("✅ إنهاء الاختيار",callback_data="linkedmanual_done")],[InlineKeyboardButton("◀️ الفصول",callback_data="linkedmanual_start")]]
        await query.answer(); await query.edit_message_text(bold(f"📘 الفصل {ch}\n\nالمختار: {', '.join('م'+str(n) for c,n in sorted(selected) if c==ch) or 'لا يوجد'}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("linkedmanual_lect|"):
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state: await query.answer("ابدأ العملية من جديد.",show_alert=True); return
        _,ch_s,lec_s=data.split("|"); pair=(int(ch_s),int(lec_s)); selected=[tuple(x) for x in state.get("selected_lectures",[])]
        if pair in selected: selected.remove(pair)
        else: selected.append(pair)
        state["selected_lectures"]=selected; ch=pair[0]; ids=[x[0] for x in PLAYLISTS.get(ch,[])]; sel=set(selected); kb=[]
        for i in range(0,len(ids),3): kb.append([InlineKeyboardButton(("☑️ " if (ch,n) in sel else "☐ ")+f"م{n}",callback_data=f"linkedmanual_lect|{ch}|{n}") for n in ids[i:i+3]])
        kb += [[InlineKeyboardButton("✅ إنهاء الاختيار",callback_data="linkedmanual_done")],[InlineKeyboardButton("◀️ الفصول",callback_data="linkedmanual_start")]]
        await query.answer(); await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb)); return
    if data=="linkedmanual_done":
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state or not state.get("selected_lectures"): await query.answer("اختر محاضرة واحدة على الأقل.",show_alert=True); return
        state["step"]="title"; await query.answer(); await query.edit_message_text(bold("🎬 تم اختيار المحاضرات يدويًا.\n\n✍️ أرسل الآن اسم الامتحان."),parse_mode=ParseMode.HTML); return
    if data=="linkedexamfinish":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        state=context.user_data.get("linked_exam")
        if not state or state.get("step")!="media" or not state.get("media") or (not state.get("selected_preps") and not state.get("selected_lectures")):
            await query.answer("أكمل اختيار مجموعات المحاضرات وأرسل ملفاً واحداً على الأقل.",show_alert=True); return
        if state.get("audience")=="chapter" and state.get("selected_preps") and len({c for c,p in state["selected_preps"]})!=1:
            await query.answer("امتحان الفصل يجب أن تكون محاضراته من الفصل نفسه.",show_alert=True); return
        try:
            definition=await create_linked_exam_definition(state.get("selected_preps",[]),state["title"],uid,state["media"],state.get("audience","chapter"),state.get("selected_lectures",[]),"cumulative" if state.get("cumulative") else "normal",DEFAULT_EXAM_HOURS)
        except Exception as exc:
            logger.exception("Could not create linked exam definition: %s",exc)
            await query.answer("تعذر حفظ الامتحان. حاول مرة أخرى.",show_alert=True); return
        selected_text="، ".join(f"الفصل {c}/مجموعة {p}" for c,p in state.get("selected_preps",[])) or "اختيار يدوي"
        lecture_labels=[]
        for c,pno in state.get("selected_preps",[]):
            groups=CHAPTER_PREPARATION_DISTRIBUTION.get(c,[])
            if 1<=pno<=len(groups): lecture_labels.extend(f"ف{c}/م{number}" for number in groups[pno-1])
        lecture_labels.extend(f"ف{c}/م{l}" for c,l in state.get("selected_lectures",[]))
        audience="الدورة الحالية فقط" if state.get("audience")=="course" else "الفصل المحدد"
        context.user_data.pop("linked_exam",None)
        await query.answer("تم حفظ الامتحان")
        await query.edit_message_text(bold(f"✅ تم حفظ الامتحان بنجاح\n\n🎯 النطاق: {audience}\n📚 المجموعات المختارة: {selected_text}\n🎬 المحاضرات الداخلة: {', '.join(sorted(set(lecture_labels)))}\n\n⏰ يصبح مستحقًا بعد إكمال جميع المحاضرات الداخلة.\n🔐 ثم يحتاج إلى موافقة ولي الأمر أو الإدارة قبل التفعيل.\n🔔 ويصله إشعار هو وولي الأمر."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ إضافة امتحان آخر",callback_data="courseexam_start" if state.get("audience")=="course" else "linkedexam_start")],[back_menu()]])); return
    if data=="linkedexamcancel":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        context.user_data.pop("linked_exam",None)
        await query.answer("تم إلغاء العملية")
        await query.edit_message_text(bold("❌ تم إلغاء إضافة الامتحان المرتبط."),parse_mode=ParseMode.HTML,reply_markup=main_menu(True)); return
    if data=="scheduled_list":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        rows=await pending_scheduled_tasks(); kb=[]; lines=[]
        for row in rows[:20]:
            label="واجب" if row["kind"]=="homework" else "امتحان تراكمي" if row["title"].startswith("[تراكمي]") else "امتحان يومي"
            lines.append(f"🆔 {row['id']} | {label}\n📌 {row['title'].replace('[تراكمي] ','')}\n🚀 {row['publish_at'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')} | ⏰ {row['submission_hours']} ساعة")
            kb.append([InlineKeyboardButton(f"❌ إلغاء رقم {row['id']}",callback_data=f"cancelschedule|{row['id']}")])
        kb.append([InlineKeyboardButton("◀️ رجوع",callback_data="admin_publish"),back_menu()])
        text="📭 لا توجد منشورات مجدولة." if not rows else "🗓 المنشورات المجدولة\n\n"+"\n\n".join(lines)
        await query.answer(); await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("cancelschedule|"):
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        schedule_id=int(data.split("|")[1]); removed=await cancel_scheduled_task(schedule_id)
        await query.answer("تم إلغاء الجدولة" if removed else "المنشور نُشر أو أُلغي مسبقاً",show_alert=not removed)
        rows=await pending_scheduled_tasks(); kb=[]; lines=[]
        for row in rows[:20]:
            lines.append(f"🆔 {row['id']} | {row['title'].replace('[تراكمي] ','')}\n🚀 {row['publish_at'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}")
            kb.append([InlineKeyboardButton(f"❌ إلغاء رقم {row['id']}",callback_data=f"cancelschedule|{row['id']}")])
        kb.append([InlineKeyboardButton("◀️ رجوع",callback_data="admin_publish"),back_menu()])
        await query.edit_message_text(bold("📭 لا توجد منشورات مجدولة." if not rows else "🗓 المنشورات المجدولة\n\n"+"\n\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data=="manualmediafinish":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        state=context.user_data.get("manual_task")
        if not state or not state.get("media"): await query.answer("أرسل ملفاً واحداً على الأقل.",show_alert=True); return
        if state.get("kind") in {"exam","cumulative"}:
            await query.answer("الامتحانات تُضاف حصراً من نظام ربط الامتحان بالتحضير.",show_alert=True); return
        state["step"]="target_scope"
        scope_rows=[[InlineKeyboardButton("👥 مسار الدورة",callback_data="manualscope|course",style='success')]]
        scope_rows += [[InlineKeyboardButton(f"📘 الفصل {chapter}",callback_data=f"manualscope|chapter_{chapter}",style='primary')
                        for chapter in range(start,min(start+2,6))] for start in range(1,6,2)]
        scope_rows += [[InlineKeyboardButton("📚 جميع المسارات",callback_data="manualscope|all",style='primary')],
                       [InlineKeyboardButton("❌ إلغاء العملية",callback_data="manualcancel",style='danger')]]
        kb=InlineKeyboardMarkup(scope_rows)
        await query.answer(); await query.edit_message_text(bold(f"✅ اكتملت إضافة {len(state['media'])} ملفات.\nحدد الطلاب المستهدفين بهذا الواجب:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("manualscope|"):
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        state=context.user_data.get("manual_task")
        if not state or state.get("step")!="target_scope": await query.answer("ابدأ إضافة المنشور من جديد.",show_alert=True); return
        state["target_scope"]=data.split("|",1)[1]; state["step"]="publish_time"
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 النشر الآن",callback_data="manualpublishnow")],[InlineKeyboardButton("🗓 جدولة النشر",callback_data="manualschedule")],[InlineKeyboardButton("❌ إلغاء العملية",callback_data="manualcancel")]])
        place="كروب الأحياء والبوت" if state["target_scope"]=="course" else "البوت فقط بدون النشر في المجموعة"
        await query.answer(); await query.edit_message_text(bold(f"✅ تم تحديد المسار.\nطريقة الإرسال: {place}\nمتى تريد نشره؟"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="manualcancel":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        context.user_data.pop("manual_task",None)
        await query.answer("تم الإلغاء"); await query.edit_message_text(bold("❌ تم إلغاء العملية ولم يُنشر شيء."),parse_mode=ParseMode.HTML,reply_markup=main_menu(True)); return
    if data=="manualschedule":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        state=context.user_data.get("manual_task")
        if not state or state.get("step")!="publish_time": await query.answer("ابدأ إضافة المنشور من جديد.",show_alert=True); return
        await query.answer(); await query.edit_message_text(bold("🗓 أرسل موعد النشر ومدة التسليم بهذه الصيغة:\n/publish_at 30/8/2026 18:00 24\n\nالرقم الأخير هو عدد ساعات التسليم ابتداءً من لحظة النشر."),parse_mode=ParseMode.HTML); return
    if data=="manualpublishnow":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        state=context.user_data.get("manual_task")
        if not state or state.get("step")!="publish_time": await query.answer("ابدأ إضافة المنشور من جديد.",show_alert=True); return
        pending=await publish_manual_now(context.bot,state,uid)
        if not pending: await query.answer("تعذر النشر. تحقق من صلاحيات البوت وأرقام Topics.",show_alert=True); return
        context.user_data.pop("manual_task",None)
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("ساعتان",callback_data=f"deadline|{pending['id']}|2"),InlineKeyboardButton("6 ساعات",callback_data=f"deadline|{pending['id']}|6")],[InlineKeyboardButton("12 ساعة",callback_data=f"deadline|{pending['id']}|12"),InlineKeyboardButton("24 ساعة",callback_data=f"deadline|{pending['id']}|24")],[InlineKeyboardButton("48 ساعة",callback_data=f"deadline|{pending['id']}|48"),InlineKeyboardButton("72 ساعة",callback_data=f"deadline|{pending['id']}|72")],[InlineKeyboardButton("🗓 موعد انتهاء مخصص",callback_data=f"customdeadline|{pending['id']}")]])
        place="كروب الأحياء والبوت" if state.get("target_scope")=="course" else "البوت فقط للمسار المحدد"
        await query.answer("تم النشر"); await query.edit_message_text(bold(f"✅ تم تجهيز المطلوب عبر {place}.\n⏰ اختر مدة انتهاء التسليم:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="prep_schedule":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        row=await next_unpublished_preparation()
        info="لا توجد تحاضير قادمة." if not row else f"التحضير القادم: الفصل {row['chapter']} – التحضير {row['chapter_prep_no']}\nالمحاضرات: {row['lectures']}\nالتاريخ: {row['target_date'].strftime('%d/%m/%Y')}"
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("⏩ تقديم موعد",callback_data="prep_shift|advance"),InlineKeyboardButton("⏪ تأخير موعد",callback_data="prep_shift|delay")],[InlineKeyboardButton("🏖 إعلان عطلة وتأجيل الجدول",callback_data="prep_holiday")],[InlineKeyboardButton("📅 تاريخ مخصص",callback_data="prep_custom")],[back_menu()]])
        await query.answer(); await query.edit_message_text(bold(f"🗓 إدارة جدول التحاضير\n\n{info}\n\nإذا لم تغيّر شيئاً يبقى الجدول التلقائي كما هو."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("prep_shift|") or data=="prep_holiday":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        old=await next_unpublished_preparation(); direction="delay" if data in ("prep_shift|delay","prep_holiday") else "advance"
        row=await shift_unpublished_preparations(direction,datetime.now(TIMEZONE).date())
        if row=="too_early": await query.answer("لا يمكن تقديم التحضير إلى اليوم أو إلى تاريخ مضى.",show_alert=True); return
        if not row: await query.answer("لا توجد تحاضير قادمة.",show_alert=True); return
        if data=="prep_holiday" and old:
            try: await context.bot.send_message(BIOLOGY_GROUP_ID,bold(f"🏖 إعلان عطلة\nلا يوجد تحضير يوم {old['target_date'].strftime('%d/%m/%Y')}.\nتم تأجيل الجدول تلقائياً، والتحضير القادم بتاريخ {row['target_date'].strftime('%d/%m/%Y')}."),parse_mode=ParseMode.HTML,message_thread_id=PREPARATION_TOPIC_ID or None)
            except TelegramError: pass
        await query.answer("تم تحديث الجدول"); await query.edit_message_text(bold(f"✅ تم تحديث جميع التحاضير القادمة.\nالموعد الجديد للتحضير القادم: {row['target_date'].strftime('%d/%m/%Y')}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ إدارة الجدول",callback_data="prep_schedule"),back_menu()]])); return
    if data=="prep_custom":
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        await query.answer(); await query.edit_message_text(bold("أرسل في الخاص تاريخ التحضير القادم بهذه الصيغة:\n/prep_date 30/8/2026\nوسيُعاد ترتيب جميع التحاضير التالية تلقائياً."),parse_mode=ParseMode.HTML); return
    if data.startswith(("examallow|","examdeny|","adminexamallow|","adminexamdeny|")):
        action,task_s,student_s=data.split("|"); task_id,student_id=int(task_s),int(student_s); s=await get_student(student_id)
        allowed=(action not in ("examdeny","adminexamdeny"))
        if action in ("adminexamallow","adminexamdeny") and not is_admin(uid): await query.answer("هذا الزر للإدارة فقط.",show_alert=True); return
        if action in ("examallow","examdeny") and student_id not in {x["user_id"] for x in await students_by_parent(uid,True)}: await query.answer("هذا الطلب ليس تابعاً لحسابك.",show_alert=True); return
        await decide_exam_access(task_id,student_id,"approved" if allowed else "denied",uid)
        if allowed:
            task=await activate_exam(task_id,student_id,uid)
            if not task:
                await query.answer("هذا التفعيل غير صالح أو تمت معالجته سابقاً؛ لم يتغير موعد الامتحان.",show_alert=True); return
            if task:
                try: await context.bot.send_message(student_id,bold(f"✅ تمت الموافقة وتفعيل الامتحان.\n📝 {task['title']}\n⏰ آخر موعد: {task['deadline'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}\n\n📚 لن يصلك التحضير التالي حتى تؤدي هذا الامتحان."),parse_mode=ParseMode.HTML)
                except TelegramError: pass
        else:
            try: await context.bot.send_message(student_id,bold("❌ لم تتم الموافقة على تفعيل الامتحان. سيبقى الامتحان مغلقاً حتى تتم الموافقة ثم تفعيله."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        await query.answer("تم تفعيل الامتحان" if allowed else "تم رفض التفعيل",show_alert=True)
        return
    if data.startswith(("extapprove|","extdeny|")):
        action,request_s,student_s=data.split("|"); student_id=int(student_s); s=await get_student(student_id)
        if student_id not in {x["user_id"] for x in await students_by_parent(uid,True)}: await query.answer("هذا الطلب ليس تابعاً لحسابك.",show_alert=True); return
        row=await decide_extension_request(int(request_s),action=="extapprove")
        if not row: await query.answer("تمت معالجة الطلب سابقاً.",show_alert=True); return
        if row.get("already_submitted"):
            await query.answer("الطالب سلّم الامتحان بالفعل؛ أُلغي طلب التمديد.",show_alert=True); return
        await query.answer("تمت الموافقة" if row["status"]=="approved" else "تم الرفض",show_alert=True)
        text=f"✅ وافق ولي الأمر على تمديد الامتحان {row['hours']} ساعة. تم خصم 150 XP." if row["status"]=="approved" else "❌ لم تتم الموافقة على تمديد الامتحان."
        try: await context.bot.send_message(student_id,bold(text),parse_mode=ParseMode.HTML)
        except TelegramError: pass
        return
    if data.startswith(("leaveapprove|","leavedeny|")):
        action,request_s,student_s=data.split("|"); student_id=int(student_s); s=await get_student(student_id)
        if student_id not in {x["user_id"] for x in await students_by_parent(uid,True)}: await query.answer("هذا الطلب ليس تابعاً لحسابك.",show_alert=True); return
        row=await decide_leave_request(int(request_s),action=="leaveapprove")
        if not row: await query.answer("تمت معالجة الطلب سابقاً.",show_alert=True); return
        await query.answer("تمت الموافقة" if row["status"]=="approved" else "تم الرفض",show_alert=True)
        try: await context.bot.send_message(student_id,bold("✅ تمت الموافقة على إجازة يوم كامل وخصم 400 XP." if row["status"]=="approved" else "❌ رفض ولي الأمر طلب الإجازة."),parse_mode=ParseMode.HTML)
        except TelegramError: pass
        return
    if data.startswith("approve|") or data.startswith("reject|"):
        if not is_admin(uid): await query.answer("هذا الزر للأدمن فقط.",show_alert=True); return
        student_id=int(data.split("|")[1]); student=await get_student(student_id)
        if not student: await query.answer("الطلب غير موجود أو سبق رفضه.",show_alert=True); return
        if data.startswith("approve|"):
            if not student.get("parent_chat_id"):
                await query.answer("لا يمكن التفعيل قبل ربط ولي الأمر.",show_alert=True); return
            if not await is_channel_member(context.bot,student_id):
                await query.answer("لا يمكن التفعيل: الطالب غير مشترك بالقناة.",show_alert=True); return
            if not await is_group_member(context.bot,student_id):
                await query.answer("لا يمكن التفعيل: الطالب غير موجود بكروب الأحياء.",show_alert=True); return
            await set_student_approval(student_id,True); await mark_member_compliant(student_id)
            try: await context.bot.send_message(student_id,bold("🎉 وافقت الإدارة على طلبك وتم تفعيل حسابك.\nيمكنك الآن استخدام بوت الأحياء."),parse_mode=ParseMode.HTML,reply_markup=main_menu())
            except TelegramError: pass
            await query.answer("تم تفعيل الطالب"); await query.edit_message_text((query.message.text_html or bold("طلب التفعيل"))+bold("\n\n✅ تم قبول الطالب وتفعيل حسابه."),parse_mode=ParseMode.HTML); return
        await delete_student(student_id)
        try: await context.bot.send_message(student_id,bold("❌ رفضت الإدارة طلب التفعيل.\nيمكنك إعادة التسجيل لاحقاً بعد التواصل مع الإدارة."),parse_mode=ParseMode.HTML)
        except TelegramError: pass
        await query.answer("تم رفض الطلب"); await query.edit_message_text((query.message.text_html or bold("طلب التفعيل"))+bold("\n\n❌ تم رفض الطلب."),parse_mode=ParseMode.HTML); return
    admin=is_admin(uid); student=None if admin else await get_student(uid)
    public=(data in {"menu","today_prep","playlists","study_resources","past_exams"} or data.startswith(("prepopen|","chapter|","lecture|","archivechapter|","archivelecture|","archiveexam|","resourcecategory|booklet","resourcecategory|summary","resourcecategory|ministerial","resourcechapter|booklet","resourcechapter|summary","resourcechapter|ministerial","resourceitem|")))
    if not admin and student and student["approved"] and not student.get("parent_chat_id") and not public:
        await query.answer("يجب ربط ولي الأمر أولاً لفتح خدمات الدورة.",show_alert=True); return
    if not admin and (not student or not student["approved"]) and not public:
        await query.answer("هذه الخدمة مخصصة لطلاب الدورة المفعّلين.",show_alert=True); return
    if not admin and student and student["approved"] and not await is_channel_member(context.bot,uid):
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("📢 الاشتراك بالقناة",url=REQUIRED_CHANNEL_URL)]])
        await query.answer(); await query.edit_message_text(bold("🔒 تم قفل خدمات البوت لأنك غير مشترك بقناة منصة المجتهد."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    await query.answer()
    if data=="menu": await query.edit_message_text(bold("🧪 أكاديمية الأحياء\n━━━━━━━━━━━━━━━━━━\nاختر القسم المطلوب ✨"),parse_mode=ParseMode.HTML,reply_markup=main_menu(admin) if (admin or (student and student["approved"])) else guest_menu()); return
    if data=="account_settings":
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 حسابي",callback_data="profile"),InlineKeyboardButton("👨‍👩‍👦 ولي الأمر",callback_data="parent_link")],
            [InlineKeyboardButton("✏️ تعديل معلوماتي",callback_data="edit_profile")],
            [InlineKeyboardButton("🔄 تغيير فصل البداية / مسار الدراسة",callback_data="change_study_track")],
            [InlineKeyboardButton("🗓️ جدولي الدراسي",callback_data="personal_schedule")],
            [back_menu()]
        ])
        await query.edit_message_text(bold("⚙️ إعدادات الحساب\n\nمن هنا يمكنك تغيير فصل البداية، أو اختيار «أكمل مع الدورة الحالية» للعودة إلى مسار الدورة كما كان قبل التحديث."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="change_study_track":
        await show_onboarding_track(query,True); return
    if data=="personal_schedule":
        sched=await student_schedule(uid); count_used=sched.get("schedule_change_count",0) if sched else 0
        kb=[]
        for n in range(1,8): kb.append([InlineKeyboardButton(f"{n} أيام دراسة بالأسبوع",callback_data=f"sched_count|{n}")])
        kb.append([back_menu()])
        await query.edit_message_text(bold(f"🗓️ إعداد الجدول الشخصي\n\nاختر عدد أيام الدراسة في الأسبوع أولاً.\nبعدها تختار الأيام بنفسك.\n\n🔢 تغييرات الجدول المستخدمة: {count_used}/3"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("sched_count|"):
        n=int(data.split("|")[1]); sched=await student_schedule(uid); used=sched.get("schedule_change_count",0) if sched else 0
        if used>=3: await query.answer("استنفدت مرات تغيير الجدول الثلاث.",show_alert=True); return
        old_days=list(sched.get("study_days") or []) if sched else []
        selected=set(old_days[:n]) if len(old_days)>=n else set(old_days)
        # If the previous set is shorter, fill with the first unused weekdays.
        for j in range(7):
            if len(selected)>=n: break
            selected.add(j)
        context.user_data["schedule_required"]=n; context.user_data["schedule_days"]=sorted(selected)
        names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
        kb=[[InlineKeyboardButton(("☑️ " if j in selected else "☐ ")+names[j],callback_data=f"sched_day|{j}")] for j in range(7)]
        kb += [[InlineKeyboardButton("💾 حفظ الجدول",callback_data="sched_save")],[InlineKeyboardButton("◀️ عدد الأيام",callback_data="personal_schedule")],[back_menu()]]
        await query.answer(); await query.edit_message_text(bold(f"🗓️ اختر أيام الدراسة\n\nالمطلوب: {n} أيام بالضبط.\nالمحدد حاليًا: {len(selected)}/{n}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("sched_day|"):
        i=int(data.split("|")[1]); days=set(context.user_data.get("schedule_days",[])); required=int(context.user_data.get("schedule_required",len(days) or 1))
        if i in days: days.remove(i)
        elif len(days)<required: days.add(i)
        else: await query.answer(f"اختر {required} أيام فقط.",show_alert=True); return
        context.user_data["schedule_days"]=sorted(days); names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
        kb=[[InlineKeyboardButton(("☑️ " if j in days else "☐ ")+names[j],callback_data=f"sched_day|{j}")] for j in range(7)]
        kb += [[InlineKeyboardButton("💾 حفظ الجدول",callback_data="sched_save")],[InlineKeyboardButton("◀️ عدد الأيام",callback_data="personal_schedule")],[back_menu()]]
        await query.answer(); await query.edit_message_text(bold(f"🗓️ اختر أيام الدراسة\n\nالمطلوب: {required} أيام بالضبط.\nالمحدد: {len(days)}/{required}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data=="sched_save":
        days=context.user_data.get("schedule_days",[]); required=int(context.user_data.get("schedule_required",len(days)))
        if len(days)!=required: await query.answer(f"يجب اختيار {required} أيام بالضبط.",show_alert=True); return
        result=await set_student_schedule(uid,days,"custom")
        if result.get("status")=="limit": await query.answer("استنفدت مرات تغيير الجدول الثلاث.",show_alert=True); return
        if result.get("status")!="ok": await query.answer("تعذر حفظ الجدول.",show_alert=True); return
        context.user_data.pop("schedule_days",None); context.user_data.pop("schedule_required",None)
        names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
        await query.answer("تم حفظ الجدول",show_alert=True)
        await query.edit_message_text(bold(f"✅ تم حفظ جدولك الشخصي.\n\n📅 أيام الدراسة: {', '.join(names[i] for i in days)}\n📚 كل يوم دراسي = تحضير واحد.\n🔢 التغييرات المستخدمة: {result['count']}/3\n\nيمكنك دراسة التحضير التالي للتعويض، لكن ترتيب التحاضير لا يتغير."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗓️ تعديل الجدول",callback_data="personal_schedule")],[back_menu()]])); return
    if data=="sched_regular":
        result=await reset_personal_schedule_to_regular(uid)
        if not result: await query.answer("استنفدت مرات تغيير الجدول الثلاث.",show_alert=True); return
        await query.answer("تمت العودة للجدول المنتظم",show_alert=True)
        await query.edit_message_text(bold("✅ تمت العودة إلى الجدول المنتظم."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data=="schedules_menu":
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("📚 جدول إكمال الفصول",callback_data="chapter_completion_schedule")],[InlineKeyboardButton("🗓 جدول هذا الأسبوع",callback_data="weekly_schedule")],[back_menu()]])
        await query.edit_message_text(bold("🗓 الجداول الدراسية\nاختر الجدول المطلوب:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="chapter_completion_schedule":
        finish=await student_finish_date(uid)
        student=await get_student(uid)
        track="الدورة الحالية" if student and student.get("study_track")=="course" else f"الفصل {student.get('current_chapter')}" if student else "-"
        value=finish.strftime("%d/%m/%Y") if finish else "لا يوجد موعد فعلي حالياً"
        await query.edit_message_text(bold(f"📅 متى سوف ننهي المنهج؟\n{DIV}\n📚 مسارك: {track}\n🏁 موعد إكمال المنهج حسب جدول البوت الفعلي: {value}\n\nℹ️ هذا التاريخ يُحسب من مواعيد التحاضير الفعلية داخل البوت، ويتحرك تلقائياً عند تغيير الجدول أو إجازات الطالب."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحديث الموعد",callback_data="chapter_completion_schedule")],[InlineKeyboardButton("◀️ الجداول",callback_data="schedules_menu"),back_menu()]])); return
    if data=="achievement_menu":
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("☀️ اليوم",callback_data="achievement|day"),InlineKeyboardButton("📅 الأسبوع",callback_data="achievement|week"),InlineKeyboardButton("🗓 الشهر",callback_data="achievement|month")],[back_menu()]])
        await query.edit_message_text(bold("🏅 إنجازي\nاختر الفترة التي تريد عرضها:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="xp_store":
        s=await get_student(uid); kb=InlineKeyboardMarkup([[InlineKeyboardButton("⏳ تمديد امتحان — 150 XP",callback_data="exams_menu")],[InlineKeyboardButton("🏖 إجازة يوم كامل — 400 XP",callback_data="leave_menu")],[InlineKeyboardButton("⚠️ فك إنذار — 500 XP",callback_data="buy_unwarn")],[back_menu()]])
        await query.edit_message_text(bold(f"⭐ رصيدك: {s['xp']} XP\n━━━━━━━━━━━━━━━━━━\nاختر الميزة المطلوبة:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="buy_unwarn":
        result=await buy_remove_warning(uid)
        if result=="none": await query.answer("ما عندك إنذارات حتى تفكها.",show_alert=True); return
        if result=="xp": await query.answer("تحتاج 500 XP.",show_alert=True); return
        await query.edit_message_text(bold("✅ تم خصم 500 XP وفك إنذار واحد."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data.startswith("achievement|"):
        period=data.split("|")[1]; now=datetime.now(TIMEZONE)
        start=now.replace(hour=0,minute=0,second=0,microsecond=0) if period=="day" else (now-timedelta(days=7) if period=="week" else now.replace(day=1,hour=0,minute=0,second=0,microsecond=0))
        a=await student_achievements(uid,start); label={"day":"اليومي","week":"الأسبوعي","month":"الشهري"}[period]
        await query.edit_message_text(bold(f"🏅 إنجازي {label}\n━━━━━━━━━━━━━━━━━━\n🎬 محاضرات مكتملة: {a['lectures']}\n📚 واجبات مسلّمة: {a['homeworks']}\n📝 امتحانات: {a['exams']}\n📊 معدل الامتحانات: {a['average'] if a['average'] is not None else '-'}\n⭐ صافي XP: {a['xp_earned']}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الفترات",callback_data="achievement_menu"),back_menu()]])); return
    if data=="weekly_schedule":
        today=datetime.now(TIMEZONE).date(); start=today-timedelta(days=today.weekday()); end=start+timedelta(days=6); preps,tasks=await weekly_schedule(start,end)
        lines=[f"🗓 جدول هذا الأسبوع\n{start.strftime('%d/%m')} — {end.strftime('%d/%m')}",DIV]
        for p in preps: lines.append(f"🎬 {p['target_date'].strftime('%d/%m')} | الفصل {p['chapter']} | المحاضرات {p['lectures']}")
        for t in tasks: lines.append(f"{'📚' if t['kind']=='homework' else '📝'} {t['publish_at'].astimezone(TIMEZONE).strftime('%d/%m %H:%M')} | {t['title']}")
        if len(lines)==2: lines.append("لا توجد عناصر مجدولة حالياً.")
        await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data=="leave_menu":
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("اليوم",callback_data="leave|0"),InlineKeyboardButton("غداً",callback_data="leave|1")],[InlineKeyboardButton("📅 تاريخ آخر",callback_data="leavecustom")],[back_menu()]])
        await query.edit_message_text(bold("🏖 طلب إجازة يوم كامل\nالكلفة 400 XP، ولا تخصم إلا بعد موافقة ولي الأمر. الإجازة تعفيك من جميع مطلوبات ذلك اليوم."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("leave|"):
        leave_date=datetime.now(TIMEZONE).date()+timedelta(days=int(data.split("|")[1])); result=await create_leave_request(uid,leave_date)
        if result["status"]=="xp": await query.answer("تحتاج 400 XP لطلب الإجازة.",show_alert=True); return
        if result["status"]!="ok": await query.answer("يوجد طلب لهذا اليوم مسبقاً.",show_alert=True); return
        s=result["student"]; req=result["request"]
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم",callback_data=f"leaveapprove|{req['id']}|{uid}"),InlineKeyboardButton("❌ لا",callback_data=f"leavedeny|{req['id']}|{uid}")]])
        try: await context.bot.send_message(s["parent_chat_id"],bold(f"🏖 طلب إجازة\nالطالب {s['full_name']} طلب إجازة من جميع مطلوبات يوم {leave_date.strftime('%d/%m/%Y')}.\nالكلفة 400 XP. هل توافق؟"),parse_mode=ParseMode.HTML,reply_markup=kb)
        except TelegramError: pass
        await query.edit_message_text(bold("⏳ أرسل طلب الإجازة إلى ولي أمرك."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data=="leavecustom":
        context.user_data["awaiting_date"]="leave"; await query.edit_message_text(bold("📅 أرسل تاريخ الإجازة بهذه الصيغة: 30/8/2026"),parse_mode=ParseMode.HTML); return
    if data=="profile_change_chapter":
        if admin:
            await query.answer("هذا الخيار للطلاب فقط.",show_alert=True); return
        await query.answer()
        await query.edit_message_text(bold("📚 تغيير فصل البداية\n\nاختر الفصل الذي تريد أن يبدأ منه جدولك الجديد.\nسيُعاد بناء التحضير من تاريخ اليوم، وتبقى بقية ميزات حسابك كما هي."),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📘 1",callback_data="profilechapter|1"),InlineKeyboardButton("📗 2",callback_data="profilechapter|2"),InlineKeyboardButton("📙 3",callback_data="profilechapter|3")],
                [InlineKeyboardButton("📕 4",callback_data="profilechapter|4"),InlineKeyboardButton("📒 5",callback_data="profilechapter|5")],
                [InlineKeyboardButton("◀️ الملف الشخصي",callback_data="profile"),back_menu()]
            ]))
        return
    if data.startswith("profilechapter|"):
        if admin:
            await query.answer("هذا الخيار للطلاب فقط.",show_alert=True); return
        chapter=int(data.split("|",1)[1])
        if chapter not in range(1,6):
            await query.answer("الفصل غير متاح.",show_alert=True); return
        plan=build_personal_plan(chapter,datetime.now(TIMEZONE).date())
        await set_student_onboarding(uid,"chapter",chapter,datetime.now(TIMEZONE).date(),plan)
        await query.answer("تم تغيير فصل البداية")
        await query.edit_message_text(bold(f"✅ تم تغيير فصل البداية إلى الفصل {chapter}.\n\nتم إعادة بناء جدول التحضير من اليوم وفق التوزيع المعتمد، ولن تتأثر درجاتك أو إنذاراتك أو نقاطك أو باقي ميزات الحساب."),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 الملف الشخصي",callback_data="profile")],[back_menu()]]))
        return
    if data=="edit_profile":
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("الاسم",callback_data="editfield|full_name"),InlineKeyboardButton("المدرسة",callback_data="editfield|school")],[InlineKeyboardButton("المعدل المطلوب",callback_data="editfield|target_grade")],[back_menu()]])
        await query.edit_message_text(bold("✏️ اختر المعلومة التي تريد تعديلها:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("editfield|"):
        context.user_data["edit_field"]=data.split("|")[1]; await query.edit_message_text(bold("أرسل الآن المعلومة الجديدة في رسالة واحدة."),parse_mode=ParseMode.HTML); return
    if data=="today_prep": await show_today_preparation(query); return
    if data.startswith("prepopen|"):
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s); item=PLAYLISTS[chapter][lecture-1]
        progress=await lecture_progress(uid,chapter,lecture)
        if progress and progress.get("completed_at"):
            await query.answer("تم تسجيل دراستك لهذه المحاضرة مسبقاً.",show_alert=True); return
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ مشاهدة محاضرة البوت",callback_data=f"prepwatch|{chapter}|{lecture}")],[InlineKeyboardButton("📚 لقد درست هذه المحاضرة",callback_data=f"prepprivate|{chapter}|{lecture}")],[back_menu()]])
        await query.edit_message_text(bold(f"🎬 الفصل {chapter} | المحاضرة {lecture}\n{item[1]}\n\nاختر طريقة دراستك للمحاضرة:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("prepwatch|"):
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s); item=PLAYLISTS[chapter][lecture-1]
        if student and student["approved"]: await mark_lecture_progress(uid,chapter,lecture,False)
        kb=InlineKeyboardMarkup(biology_video_buttons(chapter,lecture)+[[InlineKeyboardButton("✅ أنهيت المحاضرة",callback_data=f"prepcomplete|{chapter}|{lecture}")],[back_menu()]])
        await query.edit_message_text(bold(f"🎬 الفصل {chapter} | المحاضرة {lecture}\n{item[1]}\n\nبعد المشاهدة ارجع واضغط «أنهيت المحاضرة»."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("prepprivate|"):
        if admin: return
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s)
        progress=await lecture_progress(uid,chapter,lecture)
        if progress and progress.get("completed_at"):
            await query.answer("تم تسجيل دراستك لهذه المحاضرة مسبقاً.",show_alert=True); return
        context.user_data["awaiting_private_study_oath"]={"chapter":chapter,"lecture":lecture}
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ إلغاء والعودة",callback_data=f"prepopen|{chapter}|{lecture}")]])
        await query.edit_message_text(bold("📚 إثبات الدراسة من مصدر خاص\n\nانسخ القسم التالي حرفياً ثم أرسله إلى البوت في رسالة واحدة:\n\n")+f"<code>{escape(PRIVATE_STUDY_OATH)}</code>",parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("prepcomplete|"):
        if admin: return
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s)
        progress=await lecture_progress(uid,chapter,lecture)
        if progress and progress.get("completed_at"):
            await query.answer("تم تسجيل مشاهدتك للمحاضرة بنجاح مسبقاً.",show_alert=True); return
        if not progress or not progress.get("opened_at"):
            await query.answer("افتح المحاضرة أولاً ثم ارجع بعد مشاهدتها.",show_alert=True); return
        elapsed=(datetime.now(progress["opened_at"].tzinfo)-progress["opened_at"]).total_seconds()
        required=MIN_LECTURE_WATCH_MINUTES*60
        if elapsed<required:
            remain=max(1,int((required-elapsed+59)//60)); await query.answer(f"لا يمكن اعتمادها الآن. بقي نحو {remain} دقيقة من وقت التحقق.",show_alert=True); return
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم، شاهدتها كاملة",callback_data=f"prepverify|{chapter}|{lecture}")],[InlineKeyboardButton("↩️ العودة للمحاضرة",callback_data=f"prepopen|{chapter}|{lecture}")]])
        await query.edit_message_text(bold("🔍 تحقق الإكمال\nهل شاهدت المحاضرة كاملة وفهمت أفكارها الأساسية؟\nسيُسجل هذا الإقرار في متابعتك الدراسية."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("prepverify|"):
        if admin: return
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s)
        progress=await lecture_progress(uid,chapter,lecture)
        if progress and progress.get("completed_at"):
            await query.answer("تم تسجيل مشاهدتك للمحاضرة بنجاح مسبقاً.",show_alert=True); return
        await mark_lecture_progress(uid,chapter,lecture,True)
        await linked_exam_dispatch_job(context)
        backlog_done=await complete_backlog(uid,chapter,lecture)
        prep_award=await award_daily_preparation(uid,chapter,lecture)
        rows=[]
        if backlog_done: rows.append([InlineKeyboardButton("📝 نعم، جاهز للامتحان",callback_data=f"backlogexam|{chapter}|{lecture}")])
        rows.append([back_menu()])
        extra="\n\n🎉 مبروك! أنهيت محاضرة متراكمة وحولت جزءاً من التأخير إلى إنجاز حقيقي. هل أنت جاهز لامتحانها؟" if backlog_done else ""
        xp_text="\n⭐ حصلت على 15 XP لإكمال التحضير اليومي كاملاً." if prep_award and prep_award.get("awarded") else ""
        await query.edit_message_text(bold(f"✅ أحسنت! تم تسجيل إكمال الفصل {chapter} – المحاضرة {lecture}.{xp_text}{extra}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))
        s=await get_student(uid)
        for p in await student_parents(uid,True):
            try: await context.bot.send_message(p["parent_chat_id"],bold(f"🌟 إنجاز دراسي جديد\nأكمل الطالب {s['full_name']} محاضرة الأحياء رقم {lecture} من الفصل {chapter}."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        return
    if data=="backlog_auto":
        items=await unwatched_lectures_for_student(uid,datetime.now(TIMEZONE))
        if not items:
            await query.edit_message_text(bold("🎉 لا توجد لديك محاضرات متراكمة حالياً.\n\nكل المحاضرات المستحقة لك مسجلة كمكتملة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
        kb=[]
        for item in items:
            label=f"📘 ف{item['chapter']} — م{item['lecture']} | تحضير {item['prep_no']}"
            kb.append([InlineKeyboardButton(label,callback_data=f"backlogview|{item['chapter']}|{item['lecture']}")])
        kb.append([back_menu()])
        await query.edit_message_text(bold(f"📚 المحاضرات المتراكمة\n━━━━━━━━━━━━━━━━━━\nهذه كل المحاضرات المستحقة التي لم تسجل إكمالها بعد.\n\nعددها: {len(items)}\n\nأكمل أي محاضرة بمشاهدتها من البوت أو بإرسال القسم المخصص للدراسة من مصدر خاص. عند تسجيل الإكمال تُحذف تلقائياً من هذه القائمة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("backlogview|"):
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s)
        progress=await lecture_progress(uid,chapter,lecture)
        if progress and progress.get("completed_at"):
            await query.answer("هذه المحاضرة مكتملة مسبقاً.",show_alert=True); await query.edit_message_text(bold("🎉 تم إكمال هذه المحاضرة مسبقاً، لذلك أزيلت من التراكمات."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📚 المحاضرات المتراكمة",callback_data="backlog_auto"),back_menu()]])); return
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ مشاهدة المحاضرة",callback_data=f"prepwatch|{chapter}|{lecture}")],
            [InlineKeyboardButton("✍️ أكملت تراكمي",callback_data=f"backlogoath|{chapter}|{lecture}")],
            [InlineKeyboardButton("◀️ المحاضرات المتراكمة",callback_data="backlog_auto"),back_menu()]
        ])
        item=PLAYLISTS[chapter][lecture-1]
        await query.edit_message_text(bold(f"📚 محاضرة متراكمة\n\n📘 الفصل {chapter}\n🎬 المحاضرة {lecture}\n{item[1]}\n\nيمكنك إكمالها بمشاهدتها من البوت، أو الضغط على «أكملت تراكمي» لإرسال القسم الجاهز للنسخ."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("backlogoath|"):
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s)
        context.user_data["awaiting_private_study_oath"]={"chapter":chapter,"lecture":lecture,"from_backlog":True}
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ العودة للتراكمات",callback_data="backlog_auto"),back_menu()]])
        await query.edit_message_text(bold("✍️ إكمال محاضرة متراكمة من مصدر خاص\n\nانسخ القسم التالي حرفياً ثم أرسله إلى البوت في رسالة واحدة:\n\n")+f"<code>{escape(PRIVATE_STUDY_OATH)}</code>",parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="backlog_menu":
        items=await backlog_items(uid)
        kb=[[InlineKeyboardButton(f"📘 الفصل {i}",callback_data=f"backlogchapter|{i}")] for i in range(1,6)]
        kb.append([InlineKeyboardButton(f"🗓 تحديد موعد إنهاء التراكم ({len(items)})",callback_data="backlogplan")])
        kb.append([back_menu()])
        await query.edit_message_text(bold(f"📈 حل تراكماتي\n━━━━━━━━━━━━━━━━━━\nاختر الفصل ثم اضغط المحاضرات المتراكمة عليك.\nعدد المحاضرات المسجلة حالياً: {len(items)}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("backlogchapter|"):
        chapter=int(data.split("|")[1]); selected={(x["chapter"],x["lecture"]) for x in await backlog_items(uid)}; lectures=PLAYLISTS[chapter]; kb=[]
        for i in range(0,len(lectures),3):
            kb.append([InlineKeyboardButton(("✅ " if (chapter,x[0]) in selected else "➕ ")+str(x[0]),callback_data=f"backlogtoggle|{chapter}|{x[0]}") for x in lectures[i:i+3]])
        kb.append([InlineKeyboardButton("◀️ رجوع للفصول",callback_data="backlog_menu"),back_menu()])
        await query.edit_message_text(bold(f"📘 الفصل {chapter}\nاضغط رقم المحاضرة لإضافتها أو إزالتها من تراكماتك:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("backlogtoggle|"):
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s); await toggle_backlog(uid,chapter,lecture)
        selected={(x["chapter"],x["lecture"]) for x in await backlog_items(uid)}; lectures=PLAYLISTS[chapter]; kb=[]
        for i in range(0,len(lectures),3): kb.append([InlineKeyboardButton(("✅ " if (chapter,x[0]) in selected else "➕ ")+str(x[0]),callback_data=f"backlogtoggle|{chapter}|{x[0]}") for x in lectures[i:i+3]])
        kb.append([InlineKeyboardButton("◀️ رجوع للفصول",callback_data="backlog_menu"),back_menu()])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb)); return
    if data=="backlogplan":
        rows=await backlog_items(uid)
        if not rows:
            await query.edit_message_text(bold("📭 لم تضف أي محاضرة متراكمة بعد."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ إضافة تراكمات",callback_data="backlog_menu"),back_menu()]])); return
        context.user_data["awaiting_date"]="backlog"; await query.edit_message_text(bold(f"📅 عندك {len(rows)} محاضرة متراكمة. أرسل التاريخ الذي تريد إنهاء جميع التراكمات قبله بهذه الصيغة: 30/9/2026\n\nالبوت سيوزعها تلقائياً بأيام متوازنة وبدون ضغط."),parse_mode=ParseMode.HTML); return
    if data=="exams_menu":
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("📝 الامتحانات",callback_data="tasks|exam|all")],[InlineKeyboardButton("🏆 الامتحانات التراكمية",callback_data="tasks|exam|cumulative")],[InlineKeyboardButton("🗂 الامتحانات السابقة",callback_data="past_exams")],[back_menu()]])
        await query.edit_message_text(bold("📝 قسم الامتحانات\nاختر القسم المطلوب:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("tasks|"):
        parts=data.split("|"); await show_tasks(query,parts[1],parts[2] if len(parts)>2 else None); return
    if data.startswith("task|"): await show_task(query,context,int(data.split("|")[1])); return
    if data.startswith("submit|"):
        context.user_data["waiting_submission"]=int(data.split("|")[1]); await context.bot.send_message(uid,bold("📤 أرسل الآن صور أو ملف الحل في رسالة واحدة."),parse_mode=ParseMode.HTML); return
    if data.startswith("extend|"):
        task_id=int(data.split("|")[1]); task=await get_task(task_id)
        if not task or task["kind"]!="exam": await query.answer("التمديد متاح للامتحانات فقط.",show_alert=True); return
        kb=[]
        for start in range(1,25,4):
            kb.append([InlineKeyboardButton(f"{hours} ساعة",callback_data=f"extendhours|{task_id}|{hours}") for hours in range(start,min(start+4,25))])
        kb.append([InlineKeyboardButton("◀️ رجوع للامتحان",callback_data=f"task|{task_id}"),back_menu()])
        await query.edit_message_text(bold("⏳ اختر مدة تمديد الامتحان\nيمكنك استعمال التمديد مرتين فقط خلال الأسبوع:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("extendhours|"):
        _,task_s,hours_s=data.split("|"); task_id,hours=int(task_s),int(hours_s)
        if not 1<=hours<=24: await query.answer("مدة التمديد غير صحيحة.",show_alert=True); return
        task=await get_task(task_id)
        if not task or task["kind"]!="exam": await query.answer("التمديد متاح للامتحانات فقط.",show_alert=True); return
        result=await create_extension_request(task_id,uid,hours)
        if result["status"]=="xp": await query.answer("تحتاج 150 XP لطلب التمديد.",show_alert=True); return
        if result["status"]=="limit": await query.answer("استخدمت طلبي التمديد لهذا الأسبوع.",show_alert=True); return
        if result["status"]=="exists": await query.answer("يوجد طلب تمديد لهذا الامتحان.",show_alert=True); return
        if result["status"]=="submitted": await query.answer("أنت سلّمت هذا الامتحان بالفعل ولا تحتاج إلى تمديد.",show_alert=True); return
        if result["status"]=="parent_missing": await query.answer("لا يوجد ولي أمر مربوط بحسابك لاستلام الموافقة.",show_alert=True); return
        if result["status"]!="ok": await query.answer("لا يمكن طلب التمديد الآن.",show_alert=True); return
        s=result["student"]; req=result["request"]
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم",callback_data=f"extapprove|{req['id']}|{uid}"),InlineKeyboardButton("❌ لا",callback_data=f"extdeny|{req['id']}|{uid}")]])
        try:
            await context.bot.send_message(s["parent_chat_id"],bold(f"⏳ طلب تمديد امتحان\nالطالب {s['full_name']} طلب تمديد «{task['title']}» لمدة {hours} ساعة.\nالكلفة: 150 XP. هل توافق؟"),parse_mode=ParseMode.HTML,reply_markup=kb)
        except TelegramError as exc:
            await decide_extension_request(req["id"],False)
            logger.warning("Extension request delivery failed for student %s: %s",uid,exc)
            await query.answer("تعذر إيصال الطلب إلى ولي الأمر. اطلب منه فتح البوت ثم حاول مجدداً.",show_alert=True); return
        await query.edit_message_text(bold("⏳ أُرسل طلب التمديد إلى ولي أمرك. لن يُخصم 150 XP إلا بعد موافقته."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data.startswith("backlogexam|"):
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s); rows=await archive_exams_by_lecture(chapter,lecture)
        if not rows: await query.answer("لا يوجد امتحان سابق مربوط بهذه المحاضرة حالياً.",show_alert=True); return
        exam=rows[0]; media=await get_archive_exam_media(exam["id"])
        await query.edit_message_text(bold(f"📝 امتحان المحاضرة {lecture} — الفصل {chapter}\n{exam['title']}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]]))
        for index,item in enumerate(media):
            caption=bold(exam["title"]) if index==0 else None
            if item["payload_type"]=="photo": await context.bot.send_photo(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
            elif item["payload_type"]=="document": await context.bot.send_document(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
            else: await context.bot.send_video(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
        return
    if data=="past_exams":
        kb=InlineKeyboardMarkup([[InlineKeyboardButton(f"📘 الفصل {i}",callback_data=f"archivechapter|{i}")] for i in range(1,6)]+[[back_menu()]])
        await query.edit_message_text(bold("🗂 الامتحانات السابقة\nاختر الفصل:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("archivechapter|"):
        chapter=int(data.split("|")[1]); lectures=await archive_lectures(chapter); legacy=[r for r in await archive_exams(chapter) if not r.get("lecture")]
        kb=[[InlineKeyboardButton(f"🎬 المحاضرة {lecture}",callback_data=f"archivelecture|{chapter}|{lecture}")] for lecture in lectures]
        kb.extend([[InlineKeyboardButton(f"📝 {row['title']}",callback_data=f"archiveexam|{row['id']}")] for row in legacy])
        if admin: kb.append([InlineKeyboardButton("➕ إضافة امتحان سابق",callback_data=f"addarchive|{chapter}")])
        kb.append([InlineKeyboardButton("◀️ الفصول",callback_data="past_exams"),back_menu()])
        text=f"📘 امتحانات الفصل {chapter}\nاختر المحاضرة:" if (lectures or legacy) else f"📭 لا توجد امتحانات مضافة للفصل {chapter}."
        await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("archivelecture|"):
        _,chapter_s,lecture_s=data.split("|"); chapter,lecture=int(chapter_s),int(lecture_s); rows=await archive_exams_by_lecture(chapter,lecture)
        kb=[[InlineKeyboardButton(f"📝 {row['title']}",callback_data=f"archiveexam|{row['id']}")] for row in rows]
        kb.append([InlineKeyboardButton("◀️ المحاضرات",callback_data=f"archivechapter|{chapter}"),back_menu()])
        await query.edit_message_text(bold(f"🎬 الفصل {chapter} — المحاضرة {lecture}\nاختر الامتحان:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("archiveexam|"):
        archive_id=int(data.split("|")[1]); exam=await get_archive_exam(archive_id); media=await get_archive_exam_media(archive_id)
        if not exam: await query.answer("الامتحان غير موجود",show_alert=True); return
        await query.edit_message_text(bold(f"📝 {exam['title']}\n📘 الفصل {exam['chapter']}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ رجوع",callback_data=f"archivechapter|{exam['chapter']}"),back_menu()]]))
        for index,item in enumerate(media):
            caption=bold(exam["title"]) if index==0 else None
            if item["payload_type"]=="photo": await context.bot.send_photo(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
            elif item["payload_type"]=="document": await context.bot.send_document(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
            elif item["payload_type"]=="video": await context.bot.send_video(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
        return
    if data=="study_resources":
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 الملازم",callback_data="resourcecategory|booklet"),InlineKeyboardButton("📝 الملخصات",callback_data="resourcecategory|summary")],
            [InlineKeyboardButton("🏛 الأسئلة الوزارية",callback_data="resourcecategory|ministerial")],
            [back_menu()],
        ])
        await query.edit_message_text(bold("📚 اختر القسم المطلوب:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("resourcecategory|"):
        category=data.split("|")[1]
        kb=[[InlineKeyboardButton(f"📘 الفصل {i}",callback_data=f"resourcechapter|{category}|{i}")] for i in range(1,6)]
        kb.append([InlineKeyboardButton("◀️ رجوع",callback_data="study_resources" if category!="model_answer" else "menu"),back_menu()])
        await query.edit_message_text(bold(f"{RESOURCE_LABELS[category]}\nاختر الفصل:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("resourcechapter|"):
        _,category,chapter_s=data.split("|"); chapter=int(chapter_s); rows=await resources_by_chapter(category,chapter)
        kb=[[InlineKeyboardButton(f"📄 {row['title']}",callback_data=f"resourceitem|{row['id']}")] for row in rows]
        if admin: kb.append([InlineKeyboardButton("➕ إضافة ملف جديد",callback_data=f"addresource|{category}|{chapter}")])
        kb.append([InlineKeyboardButton("◀️ الفصول",callback_data=f"resourcecategory|{category}"),back_menu()])
        text=f"{RESOURCE_LABELS[category]} | الفصل {chapter}" if rows else f"📭 لا توجد ملفات في الفصل {chapter}."
        await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("resourceitem|"):
        resource_id=int(data.split("|")[1]); resource=await get_resource(resource_id); media=await get_resource_media(resource_id)
        if not resource: return
        if resource["category"]=="model_answer" and not (admin or (student and student["approved"])):
            await query.answer("الإجابات النموذجية مخصصة لطلاب الدورة.",show_alert=True); return
        await query.edit_message_text(bold(f"📄 {resource['title']}\n📘 الفصل {resource['chapter']}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ رجوع",callback_data=f"resourcechapter|{resource['category']}|{resource['chapter']}"),back_menu()]]))
        for index,item in enumerate(media):
            caption=bold(resource["title"]) if index==0 else None
            if item["payload_type"]=="photo": await context.bot.send_photo(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
            elif item["payload_type"]=="document": await context.bot.send_document(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
            elif item["payload_type"]=="video": await context.bot.send_video(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
        return
    if data=="playlists":
        kb=InlineKeyboardMarkup([[InlineKeyboardButton(f"📘 الفصل {i}",callback_data=f"chapter|{i}")] for i in range(1,6)]+[[back_menu()]])
        await query.edit_message_text(bold("🎬 اختر الفصل:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("chapter|"):
        ch=int(data.split("|")[1]); lectures=PLAYLISTS[ch]; kb=[]
        for i in range(0,len(lectures),4): kb.append([InlineKeyboardButton(str(x[0]),callback_data=f"lecture|{ch}|{x[0]}") for x in lectures[i:i+4]])
        kb.append([InlineKeyboardButton("◀️ الفصول",callback_data="playlists"),back_menu()]); await query.edit_message_text(bold(f"🎬 الفصل {ch} | اختر المحاضرة:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("lecture|"):
        _,ch_s,no_s=data.split("|"); ch,no=int(ch_s),int(no_s); item=PLAYLISTS[ch][no-1]
        text=bold(f"🎬 المحاضرة رقم ({no})\n📌 موضوع المحاضرة:\n{item[1]}")
        rows=[[InlineKeyboardButton("▶️ فتح المحاضرة",url=item[2],style='success')]]
        for title,url in LECTURE_SUPPLEMENTS.get((ch,no),[]):
            rows.append([InlineKeyboardButton(f"📎 {title}",url=url,style='primary')])
        rows.append([InlineKeyboardButton("◀️ رجوع للمحاضرات",callback_data=f"chapter|{ch}",style='primary'),back_menu()])
        kb=InlineKeyboardMarkup(rows)
        await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="profile":
        if admin:
            await query.edit_message_text(bold("👑 حساب إداري معتمد\nلا يحتاج إلى تسجيل أو موافقة تفعيل."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
        s=await get_student(uid)
        track = "الدورة الحالية" if s.get("study_track")=="course" else f"الفصل {s.get('current_chapter') or '-'}"
        sched=await student_schedule(uid)
        names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
        schedule_text="منتظم" if not sched or sched.get("schedule_mode")!="custom" else f"شخصي — {', '.join(names[i] for i in (sched.get('study_days') or []))}"
        text=bold(f"👤 ملفي الشخصي\n\nالاسم: {s['full_name']}\nالمدرسة: {s['school']}\nالمعدل المطلوب: {s['target_grade']}\n📚 مسار الدراسة: {track}\n🗓️ نظام الجدول: {schedule_text}\n\n⚠️ إنذاراتي: {s['warnings']}/{MAX_WARNINGS}\n⭐ نقاطي: {s['xp']} XP")
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("📚 تغيير فصل البداية",callback_data="profile_change_chapter")],
                                 [InlineKeyboardButton("✏️ تعديل المعلومات",callback_data="edit_profile")],[back_menu()]])
        await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data=="parent_link":
        if admin:
            await query.edit_message_text(bold("👑 حساب الإدارة لا يحتاج إلى ربط ولي أمر."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
        s=await get_student(uid)
        status="✅ تم ربط ولي الأمر" if s.get("parent_chat_id") else "⏳ لم يُربط ولي الأمر بعد"
        await query.edit_message_text(bold(f"👨‍👩‍👦 ربط ولي الأمر\n\nرمزك الخاص: {s['parent_link_code']}\n\nأرسل هذا الأمر لولي أمرك حتى ينسخه ويرسله:")+f"\n<code>/parent {escape(s['parent_link_code'])}</code>\n"+bold(status),parse_mode=ParseMode.HTML,reply_markup=parent_copy_markup(s["parent_link_code"],True)); return
    if data=="cumulative":
        exam=await get_cumulative_exam()
        text=bold("📭 لم يحدد الأستاذ موعد الامتحان التراكمي بعد.") if not exam else bold(f"🏆 الامتحان التراكمي\n\n📅 الموعد: {exam['exam_at'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}\n📚 المادة الداخلة: {exam['syllabus']}")
        await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return


async def private_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "registration" in context.user_data: return
    refresh=context.user_data.get("profile_refresh")
    if refresh:
        value=(update.message.text or "").strip(); step=refresh["step"]
        if step=="full_name":
            if len(value.split())<3: await update.effective_message.reply_text(bold("⚠️ أرسل اسماً ثلاثياً أو رباعياً صحيحاً."),parse_mode=ParseMode.HTML); return
            refresh["full_name"]=value; refresh["step"]="school"
            await update.effective_message.reply_text(bold("🏫 أرسل اسم المدرسة الجديد:"),parse_mode=ParseMode.HTML); return
        if step=="school":
            if len(value)<2: await update.effective_message.reply_text(bold("⚠️ أرسل اسم مدرسة صحيحاً."),parse_mode=ParseMode.HTML); return
            refresh["school"]=value; refresh["step"]="target_grade"
            await update.effective_message.reply_text(bold("🎯 أرسل المعدل الذي تريد الحصول عليه:"),parse_mode=ParseMode.HTML); return
        if not value: await update.effective_message.reply_text(bold("⚠️ أرسل المعدل المطلوب."),parse_mode=ParseMode.HTML); return
        await update_student_profile(update.effective_user.id,"full_name",refresh["full_name"])
        await update_student_profile(update.effective_user.id,"school",refresh["school"])
        await update_student_profile(update.effective_user.id,"target_grade",value)
        context.user_data.pop("profile_refresh",None)
        await update.effective_message.reply_text(bold("✅ تم تحديث الاسم والمدرسة والمعدل المطلوب."),parse_mode=ParseMode.HTML)
        await show_onboarding_track(update.message); return
    oath_state=context.user_data.get("awaiting_private_study_oath")
    if oath_state:
        received=(update.message.text or "").strip()
        if received!=PRIVATE_STUDY_OATH:
            await update.effective_message.reply_text(bold("⚠️ يجب إرسال القسم حرفياً بدون تغيير. اضغط على النص السابق لنسخه ثم أرسله في رسالة واحدة."),parse_mode=ParseMode.HTML); return
        context.user_data.pop("awaiting_private_study_oath",None)
        chapter,lecture=oath_state["chapter"],oath_state["lecture"]
        from_backlog=bool(oath_state.get("from_backlog"))
        progress=await lecture_progress(update.effective_user.id,chapter,lecture)
        if progress and progress.get("completed_at"):
            await update.effective_message.reply_text(bold("✅ تم تسجيل دراستك لهذه المحاضرة بنجاح مسبقاً."),parse_mode=ParseMode.HTML,reply_markup=main_menu()); return
        await mark_lecture_progress(update.effective_user.id,chapter,lecture,True,"private_source_oath")
        await linked_exam_dispatch_job(context)
        backlog_done=await complete_backlog(update.effective_user.id,chapter,lecture)
        prep_award=await award_daily_preparation(update.effective_user.id,chapter,lecture)
        rows=[]
        if backlog_done: rows.append([InlineKeyboardButton("📝 نعم، جاهز للامتحان",callback_data=f"backlogexam|{chapter}|{lecture}")])
        rows.append([back_menu()])
        xp_text="\n⭐ حصلت على 15 XP لإكمال التحضير اليومي كاملاً." if prep_award and prep_award.get("awarded") else ""
        extra="\n\n🎉 مبروك! أنهيت محاضرة متراكمة. هل أنت جاهز لامتحانها؟" if backlog_done else ""
        await update.effective_message.reply_text(bold(f"✅ تم حفظ قسمك وتسجيل دراسة الفصل {chapter} – المحاضرة {lecture} من مصدرك الخاص.{xp_text}{extra}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))
        student_row=await get_student(update.effective_user.id)
        for parent in await student_parents(update.effective_user.id,True):
            try: await context.bot.send_message(parent["parent_chat_id"],bold(f"🌟 إنجاز دراسي جديد\nأكمل الطالب {student_row['full_name']} محاضرة الأحياء رقم {lecture} من الفصل {chapter} من مصدره الدراسي الخاص بعد إرسال الإقرار."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        return
    if context.user_data.get("awaiting_reopen_hours") and is_admin(update.effective_user.id):
        try:
            hours=int((update.message.text or "").strip())
            if not 1<=hours<=72: raise ValueError
        except ValueError:
            await update.effective_message.reply_text(bold("⚠️ أرسل رقماً من 1 إلى 72."),parse_mode=ParseMode.HTML); return
        context.user_data.pop("awaiting_reopen_hours",None); row=await reopen_latest_daily_exam(hours)
        if not row: await update.effective_message.reply_text(bold("⚠️ لا يوجد امتحان يومي سابق."),parse_mode=ParseMode.HTML); return
        deadline=row["deadline"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
        for s in await students_pending_task(row["id"]):
            try: await context.bot.send_message(s["user_id"],bold(f"🔓 تمت إعادة فتح الامتحان\n📝 {row['title']}\n⏳ متاح لمدة {hours} ساعة\n🕐 يغلق: {deadline}"),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        await update.effective_message.reply_text(bold(f"✅ تمت إعادة فتح «{row['title']}» لمدة {hours} ساعة.\nينتهي: {deadline}\nتم إعلام الطلبة."),parse_mode=ParseMode.HTML,reply_markup=main_menu(True)); return
    if await receive_linked_exam(update,context): return
    if await receive_manual_task(update,context): return
    if await receive_resource_input(update,context): return
    if await receive_archive_media(update,context): return
    if await receive_submission(update,context): return
    if await receive_private_reply(update,context): return
    if context.user_data.get("edit_field"):
        field=context.user_data.pop("edit_field"); value=(update.message.text or "").strip()
        if not value: await update.effective_message.reply_text(bold("⚠️ أرسل قيمة صحيحة."),parse_mode=ParseMode.HTML); return
        await update_student_profile(update.effective_user.id,field,value)
        await update.effective_message.reply_text(bold("✅ تم تعديل معلوماتك بنجاح."),parse_mode=ParseMode.HTML,reply_markup=main_menu()); return
    if context.user_data.get("awaiting_date"):
        mode=context.user_data.get("awaiting_date")
        try:
            target=datetime.strptime((update.message.text or "").strip(),"%d/%m/%Y").date()
            if target<datetime.now(TIMEZONE).date(): raise ValueError
        except ValueError:
            await update.effective_message.reply_text(bold("⚠️ التاريخ غير صحيح. أرسله مثل: 30/9/2026"),parse_mode=ParseMode.HTML); return
        context.user_data.pop("awaiting_date",None)
        if mode=="backlog":
            await set_backlog_deadline(update.effective_user.id,target)
            rows=await plan_backlog(update.effective_user.id,datetime.now(TIMEZONE).date()+timedelta(days=1),3,target)
            lines=["📈 جدول إنهاء التراكمات",DIV,f"🎯 موعد الإكمال: {target.strftime('%d/%m/%Y')}"]
            for row in rows:
                item=PLAYLISTS[row["chapter"]][row["lecture"]-1]
                lines.append(f"📅 {row['planned_date'].strftime('%d/%m')} | 🎬 الفصل {row['chapter']} – المحاضرة {row['lecture']}\n🔗 {item[2]}")
            await update.effective_message.reply_text(bold("\n\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=main_menu()); return
        student=await get_student(update.effective_user.id)
        if not student or not (student.get("study_track")=="chapter" or student.get("schedule_mode")=="custom"):
            await update.effective_message.reply_text(bold("ℹ️ نظام الإجازات الأربع شهرياً متاح للطلاب على جدول شخصي أو مسار فصل مستقل."),parse_mode=ParseMode.HTML); return
        if await leave_month_usage(update.effective_user.id,target)>=4:
            await update.effective_message.reply_text(bold("⚠️ استنفدت 4 إجازات لهذا الشهر."),parse_mode=ParseMode.HTML); return
        result=await create_leave_request(update.effective_user.id,target)
        if result["status"]=="track": await update.effective_message.reply_text(bold("ℹ️ الإجازات الأربع شهرياً مخصصة لمسار الفصل فقط."),parse_mode=ParseMode.HTML); return
        if result["status"]=="limit": await update.effective_message.reply_text(bold("⚠️ استنفدت 4 إجازات لهذا الشهر."),parse_mode=ParseMode.HTML); return
        if result["status"]=="xp": await update.effective_message.reply_text(bold("⚠️ تحتاج 400 XP لطلب الإجازة."),parse_mode=ParseMode.HTML); return
        if result["status"]!="ok": await update.effective_message.reply_text(bold("⚠️ يوجد طلب لهذا اليوم مسبقاً."),parse_mode=ParseMode.HTML); return
        s=result["student"]; req=result["request"]
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم",callback_data=f"leaveapprove|{req['id']}|{update.effective_user.id}"),InlineKeyboardButton("❌ لا",callback_data=f"leavedeny|{req['id']}|{update.effective_user.id}")]])
        try: await context.bot.send_message(s["parent_chat_id"],bold(f"🏖 طلب إجازة\nالطالب {s['full_name']} طلب إجازة من جميع مطلوبات يوم {target.strftime('%d/%m/%Y')}. الكلفة 400 XP. هل توافق؟"),parse_mode=ParseMode.HTML,reply_markup=kb)
        except TelegramError: pass
        await update.effective_message.reply_text(bold("⏳ أرسل طلب الإجازة إلى ولي أمرك."),parse_mode=ParseMode.HTML); return
    if is_admin(update.effective_user.id):
        await update.effective_message.reply_text(bold("👑 استخدم أزرار لوحة الإدارة."),parse_mode=ParseMode.HTML,reply_markup=main_menu(True)); return
    student=await get_student(update.effective_user.id)
    if not student or not student["approved"]:
        await update.effective_message.reply_text(bold("⏳ حسابك غير مفعّل. أكمل التسجيل عبر /start وانتظر موافقة الإدارة."),parse_mode=ParseMode.HTML); return
    if not student.get("parent_chat_id"):
        await update.effective_message.reply_text(bold(f"🔒 خدمات الدورة متوقفة حتى ربط ولي الأمر.\nرمز الربط: {student['parent_link_code']}\nولي الأمر يفتح البوت وينسخ هذا الأمر ويرسله:")+f"\n<code>/parent {escape(student['parent_link_code'])}</code>",parse_mode=ParseMode.HTML,reply_markup=parent_copy_markup(student["parent_link_code"])); return
    await update.effective_message.reply_text(bold("استخدم أزرار القائمة الرئيسية للمتابعة."),parse_mode=ParseMode.HTML,reply_markup=main_menu())


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)<2: await update.effective_message.reply_text(bold("الاستخدام: /warn ID السبب"),parse_mode=ParseMode.HTML); return
    try: uid=int(context.args[0])
    except ValueError: return
    reason=" ".join(context.args[1:]); count=await add_warning(uid,reason,update.effective_user.id)
    await update.effective_message.reply_text(bold(f"✅ أصبح لدى الطالب {count}/{MAX_WARNINGS} إنذارات."),parse_mode=ParseMode.HTML)
    student=await get_student(uid)
    if student: await notify_student_and_parent(context.bot,student,f"⚠️ إنذار إداري ({count}/{MAX_WARNINGS})\nالسبب: {reason}")
    if count>=MAX_WARNINGS and BIOLOGY_GROUP_ID:
        try: await context.bot.ban_chat_member(BIOLOGY_GROUP_ID,uid)
        except TelegramError: pass


async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args: return
    try: uid=int(context.args[0])
    except ValueError: return
    try: warning_id=int(context.args[1]) if len(context.args)>1 else None
    except ValueError: await update.effective_message.reply_text(bold("⚠️ رقم الإنذار غير صحيح."),parse_mode=ParseMode.HTML); return
    count=await remove_warning(uid,update.effective_user.id,warning_id)
    if count is None: await update.effective_message.reply_text(bold("⚠️ الإنذار غير موجود أو ليس تابعاً لهذا الطالب."),parse_mode=ParseMode.HTML); return
    await update.effective_message.reply_text(bold(f"✅ تم حذف الإنذار. المتبقي: {count}/{MAX_WARNINGS}"),parse_mode=ParseMode.HTML)


async def warnings_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args)!=1: return
    try: student_id=int(context.args[0])
    except ValueError: return
    student=await get_student(student_id); rows=await student_warning_history(student_id)
    if not student: await update.effective_message.reply_text(bold("⚠️ الطالب غير موجود."),parse_mode=ParseMode.HTML); return
    lines=[f"⚠️ إنذارات {student['full_name']}",f"المجموع: {student['warnings']}",DIV]
    for row in rows: lines.append(f"🆔 الإنذار: {row['id']}\nالسبب: {row['reason']}\nالوقت: {row['created_at'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}")
    if not rows: lines.append("لا توجد إنذارات.")
    lines.append(f"\nللحذف: /unwarn {student_id} WARNING_ID")
    await update.effective_message.reply_text(bold("\n\n".join(lines)),parse_mode=ParseMode.HTML)


async def audit_exam_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    task=None
    if context.args:
        try: task=await get_task(int(context.args[0]))
        except ValueError: pass
    else:
        task=await latest_daily_exam()
    if not task or task["kind"]!="exam":
        await update.effective_message.reply_text(bold("⚠️ الامتحان غير موجود. استخدم: /audit_exam TASK_ID"),parse_mode=ParseMode.HTML); return
    audit=await task_warning_audit(task["id"])
    now=datetime.now(TIMEZONE); submitted=[]; warned=[]; waiting=[]; missing=[]
    for row in audit["students"]:
        if row["submitted"]: submitted.append(row)
        elif row["extended_until"] and row["extended_until"]>now: waiting.append(row)
        elif row["warned"]: warned.append(row)
        else: missing.append(row)
    lines=[f"🔎 تدقيق الامتحان #{task['id']}",task["title"],DIV,
           f"👥 المشمولون: {len(audit['students'])}",f"✅ امتحنوا: {len(submitted)}",
           f"⚠️ أخذوا إنذار: {len(warned)}",f"⏳ تمديد فعال: {len(waiting)}",f"❗ ناقص إنذار الآن: {len(missing)}"]
    if missing: lines.append("\nالطلبة الناقص إنذارهم:\n"+"\n".join(f"• {x['full_name']} | {x['user_id']}" for x in missing))
    if waiting: lines.append("\nبانتظار انتهاء التمديد:\n"+"\n".join(f"• {x['full_name']} | إلى {x['extended_until'].astimezone(TIMEZONE).strftime('%d/%m %H:%M')}" for x in waiting))
    lines.append(f"\nللإصلاح الفوري: /repair_exam_warnings {task['id']}")
    await update.effective_message.reply_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML)


async def repair_exam_warnings_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    task=None
    if context.args:
        try: task=await get_task(int(context.args[0]))
        except ValueError: pass
    else: task=await latest_daily_exam()
    if not task or task["kind"]!="exam":
        await update.effective_message.reply_text(bold("⚠️ الامتحان غير موجود."),parse_mode=ParseMode.HTML); return
    if task["deadline"]>datetime.now(TIMEZONE):
        await update.effective_message.reply_text(bold("⚠️ الامتحان لم يصل إلى موعد انتهائه الأصلي بعد."),parse_mode=ParseMode.HTML); return
    warned,removed=await issue_missing_task_warnings(context,task)
    await update.effective_message.reply_text(bold(f"✅ اكتمل فحص الامتحان #{task['id']}.\nالإنذارات الجديدة: {warned}\nالحظر بعد بلوغ الحد: {removed}\nأصحاب التمديد الفعال سيُفحصون تلقائياً بعد انتهائه."),parse_mode=ParseMode.HTML)


async def ban_student_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args: return
    try: student_id=int(context.args[0])
    except ValueError: return
    try:
        await context.bot.ban_chat_member(BIOLOGY_GROUP_ID,student_id)
        await update.effective_message.reply_text(bold("✅ تم حظر الطالب يدوياً من الدورة."),parse_mode=ParseMode.HTML)
    except TelegramError as exc: await update.effective_message.reply_text(bold(f"⚠️ تعذر الحظر: {exc}"),parse_mode=ParseMode.HTML)


async def xp_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args)<2: return
    try: student_id=int(context.args[0]); delta=int(context.args[1])
    except ValueError: return
    reason=" ".join(context.args[2:]) or "تعديل إداري"
    student,change=await adjust_xp(student_id,delta,reason,update.effective_user.id)
    if not student: await update.effective_message.reply_text(bold("⚠️ الطالب غير موجود."),parse_mode=ParseMode.HTML); return
    await update.effective_message.reply_text(bold(f"✅ تم تعديل XP بمقدار {change:+d}. الرصيد الحالي: {student['xp']} XP"),parse_mode=ParseMode.HTML)


async def prep_swap_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    ok=await swap_next_preparations(); await update.effective_message.reply_text(bold("✅ تم تبديل التحضيرين القادمين." if ok else "⚠️ لا يوجد تحضيران قادمان."),parse_mode=ParseMode.HTML)


async def prep_add_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args)!=3:
        if is_admin(update.effective_user.id): await update.effective_message.reply_text(bold("الاستخدام: /prep_add 30/8/2026 3 10,11"),parse_mode=ParseMode.HTML)
        return
    try:
        target=datetime.strptime(context.args[0],"%d/%m/%Y").date(); chapter=int(context.args[1]); lectures=context.args[2]
        lecture_numbers=sorted({int(x) for x in lectures.split(",")})
        if chapter not in PLAYLISTS or not lecture_numbers or any(n<1 or n>len(PLAYLISTS[chapter]) for n in lecture_numbers): raise ValueError
        lectures=",".join(map(str,lecture_numbers))
    except ValueError: await update.effective_message.reply_text(bold("⚠️ البيانات غير صحيحة."),parse_mode=ParseMode.HTML); return
    row=await add_extra_preparation(target,chapter,lectures); await update.effective_message.reply_text(bold(f"✅ أضيف تحضير يومي إضافي بتاريخ {row['target_date'].strftime('%d/%m/%Y')}."),parse_mode=ParseMode.HTML)


async def set_cumulative_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)<3: await update.effective_message.reply_text(bold("الاستخدام: /set_cumulative 30/8/2026 20:00 الفصل الثالث محاضرات 1-10"),parse_mode=ParseMode.HTML); return
    try:
        exam_at=datetime.strptime(f"{context.args[0]} {context.args[1]}","%d/%m/%Y %H:%M").replace(tzinfo=TIMEZONE)
    except ValueError: await update.effective_message.reply_text(bold("⚠️ صيغة التاريخ غير صحيحة."),parse_mode=ParseMode.HTML); return
    syllabus=" ".join(context.args[2:]); await set_cumulative_exam(exam_at,syllabus,update.effective_user.id)
    await update.effective_message.reply_text(bold("✅ تم تحديث موعد ومادة الامتحان التراكمي."),parse_mode=ParseMode.HTML)


async def parent_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if len(context.args)!=1:
        await update.effective_message.reply_text(bold("أرسل رمز الطالب بهذه الصيغة:\n/parent ABCD1234"),parse_mode=ParseMode.HTML); return
    context.user_data.pop('registration',None)
    if await get_student(update.effective_user.id):
        await update.effective_message.reply_text('🚫 حسابك مسجل كطالب. ما تكدر تربطه كولي أمر لطالب آخر.')
        return ConversationHandler.END
    code=context.args[0].strip(); student=await get_student_by_parent_code(code)
    if not student:
        await update.effective_message.reply_text(bold("⚠️ رمز الربط غير صحيح."),parse_mode=ParseMode.HTML); return
    if student["user_id"]==update.effective_user.id:
        await update.effective_message.reply_text(bold("🚫 لا يمكن ربط حساب الطالب نفسه كولي أمر.\nأرسل الأمر إلى حساب Telegram مختلف يعود لولي أمرك، وليقم هو بإرساله إلى البوت."),parse_mode=ParseMode.HTML); return
    context.user_data["pending_parent_link"]={"code":code}
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم، أخبر الطالب",callback_data="parentnotify|yes")],[InlineKeyboardButton("🔕 لا، لا تخبره",callback_data="parentnotify|no")]])
    await update.effective_message.reply_text(bold(f"👪 سيتم ربطك بالطالب: {student['full_name']}\nهل تريد إعلام الطالب بتسجيلك كولي أمر؟"),parse_mode=ParseMode.HTML,reply_markup=kb)


async def approve_parent_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) not in (1,2): return
    try: student_id=int(context.args[0])
    except ValueError: return
    try: parent_id=int(context.args[1]) if len(context.args)==2 else None
    except ValueError: return
    student=await approve_parent(student_id,True,parent_id)
    if not student:
        await update.effective_message.reply_text(bold("⚠️ لا يوجد ولي أمر مربوط بهذا الطالب."),parse_mode=ParseMode.HTML); return
    parent_id=student["approved_parent_chat_id"]
    await update.effective_message.reply_text(bold(f"✅ تم تفعيل ولي أمر الطالب {student['full_name']}\nالحساب: @{student.get('approved_parent_username') or '-'}\nالمعرف: {parent_id}"),parse_mode=ParseMode.HTML)
    try: await context.bot.send_message(parent_id,bold(f"✅ فعّلت الإدارة حسابك كولي أمر للطالب {student['full_name']}.\nأرسل /start لفتح واجهة ولي الأمر."),parse_mode=ParseMode.HTML)
    except TelegramError: pass


async def grade_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not update.message.reply_to_message or len(context.args)!=1:
        await update.effective_message.reply_text(bold("رد على رسالة حل الطالب واكتب مثلاً: /grade 85"),parse_mode=ParseMode.HTML); return
    try:
        grade=int(context.args[0])
        if not 0<=grade<=100: raise ValueError
    except ValueError:
        await update.effective_message.reply_text(bold("⚠️ الدرجة يجب أن تكون من 0 إلى 100."),parse_mode=ParseMode.HTML); return
    reference=await submission_by_review(update.effective_chat.id,update.message.reply_to_message.message_id)
    if reference and reference["kind"]=="homework":
        await update.effective_message.reply_text(bold("📚 الواجبات لا تُعطى لها درجات؛ البوت يسجل التسليم بعلامة ✅ فقط."),parse_mode=ParseMode.HTML); return
    row=await grade_submission_by_review(update.effective_chat.id,update.message.reply_to_message.message_id,grade,update.effective_user.id)
    if not row:
        await update.effective_message.reply_text(bold("⚠️ يجب الرد على رسالة حل أرسلها البوت داخل مجموعة الواجبات أو الامتحانات."),parse_mode=ParseMode.HTML); return
    noun="الواجب" if row["kind"]=="homework" else "الامتحان"
    await update.effective_message.reply_text(bold(f"✅ تم تسجيل درجة {row['full_name']}: {grade}/100"),parse_mode=ParseMode.HTML)
    await notify_student_and_parent(context.bot,row,f"📊 درجة {noun}\n👤 الطالب: {row['full_name']}\n📌 {row['title']}\n✅ الدرجة: {grade}/100")
    if row["kind"]=="exam" and grade<60:
        count=await add_warning(row["user_id"],f"رسوب في {row['title']} بدرجة {grade}",update.effective_user.id,reference["task_id"])
        await notify_student_and_parent(context.bot,row,f"⚠️ إنذار رسوب ({count}/{MAX_WARNINGS})\nالدرجة: {grade}/100")
    if row["kind"]=="exam": await announce_champions(context,reference["task_id"])


async def add_previous_exam_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if update.effective_chat.type!="private":
        await update.effective_message.reply_text(bold("⚠️ أرسل هذا الأمر في المحادثة الخاصة مع البوت حتى ترفع ملفات الامتحان بأمان."),parse_mode=ParseMode.HTML); return
    if len(context.args)<2:
        await update.effective_message.reply_text(bold("الاستخدام القديم ما زال يعمل:\n/add_previous_exam رقم_الفصل اسم الامتحان\n\nوالترتيب الجديد حسب المحاضرة:\n/add_previous_exam رقم_الفصل رقم_المحاضرة اسم الامتحان"),parse_mode=ParseMode.HTML); return
    try:
        chapter=int(context.args[0])
        if not 1<=chapter<=5: raise ValueError
    except ValueError:
        await update.effective_message.reply_text(bold("⚠️ رقم الفصل يجب أن يكون من 1 إلى 9."),parse_mode=ParseMode.HTML); return
    lecture=None
    if len(context.args)>=3 and context.args[1].isdigit():
        lecture=int(context.args[1])
        if not 1<=lecture<=len(PLAYLISTS[chapter]):
            await update.effective_message.reply_text(bold("⚠️ رقم المحاضرة غير صحيح."),parse_mode=ParseMode.HTML); return
        title=" ".join(context.args[2:]).strip()
    else: title=" ".join(context.args[1:]).strip()
    archive=await create_archive_exam(chapter,title,update.effective_user.id,lecture)
    context.user_data["waiting_archive_id"]=archive["id"]
    await update.effective_message.reply_text(bold(f"🗂 تم إنشاء «{title}» في الفصل {chapter}.\nأرسل الآن الصور أو ملف PDF، ثم اضغط إنهاء وحفظ."),parse_mode=ParseMode.HTML)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.effective_message.reply_text(bold("👑 مركز ادارة الأحياء\n━━━━━━━━━━━━━━━━━━\nاختر القسم الذي تريد ادارته"),parse_mode=ParseMode.HTML,reply_markup=main_menu(True)); return
    student=await get_student(update.effective_user.id)
    if not student or not student["approved"]: await start(update,context); return
    if not student.get("parent_chat_id"):
        await update.effective_message.reply_text(bold(f"🔒 يجب ربط ولي الأمر أولاً.\nرمزك: {student['parent_link_code']}\nولي الأمر ينسخ ويرسل:")+f"\n<code>/parent {escape(student['parent_link_code'])}</code>",parse_mode=ParseMode.HTML,reply_markup=parent_copy_markup(student["parent_link_code"])); return
    if not await is_channel_member(context.bot,update.effective_user.id):
        await update.effective_message.reply_text(bold("🔒 يجب الاشتراك بقناة منصة المجتهد أولاً."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 الاشتراك بالقناة",url=REQUIRED_CHANNEL_URL)]])); return
    await update.effective_message.reply_text(bold(f"⚡ بوت الأحياء | منصة المجتهد التعليمية\n━━━━━━━━━━━━━━━━━━\nأهلا {student['full_name']} 👋\n🎯 مهامي اليومية تختار لك الخطوة الأهم تلقائيا"),parse_mode=ParseMode.HTML,reply_markup=main_menu())


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(bold(f"👤 معرفك: {update.effective_user.id}\n💬 معرف المحادثة: {update.effective_chat.id}\n📂 معرف الموضوع: {update.effective_message.message_thread_id or 0}"),parse_mode=ParseMode.HTML)


async def deadline_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=3:
        await update.effective_message.reply_text(bold("الاستخدام: /deadline رقم_المنشور 26/8/2026 23:00"),parse_mode=ParseMode.HTML); return
    try:
        pending_id=int(context.args[0])
        deadline=datetime.strptime(f"{context.args[1]} {context.args[2]}","%d/%m/%Y %H:%M").replace(tzinfo=TIMEZONE)
        if deadline<=datetime.now(TIMEZONE): raise ValueError
    except ValueError:
        await update.effective_message.reply_text(bold("⚠️ الرقم أو التاريخ غير صحيح، أو أن الموعد قد مضى."),parse_mode=ParseMode.HTML); return
    task=await confirm_pending_task(pending_id,deadline)
    if not task:
        await update.effective_message.reply_text(bold("⚠️ المنشور غير موجود أو تم ربطه مسبقاً."),parse_mode=ParseMode.HTML); return
    await notify_task_assignment(context.bot,task)
    await update.effective_message.reply_text(bold(f"✅ تم الربط بنجاح.\n🆔 رقم المهمة: {task['id']}\n⏰ انتهاء التسليم: {deadline.strftime('%d/%m/%Y %H:%M')}"),parse_mode=ParseMode.HTML)


async def prep_date_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=1:
        await update.effective_message.reply_text(bold("الاستخدام: /prep_date 30/8/2026"),parse_mode=ParseMode.HTML); return
    try:
        target=datetime.strptime(context.args[0],"%d/%m/%Y").date()
        if target<=datetime.now(TIMEZONE).date(): raise ValueError
        row=await reschedule_unpublished_preparations(target)
    except ValueError:
        await update.effective_message.reply_text(bold("⚠️ التاريخ غير صحيح أو ليس بعد تاريخ اليوم."),parse_mode=ParseMode.HTML); return
    except Exception:
        await update.effective_message.reply_text(bold("⚠️ تعذر اعتماد التاريخ لأنه يتعارض مع تحضير منشور سابق."),parse_mode=ParseMode.HTML); return
    if not row: await update.effective_message.reply_text(bold("لا توجد تحاضير قادمة."),parse_mode=ParseMode.HTML); return
    await update.effective_message.reply_text(bold(f"✅ تم تحديد موعد التحضير القادم في {target.strftime('%d/%m/%Y')} وإعادة ترتيب جميع التحاضير التالية تلقائياً."),parse_mode=ParseMode.HTML,reply_markup=main_menu(True))


async def chapter_end_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)<2:
        await update.effective_message.reply_text(bold("الاستخدام: /chapter_end 3 30/9/2026\nللفصول من 1 إلى 5."),parse_mode=ParseMode.HTML); return
    try:
        chapter=int(context.args[0])
        if not 1<=chapter<=5: raise ValueError
        target=datetime.strptime(context.args[1],"%d/%m/%Y").date()
    except ValueError:
        await update.effective_message.reply_text(bold("⚠️ أرسل رقم فصل من 1 إلى 9 وتاريخاً بالصيغة 30/9/2026."),parse_mode=ParseMode.HTML); return
    value=target.strftime("%d/%m/%Y")
    await set_setting_value(f"chapter_{chapter}_completion",value)
    await update.effective_message.reply_text(bold(f"✅ تم تحديد موعد إكمال الفصل {chapter}: {value}"),parse_mode=ParseMode.HTML)


async def reopen_exam_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=1:
        await update.effective_message.reply_text(bold("الاستخدام: /reopen_exam 3\nالرقم هو عدد ساعات إعادة فتح آخر امتحان يومي."),parse_mode=ParseMode.HTML); return
    try:
        hours=int(context.args[0])
        if not 1<=hours<=72: raise ValueError
    except ValueError:
        await update.effective_message.reply_text(bold("⚠️ عدد الساعات يجب أن يكون من 1 إلى 72."),parse_mode=ParseMode.HTML); return
    row=await reopen_latest_daily_exam(hours)
    if not row:
        await update.effective_message.reply_text(bold("⚠️ لا يوجد امتحان يومي سابق."),parse_mode=ParseMode.HTML); return
    deadline=row["deadline"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
    for s in await students_pending_task(row["id"]):
        try: await context.bot.send_message(s["user_id"],bold(f"🔓 تمت إعادة فتح الامتحان\n📝 {row['title']}\n⏳ متاح لمدة {hours} ساعة\n🕐 يغلق: {deadline}"),parse_mode=ParseMode.HTML)
        except TelegramError: pass
    await update.effective_message.reply_text(bold(f"✅ تمت إعادة فتح «{row['title']}» لمدة {hours} ساعة.\nينتهي: {deadline}\nتم إعلام الطلبة."),parse_mode=ParseMode.HTML)


async def publish_at_command(update: Update,context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    state=context.user_data.get("manual_task")
    if not state or state.get("step")!="publish_time":
        await update.effective_message.reply_text(bold("⚠️ اختر أولاً «نشر واجب أو امتحان»، ثم أرسل الاسم والملف واختر جدولة النشر."),parse_mode=ParseMode.HTML); return
    if len(context.args)!=3:
        await update.effective_message.reply_text(bold("الاستخدام: /publish_at 30/8/2026 18:00 24\nالرقم الأخير هو عدد ساعات التسليم بعد النشر."),parse_mode=ParseMode.HTML); return
    try:
        publish_at=datetime.strptime(f"{context.args[0]} {context.args[1]}","%d/%m/%Y %H:%M").replace(tzinfo=TIMEZONE)
        hours=int(context.args[2])
        if publish_at<=datetime.now(TIMEZONE) or not 1<=hours<=720: raise ValueError
    except ValueError:
        await update.effective_message.reply_text(bold("⚠️ الموعد يجب أن يكون في المستقبل، ومدة التسليم بين ساعة و720 ساعة."),parse_mode=ParseMode.HTML); return
    manual_kind=state["kind"]; kind="homework" if manual_kind=="homework" else "exam"
    if kind=="exam" and state.get("target_scope")!="course": state["target_scope"]="course"
    title=("[تراكمي] " if manual_kind=="cumulative" else "")+state["title"]
    row=await create_scheduled_task(kind,title,state["media"],publish_at,hours,update.effective_user.id,state.get("target_scope","course"))
    context.user_data.pop("manual_task",None)
    await update.effective_message.reply_text(bold(f"✅ تمت جدولة المنشور بنجاح.\n🆔 رقم الجدولة: {row['id']}\n🚀 موعد النشر: {publish_at.strftime('%d/%m/%Y %H:%M')}\n⏰ مدة التسليم بعد النشر: {hours} ساعة."),parse_mode=ParseMode.HTML,reply_markup=main_menu(True))


def rtl(value):
    return get_display(arabic_reshaper.reshape(str(value)))


def period_metrics(rows):
    total=len(rows); submitted=sum(1 for row in rows if row["submitted_at"])
    completion=round(submitted*100/total,1) if total else 100.0
    grades=[float(row["grade"]) for row in rows if row["kind"]=="exam" and row["grade"] is not None]
    average=round(sum(grades)/len(grades),1) if grades else None
    score=round((completion+(average if average is not None else completion))/2,1)
    return {"total":total,"submitted":submitted,"completion":completion,"average":average,"score":score}


def build_weekly_pdf(bundle,current_start,current_end):
    buffer=BytesIO()
    font_path=os.getenv("REPORT_FONT_PATH","/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font_name="ArabicReport"
    if font_name not in pdfmetrics.getRegisteredFontNames(): pdfmetrics.registerFont(TTFont(font_name,font_path))
    styles=getSampleStyleSheet()
    title=ParagraphStyle("ArabicTitle",parent=styles["Title"],fontName=font_name,fontSize=18,leading=26,alignment=TA_CENTER,textColor=colors.HexColor("#14532d"))
    heading=ParagraphStyle("ArabicHeading",parent=styles["Heading2"],fontName=font_name,fontSize=13,leading=20,alignment=TA_RIGHT,textColor=colors.HexColor("#166534"))
    body=ParagraphStyle("ArabicBody",parent=styles["BodyText"],fontName=font_name,fontSize=10,leading=16,alignment=TA_RIGHT)
    doc=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=34,leftMargin=34,topMargin=32,bottomMargin=32)
    student=bundle["student"]; current=bundle["current"]; warnings=bundle["warnings"]
    now_metrics=period_metrics(current); previous_metrics=period_metrics(bundle["previous"])
    if previous_metrics["score"]:
        change=round((now_metrics["score"]-previous_metrics["score"])*100/previous_metrics["score"],1)
        comparison=(f"ازداد الأداء بنسبة {change}% عن الأسبوع السابق" if change>=0 else f"انخفض الأداء بنسبة {abs(change)}% عن الأسبوع السابق")
    else: comparison="لا تتوفر بيانات كافية للمقارنة مع الأسبوع السابق"
    achievements=[]
    if now_metrics["total"] and now_metrics["submitted"]==now_metrics["total"]: achievements.append("أكمل جميع الواجبات والامتحانات المطلوبة")
    if now_metrics["average"] is not None and now_metrics["average"]>=90: achievements.append("حقق معدلاً ممتازاً في الامتحانات")
    if not warnings: achievements.append("أسبوع كامل بلا إنذارات")
    if not achievements: achievements.append("لا توجد إنجازات خاصة مسجلة هذا الأسبوع")
    story=[Paragraph(rtl("منصة المجتهد التعليمية"),title),Paragraph(rtl("التقرير الأسبوعي لأداء الطالب"),heading),Spacer(1,8)]
    info=[[rtl("الفترة"),f"{current_start.strftime('%d/%m/%Y')} - {current_end.strftime('%d/%m/%Y')}"],[rtl("اسم الطالب"),rtl(student["full_name"])],[rtl("المدرسة"),rtl(student["school"])],[rtl("نسبة الإنجاز"),f"{now_metrics['completion']}%"],[rtl("معدل درجات الامتحانات"),f"{now_metrics['average']}/100" if now_metrics["average"] is not None else rtl("لا توجد درجة")],[rtl("المقارنة"),rtl(comparison)],[rtl("مجموع النقاط"),f"{student['xp']} XP"],[rtl("مجموع الإنذارات"),f"{student['warnings']}/{MAX_WARNINGS}"]]
    table=Table(info,colWidths=[170,340],hAlign="RIGHT")
    table.setStyle(TableStyle([("FONTNAME",(0,0),(-1,-1),font_name),("FONTSIZE",(0,0),(-1,-1),10),("ALIGN",(0,0),(-1,-1),"RIGHT"),("GRID",(0,0),(-1,-1),0.5,colors.HexColor("#d1d5db")),("BACKGROUND",(0,0),(0,-1),colors.HexColor("#dcfce7")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("BOTTOMPADDING",(0,0),(-1,-1),7),("TOPPADDING",(0,0),(-1,-1),7)]))
    story.extend([table,Spacer(1,14),Paragraph(rtl("تفاصيل واجبات وامتحانات هذا الأسبوع"),heading)])
    task_data=[[rtl("الحالة"),rtl("الدرجة"),rtl("النوع"),rtl("العنوان")]]
    for row in current:
        status=rtl("تم التسليم") if row["submitted_at"] else rtl("لم يسلّم")
        grade=f"{row['grade']}/100" if row["grade"] is not None else "-"
        kind=rtl("واجب") if row["kind"]=="homework" else rtl("امتحان")
        task_data.append([status,grade,kind,rtl(row["title"])])
    if len(task_data)==1: task_data.append(["-","-","-",rtl("لا توجد مطلوبات خلال هذا الأسبوع")])
    tasks_table=Table(task_data,colWidths=[90,70,75,275],repeatRows=1,hAlign="RIGHT")
    tasks_table.setStyle(TableStyle([("FONTNAME",(0,0),(-1,-1),font_name),("FONTSIZE",(0,0),(-1,-1),9),("ALIGN",(0,0),(-1,-1),"RIGHT"),("GRID",(0,0),(-1,-1),0.5,colors.HexColor("#d1d5db")),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#166534")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("BOTTOMPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),6)]))
    story.extend([tasks_table,Spacer(1,14),Paragraph(rtl("إنذارات هذا الأسبوع"),heading)])
    if warnings:
        for warning in warnings: story.append(Paragraph(rtl(f"• {warning['reason']} - {warning['created_at'].astimezone(TIMEZONE).strftime('%d/%m/%Y')}"),body))
    else: story.append(Paragraph(rtl("لا توجد إنذارات خلال هذا الأسبوع"),body))
    story.extend([Spacer(1,12),Paragraph(rtl("الإنجازات الأسبوعية"),heading)])
    for achievement in achievements: story.append(Paragraph(rtl(f"• {achievement}"),body))
    story.extend([Spacer(1,18),Paragraph(rtl("منصة المجتهد التعليمية - مجانية مع الجدية"),body)])
    doc.build(story); buffer.seek(0); return buffer


async def weekly_reports_job(context: ContextTypes.DEFAULT_TYPE):
    now=datetime.now(TIMEZONE)
    if now.weekday()!=4 or now.hour!=20: return
    current_end=now; current_start=now-timedelta(days=7); previous_start=now-timedelta(days=14)
    for row in await weekly_parent_reports():
        key=f"weekly_parent_report_{now.date().isoformat()}_{row['user_id']}_{row['parent_chat_id']}"
        if await setting_value(key): continue
        try:
            bundle=await parent_report_bundle(row["user_id"],current_start,current_end,previous_start)
            pdf=build_weekly_pdf(bundle,current_start,current_end); pdf.name=f"weekly_report_{row['user_id']}_{now.date().isoformat()}.pdf"
            await context.bot.send_document(row["parent_chat_id"],pdf,caption=bold(f"📊 التقرير الأسبوعي للطالب {row['full_name']}"),parse_mode=ParseMode.HTML)
            await set_setting_value(key,"sent")
        except Exception as exc: logger.exception("Weekly PDF report failed for %s: %s",row["user_id"],exc)


async def post_init(app):
    init_db(); await backfill_personal_prep_numbers(CHAPTER_PREPARATION_DISTRIBUTION); await seed_preparations(preparation_rows()); await observe_known_unactivated_members(ACTIVATION_GRACE_HOURS)
    if not await setting_value("v19_onboarding_broadcast_sent"):
        for student in await students_requiring_onboarding(19):
            try: await app.bot.send_message(student["user_id"],bold("🆕 تم تحديث بوت الأحياء بنظام دراسي جديد.\n\nلتحديث حسابك: افتح البوت واضغط زر Start أو أرسل /start، ثم اختر الاحتفاظ بمعلوماتك أو تعديلها وحدد الفصل الذي تريد البدء منه."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        await set_setting_value("v19_onboarding_broadcast_sent","sent")
    await app.bot.set_my_commands([BotCommand("start","بدء البوت"),BotCommand("menu","القائمة الرئيسية"),BotCommand("id","عرض المعرفات"),BotCommand("parent","ربط ولي الأمر"),BotCommand("approve_parent","تفعيل ولي الأمر - إدارة"),BotCommand("msg_student","مراسلة طالب - إدارة"),BotCommand("msg_parent","مراسلة ولي أمر - إدارة"),BotCommand("publish_at","جدولة منشور - إدارة"),BotCommand("set_cumulative","تحديد التراكمي - إدارة"),BotCommand("audit_exam","تدقيق إنذارات امتحان - إدارة"),BotCommand("repair_exam_warnings","إصلاح إنذارات امتحان - إدارة"),BotCommand("prep_date","موعد التحضير - إدارة"),BotCommand("chapter_end","موعد إكمال فصل - إدارة"),BotCommand("prep_swap","تبديل التحضيرين - إدارة"),BotCommand("prep_add","إضافة تحضير - إدارة"),BotCommand("warn","إضافة إنذار - إدارة"),BotCommand("warnings","عرض إنذارات طالب - إدارة"),BotCommand("unwarn","حذف إنذار - إدارة"),BotCommand("xp","تعديل XP - إدارة"),BotCommand("ban_student","حظر طالب - إدارة"),BotCommand("grade","درجة امتحان - إدارة")])
    config_warnings=[]
    if not BIOLOGY_GROUP_ID: config_warnings.append("BIOLOGY_GROUP_ID غير مضبوط")
    if not EXAM_TOPIC_ID: config_warnings.append("EXAM_TOPIC_ID غير مضبوط؛ الأسئلة ستنزل في الصفحة العامة")
    if not WARNINGS_TOPIC_ID: config_warnings.append("WARNINGS_TOPIC_ID غير مضبوط")
    if not CHAMPIONS_TOPIC_ID: config_warnings.append("CHAMPIONS_TOPIC_ID غير مضبوط")
    if OWNER_CHAT_ID:
        status=f"✅ اشتغل بوت الأحياء بنجاح — {BUILD_VERSION}\n✅ قاعدة البيانات جاهزة\n✅ نظام استرداد الإنذارات فعال"
        if config_warnings: status+="\n\n⚠️ تنبيهات الإعداد:\n"+"\n".join(f"• {x}" for x in config_warnings)
        try: await app.bot.send_message(OWNER_CHAT_ID,bold(status),parse_mode=ParseMode.HTML)
        except TelegramError: pass
    app.job_queue.run_repeating(publish_preparations_job,60,first=5,name="preparations")
    app.job_queue.run_repeating(personal_preparations_job,60,first=8,name="personal_preparations")
    app.job_queue.run_repeating(scheduled_tasks_job,30,first=10,name="scheduled_tasks")
    app.job_queue.run_repeating(linked_exam_dispatch_job,60,first=15,name="linked_exam_dispatch")
    app.job_queue.run_repeating(exam_parent_readiness_job,60,first=12,name="exam_parent_readiness")
    app.job_queue.run_repeating(activation_compliance_job,300,first=45,name="activation_compliance")
    app.job_queue.run_repeating(close_tasks_job,60,first=15,name="task_deadlines")
    app.job_queue.run_repeating(exam_reminders_job,60,first=25,name="exam_six_hour_reminders")
    app.job_queue.run_repeating(teacher_exam_deadline_job,60,first=30,name="teacher_exam_deadline")
    app.job_queue.run_repeating(study_and_progress_job,300,first=35,name="study_and_progress")
    app.job_queue.run_repeating(weekly_reports_job,60,first=20,name="weekly_parent_reports")


async def error_handler(update,context):
    # Telegram returns this harmless response when a button is pressed twice,
    # or two callbacks try to render the exact same text and keyboard.
    # It is not a bot failure and must not trigger an owner alarm.
    if isinstance(context.error, BadRequest) and "message is not modified" in str(context.error).lower():
        if update and getattr(update, "callback_query", None):
            try:
                await update.callback_query.answer()
            except TelegramError:
                pass
        return
    # Telegram can close an idle connection; polling recovers by itself.
    # Expired callback tokens cannot be answered again. Keep diagnostics in logs
    # without flooding the owner chat every minute for these transient events.
    if isinstance(context.error, (NetworkError, TimedOut)) or any(
        cls.__module__.startswith('httpx') and cls.__name__ in
        {'ReadError', 'ConnectError', 'RemoteProtocolError', 'ReadTimeout', 'ConnectTimeout'}
        for cls in type(context.error).__mro__
    ):
        logger.warning('Temporary Telegram transport failure: %r', context.error)
        return
    if isinstance(context.error, BadRequest) and any(
        phrase in str(context.error).lower() for phrase in
        ('query is too old', 'response timeout expired', 'query id is invalid')
    ):
        logger.info('Expired Telegram callback; student can press the button again: %s', context.error)
        return
    logger.exception("Unhandled error",exc_info=context.error)
    if OWNER_CHAT_ID:
        global _OWNER_ERROR_ALERT_AT
        now=time.monotonic()
        if now-_OWNER_ERROR_ALERT_AT<60:
            return
        _OWNER_ERROR_ALERT_AT=now
        try: await context.bot.send_message(OWNER_CHAT_ID,bold(f"🚨 خطأ في بوت الأحياء:\n{str(context.error)[:2000]}"),parse_mode=ParseMode.HTML)
        except TelegramError: pass


async def spam_guard(update: Update,context: ContextTypes.DEFAULT_TYPE):
    """Lightweight flood protection for private chats; media albums remain usable."""
    if not update.effective_user or is_admin(update.effective_user.id) or update.effective_chat.type!="private": return
    now=time.monotonic(); events=_spam_events[update.effective_user.id]
    while events and now-events[0]>10: events.popleft()
    events.append(now)
    if len(events)<=12: return
    if len(events)==13:
        try: await update.effective_message.reply_text(bold("🚫 تم إيقاف الرسائل مؤقتاً بسبب الإرسال السريع. انتظر 10 ثوانٍ ثم حاول مجدداً."),parse_mode=ParseMode.HTML)
        except TelegramError: pass
    raise ApplicationHandlerStop


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        # Render polls "/" frequently. A database query here kept Neon awake all
        # day and consumed the free compute quota even when no student used the bot.
        # The normal health check is now process-only; /db-ready remains available
        # for an explicit database diagnostic.
        if self.path.split("?",1)[0]=="/db-ready":
            try:
                with db.connect() as conn,conn.cursor() as cur:
                    cur.execute("SELECT 1;"); cur.fetchone()
                self.send_response(200); body=b"database ready"
            except Exception:
                self.send_response(503); body=b"database unavailable"
        else:
            self.send_response(200); body=b"alive"
        self.send_header("Content-Type","text/plain; charset=utf-8"); self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass


def health_server():
    HTTPServer(("0.0.0.0",_env_int("PORT",8080)),Health).serve_forever()


_INSTANCE_LOCK_CONN=None

def acquire_single_instance_lock():
    global _INSTANCE_LOCK_CONN
    if not DATABASE_URL or not USE_DATABASE_INSTANCE_LOCK:
        logger.info("Database advisory lock disabled; Render single-instance mode is expected.")
        return True
    _INSTANCE_LOCK_CONN=psycopg.connect(DATABASE_URL,sslmode="require",connect_timeout=10,application_name="physics-bot-instance-lock")
    cur=_INSTANCE_LOCK_CONN.cursor()
    lock_id=int.from_bytes(hashlib.sha256(BOT_TOKEN.encode("utf-8")).digest()[:8],"big",signed=True)
    cur.execute("SELECT pg_try_advisory_lock(%s);",(lock_id,))
    locked=cur.fetchone()[0]
    if not locked:
        _INSTANCE_LOCK_CONN.close(); _INSTANCE_LOCK_CONN=None
        raise RuntimeError("Another Chemistry bot instance is already running with this BOT_TOKEN.")
    _INSTANCE_LOCK_CONN.commit()
    return True


async def parent_during_registration(update,context):
    context.user_data.pop('registration',None)
    context.user_data.pop('registration_started_at',None)
    await parent_command(update,context)
    return ConversationHandler.END


async def cancel_registration(update,context):
    context.user_data.pop('registration',None)
    context.user_data.pop('registration_started_at',None)
    await update.effective_message.reply_text('تم إلغاء تسجيل الطالب. يمكنك إرسال /parent مع رمز طالب، أو /start للبدء من جديد.')
    return ConversationHandler.END


async def parent_restart_registration(update,context):
    await update.callback_query.answer()
    context.user_data.clear()
    return await start(update,context)


def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is required")
    if not DATABASE_URL: raise RuntimeError("DATABASE_URL is required")
    acquire_single_instance_lock()
    threading.Thread(target=health_server,daemon=True).start()
    app=Application.builder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).post_init(post_init).post_shutdown(post_shutdown).build()
    registration=ConversationHandler(
        entry_points=[CommandHandler("start",start), CallbackQueryHandler(parent_restart_registration,pattern="^parent_restart$"), CallbackQueryHandler(v47_reset_confirm,pattern="^v47_reset_confirm$")],
        states={REG_NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND,reg_name)],REG_SCHOOL:[MessageHandler(filters.TEXT & ~filters.COMMAND,reg_school)],REG_GRADE:[MessageHandler(filters.TEXT & ~filters.COMMAND,reg_grade)],REG_JOIN:[CallbackQueryHandler(verify_join,pattern="^verify_join$")]},
        fallbacks=[CommandHandler("start",start),CommandHandler("parent",parent_during_registration),CommandHandler("cancel",cancel_registration)],allow_reentry=True,conversation_timeout=1800,
    )
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE,spam_guard),group=-1)
    app.add_handler(ChatMemberHandler(track_chat_member,ChatMemberHandler.CHAT_MEMBER),group=-2)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS,observe_group_activity),group=-2)
    app.add_handler(registration,group=0)
    app.add_handler(CommandHandler("menu",menu_command),group=0)
    app.add_handler(CommandHandler("cancel",cancel_registration),group=0)
    app.add_handler(CommandHandler("warn",warn_command),group=0)
    app.add_handler(CommandHandler("unwarn",unwarn_command),group=0)
    app.add_handler(CommandHandler("warnings",warnings_command),group=0)
    app.add_handler(CommandHandler("audit_exam",audit_exam_command),group=0)
    app.add_handler(CommandHandler("repair_exam_warnings",repair_exam_warnings_command),group=0)
    app.add_handler(CommandHandler("ban_student",ban_student_command),group=0)
    app.add_handler(CommandHandler("xp",xp_command),group=0)
    app.add_handler(CommandHandler("prep_swap",prep_swap_command),group=0)
    app.add_handler(CommandHandler("prep_add",prep_add_command),group=0)
    app.add_handler(CommandHandler("set_cumulative",set_cumulative_command),group=0)
    app.add_handler(CommandHandler("id",id_command),group=0)
    app.add_handler(CommandHandler("deadline",deadline_command),group=0)
    app.add_handler(CommandHandler("prep_date",prep_date_command),group=0)
    app.add_handler(CommandHandler("chapter_end",chapter_end_command),group=0)
    app.add_handler(CommandHandler("publish_at",publish_at_command),group=0)
    app.add_handler(CommandHandler("msg_student",msg_student_command),group=0)
    app.add_handler(CommandHandler("msg_parent",msg_parent_command),group=0)
    app.add_handler(CommandHandler("parent",parent_command),group=0)
    app.add_handler(CommandHandler("approve_parent",approve_parent_command),group=0)
    app.add_handler(CommandHandler("grade",grade_command),group=0)
    app.add_handler(CommandHandler("add_question",v39_add_question_command),group=0)
    app.add_handler(CommandHandler("extend_exam",v28_admin_extend_exam_command),group=0)
    app.add_handler(CommandHandler("exam_notice",v28_exam_notice_command),group=0)
    app.add_handler(CommandHandler("add_previous_exam",add_previous_exam_command),group=0)
    app.add_handler(CommandHandler("reopen_exam",reopen_exam_command),group=0)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND & filters.TEXT,receive_grade_value),group=0)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND & (filters.PHOTO|filters.Document.ALL|filters.VIDEO),receive_exam_correction),group=0)
    app.add_handler(CallbackQueryHandler(button_handler,pattern=r"^(?!verify_join$).+"),group=1)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND & (filters.TEXT|filters.PHOTO|filters.Document.ALL|filters.VIDEO),receive_admin_communication),group=1)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND & (filters.TEXT|filters.PHOTO|filters.Document.ALL|filters.VIDEO),capture_group_task),group=1)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND & (filters.TEXT|filters.PHOTO|filters.Document.ALL|filters.VIDEO),private_messages),group=2)
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=False)


# ========================= v28 CLEAN ACADEMIC OVERRIDES =========================
import database as db
from academic_engine import dispatch_exams, weekly_study_day_count

async def show_today_preparation(query):
    student=await get_student(query.from_user.id)
    personal=bool(student and (student.get("study_track")=="chapter" or student.get("schedule_mode")=="custom"))
    row=await personal_preparation_for_student(query.from_user.id,datetime.now(TIMEZONE).date()) if personal else await preparation_for_date(datetime.now(TIMEZONE).date())
    if not row and not personal: row=await latest_preparation()
    text=preparation_text(row) if row else bold("📭 لا يوجد تحضير منشور حالياً.")
    kb=[]
    if row:
        chapter=int(row.get("chapter") or (student.get("current_chapter") if student else 3) or 3)
        for lecture in map(int,(row.get("lectures") or "").split(",")): kb.append([InlineKeyboardButton(f"▶️ الذهاب إلى المحاضرة {lecture}",callback_data=f"prepopen|{chapter}|{lecture}")])
    kb.append([back_menu()]); await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def show_tasks(query,kind,category=None):
    uid=query.from_user.id
    if kind=="homework": rows=await db.v28_student_homeworks(uid) if not is_admin(uid) else await open_tasks("homework",None); title="📚 الواجبات"
    else:
        rows=await db.v28_student_exam_tasks(uid) if not is_admin(uid) else await open_exam_tasks(False,None)
        cumulative=(category=="cumulative")
        rows=[r for r in rows if (r.get("title","").startswith("[تراكمي]") == cumulative)]
        title="🏆 الامتحانات التراكمية" if cumulative else "📝 الامتحانات"
    if not rows:
        msg="📭 لا توجد امتحانات مفتوحة لك حالياً.\n\nالامتحان المرتبط بالتحضير يظهر حسب مسارك الدراسي، والامتحان التراكمي يظهر عند نشره للمسار المستهدف." if kind=="exam" else "📭 لا توجد واجبات مفتوحة لك حالياً."
        await query.edit_message_text(bold(msg),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    kb=[]
    for row in rows[:30]:
        label=("🔐 " if row.get("exam_pending_activation") else ("📝 " if kind=="exam" else "📚 "))+row["title"]
        if row.get("exam_pending_activation"): label+=" — بانتظار الموافقة"
        kb.append([InlineKeyboardButton(label,callback_data=f"task|{row['id']}")])
    kb.append([back_menu()]); await query.edit_message_text(bold(f"{title}\n━━━━━━━━━━━━━━━━━━\nعدد المطلوبات: {len(rows)}\n\n⏱️ مدة كل امتحان تظهر حسب المدة التي حددتها الإدارة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def linked_exam_dispatch_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        created=await dispatch_exams(datetime.now(TIMEZONE))
        for task in created:
            for student in await assigned_students(task["id"]):
                uid=student["user_id"]
                pending=bool(task.get("exam_pending_activation"))
                if pending:
                    await db.v28_notification(uid,"exam_ready","📝 امتحان جاهز",f"تم إكمال المحتوى المطلوب للامتحان «{task['title']}». الامتحان بانتظار موافقة ولي الأمر أو الإدارة.","high",entity_type="exam",entity_id=task["id"],dedupe_key=f"exam-ready:{task['id']}:{uid}")
                    try:
                        await context.bot.send_message(uid,bold(f"📝 امتحان جاهز\n━━━━━━━━━━━━━━━━━━\n{task['title']}\n\n🔐 الامتحان بانتظار موافقة ولي الأمر أو الإدارة قبل التفعيل."),parse_mode=ParseMode.HTML)
                    except TelegramError: pass
                    parents=await student_parents(uid,True)
                    approval_kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تفعيل الامتحان",callback_data=f"examallow|{task['id']}|{uid}"),InlineKeyboardButton("❌ رفض",callback_data=f"examdeny|{task['id']}|{uid}")]])
                    if parents:
                        for parent in parents:
                            await db.v28_notification(parent["parent_chat_id"],"exam_approval","🔐 طلب موافقة على امتحان",f"الطالب {student['full_name']} أكمل المحتوى المطلوب لامتحان «{task['title']}». يرجى الموافقة لتفعيله.","high",entity_type="exam",entity_id=task["id"],dedupe_key=f"exam-parent:{task['id']}:{parent['parent_chat_id']}")
                            try:
                                await context.bot.send_message(parent["parent_chat_id"],bold(f"🔐 طلب موافقة على امتحان\n━━━━━━━━━━━━━━━━━━\n👤 الطالب: {student['full_name']}\n📝 {task['title']}\n\nالطالب أكمل المحتوى المطلوب. هل توافق على تفعيل الامتحان؟"),parse_mode=ParseMode.HTML,reply_markup=approval_kb)
                            except TelegramError: pass
                    else:
                        admin_chat=OWNER_CHAT_ID or task.get("created_by")
                        if admin_chat:
                            try:
                                await context.bot.send_message(admin_chat,bold(f"🔐 طلب تفعيل امتحان — لا يوجد ولي أمر مربوط\n━━━━━━━━━━━━━━━━━━\n👤 الطالب: {student['full_name']}\n📝 {task['title']}\n\nيرجى تفعيل الامتحان من زر الإدارة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تفعيل الامتحان",callback_data=f"adminexamallow|{task['id']}|{uid}"),InlineKeyboardButton("❌ رفض",callback_data=f"adminexamdeny|{task['id']}|{uid}")]]))
                            except TelegramError: pass
                else:
                    hours=int(task.get("exam_duration_hours") or DEFAULT_EXAM_HOURS)
                    deadline_label=task["deadline"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
                    await db.v28_notification(uid,"exam_published","📝 امتحان جديد",f"نزل الامتحان: {task['title']}\n⏰ آخر موعد للتسليم: {deadline_label} بتوقيت بغداد.","high",entity_type="exam",entity_id=task["id"],dedupe_key=f"exam-published:{task['id']}:{uid}")
                    try:
                        await context.bot.send_message(uid,bold(f"📝 امتحان جديد\n━━━━━━━━━━━━━━━━━━\n{task['title']}\n\n⏰ آخر موعد للتسليم: {deadline_label} بتوقيت بغداد.\nافتحه من قسم 📝 الامتحانات."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 فتح الامتحان",callback_data=f"examopen|{task['id']}")]]))
                    except TelegramError: pass
    except Exception:
        logger.exception("v30 exam dispatch failed")

async def v28_notification_job(context: ContextTypes.DEFAULT_TYPE):
    for notice in await db.v28_due_exam_notices():
        students=await students_for_scope(notice["target_scope"]); exam_at=notice["exam_at"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M"); sent=False
        for st in students:
            try: await context.bot.send_message(st["user_id"],bold(f"🔔 تبليغ امتحان تراكمي\n\n📌 {notice['title']}\n📅 الموعد: {exam_at}\n\n{notice['body']}"),parse_mode=ParseMode.HTML); sent=True
            except TelegramError: pass
            for parent in await student_parents(st["user_id"],True):
                try: await context.bot.send_message(parent["parent_chat_id"],bold(f"🔔 تبليغ امتحان تراكمي للطالب {st['full_name']}\n\n📌 {notice['title']}\n📅 الموعد: {exam_at}\n\n{notice['body']}"),parse_mode=ParseMode.HTML)
                except TelegramError: pass
        if sent or not students: await db.v28_mark_exam_notice_sent(notice["id"])

async def v28_gamification_job(context: ContextTypes.DEFAULT_TYPE):
    since=datetime.now(TIMEZONE).replace(hour=0,minute=0,second=0,microsecond=0)
    for st in await approved_students():
        try:
            if await db.v28_has_activity_since(st["user_id"],since): await db.v28_record_activity(st["user_id"],"academic activity",0)
        except Exception: logger.exception("gamification failed for %s",st["user_id"])

async def v28_admin_extend_exam_command(update,context):
    if not is_admin(update.effective_user.id): return
    if len(context.args)<2 or not all(x.isdigit() for x in context.args[:2]): await update.effective_message.reply_text(bold("الاستخدام: /extend_exam رقم_الامتحان عدد_الساعات"),parse_mode=ParseMode.HTML); return
    row=await db.v28_extend_exam(int(context.args[0]),int(context.args[1]))
    await update.effective_message.reply_text(bold("❌ لم يتم العثور على الامتحان." if not row else f"✅ تم تمديد الامتحان رقم {row['id']}.\n⏰ الموعد النهائي: {row['deadline'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')}"),parse_mode=ParseMode.HTML)

async def v28_exam_notice_command(update,context):
    if not is_admin(update.effective_user.id): return
    parts=[x.strip() for x in (update.message.text or "").partition(" ")[2].split("|")]
    if len(parts)<4: await update.effective_message.reply_text(bold("الاستخدام:\n/exam_notice المسار | العنوان | DD/MM/YYYY HH:MM | نص التبليغ\nالمسار: course أو chapter_1 ... chapter_5"),parse_mode=ParseMode.HTML); return
    scope,title,when,body=parts[:4]
    try: exam_at=datetime.strptime(when,"%d/%m/%Y %H:%M").replace(tzinfo=TIMEZONE)
    except ValueError: await update.effective_message.reply_text(bold("❌ صيغة التاريخ غير صحيحة."),parse_mode=ParseMode.HTML); return
    valid={"course",*(f"chapter_{i}" for i in range(1,6))}
    if scope not in valid: await update.effective_message.reply_text(bold("❌ المسار غير صحيح."),parse_mode=ParseMode.HTML); return
    row=await db.v28_create_exam_notice(title,body,scope,exam_at,update.effective_user.id)
    await update.effective_message.reply_text(bold(f"✅ تم حفظ تبليغ الامتحان.\n📌 {row['title']}\n📅 {exam_at.strftime('%d/%m/%Y %H:%M')}"),parse_mode=ParseMode.HTML)

legacy_button_handler=button_handler
async def button_handler(update: Update,context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; data=query.data or ""; uid=query.from_user.id
    if data=="admin_publish":
        if not is_admin(uid): await query.answer("للإدارة فقط.",show_alert=True); return
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("📚 نشر واجب",callback_data="v29_pub|homework")],
                                 [InlineKeyboardButton("📝 نشر امتحان مرتبط بتحضير",callback_data="v29_pub|exam")],
                                 [InlineKeyboardButton("🏆 نشر امتحان تراكمي",callback_data="v29_pub|cumulative")],
                                 [InlineKeyboardButton("◀️ رجوع",callback_data="menu")]])
        await query.answer(); await query.edit_message_text(bold("➕ نشر جديد\n\nاختر نوع المنشور فقط.\nلا توجد خطوات زائدة أو إعدادات معقدة."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("v29_pub|"):
        if not is_admin(uid): await query.answer("للإدارة فقط.",show_alert=True); return
        kind=data.split("|",1)[1]
        context.user_data["v29_publish"]={"kind":kind,"step":"scope"}
        if kind in {"exam","cumulative"}:
            scope_rows=[[InlineKeyboardButton("👥 الدورة الحالية (طلاب الدورة)",callback_data="v29_scope|course",style='success')]]
            scope_rows += [[InlineKeyboardButton(f"📘 الفصل {chapter}",callback_data=f"v29_scope|chapter_{chapter}",style='primary')
                            for chapter in range(start,min(start+2,6))] for start in range(1,6,2)]
            scope_rows.append([InlineKeyboardButton("❌ إلغاء",callback_data="v29_cancel",style='danger')])
            kb=InlineKeyboardMarkup(scope_rows)
        else:
            scope_rows=[[InlineKeyboardButton("👥 الدورة الحالية",callback_data="v29_scope|course",style='success')]]
            scope_rows += [[InlineKeyboardButton(f"📘 الفصل {chapter}",callback_data=f"v29_scope|chapter_{chapter}",style='primary')
                            for chapter in range(start,min(start+2,6))] for start in range(1,6,2)]
            scope_rows += [[InlineKeyboardButton("📚 جميع المسارات",callback_data="v29_scope|all",style='primary')],
                           [InlineKeyboardButton("❌ إلغاء",callback_data="v29_cancel",style='danger')]]
            kb=InlineKeyboardMarkup(scope_rows)
        await query.answer(); await query.edit_message_text(bold("🎯 اختر المسار المستهدف:"),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("v29_scope|"):
        if not is_admin(uid): await query.answer("للإدارة فقط.",show_alert=True); return
        st=context.user_data.get("v29_publish"); scope=data.split("|",1)[1]
        if not st: await query.answer("ابدأ النشر من جديد.",show_alert=True); return
        if st["kind"]=="homework":
            st.update(scope=scope,step="title")
            await query.answer(); await query.edit_message_text(bold("✍️ أرسل اسم الواجب الآن."),parse_mode=ParseMode.HTML); return
        if scope=="all":
            await query.answer("الامتحان يجب أن يحدد مساراً واحداً.",show_alert=True); return
        st.update(scope=scope,step="prep")
        if scope=="course":
            rows=await all_preparations(); kb=[]
            for r in rows[:60]:
                p=r.get("chapter_prep_no") or r.get("prep_no"); kb.append([InlineKeyboardButton(f"ف{r['chapter']} — تحضير {p} — م{str(r['lectures']).replace(',', '+م')}",callback_data=f"v29_prep|{r['chapter']}|{p}")])
            kb.append([InlineKeyboardButton("❌ إلغاء",callback_data="v29_cancel")])
            await query.answer(); await query.edit_message_text(bold("📚 اختر التحضير الذي يرتبط به الامتحان:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
        chapter=int(scope.split("_")[1]); groups=CHAPTER_PREPARATION_DISTRIBUTION.get(chapter,[]); kb=[]
        for i,nums in enumerate(groups,1): kb.append([InlineKeyboardButton(f"تحضير {i} — م{'+م'.join(map(str,nums))}",callback_data=f"v29_prep|{chapter}|{i}")])
        kb.append([InlineKeyboardButton("❌ إلغاء",callback_data="v29_cancel")])
        await query.answer(); await query.edit_message_text(bold(f"📘 الفصل {chapter}\n\nاختر التحضير المرتبط بالامتحان:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("v29_prep|"):
        if not is_admin(uid): await query.answer("للإدارة فقط.",show_alert=True); return
        st=context.user_data.get("v29_publish");
        if not st: await query.answer("ابدأ النشر من جديد.",show_alert=True); return
        _,ch,p=data.split("|"); st.update(chapter=int(ch),prep_no=int(p),step="title")
        await query.answer(); await query.edit_message_text(bold("✍️ أرسل اسم الامتحان الآن."),parse_mode=ParseMode.HTML); return
    if data=="v29_media_done":
        if not is_admin(uid): await query.answer("للإدارة فقط.",show_alert=True); return
        st=context.user_data.get("v29_publish")
        if not st or not st.get("media"): await query.answer("أرسل ملف الامتحان أو الواجب أولاً.",show_alert=True); return
        if st["kind"]=="homework":
            task=await create_task("homework",st["title"],OWNER_CHAT_ID or uid,0,-int(time.time()*1000),st["media"][0][0],st["media"][0][1],None,st["title"],datetime.now(TIMEZONE)+timedelta(hours=24),30,uid,st["scope"],"")
            for i,(t,f) in enumerate(st["media"][1:],1): await add_task_media_by_id(task["id"],t,f,-int(time.time()*1000)-i)
            await notify_task_assignment(context.bot,task)
            context.user_data.pop("v29_publish",None)
            await query.answer("تم النشر",show_alert=True); await query.edit_message_text(bold("✅ تم نشر الواجب بنجاح ووصل إشعار للطلاب المستهدفين.\n⏰ مدة التسليم: 24 ساعة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
        exam_type="cumulative" if st["kind"]=="cumulative" else "normal"
        definition=await create_linked_exam_definition([(st["chapter"],st["prep_no"])],st["title"],uid,st["media"],"course" if st["scope"]=="course" else "chapter",None,exam_type,24)
        context.user_data.pop("v29_publish",None)
        await query.answer("تم حفظ الامتحان",show_alert=True); await query.edit_message_text(bold("✅ تم نشر/تسجيل الامتحان بنجاح.\n\n📝 الدورة: يظهر تلقائياً الساعة 6 مساءً من اليوم التالي للتحضير.\n📘 الفصول: يظهر للطالب بعد إكمال التحضير، ثم يحتاج موافقة ولي الأمر أو الإدارة.\n⏱️ المدة: 24 ساعة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data=="v29_cancel":
        context.user_data.pop("v29_publish",None); await query.answer("تم الإلغاء"); await query.edit_message_text(bold("❌ تم إلغاء النشر."),parse_mode=ParseMode.HTML,reply_markup=main_menu(is_admin(uid))); return
    if data=="exams_menu":
        await query.answer(); await query.edit_message_text(bold("📝 قسم الامتحانات\n━━━━━━━━━━━━━━━━━━\nتظهر هنا الامتحانات المنشورة لمسارك فقط.\n\n• الدورة الحالية: الساعة 6 مساءً من اليوم التالي ليوم التحضير.\n• طلاب الفصول: بعد إكمال التحضير، ثم موافقة ولي الأمر أو الإدارة.\n• مدة كل امتحان تحددها الإدارة ويمكن تمديدها.\n• لا يوجد نظام منفصل باسم امتحانات اليوم."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 الامتحانات",callback_data="tasks|exam|all")],[InlineKeyboardButton("🏆 الامتحانات التراكمية",callback_data="tasks|exam|cumulative")],[InlineKeyboardButton("🗂 الامتحانات السابقة",callback_data="past_exams")],[back_menu()]])); return
    if data.startswith("tasks|exam|"): await show_tasks(query,"exam",data.split("|",2)[2]); return
    if data=="notifications":
        rows=await db.v28_unread_notifications(uid,20)
        if not rows: await query.answer("لا توجد إشعارات جديدة.",show_alert=True); return
        await db.v28_mark_notifications_read(uid,[r["id"] for r in rows]); await query.edit_message_text(bold("🔔 مركز الإشعارات\n━━━━━━━━━━━━━━━━━━\n"+"\n\n".join(f"• {r['title']}\n{r['body']}" for r in rows)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data=="academic_dashboard":
        d=await db.v28_dashboard(uid); s=d["student"]; track="الدورة الحالية" if s.get("study_track")=="course" else f"الفصل {s.get('current_chapter') or '-'}"; avg=f"{d['average']:.1f}/100" if d["average"] is not None else "لا توجد درجات بعد"
        await query.edit_message_text(bold(f"📊 لوحتي الأكاديمية\n━━━━━━━━━━━━━━━━━━\n🎯 المسار: {track}\n🎬 المحاضرات المكتملة: {d['lectures']}\n📚 الواجبات المسلّمة: {d['homeworks']}\n📝 الامتحانات المصححة: {d['exams']}\n📈 معدل الامتحانات: {avg}\n🔥 السلسلة الحالية: {d['streak']} يوم\n🏆 أفضل سلسلة: {d['best_streak']} يوم"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data in {"personal_schedule","sched_save"} or data.startswith("schednum|") or data.startswith("schedday|"):
        student=await get_student(uid)
        if not student or student.get("study_track")!="chapter":
            await query.answer("الجدول الشخصي متاح لطلاب الفصول والمسارات الشخصية.",show_alert=True); return
        chapter=int(student.get("current_chapter") or 3)
        required=weekly_study_day_count(chapter)
        names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
        if data=="personal_schedule":
            sched=await student_schedule(uid); days=set((sched or {}).get("study_days") or [])
            kb=[[InlineKeyboardButton(f"✏️ تعديل الأيام ({len(days)}/{required})",callback_data=f"schednum|{required}")],[InlineKeyboardButton("◀️ رجوع",callback_data="schedules_menu")]]
            await query.edit_message_text(bold(f"🗓️ جدولك الدراسي\n\nالفصل {chapter}: يجب أن يكون لديك {required} أيام دراسة أسبوعياً.\nالأيام الحالية: {', '.join(names[i] for i in sorted(days)) or 'لم تحدد بعد'}\n\nيمكنك استبدال يوم بيوم آخر، مع بقاء العدد {required} دائماً. الحد الأقصى للتعديلات: 3."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
        if data.startswith("schednum|"):
            context.user_data["schedule_required"]=required; context.user_data["schedule_days"]=set((await student_schedule(uid) or {}).get("study_days") or [])
            days=context.user_data["schedule_days"]; kb=[]
            for i,n in enumerate(names): kb.append([InlineKeyboardButton(("☑️ " if i in days else "☐ ")+n,callback_data=f"schedday|{i}")])
            kb.append([InlineKeyboardButton("💾 حفظ الجدول",callback_data="sched_save")],[back_menu()])
            await query.edit_message_text(bold(f"اختر بالضبط {required} أيام دراسة.\nالمحدد الآن: {len(days)}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
        if data.startswith("schedday|"):
            i=int(data.split("|")[1]); days=set(context.user_data.get("schedule_days",set()))
            if i in days: days.remove(i)
            elif len(days)<required: days.add(i)
            else: await query.answer(f"لا يمكن تجاوز {required} أيام.",show_alert=True); return
            context.user_data["schedule_days"]=days; kb=[]
            for j,n in enumerate(names): kb.append([InlineKeyboardButton(("☑️ " if j in days else "☐ ")+n,callback_data=f"schedday|{j}")])
            kb.append([InlineKeyboardButton("💾 حفظ الجدول",callback_data="sched_save")],[back_menu()])
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb)); return
        if data=="sched_save":
            days=sorted(context.user_data.get("schedule_days",set()))
            if len(days)!=required: await query.answer(f"اختر {required} أيام بالضبط.",show_alert=True); return
            result=await set_student_schedule(uid,days,"custom")
            if not result: await query.answer("لا توجد تغييرات متبقية لهذا الحساب.",show_alert=True); return
            await query.edit_message_text(bold(f"✅ تم تحديث جدولك بنجاح.\n\n📅 {', '.join(names[i] for i in days)}\n🔢 التغييرات المستخدمة: {result['schedule_change_count']}/3\n\nعدد أيام الدراسة بقي {required} أيام؛ الذي تغيّر هو اليوم فقط."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗓 تعديل الجدول",callback_data="personal_schedule")],[back_menu()]])); return
    if data=="reopen_latest_exam":
        await query.answer("تم إلغاء نظام إعادة فتح الامتحان اليومي. الامتحانات أصبحت مرتبطة بالتحضير وتدار من قسم الامتحانات.",show_alert=True); return
    if data=="admin_exam_extensions":
        if not is_admin(uid): await query.answer("للإدارة فقط.",show_alert=True); return
        await query.edit_message_text(bold("⏳ تمديد الامتحان\n\n/extend_exam رقم_الامتحان عدد_الساعات\n\nالتمديد مباشر ولا يغير مدة الامتحانات الأخرى."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    await legacy_button_handler(update,context)

_legacy_private_messages=private_messages
async def private_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st=context.user_data.get("v29_publish")
    if st and is_admin(update.effective_user.id):
        msg=update.message
        if st.get("step")=="title":
            title=(msg.text or "").strip()
            if not title: await msg.reply_text(bold("⚠️ أرسل اسماً صحيحاً."),parse_mode=ParseMode.HTML); return
            st["title"]=title; st["step"]="media"; st["media"]=[]
            await msg.reply_text(bold("📎 أرسل صورة أو PDF أو فيديو للواجب/الامتحان.\nبعد الانتهاء اضغط «تم»."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم",callback_data="v29_media_done")]])); return
        if st.get("step")=="media":
            t,f,_=message_payload(msg)
            if not f: await msg.reply_text(bold("⚠️ أرسل صورة أو PDF أو فيديو."),parse_mode=ParseMode.HTML); return
            st.setdefault("media",[]).append((t,f))
            await msg.reply_text(bold(f"✅ تمت إضافة الملف رقم {len(st['media'])}.\nيمكنك إرسال ملف آخر أو الضغط على «تم»."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم",callback_data="v29_media_done")]])); return
    return await _legacy_private_messages(update,context)

_old_main_menu=main_menu
def main_menu(admin=False):
    kb=_old_main_menu(admin); rows=[row for row in kb.inline_keyboard if not any("امتحان يومي" in (b.text or "") or "إعادة فتح" in (b.text or "") for b in row)]
    rows.insert(4,[InlineKeyboardButton("🔔 الإشعارات",callback_data="notifications"),InlineKeyboardButton("📊 لوحتي الأكاديمية",callback_data="academic_dashboard")])
    return InlineKeyboardMarkup(rows)

async def post_init(app):
    init_db()
    await backfill_personal_prep_numbers(CHAPTER_PREPARATION_DISTRIBUTION)
    await seed_preparations(preparation_rows())
    await observe_known_unactivated_members(ACTIVATION_GRACE_HOURS)
    if OWNER_CHAT_ID:
        try: await app.bot.send_message(OWNER_CHAT_ID,bold("🚀 Chemistry Bot v32 Clean Production بدأ العمل\n\n📝 امتحانات مرتبطة بالتحضير\n📚 واجبات حسب المسار\n🔔 مركز إشعارات\n🏆 تحفيز وسلاسل دراسة\n📊 تقارير أسبوعية\n🧠 محركات أكاديمية"),parse_mode=ParseMode.HTML)
        except TelegramError: pass
    app.job_queue.run_repeating(publish_preparations_job,60,first=5,name="preparations")
    app.job_queue.run_repeating(personal_preparations_job,60,first=8,name="personal_preparations")
    app.job_queue.run_repeating(linked_exam_dispatch_job,30,first=10,name="exam_engine")
    app.job_queue.run_repeating(v28_notification_job,30,first=15,name="exam_notices")
    app.job_queue.run_repeating(v28_gamification_job,3600,first=60,name="gamification")
    app.job_queue.run_repeating(scheduled_tasks_job,30,first=20,name="scheduled_tasks")
    app.job_queue.run_repeating(close_tasks_job,60,first=25,name="task_deadlines")
    app.job_queue.run_repeating(weekly_reports_job,60,first=30,name="weekly_reports")






# ========================= v30 EXAM UX / TRACK ROUTER =========================
async def _v30_send_exam_archive(query, context, task_id):
    uid=query.from_user.id
    task=await db.v30_exam_task_for_student(uid, task_id)
    if not task:
        await query.answer("هذا الامتحان غير متاح لحسابك.", show_alert=True); return
    submitted=bool(task.get("student_submitted_at"))
    closed=bool(task.get("closed")) or (task.get("deadline") and task["deadline"] <= datetime.now(TIMEZONE))
    if not submitted and not closed:
        await query.answer("هذا الامتحان ما زال ضمن الامتحانات الحالية.", show_alert=True); return
    media=await get_task_media(task_id)
    await query.edit_message_text(bold(f"🗂 الامتحان السابق\n━━━━━━━━━━━━━━━━━━\n📝 {task['title']}\n📚 الدورة الحالية\n\nهذا الامتحان محفوظ تلقائياً ضمن الامتحانات السابقة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الامتحانات السابقة",callback_data="course_past_exams"),back_menu()]]))
    for index,item in enumerate(media):
        caption=bold(task["title"]) if index==0 else None
        try:
            if item["payload_type"]=="photo": await context.bot.send_photo(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
            elif item["payload_type"]=="document": await context.bot.send_document(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
            elif item["payload_type"]=="video": await context.bot.send_video(uid,item["file_id"],caption=caption,parse_mode=ParseMode.HTML if caption else None)
        except TelegramError:
            logger.exception("Failed to send archived exam %s to %s",task_id,uid)

async def v30_show_course_current(query):
    uid=query.from_user.id
    rows=await db.v30_student_course_current_exams(uid)
    if not rows:
        await query.edit_message_text(bold("📝 امتحانات الدورة الحالية\n━━━━━━━━━━━━━━━━━━\n📭 لا يوجد امتحان مفتوح حالياً.\n\nعند نشر الأستاذ امتحاناً مرتبطاً بتحضير، سيظهر هنا تلقائياً في موعده."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗂 الامتحانات السابقة",callback_data="course_past_exams")],[back_menu()]])); return
    kb=[]
    for row in rows:
        remaining=""
        if row.get("deadline"):
            remaining=f" — ينتهي {row['deadline'].astimezone(TIMEZONE).strftime('%d/%m %H:%M')}"
        kb.append([InlineKeyboardButton(f"📝 {row['title']}{remaining}",callback_data=f"examopen|{row['id']}")])
    kb.append([InlineKeyboardButton("🗂 الامتحانات السابقة",callback_data="course_past_exams"),back_menu()])
    await query.edit_message_text(bold("📝 امتحانات الدورة الحالية\n━━━━━━━━━━━━━━━━━━\nالامتحانات المفتوحة لك الآن:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v30_show_course_past(query):
    uid=query.from_user.id
    rows=await db.v30_student_course_past_exams(uid)
    if not rows:
        await query.edit_message_text(bold("🗂 الامتحانات السابقة\n━━━━━━━━━━━━━━━━━━\n📭 لا توجد امتحانات سابقة بعد.\n\nلا تحتاج الإدارة إلى إضافة الامتحان يدوياً هنا؛ ينتقل تلقائياً بعد انتهاء مدته أو تسليمه."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ امتحانات الدورة الحالية",callback_data="course_current_exams")],[back_menu()]])); return
    kb=[]
    for row in rows[:50]:
        status="تم التسليم" if row.get("student_submitted_at") else "انتهى الوقت"
        kb.append([InlineKeyboardButton(f"🗂 {row['title']} — {status}",callback_data=f"examarchive|{row['id']}")])
    kb.append([InlineKeyboardButton("◀️ امتحانات الدورة الحالية",callback_data="course_current_exams"),back_menu()])
    await query.edit_message_text(bold("🗂 الامتحانات السابقة\n━━━━━━━━━━━━━━━━━━\nهذه القائمة تُبنى تلقائياً من الامتحانات التي نشرها الأستاذ."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v30_show_chapter_catalog(query, chapter):
    uid=query.from_user.id
    rows=await db.v30_student_chapter_exam_catalog(uid, chapter)
    if not rows:
        text=f"📘 الفصل {chapter}\n━━━━━━━━━━━━━━━━━━\n📭 لا توجد امتحانات منشورة لهذا الفصل حالياً."
        await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الفصول",callback_data="chapter_exam_menu")],[back_menu()]])); return
    kb=[]
    current_chapter=int((await get_student(uid) or {}).get("current_chapter") or 0)
    for row in rows:
        title=row["title"].replace("[تراكمي] ","").replace("[تراكمي]","")
        status=row["status"]
        if chapter != current_chapter:
            status="other_chapter"
        if status=="open":
            kb.append([InlineKeyboardButton(f"🟢 {title}",callback_data=f"examopen|{row['task_id']}")])
        elif status=="pending_approval":
            kb.append([InlineKeyboardButton(f"🔐 {title} — بانتظار موافقة ولي الأمر/الإدارة",callback_data=f"examlocked|pending|{row['task_id']}")])
        elif status=="submitted":
            kb.append([InlineKeyboardButton(f"✅ {title} — تم التسليم",callback_data="examlocked|submitted|0")])
        elif status=="closed":
            kb.append([InlineKeyboardButton(f"📕 {title} — انتهى",callback_data="examlocked|closed|0")])
        elif status=="ready_waiting_task":
            kb.append([InlineKeyboardButton(f"⏳ {title} — جارٍ تجهيز الامتحان",callback_data="examlocked|processing|0")])
        elif status=="other_chapter":
            kb.append([InlineKeyboardButton(f"🔒 {title} — هذا ليس فصلك الحالي",callback_data="examlocked|other_chapter|0")])
        else:
            done=row.get("completed_lectures",0); total=row.get("required_lectures",0)
            detail=f"أكمل التحضير ({done}/{total})" if total else "لم يكتمل المحتوى"
            kb.append([InlineKeyboardButton(f"🔒 {title} — {detail}",callback_data="examlocked|locked|0")])
    kb.append([InlineKeyboardButton("◀️ الفصول",callback_data="chapter_exam_menu"),back_menu()])
    await query.edit_message_text(bold(f"📘 امتحانات الفصل {chapter}\n━━━━━━━━━━━━━━━━━━\nتظهر جميع الامتحانات المنشورة للفصل.\n🟢 الامتحان الأخضر فقط هو القابل للفتح الآن.\n🔐 الامتحان الذي يحتاج موافقة يبقى مغلقاً حتى تصدر الموافقة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v30_exam_menu(query):
    uid=query.from_user.id
    student=await get_student(uid)
    if not student:
        await query.answer("سجّل دخولك أولاً.",show_alert=True); return
    if student.get("study_track")=="course":
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 الدورة الحالية",callback_data="course_current_exams")],
            [InlineKeyboardButton("🗂 الامتحانات السابقة",callback_data="course_past_exams")],
            [back_menu()],
        ])
        await query.edit_message_text(bold("📝 الامتحانات — الدورة الحالية\n━━━━━━━━━━━━━━━━━━\nهذا القسم مخصص لطلاب الدورة الحالية.\n\n📝 الامتحانات الحالية تظهر في موعدها تلقائياً.\n🗂 الامتحانات السابقة تُحفظ تلقائياً، ولا تحتاج الإدارة إلى إضافتها مرة ثانية."),parse_mode=ParseMode.HTML,reply_markup=kb)
    else:
        chapter_names=["الأول","الثاني","الثالث","الرابع","الخامس","السادس","السابع"]
        kb=InlineKeyboardMarkup([[InlineKeyboardButton(f"📘 الفصل {name}",callback_data=f"chapter_exam|{index}")]
                                 for index,name in enumerate(chapter_names,1)]+[[back_menu()]])
        await query.edit_message_text(bold("📝 الامتحانات\n━━━━━━━━━━━━━━━━━━\nاختر الفصل لعرض جميع امتحاناته.\n\n🔒 الامتحانات غير المستحقة تبقى مغلقة.\n🔐 عند إكمال المحتوى يصبح الامتحان بانتظار موافقة ولي الأمر أو الإدارة.\n🟢 بعد الموافقة فقط يمكن فتح الامتحان."),parse_mode=ParseMode.HTML,reply_markup=kb)

_old_button_handler_v30=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data; uid=query.from_user.id
    if data=="exams_menu":
        await query.answer(); await v30_exam_menu(query); return
    if data=="course_current_exams":
        await query.answer(); await v30_show_course_current(query); return
    if data=="course_past_exams":
        await query.answer(); await v30_show_course_past(query); return
    if data=="chapter_exam_menu":
        await query.answer(); await v30_exam_menu(query); return
    if data.startswith("chapter_exam|"):
        await query.answer(); await v30_show_chapter_catalog(query,int(data.split("|")[1])); return
    if data.startswith("examlocked|"):
        reason=data.split("|")[1]
        messages={"pending":"⏳ هذا الامتحان جاهز، لكنه بانتظار موافقة ولي الأمر أو الإدارة.","submitted":"✅ هذا الامتحان تم تسليمه مسبقاً.","closed":"⏰ انتهت مدة هذا الامتحان.","processing":"⏳ تم إكمال المحتوى، والبوت يجهز الامتحان الآن. أعد فتح القسم بعد لحظات.","locked":"🔒 لا يمكنك فتح هذا الامتحان بعد. أكمل المحتوى المطلوب أولاً.","other_chapter":"🔒 هذا الامتحان مخصص لفصل آخر وليس لمسارك الحالي."}
        await query.answer(messages.get(reason,"🔒 هذا الامتحان غير متاح حالياً."),show_alert=True); return
    if data.startswith("examopen|"):
        task_id=int(data.split("|")[1]); task=await db.v30_exam_task_for_student(uid,task_id)
        if not task:
            await query.answer("هذا الامتحان غير متاح لحسابك.",show_alert=True); return
        if task.get("exam_pending_activation"):
            await query.answer("🔐 الامتحان بانتظار موافقة ولي الأمر أو الإدارة.",show_alert=True); return
        if task.get("target_scope")=="chapter":
            student=await get_student(uid)
            if not student or int(student.get("current_chapter") or 0)!=int(task.get("chapter") or 0):
                await query.answer("🔒 هذا الامتحان مخصص لفصل آخر وليس لمسارك الحالي.",show_alert=True); return
        if task.get("closed") or (task.get("deadline") and task["deadline"]<=datetime.now(TIMEZONE)):
            await query.answer("⏰ انتهت مدة الامتحان.",show_alert=True); return
        await show_task(query,context,task_id); return
    if data.startswith("examarchive|"):
        await query.answer(); await _v30_send_exam_archive(query,context,int(data.split("|")[1])); return
    return await _old_button_handler_v30(update,context)

# The legacy group auto-capture is no longer the publication path for exams/homework.
_old_capture_group_task_v30=capture_group_task
async def capture_group_task(update,context):
    if update.effective_chat and update.effective_chat.id in {EXAM_GROUP_ID, HOMEWORK_GROUP_ID}:
        return
    return await _old_capture_group_task_v30(update,context)


# ========================= v31 EXAM DELETE + REAL STUDENT EXTENSIONS =========================

async def v31_admin_exam_catalog(query):
    uid=query.from_user.id
    if not is_admin(uid):
        await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
    rows=await v31_active_exam_definitions()
    if not rows:
        await query.edit_message_text(bold("🗑 إدارة الامتحانات\n━━━━━━━━━━━━━━━━━━\n📭 لا توجد امتحانات منشورة حالياً."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    kb=[]
    for d in rows[:80]:
        scope="الدورة الحالية" if d.get("target_scope")=="course" else f"الفصل {d.get('chapter') or '-'}"
        typ="تراكمي" if d.get("exam_type")=="cumulative" else "امتحان"
        title=str(d.get("title") or "").replace("[تراكمي] ","").replace("[تراكمي]","")
        kb.append([InlineKeyboardButton(f"📝 {title} — {scope} ({typ})",callback_data=f"adminexamdelete|{d['id']}")])
    kb.append([back_menu()])
    await query.edit_message_text(bold("🗑 إدارة الامتحانات\n━━━━━━━━━━━━━━━━━━\nاختر الامتحان الذي تريد حذفه نهائياً.\n\n⚠️ الحذف النهائي يزيل تعريف الامتحان وملفاته ومحاولاته ودرجاته وتمديداته من قاعدة البيانات. يبقى XP المكتسب للطلاب ولا تُحذف الرسائل المنشورة داخل مجموعات Telegram."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v31_close_tasks_job(context):
    # A student extension is per-student. Keep the task globally open until the
    # last active extension expires; effective_task_deadline blocks everyone else
    # at the original deadline and allows only the extended student to continue.
    for task in await due_tasks():
        warned,removed=await issue_missing_task_warnings(context,task)
        if await task_has_active_extensions(task["id"]):
            continue
        if await close_task(task["id"]):
            try:
                await context.bot.send_message(task["chat_id"],bold(f"⏰ تم إغلاق {task['title']}.\n⚠️ الإنذارات: {warned}\n🚫 المحظورون: {removed}"),parse_mode=ParseMode.HTML,message_thread_id=task.get("thread_id") or None)
            except TelegramError: pass
            if task["kind"]=="exam" and not task.get("questions_released"):
                try: await release_exam_questions(context,task)
                except TelegramError as exc: logger.warning("Exam question release failed for %s: %s",task["id"],exc)
            await announce_champions(context,task["id"])
    for task in await recently_closed_tasks_for_warning_recovery():
        await issue_missing_task_warnings(context,task)
    for task in await unreleased_closed_exams():
        try: await release_exam_questions(context,task)
        except TelegramError as exc: logger.warning("Exam question release recovery failed for %s: %s",task["id"],exc)

# Keep a reference to the currently active v30 handler before wrapping it.
_v31_previous_button_handler=button_handler

async def button_handler(update,context):
    query=update.callback_query; data=query.data; uid=query.from_user.id
    if data=="admin_exam_delete_menu":
        await query.answer(); await v31_admin_exam_catalog(query); return
    if data.startswith("adminexamdelete|"):
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        did=int(data.split("|")[1]); d=await v31_exam_definition_for_admin(did)
        if not d: await query.answer("الامتحان غير موجود أو حُذف مسبقاً.",show_alert=True); return
        title=str(d.get("title") or "").replace("[تراكمي] ","").replace("[تراكمي]","")
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 نعم، حذف نهائي",callback_data=f"adminexamdeleteconfirm|{did}")],[InlineKeyboardButton("❌ تراجع",callback_data="admin_exam_delete_menu")]])
        await query.edit_message_text(bold(f"⚠️ تأكيد الحذف النهائي\n━━━━━━━━━━━━━━━━━━\n📝 {title}\n\nسيُحذف الامتحان وملفاته ومحاولاته ودرجاته وتمديداته نهائياً من قاعدة البيانات. يبقى XP المكتسب للطلاب فقط. لا يمكن التراجع بعد التأكيد."),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data.startswith("adminexamdeleteconfirm|"):
        if not is_admin(uid): await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
        did=int(data.split("|")[1]); row=await v31_delete_exam_definition(did,uid)
        if not row: await query.answer("الامتحان غير موجود أو حُذف مسبقاً.",show_alert=True); return
        await query.answer(f"تم الحذف النهائي: {row.get('deleted_tasks',0)} مهمة و{row.get('deleted_submissions',0)} تسليم",show_alert=True)
        await v31_admin_exam_catalog(query); return
    if data.startswith("examopen|"):
        task_id=int(data.split("|")[1]); task=await db.v30_exam_task_for_student(uid,task_id)
        if not task:
            await query.answer("هذا الامتحان غير متاح لحسابك.",show_alert=True); return
        if task.get("exam_pending_activation"):
            await query.answer("🔐 الامتحان بانتظار موافقة ولي الأمر أو الإدارة.",show_alert=True); return
        if task.get("target_scope")=="chapter":
            student=await get_student(uid)
            if not student or int(student.get("current_chapter") or 0)!=int(task.get("chapter") or 0):
                await query.answer("🔒 هذا الامتحان مخصص لفصل آخر وليس لمسارك الحالي.",show_alert=True); return
        effective=await effective_task_deadline(task_id,uid)
        if not effective or not effective.get("assigned") or (effective.get("deadline") and effective["deadline"]<=datetime.now(TIMEZONE) and not effective.get("submitted")):
            await query.answer("⏰ انتهت مدة الامتحان. إذا كان لديك تمديد معتمد فافتح قسم الامتحانات من جديد.",show_alert=True); return
        await show_task(query,context,task_id); return
    if data.startswith("extendhours|"):
        # The student request is now backed by a real per-student deadline.
        _,task_s,hours_s=data.split("|"); task_id,hours=int(task_s),int(hours_s)
        if not 1<=hours<=24: await query.answer("مدة التمديد غير صحيحة.",show_alert=True); return
        task=await get_task(task_id)
        if not task or task["kind"]!="exam": await query.answer("التمديد متاح للامتحانات فقط.",show_alert=True); return
        result=await create_extension_request(task_id,uid,hours)
        if result.get("status")=="xp": await query.answer("تحتاج 150 XP لطلب التمديد.",show_alert=True); return
        if result.get("status")=="limit": await query.answer("استخدمت طلبي التمديد لهذا الأسبوع.",show_alert=True); return
        if result.get("status")=="exists": await query.answer("يوجد طلب تمديد لهذا الامتحان قيد المعالجة.",show_alert=True); return
        if result.get("status")=="submitted": await query.answer("تم تسليم الامتحان بالفعل.",show_alert=True); return
        if result.get("status") in {"closed","late"}: await query.answer("انتهى وقت طلب التمديد لهذا الامتحان.",show_alert=True); return
        if result.get("status")=="parent_missing": await query.answer("لا يوجد ولي أمر مربوط بحسابك لاستلام الموافقة.",show_alert=True); return
        if result.get("status")!="ok": await query.answer("لا يمكن طلب التمديد الآن.",show_alert=True); return
        s=result["student"]; req=result["request"]
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم",callback_data=f"extapprove|{req['id']}|{uid}"),InlineKeyboardButton("❌ لا",callback_data=f"extdeny|{req['id']}|{uid}")]])
        try:
            await context.bot.send_message(s["parent_chat_id"],bold(f"⏳ طلب تمديد امتحان\nالطالب {s['full_name']} طلب تمديد «{task['title']}» لمدة {hours} ساعة.\nالكلفة: 150 XP. هل توافق؟"),parse_mode=ParseMode.HTML,reply_markup=kb)
        except TelegramError:
            await decide_extension_request(req["id"],False)
            await query.answer("تعذر إيصال الطلب إلى ولي الأمر.",show_alert=True); return
        await query.edit_message_text(bold("⏳ تم إرسال طلب التمديد إلى ولي الأمر.\nعند الموافقة سيُضاف الوقت لهذا الطالب فقط، ولن يتغير وقت بقية الطلاب."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    return await _v31_previous_button_handler(update,context)

# Add admin deletion entry without disturbing the rest of the menu.
_old_v31_main_menu=main_menu
def main_menu(admin=False):
    kb=_old_v31_main_menu(admin)
    if admin:
        rows=list(kb.inline_keyboard)
        if not any(any("إدارة الامتحانات" in (b.text or "") for b in row) for row in rows):
            rows.append([InlineKeyboardButton("🗑 إدارة الامتحانات وحذفها",callback_data="admin_exam_delete_menu")])
        return InlineKeyboardMarkup(rows)
    return kb

# Initialize the soft-delete column before scheduler jobs start.
_old_v31_post_init=post_init
async def post_init(app):
    await v31_init_exam_controls()
    await _old_v31_post_init(app)


# ========================= v32 CLEAN EXAM MANAGEMENT =========================
# This release makes the exam-management controls authoritative and keeps the
# historical exam/submission rows intact when an exam is deleted from the UI.

async def v32_admin_exam_tasks(query):
    uid=query.from_user.id
    if not is_admin(uid):
        await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
    rows=await db.v32_admin_exam_tasks()
    if not rows:
        await query.edit_message_text(bold("⏳ تمديد امتحان لطالب\n━━━━━━━━━━━━━━━━━━\n📭 لا توجد امتحانات مرتبطة حالياً."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    kb=[]
    for row in rows[:80]:
        scope="الدورة الحالية" if row.get("target_scope")=="course" else f"الفصل {row.get('chapter') or '-'}"
        state="🔒 مغلق" if row.get("closed") else "🟢 مفتوح"
        kb.append([InlineKeyboardButton(f"📝 {row['title'].replace('[تراكمي] ','')} — {scope} | {state} | 👥 {row.get('student_count',0)}",callback_data=f"adminexamtask|{row['id']}")])
    kb.append([back_menu()])
    await query.edit_message_text(bold("⏳ تمديد امتحان لطالب\n━━━━━━━━━━━━━━━━━━\nاختر الامتحان، ثم اختر الطالب، ثم مدة التمديد.\n\n✅ التمديد هنا خاص بطالب واحد فقط ولا يغيّر موعد بقية الطلاب."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v32_admin_exam_students(query, task_id):
    uid=query.from_user.id
    if not is_admin(uid):
        await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
    task=await get_task(task_id)
    if not task or task.get("kind")!="exam":
        await query.answer("الامتحان غير موجود.",show_alert=True); return
    students=await db.v32_admin_exam_students(task_id)
    if not students:
        await query.edit_message_text(bold("📭 لا يوجد طلاب مرتبطون بهذا الامتحان."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الامتحانات",callback_data="admin_exam_extensions")],[back_menu()]])); return
    kb=[]
    for s in students[:100]:
        status="✅ مُسلّم" if s.get("submitted") else "⏳ لم يُسلّم"
        ext=s.get("extended_until")
        ext_text=f" | تمديد إلى {ext.astimezone(TIMEZONE).strftime('%d/%m %H:%M')}" if ext else ""
        kb.append([InlineKeyboardButton(f"👤 {s['full_name']} | {status}{ext_text}",callback_data=f"adminexamstudent|{task_id}|{s['user_id']}")])
    kb.append([InlineKeyboardButton("◀️ الامتحانات",callback_data="admin_exam_extensions"),back_menu()])
    await query.edit_message_text(bold(f"📝 {task['title']}\n━━━━━━━━━━━━━━━━━━\nاختر الطالب الذي تريد تمديد امتحانه:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v32_admin_extend_student_menu(query, task_id, student_id):
    uid=query.from_user.id
    if not is_admin(uid):
        await query.answer("هذا القسم للإدارة فقط.",show_alert=True); return
    task=await get_task(task_id); student=await get_student(student_id)
    if not task or task.get("kind")!="exam" or not student:
        await query.answer("بيانات الامتحان أو الطالب غير موجودة.",show_alert=True); return
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("ساعة",callback_data=f"adminexamextend|{task_id}|{student_id}|1"),InlineKeyboardButton("ساعتان",callback_data=f"adminexamextend|{task_id}|{student_id}|2")],
        [InlineKeyboardButton("6 ساعات",callback_data=f"adminexamextend|{task_id}|{student_id}|6"),InlineKeyboardButton("12 ساعة",callback_data=f"adminexamextend|{task_id}|{student_id}|12")],
        [InlineKeyboardButton("24 ساعة",callback_data=f"adminexamextend|{task_id}|{student_id}|24"),InlineKeyboardButton("48 ساعة",callback_data=f"adminexamextend|{task_id}|{student_id}|48")],
        [InlineKeyboardButton("72 ساعة",callback_data=f"adminexamextend|{task_id}|{student_id}|72")],
        [InlineKeyboardButton("◀️ الطلاب",callback_data=f"adminexamtask|{task_id}"),back_menu()]
    ])
    await query.edit_message_text(bold(f"⏳ تمديد امتحان الطالب\n━━━━━━━━━━━━━━━━━━\n👤 الطالب: {student['full_name']}\n📝 الامتحان: {task['title']}\n\nاختر عدد الساعات.\n\n⚠️ التمديد لهذا الطالب فقط، ولا يحتاج موافقة ولي الأمر لأن الطلب صادر من الإدارة."),parse_mode=ParseMode.HTML,reply_markup=kb)

# Wrap the v31 handler one final time; this is the only active v32 management layer.
_v32_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data; uid=query.from_user.id
    if data=="admin_exam_extensions":
        await query.answer(); await v32_admin_exam_tasks(query); return
    if data.startswith("adminexamtask|"):
        if not is_admin(uid): await query.answer("للإدارة فقط.",show_alert=True); return
        await query.answer(); await v32_admin_exam_students(query,int(data.split("|")[1])); return
    if data.startswith("adminexamstudent|"):
        if not is_admin(uid): await query.answer("للإدارة فقط.",show_alert=True); return
        _,task_s,student_s=data.split("|"); await query.answer(); await v32_admin_extend_student_menu(query,int(task_s),int(student_s)); return
    if data.startswith("adminexamextend|"):
        if not is_admin(uid): await query.answer("للإدارة فقط.",show_alert=True); return
        _,task_s,student_s,hours_s=data.split("|"); task_id,student_id,hours=int(task_s),int(student_s),int(hours_s)
        if not 1<=hours<=72:
            await query.answer("مدة التمديد غير صحيحة.",show_alert=True); return
        result=await db.v32_admin_extend_student_exam(task_id,student_id,hours,uid)
        status=result.get("status") if result else "error"
        if status=="not_found": await query.answer("الامتحان أو الطالب غير موجود.",show_alert=True); return
        if status=="submitted": await query.answer("الطالب سلّم الامتحان بالفعل ولا يمكن تمديده.",show_alert=True); return
        if status!="ok": await query.answer("تعذر تنفيذ التمديد.",show_alert=True); return
        deadline=result["extended_until"].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')
        await query.answer("تم تمديد امتحان الطالب",show_alert=True)
        try:
            await context.bot.send_message(student_id,bold(f"⏳ تم تمديد امتحانك من الإدارة\n📝 {result['title']}\n👤 الطالب: {result['student_name']}\n🕐 الموعد الجديد: {deadline}\n\nهذا التمديد خاص بحسابك فقط."),parse_mode=ParseMode.HTML)
        except TelegramError: pass
        await v32_admin_exam_students(query,task_id); return
    return await _v32_previous_button_handler(update,context)

# The single runtime entry point is intentionally the last executable statement.

# ========================= v37 COMPLETE ALL-FEATURES OVERRIDES =========================
# This layer is intentionally last so the final runtime behavior is deterministic.

async def v37_notify_student(context, user_id, kind, title, body, priority="normal", dedupe_key=None):
    """Persist every notification first, then deliver it as a Telegram message."""
    try:
        row=await db.v37_enqueue_notification(user_id,kind,title,body,priority=priority,dedupe_key=dedupe_key)
        return row
    except Exception:
        logger.exception("notification persistence failed for %s",user_id)
        return None

async def v37_notification_delivery_job(context: ContextTypes.DEFAULT_TYPE):
    """Deliver the durable outbox exactly once after Telegram confirms sending."""
    for notice in await db.v39_due_notification_queue(100):
        try:
            await context.bot.send_message(notice["user_id"],bold(f"🔔 {notice['title']}\n\n{notice['body']}"),parse_mode=ParseMode.HTML)
            await db.v39_mark_notification_delivery(notice["queue_id"],True)
        except TelegramError as exc:
            await db.v39_mark_notification_delivery(notice["queue_id"],False,exc)
            logger.warning("notification retry pending for %s",notice["user_id"])

async def v37_notifications_menu(query):
    uid=query.from_user.id
    rows=await db.v39_notifications(uid,limit=30)
    if not rows:
        await query.edit_message_text(bold("🔔 سجل الإشعارات\n━━━━━━━━━━━━━━━━━━\n📭 لا توجد إشعارات محفوظة."),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    lines=["🔔 الإشعارات",DIV]
    for n in rows[:15]:
        when=n["created_at"].astimezone(TIMEZONE).strftime("%d/%m %H:%M") if getattr(n["created_at"],"tzinfo",None) else n["created_at"].strftime("%d/%m %H:%M")
        lines.append(f"• {when} | {n['title']}\n  {n['body'][:180]}")
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تعليم الكل كمقروء",callback_data="notifications_read_all")],[back_menu()]]))

async def v37_track_change_admin_menu(query):
    if not is_admin(query.from_user.id):
        await query.answer("للإدارة فقط.",show_alert=True); return
    rows=await db.v37_pending_track_requests()
    if not rows:
        await query.edit_message_text(bold("🔄 طلبات تغيير مسار الدراسة\n━━━━━━━━━━━━━━━━━━\n📭 لا توجد طلبات معلقة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    kb=[]
    for r in rows[:80]:
        target="الدورة الحالية" if r["requested_track"]=="course" else f"الفصل {r['requested_chapter']}"
        kb.append([InlineKeyboardButton(f"👤 {r['full_name']} ← {target}",callback_data=f"trackreq|{r['id']}")])
    kb.append([back_menu()])
    await query.edit_message_text(bold("🔄 طلبات تغيير مسار الدراسة\n━━━━━━━━━━━━━━━━━━\nاختر الطلب للمراجعة:"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v37_track_request_review(query, request_id):
    if not is_admin(query.from_user.id):
        await query.answer("للإدارة فقط.",show_alert=True); return
    rows=await db.v37_pending_track_requests()
    req=next((r for r in rows if int(r["id"])==request_id),None)
    if not req:
        await query.answer("الطلب غير موجود أو تمت معالجته.",show_alert=True); return
    target="الدورة الحالية" if req["requested_track"]=="course" else f"الفصل {req['requested_chapter']}"
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ موافقة",callback_data=f"trackdecision|{request_id}|1"),InlineKeyboardButton("❌ رفض",callback_data=f"trackdecision|{request_id}|0")],
        [InlineKeyboardButton("◀️ الطلبات",callback_data="track_change_requests"),back_menu()]])
    await query.edit_message_text(bold(f"🔄 طلب تغيير مسار دراسة\n━━━━━━━━━━━━━━━━━━\n👤 الطالب: {req['full_name']}\n🎯 المسار المطلوب: {target}\n📅 يبدأ من: {req['requested_start_date'].strftime('%d/%m/%Y')}\n\nالطالب استنفد التغييرات الثلاثة التلقائية؛ لا ينفذ التغيير إلا بعد قرار الإدارة."),parse_mode=ParseMode.HTML,reply_markup=kb)

async def v37_apply_track_choice(query, context, choice):
    uid=query.from_user.id
    student=await get_student(uid)
    if not student:
        await query.answer("سجّل حسابك أولاً.",show_alert=True); return
    if choice=="course":
        result=await db.v37_request_track_change(uid,"course",3,datetime.now(TIMEZONE).date(),[])
        target="الدورة الحالية"
    else:
        chapter=int(choice)
        if chapter not in range(1,6):
            await query.answer("الفصل غير صحيح.",show_alert=True); return
        plan=build_personal_plan(chapter,datetime.now(TIMEZONE).date())
        result=await db.v37_request_track_change(uid,"chapter",chapter,datetime.now(TIMEZONE).date(),plan)
        target=f"الفصل {chapter}"
    if result["status"]=="pending":
        await query.answer("تم إرسال الطلب للإدارة.",show_alert=True)
        for admin_id in (set(ADMIN_IDS)|set(FOUNDER_IDS)|({OWNER_CHAT_ID} if OWNER_CHAT_ID else set())):
            try:
                await context.bot.send_message(admin_id,bold(f"🔄 طلب تغيير مسار جديد\nالطالب: {student['full_name']}\nالمسار المطلوب: {target}\n\nالطالب استنفد 3 تغييرات تلقائية ويحتاج موافقة الإدارة."),parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 مراجعة الطلبات",callback_data="track_change_requests")]]))
            except TelegramError: pass
        await query.edit_message_text(bold("⏳ تم تسجيل طلب تغيير المسار.\n\nاستنفدت مرات التغيير الثلاث التلقائية، لذلك ينتظر الطلب موافقة الإدارة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if result["status"]!="ok":
        await query.answer("تعذر تغيير المسار.",show_alert=True); return
    await query.answer("تم تغيير المسار بنجاح.",show_alert=True)
    await v37_notify_student(context,uid,"track_change","تحديث مسار الدراسة",f"تم تغيير مسارك إلى {target} وإعادة بناء جدولك الدراسي من تاريخ اليوم.","high",f"track_change:{uid}:{result['count']}")
    reset_text=("🔁 بدأ الفصل من المحاضرة 1، واعيدت محاضراته وامتحاناته من البداية.\n⭐ رصيد XP السابق بقي محفوظا."
                if choice!="course" else "📅 تم ربط حسابك بجدول الدورة الرسمي، ولا يمكن تعديله من الطالب.")
    await query.edit_message_text(bold(f"✅ تم تغيير مسار الدراسة إلى {target}.\n\n🔄 التغييرات التلقائية المستخدمة: {result['count']}/3\n{reset_text}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📅 خطة إكمال الفصول",callback_data="chapter_completion_schedule")],[back_menu()]]))

async def v37_schedule_menu(query):
    sched=await db.student_schedule(query.from_user.id)
    days=list((sched or {}).get("study_days") or [])
    names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
    text=f"🗓️ إعداد الجدول الشخصي\n{DIV}\nاختر عدد أيام الدراسة، ثم حدّد الأيام.\n\n📅 الحالي: {', '.join(names[i] for i in days) if days else 'غير محدد'}"
    kb=[[InlineKeyboardButton(f"{n} أيام",callback_data=f"v37_sched_count|{n}")] for n in range(1,8)]
    kb.append([back_menu()])
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v37_schedule_count(query,context,n):
    sched=await db.student_schedule(query.from_user.id)
    old=set((sched or {}).get("study_days") or [])
    selected=set(list(old)[:n])
    for j in range(7):
        if len(selected)>=n: break
        selected.add(j)
    context.user_data["v37_schedule_required"]=n
    context.user_data["v37_schedule_days"]=sorted(selected)
    names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
    kb=[[InlineKeyboardButton(("☑️ " if j in selected else "☐ ")+names[j],callback_data=f"v37_sched_day|{j}")] for j in range(7)]
    kb += [[InlineKeyboardButton("💾 حفظ الجدول",callback_data="v37_sched_save")],[InlineKeyboardButton("◀️ العدد",callback_data="v37_personal_schedule"),back_menu()]]
    await query.edit_message_text(bold(f"🗓️ اختر أيام الدراسة\n\nالمطلوب: {n} أيام بالضبط.\nالمحدد: {len(selected)}/{n}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v37_schedule_day(query,context,i):
    days=set(context.user_data.get("v37_schedule_days",[]))
    required=int(context.user_data.get("v37_schedule_required",1))
    if i in days: days.remove(i)
    elif len(days)<required: days.add(i)
    else:
        await query.answer(f"اختر {required} أيام فقط.",show_alert=True); return
    context.user_data["v37_schedule_days"]=sorted(days)
    names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
    kb=[[InlineKeyboardButton(("☑️ " if j in days else "☐ ")+names[j],callback_data=f"v37_sched_day|{j}")] for j in range(7)]
    kb += [[InlineKeyboardButton("💾 حفظ الجدول",callback_data="v37_sched_save")],[InlineKeyboardButton("◀️ العدد",callback_data="v37_personal_schedule"),back_menu()]]
    await query.edit_message_text(bold(f"🗓️ اختر أيام الدراسة\n\nالمطلوب: {required} أيام بالضبط.\nالمحدد: {len(days)}/{required}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v37_schedule_save(query,context):
    days=context.user_data.get("v37_schedule_days",[])
    required=int(context.user_data.get("v37_schedule_required",len(days)))
    if len(days)!=required:
        await query.answer(f"يجب اختيار {required} أيام بالضبط.",show_alert=True); return
    result=await db.v37_set_student_schedule(query.from_user.id,days,"custom")
    if result.get("status")!="ok":
        await query.answer("تعذر حفظ الجدول.",show_alert=True); return
    context.user_data.pop("v37_schedule_days",None); context.user_data.pop("v37_schedule_required",None)
    names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
    await query.answer("تم تحديث الجدول.",show_alert=True)
    await query.edit_message_text(bold(f"✅ تم تحديث جدولك.\n\n📅 أيام الدراسة: {', '.join(names[i] for i in days)}\n\n🔄 تم إعادة توزيع التحاضير المستقبلية على الأيام الجديدة.\n📚 لا يوجد حد لعدد مرات تعديل أيام الجدول."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗓️ تعديل الجدول",callback_data="v37_personal_schedule")],[back_menu()]]))

async def v37_completion_menu(query):
    plan=await db.v37_chapter_completion_plan(query.from_user.id)
    lines=["📅 خطة إكمال الفصول",DIV]
    for r in plan["chapters"]:
        finish=r["finish_date"]
        date_text=finish.strftime("%d/%m/%Y") if finish else "-"
        lines.append(f"📘 الفصل {r['chapter']} — {r['prep_count']} تحضير — 🏁 {date_text}")
    full=plan["full_finish"].strftime("%d/%m/%Y") if plan["full_finish"] else "غير متاح"
    lines += [DIV,f"🏆 إكمال المنهج كاملًا: {full}"]
    kb=[]
    for r in plan["chapters"]:
        kb.append([InlineKeyboardButton(f"📘 تفاصيل الفصل {r['chapter']}",callback_data=f"v37_chapter_finish|{r['chapter']}")])
    kb += [[InlineKeyboardButton("🔄 تحديث الخطة",callback_data="v37_completion")],[back_menu()]]
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))

async def v37_chapter_finish(query,chapter):
    plan=await db.v37_chapter_completion_plan(query.from_user.id)
    row=next((r for r in plan["chapters"] if int(r["chapter"])==chapter),None)
    if not row:
        await query.answer("لا توجد بيانات لهذا الفصل.",show_alert=True); return
    finish=row["finish_date"].strftime("%d/%m/%Y") if row["finish_date"] else "-"
    await query.edit_message_text(bold(f"📘 خطة إكمال الفصل {chapter}\n{DIV}\n📚 عدد التحاضير: {row['prep_count']}\n🏁 موعد إكمال الفصل: {finish}\n\nيتحدث الموعد تلقائياً عند تغيير أيام الجدول أو تغيير مسار الدراسة أو الموافقة على إجازة."),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📅 خطة كل الفصول",callback_data="v37_completion")],[back_menu()]]))


async def v39_calendar_menu(query,period="week"):
    today=datetime.now(TIMEZONE).date(); start=today-timedelta(days=today.weekday())
    if period=="month":
        start=today.replace(day=1)
        next_month=start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)
        end=next_month-timedelta(days=1); title="📆 جدولك الدراسي لهذا الشهر"
    else:
        end=start+timedelta(days=6); title="🗓 جدولك الدراسي لهذا الأسبوع"
    calendar=await db.v39_student_calendar(query.from_user.id,start,end)
    student=calendar.get("student") or {}; names=["الاثنين","الثلاثاء","الأربعاء","الخميس","الجمعة","السبت","الأحد"]
    lines=[title,DIV,f"📚 المسار: {'الدورة الحالية' if student.get('study_track')=='course' else 'الفصل '+str(student.get('current_chapter') or '-')}",""]
    by_date={}
    for prep in calendar.get("preparations",[]): by_date.setdefault(prep["target_date"],[]).append(prep)
    cursor=start
    while cursor<=end:
        if cursor in by_date:
            lines.append(f"📅 {names[cursor.weekday()]} {cursor:%d/%m}")
            for prep in by_date[cursor]: lines.append(f"  • تحضير {prep.get('prep_no') or '-'} — الفصل {prep['chapter']} — محاضرات {prep['lectures']}")
        cursor+=timedelta(days=1)
    if len(lines)==4: lines.append("لا توجد تحاضير مجدولة ضمن هذه الفترة.")
    kb=[[InlineKeyboardButton("🗓 هذا الأسبوع",callback_data="student_calendar|week"),InlineKeyboardButton("📆 هذا الشهر",callback_data="student_calendar|month")],
        [InlineKeyboardButton("⚙️ تعديل أيام الدراسة",callback_data="personal_schedule")],[back_menu()]]
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v39_daily_session(query):
    uid=query.from_user.id; student=await get_student(uid)
    if not student:
        await query.edit_message_text(bold("🔒 يجب تسجيل حساب الطالب أولا من /start."),parse_mode=ParseMode.HTML); return
    blocking=await student_exam_lock(uid)
    if blocking:
        text=f"🎯 جلسة اليوم\n{DIV}\n1️⃣ المهمة الأهم الآن: تسليم الامتحان المستحق\n📝 {blocking['title']}\n\nبعد التسليم سيفتح التحضير التالي تلقائياً."
        kb=[[InlineKeyboardButton("📝 فتح الامتحان الآن",callback_data=f"task|{blocking['id']}")],[back_menu()]]
    else:
        personal=bool(student and (student.get("study_track")=="chapter" or student.get("schedule_mode")=="custom"))
        prep=await personal_preparation_for_student(uid,datetime.now(TIMEZONE).date()) if personal else await preparation_for_date(datetime.now(TIMEZONE).date())
        if not prep and not personal: prep=await latest_preparation()
        if prep:
            nums=[int(x) for x in str(prep.get("lectures") or "").split(",") if x.strip().isdigit()]
            text=f"🎯 جلسة اليوم\n{DIV}\n📘 الفصل {prep.get('chapter') or student.get('current_chapter') or 3}\n🎬 المحاضرات: {', '.join(map(str,nums))}\n\nابدأ بمحاضرة واحدة، ثم اختبر نفسك من بنك الأسئلة."
            chapter=int(prep.get("chapter") or student.get("current_chapter") or 3)
            kb=[[InlineKeyboardButton(f"▶️ ابدأ المحاضرة {n}",callback_data=f"prepopen|{chapter}|{n}")] for n in nums[:6]]
            kb += [[InlineKeyboardButton("🧠 اختبار مراجعة ذكي",callback_data="study_quiz")],[back_menu()]]
        else:
            text=f"🎯 جلسة اليوم\n{DIV}\n✅ لا يوجد تحضير مستحق الآن. استخدم المراجعة الذكية لتثبيت المعلومات."
            kb=[[InlineKeyboardButton("🧠 ابدأ مراجعة ذكية",callback_data="study_quiz")],[back_menu()]]
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v39_mastery_menu(query):
    stats=await db.v39_learning_mastery(query.from_user.id); lines=["🧭 خريطة إتقان المنهج",DIV]
    for chapter in range(1,6):
        done=int(stats["lectures"].get(chapter,0)); total=len(PLAYLISTS.get(chapter,[])); pct=round(done*100/total) if total else 0
        grade=stats["grades"].get(chapter); grade_text=f" | معدل الامتحان {grade:.1f}" if grade is not None else ""
        lines.append(f"الفصل {chapter}: {done}/{total} محاضرة — {pct}%{grade_text}")
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 المراجعة الملكية",callback_data="royal_review_menu"),InlineKeyboardButton("🎯 نقاط ضعفي",callback_data="weaknesses_menu")],[back_menu()]]))


async def v39_quiz_question(query):
    student=await get_student(query.from_user.id); chapter=int((student or {}).get("current_chapter") or 1)
    question=await db.v39_adaptive_question(query.from_user.id,chapter)
    if not question:
        await query.edit_message_text(bold(f"🧠 المراجعة الذكية\n{DIV}\nلا توجد أسئلة مضافة للفصل {chapter} بعد. يمكن للإدارة إضافتها بأمر /add_question."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    text=f"🧠 سؤال مراجعة — الفصل {question['chapter']}\n{DIV}\n{question['question']}\n\nفكّر في الإجابة أولاً، ثم اكشف الحل."
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👁 إظهار الحل",callback_data=f"study_answer|{question['id']}")],[back_menu()]]))


async def v39_add_question_command(update,context):
    if not is_admin(update.effective_user.id): return
    parts=[p.strip() for p in (update.message.text or "").partition(" ")[2].split("|")]
    if len(parts)<3 or not parts[0].isdigit() or int(parts[0]) not in range(1,6):
        await update.effective_message.reply_text(bold("الاستخدام:\n/add_question الفصل | السؤال | الإجابة | الصعوبة"),parse_mode=ParseMode.HTML); return
    difficulty=parts[3].lower() if len(parts)>3 else "medium"
    if difficulty not in {"easy","medium","hard"}: difficulty="medium"
    row=await db.v37_add_question(int(parts[0]),parts[1][:2000],parts[2][:2000],difficulty,"manual")
    await update.effective_message.reply_text(bold(f"✅ أضيف السؤال رقم {row['id']} إلى الفصل {row['chapter']}."),parse_mode=ParseMode.HTML)

# Keep the existing handler chain intact, but give the v37 callbacks authoritative routing.
_v37_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query
    data=query.data or ""
    uid=query.from_user.id

    if data=="daily_learning_session":
        await query.answer(); await v39_daily_session(query); return
    if data=="mastery_map":
        await query.answer(); await v39_mastery_menu(query); return
    if data in ("schedules_menu","chapter_completion_schedule","v37_completion"):
        await query.answer(); await v39_calendar_menu(query,"week"); return
    if data.startswith("student_calendar|"):
        await query.answer(); await v39_calendar_menu(query,data.split("|",1)[1]); return
    if data=="study_quiz":
        await query.answer(); await v39_quiz_question(query); return
    if data.startswith("study_answer|"):
        question_id=int(data.split("|")[1])
        target=await db.v39_question(question_id)
        if not target:
            await query.answer("السؤال غير موجود.",show_alert=True); return
        await query.answer()
        await query.edit_message_text(bold(f"🧠 {target['question']}\n{DIV}\n✅ الحل:\n{target['answer']}\n\nهل كانت إجابتك صحيحة؟"),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ عرفت الإجابة",callback_data=f"study_result|{question_id}|1"),InlineKeyboardButton("🔁 أحتاج مراجعة",callback_data=f"study_result|{question_id}|0")],[back_menu()]])); return
    if data.startswith("study_result|"):
        _,question_id,correct=data.split("|"); await db.v37_record_question_attempt(uid,int(question_id),correct=="1")
        await query.answer("تم تحديث خطة مراجعتك.",show_alert=True); await v39_quiz_question(query); return
    if data.startswith("extend|"):
        task_id=int(data.split("|")[1]); task=await get_task(task_id)
        if not task or task["kind"]!="exam": await query.answer("التمديد متاح للامتحانات فقط.",show_alert=True); return
        await query.answer()
        kb=[[InlineKeyboardButton("🎁 تمديد مجاني أسبوعي — 24 ساعة",callback_data=f"freeextend|{task_id}")],
            [InlineKeyboardButton("👨‍👩‍👦 طلب تمديد بموافقة ولي الأمر",callback_data=f"extendhours|{task_id}|24")],
            [InlineKeyboardButton("◀️ رجوع للامتحان",callback_data=f"task|{task_id}"),back_menu()]]
        await query.edit_message_text(bold("⏳ خيارات تمديد الامتحان\n\nلديك تمديد مجاني واحد كل أسبوع. ويمكنك أيضاً استعمال نظام موافقة ولي الأمر."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("freeextend|"):
        task_id=int(data.split("|")[1]); result=await db.v39_free_exam_extension(task_id,uid,24)
        messages={"used":"استخدمت التمديد المجاني لهذا الأسبوع.","submitted":"سلّمت هذا الامتحان مسبقاً.","not_found":"الامتحان غير متاح."}
        if result.get("status")!="ok": await query.answer(messages.get(result.get("status"),"تعذر التمديد."),show_alert=True); return
        until=result["extended_until"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
        await query.answer("تم التمديد.",show_alert=True); await query.edit_message_text(bold(f"🎁 تم تمديد الامتحان 24 ساعة.\n⏰ الموعد الجديد: {until}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 فتح الامتحان",callback_data=f"task|{task_id}")],[back_menu()]])); return

    if data in ("v37_personal_schedule","personal_schedule"):
        await query.answer(); await v37_schedule_menu(query); return
    if data.startswith("v37_sched_count|"):
        await query.answer(); await v37_schedule_count(query,context,int(data.split("|")[1])); return
    if data.startswith("v37_sched_day|"):
        await query.answer(); await v37_schedule_day(query,context,int(data.split("|")[1])); return
    if data=="v37_sched_save":
        await query.answer(); await v37_schedule_save(query,context); return
    if data in ("v37_completion","chapter_completion_schedule","schedules_menu"):
        await query.answer(); await v37_completion_menu(query); return
    if data.startswith("v37_chapter_finish|"):
        await query.answer(); await v37_chapter_finish(query,int(data.split("|")[1])); return
    if data=="academic_dashboard":
        d=await db.v37_student_dashboard(uid)
        s=d.get("risk") or {}
        risk=s.get("level","-")
        avg=s.get("average")
        avg_text=f"{avg:.1f}/100" if avg is not None else "-"
        await query.answer()
        await query.edit_message_text(bold(
            f"📊 لوحتي الأكاديمية والذكية\n{DIV}\n"
            f"🎬 المحاضرات المكتملة: {d['lectures']}\n"
            f"📝 الواجبات/الامتحانات المسجلة: {d['submissions']}\n"
            f"⭐ XP المكتسب: {d['xp_earned']}\n"
            f"📈 متوسط آخر 30 يومًا: {avg_text}\n"
            f"⚠️ مستوى المتابعة الذكية: {risk}\n"
            f"⏰ المهام المتأخرة: {s.get('overdue',0)}\n"
            f"🚨 الإنذارات: {s.get('warnings',0)}"
        ),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 خطة إكمال الفصول",callback_data="v37_completion")],
            [InlineKeyboardButton("🔔 الإشعارات",callback_data="notifications")],
            [back_menu()]
        ])); return

    if data=="notifications":
        await query.answer(); await v37_notifications_menu(query); return
    if data=="notifications_read_all":
        await db.v28_mark_notifications_read(uid)
        await query.answer("تم تعليم الإشعارات كمقروءة.")
        await v37_notifications_menu(query); return
    if data=="track_change_requests":
        await query.answer(); await v37_track_change_admin_menu(query); return
    if data.startswith("trackreq|"):
        await query.answer(); await v37_track_request_review(query,int(data.split("|")[1])); return
    if data.startswith("trackdecision|"):
        if not is_admin(uid):
            await query.answer("للإدارة فقط.",show_alert=True); return
        _,req_s,approve_s=data.split("|")
        result=await db.v37_admin_decide_track_change(int(req_s),approve_s=="1",uid)
        if result.get("status") not in ("approved","denied"):
            await query.answer("تعذر معالجة الطلب.",show_alert=True); return
        student_id=int(result["request"]["user_id"])
        title="تمت الموافقة على تغيير مسار الدراسة" if result["status"]=="approved" else "تم رفض طلب تغيير مسار الدراسة"
        body=("وافقت الإدارة على تغيير مسارك وإعادة بناء جدولك الدراسي." if result["status"]=="approved"
              else "رفضت الإدارة طلب تغيير مسار الدراسة. يمكنك التواصل مع الإدارة لمعرفة السبب.")
        await v37_notify_student(context,student_id,"track_change_decision",title,body,"high",f"track_decision:{req_s}:{result['status']}")
        await query.answer("تم حفظ القرار.",show_alert=True)
        await v37_track_change_admin_menu(query); return

    if data.startswith("sched_count|"):
        await query.answer(); await v37_schedule_count(query,context,int(data.split("|")[1])); return
    if data.startswith("sched_day|"):
        await query.answer(); await v37_schedule_day(query,context,int(data.split("|")[1])); return
    if data=="sched_save":
        await query.answer(); await v37_schedule_save(query,context); return
    if data=="sched_regular":
        await query.answer()
        result=await db.v37_reset_schedule_to_regular(uid)
        if result.get("status")!="ok":
            await query.answer("تعذر إعادة الجدول.",show_alert=True); return
        await query.edit_message_text(bold("✅ تمت إعادة الجدول إلى النظام المنتظم، وأُعيد توزيع التحاضير المستقبلية وفق مسار الدراسة."),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗓️ تعديل الأيام",callback_data="v37_personal_schedule")],[back_menu()]])); return

    # Authoritative preparation lock: no student can jump to a future preparation by stale buttons.
    if data.startswith(("prepopen|","prepwatch|","prepprivate|","prepcomplete|","prepverify|","backlogoath|")) and not is_admin(uid):
        blocking_exam=await student_exam_lock(uid)
        if blocking_exam:
            await query.answer("🔒 يجب تسليم الامتحان المستحق قبل الانتقال إلى التحضير التالي.",show_alert=True)
            return
        student_check=await get_student(uid)
        personal_track=bool(student_check and (student_check.get("study_track")=="chapter" or student_check.get("schedule_mode")=="custom"))
        if not personal_track:
            return await _v37_previous_button_handler(update,context)
        parts=data.split("|")
        try:
            ch,lec=int(parts[-2]),int(parts[-1])
        except Exception:
            return await _v37_previous_button_handler(update,context)
        access=await db.v37_preparation_access(uid,ch,lec)
        if not access.get("allowed"):
            row=access.get("row")
            if row:
                await query.answer(f"🔒 التحضير الحالي هو رقم {row.get('prep_no') or '-'} في الفصل {row.get('chapter')}. أكمله أولاً.",show_alert=True)
            else:
                await query.answer("🎉 أكملت جميع التحاضير المتاحة لمسارك.",show_alert=True)
            return
        return await _v37_previous_button_handler(update,context)

    if data in ("account_settings",):
        # Let the legacy UI render; the new unified controls are added by main_menu below.
        return await _v37_previous_button_handler(update,context)
    if data=="change_study_track":
        await query.answer(); await show_onboarding_track(query,True); return
    if data.startswith("onboardtrack|"):
        choice=data.split("|",1)[1]
        current=await get_student(uid)
        if current and int(current.get("onboarding_version") or 0)<19:
            start_date=datetime.now(TIMEZONE).date()
            if choice=="course":
                await set_student_onboarding(uid,"course",3,start_date,[])
            else:
                chapter=int(choice)
                if chapter not in range(1,6):
                    await query.answer("الفصل غير صحيح.",show_alert=True); return
                await set_student_onboarding(uid,"chapter",chapter,start_date,build_personal_plan(chapter,start_date))
            await query.answer("تم حفظ مسارك الدراسي.",show_alert=True)
            updated=await get_student(uid)
            await query.edit_message_text(bold("✅ اكتمل إعداد حسابك ومسارك الدراسي. يمكنك الآن بدء جلسة اليوم."),parse_mode=ParseMode.HTML,
                reply_markup=main_menu(bool(updated and is_admin(uid)))); return
        await v37_apply_track_choice(query,context,choice); return
    if data.startswith("profilechapter|"):
        await v37_apply_track_choice(query,context,data.split("|",1)[1]); return
    return await _v37_previous_button_handler(update,context)

# Unified student dashboard and notifications in the final menu.
def main_menu(admin=False):
    rows=[]
    if not admin:
        rows=[
            [InlineKeyboardButton("🎯 ابدأ جلسة اليوم",callback_data="daily_learning_session")],
            [InlineKeyboardButton("🧪 تحضير اليوم",callback_data="today_prep"),InlineKeyboardButton("🗓 جدولي",callback_data="schedules_menu")],
            [InlineKeyboardButton("📚 الواجبات",callback_data="tasks|homework"),InlineKeyboardButton("📝 الامتحانات",callback_data="exams_menu")],
            [InlineKeyboardButton("🎬 المحاضرات",callback_data="playlists"),InlineKeyboardButton("📚 التراكمي",callback_data="backlog_auto")],
            [InlineKeyboardButton("📖 الملازم والملخصات",callback_data="study_resources"),InlineKeyboardButton("✅ الاجوبة النموذجية",callback_data="resourcecategory|model_answer")],
            [InlineKeyboardButton("👑 المراجعة الملكية",callback_data="royal_review_menu"),InlineKeyboardButton("🎯 نقاط ضعفي",callback_data="weaknesses_menu")],
            [InlineKeyboardButton("📊 تقدمي",callback_data="academic_dashboard"),InlineKeyboardButton("🧭 خريطة الاتقان",callback_data="mastery_map")],
            [InlineKeyboardButton("🏅 انجازاتي",callback_data="achievement_menu"),InlineKeyboardButton("⭐ متجر XP",callback_data="xp_store")],
            [InlineKeyboardButton("🔔 الاشعارات",callback_data="notifications"),InlineKeyboardButton("⚙️ اعدادات الحساب",callback_data="account_settings")],
        ]
    else:
        rows=[
            [InlineKeyboardButton("👥 ادارة الطلبة",callback_data="admin_students"),InlineKeyboardButton("👪 اولياء الامور",callback_data="admin_parents")],
            [InlineKeyboardButton("➕ نشر واجب او امتحان",callback_data="admin_publish")],
            [InlineKeyboardButton("🗓 جدول التحاضير",callback_data="prep_schedule"),InlineKeyboardButton("⏳ تمديد الامتحانات",callback_data="admin_exam_extensions")],
            [InlineKeyboardButton("🗑 ادارة وحذف الامتحانات",callback_data="admin_exam_delete_menu")],
            [InlineKeyboardButton("🔄 طلبات تغيير المسار",callback_data="track_change_requests")],
            [InlineKeyboardButton("🧪 تحاضير اليوم",callback_data="today_prep"),InlineKeyboardButton("📝 عرض الامتحانات",callback_data="tasks|exam|all")],
            [InlineKeyboardButton("🔔 الاشعارات",callback_data="notifications"),InlineKeyboardButton("⚙️ اعدادات الحساب",callback_data="account_settings")],
        ]
    return InlineKeyboardMarkup(rows)

async def post_init(app):
    """Single authoritative startup path for v39."""
    init_db()
    await v31_init_exam_controls()
    await backfill_personal_prep_numbers(CHAPTER_PREPARATION_DISTRIBUTION)
    await seed_preparations(preparation_rows())
    await observe_known_unactivated_members(ACTIVATION_GRACE_HOURS)
    if not await setting_value("v19_onboarding_broadcast_sent"):
        for student in await students_requiring_onboarding(19):
            try: await app.bot.send_message(student["user_id"],bold("🆕 تم تحديث البوت. افتح /start واختر مسارك الدراسي لإكمال إعداد الحساب."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        await set_setting_value("v19_onboarding_broadcast_sent","sent")
    await app.bot.set_my_commands([
        BotCommand("start","بدء البوت"),BotCommand("menu","القائمة الرئيسية"),BotCommand("parent","ربط ولي الأمر"),
        BotCommand("add_question","إضافة سؤال مراجعة - إدارة"),BotCommand("extend_exam","تمديد امتحان - إدارة"),
        BotCommand("exam_notice","تبليغ امتحان - إدارة"),BotCommand("add_previous_exam","إضافة امتحان سابق - إدارة"),
        BotCommand("reopen_exam","إعادة فتح امتحان - إدارة"),BotCommand("warn","إضافة إنذار - إدارة"),
        BotCommand("unwarn","حذف إنذار - إدارة"),BotCommand("warnings","عرض الإنذارات - إدارة"),
        BotCommand("xp","تعديل XP - إدارة"),BotCommand("grade","درجة امتحان - إدارة"),BotCommand("id","عرض المعرفات")])
    if OWNER_CHAT_ID:
        try: await app.bot.send_message(OWNER_CHAT_ID,bold(f"✅ اشتغل بوت الأحياء — {BUILD_VERSION}\n✅ قاعدة Neon جاهزة\n✅ جميع مهام المتابعة مفعلة"),parse_mode=ParseMode.HTML)
        except TelegramError: pass
    app.job_queue.run_repeating(publish_preparations_job,60,first=5,name="preparations")
    app.job_queue.run_repeating(personal_preparations_job,60,first=8,name="personal_preparations")
    app.job_queue.run_repeating(scheduled_tasks_job,30,first=10,name="scheduled_tasks")
    app.job_queue.run_repeating(linked_exam_dispatch_job,45,first=15,name="linked_exam_dispatch")
    app.job_queue.run_repeating(exam_parent_readiness_job,60,first=12,name="exam_parent_readiness")
    app.job_queue.run_repeating(activation_compliance_job,300,first=45,name="activation_compliance")
    app.job_queue.run_repeating(v31_close_tasks_job,60,first=25,name="task_deadlines")
    app.job_queue.run_repeating(exam_reminders_job,60,first=30,name="exam_reminders")
    app.job_queue.run_repeating(teacher_exam_deadline_job,60,first=35,name="teacher_exam_deadline")
    app.job_queue.run_repeating(study_and_progress_job,300,first=40,name="study_progress")
    app.job_queue.run_repeating(v28_notification_job,60,first=18,name="exam_notices")
    app.job_queue.run_repeating(v28_gamification_job,3600,first=60,name="gamification")
    app.job_queue.run_repeating(weekly_reports_job,60,first=50,name="weekly_reports")
    app.job_queue.run_repeating(v37_notification_delivery_job,60,first=20,name="v37_notification_delivery")


# ========================= v41 ROYAL STUDY EXPERIENCE =========================

REVIEW_STAGE_LABELS={1:"المراجعة الاولى - بعد 6 ساعات",2:"المراجعة الثانية - بعد 24 ساعة",3:"المراجعة الثالثة - بعد اسبوع",4:"المراجعة الرابعة - بعد شهر"}


def _v41_normalize_oath(value):
    value=re.sub(r"[\s\u0640]+"," ",str(value or "")).strip()
    return value.rstrip(".،!؟ ")


def _v41_lecture_title(chapter,lecture):
    for number,title,_url in PLAYLISTS.get(int(chapter),[]):
        if int(number)==int(lecture): return title
    return f"المحاضرة {lecture}"


async def v41_review_menu(query):
    dashboard=await db.v41_review_dashboard(query.from_user.id)
    now=datetime.now(TIMEZONE); pending=dashboard["pending"]
    due=[r for r in pending if r["due_at"]<=now]
    upcoming=[r for r in pending if r["due_at"]>now]
    lines=["👑 المراجعة الذكية - طريقة الحفظ الملكية",DIV,
           "كل محاضرة تمر باربع مراجعات محسوبة من وقت اكمالها:",
           "1️⃣ بعد 6 ساعات  |  2️⃣ بعد 24 ساعة",
           "3️⃣ بعد اسبوع   |  4️⃣ بعد شهر","",
           f"🔥 مستحق الان: {len(due)}",f"⏳ قادم: {len(upcoming)}",f"✅ مراجعات مكتملة: {dashboard['completed']}"]
    kb=[]
    for row in due[:20]:
        kb.append([InlineKeyboardButton(f"📌 ف{row['chapter']} م{row['lecture']} | المراجعة {row['stage']}",callback_data=f"royal_review|{row['id']}")])
    if not pending:
        lines += ["","اكمل محاضرة من التحاضير حتى يبني البوت مواعيد مراجعتها تلقائيا."]
    elif not due:
        first=upcoming[0]
        lines += ["",f"اقرب مراجعة: ف{first['chapter']} م{first['lecture']} في {first['due_at'].astimezone(TIMEZONE):%d/%m %H:%M}"]
    kb += [[InlineKeyboardButton("🧪 اختبار ذاتي ذكي",callback_data="adaptive_quiz")],[back_menu()]]
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v41_review_open(query,context,review_id):
    row=await db.v41_review_item(query.from_user.id,review_id)
    if not row:
        await query.answer("المراجعة غير موجودة.",show_alert=True); return
    if row.get("completed_at"):
        await query.answer("هذه المراجعة مكتملة مسبقا.",show_alert=True); return
    if not row.get("due"):
        await query.answer("لم يحن موعد هذه المراجعة بعد.",show_alert=True); return
    context.user_data["v41_review_oath_id"]=int(review_id)
    await query.answer()
    title=_v41_lecture_title(row["chapter"],row["lecture"])
    text=(f"<b>👑 {escape(REVIEW_STAGE_LABELS[int(row['stage'])])}\n{DIV}\n"
          f"📘 الفصل {row['chapter']} | المحاضرة {row['lecture']}\n{escape(title)}\n\n"
          "راجع المحاضرة فعليا، وبعد الانتهاء انسخ وارسل القسم التالي برسالة:</b>\n\n"
          f"<code>{escape(ROYAL_REVIEW_OATH)}</code>")
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ المراجعات",callback_data="royal_review_menu"),back_menu()]]))


async def v41_review_reminders_job(context):
    for row in await db.v41_due_review_reminders(100):
        try:
            await context.bot.send_message(row["user_id"],bold(
                f"👑 حان موعد {REVIEW_STAGE_LABELS[int(row['stage'])]}\n"
                f"📘 الفصل {row['chapter']} | المحاضرة {row['lecture']}\n\n"
                "افتح المراجعة الذكية وثبت المحاضرة بطريقة الحفظ الملكية."),parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 ابدا المراجعة",callback_data=f"royal_review|{row['id']}")]]))
            await db.v41_mark_review_reminded(row["id"])
        except TelegramError:
            logger.warning("royal review reminder retry pending for %s",row["user_id"])


async def v41_weakness_menu(query):
    counts=await db.v41_weakness_counts(query.from_user.id); total=sum(counts.values())
    kb=[[InlineKeyboardButton(f"📘 الفصل {chapter} | {counts.get(chapter,0)} نقطة",callback_data=f"weak_chapter|{chapter}")] for chapter in range(1,6)]
    kb.append([back_menu()])
    await query.edit_message_text(bold(f"🎯 نقاط ضعفي\n{DIV}\nسجل اي نقطة غير واضحة داخل محاضرتها، وبعد تمكنك منها اقسم القسم المخصص فتحذف من القائمة وتحصل على 5 XP.\n\n📌 النقاط المفتوحة حاليا: {total}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v41_weakness_chapter(query,chapter):
    rows=await db.v41_weaknesses(query.from_user.id,chapter,None); counts=defaultdict(int)
    for row in rows: counts[int(row["lecture"])]+=1
    kb=[]; lectures=PLAYLISTS.get(chapter,[])
    for index in range(0,len(lectures),2):
        kb.append([InlineKeyboardButton(f"م{number} ({counts[number]})",callback_data=f"weak_lecture|{chapter}|{number}") for number,_title,_url in lectures[index:index+2]])
    kb += [[InlineKeyboardButton("◀️ الفصول",callback_data="weaknesses_menu"),back_menu()]]
    await query.edit_message_text(bold(f"🎯 نقاط ضعف الفصل {chapter}\n{DIV}\nاختر المحاضرة حتى تضيف نقطة ضعف او تعالج النقاط المسجلة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v41_weakness_lecture(query,chapter,lecture):
    rows=await db.v41_weaknesses(query.from_user.id,chapter,lecture); title=_v41_lecture_title(chapter,lecture); kb=[]
    for index,row in enumerate(rows[:50],1):
        label=" ".join(row["weakness_text"].split())
        kb.append([InlineKeyboardButton(f"🎯 {index}. {label[:35]}",callback_data=f"weak_open|{row['id']}")])
    kb += [[InlineKeyboardButton("➕ اضافة نقطة ضعف",callback_data=f"weak_add|{chapter}|{lecture}")],
           [InlineKeyboardButton(f"◀️ الفصل {chapter}",callback_data=f"weak_chapter|{chapter}"),back_menu()]]
    status=f"عدد نقاط الضعف المفتوحة: {len(rows)}" if rows else "لا توجد نقاط ضعف مسجلة لهذه المحاضرة."
    await query.edit_message_text(bold(f"📘 الفصل {chapter} | المحاضرة {lecture}\n{title}\n{DIV}\n{status}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v41_weakness_open(query,context,weakness_id):
    row=await db.v41_weakness_item(query.from_user.id,weakness_id)
    if not row or row.get("resolved_at"):
        await query.answer("نقطة الضعف غير موجودة او تم حلها.",show_alert=True); return
    context.user_data["v41_weakness_oath_id"]=int(weakness_id)
    await query.answer()
    text=(f"<b>🎯 نقطة ضعفي\n{DIV}\n📘 الفصل {row['chapter']} | المحاضرة {row['lecture']}\n\n"
          f"{escape(row['weakness_text'])}\n\nبعد ما تتمكن من حلها، انسخ وارسل القسم التالي برسالة لتحصل على 5 XP:</b>\n\n"
          f"<code>{escape(WEAKNESS_RESOLUTION_OATH)}</code>")
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ المحاضرة",callback_data=f"weak_lecture|{row['chapter']}|{row['lecture']}"),back_menu()]]))


async def v41_schedule_hub(query):
    student=await get_student(query.from_user.id)
    if not student:
        await query.edit_message_text(bold("سجل حساب الطالب اولا."),parse_mode=ParseMode.HTML); return
    kb=[[InlineKeyboardButton("🗓 هذا الاسبوع",callback_data="student_calendar|week"),InlineKeyboardButton("📆 هذا الشهر",callback_data="student_calendar|month")],
        [InlineKeyboardButton("🏁 متى ننهي المنهج؟",callback_data="chapter_completion_schedule")]]
    if student.get("study_track")=="chapter":
        kb.append([InlineKeyboardButton("⚙️ تغيير ايام الدراسة",callback_data="personal_schedule")])
        note="يمكنك تغيير الايام فقط، اما عدد ايام الدراسة فهو ثابت حسب الفصل."
    else:
        note="طلاب الدورة الحالية يتبعون جدول الدورة الرسمي ولا يمكنهم تغييره."
    kb.append([back_menu()])
    await query.edit_message_text(bold(f"🗓 جدولي الدراسي\n{DIV}\n{note}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v41_calendar_menu(query,period="week"):
    today=datetime.now(TIMEZONE).date()
    if period=="month":
        start=today.replace(day=1)
        next_month=start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)
        end=next_month-timedelta(days=1); title="📆 جدولي الدراسي لهذا الشهر"
    else:
        start=today-timedelta(days=today.weekday()); end=start+timedelta(days=6); title="🗓 جدولي الدراسي لهذا الاسبوع"
    calendar=await db.v41_student_calendar(query.from_user.id,start,end)
    student=calendar.get("student") or {}; rows=calendar.get("preparations") or []
    track="الدورة الحالية" if student.get("study_track")=="course" else f"الفصل {student.get('current_chapter') or '-'}"
    names=["الاثنين","الثلاثاء","الاربعاء","الخميس","الجمعة","السبت","الاحد"]
    lines=[title,DIV,f"📚 المسار: {track}",f"📅 الفترة: {start:%d/%m/%Y} - {end:%d/%m/%Y}",""]
    for row in rows:
        lectures=" + ".join(f"م{x.strip()}" for x in str(row.get("lectures") or "").split(",") if x.strip())
        today_mark=" ← اليوم" if row["target_date"]==today else ""
        lines.append(f"• {names[row['target_date'].weekday()]} {row['target_date']:%d/%m}{today_mark}\n  الفصل {row['chapter']} | تحضير {row.get('prep_no') or '-'} | {lectures}")
    if not rows: lines.append("لا توجد تحاضير ضمن هذه الفترة في الجدول الفعلي.")
    kb=[[InlineKeyboardButton("🗓 الاسبوع",callback_data="student_calendar|week"),InlineKeyboardButton("📆 الشهر",callback_data="student_calendar|month")],
        [InlineKeyboardButton("🏁 متى ننهي المنهج؟",callback_data="chapter_completion_schedule")],
        [InlineKeyboardButton("◀️ الجداول",callback_data="schedules_menu"),back_menu()]]
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v41_finish_menu(query):
    student=await get_student(query.from_user.id); plan=await db.v37_chapter_completion_plan(query.from_user.id)
    track="الدورة الحالية" if student and student.get("study_track")=="course" else f"الفصل {student.get('current_chapter') or '-'}" if student else "-"
    lines=["🏁 متى ننهي المنهج؟",DIV,f"📚 المسار: {track}",""]
    for row in plan["chapters"]:
        finish=row["finish_date"].strftime("%d/%m/%Y") if row.get("finish_date") else "-"
        lines.append(f"📘 الفصل {row['chapter']}: {finish} | {row['prep_count']} تحضير")
    full=plan["full_finish"].strftime("%d/%m/%Y") if plan.get("full_finish") else "غير متاح حاليا"
    lines += ["",DIV,f"🏆 موعد انهاء المنهج بالكامل: {full}","يتحدث الموعد تلقائيا من الجدول الحقيقي، وليس من نص ثابت."]
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحديث",callback_data="chapter_completion_schedule")],[InlineKeyboardButton("◀️ الجداول",callback_data="schedules_menu"),back_menu()]]))


async def v41_study_days_menu(query,context):
    student=await get_student(query.from_user.id)
    if not student or student.get("study_track")!="chapter":
        await query.edit_message_text(bold("🔒 طلاب الدورة الحالية يتبعون جدول الدورة الرسمي، لذلك لا يمكن تغيير ايامه من حساب الطالب."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗓 عرض الجدول",callback_data="schedules_menu"),back_menu()]])); return
    chapter=int(student.get("current_chapter") or 1); required=weekly_study_day_count(chapter)
    sched=await student_schedule(query.from_user.id); current=set((sched or {}).get("study_days") or [])
    if len(current)!=required:
        current=set({6,0,1,2,3} if chapter==1 else ({6,0,1,3} if chapter==2 else {6,1,3}))
    context.user_data["v41_study_days"]=sorted(current); context.user_data["v41_study_required"]=required
    names=["الاثنين","الثلاثاء","الاربعاء","الخميس","الجمعة","السبت","الاحد"]
    kb=[[InlineKeyboardButton(("☑️ " if day in current else "☐ ")+names[day],callback_data=f"v41_study_day|{day}")] for day in range(7)]
    kb += [[InlineKeyboardButton("💾 حفظ الايام",callback_data="v41_study_save")],[InlineKeyboardButton("◀️ الجداول",callback_data="schedules_menu"),back_menu()]]
    await query.edit_message_text(bold(f"⚙️ ايام الدراسة\n{DIV}\nالفصل {chapter} يحتاج {required} ايام دراسة اسبوعيا.\n\nيمكنك استبدال الايام فقط، ولا يمكنك زيادة العدد او تقليله.\nالمحدد: {len(current)}/{required}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v41_toggle_study_day(query,context,day):
    days=set(context.user_data.get("v41_study_days",[])); required=int(context.user_data.get("v41_study_required",0))
    if not required:
        await query.answer("افتح تعديل ايام الدراسة من جديد.",show_alert=True); return
    if day in days: days.remove(day)
    elif len(days)<required: days.add(day)
    else:
        await query.answer(f"احذف يوما اولا ثم اختر البديل. العدد ثابت: {required}.",show_alert=True); return
    context.user_data["v41_study_days"]=sorted(days)
    names=["الاثنين","الثلاثاء","الاربعاء","الخميس","الجمعة","السبت","الاحد"]
    kb=[[InlineKeyboardButton(("☑️ " if j in days else "☐ ")+names[j],callback_data=f"v41_study_day|{j}")] for j in range(7)]
    kb += [[InlineKeyboardButton("💾 حفظ الايام",callback_data="v41_study_save")],[InlineKeyboardButton("◀️ الجداول",callback_data="schedules_menu"),back_menu()]]
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))


async def v41_save_study_days(query,context):
    days=context.user_data.get("v41_study_days",[]); required=int(context.user_data.get("v41_study_required",0))
    if not required or len(days)!=required:
        await query.answer(f"يجب تحديد {required or '-'} ايام بالضبط.",show_alert=True); return
    result=await db.v41_set_study_days(query.from_user.id,days)
    if result.get("status")=="course":
        await query.answer("جدول طلاب الدورة ثابت.",show_alert=True); return
    if result.get("status")!="ok":
        await query.answer("تعذر حفظ الايام.",show_alert=True); return
    context.user_data.pop("v41_study_days",None); context.user_data.pop("v41_study_required",None)
    names=["الاثنين","الثلاثاء","الاربعاء","الخميس","الجمعة","السبت","الاحد"]
    await query.answer("تم حفظ الايام واعادة ترتيب التحاضير.",show_alert=True)
    await query.edit_message_text(bold(f"✅ تم تحديث ايام دراستك\n\n📅 {', '.join(names[d] for d in days)}\n📚 بقي العدد ثابتا: {required} ايام\n🔄 اعيد ترتيب {result['pending']} تحضير غير مكتمل فقط."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗓 عرض الجدول",callback_data="schedules_menu"),back_menu()]]))


_v41_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ""; uid=query.from_user.id
    if data=="menu":
        for key in ("v41_review_oath_id","v41_weakness_oath_id","v41_weakness_add","v41_study_days","v41_study_required"):
            context.user_data.pop(key,None)
    protected=(data in {"royal_review_menu","weaknesses_menu","mistake_notebook","schedules_menu","chapter_completion_schedule","v37_completion","personal_schedule","v37_personal_schedule","adaptive_quiz"}
               or data.startswith(("royal_review|","weak_chapter|","weak_lecture|","weak_add|","weak_open|","student_calendar|","v41_study_day|"))
               or data=="v41_study_save")
    if protected and not is_admin(uid):
        student=await get_student(uid)
        if not student or not student.get("approved"):
            await query.answer("هذه الخدمة للطلاب المفعلين فقط.",show_alert=True); return
    if data=="royal_review_menu":
        context.user_data.pop("v41_review_oath_id",None); await query.answer(); await v41_review_menu(query); return
    if data.startswith("royal_review|"):
        await v41_review_open(query,context,int(data.split("|")[1])); return
    if data=="adaptive_quiz":
        await query.answer(); await v39_quiz_question(query); return
    if data in ("weaknesses_menu","mistake_notebook"):
        context.user_data.pop("v41_weakness_add",None); context.user_data.pop("v41_weakness_oath_id",None)
        await query.answer(); await v41_weakness_menu(query); return
    if data.startswith("weak_chapter|"):
        context.user_data.pop("v41_weakness_add",None); context.user_data.pop("v41_weakness_oath_id",None)
        await query.answer(); await v41_weakness_chapter(query,int(data.split("|")[1])); return
    if data.startswith("weak_lecture|"):
        context.user_data.pop("v41_weakness_add",None); context.user_data.pop("v41_weakness_oath_id",None)
        _,chapter,lecture=data.split("|"); await query.answer(); await v41_weakness_lecture(query,int(chapter),int(lecture)); return
    if data.startswith("weak_add|"):
        _,chapter,lecture=data.split("|"); context.user_data["v41_weakness_add"]={"chapter":int(chapter),"lecture":int(lecture)}
        await query.answer(); await query.edit_message_text(bold(f"➕ اضافة نقطة ضعف\n{DIV}\n📘 الفصل {chapter} | المحاضرة {lecture}\n\nاكتب نقطة الضعف برسالة واحدة واضحة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("الغاء",callback_data=f"weak_lecture|{chapter}|{lecture}"),back_menu()]])); return
    if data.startswith("weak_open|"):
        context.user_data.pop("v41_weakness_add",None); await v41_weakness_open(query,context,int(data.split("|")[1])); return
    if data=="schedules_menu":
        await query.answer(); await v41_schedule_hub(query); return
    if data.startswith("student_calendar|"):
        await query.answer(); await v41_calendar_menu(query,data.split("|",1)[1]); return
    if data in ("chapter_completion_schedule","v37_completion"):
        await query.answer(); await v41_finish_menu(query); return
    if data in ("personal_schedule","v37_personal_schedule"):
        await query.answer(); await v41_study_days_menu(query,context); return
    if data.startswith("v41_study_day|"):
        await query.answer(); await v41_toggle_study_day(query,context,int(data.split("|")[1])); return
    if data=="v41_study_save":
        await v41_save_study_days(query,context); return
    if data.startswith(("v37_sched_count|","sched_count|","schednum|","v37_sched_day|","sched_day|","schedday|")) or data in ("v37_sched_save","sched_save","sched_regular"):
        await query.answer("تم تحديث نظام الجدول. اختر ايامك من الواجهة الجديدة.",show_alert=True); await v41_study_days_menu(query,context); return
    if data.startswith("onboardtrack|"):
        student=await get_student(uid); choice=data.split("|",1)[1]
        if student and int(student.get("onboarding_version") or 0)>=19:
            if choice=="course":
                warning="سيتم تحويلك الى جدول الدورة الرسمي، ولن تستطيع تعديل ايامه."
            else:
                warning=f"سيعاد الفصل {choice} من المحاضرة 1، ويحذف تقدم وامتحانات الفصل {choice} وما بعده حتى تؤديها من جديد. نقاط XP تبقى محفوظة."
            await query.answer(); await query.edit_message_text(bold(f"⚠️ تاكيد تغيير المسار\n{DIV}\n{warning}\n\nهل تريد المتابعة؟"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم، نفذ التغيير",callback_data=f"v41_track_confirm|{choice}")],[InlineKeyboardButton("❌ الغاء",callback_data="account_settings"),back_menu()]])); return
    if data.startswith("v41_track_confirm|"):
        await v37_apply_track_choice(query,context,data.split("|",1)[1]); return
    return await _v41_previous_button_handler(update,context)


_v41_previous_private_messages=private_messages
async def private_messages(update,context):
    text=(update.message.text or "").strip() if update.message else ""
    review_id=context.user_data.get("v41_review_oath_id")
    if review_id:
        if _v41_normalize_oath(text)!=_v41_normalize_oath(ROYAL_REVIEW_OATH):
            await update.effective_message.reply_text(f"<b>القسم غير مطابق. انسخ النص التالي كاملا:</b>\n\n<code>{escape(ROYAL_REVIEW_OATH)}</code>",parse_mode=ParseMode.HTML); return
        result=await db.v41_complete_review(update.effective_user.id,int(review_id))
        if result.get("status")=="ok":
            context.user_data.pop("v41_review_oath_id",None); row=result["review"]
            await update.effective_message.reply_text(bold(f"✅ تم تسجيل المراجعة {row['stage']} للمحاضرة {row['lecture']} من الفصل {row['chapter']}.\nاستمر على المواعيد الاربعة حتى تثبت المادة باقوى صورة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 المراجعة التالية",callback_data="royal_review_menu"),back_menu()]])); return
        await update.effective_message.reply_text(bold("تعذر تسجيل المراجعة او لم يحن موعدها بعد."),parse_mode=ParseMode.HTML); return
    weakness_id=context.user_data.get("v41_weakness_oath_id")
    if weakness_id:
        if _v41_normalize_oath(text)!=_v41_normalize_oath(WEAKNESS_RESOLUTION_OATH):
            await update.effective_message.reply_text(f"<b>القسم غير مطابق. انسخ النص التالي كاملا:</b>\n\n<code>{escape(WEAKNESS_RESOLUTION_OATH)}</code>",parse_mode=ParseMode.HTML); return
        result=await db.v41_resolve_weakness(update.effective_user.id,int(weakness_id))
        if result.get("status")=="ok":
            context.user_data.pop("v41_weakness_oath_id",None); row=result["weakness"]
            await update.effective_message.reply_text(bold(f"✅ احسنت، تم حل نقطة الضعف وحذفها من قائمتك.\n⭐ حصلت على {result['xp']} XP."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎯 نقاط ضعفي",callback_data="weaknesses_menu"),InlineKeyboardButton("◀️ المحاضرة",callback_data=f"weak_lecture|{row['chapter']}|{row['lecture']}")]])); return
        context.user_data.pop("v41_weakness_oath_id",None)
        await update.effective_message.reply_text(bold("نقطة الضعف غير موجودة او تم حلها مسبقا."),parse_mode=ParseMode.HTML); return
    weakness=context.user_data.get("v41_weakness_add")
    if weakness:
        if len(text)<3:
            await update.effective_message.reply_text(bold("اكتب نقطة ضعف واضحة من 3 احرف على الاقل."),parse_mode=ParseMode.HTML); return
        row=await db.v41_add_weakness(update.effective_user.id,weakness["chapter"],weakness["lecture"],text)
        if not row:
            await update.effective_message.reply_text(bold("تعذر حفظ نقطة الضعف."),parse_mode=ParseMode.HTML); return
        context.user_data.pop("v41_weakness_add",None)
        await update.effective_message.reply_text(bold(f"✅ تم حفظ نقطة الضعف داخل الفصل {row['chapter']} - المحاضرة {row['lecture']}."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎯 عرض نقاط المحاضرة",callback_data=f"weak_lecture|{row['chapter']}|{row['lecture']}")],[back_menu()]])); return
    return await _v41_previous_private_messages(update,context)


def main_menu(admin=False):
    if admin:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 ادارة الطلبة",callback_data="admin_students"),InlineKeyboardButton("👪 اولياء الامور",callback_data="admin_parents")],
            [InlineKeyboardButton("➕ نشر واجب او امتحان",callback_data="admin_publish")],
            [InlineKeyboardButton("🗓 جدول التحاضير",callback_data="prep_schedule"),InlineKeyboardButton("⏳ تمديد الامتحانات",callback_data="admin_exam_extensions")],
            [InlineKeyboardButton("🗑 ادارة وحذف الامتحانات",callback_data="admin_exam_delete_menu")],
            [InlineKeyboardButton("🔄 طلبات تغيير المسار",callback_data="track_change_requests")],
            [InlineKeyboardButton("🧪 تحاضير اليوم",callback_data="today_prep"),InlineKeyboardButton("📝 عرض الامتحانات",callback_data="tasks|exam|all")],
            [InlineKeyboardButton("🔔 الاشعارات",callback_data="notifications"),InlineKeyboardButton("⚙️ اعدادات الحساب",callback_data="account_settings")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 ابدا جلسة اليوم",callback_data="daily_learning_session")],
        [InlineKeyboardButton("🧪 تحضير اليوم",callback_data="today_prep"),InlineKeyboardButton("🗓 جدولي الدراسي",callback_data="schedules_menu")],
        [InlineKeyboardButton("🏁 متى ننهي المنهج؟",callback_data="chapter_completion_schedule")],
        [InlineKeyboardButton("📚 الواجبات",callback_data="tasks|homework"),InlineKeyboardButton("📝 الامتحانات",callback_data="exams_menu")],
        [InlineKeyboardButton("🎬 المحاضرات",callback_data="playlists"),InlineKeyboardButton("📚 التراكمي",callback_data="backlog_auto")],
        [InlineKeyboardButton("📖 الملازم والملخصات",callback_data="study_resources"),InlineKeyboardButton("✅ الاجوبة النموذجية",callback_data="resourcecategory|model_answer")],
        [InlineKeyboardButton("👑 المراجعة الذكية",callback_data="royal_review_menu"),InlineKeyboardButton("🎯 نقاط ضعفي",callback_data="weaknesses_menu")],
        [InlineKeyboardButton("📊 تقدمي",callback_data="academic_dashboard"),InlineKeyboardButton("🧭 خريطة الاتقان",callback_data="mastery_map")],
        [InlineKeyboardButton("🏅 انجازاتي",callback_data="achievement_menu"),InlineKeyboardButton("⭐ متجر XP",callback_data="xp_store")],
        [InlineKeyboardButton("🔔 الاشعارات",callback_data="notifications"),InlineKeyboardButton("⚙️ اعدادات الحساب",callback_data="account_settings")],
    ])


_v41_previous_post_init=post_init
async def post_init(app):
    await _v41_previous_post_init(app)
    app.job_queue.run_repeating(v41_review_reminders_job,60,first=25,name="v41_royal_review_reminders")


async def post_shutdown(app):
    """Release persistent database resources cleanly during a Render restart."""
    global _INSTANCE_LOCK_CONN
    db.close_pool()
    if _INSTANCE_LOCK_CONN is not None:
        try: _INSTANCE_LOCK_CONN.close()
        finally: _INSTANCE_LOCK_CONN=None

# ========================= v42 PREMIUM RELIABILITY =========================

def _v42_prep_lectures(row):
    return {int(x.strip()) for x in str(row.get("lectures") or "").split(",") if x.strip().isdigit()}


async def v42_review_menu(query):
    data=await db.v42_review_context(query.from_user.id); student=data.get("student") or {}; pending=data.get("pending") or []
    due=[row for row in pending if row.get("due")]
    lines=["👑 المراجعة الذكية",DIV,"طريقة الحفظ الملكية: 6 ساعات، 24 ساعة، اسبوع، ثم شهر.",
           f"🔥 مستحق الان: {len(due)}",f"✅ مكتمل: {data.get('completed',0)}",""]
    kb=[]
    if student.get("study_track")=="course":
        lines.append("📚 ترتيبك حسب تحاضير الدورة:")
        used=set(); groups=[]
        for prep in data.get("preparations") or []:
            lecture_ids=_v42_prep_lectures(prep)
            items=[r for r in pending if int(r["chapter"])==int(prep["chapter"]) and int(r["lecture"]) in lecture_ids]
            if not items: continue
            key=int(prep["prep_no"])
            if key in used: continue
            used.add(key); groups.append((prep,items))
        for prep,items in groups[:30]:
            due_count=sum(1 for item in items if item.get("due")); mark="🔥" if due_count else "⏳"
            lectures=" + ".join(f"م{x}" for x in sorted(_v42_prep_lectures(prep)))
            kb.append([InlineKeyboardButton(f"{mark} تحضير {prep.get('chapter_prep_no') or prep['prep_no']} | ف{prep['chapter']} | {lectures}",callback_data=f"v42_review_prep|{prep['prep_no']}")])
        if not groups: lines.append("اكمل اول تحضير حتى يبني البوت مراجعاته بالترتيب.")
    else:
        chapter=int(student.get("current_chapter") or 1); lines.append(f"📘 ترتيب الفصل {chapter} يبدا من اول محاضرة:")
        for row in due[:30]:
            kb.append([InlineKeyboardButton(f"🔥 م{row['lecture']} | المراجعة {row['stage']}",callback_data=f"royal_review|{row['id']}")])
        if pending and not due:
            first=pending[0]; lines.append(f"اقرب مراجعة: م{first['lecture']} في {first['due_at'].astimezone(TIMEZONE):%d/%m %H:%M}")
        elif not pending: lines.append("اكمل المحاضرة الاولى في فصلك حتى يبدا نظام المراجعة.")
    kb += [[InlineKeyboardButton("🧪 اختبار ذاتي ذكي",callback_data="adaptive_quiz")],[back_menu()]]
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v42_review_prep(query,prep_no):
    data=await db.v42_review_context(query.from_user.id); prep=next((p for p in data.get("preparations",[]) if int(p["prep_no"])==int(prep_no)),None)
    if not prep:
        await query.answer("التحضير غير موجود.",show_alert=True); return
    lectures=_v42_prep_lectures(prep); items=[r for r in data.get("pending",[]) if int(r["chapter"])==int(prep["chapter"]) and int(r["lecture"]) in lectures]
    kb=[]; lines=[f"👑 مراجعة تحضير {prep.get('chapter_prep_no') or prep['prep_no']}",DIV,f"📘 الفصل {prep['chapter']} | "+" + ".join(f"م{x}" for x in sorted(lectures)),""]
    for row in items:
        if row.get("due"): kb.append([InlineKeyboardButton(f"🔥 م{row['lecture']} | المراجعة {row['stage']}",callback_data=f"royal_review|{row['id']}")])
        else: lines.append(f"⏳ م{row['lecture']} | مراجعة {row['stage']} في {row['due_at'].astimezone(TIMEZONE):%d/%m %H:%M}")
    if not items: lines.append("✅ اكتملت المراجعات المتاحة لهذا التحضير.")
    kb.append([InlineKeyboardButton("◀️ كل التحاضير",callback_data="royal_review_menu"),back_menu()])
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v42_exam_menu(query):
    student=await get_student(query.from_user.id)
    if not student:
        await query.answer("سجل دخولك اولا.",show_alert=True); return
    if student.get("study_track")=="course":
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 امتحان اليوم",callback_data="course_current_exams")],
            [InlineKeyboardButton("📚 بنك امتحانات الفصول",callback_data="v42_exam_bank")],
            [InlineKeyboardButton("🏆 الامتحانات التراكمية",callback_data="tasks|exam|cumulative")],
            [InlineKeyboardButton("🗂 امتحاناتي السابقة",callback_data="course_past_exams")],[back_menu()]])
        text="📝 امتحانات الدورة الحالية\n"+DIV+"\nامتحان اليوم يظهر تلقائيا بعد 12 ساعة من وقت نشر التحضير المرتبط به.\n\nبنك الفصول منفصل ولا يوقف تحاضير الدورة."
    else:
        kb=InlineKeyboardMarkup([[InlineKeyboardButton(f"📘 الفصل {n}",callback_data=f"chapter_exam|{n}")] for n in range(1,6)]+[[back_menu()]])
        text="📝 امتحانات الفصول\n"+DIV+"\nاختر الفصل. امتحان فصلك الحالي يفتح بعد اكمال محتواه والموافقة عليه."
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v42_exam_bank(query):
    kb=InlineKeyboardMarkup([[InlineKeyboardButton(f"📘 الفصل {n}",callback_data=f"v42_bank_chapter|{n}")] for n in range(1,6)]+[[InlineKeyboardButton("◀️ الامتحانات",callback_data="exams_menu"),back_menu()]])
    await query.edit_message_text(bold("📚 بنك امتحانات الفصول\n"+DIV+"\nامتحانات اضافية مرتبة حسب الفصول. لا تتداخل مع امتحان اليوم ولا تحجز التحضير التالي."),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v42_bank_chapter(query,chapter):
    rows=await db.v42_exam_bank_catalog(query.from_user.id,chapter); kb=[]
    for row in rows:
        task=row.get("task") or {}; submitted=bool(task.get("student_submitted_at")); ready=bool(row.get("ready"))
        if submitted: prefix="✅"; suffix="تم التسليم"; callback="v42_bank_done"
        elif task and not task.get("closed"): prefix="🟢"; suffix="مفتوح"; callback=f"examopen|{task['id']}"
        elif ready: prefix="🟢"; suffix="جاهز"; callback=f"v42_bank_open|{row['id']}"
        else: prefix="🔒"; suffix=f"اكمل المحتوى {row.get('completed_lectures',0)}/{row.get('required_lectures',0)}"; callback="v42_bank_locked"
        title=row["title"].replace("[تراكمي] ","")
        kb.append([InlineKeyboardButton(f"{prefix} {title} | {suffix}",callback_data=callback)])
    archives=await db.archive_exams(chapter)
    for archive in archives:
        kb.append([InlineKeyboardButton('📂 '+archive['title'],callback_data=f"examarchive|{archive['id']}")])
    if not rows and not archives: message="لا توجد امتحانات منشورة لهذا الفصل حاليا."
    else: message="الاخضر جاهز، والمغلق يفتح بعد اكمال محاضراته."
    kb += [[InlineKeyboardButton("◀️ الفصول",callback_data="v42_exam_bank"),back_menu()]]
    await query.edit_message_text(bold(f"📘 بنك امتحانات الفصل {chapter}\n{DIV}\n{message}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v42_notifications_menu(query):
    rows=await db.v39_notifications(query.from_user.id,limit=30); unread=[r for r in rows if not r.get("read_at")]
    lines=["🔔 مركز الاشعارات",DIV,f"🔴 غير مقروء: {len(unread)}",""]
    for row in rows[:15]:
        mark="🔴" if not row.get("read_at") else "⚪"
        when=row["created_at"].astimezone(TIMEZONE).strftime("%d/%m %H:%M") if getattr(row["created_at"],"tzinfo",None) else row["created_at"].strftime("%d/%m %H:%M")
        lines.append(f"{mark} {when} | {row['title']}\n{row['body'][:180]}")
    if not rows: lines.append("لا توجد اشعارات محفوظة.")
    kb=[]
    if unread: kb.append([InlineKeyboardButton("✅ تعليم الكل كمقروء",callback_data="notifications_read_all")])
    kb.append([back_menu()])
    await query.edit_message_text(bold("\n\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v42_admin_exam_list(query):
    rows=await db.v42_admin_exam_definitions(); kb=[]
    for row in rows[:80]:
        scope="الدورة" if row.get("target_scope")=="course" else f"الفصل {row.get('chapter') or '-'}"
        state="🟢" if row.get("has_open") else "⚪"
        kb.append([InlineKeyboardButton(f"{state} {row['title'].replace('[تراكمي] ','')} | {scope} | 👥 {row.get('student_count',0)}",callback_data=f"v42_admin_exam|{row['id']}")])
    if not rows: text="لا توجد امتحانات منشورة حاليا."
    else: text="كل امتحان ظاهر مرة واحدة فقط. النسخ الداخلية المنفصلة تحفظ وقت وتسليم كل طالب ولا تظهر كتكرار."
    kb += [[InlineKeyboardButton("➕ نشر امتحان",callback_data="admin_publish"),back_menu()]]
    await query.edit_message_text(bold("📝 ادارة الامتحانات\n"+DIV+"\n"+text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v42_admin_exam_students(query,definition_id):
    definition=await db.v31_exam_definition_for_admin(definition_id); rows=await db.v42_admin_exam_students(definition_id)
    if not definition:
        await query.answer("الامتحان غير موجود.",show_alert=True); return
    kb=[]
    for row in rows[:100]:
        status="✅ مسلم" if row.get("submitted_at") else ("🟢 مفتوح" if not row.get("closed") else "🔒 مغلق")
        kb.append([InlineKeyboardButton(f"👤 {row['full_name']} | {status}",callback_data=f"adminexamstudent|{row['task_id']}|{row['user_id']}")])
    if not rows: message="لم ينشئ النظام نسخ الطلاب لهذا الامتحان بعد."
    else: message="اختر الطالب لعرض خيارات التمديد الخاصة بنسخته فقط."
    kb += [[InlineKeyboardButton("◀️ الامتحانات",callback_data="v42_admin_exams"),back_menu()]]
    await query.edit_message_text(bold(f"📝 {definition['title']}\n{DIV}\n{message}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v42_missing_exam_job(context):
    if not OWNER_CHAT_ID: return
    for prep in await db.v42_due_missing_course_exams(20):
        lectures=" + ".join(f"م{x}" for x in sorted(_v42_prep_lectures(prep)))
        try:
            await context.bot.send_message(OWNER_CHAT_ID,bold(f"⚠️ تنبيه امتحان غير منشور\n{DIV}\nمرت 12 ساعة على نشر التحضير ولم يرتبط به امتحان بعد.\n📘 الفصل {prep['chapter']} | تحضير {prep.get('chapter_prep_no') or prep['prep_no']} | {lectures}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ نشر الامتحان",callback_data="courseexam_start")]]))
            await db.v42_mark_missing_exam_reminded(prep["prep_no"])
        except TelegramError:
            logger.warning("missing exam reminder retry pending for prep %s",prep["prep_no"])


_v42_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ""; uid=query.from_user.id
    if data=="royal_review_menu":
        context.user_data.pop("v41_review_oath_id",None); await query.answer(); await v42_review_menu(query); return
    if data.startswith("v42_review_prep|"):
        await query.answer(); await v42_review_prep(query,int(data.split("|")[1])); return
    if data=="exams_menu":
        await query.answer(); await v42_exam_menu(query); return
    if data=="v42_exam_bank":
        await query.answer(); await v42_exam_bank(query); return
    if data.startswith("v42_bank_chapter|"):
        await query.answer(); await v42_bank_chapter(query,int(data.split("|")[1])); return
    if data=="v42_bank_locked":
        await query.answer("اكمل المحاضرات المطلوبة اولا.",show_alert=True); return
    if data=="v42_bank_done":
        await query.answer("هذا الامتحان مسلم مسبقا.",show_alert=True); return
    if data.startswith("v42_bank_open|"):
        definition_id=int(data.split("|")[1]); chapter_rows=await db.v42_exam_bank_catalog(uid,int((await db.v31_exam_definition_for_admin(definition_id) or {}).get("chapter") or 0)); row=next((r for r in chapter_rows if int(r["id"])==definition_id),None)
        if not row or not row.get("ready"):
            await query.answer("هذا الامتحان غير جاهز لحسابك.",show_alert=True); return
        task=await db.v42_open_bank_exam(definition_id,uid)
        if not task:
            await query.answer("تعذر تجهيز الامتحان.",show_alert=True); return
        await query.answer(); await show_task(query,context,task["id"]); return
    if data.startswith("examopen|"):
        task_id=int(data.split("|")[1]); task=await db.v30_exam_task_for_student(uid,task_id); student=await get_student(uid)
        if task and student and student.get("study_track")=="course" and task.get("target_scope")=="chapter":
            if task.get("closed") or (task.get("deadline") and task["deadline"]<=datetime.now(TIMEZONE)):
                await query.answer("انتهت مدة هذا الامتحان. افتحه من بنك الفصول لبدء محاولة جديدة.",show_alert=True); return
            await query.answer(); await show_task(query,context,task_id); return
    if data=="notifications":
        await query.answer(); await v42_notifications_menu(query); return
    if data=="notifications_read_all":
        count=await db.v28_mark_notifications_read(uid); await query.answer(f"تم تعليم {count} اشعار كمقروء."); await v42_notifications_menu(query); return
    if data in ("v42_admin_exams","admin_exam_extensions"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        await query.answer(); await v42_admin_exam_list(query); return
    if data.startswith("v42_admin_exam|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        await query.answer(); await v42_admin_exam_students(query,int(data.split("|")[1])); return
    if data.startswith("freeextend|"):
        task_id=int(data.split("|")[1]); await query.answer("جاري تنفيذ التمديد...")
        try: result=await db.v39_free_exam_extension(task_id,uid,24)
        except Exception:
            logger.exception("weekly free extension failed for task %s user %s",task_id,uid)
            await query.edit_message_text(bold("⚠️ تعذر تنفيذ التمديد الان. حاول مرة ثانية بعد قليل."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الامتحان",callback_data=f"task|{task_id}"),back_menu()]])); return
        messages={"used":"استخدمت التمديد المجاني لهذا الاسبوع.","submitted":"سلّمت هذا الامتحان مسبقا.","not_found":"الامتحان غير متاح."}
        if result.get("status")!="ok":
            await query.edit_message_text(bold("⚠️ "+messages.get(result.get("status"),"تعذر التمديد.")),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الامتحان",callback_data=f"task|{task_id}"),back_menu()]])); return
        until=result["extended_until"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
        await query.edit_message_text(bold(f"🎁 تم تمديد الامتحان 24 ساعة.\n⏰ الموعد الجديد: {until}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 فتح الامتحان",callback_data=f"task|{task_id}"),back_menu()]])); return
    if data=="tasks|exam|all" and is_admin(uid):
        await query.answer(); await v42_admin_exam_list(query); return
    if data=="linkedexamfinish":
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state or state.get("step")!="media" or not state.get("media"):
            return await _v42_previous_button_handler(update,context)
        selected=state.get("selected_preps") or []; labels=[]
        for chapter,prep_no in selected:
            groups=CHAPTER_PREPARATION_DISTRIBUTION.get(int(chapter),[]); nums=groups[int(prep_no)-1] if 1<=int(prep_no)<=len(groups) else []
            labels.append(f"الفصل {chapter} | المحاضرات "+" + ".join(str(x) for x in nums))
        labels += [f"الفصل {chapter} | م{lecture}" for chapter,lecture in state.get("selected_lectures",[])]
        await query.answer(); await query.edit_message_text(bold("🔎 تأكيد ربط الامتحان\n"+DIV+"\n"+("\n".join(f"• {x}" for x in labels) or "لا يوجد محتوى")+"\n\nتأكد من أرقام المحاضرات قبل النشر. هذا الربط ثابت ولن ينتقل إلى محاضرات أخرى."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نشر بهذا الربط",callback_data="v42_exam_confirm")],[InlineKeyboardButton("◀️ تعديل الاختيار",callback_data="courseexam_start" if state.get("audience")=="course" else "linkedexam_start"),back_menu()]])); return
    if data=="v42_exam_confirm":
        state=context.user_data.get("linked_exam")
        if not is_admin(uid) or not state or not state.get("media") or (not state.get("selected_preps") and not state.get("selected_lectures")):
            await query.answer("ابدأ نشر الامتحان من جديد.",show_alert=True); return
        try:
            definition=await create_linked_exam_definition(state.get("selected_preps",[]),state["title"],uid,state["media"],state.get("audience","chapter"),state.get("selected_lectures",[]),"cumulative" if state.get("cumulative") else "normal",DEFAULT_EXAM_HOURS)
        except Exception:
            logger.exception("v42 exam publishing failed"); await query.answer("تعذر حفظ الامتحان.",show_alert=True); return
        audience="الدورة الحالية" if state.get("audience")=="course" else f"الفصل {definition['chapter']}"
        selected_text=f"{len(state.get('selected_preps',[]))} مجموعة محاضرات" if state.get('selected_preps') else "محاضرات منفردة"
        context.user_data.pop("linked_exam",None)
        if state.get("audience")=="course" and (await db.v47_course_window(definition['id']))['status']=='needs_schedule':
            await query.answer('تم حفظ الأسئلة؛ حدد الموعدين')
            await query.edit_message_text(f"✅ حُفظت أسئلة الامتحان رقم {definition['id']}. موعده الأصلي مضى؛ يجب تحديد وقت النشر والانتهاء قبل فتحه للطلاب.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🗓 تحديد الموعدين',callback_data=f"v47_window|{definition['id']}")],[back_menu()]])); return
        await query.answer("تم حفظ الامتحان")
        await query.edit_message_text(bold(f"✅ تم حفظ الامتحان مرة واحدة\n{DIV}\n🎯 المسار: {audience}\n🎞 الربط الثابت: {selected_text}\n\n⏰ امتحان الدورة يظهر بعد 12 ساعة من نشر المحاضرات.\n📘 امتحان الفصل يظهر بعد إكمال الطالب للمحاضرات وموافقة ولي الأمر."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    return await _v42_previous_button_handler(update,context)


_v42_previous_post_init=post_init
async def post_init(app):
    await _v42_previous_post_init(app)
    if not await setting_value("v42_schedule_rules_repaired"):
        repaired=await db.v42_repair_chapter_schedules()
        await set_setting_value("v42_schedule_rules_repaired",str(repaired))
        logger.info("v42 repaired %s chapter schedules",repaired)
    app.job_queue.run_repeating(v42_missing_exam_job,300,first=40,name="v42_missing_course_exam")


# ========================= v43 EXAM RECOVERY UI =========================

async def v42_admin_exam_list(query):
    rows=await db.v42_admin_exam_definitions(); kb=[]
    for row in rows[:80]:
        scope="الدورة" if row.get("target_scope")=="course" else f"الفصل {row.get('chapter') or '-'}"
        state="🟢" if row.get("has_open") else "🔒"
        kb.append([InlineKeyboardButton(f"{state} {row['title'].replace('[تراكمي] ','')} | {scope} | 👥 {row.get('student_count',0)}",callback_data=f"v43_admin_exam|{row['id']}")])
    if not rows: text="لا توجد امتحانات منشورة حاليا."
    else: text="اختر الامتحان لتمديده حتى بعد انتهاء وقته، او ازالة انذاراته، او ادارة كل طالب بصورة منفصلة."
    kb += [[InlineKeyboardButton("➕ نشر امتحان",callback_data="admin_publish"),back_menu()]]
    await query.edit_message_text(bold("📝 مركز ادارة الامتحانات\n"+DIV+"\n"+text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v43_admin_exam_detail(query,definition_id):
    row=await db.v43_exam_definition_detail(definition_id)
    if not row:
        await query.answer("الامتحان غير موجود.",show_alert=True); return
    scope="الدورة الحالية" if row.get("target_scope")=="course" else f"الفصل {row.get('chapter') or '-'}"
    state="مفتوح لبعض الطلاب" if row.get("has_open") else "منتهي او لم يفتح بعد"
    text=(f"📝 {row['title']}\n{DIV}\n🎯 المسار: {scope}\n📌 الحالة: {state}\n"
          f"👥 الطلاب: {row.get('student_count',0)}\n✅ المسلّمين: {row.get('submitted_count',0)}\n"
          f"⚠️ انذارات هذا الامتحان: {row.get('warning_count',0)}\n\n"
          "يمكنك تمديد جميع الطلاب غير المسلّمين حتى اذا كان الامتحان مغلقا. عند التمديد يحذف البوت انذار عدم التسليم القديم ويفتح الامتحان من جديد.")
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("🗓 تحديد موعد النشر والانتهاء",callback_data=f"v47_window|{definition_id}")],
        [InlineKeyboardButton("⏳ تمديد وفتح للجميع",callback_data=f"v43_extend_all_menu|{definition_id}")],
        [InlineKeyboardButton("⚠️ ازالة انذارات هذا الامتحان",callback_data=f"v43_clear_exam_warn|{definition_id}")],
        [InlineKeyboardButton("👥 ادارة كل طالب",callback_data=f"v42_admin_exam|{definition_id}|students")],
        [InlineKeyboardButton("🗑 حذف الامتحان",callback_data=f"adminexamdelete|{definition_id}")],
        [InlineKeyboardButton("◀️ كل الامتحانات",callback_data="v42_admin_exams"),back_menu()]])
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v43_extend_all_menu(query,definition_id):
    row=await db.v43_exam_definition_detail(definition_id)
    if not row:
        await query.answer("الامتحان غير موجود.",show_alert=True); return
    choices=[1,2,6,12,24,48,72]
    buttons=[]
    for index in range(0,len(choices),2):
        buttons.append([InlineKeyboardButton(f"{hours} ساعة",callback_data=f"v43_extend_all|{definition_id}|{hours}") for hours in choices[index:index+2]])
    buttons.append([InlineKeyboardButton("◀️ الامتحان",callback_data=f"v43_admin_exam|{definition_id}"),back_menu()])
    await query.edit_message_text(bold(f"⏳ تمديد وفتح الامتحان للجميع\n{DIV}\n📝 {row['title']}\n\nاختر المدة. سيفتح الامتحان لكل طالب لم يسلّم، وتحذف انذارات عدم التسليم السابقة المرتبطة بهذا الامتحان."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(buttons))


async def v43_notify_exam_reopened(context,item,title):
    until=item["extended_until"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
    removed=int(item.get("warnings_removed") or 0)
    warning_text=f"\n✅ حذف البوت {removed} انذار مرتبط بهذا الامتحان." if removed else ""
    student={"user_id":item["user_id"],"full_name":item.get("student_name") or item.get("full_name") or "الطالب","parent_chat_id":item.get("parent_chat_id")}
    await notify_student_and_parent(context.bot,student,f"⏳ تم تمديد وفتح الامتحان من الادارة\n📝 {title}\n⏰ الموعد الجديد: {until}{warning_text}")
    if removed and int(item.get("warnings") or 0)<MAX_WARNINGS and BIOLOGY_GROUP_ID:
        try: await context.bot.unban_chat_member(BIOLOGY_GROUP_ID,item["user_id"],only_if_banned=True)
        except TelegramError: pass


async def v43_warnings_menu(query):
    rows=await db.v43_warning_students(); kb=[]
    for row in rows[:100]:
        kb.append([InlineKeyboardButton(f"⚠️ {row['full_name']} | {row['warnings']} انذار",callback_data=f"v43_warning_student|{row['user_id']}")])
    if not rows: text="لا توجد انذارات مسجلة على اي طالب."
    else: text="اختر الطالب، ثم اختر الانذار المحدد الذي تريد حذفه."
    kb.append([back_menu()])
    await query.edit_message_text(bold("⚠️ ادارة انذارات الطلاب\n"+DIV+"\n"+text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v43_student_warnings(query,user_id):
    student=await get_student(user_id); rows=await student_warning_history(user_id); kb=[]
    if not student:
        await query.answer("الطالب غير موجود.",show_alert=True); return
    for row in rows[:50]:
        reason=" ".join(str(row["reason"]).split())
        kb.append([InlineKeyboardButton(f"🗑 {reason[:45]}",callback_data=f"v43_warning_confirm|{user_id}|{row['id']}")])
    kb += [[InlineKeyboardButton("◀️ كل الطلاب",callback_data="v43_warnings"),back_menu()]]
    text=f"⚠️ انذارات {student['full_name']}\n{DIV}\nالمجموع: {student['warnings']}\n\nاضغط الانذار المطلوب حذفه. لن يحذف البوت اي انذار اخر."
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


def main_menu(admin=False):
    if admin:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 ادارة الطلبة",callback_data="admin_students"),InlineKeyboardButton("👪 اولياء الامور",callback_data="admin_parents")],
            [InlineKeyboardButton("⚠️ ادارة الانذارات",callback_data="v43_warnings"),InlineKeyboardButton("➕ نشر واجب او امتحان",callback_data="admin_publish")],
            [InlineKeyboardButton("🗓 جدول التحاضير",callback_data="prep_schedule"),InlineKeyboardButton("⏳ تمديد الامتحانات",callback_data="admin_exam_extensions")],
            [InlineKeyboardButton("🗑 ادارة وحذف الامتحانات",callback_data="admin_exam_delete_menu")],
            [InlineKeyboardButton("🔄 طلبات تغيير المسار",callback_data="track_change_requests")],
            [InlineKeyboardButton("🧪 تحاضير اليوم",callback_data="today_prep"),InlineKeyboardButton("📝 مركز الامتحانات",callback_data="v42_admin_exams")],
            [InlineKeyboardButton("🔔 الاشعارات",callback_data="notifications"),InlineKeyboardButton("⚙️ اعدادات الحساب",callback_data="account_settings")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 ابدا جلسة اليوم",callback_data="daily_learning_session")],
        [InlineKeyboardButton("🧪 تحضير اليوم",callback_data="today_prep"),InlineKeyboardButton("🗓 جدولي الدراسي",callback_data="schedules_menu")],
        [InlineKeyboardButton("🏁 متى ننهي المنهج؟",callback_data="chapter_completion_schedule")],
        [InlineKeyboardButton("📚 الواجبات",callback_data="tasks|homework"),InlineKeyboardButton("📝 الامتحانات",callback_data="exams_menu")],
        [InlineKeyboardButton("🎬 المحاضرات",callback_data="playlists"),InlineKeyboardButton("📚 التراكمي",callback_data="backlog_auto")],
        [InlineKeyboardButton("📖 الملازم والملخصات",callback_data="study_resources"),InlineKeyboardButton("✅ الاجوبة النموذجية",callback_data="resourcecategory|model_answer")],
        [InlineKeyboardButton("👑 المراجعة الذكية",callback_data="royal_review_menu"),InlineKeyboardButton("🎯 نقاط ضعفي",callback_data="weaknesses_menu")],
        [InlineKeyboardButton("📊 تقدمي",callback_data="academic_dashboard"),InlineKeyboardButton("🧭 خريطة الاتقان",callback_data="mastery_map")],
        [InlineKeyboardButton("🏅 انجازاتي",callback_data="achievement_menu"),InlineKeyboardButton("⭐ متجر XP",callback_data="xp_store")],
        [InlineKeyboardButton("🔔 الاشعارات",callback_data="notifications"),InlineKeyboardButton("⚙️ اعدادات الحساب",callback_data="account_settings")],
    ])


_v43_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ""; uid=query.from_user.id
    if data in ("v42_admin_exams","admin_exam_extensions") or (data=="tasks|exam|all" and is_admin(uid)):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        await query.answer(); await v42_admin_exam_list(query); return
    if data.startswith("v43_admin_exam|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        await query.answer(); await v43_admin_exam_detail(query,int(data.split("|")[1])); return
    if data.startswith("v42_admin_exam|") and data.endswith("|students"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        definition_id=int(data.split("|")[1]); await query.answer(); await v42_admin_exam_students(query,definition_id); return
    if data.startswith("v43_extend_all_menu|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        await query.answer(); await v43_extend_all_menu(query,int(data.split("|")[1])); return
    if data.startswith("v43_extend_all|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        _,definition_s,hours_s=data.split("|"); definition_id,hours=int(definition_s),int(hours_s); await query.answer("جاري فتح الامتحان وتمديده...")
        result=await db.v43_extend_exam_definition(definition_id,hours,uid)
        if result.get("status")!="ok":
            await query.edit_message_text(bold("⚠️ تعذر تمديد الامتحان."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
        for item in result["students"]: await v43_notify_exam_reopened(context,item,result["definition"]["title"])
        await query.edit_message_text(bold(f"✅ تم فتح وتمديد الامتحان\n{DIV}\n📝 {result['definition']['title']}\n👥 الطلاب غير المسلّمين: {len(result['students'])}\n⚠️ الانذارات المحذوفة: {result['warnings_removed']}\n⏳ مدة التمديد: {hours} ساعة."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الامتحان",callback_data=f"v43_admin_exam|{definition_id}"),back_menu()]])); return
    if data.startswith("adminexamextend|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        _,task_s,student_s,hours_s=data.split("|"); task_id,student_id,hours=int(task_s),int(student_s),int(hours_s); await query.answer("جاري التمديد...")
        result=await db.v43_extend_exam_student(task_id,student_id,hours,uid)
        messages={"not_found":"الامتحان او الطالب غير موجود.","submitted":"الطالب سلّم الامتحان مسبقا."}
        if result.get("status")!="ok":
            await query.edit_message_text(bold("⚠️ "+messages.get(result.get("status"),"تعذر تنفيذ التمديد.")),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
        await v43_notify_exam_reopened(context,result,result["title"]); until=result["extended_until"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
        await query.edit_message_text(bold(f"✅ تم فتح وتمديد امتحان الطالب\n{DIV}\n👤 {result['student_name']}\n📝 {result['title']}\n⏰ الموعد الجديد: {until}\n⚠️ الانذارات المحذوفة: {result['warnings_removed']}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الامتحان",callback_data=f"v43_admin_exam|{result['definition_id']}"),back_menu()]])); return
    if data.startswith("v43_clear_exam_warn|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        definition_id=int(data.split("|")[1]); row=await db.v43_exam_definition_detail(definition_id)
        if not row: await query.answer("الامتحان غير موجود.",show_alert=True); return
        await query.answer(); await query.edit_message_text(bold(f"⚠️ تاكيد ازالة انذارات الامتحان\n{DIV}\n📝 {row['title']}\n📌 عدد الانذارات المرتبطة: {row.get('warning_count',0)}\n\nسيحذف البوت انذارات هذا الامتحان فقط، ولن يعيدها نظام الاسترداد."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم، ازلها",callback_data=f"v43_clear_exam_warn_confirm|{definition_id}")],[InlineKeyboardButton("❌ الغاء",callback_data=f"v43_admin_exam|{definition_id}"),back_menu()]])); return
    if data.startswith("v43_clear_exam_warn_confirm|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        definition_id=int(data.split("|")[1]); await query.answer("جاري ازالة الانذارات..."); result=await db.v43_clear_exam_definition_warnings(definition_id,uid)
        if result.get("status")!="ok":
            await query.edit_message_text(bold("⚠️ تعذر ازالة الانذارات."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
        for item in result["students"]:
            if int(item.get("warnings_removed") or 0)<=0: continue
            student={"user_id":item["user_id"],"full_name":item["full_name"],"parent_chat_id":item.get("parent_chat_id")}
            await notify_student_and_parent(context.bot,student,f"✅ ازالت الادارة انذار عدم التسليم الخاص بامتحان:\n📝 {result['title']}")
            if int(item.get("warnings") or 0)<MAX_WARNINGS and BIOLOGY_GROUP_ID:
                try: await context.bot.unban_chat_member(BIOLOGY_GROUP_ID,item["user_id"],only_if_banned=True)
                except TelegramError: pass
        await query.edit_message_text(bold(f"✅ تمت ازالة انذارات الامتحان\n{DIV}\n📝 {result['title']}\n⚠️ عدد الانذارات المحذوفة: {result['removed']}\nلم تتاثر بقية انذارات الطلاب."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الامتحان",callback_data=f"v43_admin_exam|{definition_id}"),back_menu()]])); return
    if data=="v43_warnings":
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        await query.answer(); await v43_warnings_menu(query); return
    if data.startswith("v43_warning_student|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        await query.answer(); await v43_student_warnings(query,int(data.split("|")[1])); return
    if data.startswith("v43_warning_confirm|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        _,student_s,warning_s=data.split("|"); student_id,warning_id=int(student_s),int(warning_s); rows=await student_warning_history(student_id); warning=next((row for row in rows if int(row["id"])==warning_id),None)
        if not warning: await query.answer("الانذار غير موجود.",show_alert=True); return
        await query.answer(); await query.edit_message_text(bold(f"🗑 تاكيد حذف الانذار\n{DIV}\nالسبب: {warning['reason']}\n\nسيحذف هذا الانذار وحده فقط."),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ حذف الانذار",callback_data=f"v43_warning_remove|{student_id}|{warning_id}")],[InlineKeyboardButton("❌ الغاء",callback_data=f"v43_warning_student|{student_id}"),back_menu()]])); return
    if data.startswith("v43_warning_remove|"):
        if not is_admin(uid): await query.answer("هذا القسم للادارة فقط.",show_alert=True); return
        _,student_s,warning_s=data.split("|"); student_id,warning_id=int(student_s),int(warning_s); result=await db.v43_remove_warning(student_id,warning_id,uid)
        if result.get("status")!="ok": await query.answer("الانذار غير موجود.",show_alert=True); return
        await query.answer("تم حذف الانذار.",show_alert=True); student=await get_student(student_id)
        if student: await notify_student_and_parent(context.bot,student,f"✅ ازالت الادارة انذارا من حسابك\nالسبب: {result['warning']['reason']}\n⚠️ المتبقي: {result['student']['warnings']}/{MAX_WARNINGS}")
        if int(result["student"]["warnings"] or 0)<MAX_WARNINGS and BIOLOGY_GROUP_ID:
            try: await context.bot.unban_chat_member(BIOLOGY_GROUP_ID,student_id,only_if_banned=True)
            except TelegramError: pass
        await v43_student_warnings(query,student_id); return
    return await _v43_previous_button_handler(update,context)


_v43_previous_post_init=post_init
async def post_init(app):
    await _v43_previous_post_init(app)
    if not await setting_value("v43_premature_exam_warnings_repaired"):
        repaired=await db.v43_repair_premature_exam_warnings()
        await set_setting_value("v43_premature_exam_warnings_repaired",str(len(repaired)))
        for item in repaired:
            if int(item.get("warnings") or 0)<MAX_WARNINGS and BIOLOGY_GROUP_ID:
                try: await app.bot.unban_chat_member(BIOLOGY_GROUP_ID,item["user_id"],only_if_banned=True)
                except TelegramError: pass
        if OWNER_CHAT_ID and repaired:
            try:
                await app.bot.send_message(OWNER_CHAT_ID,bold(f"✅ اصلاح تلقائي لانذارات الامتحانات\n{DIV}\nتم حذف {len(repaired)} انذار صدر قبل الموعد الصحيح بسبب نشر الامتحان متاخرا.\nيمكنك ادارة اي انذار اخر من زر ادارة الانذارات."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
    reconciled=await db.v43_reconcile_warning_counts()
    if reconciled: logger.info("v43 reconciled %s warning counters",reconciled)


# ========================= v44 STUDY FLOW RELIABILITY =========================

async def v44_show_today_preparation(query):
    """Render one authoritative current preparation with working lecture links."""
    uid=query.from_user.id
    student=await get_student(uid)
    if not student and not is_admin(uid):
        await query.edit_message_text(bold("🔒 سجل حساب الطالب من /start اولا."),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if student and not is_admin(uid):
        blocking=await student_exam_lock(uid)
        if blocking:
            effective=await effective_task_deadline(blocking["id"],uid)
            expired=bool(effective and effective.get("deadline") and datetime.now(TIMEZONE)>=effective["deadline"])
            state="منتهي ويحتاج تمديد" if expired or blocking.get("closed") else ("بانتظار التفعيل" if blocking.get("exam_pending_activation") else "مفتوح ولم يسلم")
            await query.edit_message_text(bold(
                f"🔒 التحضير التالي متوقف مؤقتا\n{DIV}\n📝 {blocking['title']}\n📌 الحالة: {state}\n\n"
                "سلم الامتحان اولا. اذا انتهى وقته افتحه واختر التمديد المجاني الاسبوعي او اطلب تمديدا من الادارة."),
                parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 الذهاب الى الامتحان",callback_data=f"task|{blocking['id']}")],
                    [back_menu()]])); return
    personal=bool(student and (student.get("study_track")=="chapter" or student.get("schedule_mode")=="custom"))
    row=await personal_preparation_for_student(uid,datetime.now(TIMEZONE).date()) if personal else await latest_preparation()
    if not row:
        await query.edit_message_text(bold("📭 لا يوجد تحضير منشور او مستحق حاليا."),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 المراجعة الملكية",callback_data="royal_review_menu")],[back_menu()]])); return
    chapter=int(row.get("chapter") or (student or {}).get("current_chapter") or 1)
    lectures=[int(x) for x in str(row.get("lectures") or "").split(",") if x.strip().isdigit()]
    available=[]; completed=[]
    for lecture in lectures:
        progress=await lecture_progress(uid,chapter,lecture) if student else None
        (completed if progress and progress.get("completed_at") else available).append(lecture)
    date_value=row.get("target_date")
    date_text=date_value.strftime("%d/%m/%Y") if date_value else "-"
    lines=["🧪 تحضيرك الحالي",DIV,f"📘 الفصل {chapter}",f"🎬 المحاضرات: {' + '.join(map(str,lectures)) or '-'}",f"📅 التاريخ: {date_text}"]
    kb=[]
    for lecture in available:
        kb.append([InlineKeyboardButton(f"▶️ الذهاب الى المحاضرة {lecture}",callback_data=f"prepopen|{chapter}|{lecture}")])
    if completed: lines.append(f"✅ مكتمل: {' + '.join(map(str,completed))}")
    if not available:
        lines += ["","✅ اكملت هذا التحضير.","سيظهر التحضير التالي فور نشره للدورة، او حسب ترتيب جدولك الشخصي."]
        kb.append([InlineKeyboardButton("👑 افتح المراجعة الملكية",callback_data="royal_review_menu")])
    kb.append([InlineKeyboardButton("⚡ اختبارات المراجعة السريعة",callback_data="v48_quick_reviews")])
    kb.append([back_menu()])
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v44_open_preparation(query,chapter,lecture):
    """Open a lecture without passing through the legacy callback layers."""
    uid=query.from_user.id
    if chapter not in PLAYLISTS or lecture<1 or lecture>len(PLAYLISTS[chapter]):
        await query.answer("المحاضرة غير موجودة.",show_alert=True); return
    student=await get_student(uid)
    if student and not is_admin(uid):
        blocking=await student_exam_lock(uid)
        if blocking:
            await query.answer("يجب تسليم الامتحان قبل فتح التحضير التالي.",show_alert=True)
            await v44_show_today_preparation(query); return
        personal=student.get("study_track")=="chapter" or student.get("schedule_mode")=="custom"
        if personal:
            access=await db.v37_preparation_access(uid,chapter,lecture)
            if not access.get("allowed"):
                await query.answer("هذه المحاضرة ليست ضمن تحضيرك الحالي.",show_alert=True)
                await v44_show_today_preparation(query); return
        progress=await lecture_progress(uid,chapter,lecture)
        if progress and progress.get("completed_at"):
            await query.answer("هذه المحاضرة مكتملة. عرضنا لك التحضير الحالي.")
            await v44_show_today_preparation(query); return
    await query.answer("تم فتح التحضير.")
    item=PLAYLISTS[chapter][lecture-1]
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ مشاهدة محاضرة البوت",callback_data=f"prepwatch|{chapter}|{lecture}")],
        [InlineKeyboardButton("📚 درستها من مصدر خاص",callback_data=f"prepprivate|{chapter}|{lecture}")],
        [InlineKeyboardButton("◀️ تحضير اليوم",callback_data="today_prep"),back_menu()]])
    await query.edit_message_text(bold(f"🎬 الفصل {chapter} | المحاضرة {lecture}\n{item[1]}\n\nاختر طريقة دراستك للمحاضرة:"),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v39_daily_session(query):
    """Today's session contains only the actual next study action."""
    uid=query.from_user.id; student=await get_student(uid)
    if not student:
        await query.edit_message_text(bold("🔒 سجل حساب الطالب من /start اولا."),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    blocking=await student_exam_lock(uid)
    if blocking:
        text=f"🎯 جلسة اليوم\n{DIV}\n1️⃣ المهمة الاهم: تسليم الامتحان\n📝 {blocking['title']}\n\nبعد التسليم يفتح التحضير التالي تلقائيا."
        kb=[[InlineKeyboardButton("📝 فتح الامتحان",callback_data=f"task|{blocking['id']}")],[back_menu()]]
    else:
        personal=student.get("study_track")=="chapter" or student.get("schedule_mode")=="custom"
        prep=await personal_preparation_for_student(uid,datetime.now(TIMEZONE).date()) if personal else await latest_preparation()
        if prep:
            chapter=int(prep.get("chapter") or student.get("current_chapter") or 1)
            lectures=[int(x) for x in str(prep.get("lectures") or "").split(",") if x.strip().isdigit()]
            available=[]
            for lecture in lectures:
                progress=await lecture_progress(uid,chapter,lecture)
                if not progress or not progress.get("completed_at"): available.append(lecture)
            if available:
                text=f"🎯 جلسة اليوم\n{DIV}\n📘 الفصل {chapter}\n🎬 ابدأ بالمحاضرة {available[0]}"
                kb=[[InlineKeyboardButton(f"▶️ الذهاب الى المحاضرة {available[0]}",callback_data=f"prepopen|{chapter}|{available[0]}")],
                    [InlineKeyboardButton("🧪 عرض التحضير كاملا",callback_data="today_prep"),back_menu()]]
            else:
                text=f"🎯 جلسة اليوم\n{DIV}\n✅ تحضير الفصل {chapter} مكتمل.\nافتح المراجعة الملكية او انتظر التحضير التالي."
                kb=[[InlineKeyboardButton("👑 المراجعة الملكية",callback_data="royal_review_menu")],[back_menu()]]
        else:
            text=f"🎯 جلسة اليوم\n{DIV}\n✅ لا يوجد تحضير مستحق حاليا.\nراجع المواعيد المستحقة في المراجعة الملكية."
            kb=[[InlineKeyboardButton("👑 المراجعة الملكية",callback_data="royal_review_menu")],[back_menu()]]
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v42_review_menu(query):
    """Royal spaced repetition only; the self-quiz was intentionally removed."""
    data=await db.v42_review_context(query.from_user.id); student=data.get("student") or {}; pending=data.get("pending") or []
    due=[row for row in pending if row.get("due")]
    lines=["👑 المراجعة الملكية",DIV,"المواعيد: بعد 6 ساعات، 24 ساعة، اسبوع، ثم شهر.",
           f"🔥 مستحق الان: {len(due)}",f"✅ مكتمل: {data.get('completed',0)}",""]
    kb=[]
    if student.get("study_track")=="course":
        lines.append("📚 تبدأ من تحضير 9 في الفصل الثالث، وما قبله غير محسوب:")
        groups=[]; used=set()
        for prep in data.get("preparations") or []:
            lectures=_v42_prep_lectures(prep)
            items=[r for r in pending if int(r["chapter"])==int(prep["chapter"]) and int(r["lecture"]) in lectures]
            if items and int(prep["prep_no"]) not in used:
                used.add(int(prep["prep_no"])); groups.append((prep,items))
        for prep,items in groups[:30]:
            mark="🔥" if any(item.get("due") for item in items) else "⏳"
            lectures=" + ".join(f"م{x}" for x in sorted(_v42_prep_lectures(prep)))
            kb.append([InlineKeyboardButton(f"{mark} تحضير {prep.get('chapter_prep_no') or prep['prep_no']} | ف{prep['chapter']} | {lectures}",callback_data=f"v42_review_prep|{prep['prep_no']}")])
        if not groups: lines.append("اكمل اول تحضير حتى يبني البوت مواعيد مراجعته.")
    else:
        chapter=int(student.get("current_chapter") or 1); lines.append(f"📘 الفصل {chapter} من اول محاضرة:")
        for row in due[:30]:
            kb.append([InlineKeyboardButton(f"🔥 م{row['lecture']} | المراجعة {row['stage']}",callback_data=f"royal_review|{row['id']}")])
        if pending and not due:
            first=pending[0]; lines.append(f"اقرب مراجعة: م{first['lecture']} في {first['due_at'].astimezone(TIMEZONE):%d/%m %H:%M}")
        elif not pending: lines.append("اكمل المحاضرة الاولى حتى يبدا نظام المراجعة.")
    kb.append([back_menu()])
    await query.edit_message_text(bold("\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v44_notifications_menu(query):
    """Show unread notifications only, so marking read removes them visibly."""
    rows=await db.v28_unread_notifications(query.from_user.id,limit=30)
    lines=["🔔 الاشعارات الجديدة",DIV,f"🔴 غير مقروء: {len(rows)}",""]
    for row in rows[:20]:
        created=row["created_at"]
        when=created.astimezone(TIMEZONE).strftime("%d/%m %H:%M") if getattr(created,"tzinfo",None) else created.strftime("%d/%m %H:%M")
        lines.append(f"🔴 {when} | {row['title']}\n{row['body'][:220]}")
    if not rows: lines.append("📭 لا توجد اشعارات جديدة.")
    kb=[]
    if rows: kb.append([InlineKeyboardButton("✅ تعليم الكل كمقروء",callback_data="notifications_read_all")])
    kb.append([back_menu()])
    await query.edit_message_text(bold("\n\n".join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v41_review_reminders_job(context):
    """Queue durable royal-review reminders in the notification center."""
    for row in await db.v41_due_review_reminders(100):
        stage=int(row["stage"])
        title=f"👑 حان موعد {REVIEW_STAGE_LABELS[stage]}"
        body=(f"📘 الفصل {row['chapter']} | المحاضرة {row['lecture']}\n"
              "افتح المراجعة الملكية وراجع المحاضرة، ثم ارسل القسم المخصص لاعتمادها.")
        try:
            await db.v44_queue_review_notification(row["id"],title,body)
        except Exception:
            logger.exception("royal review notification queue failed for review %s",row["id"])


_v44_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ""; uid=query.from_user.id
    if data=="today_prep":
        await query.answer("جاري فتح التحضير..."); await v44_show_today_preparation(query); return
    if data.startswith("prepopen|"):
        try:
            _,chapter_s,lecture_s=data.split("|")
            chapter,lecture=int(chapter_s),int(lecture_s)
        except (TypeError,ValueError):
            await query.answer("رابط التحضير غير صالح.",show_alert=True); return
        await v44_open_preparation(query,chapter,lecture); return
    if data=="daily_learning_session":
        await query.answer(); await v39_daily_session(query); return
    if data=="royal_review_menu":
        context.user_data.pop("v41_review_oath_id",None); await query.answer(); await v42_review_menu(query); return
    if data=="notifications":
        await query.answer(); await v44_notifications_menu(query); return
    if data=="notifications_read_all":
        count=await db.v28_mark_notifications_read(uid)
        await query.answer(f"تمت ازالة {count} اشعار من القائمة.")
        await v44_notifications_menu(query); return
    if data.startswith("freeextend|"):
        try: task_id=int(data.split("|",1)[1])
        except (TypeError,ValueError):
            await query.answer("رقم الامتحان غير صالح.",show_alert=True); return
        await query.answer("جاري تنفيذ التمديد...")
        try: result=await db.v44_free_exam_extension(task_id,uid,24)
        except Exception:
            logger.exception("v44 weekly extension failed for task %s user %s",task_id,uid)
            await query.edit_message_text(bold("⚠️ تعذر تنفيذ التمديد حاليا. حاول مرة ثانية."),parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الامتحان",callback_data=f"task|{task_id}"),back_menu()]])); return
        messages={"used":"استخدمت التمديد المجاني لهذا الاسبوع.","submitted":"سلمت هذا الامتحان مسبقا.","not_found":"الامتحان غير موجود في حسابك."}
        if result.get("status")!="ok":
            await query.edit_message_text(bold("⚠️ "+messages.get(result.get("status"),"تعذر تنفيذ التمديد.")),parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ الامتحان",callback_data=f"task|{task_id}"),back_menu()]])); return
        if BIOLOGY_GROUP_ID:
            student=await get_student(uid)
            if student and int(student.get("warnings") or 0)<MAX_WARNINGS:
                try: await context.bot.unban_chat_member(BIOLOGY_GROUP_ID,uid,only_if_banned=True)
                except TelegramError: pass
        until=result["extended_until"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
        warning_text=f"\n✅ حذف انذار هذا الامتحان: {result.get('warnings_removed',0)}" if result.get("warnings_removed") else ""
        await query.edit_message_text(bold(f"🎁 تم فتح وتمديد الامتحان 24 ساعة\n{DIV}\n📝 {result['title']}\n⏰ الموعد الجديد: {until}{warning_text}"),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 فتح الامتحان الان",callback_data=f"task|{task_id}")],[back_menu()]])); return
    return await _v44_previous_button_handler(update,context)


_v44_previous_post_init=post_init
async def post_init(app):
    await _v44_previous_post_init(app)
    if not await setting_value("v44_review_reminders_rearmed"):
        changed=await db.v44_rearm_legacy_review_reminders()
        await set_setting_value("v44_review_reminders_rearmed",str(changed))
        logger.info("v44 rearmed %s legacy royal-review reminders",changed)


# ========================= v45 EXAM ENFORCEMENT CUTOVER =========================

_v45_previous_show_task=show_task
async def show_task(query,context,task_id):
    """Do not expose expired questions before a teacher approves a late attempt."""
    if is_admin(query.from_user.id):
        return await _v45_previous_show_task(query,context,task_id)
    status=await db.v45_exam_task_status(query.from_user.id,task_id)
    if not status:
        await query.answer("هذا الامتحان غير موجود في حسابك.",show_alert=True); return
    if not status.get("track_allowed"):
        await query.edit_message_text(bold(
            "✅ هذا الامتحان لا يتبع مسارك الدراسي الحالي.\n\n"
            "لن يمنع تحضير اليوم ولن يحسب عليك انذارا."),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧪 الذهاب الى تحضير اليوم",callback_data="today_prep")],[back_menu()]])); return
    if not status.get("enforced"):
        await query.edit_message_text(bold(
            "✅ هذا امتحان سابق وتمت ازالته من النظام الالزامي.\n\n"
            "لا يمنع تحضير اليوم ولا يضيف عليك انذارا."),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧪 الذهاب الى تحضير اليوم",callback_data="today_prep")],[back_menu()]])); return
    if status.get("submitted"):
        return await _v45_previous_show_task(query,context,task_id)
    expired=bool(status.get("effective_deadline") and status["effective_deadline"]<=status["now"])
    if not expired:
        return await _v45_previous_show_task(query,context,task_id)
    request_status=status.get("late_request_status")
    if request_status=="pending":
        request_line="⏳ طلبك مرسل وينتظر موافقة الاستاذ."
        request_button=[]
    elif request_status=="denied":
        request_line="❌ رفض الطلب السابق. يمكنك ارسال طلب جديد اذا سمح لك الاستاذ."
        request_button=[[InlineKeyboardButton(f"👨‍🏫 اريد امتحن الامتحان - خصم {LATE_EXAM_XP_COST} XP",callback_data=f"v45_late_request|{task_id}")]]
    else:
        request_line="يمكنك طلب محاولة متاخرة من الاستاذ، ولن يخصم XP الا بعد الموافقة."
        request_button=[[InlineKeyboardButton(f"👨‍🏫 اريد امتحن الامتحان - خصم {LATE_EXAM_XP_COST} XP",callback_data=f"v45_late_request|{task_id}")]]
    kb=request_button+[
        [InlineKeyboardButton("🎁 التمديد المجاني الاسبوعي",callback_data=f"freeextend|{task_id}")],
        [InlineKeyboardButton("◀️ الامتحانات",callback_data="exams_menu"),back_menu()]]
    await query.edit_message_text(bold(
        f"⏰ انتهى وقت الامتحان\n{DIV}\n📝 {status['title']}\n\n{request_line}\n\n"
        f"عند موافقة الاستاذ يفتح الامتحان لمدة {max(1,int(status.get('exam_duration_hours') or 2))} ساعة ويخصم {LATE_EXAM_XP_COST} XP."),
        parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


_v45_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ""; uid=query.from_user.id
    if data.startswith("task|") and not is_admin(uid):
        try: task_id=int(data.split("|",1)[1])
        except (TypeError,ValueError):
            await query.answer("رقم الامتحان غير صالح.",show_alert=True); return
        await query.answer(); await show_task(query,context,task_id); return
    if data.startswith("v45_late_request|"):
        try: task_id=int(data.split("|",1)[1])
        except (TypeError,ValueError):
            await query.answer("رقم الامتحان غير صالح.",show_alert=True); return
        status=await db.v45_exam_task_status(uid,task_id)
        hours=max(1,min(24,int((status or {}).get("exam_duration_hours") or 2)))
        result=await db.v45_request_late_exam(task_id,uid,LATE_EXAM_XP_COST,hours)
        messages={"not_found":"هذا الامتحان غير معتمد او ليس ضمن حسابك.","submitted":"انت سلمت هذا الامتحان مسبقا.",
                  "open":"الامتحان مفتوح حاليا ولا يحتاج موافقة.","exists":"طلبك مرسل مسبقا وينتظر قرار الاستاذ."}
        if result.get("status")=="xp":
            await query.answer(f"تحتاج {result['required']} XP. رصيدك الحالي {result['current']} XP.",show_alert=True); return
        if result.get("status")!="ok":
            await query.answer(messages.get(result.get("status"),"تعذر ارسال الطلب."),show_alert=True); return
        destination=EXAM_SUBMISSIONS_CHAT_ID or OWNER_CHAT_ID
        if not destination:
            await query.answer("حساب الاستاذ غير مضبوط في اعدادات البوت.",show_alert=True); return
        request=result["request"]; task=result["task"]
        kb=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ موافقة وفتح الامتحان",callback_data=f"v45_late_decide|{request['id']}|1"),
            InlineKeyboardButton("❌ رفض",callback_data=f"v45_late_decide|{request['id']}|0")]])
        thread_id=await ensure_student_topic(context.bot,await get_student(uid),destination)
        try:
            await context.bot.send_message(destination,bold(
                f"👨‍🏫 طلب امتحان متاخر\n{DIV}\n👤 الطالب: {result['student_name']}\n📝 {task['title']}\n"
                f"💰 الخصم عند الموافقة: {LATE_EXAM_XP_COST} XP\n⏳ مدة المحاولة: {request['hours']} ساعة"),
                parse_mode=ParseMode.HTML,message_thread_id=thread_id,reply_markup=kb)
        except TelegramError:
            logger.exception("late exam request delivery failed for request %s",request["id"])
            await db.v45_decide_late_exam_request(request["id"],False,0)
            await query.answer("تعذر ايصال الطلب الى الاستاذ حاليا.",show_alert=True); return
        await query.answer("تم ارسال طلبك الى الاستاذ.",show_alert=True)
        await query.edit_message_text(bold(
            f"⏳ تم ارسال طلب الامتحان المتاخر\n{DIV}\n📝 {task['title']}\n\n"
            f"لن يخصم {LATE_EXAM_XP_COST} XP الا اذا وافق الاستاذ وفتح الامتحان."),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 حالة الامتحان",callback_data=f"task|{task_id}")],[back_menu()]])); return
    if data.startswith("v45_late_decide|"):
        if not is_admin(uid):
            await query.answer("هذا القرار للاستاذ فقط.",show_alert=True); return
        try:
            _,request_s,approved_s=data.split("|")
            request_id=int(request_s); approved=approved_s=="1"
        except (TypeError,ValueError):
            await query.answer("الطلب غير صالح.",show_alert=True); return
        result=await db.v45_decide_late_exam_request(request_id,approved,uid)
        if result.get("status")=="processed":
            await query.answer("تمت معالجة الطلب سابقا.",show_alert=True); return
        if result.get("status")=="submitted":
            await query.answer("الطالب سلم الامتحان مسبقا.",show_alert=True); return
        if result.get("status")=="xp":
            await query.answer(f"رصيد الطالب اقل من {result['xp_cost']} XP.",show_alert=True); return
        if result.get("status")=="denied":
            await query.answer("تم رفض الطلب.",show_alert=True)
            try: await context.bot.send_message(result["user_id"],bold(f"❌ رفض الاستاذ طلب الامتحان المتاخر\n📝 {result['title']}"),parse_mode=ParseMode.HTML)
            except TelegramError: pass
            try: await query.edit_message_text(bold(f"❌ تم رفض طلب الامتحان المتاخر\n{DIV}\n👤 {result['full_name']}\n📝 {result['title']}"),parse_mode=ParseMode.HTML)
            except TelegramError: pass
            return
        if result.get("status")!="approved":
            await query.answer("تعذر معالجة الطلب.",show_alert=True); return
        until=result["extended_until"].astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M")
        if BIOLOGY_GROUP_ID:
            student=await get_student(result["user_id"])
            if student and int(student.get("warnings") or 0)<MAX_WARNINGS:
                try: await context.bot.unban_chat_member(BIOLOGY_GROUP_ID,result["user_id"],only_if_banned=True)
                except TelegramError: pass
        await query.answer("تم فتح الامتحان وخصم XP.",show_alert=True)
        try: await query.edit_message_text(bold(f"✅ تمت الموافقة على طلب الامتحان المتاخر\n{DIV}\n👤 {result['full_name']}\n📝 {result['title']}\n⏰ مفتوح لغاية: {until}\n💰 تم خصم {result['xp_cost']} XP."),parse_mode=ParseMode.HTML)
        except TelegramError: pass
        try:
            await context.bot.send_message(result["user_id"],bold(
                f"✅ وافق الاستاذ وفتح الامتحان\n{DIV}\n📝 {result['title']}\n⏰ اخر موعد: {until}\n"
                f"💰 تم خصم {result['xp_cost']} XP."),parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 ابدا الامتحان الان",callback_data=f"task|{result['task_id']}")]]))
        except TelegramError: pass
        return
    return await _v45_previous_button_handler(update,context)


_v45_previous_post_init=post_init
async def post_init(app):
    await _v45_previous_post_init(app)
    cutoff=await db.v45_configure_exam_policy(EXAM_ENFORCEMENT_START_DATE)
    cleanup_key=f"v45_legacy_exam_cleanup_{cutoff.isoformat()}"
    if not await setting_value(cleanup_key):
        result=await db.v45_retire_legacy_unsubmitted_exams(cutoff)
        await set_setting_value(cleanup_key,str(result.get("retired",0)))
        logger.info("v45 retired %s legacy exam assignments before %s",result.get("retired",0),cutoff)


_v46_previous_post_init=post_init
async def post_init(app):
    await _v46_previous_post_init(app)
    repaired=await db.v46_repair_retained_exam_state(EXAM_ENFORCEMENT_START_DATE)
    reviews=await db.v46_cleanup_course_review_history()
    if repaired.get("reopened") or repaired.get("waivers"):
        logger.info("v46 exam repair reopened=%s waivers=%s",repaired.get("reopened",0),repaired.get("waivers",0))
    if reviews.get("reviews") or reviews.get("notifications"):
        logger.info("v46 course review cleanup reviews=%s notifications=%s",reviews.get("reviews",0),reviews.get("notifications",0))




# ========================= v47 enrollment and scheduling UI =========================
def onboarding_track_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('👥 أكمل مع دورتي الحالية',callback_data='onboardtrack|course')],
        [InlineKeyboardButton('📚 اختيار فصل للدراسة',callback_data='v47_chapters')]])


_v47_previous_main_menu=main_menu
def main_menu(admin=False):
    keyboard=_v47_previous_main_menu(admin)
    if admin: return keyboard
    rows=[list(row) for row in keyboard.inline_keyboard]
    rows.append([InlineKeyboardButton('🔄 إعادة تعيين معلوماتي بالكامل',callback_data='v47_reset')])
    return InlineKeyboardMarkup(rows)


_v47_previous_start=start
async def start(update,context):
    student=await get_student(update.effective_user.id)
    if student and student.get('reset_pending'):
        context.user_data.clear(); context.user_data['registration']={}
        await update.effective_message.reply_text('✍️ أرسل اسمك الثلاثي أو الرباعي لإكمال إعادة التسجيل:')
        return REG_NAME
    return await _v47_previous_start(update,context)


async def v47_reset_confirm(update,context):
    query=update.callback_query
    # A short-lived confirmation nonce belongs to this user, never a callback supplied ID.
    requested=context.user_data.pop('v47_reset_requested',None)
    if not requested or (datetime.now(TIMEZONE)-requested).total_seconds()>600:
        await query.answer('افتح إعادة التعيين من القائمة وأكد الطلب من جديد.',show_alert=True)
        return ConversationHandler.END
    if is_admin(query.from_user.id) or not await db.v47_reset_student(query.from_user.id):
        await query.answer('تعذر إعادة تعيين الحساب.',show_alert=True); return ConversationHandler.END
    context.user_data.clear(); context.user_data['registration']={}
    await query.answer()
    await query.edit_message_text('✅ حُذفت معلوماتك ومسارك وتقدمك السابق. بقي XP والإنذارات وولي الأمر محفوظين.\n\n✍️ أرسل اسمك الثلاثي أو الرباعي:')
    return REG_NAME


_v47_previous_reg_grade=reg_grade
async def reg_grade(update,context):
    student=await get_student(update.effective_user.id)
    if not student or not student.get('reset_pending'): return await _v47_previous_reg_grade(update,context)
    value=(update.message.text or '').strip()
    try: grade=float(value)
    except ValueError: grade=-1
    if not 0<=grade<=100:
        await update.effective_message.reply_text('أرسل معدلاً بين 0 و100، مثل 97.2.'); return REG_GRADE
    reg=context.user_data.get('registration',{})
    if not reg.get('full_name') or not reg.get('school'):
        return await start(update,context)
    registered=await register_student(update.effective_user.id,update.effective_user.username,reg['full_name'],reg['school'],value)
    if registered.get('status')=='parent_account':
        context.user_data.pop('registration',None)
        await update.effective_message.reply_text('هذا الحساب مربوط كولي أمر. احذف حساب ولي الأمر أولاً إذا تريد تسجله كطالب.')
        return ConversationHandler.END
    context.user_data.pop('registration',None)
    await update.effective_message.reply_text('📚 اختر مسار دراستك:',reply_markup=onboarding_track_keyboard())
    return ConversationHandler.END


async def v47_finish_track(query,track,chapter=3,prep_no=1):
    student=await db.v47_choose_track(query.from_user.id,track,chapter,prep_no,datetime.now(TIMEZONE).date())
    if not student:
        await query.answer('المسار محفوظ بالفعل، أو أكمل تسجيل معلوماتك عبر /start.',show_alert=True); return
    await query.answer('تم حفظ المسار')
    if track=='course':
        text='✅ تم ربطك بالدورة الحالية.\nتصل محاضرات الدورة الساعة 11 ليلاً من اليوم السابق، والامتحان بعد 12 ساعة ويبقى 24 ساعة.'
    else:
        groups=CHAPTER_PREPARATION_DISTRIBUTION.get(int(chapter),[])
        numbers=', '.join(map(str,groups[int(prep_no)-1])) if 1<=int(prep_no)<=len(groups) else '-'
        text=(f'✅ يبدأ مسارك من الفصل {chapter}، من المحاضرات {numbers}.\n'
              'لا إنذارات على المحاضرات السابقة، وامتحانات الفصول تظهر حسب المحاضرات المكتملة.\n'
              'المراجعة الملكية تبدأ فقط للمحاضرات التي تكملها فعلياً.')
    if not student.get('parent_chat_id'):
        text+=f"\n\nلربط ولي الأمر يرسل من حسابه:\n/parent {student['parent_link_code']}"
    elif not student.get('approved'):
        text+='\n\nحسابك ينتظر تفعيل الإدارة.'
    await query.edit_message_text(text,reply_markup=main_menu() if student.get('approved') else parent_copy_markup(student['parent_link_code']))


async def v47_window_help(query,definition_id):
    row=await db.v31_exam_definition_for_admin(definition_id)
    if not row: await query.answer('الامتحان غير موجود.',show_alert=True); return
    await query.answer()
    await query.edit_message_text(
        f'🗓 تحديد نشر وانتهاء الامتحان رقم {definition_id}\n\n'
        'أرسل الأمر التالي مع تغيير التاريخ والساعة (بتوقيت بغداد):\n'
        f'/exam_window {definition_id} 2026-09-10 11:00 | 2026-09-11 11:00\n\n'
        'يجب أن يكون النشر في المستقبل والانتهاء بعده. بعد بدء الامتحان استخدم أزرار التمديد.',
        reply_markup=InlineKeyboardMarkup([[back_menu()]]))


async def v47_exam_window_command(update,context):
    if not is_admin(update.effective_user.id): return
    raw=(update.message.text or '').partition(' ')[2]
    try:
        left,right=raw.split('|'); identifier,stamp=left.strip().split(' ',1)
        publish=datetime.strptime(stamp.strip(),'%Y-%m-%d %H:%M').replace(tzinfo=TIMEZONE)
        end=datetime.strptime(right.strip(),'%Y-%m-%d %H:%M').replace(tzinfo=TIMEZONE)
        changed=await db.v47_set_exam_window(int(identifier),publish,end)
    except (ValueError,TypeError): changed=False
    if not changed:
        await update.effective_message.reply_text('تعذر الحفظ. تحقق من رقم الامتحان وتاريخ مستقبلي، وأنه لم يبدأ للطلاب.\nالصيغة:\n/exam_window 12 2026-09-10 11:00 | 2026-09-11 11:00'); return
    await update.effective_message.reply_text(f'✅ حُفظ موعد النشر {publish:%Y-%m-%d %H:%M} والانتهاء {end:%Y-%m-%d %H:%M} بتوقيت بغداد.')


async def v47_window_notices_job(context):
    for row in await db.v47_late_exam_notices():
        try:
            await context.bot.send_message(OWNER_CHAT_ID or row['created_by'],
                f"🗓 يحتاج الامتحان «{row['title']}» إلى موعد نشر وانتهاء لأن موعده الأصلي مضى قبل إضافة الأسئلة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🗓 تحديد الموعدين',callback_data=f"v47_window|{row['id']}")]]))
            await db.v47_mark_window_notice(row['id'])
        except TelegramError: logger.exception('Exam window notification failed: %s',row['id'])


_v47_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ''; uid=query.from_user.id
    if data=='v47_reset':
        if is_admin(uid): await query.answer('هذا الخيار خاص بالطلاب.'); return
        student=await get_student(uid)
        if not student: await query.answer('سجل عبر /start أولاً.',show_alert=True); return
        context.user_data['v47_reset_requested']=datetime.now(TIMEZONE)
        await query.answer()
        await query.edit_message_text('⚠️ تأكيد إعادة تعيين معلوماتي بالكامل\n\nسيُحذف الاسم والمدرسة والمعدل والمسار والتحاضير والتسليمات والتقدم والمراجعات السابقة.\nيبقى رصيد XP والإنذارات وربط ولي الأمر محفوظاً.\n\nهل تؤكد؟',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ نعم، إعادة التعيين',callback_data='v47_reset_confirm')],
                [InlineKeyboardButton('❌ إلغاء',callback_data='v47_reset_cancel')]])); return
    if data=='v47_reset_cancel':
        context.user_data.pop('v47_reset_requested',None)
        await query.answer(); await query.edit_message_text('تم إلغاء إعادة التعيين.',reply_markup=main_menu()); return
    if data.startswith('v47_window|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        try: identifier=int(data.split('|')[1])
        except ValueError: await query.answer('رقم غير صحيح.'); return
        await v47_window_help(query,identifier); return
    student=await get_student(uid) if not is_admin(uid) else None
    if data=='v47_chapters':
        await query.answer()
        await query.edit_message_text('📚 اختر الفصل الذي تبدأ منه:',reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(f'الفصل {n}',callback_data=f'onboardtrack|{n}')] for n in range(1,6)])); return
    if data.startswith('onboardtrack|') and student:
        choice=data.split('|')[1]
        if choice=='course':
            if student.get('reset_pending') or int(student.get('onboarding_version') or 0)<19:
                await v47_finish_track(query,'course'); return
            return await _v47_previous_button_handler(update,context)
        if not choice.isdigit() or int(choice) not in range(1,6): await query.answer('فصل غير صحيح.'); return
        chapter=int(choice); groups=CHAPTER_PREPARATION_DISTRIBUTION.get(chapter,[])
        await query.answer()
        await query.edit_message_text(f'📘 الفصل {chapter}: اختر أول مجموعة محاضرات تريد أن تبدأ منها.\nالمحاضرات السابقة لن تُحسب عليك كمطلوبات.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('المحاضرات '+', '.join(map(str,nums)),callback_data=f'v47_startprep|{chapter}|{i}')] for i,nums in enumerate(groups,1)])); return
    if data.startswith('v47_startprep|'):
        try:
            _,ch,number=data.split('|'); chapter=int(ch); prep=int(number)
            db.v47_plan_rows(chapter,prep,datetime.now(TIMEZONE).date())
        except (ValueError,TypeError): await query.answer('مجموعة المحاضرات غير صحيحة.',show_alert=True); return
        if student and not student.get('reset_pending') and int(student.get('onboarding_version') or 0)>=19:
            context.user_data['v47_track_choice']=(chapter,prep,datetime.now(TIMEZONE))
            await query.answer()
            numbers=', '.join(map(str,CHAPTER_PREPARATION_DISTRIBUTION[chapter][prep-1]))
            await query.edit_message_text(f'تأكيد تغيير المسار إلى الفصل {chapter} ابتداءً من المحاضرات {numbers}. سيُعاد تقدم هذا الفصل وما بعده. يبقى XP محفوظاً.',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ تأكيد',callback_data=f'v47_change_confirm|{chapter}|{prep}')],[back_menu()]])); return
        await v47_finish_track(query,'chapter',chapter,prep); return
    if data.startswith('v47_change_confirm|'):
        confirmed=context.user_data.pop('v47_track_choice',None)
        if not confirmed or (datetime.now(TIMEZONE)-confirmed[2]).total_seconds()>600 or data!=f'v47_change_confirm|{confirmed[0]}|{confirmed[1]}':
            await query.answer('اختر الفصل ومجموعة المحاضرات من جديد لتأكيد التغيير.',show_alert=True); return
        if not student or student.get('reset_pending'): await query.answer('أكمل التسجيل أولاً.'); return
        try:
            _,ch,number=data.split('|'); chapter=int(ch); prep=int(number)
            plan=db.v47_plan_rows(chapter,prep,datetime.now(TIMEZONE).date())
        except (ValueError,TypeError): await query.answer('مجموعة المحاضرات غير صحيحة.'); return
        result=await db.v37_request_track_change(uid,'chapter',chapter,datetime.now(TIMEZONE).date(),plan)
        await query.answer()
        message=('✅ تم تغيير المسار من مجموعة المحاضرات المحددة. ما قبلها لا يسبب إنذارات أو أقفالاً.'
            if result['status']=='ok' else '⏳ استُنفدت التغييرات الثلاثة؛ طلب المسار ومجموعة المحاضرات المحددة ينتظر موافقة الإدارة.'
            if result['status']=='pending' else 'تعذر تغيير المسار.')
        if result['status']=='pending' and OWNER_CHAT_ID:
            await context.bot.send_message(OWNER_CHAT_ID,f'طلب تغيير مسار للطالب {uid}: الفصل {chapter}، مجموعة المحاضرات {prep}.',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('مراجعة الطلب',callback_data='track_change_requests')]]))
        await query.edit_message_text(message,reply_markup=main_menu()); return
    if student and student.get('reset_pending'):
        await query.answer('أكمل إعادة تسجيل معلوماتك واختيار المسار. للبدء مجدداً أرسل /start.',show_alert=True); return
    if data.startswith('examopen|') and student:
        try: identifier=int(data.split('|')[1])
        except ValueError: await query.answer('رقم غير صحيح.'); return
        await query.answer(); await show_task(query,context,identifier); return
    if data.startswith('chapter_exam|') and student:
        try: chapter=int(data.split('|')[1])
        except ValueError: await query.answer('فصل غير صحيح.'); return
        await query.answer(); await v42_bank_chapter(query,chapter); return
    if data=='exams_menu' and student and student.get('study_track')=='chapter':
        await query.answer()
        await query.edit_message_text('📝 الامتحانات\nامتحان محاضراتك يفتح بعد إكمالها وموافقة ولي الأمر، وبنك الامتحانات مرتب حسب الفصل والمحاضرة.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 امتحاناتي المطلوبة',callback_data='tasks|exam|normal')],
                [InlineKeyboardButton('📚 بنك امتحانات الفصول',callback_data='v42_exam_bank')],[back_menu()]])); return
    return await _v47_previous_button_handler(update,context)


_v47_previous_show_task=show_task
async def show_task(query,context,task_id):
    task=await get_task(task_id)
    if not is_admin(query.from_user.id):
        student=await get_student(query.from_user.id)
        if not student or not student.get('approved') or student.get('reset_pending') or not student.get('parent_chat_id'):
            await query.answer('هذه الخدمة تحتاج حساباً مفعلاً وربط ولي الأمر.',show_alert=True);return
        if not await is_channel_member(context.bot,query.from_user.id):
            await query.answer('اشترك بالقناة المطلوبة أولاً.',show_alert=True);return
        if task and task.get('closed'):
            effective=await effective_task_deadline(task_id,query.from_user.id)
            if effective and effective.get('assigned') and effective['deadline']>datetime.now(TIMEZONE) and effective['deadline']<=task['deadline']:
                await query.answer('أغلقت الإدارة هذه المهمة. اطلب إعادة فتحها أو تمديدها.',show_alert=True);return
    # The legacy v45 wrapper applied exam-only eligibility checks even to homework.
    if task and task['kind']=='homework': return await _v45_previous_show_task(query,context,task_id)
    if task and task.get('optional_practice') and not is_admin(query.from_user.id):
        effective=await effective_task_deadline(task_id,query.from_user.id)
        if not effective or not effective.get('assigned'): await query.answer('هذا الامتحان ليس ضمن حسابك.',show_alert=True); return
        if effective['deadline']<=datetime.now(TIMEZONE) and not effective.get('submitted'):
            await query.edit_message_text('انتهى وقت المحاولة الاختيارية. لا إنذار عليها ولا تمنع تحضيرك. يمكنك استعمال التمديد.',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🎁 تمديد مجاني أسبوعي',callback_data=f'freeextend|{task_id}')],[back_menu()]])); return
        return await _v45_previous_show_task(query,context,task_id)
    return await _v47_previous_show_task(query,context,task_id)


_v47_previous_post_init=post_init
async def post_init(app):
    await _v47_previous_post_init(app)
    app.job_queue.run_repeating(v47_window_notices_job,60,first=20,name='v47_window_notices')


# ========================= v48 stable educational release =========================

_v48_previous_main_menu=main_menu
def main_menu(admin=False):
    keyboard=_v48_previous_main_menu(admin)
    rows=[list(row) for row in keyboard.inline_keyboard]
    callback='v48_quick_admin' if admin else 'v48_quick_reviews'
    if not any(button.callback_data==callback for row in rows for button in row):
        label='⚡ ادارة اختبارات المراجعة' if admin else '⚡ اختبارات المراجعة السريعة'
        insert_at=len(rows)-1 if not admin and rows else len(rows)
        rows.insert(insert_at,[InlineKeyboardButton(label,callback_data=callback)])
    return InlineKeyboardMarkup(rows)


async def v48_window_begin(query,context,definition_id):
    row=await db.v31_exam_definition_for_admin(int(definition_id))
    if not row or row.get('target_scope')!='course' or row.get('deleted_at'):
        await query.answer('الامتحان غير موجود او ليس امتحان دورة.',show_alert=True); return
    context.user_data['v48_window']={'definition_id':int(definition_id),'part':'s'}
    await query.answer()
    await v48_window_date_menu(query,context,'s')


async def v48_window_date_menu(query,context,part):
    state=context.user_data.get('v48_window') or {}
    if state.get('part')!=part: state['part']=part
    today=datetime.now(TIMEZONE).date(); buttons=[]
    for offset in range(8):
        day=today+timedelta(days=offset)
        label=('اليوم' if offset==0 else 'غدا' if offset==1 else day.strftime('%d/%m'))
        buttons.append(InlineKeyboardButton(label,callback_data=f"v48wd|{state['definition_id']}|{part}|{offset}"))
    rows=[buttons[i:i+2] for i in range(0,len(buttons),2)]
    rows.append([InlineKeyboardButton('الغاء',callback_data=f"v43_admin_exam|{state['definition_id']}")])
    title='موعد بداية نشر الامتحان' if part=='s' else 'موعد انتهاء استلام الاجوبة'
    await query.edit_message_text(f'🗓 {title}\n\nاختر اليوم بتوقيت بغداد:',reply_markup=InlineKeyboardMarkup(rows))


async def v48_window_hour_menu(query,context,part):
    state=context.user_data['v48_window']; rows=[]
    hours=[InlineKeyboardButton(f'{hour:02d}:00',callback_data=f"v48wh|{state['definition_id']}|{part}|{hour}") for hour in range(24)]
    for index in range(0,24,4): rows.append(hours[index:index+4])
    rows.append([InlineKeyboardButton('◀️ اليوم',callback_data=f"v48wback|{state['definition_id']}|{part}")])
    await query.edit_message_text('⏰ اختر الساعة بتوقيت بغداد:',reply_markup=InlineKeyboardMarkup(rows))


async def v48_window_minute_menu(query,context,part):
    state=context.user_data['v48_window']; hour=state[f'{part}_hour']
    rows=[[InlineKeyboardButton(f'{hour:02d}:{minute:02d}',callback_data=f"v48wm|{state['definition_id']}|{part}|{minute}") for minute in (0,15)],
          [InlineKeyboardButton(f'{hour:02d}:{minute:02d}',callback_data=f"v48wm|{state['definition_id']}|{part}|{minute}") for minute in (30,45)]]
    rows.append([InlineKeyboardButton('◀️ الساعة',callback_data=f"v48wday|{state['definition_id']}|{part}")])
    await query.edit_message_text('⏱ اختر الدقائق:',reply_markup=InlineKeyboardMarkup(rows))


async def v48_admin_quick_menu(query):
    catalog=await db.v48_quick_review_catalog(); total=sum(int(r['question_count']) for r in catalog)
    rows=[[InlineKeyboardButton('➕ اضافة سؤال جديد',callback_data='v48qadd')]]
    for item in catalog:
        rows.append([InlineKeyboardButton(f"ف{item['chapter']} - تحضير {item['prep_no']} | {item['question_count']} سؤال",callback_data=f"v48qlist|{item['chapter']}|{item['prep_no']}")])
    rows.append([back_menu()])
    await query.edit_message_text(f'⚡ ادارة اختبارات المراجعة السريعة\n{DIV}\nالاسئلة الفعالة: {total}\n\nيدعم السؤال والجواب: نص او صورة او PDF.',reply_markup=InlineKeyboardMarkup(rows))


async def v48_quick_student_menu(query):
    catalog=await db.v48_quick_review_catalog(query.from_user.id)
    chapters=sorted({int(r['chapter']) for r in catalog}); rows=[]
    for chapter in chapters:
        count=sum(int(r['question_count']) for r in catalog if int(r['chapter'])==chapter)
        rows.append([InlineKeyboardButton(f'الفصل {chapter} | {count} سؤال',callback_data=f'v48qsch|{chapter}')])
    if not rows: text='📭 لم يضف الاستاذ اسئلة مراجعة سريعة لمسارك بعد.'
    else: text='⚡ اختبارات المراجعة السريعة\nاختر الفصل، ثم التحضير. اكشف الجواب بعد ان تحاول تذكره بنفسك.'
    rows.append([InlineKeyboardButton('◀️ المراجعة الملكية',callback_data='royal_review_menu'),back_menu()])
    await query.edit_message_text(text,reply_markup=InlineKeyboardMarkup(rows))


async def v48_quick_student_chapter(query,chapter):
    catalog=[r for r in await db.v48_quick_review_catalog(query.from_user.id) if int(r['chapter'])==int(chapter)]
    rows=[[InlineKeyboardButton(f"تحضير {r['prep_no']} | {r['question_count']} سؤال",callback_data=f"v48qsp|{chapter}|{r['prep_no']}")] for r in catalog]
    rows.append([InlineKeyboardButton('◀️ الفصول',callback_data='v48_quick_reviews'),back_menu()])
    await query.edit_message_text(f'⚡ الفصل {chapter}\nاختر التحضير:',reply_markup=InlineKeyboardMarkup(rows))


async def v48_quick_student_prep(query,chapter,prep_no):
    questions=await db.v48_quick_review_questions(chapter,prep_no)
    rows=[[InlineKeyboardButton(f'سؤال تنشيط الذاكرة {index}',callback_data=f"v48qopen|{row['id']}")] for index,row in enumerate(questions,1)]
    rows.append([InlineKeyboardButton('◀️ التحاضير',callback_data=f'v48qsch|{chapter}'),back_menu()])
    await query.edit_message_text(f'⚡ الفصل {chapter} - تحضير {prep_no}\nاختر سؤالا وحاول الاجابة قبل كشف الحل:',reply_markup=InlineKeyboardMarkup(rows))


async def _v48_send_quick_payload(bot,chat_id,payload_type,file_id,text,reply_markup=None,title=None):
    caption=bold(((title+'\n') if title else '')+str(text or '')) or None
    if payload_type=='photo': return await bot.send_photo(chat_id,file_id,caption=caption,parse_mode=ParseMode.HTML if caption else None,reply_markup=reply_markup)
    if payload_type=='document': return await bot.send_document(chat_id,file_id,caption=caption,parse_mode=ParseMode.HTML if caption else None,reply_markup=reply_markup)
    return await bot.send_message(chat_id,bold(((title+'\n') if title else '')+str(text or '')),parse_mode=ParseMode.HTML,reply_markup=reply_markup)


async def v48_capture_quick_review(update,context):
    state=context.user_data.get('v48_quick_add')
    if not state or not is_admin(update.effective_user.id): return False
    msg=update.effective_message; payload_type,file_id,content=message_payload(msg)
    if payload_type not in ('text','photo','document'):
        await msg.reply_text('ارسل نصا او صورة او ملف PDF فقط.'); return True
    if payload_type=='document' and (msg.document.mime_type or '').lower()!='application/pdf':
        await msg.reply_text('الملفات المدعومة هنا PDF فقط.'); return True
    item={'payload_type':payload_type,'file_id':file_id,'text':content or ''}
    if state['step']=='question':
        state['question']=item; state['step']='answer'
        await msg.reply_text('✅ تم حفظ السؤال مؤقتا. الان ارسل الاجابة كنص او صورة او PDF.'); return True
    row=await db.v48_add_quick_review_question(state['chapter'],state['prep_no'],
        state['question']['payload_type'],state['question']['file_id'],state['question']['text'],
        item['payload_type'],item['file_id'],item['text'],update.effective_user.id)
    context.user_data.pop('v48_quick_add',None)
    if not row: await msg.reply_text('تعذر حفظ السؤال. اعد المحاولة من لوحة الادارة.'); return True
    await msg.reply_text(f"✅ حفظ السؤال رقم {row['id']} في الفصل {row['chapter']} - تحضير {row['prep_no']}.",reply_markup=main_menu(True)); return True


async def _v48_deliver_submission_row(bot,row):
    """Deliver once, then register; a sent Telegram message is retained for DB retry."""
    sent_id=row.get('delivered_message_id')
    caption=bold(f"📥 {row['title']}\n👤 الطالب: {row['full_name']}\n🆔 {row['user_id']}\n🔐 رقم التسليم: {row['id']}")
    if not sent_id:
        kwargs={'chat_id':row['destination_chat_id'],'caption':caption,'parse_mode':ParseMode.HTML,
                'message_thread_id':row.get('destination_thread_id') or None}
        try:
            if row['payload_type']=='photo': sent=await bot.send_photo(photo=row['file_id'],**kwargs)
            elif row['payload_type']=='document': sent=await bot.send_document(document=row['file_id'],**kwargs)
            else: sent=await bot.send_video(video=row['file_id'],**kwargs)
        except BadRequest:
            if not kwargs.get('message_thread_id'): raise
            kwargs['message_thread_id']=None
            if row['payload_type']=='photo': sent=await bot.send_photo(photo=row['file_id'],**kwargs)
            elif row['payload_type']=='document': sent=await bot.send_document(document=row['file_id'],**kwargs)
            else: sent=await bot.send_video(video=row['file_id'],**kwargs)
            row={**dict(row),'destination_thread_id':None}
        marked=await db.v48_mark_delivery_sent(row['id'],sent.message_id,row.get('destination_thread_id'))
        if not marked:
            try: await bot.delete_message(row['destination_chat_id'],sent.message_id)
            except TelegramError: pass
            raise RuntimeError('delivery row disappeared before Telegram message was recorded')
        row={**dict(row),**dict(marked)}; sent_id=sent.message_id
    result=await record_submission(row['task_id'],row['user_id'],row['student_message_id'],
        row['file_unique_id'],row.get('media_group_id'))
    registered=await db.v48_delivery_submission_registered(row['id'])
    if result in ('added','replaced','album_part') or registered:
        await add_submission_review_message(row['destination_chat_id'],sent_id,row['task_id'],row['user_id'])
        await db.v48_finish_submission_delivery(row['id'],'delivered')
        return 'delivered' if registered and result not in ('added','replaced','album_part') else result
    await db.v48_finish_submission_delivery(row['id'],'rejected',result)
    try: await bot.delete_message(row['destination_chat_id'],sent_id)
    except TelegramError: pass
    return result


async def _v49_delete_private_answer(bot,row):
    """Delete only the student's private media after its group copy is durable."""
    if not DELETE_STUDENT_ANSWER_AFTER_DELIVERY or not row: return 'disabled'
    delivery_id=int(row.get('id') or 0); user_id=int(row.get('user_id') or 0)
    message_id=int(row.get('student_message_id') or 0)
    if not delivery_id or not user_id or not message_id: return 'invalid'
    status='deleted'; error=None
    try:
        await bot.delete_message(chat_id=user_id,message_id=message_id)
    except BadRequest as exc:
        message=str(exc).lower()
        if 'message to delete not found' in message or 'message_id_invalid' in message:
            status='deleted'
        elif "can't be deleted" in message or 'cannot be deleted' in message:
            status='expired'; error=str(exc)
        else:
            status='failed'; error=str(exc)
    except TelegramError as exc:
        status='failed'; error=str(exc)
    try: await db.v49_mark_answer_message_delete(delivery_id,status,error)
    except Exception: logger.exception('Could not persist private answer cleanup state: %s',delivery_id)
    return status


async def receive_submission(update: Update,context: ContextTypes.DEFAULT_TYPE):
    task_id=context.user_data.get('waiting_submission')
    if not task_id: return False
    student=await get_student(update.effective_user.id)
    if not student or not student.get('approved') or not student.get('parent_chat_id') or not await is_channel_member(context.bot,update.effective_user.id):
        context.user_data.pop('waiting_submission',None)
        await update.effective_message.reply_text(bold('🔒 لا يمكنك التسليم: يجب تفعيل الحساب وربط ولي الامر والاشتراك بالقناة.'),parse_mode=ParseMode.HTML); return True
    task=await get_task(task_id); effective=await effective_task_deadline(task_id,update.effective_user.id) if task else None
    if not task or not effective or not effective.get('assigned') or datetime.now(TIMEZONE)>=effective['deadline']:
        context.user_data.pop('waiting_submission',None)
        await update.effective_message.reply_text(bold('⏰ انتهى وقت التسليم. استخدم التمديد او اطلب موافقة الاستاذ ثم اعد الارسال.'),parse_mode=ParseMode.HTML); return True
    msg=update.effective_message; media=msg.document or (msg.photo[-1] if msg.photo else None) or msg.video
    if not media:
        await msg.reply_text(bold('⚠️ ارسل صورة او ملف PDF او فيديو كحل.'),parse_mode=ParseMode.HTML); return True
    destination=(HOMEWORK_SUBMISSIONS_CHAT_ID if task['kind']=='homework' else EXAM_SUBMISSIONS_CHAT_ID) or OWNER_CHAT_ID
    if not destination:
        await msg.reply_text(bold('⚠️ وجهة استلام الحلول غير مضبوطة. لم يسجل الحل، تواصل مع الادارة.'),parse_mode=ParseMode.HTML); return True
    payload_type='document' if msg.document else 'photo' if msg.photo else 'video'
    staged=await db.v48_stage_submission_delivery(task_id,update.effective_user.id,msg.message_id,payload_type,
        media.file_id,media.file_unique_id,msg.media_group_id,destination,None)
    status=staged.get('status')
    if status=='delivered':
        context.user_data.pop('waiting_submission',None)
        await msg.reply_text('✅ هذا الحل مسجل وواصل الى الاستاذ مسبقا.')
        await _v49_delete_private_answer(context.bot,staged.get('delivery')); return True
    if status in ('expired','not_allowed'):
        context.user_data.pop('waiting_submission',None)
        await msg.reply_text('⏰ انتهت صلاحية التسليم او تغيرت حالة الامتحان. افتحه من جديد بعد التمديد.'); return True
    if status=='duplicate':
        await msg.reply_text('🚫 هذا الملف مسجل كتسليم سابق لطالب اخر. ارسل ملف اجابتك الاصلي.'); return True
    # Topic creation is a Telegram network operation. The durable row must exist first;
    # if the forum/topic is unavailable, deliver to the configured chat without a topic.
    thread_id=None
    try: thread_id=await ensure_student_topic(context.bot,student,destination)
    except Exception as exc: logger.warning('Student topic unavailable; using destination root: %s',exc)
    if thread_id:
        staged=await db.v48_stage_submission_delivery(task_id,update.effective_user.id,msg.message_id,payload_type,
            media.file_id,media.file_unique_id,msg.media_group_id,destination,thread_id)
    row={**dict(staged['delivery']),'title':task['title'],'kind':task['kind'],'full_name':student['full_name'],'parent_chat_id':student.get('parent_chat_id')}
    try:
        result=await _v48_deliver_submission_row(context.bot,row)
    except Exception as exc:
        logger.exception('Durable submission delivery queued: task=%s user=%s',task_id,update.effective_user.id)
        await db.v48_mark_delivery_retry(row['id'],exc,SUBMISSION_RETRY_SECONDS)
        await msg.reply_text(bold('⏳ حفظ البوت الحل في طابور مضمون، لكنه لم يصل الى الاستاذ بعد. سيعيد الارسال تلقائيا ويبلغك عند نجاحه.'),parse_mode=ParseMode.HTML)
        return True
    if result not in ('added','replaced','album_part','delivered'):
        context.user_data.pop('waiting_submission',None)
        text='⏰ انتهى الوقت اثناء الرفع. لم يسجل الحل.' if result=='expired' else '🔒 لم يسجل الحل بسبب تغير صلاحية الامتحان.'
        await msg.reply_text(text); return True
    first=result=='added'; same_album=result=='album_part'
    speed_reward=await db.v54_exam_speed_reward(task_id,update.effective_user.id) if task['kind']=='exam' else None
    if first and student.get('parent_chat_id'):
        noun='الواجب' if task['kind']=='homework' else 'الامتحان'
        try: await context.bot.send_message(student['parent_chat_id'],bold(f"✅ تم تسجيل ووصول {noun}\n👤 الطالب: {student['full_name']}\n📌 {task['title']}"),parse_mode=ParseMode.HTML)
        except TelegramError: pass
    if msg.media_group_id:
        token=time.monotonic(); state=context.user_data.get('submission_album')
        if not state or state.get('id')!=msg.media_group_id or state.get('task_id')!=task_id:
            context.user_data['submission_album']={'id':msg.media_group_id,'task_id':task_id,'added':first,'token':token}
        else: state['token']=token
        context.job_queue.run_once(finalize_album_submission,8,data={'user_id':update.effective_user.id,'media_group_id':msg.media_group_id,'token':token})
    elif not same_album:
        context.user_data.pop('waiting_submission',None)
        kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ الاجابة صحيحة',callback_data=f'submissionok|{task_id}'),InlineKeyboardButton('🔄 تغيير الاجابة',callback_data=f'retrysubmission|{task_id}')],[InlineKeyboardButton('🎬 فتح المحاضرات التالية',callback_data='today_prep')]])
        bonus=(f"\n🏆 ترتيبك {speed_reward['submission_rank']} وحصلت على +{speed_reward['bonus_xp']} XP سرعة." if speed_reward else '')
        await msg.reply_text(bold('✅ تم تسجيل الحل ووصل الى الاستاذ بنجاح.\n⭐ احتسبت نقاط المهمة مرة واحدة.'+bonus),parse_mode=ParseMode.HTML,reply_markup=kb)
    await _v49_delete_private_answer(context.bot,row)
    return True


async def v48_submission_delivery_job(context):
    for row in await db.v48_pending_submission_deliveries(50,SUBMISSION_MAX_RETRIES):
        try:
            result=await _v48_deliver_submission_row(context.bot,row)
            if result in ('added','replaced','album_part','delivered'):
                speed=await db.v54_exam_speed_reward(row['task_id'],row['user_id']) if row.get('kind')=='exam' else None
                bonus=(f"\n🏆 ترتيبك {speed['submission_rank']} وحصلت على +{speed['bonus_xp']} XP سرعة." if speed else '')
                try: await context.bot.send_message(row['user_id'],bold(f"✅ وصل حلك الى الاستاذ وتم تسجيله بنجاح.\n📌 {row['title']}"+bonus),parse_mode=ParseMode.HTML)
                except TelegramError: pass
                await _v49_delete_private_answer(context.bot,row)
            elif result in ('expired','not_allowed'):
                try: await context.bot.send_message(row['user_id'],bold(f"⚠️ لم يسجل الحل المؤجل لان صلاحية الامتحان انتهت.\n📌 {row['title']}\nاطلب تمديدا ثم ارسل الحل من جديد."),parse_mode=ParseMode.HTML)
                except TelegramError: pass
        except Exception as exc:
            logger.exception('Submission delivery retry failed: %s',row['id'])
            retry=await db.v48_mark_delivery_retry(row['id'],exc,SUBMISSION_RETRY_SECONDS)
            if retry and int(retry.get('attempts') or 0)>=SUBMISSION_MAX_RETRIES and not retry.get('admin_alerted_at') and OWNER_CHAT_ID:
                try:
                    await context.bot.send_message(OWNER_CHAT_ID,bold(f"🚨 تعذر ايصال حل بعد {retry['attempts']} محاولة\n📌 {row['title']}\n👤 {row['full_name']} - {row['user_id']}\n\nتحقق من EXAM_SUBMISSIONS_CHAT_ID وصلاحيات البوت. سيستمر البوت بالمحاولة تلقائيا."),parse_mode=ParseMode.HTML)
                    await db.v48_mark_delivery_admin_alerted(row['id'])
                except TelegramError: pass


async def v31_close_tasks_job(context):
    """Close current obligations once; individual linked exams never spam the admin chat."""
    for task in await due_tasks():
        warned,removed=await issue_missing_task_warnings(context,task)
        if await task_has_active_extensions(task['id']): continue
        if not await close_task(task['id']): continue
        individual=str(task.get('target_scope') or '').startswith('student:')
        if not individual:
            try:
                await context.bot.send_message(task['chat_id'],bold(f"⏰ تم اغلاق {task['title']}.\n⚠️ الانذارات: {warned}\n🚫 المحظورون: {removed}"),parse_mode=ParseMode.HTML,message_thread_id=task.get('thread_id') or None)
            except TelegramError: pass
            if task['kind']=='exam' and not task.get('questions_released'):
                try: await release_exam_questions(context,task)
                except TelegramError as exc: logger.warning('Exam question release failed for %s: %s',task['id'],exc)
            await announce_champions(context,task['id'])
    for task in await recently_closed_tasks_for_warning_recovery(): await issue_missing_task_warnings(context,task)
    for task in await unreleased_closed_exams():
        try: await release_exam_questions(context,task)
        except TelegramError as exc: logger.warning('Exam question release recovery failed for %s: %s',task['id'],exc)


_v48_previous_private_messages=private_messages
async def private_messages(update,context):
    if await v48_capture_quick_review(update,context): return
    return await _v48_previous_private_messages(update,context)


_v48_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ''; uid=query.from_user.id
    if data=='menu':
        context.user_data.pop('v48_quick_add',None); context.user_data.pop('v48_window',None)
    if data=='v48_quick_admin':
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        await query.answer(); await v48_admin_quick_menu(query); return
    if data=='v48qadd':
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        rows=[[InlineKeyboardButton(f'الفصل {n}',callback_data=f'v48qaddc|{n}')] for n in range(1,6)]+[[back_menu()]]
        await query.answer(); await query.edit_message_text('اختر فصل سؤال المراجعة السريعة:',reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith('v48qaddc|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        chapter=int(data.split('|')[1]); groups=CHAPTER_PREPARATION_DISTRIBUTION.get(chapter,[])
        rows=[[InlineKeyboardButton(f"تحضير {i} - محاضرات {', '.join(map(str,lectures))}",callback_data=f'v48qaddp|{chapter}|{i}')] for i,lectures in enumerate(groups,1)]
        rows.append([InlineKeyboardButton('◀️ الفصول',callback_data='v48qadd'),back_menu()])
        await query.answer(); await query.edit_message_text(f'اختر تحضير الفصل {chapter}:',reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith('v48qaddp|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        _,chapter,prep=data.split('|'); context.user_data['v48_quick_add']={'chapter':int(chapter),'prep_no':int(prep),'step':'question'}
        await query.answer(); await query.edit_message_text('ارسل الان سؤال تنشيط الذاكرة كنص او صورة او ملف PDF.\nيمكنك كتابة وصف داخل تعليق الصورة او الملف.'); return
    if data.startswith('v48qlist|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        _,chapter,prep=data.split('|'); questions=await db.v48_quick_review_questions(int(chapter),int(prep))
        rows=[[InlineKeyboardButton(f'🗑 حذف السؤال {index}',callback_data=f"v48qdelask|{row['id']}")] for index,row in enumerate(questions,1)]
        rows.append([InlineKeyboardButton('◀️ بنك الاسئلة',callback_data='v48_quick_admin'),back_menu()])
        await query.answer(); await query.edit_message_text(f'اسئلة الفصل {chapter} - تحضير {prep}: {len(questions)}',reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith('v48qdelask|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        question_id=int(data.split('|')[1]); await query.answer(); await query.edit_message_text('هل تريد حذف هذا السؤال من واجهة الطلاب؟',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ نعم',callback_data=f'v48qdelok|{question_id}')],[InlineKeyboardButton('❌ الغاء',callback_data='v48_quick_admin')]])); return
    if data.startswith('v48qdelok|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        row=await db.v48_retire_quick_review_question(int(data.split('|')[1]),uid)
        await query.answer('تم الحذف' if row else 'محذوف مسبقا',show_alert=True); await v48_admin_quick_menu(query); return
    if data=='v48_quick_reviews':
        student=await get_student(uid)
        if not student or not student.get('approved') or student.get('reset_pending'):
            await query.answer('اكمل التسجيل والتفعيل اولا.',show_alert=True); return
        await query.answer(); await v48_quick_student_menu(query); return
    if data.startswith('v48qsch|'):
        await query.answer(); await v48_quick_student_chapter(query,int(data.split('|')[1])); return
    if data.startswith('v48qsp|'):
        _,chapter,prep=data.split('|'); await query.answer(); await v48_quick_student_prep(query,int(chapter),int(prep)); return
    if data.startswith('v48qopen|'):
        question=await db.v48_quick_review_question(int(data.split('|')[1]),uid)
        if not question: await query.answer('السؤال غير متاح لمسارك.',show_alert=True); return
        await query.answer(); markup=InlineKeyboardMarkup([[InlineKeyboardButton('👁 كشف الاجابة',callback_data=f"v48qanswer|{question['id']}")],[InlineKeyboardButton('◀️ الاسئلة',callback_data=f"v48qsp|{question['chapter']}|{question['prep_no']}")]])
        await _v48_send_quick_payload(context.bot,uid,question['question_payload_type'],question.get('question_file_id'),question.get('question_text'),markup,'⚡ سؤال تنشيط الذاكرة')
        try: await query.edit_message_text('✅ تم ارسال السؤال. حاول استرجاع الجواب ثم اضغط كشف الاجابة.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('◀️ الاسئلة',callback_data=f"v48qsp|{question['chapter']}|{question['prep_no']}")],[back_menu()]]))
        except BadRequest: pass
        return
    if data.startswith('v48qanswer|'):
        question=await db.v48_reveal_quick_review_answer(int(data.split('|')[1]),uid)
        if not question: await query.answer('السؤال غير متاح.',show_alert=True); return
        await query.answer(); markup=InlineKeyboardMarkup([[InlineKeyboardButton('⚡ بقية الاسئلة',callback_data=f"v48qsp|{question['chapter']}|{question['prep_no']}")],[back_menu()]])
        await _v48_send_quick_payload(context.bot,uid,question['answer_payload_type'],question.get('answer_file_id'),question.get('answer_text'),markup,'✅ الاجابة')
        return
    if data.startswith('v47_window|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        await v48_window_begin(query,context,int(data.split('|')[1])); return
    if data.startswith('v48wback|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        _,identifier,part=data.split('|'); state=context.user_data.get('v48_window') or {}
        if int(state.get('definition_id') or 0)!=int(identifier): await query.answer('ابدأ تحديد الموعد من جديد.',show_alert=True); return
        await query.answer(); await v48_window_date_menu(query,context,part); return
    if data.startswith('v48wd|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        _,identifier,part,offset=data.split('|'); state=context.user_data.get('v48_window') or {}
        if int(state.get('definition_id') or 0)!=int(identifier): await query.answer('ابدأ تحديد الموعد من جديد.',show_alert=True); return
        state[f'{part}_date']=datetime.now(TIMEZONE).date()+timedelta(days=int(offset)); state['part']=part
        await query.answer(); await v48_window_hour_menu(query,context,part); return
    if data.startswith('v48wday|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        _,identifier,part=data.split('|'); state=context.user_data.get('v48_window') or {}
        if int(state.get('definition_id') or 0)!=int(identifier) or f'{part}_date' not in state: await query.answer('ابدأ تحديد الموعد من جديد.',show_alert=True); return
        await query.answer(); await v48_window_hour_menu(query,context,part); return
    if data.startswith('v48wh|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        _,identifier,part,hour=data.split('|'); state=context.user_data.get('v48_window') or {}
        if int(state.get('definition_id') or 0)!=int(identifier) or f'{part}_date' not in state: await query.answer('ابدأ تحديد الموعد من جديد.',show_alert=True); return
        state[f'{part}_hour']=int(hour); await query.answer(); await v48_window_minute_menu(query,context,part); return
    if data.startswith('v48wm|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        _,identifier,part,minute=data.split('|'); state=context.user_data.get('v48_window') or {}
        if int(state.get('definition_id') or 0)!=int(identifier) or f'{part}_hour' not in state: await query.answer('ابدأ تحديد الموعد من جديد.',show_alert=True); return
        stamp=datetime.combine(state[f'{part}_date'],datetime.min.time(),tzinfo=TIMEZONE).replace(hour=state[f'{part}_hour'],minute=int(minute))
        if part=='s':
            if stamp<=datetime.now(TIMEZONE): await query.answer('وقت البداية يجب ان يكون في المستقبل.',show_alert=True); await v48_window_date_menu(query,context,'s'); return
            state['start']=stamp; state['part']='e'; await query.answer(); await v48_window_date_menu(query,context,'e'); return
        start=state.get('start')
        if not start or stamp<=start: await query.answer('وقت الانتهاء يجب ان يكون بعد وقت البداية.',show_alert=True); await v48_window_date_menu(query,context,'e'); return
        changed=await db.v47_set_exam_window(int(identifier),start,stamp); context.user_data.pop('v48_window',None)
        if not changed: await query.answer('تعذر الحفظ. ربما بدأ الامتحان او تغيرت حالته.',show_alert=True); return
        await query.answer('تم حفظ الموعدين',show_alert=True)
        await query.edit_message_text(f"✅ تم تحديد الامتحان\n🚀 النشر: {start:%d/%m/%Y %H:%M}\n⏰ الانتهاء: {stamp:%d/%m/%Y %H:%M}\n🕰 التوقيت: بغداد",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 تفاصيل الامتحان',callback_data=f'v43_admin_exam|{identifier}')],[back_menu()]])); return
    if data=='xp_store':
        wallets=await db.v48_student_wallets(uid)
        if wallets:
            active='الدورة' if wallets.get('study_track')=='course' else 'الفصول المستقلة'
            kb=InlineKeyboardMarkup([[InlineKeyboardButton('⏳ تمديد امتحان - 150 XP',callback_data='exams_menu')],[InlineKeyboardButton('🏖 اجازة يوم كامل - 400 XP',callback_data='leave_menu')],[InlineKeyboardButton('⚠️ فك انذار - 500 XP',callback_data='buy_unwarn')],[back_menu()]])
            await query.answer(); await query.edit_message_text(f"⭐ ارصدتك المنفصلة\n{DIV}\n👥 رصيد الدورة: {wallets['course_xp']} XP\n📚 رصيد الفصول المستقلة: {wallets['chapter_xp']} XP\n\nالرصيد المستخدم حاليا: {wallets['xp']} XP - مسار {active}.\nلا ينتقل الرصيد بين المسارين.",reply_markup=kb); return
    return await _v48_previous_button_handler(update,context)


_v48_previous_post_init=post_init
async def post_init(app):
    await _v48_previous_post_init(app)
    retired=await db.v48_retire_stale_exam_obligations(EXAM_ENFORCEMENT_START_DATE)
    if retired.get('tasks') or retired.get('definitions'):
        logger.info('v48 retired stale exam tasks=%s definitions=%s',retired.get('tasks'),retired.get('definitions'))
    configuration=[]
    if _INVALID_INTEGER_ENV:
        configuration.append('قيم رقمية غير صحيحة: '+', '.join(sorted(_INVALID_INTEGER_ENV)))
    if not (EXAM_SUBMISSIONS_CHAT_ID or OWNER_CHAT_ID):
        configuration.append('اضبط EXAM_SUBMISSIONS_CHAT_ID او OWNER_CHAT_ID حتى تصل حلول الامتحانات')
    if configuration:
        logger.warning('v48 configuration: %s',' | '.join(configuration))
        if OWNER_CHAT_ID:
            try: await app.bot.send_message(OWNER_CHAT_ID,bold('⚠️ تنبيه اعدادات v48\n'+"\n".join(f'• {item}' for item in configuration)),parse_mode=ParseMode.HTML)
            except TelegramError: pass
    app.job_queue.run_repeating(v48_submission_delivery_job,30,first=12,name='v48_submission_delivery')


# ========================= v49 Neon-safe runtime and cleanup center =========================

_V49_MAINTENANCE_LOCK=asyncio.Lock()
_V49_ACTIVITY_LAST=0.0
_V49_LEGACY_JOB_NAMES={
    'preparations','personal_preparations','scheduled_tasks','linked_exam_dispatch','exam_engine',
    'exam_parent_readiness','activation_compliance','task_deadlines','exam_reminders',
    'exam_six_hour_reminders','teacher_exam_deadline','study_progress','study_and_progress',
    'exam_notices','gamification','weekly_reports','weekly_parent_reports','v37_notification_delivery',
    'v41_royal_review_reminders','v42_missing_course_exam','v47_window_notices','v48_submission_delivery',
}


async def _v49_run_maintenance_step(context,name,callback):
    try: await callback(context)
    except Exception: logger.exception('v49 maintenance step failed: %s',name)


async def v49_eco_critical_job(context):
    """One consolidated wake-up replaces many 30-60 second Neon polling loops."""
    async with _V49_MAINTENANCE_LOCK:
        steps=(
            ('preparations',publish_preparations_job),
            ('personal preparations',personal_preparations_job),
            ('scheduled tasks',scheduled_tasks_job),
            ('linked exams',linked_exam_dispatch_job),
            ('parent readiness',exam_parent_readiness_job),
            ('deadlines',v31_close_tasks_job),
            ('six hour reminders',exam_reminders_job),
            ('teacher deadline',teacher_exam_deadline_job),
            ('exam notices',v28_notification_job),
            ('weekly reports',weekly_reports_job),
            ('notification delivery',v37_notification_delivery_job),
            ('royal review reminders',v41_review_reminders_job),
            ('missing course exams',v42_missing_exam_job),
            ('late exam windows',v47_window_notices_job),
            ('submission recovery',v48_submission_delivery_job),
        )
        for name,callback in steps: await _v49_run_maintenance_step(context,name,callback)


async def v49_eco_background_job(context):
    async with _V49_MAINTENANCE_LOCK:
        for name,callback in (
            ('activation compliance',activation_compliance_job),
            ('study progress',study_and_progress_job),
            ('gamification',v28_gamification_job),
        ):
            await _v49_run_maintenance_step(context,name,callback)


async def v49_activity_sweep(context):
    """Refresh student-facing due work immediately while active use already woke Neon."""
    global _V49_ACTIVITY_LAST
    now=time.monotonic()
    if now-_V49_ACTIVITY_LAST<NEON_ACTIVITY_SWEEP_SECONDS or _V49_MAINTENANCE_LOCK.locked(): return
    _V49_ACTIVITY_LAST=now
    async with _V49_MAINTENANCE_LOCK:
        for name,callback in (
            ('activity preparations',publish_preparations_job),
            ('activity scheduled tasks',scheduled_tasks_job),
            ('activity linked exams',linked_exam_dispatch_job),
            ('activity submission recovery',v48_submission_delivery_job),
        ):
            await _v49_run_maintenance_step(context,name,callback)


async def v49_cleanup_menu(query):
    stats=await db.v49_answer_cleanup_preview(ANSWER_CLEANUP_LOOKBACK_DAYS)
    text=(f"🧹 مركز تنظيف اجابات الطلاب\n{DIV}\n"
          f"📅 النطاق: اخر {ANSWER_CLEANUP_LOOKBACK_DAYS} ايام\n"
          f"✅ اجابات وصلت للمجموعات: {stats['delivered']}\n"
          f"🗑 رسائل خاصة تنتظر الحذف: {stats['delete_candidates']}\n"
          f"🧾 مراجع ملفات تنتظر التنظيف: {stats['payloads_to_purge']}\n"
          f"🛡 اجابات معلقة محمية من الحذف: {stats['pending_protected']}\n\n"
          "التنظيف لا يحذف اي صورة من مجموعة الامتحانات او الواجبات، ولا يمس الدرجات او XP او الانذارات. "
          "تيليجرام يسمح بحذف الرسائل الخاصة الحديثة فقط؛ لذلك يحذف البوت الاجابات الجديدة تلقائيا بعد ضمان وصولها للمجموعة.")
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('🧹 تنظيف امن الان',callback_data='v49_cleanup_request')],
        [InlineKeyboardButton('📜 سجل عمليات التنظيف',callback_data='v49_cleanup_history')],
        [back_menu()],
    ])
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v49_run_answer_cleanup(query,context):
    candidates=await db.v49_answer_cleanup_candidates(ANSWER_CLEANUP_LOOKBACK_DAYS,500)
    deleted=expired=failed=0; now=datetime.now(TIMEZONE)
    for row in candidates:
        created=row.get('created_at')
        if created and getattr(created,'tzinfo',None) is None: created=created.replace(tzinfo=TIMEZONE)
        if created and now-created.astimezone(TIMEZONE)>timedelta(hours=TELEGRAM_DELETE_LIMIT_HOURS):
            await db.v49_mark_answer_message_delete(row['id'],'expired','Telegram deletion window expired')
            expired+=1; continue
        status='deleted'; error=None
        try: await context.bot.delete_message(chat_id=row['user_id'],message_id=row['student_message_id'])
        except BadRequest as exc:
            message=str(exc).lower()
            if 'message to delete not found' in message or 'message_id_invalid' in message: status='deleted'
            elif "can't be deleted" in message or 'cannot be deleted' in message: status='expired'; error=str(exc)
            else: status='failed'; error=str(exc)
        except TelegramError as exc: status='failed'; error=str(exc)
        await db.v49_mark_answer_message_delete(row['id'],status,error)
        if status=='deleted': deleted+=1
        elif status=='expired': expired+=1
        else: failed+=1
    preview=await db.v49_answer_cleanup_preview(ANSWER_CLEANUP_LOOKBACK_DAYS)
    finish=await db.v49_finish_answer_cleanup(query.from_user.id,ANSWER_CLEANUP_LOOKBACK_DAYS,
        len(candidates),deleted,expired,failed,preview['pending_protected'])
    return {'candidates':len(candidates),'deleted':deleted,'expired':expired,'failed':failed,
            'purged':finish['purged'],'pending':preview['pending_protected'],'audit':finish['audit']}


_v49_previous_main_menu=main_menu
def main_menu(admin=False):
    keyboard=_v49_previous_main_menu(admin); rows=[list(row) for row in keyboard.inline_keyboard]
    if admin and not any(button.callback_data=='v49_cleanup' for row in rows for button in row):
        rows.insert(max(0,len(rows)-1),[
            InlineKeyboardButton('🧹 تنظيف الاجابات',callback_data='v49_cleanup'),
            InlineKeyboardButton('📦 حجم قاعدة البيانات',callback_data='v50_db_storage'),
        ])
    return InlineKeyboardMarkup(rows)


_v49_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ''; uid=query.from_user.id
    if data=='v50_db_storage':
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        await query.answer('جاري حساب الحجم الحقيقي...')
        stats=await db.v50_database_storage()
        await query.edit_message_text(bold(
            f"📦 حجم قاعدة البيانات الحالية\n{DIV}\n"
            f"🗄 القاعدة: {stats['database_name']}\n"
            f"📊 الحجم الكلي: {stats['database_pretty']}\n"
            f"🧪 جداول البوت: {stats['biology_pretty']}\n"
            f"📋 عدد جداول البوت: {stats['biology_tables']}\n\n"
            "Telegram يحتفظ بالصور والفيديوات؛ قاعدة Neon تخزن معرفات الملفات والسجلات فقط، لذلك حذف امتحان لا يساوي حجم الصور المنشورة."),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data=='v49_cleanup':
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        context.user_data.pop('v49_cleanup_confirm',None)
        await query.answer(); await v49_cleanup_menu(query); return
    if data=='v49_cleanup_request':
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        token=secrets.token_urlsafe(8); context.user_data['v49_cleanup_confirm']=(token,datetime.now(TIMEZONE))
        await query.answer(); await query.edit_message_text(bold(
            f"⚠️ تاكيد التنظيف الامن\n{DIV}\n"
            f"سيحاول البوت حذف اجابات اخر {ANSWER_CLEANUP_LOOKBACK_DAYS} ايام من المحادثات الخاصة فقط بعد التاكد من وصولها للمجموعة.\n"
            "لن يحذف الاجابات المعلقة، ولن يحذف نسخ المجموعات او الدرجات او XP او الانذارات.\n\nهل تؤكد؟"),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('✅ نعم، ابدأ التنظيف',callback_data=f'v49_cleanup_confirm|{token}')],
                [InlineKeyboardButton('❌ الغاء',callback_data='v49_cleanup')],
            ])); return
    if data.startswith('v49_cleanup_confirm|'):
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        saved=context.user_data.pop('v49_cleanup_confirm',None); supplied=data.partition('|')[2]
        if not saved or not secrets.compare_digest(str(saved[0]),str(supplied)) or datetime.now(TIMEZONE)-saved[1]>timedelta(minutes=10):
            await query.answer('انتهت صلاحية التاكيد. افتح التنظيف من جديد.',show_alert=True); return
        await query.answer('بدأ التنظيف')
        await query.edit_message_text(bold('⏳ جاري تنظيف الاجابات الامنة...'),parse_mode=ParseMode.HTML)
        try: result=await v49_run_answer_cleanup(query,context)
        except Exception:
            logger.exception('v49 answer cleanup failed')
            await query.edit_message_text(bold('⚠️ توقف التنظيف بسبب مشكلة مؤقتة. لم تُحذف الاجابات المعلقة ويمكن اعادة المحاولة بامان.'),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔄 اعادة المحاولة',callback_data='v49_cleanup')],[back_menu()]])); return
        await query.edit_message_text(bold(
            f"✅ اكتمل التنظيف الامن\n{DIV}\n"
            f"🗑 رسائل خاصة حذفت: {result['deleted']}\n"
            f"⌛ تجاوزت مهلة حذف تيليجرام: {result['expired']}\n"
            f"🔄 تعذر حذفها مؤقتا: {result['failed']}\n"
            f"🧾 مراجع ملفات نظفت: {result['purged']}\n"
            f"🛡 اجابات معلقة بقيت محمية: {result['pending']}\n\n"
            "نسخ مجموعات الامتحانات والواجبات بقيت كما هي."),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🧹 مركز التنظيف',callback_data='v49_cleanup')],[back_menu()]])); return
    if data=='v49_cleanup_history':
        if not is_admin(uid): await query.answer('للادارة فقط.',show_alert=True); return
        history=await db.v49_answer_cleanup_history(10); lines=['📜 سجل تنظيف الاجابات',DIV]
        for row in history:
            stamp=row['created_at'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')
            lines.append(f"• {stamp} | حذف {row['deleted_messages']} | منتهي {row['expired_messages']} | فشل {row['failed_messages']} | تنظيف {row['purged_payloads']}")
        if not history: lines.append('لا توجد عمليات سابقة.')
        await query.answer(); await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('◀️ مركز التنظيف',callback_data='v49_cleanup')],[back_menu()]])); return
    result=await _v49_previous_button_handler(update,context)
    if NEON_ECO_MODE and (data in {'today_prep','exams_menu'} or data.startswith(('tasks|','task|','examopen|','prepopen|'))):
        # Render the requested screen first. Global due-work maintenance runs
        # independently so it can never hold the Telegram callback spinner.
        app=getattr(context,'application',None)
        if app and hasattr(app,'create_task'):
            app.create_task(v49_activity_sweep(context),update=update,name=f'v50-activity-{uid}')
    return result


_v49_previous_post_init=post_init
async def post_init(app):
    await _v49_previous_post_init(app)
    status=await db.v49_installation_status()
    fresh_setup={'status':'existing','preparations':0}
    if status['fresh']: fresh_setup=await db.v49_prepare_fresh_database()
    elif status['first_boot']: await db.v49_mark_installation_initialized()
    if NEON_ECO_MODE:
        removed=0
        for name in _V49_LEGACY_JOB_NAMES:
            for job in app.job_queue.get_jobs_by_name(name): job.schedule_removal(); removed+=1
        app.job_queue.run_repeating(v49_eco_critical_job,NEON_ECO_INTERVAL_SECONDS,first=15,name='v49_neon_eco_critical')
        app.job_queue.run_repeating(v49_eco_background_job,NEON_BACKGROUND_INTERVAL_SECONDS,first=90,name='v49_neon_eco_background')
        logger.info('Neon eco mode enabled: removed=%s critical=%ss background=%ss',removed,
            NEON_ECO_INTERVAL_SECONDS,NEON_BACKGROUND_INTERVAL_SECONDS)
    if OWNER_CHAT_ID and (status['first_boot'] or NEON_ECO_MODE):
        state=('🆕 قاعدة جديدة: تسجيل الطلاب سيبدأ من الصفر.' if status['fresh']
            else f"📚 قاعدة موجودة: {status['students']} طالب، لم يحذف البوت اي بيانات.")
        eco=(f"♻️ وضع Neon الاقتصادي فعال: فحص موحد كل {NEON_ECO_INTERVAL_SECONDS//60} دقيقة."
            if NEON_ECO_MODE else '⚡ وضع Neon المباشر فعال.')
        try: await app.bot.send_message(OWNER_CHAT_ID,bold(
            f"✅ اكتمل تشغيل {BUILD_VERSION}\n{state}\n{eco}\n"
            f"🗓 التحاضير القديمة المؤرشفة بصمت: {fresh_setup.get('preparations',0)}\n"
            "🧹 حذف اجابات الطلاب الخاصة بعد وصولها للمجموعة: مفعل."),parse_mode=ParseMode.HTML)
        except TelegramError: pass


# ========================= v51 final learning experience =========================

from contextvars import ContextVar

_V51_STUDENT_CACHE=ContextVar('v51_student_cache',default=None)
_v51_database_get_student=get_student
_V51_MEMBERSHIP_CACHE={}


async def get_student(user_id):
    """One student SELECT per update, even when legacy routing layers are traversed."""
    cache=_V51_STUDENT_CACHE.get()
    key=int(user_id)
    if cache is not None and key in cache: return cache[key]
    row=await _v51_database_get_student(key)
    if cache is not None: cache[key]=row
    return row


async def _v51_cached_membership(bot,chat_id,user_id):
    if not chat_id: return True
    key=(str(chat_id),int(user_id)); now=time.monotonic(); saved=_V51_MEMBERSHIP_CACHE.get(key)
    if saved and saved[1]>now: return saved[0]
    try:
        member=await bot.get_chat_member(chat_id,user_id)
        allowed=member.status not in (ChatMemberStatus.LEFT,ChatMemberStatus.BANNED)
        _V51_MEMBERSHIP_CACHE[key]=(allowed,now+(300 if allowed else 30))
        return allowed
    except TelegramError:
        # A temporary Telegram outage must not repeatedly slow or lock an
        # already verified student during the same process lifetime.
        if saved: return saved[0]
        _V51_MEMBERSHIP_CACHE[key]=(False,now+15)
        return False


async def is_channel_member(bot,user_id):
    return await _v51_cached_membership(bot,REQUIRED_CHANNEL,user_id)


async def is_group_member(bot,user_id):
    return await _v51_cached_membership(bot,BIOLOGY_GROUP_ID,user_id)


def _v51_neutral_button(text,callback_data=None,**kwargs):
    """Telegram default button, used to keep navigation visually calm."""
    return TelegramInlineKeyboardButton(text,callback_data=callback_data,**kwargs)


def main_menu(admin=False):
    """A compact, colour-coordinated home screen with no duplicated sections."""
    if admin:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton('👥 إدارة الطلبة',callback_data='admin_students',style='primary'),
             InlineKeyboardButton('👪 إدارة أولياء الأمور',callback_data='admin_parents',style='primary')],
            [InlineKeyboardButton('➕ نشر واجب أو امتحان',callback_data='admin_publish',style='success')],
            [_v51_neutral_button('🗓 إدارة التحاضير',callback_data='prep_schedule'),
             _v51_neutral_button('⏳ إدارة الامتحانات',callback_data='v42_admin_exams')],
            [InlineKeyboardButton('⚠️ إدارة الإنذارات',callback_data='v43_warnings',style='primary'),
             InlineKeyboardButton('🔄 طلبات المسارات',callback_data='track_change_requests',style='primary')],
            [InlineKeyboardButton('⚡ أسئلة المراجعة',callback_data='v48_quick_admin',style='primary')],
            [InlineKeyboardButton('🏫 إدارة مراجعة المدرسة',callback_data='v54_school_admin',style='success')],
            [_v51_neutral_button('🧹 تنظيف الإجابات',callback_data='v49_cleanup'),
             _v51_neutral_button('📦 حجم قاعدة البيانات',callback_data='v50_db_storage')],
            [InlineKeyboardButton('🗑 حذف امتحان نهائياً',callback_data='admin_exam_delete_menu',style='danger')],
            [_v51_neutral_button('🔔 الإشعارات',callback_data='notifications'),
             _v51_neutral_button('⚙️ الإعدادات',callback_data='account_settings')],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ مهامي اليومية',callback_data='daily_learning_session',style='success')],
        [InlineKeyboardButton('🧪 تحضير اليوم',callback_data='today_prep',style='primary'),
         InlineKeyboardButton('📝 الامتحانات',callback_data='exams_menu',style='primary')],
        [InlineKeyboardButton('📚 الواجبات',callback_data='tasks|homework',style='primary'),
         InlineKeyboardButton('🎬 المحاضرات',callback_data='playlists',style='primary')],
        [InlineKeyboardButton('👑 المراجعة الملكية',callback_data='royal_review_menu',style='primary'),
         InlineKeyboardButton('📚 تراكمي',callback_data='backlog_auto',style='primary')],
        [InlineKeyboardButton('🏫 مراجعة للمدرسة',callback_data='v54_school_review',style='success')],
        [InlineKeyboardButton('📊 تقدمي',callback_data='v51_progress',style='primary'),
         InlineKeyboardButton('🗓 جدولي الدراسي',callback_data='schedules_menu',style='primary')],
        [_v51_neutral_button('📖 الملازم والملخصات',callback_data='study_resources'),
         _v51_neutral_button('⭐ متجر XP',callback_data='xp_store')],
        [_v51_neutral_button('🔔 الإشعارات',callback_data='notifications'),
         _v51_neutral_button('⚙️ إعدادات الحساب',callback_data='account_settings')],
    ])


def _v51_prep_number(chapter,lecture):
    for prep_no,lectures in enumerate(CHAPTER_PREPARATION_DISTRIBUTION.get(int(chapter),[]),1):
        if int(lecture) in {int(value) for value in lectures}: return prep_no
    return 0


async def v51_menu(query):
    await query.answer()
    admin=is_admin(query.from_user.id)
    student=None if admin else await get_student(query.from_user.id)
    allowed=admin or bool(student and student.get('approved') and not student.get('reset_pending'))
    title='👑 مركز إدارة الأحياء' if admin else '🧪 منصة المجتهد التعليمية'
    note='إدارة مرتبة وسريعة لكل أقسام الدورة.' if admin else 'اختر مهمتك؛ كل قسم في مكان واحد من دون تكرار.'
    await query.edit_message_text(bold(f'{title}\n{DIV}\n{note}'),parse_mode=ParseMode.HTML,
        reply_markup=main_menu(admin) if allowed else guest_menu())


async def v51_account_settings(query):
    if is_admin(query.from_user.id):
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton('📦 حجم قاعدة البيانات',callback_data='v50_db_storage',style='primary'),
             InlineKeyboardButton('🧹 تنظيف الإجابات',callback_data='v49_cleanup',style='primary')],
            [back_menu()]])
        await query.edit_message_text(bold(f'⚙️ إعدادات الإدارة\n{DIV}\nأدوات النظام والصيانة الآمنة.'),parse_mode=ParseMode.HTML,reply_markup=kb); return
    student=await get_student(query.from_user.id)
    if not student or student.get('reset_pending'):
        await query.edit_message_text(bold('أكمل تسجيل حسابك أولاً عبر /start.'),parse_mode=ParseMode.HTML); return
    goal=int(student.get('daily_prep_goal') or student.get('review_daily_goal') or 1)
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('👤 حسابي',callback_data='profile',style='primary'),
         InlineKeyboardButton('👨‍👩‍👦 ولي الأمر',callback_data='parent_link',style='primary')],
        [_v51_neutral_button('✏️ تعديل معلوماتي',callback_data='edit_profile')],
        [_v51_neutral_button(f'📚 عدد التحاضير يومياً: {goal}',callback_data='v51_review_goal')],
        [_v51_neutral_button('🔄 تغيير فصل البداية أو المسار',callback_data='change_study_track')],
        [_v51_neutral_button('🗓 جدولي الدراسي',callback_data='personal_schedule')],
        [InlineKeyboardButton('🗑 إعادة تعيين معلوماتي بالكامل',callback_data='v47_reset',style='danger')],
        [back_menu()]])
    await query.edit_message_text(bold(f'⚙️ إعدادات الحساب\n{DIV}\nمعلوماتك، نظام دراستك، وربط ولي الأمر.'),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v51_exams_menu(query):
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('📝 امتحاناتي المطلوبة',callback_data='tasks|exam|normal',style='primary')],
        [InlineKeyboardButton('🏆 امتحانات تراكمي',callback_data='tasks|exam|cumulative',style='primary')],
        [_v51_neutral_button('📚 بنك امتحانات الفصول',callback_data='v42_exam_bank'),
         _v51_neutral_button('🗂 الامتحانات السابقة',callback_data='past_exams')],
        [InlineKeyboardButton('✅ الأجوبة النموذجية',callback_data='resourcecategory|model_answer',style='success')],
        [back_menu()]])
    await query.edit_message_text(bold(f'📝 الامتحانات\n{DIV}\nالامتحانات والأجوبة النموذجية أصبحت في قسم واحد.'),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v51_progress_menu(query):
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('🏅 إنجازاتي',callback_data='achievement_menu',style='primary'),
         InlineKeyboardButton('📊 تقدمي',callback_data='academic_dashboard',style='primary')],
        [_v51_neutral_button('🧭 خريطة الإتقان',callback_data='mastery_map'),
         _v51_neutral_button('🎯 نقاط ضعفي',callback_data='weaknesses_menu')],
        [back_menu()]])
    await query.edit_message_text(bold(f'📊 تقدمي\n{DIV}\nكل أدوات قياس تقدمك وتطوير مستواك في مكان واحد.'),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v51_daily_tasks_menu(query):
    bundle=await db.v51_daily_tasks(query.from_user.id); student=bundle.get('student')
    if not student or not student.get('approved') or student.get('reset_pending'):
        await query.edit_message_text(bold('🔒 أكمل تسجيل حسابك وتفعيله أولاً.'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    goal=max(1,min(5,int(student.get('daily_prep_goal') or student.get('review_daily_goal') or 1)))
    prep=bundle.get('preparation'); all_tasks=bundle.get('tasks') or []
    exams=[row for row in all_tasks if row.get('kind')=='exam']
    homework=[row for row in all_tasks if row.get('kind')=='homework']
    reviews=bundle.get('reviews') or []; selected_reviews=[]; used_preps=[]
    for row in reviews:
        prep_key=(int(row['chapter']),_v51_prep_number(row['chapter'],row['lecture']))
        if prep_key not in used_preps:
            if len(used_preps)>=goal: continue
            used_preps.append(prep_key)
        selected_reviews.append(row)
    lines=['✅ مهامي اليومية',DIV]
    kb=[]; count=0
    if prep and prep.get('pending_lectures'):
        pending=' + '.join(f"م{n}" for n in prep['pending_lectures'])
        lines.append(f"🧪 التحضير: الفصل {prep['chapter']} | {pending}"); count+=1
        kb.append([InlineKeyboardButton('🧪 فتح تحضير اليوم',callback_data='today_prep',style='success')])
    elif prep:
        lines.append('✅ تحضير اليوم مكتمل')
    if exams:
        lines.append(f'📝 امتحانات مطلوبة: {len(exams)}'); count+=len(exams)
        for row in exams[:3]:
            title=str(row.get('title') or '').replace('[تراكمي] ','')
            kb.append([InlineKeyboardButton(f'📝 {title[:42]}',callback_data=f"task|{row['id']}",style='primary')])
    if homework:
        lines.append(f'📚 واجبات مطلوبة: {len(homework)}'); count+=len(homework)
        kb.append([InlineKeyboardButton('📚 فتح الواجبات',callback_data='tasks|homework',style='primary')])
    if selected_reviews:
        lines.append(f'👑 مراجعات ملكية اليوم: {len(selected_reviews)} ضمن {len(used_preps)} تحضير'); count+=len(selected_reviews)
        kb.append([InlineKeyboardButton('👑 فتح المراجعة الملكية',callback_data='royal_review_menu',style='primary')])
    if bundle.get('weaknesses'):
        lines.append(f"🎯 نقاط ضعف مفتوحة: {bundle['weaknesses']}")
        kb.append([_v51_neutral_button('🎯 نقاط ضعفي',callback_data='weaknesses_menu')])
    if count==0:
        lines += ['🌟 أنجزت كل المطلوب منك اليوم.','يمكنك استثمار الوقت في المراجعة الملكية أو معالجة نقطة ضعف.']
        kb.append([InlineKeyboardButton('👑 المراجعة الملكية',callback_data='royal_review_menu',style='primary')])
    else:
        lines += ['',f'📌 مجموع المطلوبات الظاهرة: {count}','ابدأ بالأول ثم ارجع لهذه الصفحة حتى تكمل يومك.']
    kb.append([back_menu()])
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v51_today_preparation(query):
    uid=query.from_user.id; blocking=await student_exam_lock(uid)
    if blocking:
        state='بانتظار التفعيل' if blocking.get('exam_pending_activation') else 'مفتوح ولم يُسلّم'
        await query.edit_message_text(bold(
            f"🔒 التحضير التالي متوقف مؤقتاً\n{DIV}\n📝 {blocking['title']}\n📌 {state}\n\n"
            'أكمل الامتحان المطلوب أو افتحه لاستخدام التمديد المتاح.'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('📝 فتح الامتحان',callback_data=f"task|{blocking['id']}",style='success')],
                [back_menu()]])); return
    bundle=await db.v51_daily_tasks(uid); student=bundle.get('student'); prep=bundle.get('preparation')
    if not student or not student.get('approved') or student.get('reset_pending'):
        await query.edit_message_text(bold('🔒 أكمل تسجيل حسابك وتفعيله أولاً.'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if not prep:
        await query.edit_message_text(bold(f'🧪 تحضير اليوم\n{DIV}\n📭 لا يوجد تحضير منشور أو مستحق الآن.'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ مهامي اليومية',callback_data='daily_learning_session',style='success')],[back_menu()]])); return
    chapter=int(prep['chapter']); lectures=list(prep.get('pending_lectures') or [])
    all_lectures=list(prep.get('pending_lectures') or [])+list(prep.get('completed_lectures') or [])
    lines=['🧪 تحضيرك الحالي',DIV,f'📘 الفصل {chapter}',
        '🎬 المحاضرات: '+(' + '.join(map(str,sorted(all_lectures))) or '-')]
    if prep.get('target_date'): lines.append(f"📅 التاريخ: {prep['target_date']:%d/%m/%Y}")
    if prep.get('completed_lectures'):
        lines.append('✅ مكتمل: '+' + '.join(map(str,prep['completed_lectures'])))
    kb=[[InlineKeyboardButton(f'▶️ الذهاب إلى المحاضرة {lecture}',
        callback_data=f'prepopen|{chapter}|{lecture}',style='success')] for lecture in lectures]
    if not lectures:
        lines += ['','🌟 أكملت هذا التحضير بالكامل.']
        kb.append([InlineKeyboardButton('👑 المراجعة الملكية',callback_data='royal_review_menu',style='primary')])
    kb.append([_v51_neutral_button('⚡ اختبارات المراجعة السريعة',callback_data='v48_quick_reviews')])
    kb.append([back_menu()])
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v51_review_menu(query):
    data=await db.v42_review_context(query.from_user.id); student=data.get('student') or {}
    if not student:
        await query.edit_message_text(bold('🔒 أكمل تسجيل حساب الطالب أولاً.'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    pending=data.get('pending') or []; goal=max(1,min(5,int(student.get('daily_prep_goal') or student.get('review_daily_goal') or 1)))
    due=[row for row in pending if row.get('due')]
    lines=['👑 المراجعة الملكية',DIV,
        'تبدأ من الفصل الأول للجميع، وتُبنى فقط على المحاضرات التي درستها فعلياً.',
        '1️⃣ بعد 6 ساعات  •  2️⃣ بعد 24 ساعة',
        '3️⃣ بعد أسبوع   •  4️⃣ بعد شهر','',
        f'📚 نظامك الدراسي: {goal} تحضير يومياً',f'🔥 مستحق الآن: {len(due)}',
        f"✅ مراجعات مكتملة: {data.get('completed',0)}"]
    kb=[]
    for chapter in range(1,6):
        chapter_rows=[row for row in pending if int(row['chapter'])==chapter]
        chapter_due=sum(bool(row.get('due')) for row in chapter_rows)
        kb.append([InlineKeyboardButton(
            f"📘 الفصل {chapter} | 🔥 {chapter_due} | ⏳ {len(chapter_rows)-chapter_due}",
            callback_data=f'v51_review_chapter|{chapter}',style='primary')])
    kb.append([_v51_neutral_button('⚡ اختبارات المراجعة السريعة',callback_data='v48_quick_reviews')])
    kb.append([_v51_neutral_button(f'⚙️ عدد التحاضير اليومية: {goal}',callback_data='v51_review_goal')])
    kb.append([back_menu()])
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v51_review_chapter(query,chapter):
    data=await db.v42_review_context(query.from_user.id); student=data.get('student') or {}
    goal=max(1,min(5,int(student.get('daily_prep_goal') or student.get('review_daily_goal') or 1)))
    pending=[row for row in data.get('pending') or [] if int(row['chapter'])==int(chapter)]
    groups=defaultdict(list)
    for row in pending: groups[_v51_prep_number(chapter,row['lecture'])].append(row)
    lines=[f'👑 المراجعة الملكية | الفصل {chapter}',DIV,f'🎯 حدك اليومي: {goal} تحضير']
    kb=[]; shown=0
    for prep_no in sorted(groups):
        rows=groups[prep_no]; due=[row for row in rows if row.get('due')]
        if not due: continue
        if shown>=goal: break
        shown+=1; lectures=' + '.join(f"م{row['lecture']}" for row in due)
        lines.append(f'🔥 تحضير {prep_no or "-"}: {lectures}')
        for row in due:
            kb.append([InlineKeyboardButton(f"م{row['lecture']} | المراجعة {row['stage']}",
                callback_data=f"royal_review|{row['id']}",style='success')])
    upcoming=[row for row in pending if not row.get('due')]
    if not shown and upcoming:
        first=upcoming[0]; stamp=first['due_at'].astimezone(TIMEZONE).strftime('%d/%m %H:%M')
        lines += ['',f"⏳ أقرب مراجعة: م{first['lecture']} في {stamp}"]
    elif not pending:
        lines += ['','لا توجد مراجعات لهذا الفصل بعد. أكمل أي محاضرة منه فينشئ البوت مواعيدها الأربع تلقائياً.']
    kb += [[_v51_neutral_button('◀️ جميع الفصول',callback_data='royal_review_menu'),back_menu()]]
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v51_review_goal_menu(query):
    student=await get_student(query.from_user.id); current=int((student or {}).get('daily_prep_goal') or (student or {}).get('review_daily_goal') or 1)
    rows=[]
    for start in (1,4):
        buttons=[]
        for goal in range(start,min(start+3,6)):
            label=('✅ ' if goal==current else '')+f'{goal} تحضير يومياً'
            buttons.append(InlineKeyboardButton(label,callback_data=f'v51_review_goal_set|{goal}',style='success' if goal==current else 'primary'))
        rows.append(buttons)
    rows.append([_v51_neutral_button('◀️ المراجعة الملكية',callback_data='royal_review_menu'),back_menu()])
    track_note=('مسار الدورة يبقى مرتبطاً بموعد الأستاذ، ويُنظّم هذا العدد مهامك ومراجعاتك.'
        if student and student.get('study_track')=='course' else
        'سيعيد البوت توزيع التحاضير القادمة في جدولك بهذا العدد، من دون تغيير تحضير اليوم.')
    await query.edit_message_text(bold(f'📚 عدد التحاضير اليومية\n{DIV}\nاختر من 1 إلى 5 تحاضير في يوم الدراسة.\n\n{track_note}'),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v51_backlog_menu(query):
    items=await unwatched_lectures_for_student(query.from_user.id,datetime.now(TIMEZONE))
    kb=[]
    for item in items[:50]:
        kb.append([InlineKeyboardButton(f"📘 ف{item['chapter']} — م{item['lecture']} | تحضير {item['prep_no']}",
            callback_data=f"backlogview|{item['chapter']}|{item['lecture']}",style='primary')])
    kb.append([back_menu()])
    if items:
        text=(f'📚 تراكمي\n{DIV}\nهذه فقط المحاضرات التي انتهت مهلتها بعد أن بدأت مسارك، '
              f'ولا يشمل التحاضير السابقة أو تحضير اليوم.\n\n📌 العدد: {len(items)}')
    else:
        text='📚 تراكمي\n'+DIV+'\n🌟 لا يوجد عليك تراكم حقيقي حالياً.'
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v51_admin_students_menu(query):
    rows=await db.v51_admin_students(); active=[r for r in rows if r.get('approved')]
    course=[r for r in active if r.get('study_track')=='course']; chapter=[r for r in active if r.get('study_track')=='chapter']
    pending=[r for r in rows if not r.get('approved')]; warnings=[r for r in rows if int(r.get('warnings') or 0)>0]
    no_parent=[r for r in rows if not r.get('parent_chat_id')]
    text=(f'👥 إدارة الطلبة\n{DIV}\n📊 جميع الطلبة: {len(rows)}\n✅ مفعلون: {len(active)}\n'
          f'👥 الدورة الحالية: {len(course)}\n📚 الفصول المستقلة: {len(chapter)}\n'
          f'⏳ بانتظار التفعيل: {len(pending)}\n⚠️ لديهم إنذارات: {len(warnings)}\n'
          f'👨‍👩‍👦 بلا ولي أمر: {len(no_parent)}')
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ المفعلون',callback_data='v51_students|active|0',style='success'),
         InlineKeyboardButton('⏳ بانتظار التفعيل',callback_data='v51_students|pending|0',style='primary')],
        [_v51_neutral_button('⚠️ لديهم إنذارات',callback_data='v51_students|warnings|0'),
         _v51_neutral_button('👨‍👩‍👦 بلا ولي أمر',callback_data='v51_students|no_parent|0')],
        [InlineKeyboardButton('📋 جميع الطلبة',callback_data='v51_students|all|0',style='primary')],
        [_v51_neutral_button('⚠️ مركز الإنذارات',callback_data='v43_warnings'),back_menu()]])
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v51_admin_students_page(query,filter_name,page):
    rows=await db.v51_admin_students()
    filters={
        'active':lambda row: bool(row.get('approved')),
        'pending':lambda row:not row.get('approved'),
        'warnings':lambda row:int(row.get('warnings') or 0)>0,
        'no_parent':lambda row:not row.get('parent_chat_id'),
        'all':lambda row:True,
    }
    chosen=[row for row in rows if filters.get(filter_name,filters['all'])(row)]
    size=6; pages=max(1,(len(chosen)+size-1)//size); page=max(0,min(int(page),pages-1))
    labels={'active':'المفعلون','pending':'بانتظار التفعيل','warnings':'لديهم إنذارات','no_parent':'بلا ولي أمر','all':'جميع الطلبة'}
    lines=[f"👥 {labels.get(filter_name,'جميع الطلبة')}",DIV,f'الصفحة {page+1}/{pages} | العدد {len(chosen)}','']
    for row in chosen[page*size:(page+1)*size]:
        track='الدورة' if row.get('study_track')=='course' else f"فصل {row.get('current_chapter') or '-'}"
        lines.append(f"👤 {row['full_name']}\n🆔 {row['user_id']} | @{row.get('username') or '-'}\n"
            f"📚 {track} | ⭐ {row.get('xp',0)} | ⚠️ {row.get('warnings',0)} | "
            f"{'👨‍👩‍👦 مربوط' if row.get('parent_chat_id') else '🚫 بلا ولي أمر'}")
    if not chosen: lines.append('لا توجد حسابات ضمن هذا التصنيف.')
    nav=[]
    if page>0: nav.append(_v51_neutral_button('◀️ السابق',callback_data=f'v51_students|{filter_name}|{page-1}'))
    if page+1<pages: nav.append(_v51_neutral_button('التالي ▶️',callback_data=f'v51_students|{filter_name}|{page+1}'))
    kb=[]
    if nav: kb.append(nav)
    kb += [[_v51_neutral_button('◀️ إدارة الطلبة',callback_data='admin_students'),back_menu()]]
    await query.edit_message_text(bold('\n\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v51_admin_parents_menu(query):
    rows=await db.v51_admin_parents(); active=[r for r in rows if r.get('approved')]
    pending=[r for r in rows if not r.get('approved')]
    unique=len({int(r['parent_chat_id']) for r in rows})
    text=(f'👪 إدارة أولياء الأمور\n{DIV}\n👤 الحسابات الفريدة: {unique}\n'
          f'🔗 روابط الطلاب: {len(rows)}\n✅ مفعلة: {len(active)}\n⏳ بانتظار التفعيل: {len(pending)}')
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ المفعّلون',callback_data='v51_parents|active|0',style='success'),
         InlineKeyboardButton('⏳ قيد التفعيل',callback_data='v51_parents|pending|0',style='primary')],
        [InlineKeyboardButton('📋 جميع أولياء الأمور',callback_data='v51_parents|all|0',style='primary')],
        [back_menu()]])
    await query.edit_message_text(bold(text),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v51_admin_parents_page(query,filter_name,page):
    rows=await db.v51_admin_parents()
    if filter_name=='active': rows=[r for r in rows if r.get('approved')]
    elif filter_name=='pending': rows=[r for r in rows if not r.get('approved')]
    size=6; pages=max(1,(len(rows)+size-1)//size); page=max(0,min(int(page),pages-1))
    lines=['👪 أولياء الأمور',DIV,f'الصفحة {page+1}/{pages} | الروابط {len(rows)}','']
    for row in rows[page*size:(page+1)*size]:
        lines.append(f"👪 {row.get('parent_full_name') or '-'} | @{row.get('parent_username') or '-'}\n"
            f"🆔 {row['parent_chat_id']}\n👤 {row['student_name']} ({row['student_id']})\n"
            f"{'✅ مفعّل' if row.get('approved') else '⏳ ينتظر التفعيل'}")
    if not rows: lines.append('لا توجد روابط ضمن هذا التصنيف.')
    nav=[]
    if page>0: nav.append(_v51_neutral_button('◀️ السابق',callback_data=f'v51_parents|{filter_name}|{page-1}'))
    if page+1<pages: nav.append(_v51_neutral_button('التالي ▶️',callback_data=f'v51_parents|{filter_name}|{page+1}'))
    kb=[]
    if nav: kb.append(nav)
    kb += [[_v51_neutral_button('◀️ إدارة أولياء الأمور',callback_data='admin_parents'),back_menu()]]
    await query.edit_message_text(bold('\n\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))


async def v51_preparation_admin_menu(query):
    row=await next_unpublished_preparation()
    if row:
        current=(f"📌 المجموعة القادمة: الفصل {row['chapter']}\n"
                 f"🎬 المحاضرات: {row['lectures']}\n📅 {row['target_date']:%d/%m/%Y}")
    else: current='📭 لا توجد مجموعة محاضرات آلية قادمة.'
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('✏️ تعديل مجموعات المحاضرات',callback_data='v51_prep_chapters',style='success')],
        [_v51_neutral_button('⏩ تقديم الموعد',callback_data='prep_shift|advance'),
         _v51_neutral_button('⏪ تأخير الموعد',callback_data='prep_shift|delay')],
        [_v51_neutral_button('🏖 عطلة وتأجيل الجدول',callback_data='prep_holiday'),
         _v51_neutral_button('📅 تاريخ مخصص',callback_data='prep_custom')],
        [back_menu()]])
    await query.edit_message_text(bold(f'🗓 إدارة مجموعات المحاضرات\n{DIV}\n{current}\n\nالتعديل يحفظ في قاعدة البيانات ويصل إلى جداول الطلاب الحالية والقادمة.'),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v51_preparation_chapters(query):
    rows=[[InlineKeyboardButton(f'الفصل {n}',callback_data=f'v51_prep_chapter|{n}',style='primary')] for n in range(1,6)]
    rows.append([_v51_neutral_button('◀️ إدارة التحاضير',callback_data='prep_schedule'),back_menu()])
    await query.edit_message_text(bold('✏️ تعديل مجموعات المحاضرات\n'+DIV+'\nاختر الفصل:'),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v51_preparation_chapter(query,chapter):
    groups=CHAPTER_PREPARATION_DISTRIBUTION.get(int(chapter),[]); rows=[]
    for prep_no,lectures in enumerate(groups,1):
        label=' + '.join(f'م{n}' for n in lectures)
        rows.append([InlineKeyboardButton(f'🎞 {label}',callback_data=f'v51_prep_edit|{chapter}|{prep_no}',style='primary')])
    rows += [[_v51_neutral_button('◀️ الفصول',callback_data='v51_prep_chapters'),back_menu()]]
    await query.edit_message_text(bold(f'📘 محاضرات الفصل {chapter}\n{DIV}\nاختر مجموعة المحاضرات التي تريد تعديلها:'),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def _v51_require_student(query):
    student=await get_student(query.from_user.id)
    if student and student.get('approved') and not student.get('reset_pending'): return student
    await query.answer('هذه الخدمة للطلاب المفعّلين فقط.',show_alert=True)
    return None


_v51_previous_button_handler=button_handler
async def button_handler(update,context):
    """Direct hot-path router; legacy features remain available behind it."""
    query=update.callback_query; data=query.data or ''; uid=query.from_user.id
    token=_V51_STUDENT_CACHE.set({})
    try:
        if data=='menu':
            context.user_data.pop('v51_edit_prep',None)
            await v51_menu(query); return
        if data=='account_settings':
            await query.answer(); await v51_account_settings(query); return
        if data=='daily_learning_session':
            await query.answer('جاري ترتيب مهامك...'); await v51_daily_tasks_menu(query); return
        if data=='today_prep':
            if not await _v51_require_student(query): return
            await query.answer('جاري فتح التحضير...'); await v51_today_preparation(query); return
        if data=='exams_menu':
            if not await _v51_require_student(query): return
            await query.answer(); await v51_exams_menu(query); return
        if data=='v51_progress':
            if not await _v51_require_student(query): return
            await query.answer(); await v51_progress_menu(query); return
        if data=='backlog_auto':
            if not await _v51_require_student(query): return
            await query.answer(); await v51_backlog_menu(query); return
        if data=='royal_review_menu':
            if not await _v51_require_student(query): return
            context.user_data.pop('v41_review_oath_id',None)
            await query.answer(); await v51_review_menu(query); return
        if data.startswith('v51_review_chapter|'):
            if not await _v51_require_student(query): return
            try: chapter=int(data.split('|')[1])
            except (ValueError,IndexError): await query.answer('الفصل غير صحيح.',show_alert=True); return
            if chapter not in range(1,6): await query.answer('الفصل غير صحيح.',show_alert=True); return
            await query.answer(); await v51_review_chapter(query,chapter); return
        if data=='v51_review_goal':
            if not await _v51_require_student(query): return
            await query.answer(); await v51_review_goal_menu(query); return
        if data.startswith('v51_review_goal_set|'):
            if not await _v51_require_student(query): return
            try: goal=int(data.split('|')[1])
            except (ValueError,IndexError): await query.answer('العدد غير صحيح.',show_alert=True); return
            saved=await db.v51_set_review_daily_goal(uid,goal)
            if not saved: await query.answer('اختر عدداً من 1 إلى 5.',show_alert=True); return
            await query.answer(f'تم اعتماد {goal} تحضير يومياً.',show_alert=True)
            await v51_review_menu(query); return
        if data=='admin_students':
            if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
            await query.answer('جاري تحميل الملخص...'); await v51_admin_students_menu(query); return
        if data.startswith('v51_students|'):
            if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
            try: _,filter_name,page=data.split('|')
            except ValueError: await query.answer('الرابط غير صحيح.',show_alert=True); return
            await query.answer(); await v51_admin_students_page(query,filter_name,int(page)); return
        if data=='admin_parents':
            if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
            await query.answer('جاري تحميل الملخص...'); await v51_admin_parents_menu(query); return
        if data.startswith('v51_parents|'):
            if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
            try: _,filter_name,page=data.split('|')
            except ValueError: await query.answer('الرابط غير صحيح.',show_alert=True); return
            await query.answer(); await v51_admin_parents_page(query,filter_name,int(page)); return
        if data=='prep_schedule':
            if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
            context.user_data.pop('v51_edit_prep',None)
            await query.answer(); await v51_preparation_admin_menu(query); return
        if data=='v51_prep_chapters':
            if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
            await query.answer(); await v51_preparation_chapters(query); return
        if data.startswith('v51_prep_chapter|'):
            if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
            try: chapter=int(data.split('|')[1])
            except (ValueError,IndexError): await query.answer('الفصل غير صحيح.',show_alert=True); return
            if chapter not in range(1,6): await query.answer('الفصل غير صحيح.',show_alert=True); return
            await query.answer(); await v51_preparation_chapter(query,chapter); return
        if data.startswith('v51_prep_edit|'):
            if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
            try: _,chapter_s,prep_s=data.split('|'); chapter,prep_no=int(chapter_s),int(prep_s)
            except (ValueError,TypeError): await query.answer('التحضير غير صحيح.',show_alert=True); return
            groups=CHAPTER_PREPARATION_DISTRIBUTION.get(chapter,[])
            if not 1<=prep_no<=len(groups): await query.answer('التحضير غير موجود.',show_alert=True); return
            context.user_data['v51_edit_prep']={'chapter':chapter,'prep_no':prep_no,'started_at':datetime.now(TIMEZONE)}
            current='، '.join(map(str,groups[prep_no-1]))
            await query.answer()
            await query.edit_message_text(bold(
                f'✏️ تعديل مجموعة محاضرات الفصل {chapter}\n{DIV}\n'
                f'المحاضرات الحالية: {current}\n\nأرسل أرقام المحاضرات الجديدة برسالة واحدة، مثال:\n4, 5'),
                parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([
                    [_v51_neutral_button('❌ إلغاء',callback_data=f'v51_prep_chapter|{chapter}')],
                    [back_menu()]])); return
        return await _v51_previous_button_handler(update,context)
    finally:
        _V51_STUDENT_CACHE.reset(token)


_v51_previous_private_messages=private_messages
async def private_messages(update,context):
    state=context.user_data.get('v51_edit_prep')
    if state and is_admin(update.effective_user.id):
        if datetime.now(TIMEZONE)-state.get('started_at',datetime.now(TIMEZONE))>timedelta(minutes=15):
            context.user_data.pop('v51_edit_prep',None)
            await update.effective_message.reply_text('انتهت مهلة التعديل. افتح إدارة التحاضير من جديد.'); return
        raw=(update.effective_message.text or '').translate(str.maketrans('٠١٢٣٤٥٦٧٨٩','0123456789'))
        numbers=[]
        for value in re.findall(r'\d+',raw):
            number=int(value)
            if number not in numbers: numbers.append(number)
        chapter=int(state['chapter']); prep_no=int(state['prep_no']); maximum=len(PLAYLISTS.get(chapter,[]))
        if not numbers or any(number<1 or number>maximum for number in numbers):
            await update.effective_message.reply_text(bold(
                f'⚠️ أرسل أرقاماً صحيحة بين 1 و{maximum}، مثل: 4, 5'),parse_mode=ParseMode.HTML); return
        result=await db.v51_update_preparation_catalog(chapter,prep_no,numbers,update.effective_user.id)
        if result.get('status')!='ok':
            await update.effective_message.reply_text('تعذر حفظ التعديل. حاول مرة أخرى.'); return
        CHAPTER_PREPARATION_DISTRIBUTION[chapter][prep_no-1]=list(result['lectures'])
        context.user_data.pop('v51_edit_prep',None)
        await update.effective_message.reply_text(bold(
            f"✅ تم تعديل مجموعة المحاضرات فعليا\n{DIV}\n📘 الفصل {chapter}\n"
            f"🎬 المحاضرات الجديدة: {', '.join(map(str,result['lectures']))}\n"
            f"🗓 صفوف الدورة المحدثة: {result['course_rows']}\n"
            f"👥 جداول الطلاب الحالية والقادمة: {result['personal_rows']}"),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f'◀️ محاضرات الفصل {chapter}',callback_data=f'v51_prep_chapter|{chapter}',style='primary')],
                [back_menu()]])); return
    return await _v51_previous_private_messages(update,context)


_v51_previous_post_init=post_init
async def post_init(app):
    await _v51_previous_post_init(app)
    for row in await db.v51_preparation_catalog_overrides():
        chapter=int(row['chapter']); prep_no=int(row['prep_no'])
        groups=CHAPTER_PREPARATION_DISTRIBUTION.get(chapter,[])
        numbers=[int(value) for value in str(row['lectures']).split(',') if value.strip().isdigit()]
        if 1<=prep_no<=len(groups) and numbers: groups[prep_no-1]=numbers
    inserted=await db.v51_backfill_review_plans()
    logger.info('v51 loaded preparation overrides and backfilled %s royal review rows',inserted)


# ========================= v52 lecture-first final release =========================

def _v51_neutral_button(text,callback_data=None,**kwargs):
    """Every ordinary action is blue; destructive/navigation-home stays red."""
    return InlineKeyboardButton(text,callback_data=callback_data,style='primary',**kwargs)


def back_menu():
    return InlineKeyboardButton('🏠 القائمة الرئيسية',callback_data='menu',style='danger')


def main_menu(admin=False):
    if admin:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton('👥 إدارة الطلبة',callback_data='admin_students',style='primary'),
             InlineKeyboardButton('👪 إدارة أولياء الأمور',callback_data='admin_parents',style='primary')],
            [InlineKeyboardButton('➕ نشر واجب أو امتحان',callback_data='admin_publish',style='success')],
            [InlineKeyboardButton('🗓 إدارة مجموعات المحاضرات',callback_data='prep_schedule',style='primary'),
             InlineKeyboardButton('⏳ إدارة الامتحانات',callback_data='v42_admin_exams',style='primary')],
            [InlineKeyboardButton('⚠️ إدارة الإنذارات',callback_data='v43_warnings',style='primary'),
             InlineKeyboardButton('🔄 طلبات المسارات',callback_data='track_change_requests',style='primary')],
            [InlineKeyboardButton('⚡ أسئلة المراجعة',callback_data='v48_quick_admin',style='primary')],
            [InlineKeyboardButton('🏫 إدارة مراجعة المدرسة',callback_data='v54_school_admin',style='success')],
            [InlineKeyboardButton('🧹 تنظيف الإجابات',callback_data='v49_cleanup',style='primary'),
             InlineKeyboardButton('📦 حجم قاعدة البيانات',callback_data='v50_db_storage',style='primary')],
            [InlineKeyboardButton('🗑 حذف امتحان نهائيا',callback_data='admin_exam_delete_menu',style='danger')],
            [InlineKeyboardButton('🔔 الإشعارات',callback_data='notifications',style='primary'),
             InlineKeyboardButton('⚙️ الإعدادات',callback_data='account_settings',style='primary')],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ مهامي اليومية',callback_data='daily_learning_session',style='success')],
        [InlineKeyboardButton('🎬 محاضراتي الحالية',callback_data='today_prep',style='primary'),
         InlineKeyboardButton('📝 الامتحانات',callback_data='exams_menu',style='primary')],
        [InlineKeyboardButton('📚 الواجبات',callback_data='tasks|homework',style='primary'),
         InlineKeyboardButton('🎞 جميع المحاضرات',callback_data='playlists',style='primary')],
        [InlineKeyboardButton('👑 المراجعة الملكية',callback_data='royal_review_menu',style='primary'),
         InlineKeyboardButton('📚 تراكمي',callback_data='backlog_auto',style='primary')],
        [InlineKeyboardButton('🏫 مراجعة للمدرسة',callback_data='v54_school_review',style='success')],
        [InlineKeyboardButton('📊 تقدمي',callback_data='v51_progress',style='primary'),
         InlineKeyboardButton('🗓 جدولي الدراسي',callback_data='schedules_menu',style='primary')],
        [InlineKeyboardButton('📖 الملازم والملخصات',callback_data='study_resources',style='primary'),
         InlineKeyboardButton('⭐ متجر XP',callback_data='xp_store',style='primary')],
        [InlineKeyboardButton('🔔 الإشعارات',callback_data='notifications',style='primary'),
         InlineKeyboardButton('⚙️ إعدادات الحساب',callback_data='account_settings',style='primary')],
    ])


async def v51_exams_menu(query):
    rows=[
        [InlineKeyboardButton('📝 امتحاناتي الحالية',callback_data='tasks|exam|normal',style='primary')],
        [InlineKeyboardButton('📚 امتحانات الفصول والمحاضرات',callback_data='v42_exam_bank',style='primary')],
        [InlineKeyboardButton('🏆 امتحانات تراكمي',callback_data='tasks|exam|cumulative',style='primary')]]
    if await db.v54_school_review_access(query.from_user.id):
        rows.append([InlineKeyboardButton('🏫 امتحانات مراجعة المدرسة',callback_data='v54_school_exams',style='success')])
    rows.append([back_menu()]); kb=InlineKeyboardMarkup(rows)
    await query.edit_message_text(bold(
        f'📝 الامتحانات\n{DIV}\n'
        'امتحانات الفصول مرتبة حسب المحاضرات التي أكملتها.\n'
        'يمكن أن يجمع الامتحان محاضرة واحدة أو عدة محاضرات، ويفتح بعد موافقة ولي الأمر.'),
        parse_mode=ParseMode.HTML,reply_markup=kb)


async def v52_exam_bank(query):
    rows=[[InlineKeyboardButton(f'📘 الفصل {chapter}',callback_data=f'v52_exam_chapter|{chapter}',style='primary')]
          for chapter in range(1,6)]
    rows += [[InlineKeyboardButton('◀️ الامتحانات',callback_data='exams_menu',style='primary'),back_menu()]]
    await query.edit_message_text(bold(
        f'📚 امتحانات الفصول والمحاضرات\n{DIV}\n'
        'اختر الفصل، ثم المحاضرة التي أكملتها. لا توجد خانة منفصلة للامتحانات السابقة.'),
        parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v52_exam_chapter(query,chapter):
    bundle=await db.v52_chapter_exam_bundle(query.from_user.id,chapter)
    completed=set(bundle.get('completed') or set()); exams=bundle.get('exams') or []
    lecture_ids=[int(item[0]) for item in PLAYLISTS.get(int(chapter),[])]
    rows=[]
    for start in range(0,len(lecture_ids),2):
        line=[]
        for lecture in lecture_ids[start:start+2]:
            count=sum((int(chapter),lecture) in set(map(tuple,exam.get('required') or [])) for exam in exams)
            ready=lecture in completed
            mark='✅' if ready else '🔒'
            suffix=f' • {count} امتحان' if count else ''
            callback=(f'v52_exam_lecture|{chapter}|{lecture}' if ready
                      else f'v55_exam_study|{chapter}|{lecture}')
            line.append(InlineKeyboardButton(f'{mark} المحاضرة {lecture}{suffix}',callback_data=callback,
                style='success' if ready and count else 'primary'))
        rows.append(line)
    rows += [[InlineKeyboardButton('◀️ الفصول',callback_data='v42_exam_bank',style='primary'),back_menu()]]
    await query.edit_message_text(bold(
        f'📘 الفصل {chapter}\n{DIV}\n'
        '✅ المحاضرة المكتملة يمكن فتح امتحاناتها.\n'
        '🔒 اضغط المحاضرة غير المكتملة لتفعيلها بالمشاهدة أو بقسم الدراسة.'),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows))


async def v52_exam_lecture(query,chapter,lecture):
    bundle=await db.v52_chapter_exam_bundle(query.from_user.id,chapter)
    if int(lecture) not in set(bundle.get('completed') or set()):
        await query.answer('أكمل هذه المحاضرة أولا.',show_alert=True); return
    exams=[]
    for exam in bundle.get('exams') or []:
        required=set(map(tuple,exam.get('required') or []))
        if (int(chapter),int(lecture)) in required: exams.append(exam)
    rows=[]
    for exam in exams:
        task=exam.get('task') or {}; required=exam.get('required') or []
        lectures=' + '.join(f'م{number}' for ch,number in required if int(ch)==int(chapter))
        if task.get('submitted_at'):
            mark='✅'; state='تم التسليم'; callback='v52_exam_submitted'
        elif not exam.get('ready'):
            mark='🔒'; state='أكمل بقية المحاضرات'; callback=f"v55_exam_requirements|{exam['id']}"
        elif task and task.get('exam_pending_activation'):
            mark='⏳'; state='بانتظار ولي الأمر'; callback=f"v52_exam_open|{exam['id']}"
        else:
            mark='🟢'; state='جاهز'; callback=f"v52_exam_open|{exam['id']}"
        title=str(exam.get('title') or '').replace('[تراكمي] ','')
        rows.append([InlineKeyboardButton(f'{mark} {title} | {lectures} | {state}',callback_data=callback,
            style='success' if mark=='🟢' else 'primary')])
    if not rows:
        message='لا يوجد امتحان مربوط بهذه المحاضرة حاليا.'
    else:
        message='إذا كان الامتحان مدمجا، يجب إكمال جميع محاضراته قبل طلب موافقة ولي الأمر.'
    rows += [[InlineKeyboardButton(f'◀️ محاضرات الفصل {chapter}',callback_data=f'v52_exam_chapter|{chapter}',style='primary'),back_menu()]]
    await query.edit_message_text(bold(f'📝 الفصل {chapter} | المحاضرة {lecture}\n{DIV}\n{message}'),
        parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v52_open_exam(query,context,definition_id):
    student=await get_student(query.from_user.id)
    if not student or not student.get('parent_chat_id'):
        await query.answer('يجب ربط ولي الأمر أولا.',show_alert=True); return
    result=await db.v52_prepare_chapter_exam(definition_id,query.from_user.id)
    if result.get('status')=='locked':
        await query.answer('أكمل جميع المحاضرات المرتبطة بهذا الامتحان أولا.',show_alert=True); return
    if result.get('status')=='missing':
        await query.answer('الامتحان غير موجود أو لا يحتوي أسئلة.',show_alert=True); return
    task=result.get('task')
    if result.get('status')=='submitted':
        await query.answer('تم تسليم هذا الامتحان مسبقا.',show_alert=True); return
    if result.get('status')=='open':
        await query.answer(); await show_task(query,context,task['id']); return
    await request_exam_access(task['id'],query.from_user.id)
    if result.get('notify'):
        kb=InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ موافق، فتح الامتحان',callback_data=f"examallow|{task['id']}|{query.from_user.id}",style='success'),
            InlineKeyboardButton('❌ رفض',callback_data=f"examdeny|{task['id']}|{query.from_user.id}",style='danger')]])
        try:
            await context.bot.send_message(student['parent_chat_id'],bold(
                f"📝 طلب فتح امتحان\n{DIV}\n👤 الطالب: {student['full_name']}\n📌 {task['title']}\n\n"
                'لا يفتح الامتحان إلا بعد موافقتكم.'),parse_mode=ParseMode.HTML,reply_markup=kb)
        except TelegramError:
            await query.answer('تعذر إرسال الطلب إلى ولي الأمر. اطلب منه فتح البوت ثم حاول مجددا.',show_alert=True); return
    await query.answer('أرسل طلب الموافقة إلى ولي الأمر.',show_alert=True)
    await query.edit_message_text(bold(
        f"⏳ بانتظار موافقة ولي الأمر\n{DIV}\n📝 {task['title']}\n\n"
        'بعد الموافقة ارجع إلى المحاضرة واضغط الامتحان مرة أخرى.'),
        parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]]))


async def v51_review_menu(query):
    data=await db.v52_review_queue(query.from_user.id); due=data.get('due') or []
    lines=['👑 المراجعة الملكية',DIV,
        'هنا تظهر فقط المحاضرات التي حان وقت مراجعتها، مرتبة من الأقدم إلى الأحدث.',
        '1️⃣ بعد 6 ساعات  •  2️⃣ بعد 24 ساعة',
        '3️⃣ بعد أسبوع   •  4️⃣ بعد شهر','']
    rows=[]
    for row in due[:30]:
        stamp=row['due_at'].astimezone(TIMEZONE).strftime('%d/%m %H:%M')
        lines.append(f"🔥 الفصل {row['chapter']} | المحاضرة {row['lecture']} | المراجعة {row['stage']} | {stamp}")
        rows.append([InlineKeyboardButton(
            f"✅ راجع المحاضرة {row['lecture']} — الفصل {row['chapter']}",
            callback_data=f"royal_review|{row['id']}",style='success')])
    if not due:
        upcoming=data.get('next')
        if upcoming:
            stamp=upcoming['due_at'].astimezone(TIMEZONE).strftime('%d/%m/%Y %H:%M')
            lines += ['🌟 لا توجد مراجعة مستحقة الآن.',
                f"⏳ القادمة: الفصل {upcoming['chapter']} | المحاضرة {upcoming['lecture']} | {stamp}"]
        else:
            lines += ['🌟 لا توجد مراجعات مستحقة.',
                'بعد إكمال أي محاضرة ينشئ البوت مواعيد مراجعاتها الأربع تلقائيا.']
    lines += ['',f"✅ المراجعات المكتملة: {data.get('completed',0)}"]
    rows += [[InlineKeyboardButton('⚡ اختبارات تنشيط الذاكرة',callback_data='v48_quick_reviews',style='primary')],[back_menu()]]
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows))


async def v52_progress_overview(query):
    data=await db.v52_progress_overview(query.from_user.id)
    if not data.get('student'):
        await query.edit_message_text('أكمل تسجيل حسابك أولا.',reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    completed=data.get('completed') or {}; lines=['📅 متى ننهي المنهج؟',DIV]
    overall_done=0; overall_total=0
    for chapter in range(1,6):
        valid=[int(item[0]) for item in PLAYLISTS.get(chapter,[])]
        done=sorted(set(completed.get(chapter,[])).intersection(valid))
        overall_done+=len(done); overall_total+=len(valid)
        percent=round((len(done)/len(valid))*100) if valid else 0
        lines += ['',f'📘 الفصل {chapter} — {percent}%',
            '✅ المحاضرات المكتملة: '+('، '.join(map(str,done)) if done else 'لا توجد'),
            f'📊 الإنجاز: {len(done)}/{len(valid)}']
    overall=round((overall_done/overall_total)*100) if overall_total else 0
    finish=data.get('finish_date')
    finish_text=finish.strftime('%d/%m/%Y') if finish else 'تم إكمال المسار الحالي'
    lines += ['',DIV,f'🎯 الإنجاز الكلي: {overall}%',f'🏁 موعد الإكمال المتوقع: {finish_text}',
        'يتحدث الموعد تلقائيا عند إكمال المحاضرات التالية قبل وقتها.']
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('🔄 تحديث التقدم والموعد',callback_data='chapter_completion_schedule',style='primary')],
        [InlineKeyboardButton('◀️ الجداول',callback_data='schedules_menu',style='primary'),back_menu()]])
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=kb)


async def v51_today_preparation(query):
    uid=query.from_user.id; blocking=await student_exam_lock(uid)
    if blocking:
        state='بانتظار موافقة ولي الأمر' if blocking.get('exam_pending_activation') else 'مفتوح ولم يسلم'
        await query.edit_message_text(bold(
            f"🔒 المحاضرات التالية متوقفة مؤقتا\n{DIV}\n📝 {blocking['title']}\n📌 {state}\n\n"
            'أكمل الامتحان المطلوب أولا، ثم تفتح لك المحاضرات التالية.'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('📝 فتح الامتحان',callback_data=f"task|{blocking['id']}",style='success')],
                [back_menu()]])); return
    prep=await db.v52_current_preparation(uid)
    lines=['🎬 محاضراتك الحالية',DIV]; rows=[]
    if not prep:
        lines += ['✅ أنجزت كل المحاضرات المستحقة حاليا.',
            'يمكنك فتح المحاضرات التالية مبكرا والحصول على XP مضاعف.']
        rows.append([InlineKeyboardButton('⚡ إكمال المحاضرات التالية — 30 XP',callback_data='v52_unlock_next',style='success')])
    else:
        chapter=int(prep['chapter']); pending=list(prep.get('pending_lectures') or [])
        all_lectures=sorted(set(pending+list(prep.get('completed_lectures') or [])))
        lines += [f'📘 الفصل {chapter}',
            '🎞 المحاضرات: '+(' + '.join(map(str,all_lectures)) or '-')]
        if prep.get('early'):
            lines += ['⚡ دراسة مبكرة قبل الموعد','⭐ المكافأة عند إكمال المجموعة: 30 XP']
        elif prep.get('target_date'):
            lines.append(f"📅 الموعد: {prep['target_date']:%d/%m/%Y}")
        if prep.get('completed_lectures'):
            lines.append('✅ المكتمل: '+' + '.join(map(str,prep['completed_lectures'])))
        for lecture in pending:
            rows.append([InlineKeyboardButton(f'▶️ المحاضرة {lecture}',
                callback_data=f'prepopen|{chapter}|{lecture}',style='success')])
        if not pending:
            lines += ['','🌟 أكملت هذه المحاضرات بالكامل.']
            rows.append([InlineKeyboardButton('⚡ إكمال المحاضرات التالية — 30 XP',callback_data='v52_unlock_next',style='success')])
    rows += [[InlineKeyboardButton('⚡ اختبارات تنشيط الذاكرة',callback_data='v48_quick_reviews',style='primary')],[back_menu()]]
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v52_unlock_next(query):
    blocking=await student_exam_lock(query.from_user.id)
    if blocking:
        await query.answer('أكمل الامتحان المطلوب أولا.',show_alert=True); return
    result=await db.v52_unlock_next_preparation(query.from_user.id)
    messages={
        'finished':'🎉 أكملت جميع محاضرات مسارك الحالي.',
        'current':'أكمل المحاضرات الحالية أولا.',
        'student':'حساب الطالب غير مفعل.',
    }
    if result.get('status') not in {'ok','existing'}:
        await query.answer(messages.get(result.get('status'),'تعذر فتح المحاضرات التالية.'),show_alert=True); return
    row=result['row']; lectures=' + '.join(str(value) for value in str(row['lectures']).split(',') if value.strip())
    await query.answer('تم فتح المحاضرات التالية مع XP مضاعف.',show_alert=True)
    await query.edit_message_text(bold(
        f"⚡ فُتحت المحاضرات التالية\n{DIV}\n📘 الفصل {row['chapter']}\n🎞 المحاضرات: {lectures}\n"
        '⭐ تحصل على 30 XP بعد إكمالها بالكامل.\n'
        '📅 يتحدث تقدمك وموعد إكمال المنهج تلقائيا.'),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('▶️ فتح المحاضرات الآن',callback_data='today_prep',style='success')],
            [back_menu()]]))


async def _v52_complete_lecture(query,context,chapter,lecture):
    uid=query.from_user.id; student=await get_student(uid)
    progress=await lecture_progress(uid,chapter,lecture)
    if progress and progress.get('completed_at'):
        await query.answer('تم تسجيل هذه المحاضرة مسبقا.',show_alert=True); return
    current=await db.v52_current_preparation(uid)
    allowed=bool(current and int(current['chapter'])==int(chapter) and int(lecture) in
        set((current.get('pending_lectures') or [])+(current.get('completed_lectures') or [])))
    if not allowed:
        allowed=await db.lecture_opened_in_assigned_preparation(uid,chapter,lecture)
    if not allowed:
        await query.answer('هذه المحاضرة ليست ضمن محاضراتك الحالية.',show_alert=True); return
    if not progress or not progress.get('opened_at'):
        await query.answer('افتح المحاضرة أولا ثم ارجع بعد مشاهدتها.',show_alert=True); return
    elapsed=(datetime.now(progress['opened_at'].tzinfo)-progress['opened_at']).total_seconds()
    required=MIN_LECTURE_WATCH_MINUTES*60
    if elapsed<required:
        remain=max(1,int((required-elapsed+59)//60))
        await query.answer(f'بقي نحو {remain} دقيقة من وقت التحقق.',show_alert=True); return
    await mark_lecture_progress(uid,chapter,lecture,True)
    await linked_exam_dispatch_job(context)
    backlog_done=await complete_backlog(uid,chapter,lecture)
    award=await award_daily_preparation(uid,chapter,lecture)
    xp=int((award or {}).get('xp') or 0)
    lines=[f'✅ تم تسجيل إكمال الفصل {chapter} — المحاضرة {lecture}.']
    if xp: lines.append(f'⭐ حصلت على {xp} XP'+(' مضاعفة للدراسة المبكرة.' if (award or {}).get('early') else '.'))
    lines += ['📝 إذا اكتملت محاضرات امتحان مرتبط بها، سيظهر لك ويحتاج موافقة ولي الأمر قبل الفتح.']
    rows=[[InlineKeyboardButton('🎬 متابعة محاضراتي',callback_data='today_prep',style='success')],
          [InlineKeyboardButton('⚡ إكمال المحاضرات التالية',callback_data='v52_unlock_next',style='primary')],
          [back_menu()]]
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))
    for parent in await student_parents(uid,True):
        try: await context.bot.send_message(parent['parent_chat_id'],bold(
            f"🌟 إنجاز دراسي جديد\nأكمل الطالب {student['full_name']} المحاضرة {lecture} من الفصل {chapter}."),parse_mode=ParseMode.HTML)
        except TelegramError: pass


async def v52_admin_exam_students(query,definition_id):
    definition=await db.v31_exam_definition_for_admin(definition_id); students=await db.v42_admin_exam_students(definition_id)
    if not definition:
        await query.answer('الامتحان غير موجود.',show_alert=True); return
    answer=await db.v52_exam_model_answer(definition_id)
    rows=[]
    for row in students[:60]:
        status='✅ مسلم' if row.get('submitted_at') else ('🟢 مفتوح' if not row.get('closed') else '🔒 مغلق')
        rows.append([InlineKeyboardButton(f"👤 {row['full_name']} | {status}",callback_data=f"adminexamstudent|{row['task_id']}|{row['user_id']}",style='primary')])
    label='✏️ تغيير الجواب النموذجي' if answer else '➕ إضافة الجواب النموذجي'
    rows.append([InlineKeyboardButton(label,callback_data=f'v52_model_answer_add|{definition_id}',style='success')])
    if answer:
        rows.append([InlineKeyboardButton('🗑 حذف الجواب النموذجي',callback_data=f'v52_model_answer_delete_ask|{definition_id}',style='danger')])
    rows += [[InlineKeyboardButton('◀️ الامتحانات',callback_data='v42_admin_exams',style='primary'),back_menu()]]
    lectures=await db.linked_exam_lectures_text(definition_id)
    await query.edit_message_text(bold(
        f"📝 {definition['title']}\n{DIV}\n🎞 المحاضرات: {lectures or '-'}\n"
        f"🧠 الجواب النموذجي: {'مضاف — يرسل بعد 8 ساعات من التسليم' if answer else 'غير مضاف'}\n"
        f"👥 نسخ الطلبة: {len(students)}"),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v52_model_answer_job(context):
    for row in await db.v52_due_model_answers(50):
        items=await db.v52_exam_model_answer(row['exam_definition_id'])
        try:
            await context.bot.send_message(row['user_id'],bold(
                f"🧠 الجواب النموذجي\n{DIV}\n📝 {row['title']}\n\n"
                'مرّت 8 ساعات على تسليمك؛ أصبحت إجابتك مثبتة ولا يمكن تغييرها.'),parse_mode=ParseMode.HTML)
            for item in items:
                caption=bold(item.get('text_content') or 'الجواب النموذجي')
                if item['payload_type']=='text':
                    await context.bot.send_message(row['user_id'],caption,parse_mode=ParseMode.HTML)
                elif item['payload_type']=='photo':
                    await context.bot.send_photo(row['user_id'],item['file_id'],caption=caption,parse_mode=ParseMode.HTML)
                elif item['payload_type']=='document':
                    await context.bot.send_document(row['user_id'],item['file_id'],caption=caption,parse_mode=ParseMode.HTML)
                else:
                    await context.bot.send_video(row['user_id'],item['file_id'],caption=caption,parse_mode=ParseMode.HTML)
            marked=await db.v52_mark_model_answer_sent(row['task_id'],row['user_id'])
            if marked and row.get('parent_chat_id'):
                try: await context.bot.send_message(row['parent_chat_id'],bold(
                    f"🧠 وصل الجواب النموذجي للطالب {row['full_name']} عن امتحان «{row['title']}»، وتم تثبيت إجابته."),parse_mode=ParseMode.HTML)
                except TelegramError: pass
        except TelegramError:
            logger.exception('Could not deliver model answer: task=%s user=%s',row['task_id'],row['user_id'])


async def v51_daily_tasks_menu(query):
    bundle=await db.v51_daily_tasks(query.from_user.id); student=bundle.get('student')
    if not student or not student.get('approved') or student.get('reset_pending'):
        await query.edit_message_text(bold('🔒 أكمل تسجيل حسابك وتفعيله أولا.'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    prep=await db.v52_current_preparation(query.from_user.id)
    tasks=bundle.get('tasks') or []; exams=[row for row in tasks if row.get('kind')=='exam']
    homework=[row for row in tasks if row.get('kind')=='homework']; reviews=bundle.get('reviews') or []
    lines=['✅ مهامي اليومية',DIV]; rows=[]; total=0
    if prep and prep.get('pending_lectures'):
        lectures=' + '.join(str(number) for number in prep['pending_lectures'])
        early=' — دراسة مبكرة' if prep.get('early') else ''
        lines.append(f"🎬 محاضراتك: الفصل {prep['chapter']} | {lectures}{early}"); total+=len(prep['pending_lectures'])
        rows.append([InlineKeyboardButton('🎬 فتح محاضراتي',callback_data='today_prep',style='success')])
    elif prep:
        lines.append('✅ المحاضرات الحالية مكتملة')
        rows.append([InlineKeyboardButton('⚡ إكمال المحاضرات التالية — 30 XP',callback_data='v52_unlock_next',style='success')])
    else:
        lines.append('✅ لا توجد محاضرات مستحقة حاليا')
        rows.append([InlineKeyboardButton('⚡ إكمال المحاضرات التالية — 30 XP',callback_data='v52_unlock_next',style='success')])
    if exams:
        lines.append(f'📝 امتحانات مطلوبة: {len(exams)}'); total+=len(exams)
        for exam in exams[:3]:
            rows.append([InlineKeyboardButton(f"📝 {str(exam.get('title') or '')[:42]}",callback_data=f"task|{exam['id']}",style='primary')])
    if homework:
        lines.append(f'📚 واجبات مطلوبة: {len(homework)}'); total+=len(homework)
        rows.append([InlineKeyboardButton('📚 فتح الواجبات',callback_data='tasks|homework',style='primary')])
    if reviews:
        lines.append(f'👑 محاضرات للمراجعة الملكية: {len(reviews)}'); total+=len(reviews)
        rows.append([InlineKeyboardButton('👑 فتح المراجعة الملكية',callback_data='royal_review_menu',style='primary')])
    if bundle.get('weaknesses'):
        lines.append(f"🎯 نقاط ضعف مفتوحة: {bundle['weaknesses']}")
        rows.append([InlineKeyboardButton('🎯 نقاط ضعفي',callback_data='weaknesses_menu',style='primary')])
    if total==0: lines += ['','🌟 أنجزت جميع المطلوبات الحالية.']
    else: lines += ['',f'📌 مجموع المطلوبات: {total}']
    rows.append([back_menu()])
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v53_admin_exam_chapters(query,context,course=False,cumulative=False,reset=False):
    """Start exam publishing from chapters and individual lectures only."""
    if reset or not context.user_data.get('linked_exam'):
        context.user_data['linked_exam']={
            'step':'select_lectures','selected_preps':[],'selected_lectures':[],
            'audience':'course' if course else 'chapter','cumulative':bool(cumulative),
        }
    state=context.user_data['linked_exam']
    state['step']='select_lectures'; state['selected_preps']=[]
    selected={tuple(item) for item in state.get('selected_lectures',[])}
    rows=[]
    for start in range(1,6,2):
        line=[]
        for chapter in range(start,min(start+2,6)):
            count=sum(1 for ch,_ in selected if int(ch)==chapter)
            suffix=f' ({count})' if count else ''
            line.append(InlineKeyboardButton(f'📘 الفصل {chapter}{suffix}',
                callback_data=f'v53_admin_exam_chapter|{chapter}',style='primary'))
        rows.append(line)
    if selected:
        rows.append([InlineKeyboardButton(f'✅ متابعة بالمحاضرات المختارة ({len(selected)})',
            callback_data='v53_admin_exam_done',style='success')])
    rows.append([InlineKeyboardButton('❌ إلغاء النشر',callback_data='linkedexamcancel',style='danger'),back_menu()])
    exam_name='الامتحان التراكمي للدورة' if state.get('cumulative') else ('امتحان الدورة الحالية' if state.get('audience')=='course' else 'امتحان طلاب الفصول')
    target=('👥 الفئة: طلاب الدورة الحالية فقط' if state.get('audience')=='course'
        else '📘 الفئة: طلاب الدراسة حسب الفصول فقط')
    await query.edit_message_text(bold(
        f'📝 نشر {exam_name}\n{DIV}\n{target}\n\nاختر الفصل، ثم حدد المحاضرة أو المحاضرات الداخلة في الامتحان مباشرة.\n\n'
        'لن تظهر أرقام التحاضير في هذا المسار.'),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows))


async def v53_admin_exam_lecture_picker(query,context,chapter):
    state=context.user_data.get('linked_exam')
    if not state:
        await query.answer('ابدأ نشر الامتحان من جديد.',show_alert=True); return
    chapter=int(chapter); state['selected_preps']=[]; state['step']='select_lectures'
    selected={tuple(item) for item in state.get('selected_lectures',[])}
    if state.get('audience')=='chapter':
        selected={pair for pair in selected if int(pair[0])==chapter}
    lectures=[int(item[0]) for item in PLAYLISTS.get(chapter,[])]
    rows=[]
    for start in range(0,len(lectures),3):
        line=[]
        for lecture in lectures[start:start+3]:
            chosen=(chapter,lecture) in selected
            line.append(InlineKeyboardButton(
                f"{'☑️' if chosen else '☐'} محاضرة {lecture}",
                callback_data=f'v53_admin_exam_lecture|{chapter}|{lecture}',
                style='success' if chosen else 'primary'))
        rows.append(line)
    state['selected_lectures']=[list(pair) for pair in sorted(selected)]
    if selected:
        rows.append([InlineKeyboardButton(f'✅ إنهاء الاختيار ({len(selected)})',
            callback_data='v53_admin_exam_done',style='success')])
    rows.append([InlineKeyboardButton('◀️ الفصول',callback_data='v53_admin_exam_chapters',style='primary'),back_menu()])
    chosen_here=', '.join(str(lecture) for ch,lecture in sorted(selected) if int(ch)==chapter) or 'لا توجد'
    target=('طلاب الدورة الحالية فقط' if state.get('audience')=='course' else 'طلاب الدراسة حسب الفصول فقط')
    await query.edit_message_text(bold(
        f'📘 الفصل {chapter}\n{DIV}\n🎯 المسار: {target}\n\nاختر المحاضرات الداخلة في الامتحان.\n\n✅ المختارة من هذا الفصل: {chosen_here}'),
        parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


_v52_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ''; uid=query.from_user.id
    if data=='admin_publish':
        if not is_admin(uid): await query.answer('هذا القسم للإدارة فقط.',show_alert=True); return
        context.user_data.pop('v29_publish',None)
        context.user_data.pop('linked_exam',None)
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton('📚 نشر واجب',callback_data='v29_pub|homework',style='primary')],
            [InlineKeyboardButton('👥 امتحان طلاب الدورة الحالية',callback_data='v53_course_exam_start',style='success')],
            [InlineKeyboardButton('📘 امتحان طلاب الدراسة حسب الفصول',callback_data='v53_chapter_exam_start',style='primary')],
            [InlineKeyboardButton('🏆 تراكمي طلاب الدورة الحالية',callback_data='v53_course_cumulative_start',style='primary')],
            [InlineKeyboardButton('🗓 عرض المنشورات المجدولة',callback_data='scheduled_list',style='primary')],
            [back_menu()]])
        await query.answer(); await query.edit_message_text(bold(
            f'➕ نشر جديد — {BUILD_VERSION}\n{DIV}\n'
            'جميع أنواع الامتحانات أدناه تعتمد اختيار الفصل ثم المحاضرات مباشرة.\n'
            'لا يوجد اختيار حسب التحاضير.'),parse_mode=ParseMode.HTML,reply_markup=kb); return
    if data in {'v53_course_exam_start','v53_chapter_exam_start','v53_course_cumulative_start'}:
        if not is_admin(uid): await query.answer('هذا القسم للإدارة فقط.',show_alert=True); return
        course=data!='v53_chapter_exam_start'; cumulative=data=='v53_course_cumulative_start'
        await query.answer(); await v53_admin_exam_chapters(query,context,course,cumulative,True); return
    if data in {'v29_pub|exam','v29_pub|cumulative'}:
        if not is_admin(uid): await query.answer('هذا القسم للإدارة فقط.',show_alert=True); return
        context.user_data.pop('v29_publish',None)
        cumulative=data.endswith('|cumulative')
        await query.answer(); await v53_admin_exam_chapters(query,context,cumulative,cumulative,True); return
    if data.startswith('v29_scope|'):
        legacy=context.user_data.get('v29_publish') or {}
        if is_admin(uid) and legacy.get('kind') in {'exam','cumulative'}:
            scope=data.split('|',1)[1]; context.user_data.pop('v29_publish',None)
            course=scope=='course'; cumulative=legacy.get('kind')=='cumulative'
            context.user_data['linked_exam']={
                'step':'select_lectures','selected_preps':[],'selected_lectures':[],
                'audience':'course' if course else 'chapter','cumulative':cumulative,
            }
            await query.answer('تم تحويل النشر إلى نظام المحاضرات.')
            if scope.startswith('chapter_'):
                await v53_admin_exam_lecture_picker(query,context,int(scope.split('_')[1]))
            else:
                await v53_admin_exam_chapters(query,context)
            return
    if data.startswith('v29_prep|') and is_admin(uid):
        legacy=context.user_data.pop('v29_publish',None) or {}
        _,chapter_raw,_=data.split('|')
        context.user_data['linked_exam']={
            'step':'select_lectures','selected_preps':[],'selected_lectures':[],
            'audience':'course' if legacy.get('scope')=='course' else 'chapter',
            'cumulative':legacy.get('kind')=='cumulative',
        }
        await query.answer('ألغي اختيار التحاضير؛ اختر المحاضرات مباشرة.')
        await v53_admin_exam_lecture_picker(query,context,int(chapter_raw)); return
    if data in {'linkedexam_start','courseexam_start','courseexam_start|cumulative'}:
        if not is_admin(uid): await query.answer('هذا القسم للإدارة فقط.',show_alert=True); return
        course=data.startswith('courseexam_start'); cumulative=data=='courseexam_start|cumulative'
        await query.answer(); await v53_admin_exam_chapters(query,context,course,cumulative,True); return
    if data=='v53_admin_exam_chapters':
        if not is_admin(uid): await query.answer('هذا القسم للإدارة فقط.',show_alert=True); return
        await query.answer(); await v53_admin_exam_chapters(query,context); return
    if data.startswith('v53_admin_exam_chapter|'):
        if not is_admin(uid): await query.answer('هذا القسم للإدارة فقط.',show_alert=True); return
        await query.answer(); await v53_admin_exam_lecture_picker(query,context,int(data.split('|')[1])); return
    if data.startswith('v53_admin_exam_lecture|'):
        if not is_admin(uid): await query.answer('هذا القسم للإدارة فقط.',show_alert=True); return
        state=context.user_data.get('linked_exam')
        if not state: await query.answer('ابدأ نشر الامتحان من جديد.',show_alert=True); return
        _,chapter_raw,lecture_raw=data.split('|'); pair=(int(chapter_raw),int(lecture_raw))
        selected={tuple(item) for item in state.get('selected_lectures',[])}
        if state.get('audience')=='chapter': selected={item for item in selected if int(item[0])==pair[0]}
        if pair in selected: selected.remove(pair)
        else: selected.add(pair)
        state['selected_lectures']=[list(item) for item in sorted(selected)]; state['selected_preps']=[]
        await query.answer('تم تحديث اختيار المحاضرة.')
        await v53_admin_exam_lecture_picker(query,context,pair[0]); return
    if data=='v53_admin_exam_done':
        if not is_admin(uid): await query.answer('هذا القسم للإدارة فقط.',show_alert=True); return
        state=context.user_data.get('linked_exam')
        if not state or not state.get('selected_lectures'):
            await query.answer('اختر محاضرة واحدة على الأقل.',show_alert=True); return
        state['selected_preps']=[]; state['step']='title'
        labels='، '.join(f"ف{chapter}/م{lecture}" for chapter,lecture in sorted(map(tuple,state['selected_lectures'])))
        await query.answer(); await query.edit_message_text(bold(
            f'✅ المحاضرات المختارة: {labels}\n\n✍️ أرسل الآن اسم الامتحان.'),parse_mode=ParseMode.HTML); return
    if data.startswith(('linkedexamchapter|','coursepage|','courseprep|','linkedexamprep|')) or data in {'linkedprep_done','linkedprep_title','linkedmanual_start'}:
        if is_admin(uid) and context.user_data.get('linked_exam'):
            context.user_data['linked_exam']['selected_preps']=[]
            await query.answer('تم تحويل النشر إلى اختيار المحاضرات مباشرة.')
            await v53_admin_exam_chapters(query,context); return
    if data=='v42_exam_bank' or data=='past_exams':
        if not await _v51_require_student(query): return
        await query.answer(); await v52_exam_bank(query); return
    if data.startswith(('v42_bank_chapter|','chapter_exam|')):
        if not await _v51_require_student(query): return
        await query.answer(); await v52_exam_chapter(query,int(data.split('|')[1])); return
    if data.startswith('v52_exam_chapter|'):
        if not await _v51_require_student(query): return
        await query.answer(); await v52_exam_chapter(query,int(data.split('|')[1])); return
    if data.startswith('v52_exam_lecture|'):
        if not await _v51_require_student(query): return
        _,chapter,lecture=data.split('|'); await query.answer(); await v52_exam_lecture(query,int(chapter),int(lecture)); return
    if data=='v52_exam_locked':
        await query.answer('أكمل المحاضرة أو بقية المحاضرات المرتبطة بالامتحان أولا.',show_alert=True); return
    if data=='v52_exam_submitted':
        await query.answer('تم تسليم هذا الامتحان مسبقا.',show_alert=True); return
    if data.startswith('v52_exam_open|'):
        if not await _v51_require_student(query): return
        await v52_open_exam(query,context,int(data.split('|')[1])); return
    if data=='chapter_completion_schedule':
        if not await _v51_require_student(query): return
        await query.answer('جاري تحديث تقدمك...'); await v52_progress_overview(query); return
    if data=='v52_unlock_next':
        if not await _v51_require_student(query): return
        await v52_unlock_next(query); return
    if data.startswith('prepverify|') and not is_admin(uid):
        _,chapter,lecture=data.split('|'); await _v52_complete_lecture(query,context,int(chapter),int(lecture)); return
    if data.startswith(('submit|','retrysubmission|')) and not is_admin(uid):
        try: task_id=int(data.split('|')[1])
        except (ValueError,IndexError): await query.answer('رقم الامتحان غير صحيح.',show_alert=True); return
        student=await get_student(uid)
        if not student or student.get('reset_pending'):
            return await _v52_previous_button_handler(update,context)
        state=await db.v52_submission_state(task_id,uid)
        if state and state.get('model_answer_sent_at'):
            context.user_data.pop('waiting_submission',None)
            await query.answer('وصل الجواب النموذجي؛ ثبتت إجابتك ولا يمكن تغييرها.',show_alert=True); return
    if data.startswith('v52_model_answer_add|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        definition_id=int(data.split('|')[1]); definition=await db.v31_exam_definition_for_admin(definition_id)
        if not definition: await query.answer('الامتحان غير موجود.',show_alert=True); return
        context.user_data['v52_model_answer']={'definition_id':definition_id,'items':[],'started_at':datetime.now(TIMEZONE)}
        await query.answer(); await query.edit_message_text(bold(
            f"🧠 إضافة جواب نموذجي\n{DIV}\n📝 {definition['title']}\n\n"
            'أرسل الجواب كنص أو صورة أو PDF أو فيديو. يمكنك إرسال عدة ملفات، ثم اضغط «إنهاء وحفظ».'),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data.startswith('v52_model_answer_finish|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        definition_id=int(data.split('|')[1]); state=context.user_data.get('v52_model_answer') or {}
        if int(state.get('definition_id') or 0)!=definition_id or not state.get('items'):
            await query.answer('أرسل الجواب أولا.',show_alert=True); return
        count=await db.v52_replace_exam_model_answer(definition_id,state['items'],uid)
        context.user_data.pop('v52_model_answer',None)
        if not count: await query.answer('تعذر حفظ الجواب.',show_alert=True); return
        await query.answer('تم حفظ الجواب النموذجي.',show_alert=True)
        await v52_admin_exam_students(query,definition_id); return
    if data.startswith('v52_model_answer_delete_ask|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        definition_id=int(data.split('|')[1]); await query.answer()
        await query.edit_message_text(bold('هل تريد حذف الجواب النموذجي المرتبط بهذا الامتحان؟'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('✅ نعم، حذف',callback_data=f'v52_model_answer_delete|{definition_id}',style='danger'),
                InlineKeyboardButton('❌ إلغاء',callback_data=f'v42_admin_exam|{definition_id}',style='primary')]])); return
    if data.startswith('v52_model_answer_delete|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        definition_id=int(data.split('|')[1]); await db.v52_delete_exam_model_answer(definition_id,uid)
        await query.answer('تم حذف الجواب النموذجي.',show_alert=True)
        await v52_admin_exam_students(query,definition_id); return
    return await _v52_previous_button_handler(update,context)


_v52_previous_private_messages=private_messages
async def private_messages(update,context):
    state=context.user_data.get('v52_model_answer')
    if state and is_admin(update.effective_user.id):
        if datetime.now(TIMEZONE)-state.get('started_at',datetime.now(TIMEZONE))>timedelta(minutes=30):
            context.user_data.pop('v52_model_answer',None)
            await update.effective_message.reply_text('انتهت مهلة الإضافة. افتح الامتحان من لوحة الإدارة وابدأ من جديد.'); return
        payload_type,file_id,content=message_payload(update.effective_message)
        if payload_type not in {'text','photo','document','video'}:
            await update.effective_message.reply_text('أرسل نصا أو صورة أو PDF أو فيديو فقط.'); return
        if payload_type=='document' and (update.effective_message.document.mime_type or '').lower()!='application/pdf':
            await update.effective_message.reply_text('الملفات المدعومة هنا PDF فقط.'); return
        state['items'].append({'payload_type':payload_type,'file_id':file_id,'text_content':content or ''})
        await update.effective_message.reply_text(bold(
            f"✅ أضيف الجزء رقم {len(state['items'])}.\nأرسل جزءا آخر أو اضغط إنهاء وحفظ."),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('✅ إنهاء وحفظ',callback_data=f"v52_model_answer_finish|{state['definition_id']}",style='success')],
                [back_menu()]])); return
    oath=context.user_data.get('awaiting_private_study_oath')
    if oath and not is_admin(update.effective_user.id):
        received=(update.effective_message.text or '').strip()
        if received!=PRIVATE_STUDY_OATH:
            await update.effective_message.reply_text(bold('⚠️ يجب إرسال القسم حرفيا بدون تغيير.'),parse_mode=ParseMode.HTML); return
        context.user_data.pop('awaiting_private_study_oath',None)
        chapter,lecture=int(oath['chapter']),int(oath['lecture']); uid=update.effective_user.id
        current=await db.v52_current_preparation(uid)
        allowed=bool(current and int(current['chapter'])==chapter and lecture in
            set((current.get('pending_lectures') or [])+(current.get('completed_lectures') or [])))
        if not allowed:
            await update.effective_message.reply_text(bold('🔒 هذه المحاضرة ليست ضمن محاضراتك الحالية.'),parse_mode=ParseMode.HTML); return
        progress=await lecture_progress(uid,chapter,lecture)
        if progress and progress.get('completed_at'):
            await update.effective_message.reply_text(bold('✅ هذه المحاضرة مكتملة مسبقا.'),parse_mode=ParseMode.HTML,reply_markup=main_menu()); return
        await mark_lecture_progress(uid,chapter,lecture,True,'private_source_oath')
        await linked_exam_dispatch_job(context)
        await complete_backlog(uid,chapter,lecture)
        award=await award_daily_preparation(uid,chapter,lecture); xp=int((award or {}).get('xp') or 0)
        lines=[f'✅ تم تسجيل دراسة الفصل {chapter} — المحاضرة {lecture} من مصدرك الخاص.']
        if xp: lines.append(f'⭐ حصلت على {xp} XP'+(' مضاعفة للدراسة المبكرة.' if (award or {}).get('early') else '.'))
        lines.append('📝 الامتحان المرتبط يظهر بعد اكتمال محاضراته ويحتاج موافقة ولي الأمر قبل فتحه.')
        kb=InlineKeyboardMarkup([
            [InlineKeyboardButton('🎬 متابعة محاضراتي',callback_data='today_prep',style='success')],
            [InlineKeyboardButton('⚡ إكمال المحاضرات التالية',callback_data='v52_unlock_next',style='primary')],
            [back_menu()]])
        await update.effective_message.reply_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=kb)
        student=await get_student(uid)
        for parent in await student_parents(uid,True):
            try: await context.bot.send_message(parent['parent_chat_id'],bold(
                f"🌟 إنجاز دراسي جديد\nأكمل الطالب {student['full_name']} المحاضرة {lecture} من الفصل {chapter} من مصدره الخاص."),parse_mode=ParseMode.HTML)
            except TelegramError: pass
        return
    task_id=context.user_data.get('waiting_submission')
    if task_id:
        state=await db.v52_submission_state(task_id,update.effective_user.id)
        if state and state.get('model_answer_sent_at'):
            context.user_data.pop('waiting_submission',None)
            await update.effective_message.reply_text(bold(
                '🔒 وصل إليك الجواب النموذجي لهذا الامتحان، لذلك ثبتت إجابتك ولا يمكن تغييرها.'),parse_mode=ParseMode.HTML); return
    handled=await _v52_previous_private_messages(update,context)
    if task_id and handled:
        state=await db.v52_submission_state(task_id,update.effective_user.id)
        due=(state or {}).get('model_answer_due_at')
        if due and not (state or {}).get('model_answer_sent_at'):
            now=datetime.now(due.tzinfo or TIMEZONE); delay=max(1,(due-now).total_seconds())
            name=f'v52-model-answer-{task_id}-{update.effective_user.id}'
            if not context.job_queue.get_jobs_by_name(name):
                context.job_queue.run_once(v52_model_answer_job,delay,name=name)
    return handled


async def v42_admin_exam_students(query,definition_id):
    return await v52_admin_exam_students(query,definition_id)


_v52_previous_eco_critical_job=v49_eco_critical_job
async def v49_eco_critical_job(context):
    await _v52_previous_eco_critical_job(context)
    await _v49_run_maintenance_step(context,'model answers',v52_model_answer_job)


_v52_previous_activity_sweep=v49_activity_sweep
async def v49_activity_sweep(context):
    return await _v52_previous_activity_sweep(context)


_v52_previous_post_init=post_init
async def post_init(app):
    await _v52_previous_post_init(app)
    if not NEON_ECO_MODE:
        app.job_queue.run_repeating(v52_model_answer_job,60,first=30,name='v52_model_answer_delivery')
    logger.info('v52 lecture-first release ready')


# ========================= v54 school-review programme =========================

def _v54_school_review_text(review,prefix='🏫 مراجعة للمدرسة'):
    return (
        f"{prefix}\n{DIV}\n"
        f"✦ الأسبوع {review['week_label']} ✦\n\n"
        f"🔹 {review['chapter_label']}\n"
        f"📚 الموضوعات: {review['topics']}\n"
        f"⏳ موعد الامتحان: {review['exam_date'].strftime('%d/%m/%Y')}\n"
        f"{DIV}"
    )


async def v54_school_review_menu(query):
    access=await db.v54_school_review_access(query.from_user.id)
    if not access:
        await query.answer('هذا النظام يفعله الأستاذ لطلاب الدورة الحالية المحددين فقط.',show_alert=True); return
    await query.answer()
    catalog=await db.v54_school_review_catalog(query.from_user.id)
    today=datetime.now(TIMEZONE).date(); published=[r for r in catalog if r.get('published_at')]
    active=[r for r in published if r['publish_date']<=today<=r['exam_date']]
    upcoming=next((r for r in catalog if r['publish_date']>today),None)
    lines=['🏫 مراجعة للمدرسة',DIV,'تظهر لك مراجعة الأسبوع الحالي فقط حتى تبقى الخطة واضحة ومرتبة.','']
    rows=[]
    for review in active:
        status='✅ مكتملة' if review.get('completed_at') else '📚 مطلوبة'
        lines += [f"✦ الأسبوع {review['week_label']} ✦",f"🔹 {review['chapter_label']}",
            f"📖 {review['topics']}",f"⏳ الامتحان: {review['exam_date'].strftime('%d/%m/%Y')} | {status}",'']
        rows.append([InlineKeyboardButton(
            f"{'✅' if review.get('completed_at') else '📚'} الأسبوع {review['week_label']}",
            callback_data=f"v54_school_review_open|{review['id']}",
            style='success' if not review.get('completed_at') else 'primary')])
    if not active:
        lines.append('🌟 لا توجد مراجعة مدرسية مطلوبة منك اليوم.')
    if upcoming:
        lines += ['',f"⏭ القادمة: الأسبوع {upcoming['week_label']} — {upcoming['publish_date'].strftime('%d/%m/%Y')}"]
    rows += [[InlineKeyboardButton('📝 امتحانات مراجعة المدرسة',callback_data='v54_school_exams',style='primary')],[back_menu()]]
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v54_school_review_detail(query,review_id):
    if not await db.v54_school_review_access(query.from_user.id):
        await query.answer('هذا القسم غير مفعل لحسابك.',show_alert=True); return
    review=await db.v54_school_review_by_id(review_id,query.from_user.id)
    if not review or not review.get('published_at'):
        await query.answer('هذه المراجعة لم تنشر بعد.',show_alert=True); return
    await query.answer()
    today=datetime.now(TIMEZONE).date()
    rows=[]
    if review.get('completed_at'):
        rows.append([InlineKeyboardButton('✅ تم إكمال المراجعة',callback_data='v54_school_done',style='success')])
    elif review['publish_date']<=today<=review['exam_date']:
        rows.append([InlineKeyboardButton('✅ أكملت المراجعة',callback_data=f"v54_school_complete|{review['id']}",style='success')])
    else:
        rows.append([InlineKeyboardButton('⏳ انتهت مدة هذه المراجعة',callback_data='v54_school_done',style='primary')])
    rows += [[InlineKeyboardButton('📝 امتحانات المراجعة',callback_data='v54_school_exams',style='primary')],
             [InlineKeyboardButton('◀️ مراجعة للمدرسة',callback_data='v54_school_review',style='primary'),back_menu()]]
    await query.edit_message_text(bold(_v54_school_review_text(review)),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows))


async def v54_school_review_exams(query):
    if not await db.v54_school_review_access(query.from_user.id):
        await query.answer('هذا القسم غير مفعل لحسابك.',show_alert=True); return
    await query.answer()
    catalog=await db.v54_school_review_catalog(query.from_user.id); today=datetime.now(TIMEZONE).date()
    lines=['🏫 امتحانات مراجعة المدرسة',DIV,'يفتح الامتحان في موعده بعد إكمال المراجعة وموافقة ولي الأمر.','']
    rows=[]
    for review in catalog:
        if not review.get('published_at') and review['exam_date']>today: continue
        if review.get('submitted_at'):
            mark,state,callback='✅','تم التسليم','v54_school_done'
        elif not review.get('completed_at'):
            mark,state,callback='🔒','أكمل المراجعة أولا',f"v54_school_review_open|{review['id']}"
        elif review['exam_date']>today:
            mark,state,callback='⏳',review['exam_date'].strftime('%d/%m/%Y'),'v54_school_done'
        elif not int(review.get('media_count') or 0):
            mark,state,callback='📭','بانتظار أسئلة الأستاذ','v54_school_done'
        elif review.get('task_id') and not review.get('exam_pending_activation'):
            mark,state,callback='🟢','مفتوح',f"v54_school_exam_open|{review['id']}"
        else:
            mark,state,callback='🔐','يحتاج موافقة ولي الأمر',f"v54_school_exam_open|{review['id']}"
        lines.append(f"{mark} الأسبوع {review['week_label']} — {state}")
        rows.append([InlineKeyboardButton(f"{mark} الأسبوع {review['week_label']} | {state}",callback_data=callback,
            style='success' if mark=='🟢' else 'primary')])
    if not rows: lines.append('لا يوجد امتحان مراجعة منشور حاليا.')
    rows += [[InlineKeyboardButton('◀️ مراجعة للمدرسة',callback_data='v54_school_review',style='primary'),back_menu()]]
    await query.edit_message_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v54_school_admin_menu(query):
    students=await db.v54_school_review_admin_students(); enabled=sum(bool(row['school_review_enabled']) for row in students)
    catalog=await db.v54_school_review_catalog()
    ready=sum(int(row.get('media_count') or 0)>0 for row in catalog)
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('👥 تفعيل الطلاب المحددين',callback_data='v54_school_students|0',style='success')],
        [InlineKeyboardButton('🗓 الأسابيع وأسئلة الامتحانات',callback_data='v54_school_catalog',style='primary')],
        [InlineKeyboardButton('➕ إضافة أسبوع مراجعة',callback_data='v54_school_add',style='success')],
        [back_menu()]])
    await query.edit_message_text(bold(
        f'🏫 إدارة مراجعة المدرسة\n{DIV}\n'
        f'👥 الطلاب المفعلون: {enabled}\n📝 امتحانات مضافة: {ready} من {len(catalog)}\n\n'
        'الميزة لا تعمل إلا لطلاب الدورة الحالية الذين تفعلهم من هنا.'),
        parse_mode=ParseMode.HTML,reply_markup=kb)


async def v54_school_admin_students(query,page=0):
    all_rows=await db.v54_school_review_admin_students(200,0); page=max(0,int(page)); per_page=20
    start=page*per_page; students=all_rows[start:start+per_page]; rows=[]
    for student in students:
        enabled=bool(student['school_review_enabled'])
        rows.append([InlineKeyboardButton(
            f"{'✅' if enabled else '☐'} {student['full_name']} — {student['school']}",
            callback_data=f"v54_school_toggle|{student['user_id']}|{page}",style='success' if enabled else 'primary')])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton('◀️ السابق',callback_data=f'v54_school_students|{page-1}',style='primary'))
    if start+per_page<len(all_rows): nav.append(InlineKeyboardButton('التالي ▶️',callback_data=f'v54_school_students|{page+1}',style='primary'))
    if nav: rows.append(nav)
    rows += [[InlineKeyboardButton('◀️ إدارة المراجعة',callback_data='v54_school_admin',style='primary'),back_menu()]]
    await query.edit_message_text(bold(
        f'👥 تفعيل مراجعة المدرسة\n{DIV}\n'
        'اضغط اسم الطالب للتفعيل أو الإيقاف. تظهر هنا حسابات الدورة الحالية المعتمدة فقط.'),
        parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v54_school_admin_catalog(query):
    catalog=await db.v54_school_review_catalog(); rows=[]
    for review in catalog:
        count=int(review.get('media_count') or 0); mark='✅' if count else '📭'
        rows.append([InlineKeyboardButton(
            f"{mark} الأسبوع {review['week_label']} | {review['exam_date'].strftime('%d/%m/%Y')}",
            callback_data=f"v54_school_admin_review|{review['id']}",style='success' if count else 'primary')])
    rows += [[InlineKeyboardButton('◀️ إدارة المراجعة',callback_data='v54_school_admin',style='primary'),back_menu()]]
    await query.edit_message_text(bold('🗓 أسابيع مراجعة المدرسة\nاختر أسبوعا لإضافة أو استبدال أسئلة امتحانه.'),
        parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v54_school_admin_review(query,review_id):
    review=await db.v54_school_review_by_id(review_id)
    if not review:
        await query.answer('الأسبوع غير موجود.',show_alert=True); return
    count=int(review.get('media_count') or 0)
    rows=[
        [InlineKeyboardButton('➕ إضافة/استبدال أسئلة الامتحان',callback_data=f"v54_school_exam_add|{review['id']}",style='success')],
        [InlineKeyboardButton('◀️ جميع الأسابيع',callback_data='v54_school_catalog',style='primary'),back_menu()]]
    await query.edit_message_text(bold(
        _v54_school_review_text(review,'⚙️ إعداد أسبوع المراجعة')+
        f"\n📝 أجزاء الامتحان المحفوظة: {count}\n"
        f"📢 حالة النشر: {'منشور' if review.get('published_at') else 'بانتظار موعده'}"),
        parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def _v54_notify_parent_exam(context,user_id,task,review):
    student=await get_student(user_id)
    if not student: return False
    parents=await student_parents(user_id,True); parent_ids={int(p['parent_chat_id']) for p in parents if p.get('parent_chat_id')}
    if student.get('parent_chat_id'): parent_ids.add(int(student['parent_chat_id']))
    kb=InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ موافق، فتح الامتحان',callback_data=f"examallow|{task['id']}|{user_id}",style='success'),
        InlineKeyboardButton('❌ رفض',callback_data=f"examdeny|{task['id']}|{user_id}",style='danger')]])
    sent=False
    for parent_id in parent_ids:
        try:
            await context.bot.send_message(parent_id,bold(
                f"🏫 طلب امتحان مراجعة المدرسة\n{DIV}\n👤 الطالب: {student['full_name']}\n"
                f"✦ الأسبوع {review['week_label']} ✦\n📚 {review['topics']}\n\n"
                'أكمل الطالب المراجعة. وافق لفتح الامتحان لمدة 24 ساعة.'),
                parse_mode=ParseMode.HTML,reply_markup=kb); sent=True
        except TelegramError: pass
    if not sent and OWNER_CHAT_ID:
        try:
            await context.bot.send_message(OWNER_CHAT_ID,bold(
                f"⚠️ تعذر إرسال موافقة مراجعة المدرسة لولي الأمر\n👤 {student['full_name']} — {user_id}\n"
                f"📝 {task['title']}"),parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ فتح من الإدارة',
                    callback_data=f"adminexamallow|{task['id']}|{user_id}",style='success')]])); sent=True
        except TelegramError: pass
    return sent


async def v54_school_review_job(context):
    for review in await db.v54_due_school_review_publications():
        if not BIOLOGY_GROUP_ID: break
        try:
            sent=await context.bot.send_message(BIOLOGY_GROUP_ID,bold(_v54_school_review_text(review)),
                parse_mode=ParseMode.HTML,message_thread_id=SCHOOL_REVIEW_TOPIC_ID or None)
        except TelegramError:
            logger.exception('Could not publish school review %s',review['id']); continue
        await db.v54_mark_school_review_published(review['id'],sent.message_id)
        for student in await db.v54_enabled_school_review_students():
            try:
                await context.bot.send_message(student['user_id'],bold(
                    _v54_school_review_text(review,'🏫 نزلت مراجعة المدرسة لهذا الأسبوع')),
                    parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton('📚 فتح المراجعة',callback_data=f"v54_school_review_open|{review['id']}",style='success')]]))
                await db.v54_mark_school_review_notification(review['id'],student['user_id'],'published')
            except TelegramError: pass
    for reminder in await db.v54_due_school_review_reminders():
        days=max(0,(reminder['exam_date']-datetime.now(TIMEZONE).date()).days)
        label='آخر 24 ساعة' if days<=1 else f'بقي {days} أيام'
        try:
            await context.bot.send_message(reminder['user_id'],bold(
                f"⏰ تذكير مراجعة المدرسة — {label}\n{DIV}\n"
                f"✦ الأسبوع {reminder['week_label']} ✦\n📚 {reminder['topics']}\n"
                f"📝 موعد الامتحان: {reminder['exam_date'].strftime('%d/%m/%Y')}"),
                parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('📚 فتح المراجعة',callback_data=f"v54_school_review_open|{reminder['id']}",style='success')]]))
            await db.v54_mark_school_review_notification(reminder['id'],reminder['user_id'],reminder['reminder_kind'])
        except TelegramError: pass
    for due in await db.v54_due_school_review_exams():
        result=await db.v54_prepare_school_review_exam(due['review_id'],due['user_id']); task=result.get('task')
        if not task: continue
        review=await db.v54_school_review_by_id(due['review_id'],due['user_id'])
        sent=await _v54_notify_parent_exam(context,due['user_id'],task,review)
        if sent:
            await db.v54_mark_school_review_exam_notified(due['review_id'],due['user_id'])
            try:
                await context.bot.send_message(due['user_id'],bold(
                    f"🔐 امتحان مراجعة المدرسة جاهز\n{DIV}\n✦ الأسبوع {review['week_label']} ✦\n"
                    'تم إرسال طلب فتحه إلى ولي الأمر.'),parse_mode=ParseMode.HTML)
            except TelegramError: pass


_v54_previous_button_handler=button_handler
async def button_handler(update,context):
    query=update.callback_query; data=query.data or ''; uid=query.from_user.id
    if data=='menu':
        context.user_data.pop('v54_school_exam_media',None)
        context.user_data.pop('awaiting_school_review_oath',None)
        context.user_data.pop('v54_school_review_add',None)
    if data=='v54_school_review':
        await v54_school_review_menu(query); return
    if data.startswith('v54_school_review_open|'):
        await v54_school_review_detail(query,int(data.split('|')[1])); return
    if data.startswith('v54_school_complete|'):
        review_id=int(data.split('|')[1]); review=await db.v54_school_review_by_id(review_id,uid)
        if not review or review.get('completed_at'):
            await query.answer('هذه المراجعة مكتملة أو غير متاحة.',show_alert=True); return
        context.user_data['awaiting_school_review_oath']={'review_id':review_id,'started_at':datetime.now(TIMEZONE)}
        await query.answer(); await query.edit_message_text(bold(
            f"🤝 قسم إكمال مراجعة المدرسة\n{DIV}\nأرسل النص التالي حرفيا برسالة واحدة:\n\n{SCHOOL_REVIEW_OATH}"),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data=='v54_school_exams':
        await v54_school_review_exams(query); return
    if data.startswith('v54_school_exam_open|'):
        if not await db.v54_school_review_access(uid):
            await query.answer('هذا القسم غير مفعل لحسابك.',show_alert=True); return
        review_id=int(data.split('|')[1]); result=await db.v54_prepare_school_review_exam(review_id,uid)
        if result['status']=='waiting':
            await query.answer('الامتحان لم يحن موعده أو لم تضف أسئلته بعد.',show_alert=True); return
        if result['status']=='submitted':
            await query.answer('تم تسليم هذا الامتحان مسبقا.',show_alert=True); return
        task=result['task']; review=await db.v54_school_review_by_id(review_id,uid)
        if result['status']=='open':
            await query.answer(); await show_task(query,context,task['id']); return
        if result.get('notify'):
            sent=await _v54_notify_parent_exam(context,uid,task,review)
            if sent: await db.v54_mark_school_review_exam_notified(review_id,uid)
        await query.answer('الامتحان ينتظر موافقة ولي الأمر.',show_alert=True); return
    if data=='v54_school_done':
        await query.answer('لا يوجد إجراء مطلوب هنا.',show_alert=True); return
    if data=='v54_school_admin':
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        await query.answer(); await v54_school_admin_menu(query); return
    if data.startswith('v54_school_students|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        await query.answer(); await v54_school_admin_students(query,int(data.split('|')[1])); return
    if data.startswith('v54_school_toggle|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        _,student_id,page=data.split('|'); result=await db.v54_toggle_school_review_student(int(student_id),uid)
        if result['status']!='ok': await query.answer('الطالب ليس ضمن الدورة الحالية.',show_alert=True); return
        await query.answer('تم التفعيل' if result['enabled'] else 'تم إيقاف الميزة')
        await v54_school_admin_students(query,int(page)); return
    if data=='v54_school_catalog':
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        await query.answer(); await v54_school_admin_catalog(query); return
    if data=='v54_school_add':
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        context.user_data['v54_school_review_add']={'step':'week_no','started_at':datetime.now(TIMEZONE)}
        await query.answer(); await query.edit_message_text(bold(
            '➕ إضافة أسبوع مراجعة\n━━━━━━━━━━━━━━━━━━\nأرسل رقم الأسبوع، مثال: 9'),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data.startswith('v54_school_admin_review|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        await query.answer(); await v54_school_admin_review(query,int(data.split('|')[1])); return
    if data.startswith('v54_school_exam_add|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        review_id=int(data.split('|')[1]); review=await db.v54_school_review_by_id(review_id)
        if not review: await query.answer('الأسبوع غير موجود.',show_alert=True); return
        context.user_data['v54_school_exam_media']={'review_id':review_id,'items':[],'started_at':datetime.now(TIMEZONE)}
        await query.answer(); await query.edit_message_text(bold(
            f"📝 أسئلة امتحان الأسبوع {review['week_label']}\n{DIV}\n"
            'أرسل نصا أو صورة أو PDF أو فيديو. يمكنك إرسال عدة أجزاء، ثم اضغط «إنهاء وحفظ».\n'
            'الحفظ يستبدل الأسئلة القديمة لهذا الأسبوع فقط.'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[back_menu()]])); return
    if data.startswith('v54_school_exam_finish|'):
        if not is_admin(uid): await query.answer('للإدارة فقط.',show_alert=True); return
        review_id=int(data.split('|')[1]); state=context.user_data.get('v54_school_exam_media') or {}
        if int(state.get('review_id') or 0)!=review_id or not state.get('items'):
            await query.answer('أرسل سؤالا واحدا على الأقل.',show_alert=True); return
        count=await db.v54_replace_school_review_exam_media(review_id,state['items'],uid)
        context.user_data.pop('v54_school_exam_media',None)
        await query.answer(f'تم حفظ {count} جزء من الأسئلة.',show_alert=True)
        await v54_school_admin_review(query,review_id); return
    return await _v54_previous_button_handler(update,context)


_v54_previous_private_messages=private_messages
async def private_messages(update,context):
    uid=update.effective_user.id; msg=update.effective_message
    add_state=context.user_data.get('v54_school_review_add')
    if add_state and is_admin(uid):
        if datetime.now(TIMEZONE)-add_state.get('started_at',datetime.now(TIMEZONE))>timedelta(minutes=30):
            context.user_data.pop('v54_school_review_add',None)
            await msg.reply_text('انتهت مهلة الإضافة. ابدأ من لوحة مراجعة المدرسة من جديد.'); return
        value=(msg.text or '').strip(); step=add_state.get('step')
        if not value:
            await msg.reply_text('أرسل نصا صحيحا لإكمال الإضافة.'); return
        if step=='week_no':
            if not value.isdigit() or not 1<=int(value)<=60:
                await msg.reply_text('أرسل رقم أسبوع من 1 إلى 60.'); return
            add_state['week_no']=int(value); add_state['step']='chapter_label'
            await msg.reply_text('📘 أرسل اسم الفصل، مثال: الفصل الثالث'); return
        if step=='chapter_label':
            add_state['chapter_label']=value; add_state['step']='topics'
            await msg.reply_text('📚 أرسل الموضوعات الداخلة في المراجعة:'); return
        if step=='topics':
            add_state['topics']=value; add_state['step']='exam_date'
            await msg.reply_text('📅 أرسل موعد الامتحان بصيغة يوم/شهر/سنة، مثال: 02/12/2026\nسينشر البوت المراجعة قبله بـ7 أيام.'); return
        try:
            exam_date=datetime.strptime(value,'%d/%m/%Y').date()
        except ValueError:
            await msg.reply_text('صيغة التاريخ غير صحيحة. أرسلها مثل: 02/12/2026'); return
        result=await db.v54_add_school_review(add_state['week_no'],add_state['chapter_label'],add_state['topics'],exam_date,uid)
        if result['status']=='exists':
            await msg.reply_text('هذا الأسبوع موجود مسبقا. اختر رقما آخر أو عدّله من الأسابيع الحالية.'); return
        if result['status']!='ok':
            await msg.reply_text('تعذر حفظ الأسبوع. راجع المعلومات وحاول مجددا.'); return
        context.user_data.pop('v54_school_review_add',None); review=result['review']
        await msg.reply_text(bold(
            f"✅ تمت إضافة الأسبوع {review['week_label']}\n"
            f"📢 النشر: {review['publish_date'].strftime('%d/%m/%Y')}\n"
            f"📝 الامتحان: {review['exam_date'].strftime('%d/%m/%Y')}"),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('🗓 عرض الأسابيع',callback_data='v54_school_catalog',style='primary')],[back_menu()]])); return
    media_state=context.user_data.get('v54_school_exam_media')
    if media_state and is_admin(uid):
        if datetime.now(TIMEZONE)-media_state.get('started_at',datetime.now(TIMEZONE))>timedelta(minutes=30):
            context.user_data.pop('v54_school_exam_media',None)
            await msg.reply_text('انتهت مهلة الإضافة. ابدأ من لوحة مراجعة المدرسة من جديد.'); return
        payload_type,file_id,content=message_payload(msg)
        if payload_type not in {'text','photo','document','video'}:
            await msg.reply_text('أرسل نصا أو صورة أو PDF أو فيديو فقط.'); return
        if payload_type=='document' and (msg.document.mime_type or '').lower()!='application/pdf':
            await msg.reply_text('الملفات المدعومة هنا PDF فقط.'); return
        media_state['items'].append({'payload_type':payload_type,'file_id':file_id,'text_content':content or ''})
        await msg.reply_text(bold(
            f"✅ أضيف الجزء رقم {len(media_state['items'])}.\nأرسل جزءا آخر أو اضغط إنهاء وحفظ."),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('✅ إنهاء وحفظ',callback_data=f"v54_school_exam_finish|{media_state['review_id']}",style='success')],
                [back_menu()]])); return
    oath=context.user_data.get('awaiting_school_review_oath')
    if oath and not is_admin(uid):
        if datetime.now(TIMEZONE)-oath.get('started_at',datetime.now(TIMEZONE))>timedelta(minutes=30):
            context.user_data.pop('awaiting_school_review_oath',None)
            await msg.reply_text('انتهت مهلة القسم. افتح مراجعة المدرسة وحاول من جديد.'); return
        if (msg.text or '').strip()!=SCHOOL_REVIEW_OATH:
            await msg.reply_text(bold('⚠️ أرسل القسم حرفيا بدون تغيير.'),parse_mode=ParseMode.HTML); return
        context.user_data.pop('awaiting_school_review_oath',None)
        result=await db.v54_complete_school_review(oath['review_id'],uid)
        if result['status']!='ok':
            await msg.reply_text(bold('هذه المراجعة غير متاحة الآن أو انتهت مدتها.'),parse_mode=ParseMode.HTML); return
        review=result['review']; task=result.get('task')
        lines=['✅ تم تسجيل إكمال مراجعة المدرسة بنجاح.']
        if result.get('xp'): lines.append('⭐ حصلت على 30 XP.')
        if task:
            sent=await _v54_notify_parent_exam(context,uid,task,review)
            if sent: await db.v54_mark_school_review_exam_notified(review['id'],uid)
            lines.append('🔐 أرسل طلب فتح الامتحان إلى ولي الأمر.')
        elif review['exam_date']>datetime.now(TIMEZONE).date():
            lines.append(f"⏳ يجهز الامتحان بتاريخ {review['exam_date'].strftime('%d/%m/%Y')} ثم يطلب موافقة ولي الأمر.")
        else: lines.append('📭 أسئلة الامتحان لم تضف بعد؛ سيجهزها البوت فور نشرها.')
        await msg.reply_text(bold('\n'.join(lines)),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📝 امتحانات مراجعة المدرسة',callback_data='v54_school_exams',style='primary')],[back_menu()]])); return
    return await _v54_previous_private_messages(update,context)


_v54_previous_eco_critical_job=v49_eco_critical_job
async def v49_eco_critical_job(context):
    await _v54_previous_eco_critical_job(context)
    await _v49_run_maintenance_step(context,'school review',v54_school_review_job)


_v54_previous_post_init=post_init
async def post_init(app):
    await _v54_previous_post_init(app)
    if not NEON_ECO_MODE:
        app.job_queue.run_repeating(v54_school_review_job,300,first=45,name='v54_school_review')
    logger.info('v54 school-review programme ready')


# ========================= v55 warning + exam-bank unlock =========================

async def v55_exam_requirements(query,definition_id):
    definition=await db.v31_exam_definition_for_admin(int(definition_id))
    if not definition:
        await query.answer('الامتحان غير موجود.',show_alert=True); return
    bundle=await db.v52_chapter_exam_bundle(query.from_user.id,int(definition['chapter']))
    exam=next((row for row in bundle.get('exams',[]) if int(row['id'])==int(definition_id)),None)
    if not exam:
        await query.answer('الامتحان غير متاح لهذا الحساب.',show_alert=True); return
    completed={tuple(item) for item in bundle.get('completed_pairs',set())}
    missing=[(int(chapter),int(lecture)) for chapter,lecture in exam.get('required',[]) if (int(chapter),int(lecture)) not in completed]
    if not missing:
        chapter,lecture=map(int,exam['required'][0])
        await query.answer(); await v52_exam_lecture(query,chapter,lecture); return
    rows=[[InlineKeyboardButton(f'📚 إكمال الفصل {chapter} — المحاضرة {lecture}',
        callback_data=f'v55_exam_study|{chapter}|{lecture}',style='success')] for chapter,lecture in missing]
    rows += [[InlineKeyboardButton('◀️ امتحانات الفصل',callback_data=f"v52_exam_chapter|{definition['chapter']}",style='primary'),back_menu()]]
    await query.answer(); await query.edit_message_text(bold(
        f"🔒 متطلبات فتح الامتحان\n{DIV}\n📝 {str(definition['title']).replace('[تراكمي] ','')}\n\n"
        'أكمل المحاضرات الظاهرة أدناه. يمكنك اعتماد كل محاضرة بالمشاهدة أو بقسم الدراسة من مصدر خاص.'),
        parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def v55_exam_study_menu(query,chapter,lecture):
    if int(chapter) not in PLAYLISTS or not 1<=int(lecture)<=len(PLAYLISTS[int(chapter)]):
        await query.answer('المحاضرة غير موجودة.',show_alert=True); return
    progress=await lecture_progress(query.from_user.id,int(chapter),int(lecture))
    if progress and progress.get('completed_at'):
        await query.answer('هذه المحاضرة مكتملة بالفعل.'); await v52_exam_lecture(query,int(chapter),int(lecture)); return
    item=PLAYLISTS[int(chapter)][int(lecture)-1]
    rows=[
        [InlineKeyboardButton('▶️ مشاهدتها من رابط البوت',callback_data=f'v55_exam_watch|{chapter}|{lecture}',style='success')],
        [InlineKeyboardButton('📚 درستها من مصدر خاص — القسم',callback_data=f'v55_exam_oath|{chapter}|{lecture}',style='primary')],
        [InlineKeyboardButton(f'◀️ محاضرات الفصل {chapter}',callback_data=f'v52_exam_chapter|{chapter}',style='primary'),back_menu()]]
    await query.answer(); await query.edit_message_text(bold(
        f"🎬 الفصل {chapter} — المحاضرة {lecture}\n{DIV}\n📌 {item[1]}\n\n"
        'اختر طريقة إكمال المحاضرة حتى تتفعل امتحاناتها.'),parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows))


_v55_previous_button_handler=button_handler
async def button_handler(update,context):
    data=update.callback_query.data or ''
    query=update.callback_query
    uid=query.from_user.id
    if data=='parent_delete':
        if await get_student(uid) or not await students_by_parent(uid,False):
            await query.answer('لا يوجد حساب ولي أمر قابل للحذف.',show_alert=True); return
        context.user_data['parent_delete_requested_at']=datetime.now(TIMEZONE)
        await query.answer()
        await query.edit_message_text('⚠️ حذف حساب ولي الأمر نهائياً\n\nسيُلغى ربطك بجميع الطلاب، ولن تُحذف بيانات أي طالب. يمكن التسجيل من البداية بعد الحذف. هل تؤكد؟',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🗑 نعم، احذف حسابي',callback_data='parent_delete_confirm',style='danger')],
                [InlineKeyboardButton('❌ تراجع',callback_data='parent_menu',style='primary')]])); return
    if data=='parent_delete_confirm':
        requested=context.user_data.pop('parent_delete_requested_at',None)
        if not requested or datetime.now(TIMEZONE)-requested>timedelta(minutes=10):
            await query.answer('انتهت مهلة التأكيد، افتح واجهة ولي الأمر وأعد الطلب.',show_alert=True); return
        result=await db.delete_parent_account(uid)
        if result['status']!='deleted':
            await query.answer('تعذر الحذف. تحقق من نوع الحساب أو أعد فتح /start.',show_alert=True); return
        context.user_data.clear()
        await query.answer('حُذف حساب ولي الأمر')
        await query.edit_message_text('✅ حُذف حساب ولي الأمر وأُلغي ربطك بجميع الطلاب، مع بقاء ملفاتهم محفوظة. اضغط البدء من جديد أو أرسل /start.',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🆕 البدء من جديد',callback_data='parent_restart',style='success')]]))
        return
    query=update.callback_query; data=query.data or ''; uid=query.from_user.id
    if data.startswith('prepcomplete|') and not is_admin(uid):
        try:
            _,ch,lec=data.split('|'); chapter,lecture=int(ch),int(lec)
        except ValueError:
            await query.answer('رابط المحاضرة غير صالح.',show_alert=True); return
        progress=await lecture_progress(uid,chapter,lecture)
        if progress and progress.get('completed_at'):
            await query.answer('هذه المحاضرة مكتملة مسبقا.',show_alert=True); return
        if not await db.lecture_opened_in_assigned_preparation(uid,chapter,lecture):
            await query.answer('افتح محاضرتك المستحقة أولا.',show_alert=True); return
        elapsed=(datetime.now(progress['opened_at'].tzinfo)-progress['opened_at']).total_seconds()
        if elapsed<MIN_LECTURE_WATCH_MINUTES*60:
            remain=max(1,int((MIN_LECTURE_WATCH_MINUTES*60-elapsed+59)//60))
            await query.answer(f'بقي نحو {remain} دقيقة من وقت التحقق.',show_alert=True); return
        await query.answer(); await query.edit_message_text(bold('🔍 هل شاهدت المحاضرة كاملة وفهمت أفكارها الأساسية؟'),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('✅ نعم، شاهدتها كاملة',callback_data=f'prepverify|{chapter}|{lecture}',style='success')],
                [InlineKeyboardButton('↩️ العودة للمحاضرة',callback_data=f'prepopen|{chapter}|{lecture}',style='primary')]])); return
    if data.startswith('prepverify|') and not is_admin(uid):
        try:
            _,ch,lec=data.split('|'); chapter,lecture=int(ch),int(lec)
        except ValueError:
            await query.answer('رابط المحاضرة غير صالح.',show_alert=True); return
        await _v52_complete_lecture(query,context,chapter,lecture); return
    if data=='menu': context.user_data.pop('awaiting_exam_bank_oath',None)
    if data.startswith('v55_exam_requirements|'):
        await v55_exam_requirements(query,int(data.split('|')[1])); return
    if data.startswith('v55_exam_study|'):
        _,chapter,lecture=data.split('|'); await v55_exam_study_menu(query,int(chapter),int(lecture)); return
    if data.startswith('v55_exam_watch|'):
        _,chapter_raw,lecture_raw=data.split('|'); chapter,lecture=int(chapter_raw),int(lecture_raw)
        if chapter not in PLAYLISTS or not 1<=lecture<=len(PLAYLISTS[chapter]):
            await query.answer('المحاضرة غير موجودة.',show_alert=True); return
        item=PLAYLISTS[chapter][lecture-1]
        await mark_lecture_progress(uid,chapter,lecture,False,'exam_bank_watch')
        rows=biology_video_buttons(chapter,lecture)+[
              [InlineKeyboardButton('✅ أكملت مشاهدة المحاضرة',callback_data=f'v55_exam_verify|{chapter}|{lecture}',style='success')],
              [InlineKeyboardButton('◀️ طرق الإكمال',callback_data=f'v55_exam_study|{chapter}|{lecture}',style='primary'),back_menu()]]
        await query.answer(); await query.edit_message_text(bold(
            f"🎬 الفصل {chapter} — المحاضرة {lecture}\n{DIV}\n{item[1]}\n\n"
            'بعد المشاهدة ارجع واضغط «أكملت مشاهدة المحاضرة».'),parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)); return
    if data.startswith('v55_exam_verify|'):
        _,chapter_raw,lecture_raw=data.split('|'); chapter,lecture=int(chapter_raw),int(lecture_raw)
        progress=await lecture_progress(uid,chapter,lecture)
        if progress and progress.get('completed_at'):
            await query.answer('هذه المحاضرة مكتملة مسبقا.'); await v52_exam_lecture(query,chapter,lecture); return
        if not progress or not progress.get('opened_at'):
            await query.answer('افتح رابط المحاضرة أولا.',show_alert=True); return
        elapsed=(datetime.now(progress['opened_at'].tzinfo)-progress['opened_at']).total_seconds()
        required=MIN_LECTURE_WATCH_MINUTES*60
        if elapsed<required:
            remain=max(1,int((required-elapsed+59)//60))
            await query.answer(f'بقي نحو {remain} دقيقة لاعتماد المشاهدة.',show_alert=True); return
        await mark_lecture_progress(uid,chapter,lecture,True,'exam_bank_watch')
        await linked_exam_dispatch_job(context); await complete_backlog(uid,chapter,lecture)
        await query.answer('تم اعتماد المحاضرة وتفعيل امتحاناتها المستحقة.',show_alert=True)
        await v52_exam_lecture(query,chapter,lecture); return
    if data.startswith('v55_exam_oath|'):
        _,chapter_raw,lecture_raw=data.split('|'); chapter,lecture=int(chapter_raw),int(lecture_raw)
        if chapter not in PLAYLISTS or not 1<=lecture<=len(PLAYLISTS[chapter]):
            await query.answer('المحاضرة غير موجودة.',show_alert=True); return
        context.user_data['awaiting_exam_bank_oath']={
            'chapter':chapter,'lecture':lecture,'started_at':datetime.now(TIMEZONE)}
        await query.answer(); await query.edit_message_text(bold(
            f"🤝 قسم اعتماد المحاضرة\n{DIV}\nأرسل النص التالي حرفيا برسالة واحدة:\n\n{PRIVATE_STUDY_OATH}"),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('◀️ إلغاء',callback_data=f'v55_exam_study|{chapter}|{lecture}',style='danger'),back_menu()]])); return
    return await _v55_previous_button_handler(update,context)


_v55_previous_private_messages=private_messages
async def private_messages(update,context):
    if 'registration' in context.user_data:
        started=context.user_data.get('registration_started_at')
        if started is None or datetime.now(TIMEZONE)-started>timedelta(minutes=30):
            context.user_data.pop('registration',None)
            context.user_data.pop('registration_started_at',None)
            await update.effective_message.reply_text('انتهت مهلة التسجيل غير المكتمل. أرسل /start حتى تبدأ من جديد.')
            return
    state=context.user_data.get('awaiting_exam_bank_oath'); uid=update.effective_user.id
    if state and not is_admin(uid):
        msg=update.effective_message
        if datetime.now(TIMEZONE)-state.get('started_at',datetime.now(TIMEZONE))>timedelta(minutes=30):
            context.user_data.pop('awaiting_exam_bank_oath',None)
            await msg.reply_text('انتهت مهلة القسم. افتح امتحانات الفصول وحاول من جديد.'); return
        if (msg.text or '').strip()!=PRIVATE_STUDY_OATH:
            await msg.reply_text(bold('⚠️ أرسل القسم حرفيا بدون تغيير.'),parse_mode=ParseMode.HTML); return
        chapter,lecture=int(state['chapter']),int(state['lecture'])
        context.user_data.pop('awaiting_exam_bank_oath',None)
        student=await get_student(uid)
        if not student or not student.get('approved') or student.get('reset_pending'):
            await msg.reply_text(bold('حساب الطالب غير مفعل.'),parse_mode=ParseMode.HTML); return
        await mark_lecture_progress(uid,chapter,lecture,True,'exam_bank_private_oath')
        await linked_exam_dispatch_job(context); await complete_backlog(uid,chapter,lecture)
        await msg.reply_text(bold(
            f"✅ تم اعتماد الفصل {chapter} — المحاضرة {lecture}.\n📝 تفعلت امتحاناتها المستحقة، والامتحان المدمج يفتح بعد إكمال بقية محاضراته."),
            parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('📝 فتح امتحانات المحاضرة',callback_data=f'v52_exam_lecture|{chapter}|{lecture}',style='success')],[back_menu()]])); return
    return await _v55_previous_private_messages(update,context)


if __name__ == "__main__":
    main()
