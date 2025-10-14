import json
import datetime
import asyncio
import logging
import os
import fcntl
import random
import pytz
import re
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters
)
import signal
import sys
from typing import Dict, List, Any, Tuple

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8466519086:AAFKIpz3d30irZH5UedMwWyIIF62QeoNJvk")
DEFAULT_GROUP_ID = -123456789

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

DATA_DIR = "group_data"
LOCK_FILE = "bot.lock"
ARMENIA_TZ = pytz.timezone('Asia/Yerevan')

os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger(__name__)

SETTING_TIMETABLE = 0 
LONG_ADDING_SUBJECT, LONG_ADDING_TASK, LONG_ADDING_DATE = range(1, 4) 

app = None
reminder_task = None
shutdown_event = asyncio.Event()
lock_file = None
last_reminder_data = {}

INITIAL_TIMETABLE: Dict[str, List[Dict[str, str]]] = {
    "Monday": [
        {"subject": "Теория вероятности", "room": "321", "type": "л"},
        {"subject": "Теория вероятности", "room": "313", "type": "пр"},
        {"subject": "Диффур", "room": "301", "type": "л"},
        {"subject": "База данных", "room": "321", "type": "л"},
    ],
    "Tuesday": [],
    "Wednesday": [
        {"subject": "Комбинаторные алгоритмы", "room": "305", "type": "пр"},
        {"subject": "Python", "room": "321", "type": "л"},
        {"subject": "Диффур", "room": "325", "type": "л", "week": "ч/н"},
        {"subject": "Диффур", "room": "321", "type": "пр", "week": "ч/н"},
    ],
    "Thursday": [
        {"subject": "Комбинаторные алгоритмы", "room": "онлайн", "type": "л"},
        {"subject": "Физика", "room": "321", "type": "л"},
    ],
    "Friday": [
        {"subject": "База данных", "room": "319", "type": "пр"},
        {"subject": "Python", "room": "319", "type": "пр"},
        {"subject": "Диффур", "room": "322", "type": "пр"},
    ],
    "Saturday": [
        {"subject": "Комбинаторные алгоритмы", "room": "онлайн", "type": "л"},
        {"subject": "Функц. программирование", "room": "321", "type": "л"},
        {"subject": "", "room": "", "type": ""},
        {"subject": "Функц. программирование", "room": "300", "type": "пр"},
    ],
    "Sunday": []
}

def escape_markdown_v2(text: str) -> str:
    text = text.replace('\\', '\\\\')
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    text = text.replace('|', '\|')
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_homework_file(chat_id: int) -> str:
    return os.path.join(DATA_DIR, f"homework_{chat_id}.json")

def get_config_file(chat_id: int) -> str:
    return os.path.join(DATA_DIR, f"config_{chat_id}.json")

def load_json_file(filename: str) -> Dict:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Error loading {filename}: {e}")
        return {}

def save_json_file(filename: str, data: Dict):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {filename}: {e}")

def load_homework(chat_id: int):
    return load_json_file(get_homework_file(chat_id))

def save_homework(chat_id: int, hw: Dict):
    save_json_file(get_homework_file(chat_id), hw)

def load_group_config(chat_id: int) -> Dict[str, Any]:
    config = load_json_file(get_config_file(chat_id))
    
    if not config or any(key not in config for key in ["reminders_enabled", "morning_reminder"]):
        config = {
            "reminders_enabled": True,
            "morning_reminder": "08:00",
            "evening_reminder": "18:00",
            "timezone": "Asia/Yerevan",
        }

    if "timetable" not in config:
        if chat_id == DEFAULT_GROUP_ID:
            config["timetable"] = INITIAL_TIMETABLE
        else:
            config["timetable"] = {}

    save_group_config(chat_id, config)
    return config

def save_group_config(chat_id: int, config: Dict[str, Any]):
    save_json_file(get_config_file(chat_id), config)

def load_group_timetable(chat_id: int) -> Dict[str, List[Dict[str, str]]]:
    config = load_group_config(chat_id)
    return config.get("timetable", {})

def save_group_timetable(chat_id: int, timetable: Dict[str, List[Dict[str, str]]]):
    config = load_group_config(chat_id)
    config["timetable"] = timetable
    save_group_config(chat_id, config)

def get_chat_id(update: Update) -> int:
    return update.effective_chat.id

def acquire_lock():
    global lock_file
    try:
        lock_file = open(LOCK_FILE, 'w')
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        return True
    except (IOError, OSError):
        if lock_file:
            lock_file.close()
        return False

def release_lock():
    global lock_file
    if lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            os.unlink(LOCK_FILE)
        except (IOError, OSError):
            pass

def get_week_type(date: datetime.date = None) -> str:
    if date is None:
        date = datetime.date.today()
    week_num = date.isocalendar()[1]
    return "ч/н" if week_num % 2 == 0 else "н/ч"

def is_lesson_this_week(lesson: Dict, date: datetime.date = None) -> bool:
    if "week" not in lesson:
        return True
    week_type = get_week_type(date)
    return lesson["week"] == week_type

def parse_flexible_date(date_str: str) -> datetime.date | str:
    today = datetime.date.today()
    date_lower = date_str.lower().strip()
    
    if date_lower in ["none", "tbd", "n/a", "undefined", "-"]:
        return "TBD"
    
    if date_lower in ["today", "սյօր", "сегодня"]:
        return today
    elif date_lower in ["tomorrow", "վաղը", "завтра"]:
        return today + datetime.timedelta(days=1)
    elif date_lower in ["next week", "հաջորդ շաբաթ", "на след неделе"]:
        return today + datetime.timedelta(days=7)
    elif re.match(r'^\+\d+$', date_lower):
        days = int(date_lower[1:])
        return today + datetime.timedelta(days=days)
    else:
        match_dd_mm = re.match(r'^(\d{1,2})[-/](\d{1,2})$', date_lower)
        if match_dd_mm:
            day, month = map(int, match_dd_mm.groups())
            target_date = datetime.date(today.year, month, day)
            if target_date < today:
                target_date = datetime.date(today.year + 1, month, day)
            return target_date
        return datetime.datetime.strptime(date_str, '%Y-%m-%d').date()

def format_deadline_status(due_date_str: str) -> Tuple[str, int, datetime.datetime]:
    """
    Returns (status_emoji_text, priority, deadline_datetime)
    Deadline is at 00:00 (midnight at start of the due date)
    Priority: lower number = higher urgency
    """
    if due_date_str == "TBD":
        return ("TBD", 999, datetime.datetime.max)
    
    try:
        due_date = datetime.datetime.strptime(due_date_str, "%Y-%m-%d").date()
        deadline_dt = datetime.datetime.combine(due_date, datetime.time.min)
        deadline_dt = ARMENIA_TZ.localize(deadline_dt)
        
        now = datetime.datetime.now(ARMENIA_TZ)
        time_left = deadline_dt - now
        
        hours_left = time_left.total_seconds() / 3600
        
        if hours_left < 0:
            days_overdue = abs(int(hours_left / 24))
            return (f"⚠️ {days_overdue}d overdue", 0, deadline_dt)
        elif hours_left < 24:
            if hours_left < 1:
                mins = int(hours_left * 60)
                return (f"🔴 {mins}min left", 1, deadline_dt)
            else:
                hrs = int(hours_left)
                return (f"🔴 {hrs}h left", 1, deadline_dt)
        elif hours_left < 48:
            return ("🟡 tomorrow", 2, deadline_dt)
        else:
            days = int(hours_left / 24)
            return (f"{days}d", 3, deadline_dt)
    except (ValueError, TypeError):
        return ("?", 998, datetime.datetime.max)

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✗ Cancelled", parse_mode='MarkdownV2')
    context.user_data.clear()
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "*Study Bot*\n\n"
        "*Homework*\n"
        "`/hw_add Subject \\| Task \\| Date`\n"
        "`/hw_long_add` \\- interactive\n"
        "`/hw_list` \\- all homework\n"
        "`/hw_remove <subj> <id>`\n"
        "`/hw_today`, `/hw_overdue`\n"
        "`/hw_stats`, `/hw_clean`\n\n"
        "*Schedule*\n"
        "`/timetable` \\- today\n"
        "`/full_timetable` \\- week\n"
        "`/set_timetable` \\- edit\n"
        "`/next` \\- next lesson\n\n"
        "_Date: tomorrow, \\+3, 15\\-12, TBD_\n"
        "_Deadlines are at 00:00 on the due date_"
    )
    await update.message.reply_text(msg, parse_mode='MarkdownV2')

async def hw_quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    
    if not context.args:
        await update.message.reply_text(
            "Usage: `/hw_add Subject \\| Task \\| Date`\n"
            "Example: `/hw_add Python \\| Ex 5 \\| tomorrow`",
            parse_mode='MarkdownV2'
        )
        return
    
    full_text = " ".join(context.args).replace('\\|', '|').strip()
    parts = [p.strip() for p in full_text.split('|')]
    
    if len(parts) < 3:
        await update.message.reply_text(
            "Format: `Subject \\| Task \\| Date`",
            parse_mode='MarkdownV2'
        )
        return
    
    subject, task, date_str = parts[0], parts[1], parts[2]
    
    try:
        due_date_or_tbd = parse_flexible_date(date_str)
    except ValueError:
        await update.message.reply_text("Invalid date format", parse_mode='MarkdownV2')
        return
    
    if due_date_or_tbd == "TBD":
        due_iso = "TBD"
        status_text = "TBD"
    else:
        due_iso = due_date_or_tbd.isoformat()
        status_text, _, _ = format_deadline_status(due_iso)
    
    hw = load_homework(chat_id)
    hw_item = {
        "task": task,
        "due": due_iso,
        "added": datetime.date.today().isoformat()
    }
    
    hw.setdefault(subject, []).append(hw_item)
    save_homework(chat_id, hw)
    
    task_preview = task[:80] if len(task) <= 80 else task[:80] + "..."
    
    await update.message.reply_text(
        f"✓ *{escape_markdown_v2(subject)}*\n"
        f"{escape_markdown_v2(task_preview)}\n"
        f"Due: {escape_markdown_v2(status_text)}", 
        parse_mode='MarkdownV2'
    )

async def hw_long_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Subject? \\(or /cancel\\)",
        parse_mode='MarkdownV2'
    )
    return LONG_ADDING_SUBJECT

async def get_subject_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = update.message.text.strip()
    context.user_data['temp_subject'] = subject
    if context.args:
        context.args.clear()
    
    await update.message.reply_text(
        f"✓ {escape_markdown_v2(subject)}\nTask?", 
        parse_mode='MarkdownV2'
    )
    return LONG_ADDING_TASK

async def get_task_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = update.message.text.strip()
    context.user_data['temp_task'] = task
    if context.args:
        context.args.clear()
    
    await update.message.reply_text(
        "✓ Task saved\nDue date? \\(tomorrow, \\+3, 15\\-12, TBD\\)",
        parse_mode='MarkdownV2'
    )
    return LONG_ADDING_DATE

async def get_date_and_save_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    if context.args:
        context.args.clear()
    
    if 'temp_subject' not in context.user_data or 'temp_task' not in context.user_data:
        await update.message.reply_text("Error\\. Restart with /hw_long_add", parse_mode='MarkdownV2')
        context.user_data.clear()
        return ConversationHandler.END

    subject = context.user_data['temp_subject']
    task = context.user_data['temp_task']
    chat_id = get_chat_id(update)

    try:
        due_date_or_tbd = parse_flexible_date(date_str)
    except ValueError:
        await update.message.reply_text(
            "Invalid date\\. Try again or /cancel",
            parse_mode='MarkdownV2'
        )
        return LONG_ADDING_DATE

    if due_date_or_tbd == "TBD":
        due_iso = "TBD"
        status_text = "TBD"
    else:
        due_iso = due_date_or_tbd.isoformat()
        status_text, _, _ = format_deadline_status(due_iso)

    hw = load_homework(chat_id)
    hw_item = {
        "task": task,
        "due": due_iso,
        "added": datetime.date.today().isoformat()
    }
    
    hw.setdefault(subject, []).append(hw_item)
    save_homework(chat_id, hw)
    
    task_preview = task[:60] if len(task) <= 60 else task[:60] + "..."
    await update.message.reply_text(
        f"✓ *{escape_markdown_v2(subject)}*\n"
        f"{escape_markdown_v2(task_preview)}\n"
        f"Due: {escape_markdown_v2(status_text)}",
        parse_mode='MarkdownV2'
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def hw_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    hw = load_homework(chat_id)
    
    if not hw:
        await update.message.reply_text("No homework", parse_mode='MarkdownV2')
        return
    
    total = sum(len(tasks) for tasks in hw.values())
    overdue = due_today = due_tomorrow = tbd_count = 0
    
    now = datetime.datetime.now(ARMENIA_TZ)
    
    for tasks in hw.values():
        for task in tasks:
            if task["due"] == "TBD":
                tbd_count += 1
                continue
            
            status_text, priority, _ = format_deadline_status(task["due"])
            if priority == 0:
                overdue += 1
            elif priority == 1:
                due_today += 1
            elif priority == 2:
                due_tomorrow += 1
    
    msg = (
        f"*Stats*\n\n"
        f"Total: {total}\n"
        f"Overdue: {overdue}\n"
        f"Due today: {due_today}\n"
        f"Due tomorrow: {due_tomorrow}\n"
        f"TBD: {tbd_count}"
    )
    
    await update.message.reply_text(msg, parse_mode='MarkdownV2')

async def hw_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    hw = load_homework(chat_id)
    
    if not hw:
        await update.message.reply_text("No homework", parse_mode='MarkdownV2')
        return
    
    cutoff = datetime.date.today() - datetime.timedelta(days=30)
    cleaned = 0
    
    for subject in list(hw.keys()):
        keep = []
        for task in hw[subject]:
            if task["due"] == "TBD":
                keep.append(task)
                continue

            try:
                due = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                if due >= cutoff:
                    keep.append(task)
                else:
                    cleaned += 1
            except ValueError:
                keep.append(task)
        
        if keep:
            hw[subject] = keep
        else:
            del hw[subject]
    
    save_homework(chat_id, hw)
    msg = f"✓ Cleaned {cleaned} old items" if cleaned > 0 else "Nothing to clean"
    await update.message.reply_text(msg, parse_mode='MarkdownV2')

async def hw_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    hw = load_homework(chat_id)
    
    today_hw = []
    for subj, tasks in hw.items():
        for task in tasks:
            status_text, priority, _ = format_deadline_status(task["due"])
            if priority == 1:
                today_hw.append((subj, task, status_text))
    
    if not today_hw:
        await update.message.reply_text("Nothing due today", parse_mode='MarkdownV2')
        return
    
    msg = "*Due Today*\n\n"
    for subj, task, status in today_hw:
        preview = task['task'][:60] if len(task['task']) <= 60 else task['task'][:60] + "..."
        msg += f"*{escape_markdown_v2(subj)}* {escape_markdown_v2(status)}\n{escape_markdown_v2(preview)}\n\n"
    
    await update.message.reply_text(msg, parse_mode='MarkdownV2')

async def hw_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    hw = load_homework(chat_id)
    
    if not hw:
        await update.message.reply_text("No homework", parse_mode='MarkdownV2')
        return
    
    overdue = []
    
    for subj, tasks in hw.items():
        for task in tasks:
            status_text, priority, deadline_dt = format_deadline_status(task["due"])
            if priority == 0:
                overdue.append((subj, task, status_text, deadline_dt))
    
    if not overdue:
        await update.message.reply_text("✓ Nothing overdue", parse_mode='MarkdownV2')
        return
    
    overdue.sort(key=lambda x: x[3])
    msg = f"*Overdue \\({len(overdue)}\\)*\n\n"
    
    for subj, task, status, _ in overdue[:10]:
        preview = task['task'][:50] if len(task['task']) <= 50 else task['task'][:50] + "..."
        msg += f"*{escape_markdown_v2(subj)}* {escape_markdown_v2(status)}\n{escape_markdown_v2(preview)}\n\n"
    
    if len(overdue) > 10:
        msg += f"_\\.\\.\\. {len(overdue) - 10} more_"
    
    await update.message.reply_text(msg, parse_mode='MarkdownV2')

async def hw_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    hw = load_homework(chat_id)
    
    if not hw:
        await update.message.reply_text("No homework", parse_mode='MarkdownV2')
        return
    
    msg = "*Homework*\n\n"
    
    for idx, subj in enumerate(sorted(hw.keys()), 1):
        msg += f"*{idx}\\. {escape_markdown_v2(subj)}*\n"
        
        tasks_info = []
        for i, task in enumerate(hw[subj], 1):
            status_text, priority, deadline_dt = format_deadline_status(task["due"])
            tasks_info.append((i, task, status_text, priority, deadline_dt))
        
        tasks_info.sort(key=lambda x: (x[3], x[4]))
        
        for i, task, status, _, _ in tasks_info:
            preview = task['task'][:70] if len(task['task']) <= 70 else task['task'][:70] + "..."
            msg += f"   `{i}` {escape_markdown_v2(preview)} {escape_markdown_v2(status)}\n"
        msg += "\n"
    
    await update.message.reply_text(msg, parse_mode='MarkdownV2')

async def hw_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/hw_remove <subj> <id>`",
            parse_mode='MarkdownV2'
        )
        return

    subj_input, idx_str = context.args[0], context.args[1]
    
    try:
        hw_idx = int(idx_str) - 1
    except ValueError:
        await update.message.reply_text("Invalid index", parse_mode='MarkdownV2')
        return

    hw = load_homework(chat_id)
    if not hw:
        await update.message.reply_text("No homework", parse_mode='MarkdownV2')
        return

    subject = None
    try:
        subj_idx = int(subj_input) - 1
        sorted_subj = sorted(hw.keys())
        if 0 <= subj_idx < len(sorted_subj):
            subject = sorted_subj[subj_idx]
    except ValueError:
        if subj_input in hw:
            subject = subj_input
    
    if not subject or subject not in hw:
        await update.message.reply_text("Subject not found", parse_mode='MarkdownV2')
        return
    
    if hw_idx < 0 or hw_idx >= len(hw[subject]):
        await update.message.reply_text("Invalid index", parse_mode='MarkdownV2')
        return

    removed = hw[subject].pop(hw_idx)
    if not hw[subject]:
        del hw[subject]
    
    save_homework(chat_id, hw)
    
    preview = removed['task'][:60] if len(removed['task']) <= 60 else removed['task'][:60] + "..."
    await update.message.reply_text(
        f"✓ Removed\n{escape_markdown_v2(preview)}", 
        parse_mode='MarkdownV2'
    )

async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    schedule = load_group_timetable(chat_id)
    
    if not schedule:
        await update.message.reply_text(
            "No timetable\\. Use /set\\_timetable",
            parse_mode='MarkdownV2'
        )
        return
    
    today = datetime.date.today()
    day_name = today.strftime('%A')
    
    if day_name not in schedule or not schedule[day_name]:
        await update.message.reply_text(
            f"*{escape_markdown_v2(day_name)}*\nNo lessons", 
            parse_mode='MarkdownV2'
        )
        return
    
    week_type = get_week_type(today)
    msg = f"*{escape_markdown_v2(day_name)}* \\({week_type}\\)\n\n"
    
    displayed = 0
    for i, lesson in enumerate(schedule[day_name], 1):
        if not is_lesson_this_week(lesson, today):
            continue
        
        subj = lesson.get("subject", "").strip()
        room = lesson.get("room", "").strip()
        ltype = lesson.get("type", "").strip()
        
        if not subj:
            continue
        
        displayed += 1
        msg += f"`{i}` {escape_markdown_v2(subj)}"
        
        if ltype:
            msg += f" \\({escape_markdown_v2(ltype)}\\)"
        if room:
            msg += f" \\- {escape_markdown_v2(room)}"
        msg += "\n"
    
    if displayed == 0:
        await update.message.reply_text(
            f"*{escape_markdown_v2(day_name)}*\nNo lessons this week", 
            parse_mode='MarkdownV2'
        )
        return
    
    await update.message.reply_text(msg, parse_mode='MarkdownV2')

async def full_timetable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    schedule = load_group_timetable(chat_id)
    
    if not schedule:
        await update.message.reply_text(
            "No timetable\\. Use /set\\_timetable",
            parse_mode='MarkdownV2'
        )
        return
    
    today = datetime.date.today()
    week_type = get_week_type(today)
    msg = f"*Weekly Schedule* \\({week_type}\\)\n\n"
    
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    
    for day in days:
        if day not in schedule or not schedule[day]:
            continue
        
        msg += f"*{escape_markdown_v2(day)}*\n"
        
        for i, lesson in enumerate(schedule[day], 1):
            subj = lesson.get("subject", "").strip()
            room = lesson.get("room", "").strip()
            ltype = lesson.get("type", "").strip()
            week = lesson.get("week", "").strip()
            
            if not subj:
                continue
            
            msg += f"   `{i}` {escape_markdown_v2(subj)}"
            
            if ltype:
                msg += f" \\({escape_markdown_v2(ltype)}\\)"
            if room:
                msg += f" \\- {escape_markdown_v2(room)}"
            if week:
                msg += f" \\[{escape_markdown_v2(week)}\\]"
            msg += "\n"
        msg += "\n"
    
    await update.message.reply_text(msg, parse_mode='MarkdownV2')

async def set_timetable_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Set from JSON", callback_data='timetable_json')],
        [InlineKeyboardButton("Cancel", callback_data='timetable_cancel')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "*Timetable Setup*\n\nSend JSON format",
        reply_markup=reply_markup,
        parse_mode='MarkdownV2'
    )
    return SETTING_TIMETABLE

async def timetable_json_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "Send timetable as JSON\n\n"
        "Format:\n"
        "```json\n"
        '{"Monday": [{"subject": "Math", "room": "101", "type": "л"}]}\n'
        "```\n"
        "/cancel to abort",
        parse_mode='MarkdownV2'
    )
    return SETTING_TIMETABLE

async def receive_timetable_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    text = update.message.text.strip()
    
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    
    try:
        new_schedule = json.loads(text)
        
        if not isinstance(new_schedule, dict):
            await update.message.reply_text("Invalid format", parse_mode='MarkdownV2')
            return SETTING_TIMETABLE
        
        save_group_timetable(chat_id, new_schedule)
        await update.message.reply_text("✓ Timetable updated", parse_mode='MarkdownV2')
        return ConversationHandler.END
        
    except json.JSONDecodeError:
        await update.message.reply_text("Invalid JSON\\. Try again or /cancel", parse_mode='MarkdownV2')
        return SETTING_TIMETABLE

async def timetable_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✗ Cancelled", parse_mode='MarkdownV2')
    return ConversationHandler.END

async def next_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    schedule = load_group_timetable(chat_id)
    
    if not schedule:
        await update.message.reply_text("No timetable", parse_mode='MarkdownV2')
        return
    
    now = datetime.datetime.now(ARMENIA_TZ)
    today = now.date()
    day_name = today.strftime('%A')
    
    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    current_day_idx = days_order.index(day_name)
    
    for offset in range(7):
        check_day_idx = (current_day_idx + offset) % 7
        check_day = days_order[check_day_idx]
        check_date = today + datetime.timedelta(days=offset)
        
        if check_day in schedule and schedule[check_day]:
            for lesson in schedule[check_day]:
                if is_lesson_this_week(lesson, check_date):
                    subj = lesson.get("subject", "").strip()
                    room = lesson.get("room", "").strip()
                    ltype = lesson.get("type", "").strip()
                    
                    if not subj:
                        continue
                    
                    msg = f"*Next: {escape_markdown_v2(subj)}*"
                    
                    if ltype:
                        msg += f" \\({escape_markdown_v2(ltype)}\\)"
                    if room:
                        msg += f"\n{escape_markdown_v2(room)}"
                    
                    if offset == 0:
                        msg += f"\nToday"
                    elif offset == 1:
                        msg += f"\nTomorrow"
                    else:
                        msg += f"\n{escape_markdown_v2(check_day)}"
                    
                    await update.message.reply_text(msg, parse_mode='MarkdownV2')
                    return
    
    await update.message.reply_text("No upcoming lessons", parse_mode='MarkdownV2')

async def motivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quotes = [
        "soberis tryapka",
        "ուզում ես մոդուլը գա վատ գրես նեղվես հետո նոր ուշքի գաս հա արա՞՞՞՞՞՞՞",
        "ape heraxosd shprti dasd ara", 
        "hishi vor mard ka qeznic poqr a u arden senior a",
        "Нечетное число - это НЕ четное число",
        "Եթե չես կարում ասես ուրեմն չգիտես:",
        "Меня не интересуют твои примеры. Доказывай.",
        "Конечно могу, это же я написал.",
        "Я ответил на ваш вопрос?",
        "es im vaxtov jamy 4in ei zartnum vor matanaliz anei",
        "porsche es uzum? de sovori (iharke eskortnicayi tarberaky misht ka bayc du sovori)",
    ]
    
    await update.message.reply_text(escape_markdown_v2(random.choice(quotes)), parse_mode='MarkdownV2')

async def kys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    messages = [
        "nigga?",
        "hambal",
        "а ты не только зашел???",
        "likvid.",
        "es el qez em sirum", 
        "poshol naxuy",
    ]
    await update.message.reply_text(escape_markdown_v2(random.choice(messages)), parse_mode='MarkdownV2')

async def send_reminder_to_group(app: Application, chat_id: int, message: str):
    """Send reminder with error handling"""
    try:
        await app.bot.send_message(chat_id=chat_id, text=message, parse_mode='MarkdownV2')
        logger.info(f"Reminder sent to {chat_id}")
    except Exception as e:
        logger.error(f"Failed to send reminder to {chat_id}: {e}")

async def check_and_send_reminders():
    """Check and send reminders based on time"""
    global app, last_reminder_data
    
    if not app:
        return
    
    try:
        now = datetime.datetime.now(ARMENIA_TZ)
        current_time = now.strftime("%H:%M")
        today = now.date()
        tomorrow = today + datetime.timedelta(days=1)
        
        for filename in os.listdir(DATA_DIR):
            if not filename.startswith("config_"):
                continue
            
            try:
                chat_id = int(filename.replace("config_", "").replace(".json", ""))
            except ValueError:
                continue
                
            config = load_group_config(chat_id)
            
            if not config.get("reminders_enabled", True):
                continue
            
            morning_time = config.get("morning_reminder", "08:00")
            evening_time = config.get("evening_reminder", "18:00")
            
            reminder_key = f"{chat_id}_{current_time}_{today.isoformat()}"
            
            if reminder_key in last_reminder_data:
                continue
            
            # Morning reminder: Today's lessons
            if current_time == morning_time:
                schedule = load_group_timetable(chat_id)
                day_name = today.strftime('%A')
                
                if day_name in schedule and schedule[day_name]:
                    lessons_today = []
                    for lesson in schedule[day_name]:
                        if is_lesson_this_week(lesson, today):
                            subj = lesson.get("subject", "").strip()
                            room = lesson.get("room", "").strip()
                            ltype = lesson.get("type", "").strip()
                            
                            if subj:
                                lesson_info = subj
                                if ltype:
                                    lesson_info += f" ({ltype})"
                                if room:
                                    lesson_info += f" - {room}"
                                lessons_today.append(lesson_info)
                    
                    if lessons_today:
                        msg = f"🌅 *Today's Lessons*\n\n"
                        for i, lesson_info in enumerate(lessons_today, 1):
                            msg += f"`{i}` {escape_markdown_v2(lesson_info)}\n"
                        
                        await send_reminder_to_group(app, chat_id, msg)
                        last_reminder_data[reminder_key] = True
            
            # Evening reminder: Homework due tomorrow at 00:00
            elif current_time == evening_time:
                hw = load_homework(chat_id)
                
                # Find homework due tomorrow (deadline is at 00:00 tomorrow)
                tomorrow_hw = []
                for subj, tasks in hw.items():
                    for task in tasks:
                        if task["due"] == tomorrow.isoformat():
                            tomorrow_hw.append((subj, task))
                
                if tomorrow_hw:
                    msg = f"🌙 *Due Tomorrow at 00:00*\n\n"
                    for subj, task in tomorrow_hw[:5]:
                        preview = task['task'][:60] if len(task['task']) <= 60 else task['task'][:60] + "..."
                        msg += f"*{escape_markdown_v2(subj)}*\n{escape_markdown_v2(preview)}\n\n"
                    
                    if len(tomorrow_hw) > 5:
                        msg += f"_\\.\\.\\. {len(tomorrow_hw) - 5} more_"
                    
                    await send_reminder_to_group(app, chat_id, msg)
                    last_reminder_data[reminder_key] = True
        
        # Clean old reminder data
        keys_to_remove = [k for k in last_reminder_data.keys() if k.split('_')[-1] != today.isoformat()]
        for k in keys_to_remove:
            del last_reminder_data[k]
    
    except Exception as e:
        logger.error(f"Error in reminders: {e}", exc_info=True)

async def reminder_loop():
    """Main reminder loop with graceful shutdown"""
    logger.info("Reminder loop started")
    while not shutdown_event.is_set():
        try:
            await check_and_send_reminders()
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("Reminder loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in reminder loop: {e}", exc_info=True)
            await asyncio.sleep(60)
    logger.info("Reminder loop stopped")

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    shutdown_event.set()

async def post_init(application: Application):
    """Initialize bot after startup"""
    global app, reminder_task
    app = application
    
    commands = [
        BotCommand("start", "Help"),
        BotCommand("hw_add", "Add homework"),
        BotCommand("hw_long_add", "Interactive add"),
        BotCommand("hw_list", "List homework"),
        BotCommand("hw_remove", "Remove homework"),
        BotCommand("hw_today", "Due today"),
        BotCommand("hw_overdue", "Overdue"),
        BotCommand("hw_stats", "Statistics"),
        BotCommand("hw_clean", "Clean old"),
        BotCommand("timetable", "Today's schedule"),
        BotCommand("full_timetable", "Week schedule"),
        BotCommand("set_timetable", "Edit timetable"),
        BotCommand("next", "Next lesson"),
        BotCommand("motivate", "Motivation"),
        BotCommand("kys", "Random"),
    ]
    
    await application.bot.set_my_commands(commands)
    reminder_task = asyncio.create_task(reminder_loop())
    logger.info("Bot initialized successfully")

async def post_shutdown(application: Application):
    """Cleanup on shutdown"""
    global reminder_task
    logger.info("Shutting down bot...")
    shutdown_event.set()
    
    if reminder_task:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
    
    logger.info("Bot shutdown complete")

def main():
    global app
    
    if not acquire_lock():
        logger.error("Another instance is already running")
        sys.exit(1)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
        
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("hw_add", hw_quick_add))
        app.add_handler(CommandHandler("hw_list", hw_list))
        app.add_handler(CommandHandler("hw_remove", hw_remove))
        app.add_handler(CommandHandler("hw_today", hw_today))
        app.add_handler(CommandHandler("hw_overdue", hw_overdue))
        app.add_handler(CommandHandler("hw_stats", hw_stats))
        app.add_handler(CommandHandler("hw_clean", hw_clean))
        app.add_handler(CommandHandler("timetable", timetable))
        app.add_handler(CommandHandler("full_timetable", full_timetable))
        app.add_handler(CommandHandler("next", next_lesson))
        app.add_handler(CommandHandler("motivate", motivate))
        app.add_handler(CommandHandler("kys", kys))
        
        long_add_handler = ConversationHandler(
            entry_points=[CommandHandler("hw_long_add", hw_long_add_start)],
            states={
                LONG_ADDING_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_subject_long)],
                LONG_ADDING_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_long)],
                LONG_ADDING_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date_and_save_long)],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
        )
        app.add_handler(long_add_handler)
        
        timetable_handler = ConversationHandler(
            entry_points=[CommandHandler("set_timetable", set_timetable_start)],
            states={
                SETTING_TIMETABLE: [
                    CallbackQueryHandler(timetable_json_prompt, pattern='^timetable_json'),
                    CallbackQueryHandler(timetable_cancel, pattern='^timetable_cancel'),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_timetable_json),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
        )
        app.add_handler(timetable_handler)
        
        logger.info("Starting bot...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        release_lock()
        logger.info("Bot stopped")