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
from typing import Dict, List, Any

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8466519086:AAFKIpz3d30irZH5UedMwWyIIF62QeoNJvk")
DEFAULT_GROUP_ID = -123456789

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required. Set it with: export TELEGRAM_BOT_TOKEN='your_token_here'")

DATA_DIR = "group_data"
LOCK_FILE = "bot.lock"
ARMENIA_TZ = pytz.timezone('Asia/Yerevan')

os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
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
        {"subject": "Физкультура", "room": "спортзал", "type": ""},
        {"subject": "Физкультура", "room": "спортзал", "type": ""},
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
        logger.error(f"Error loading {filename} (File corruption/Invalid JSON): {e}", exc_info=True)
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
            "evening_reminder": "16:00",
            "timezone": "Asia/Yerevan",
        }

    if "timetable" not in config:
        if chat_id == DEFAULT_GROUP_ID:
            config["timetable"] = INITIAL_TIMETABLE
            logger.info(f"Initialized timetable for DEFAULT_GROUP_ID ({chat_id})")
        else:
            config["timetable"] = {}
            logger.info(f"Initialized empty timetable for new group {chat_id}")

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
        try:
            days = int(date_lower[1:])
            return today + datetime.timedelta(days=days)
        except ValueError:
            raise ValueError(f"Invalid relative date: {date_str}")
    else:
        match_dd_mm = re.match(r'^(\d{1,2})[-/](\d{1,2})$', date_lower)
        if match_dd_mm:
            day, month = map(int, match_dd_mm.groups())
            try:
                target_date = datetime.date(today.year, month, day)
                if target_date < today:
                    target_date = datetime.date(today.year + 1, month, day)
                return target_date
            except ValueError:
                raise ValueError(f"Invalid date: {date_str}. Day or month out of range.")
        
        return datetime.datetime.strptime(date_str, '%Y-%m-%d').date()

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operation cancelled\\.", parse_mode='MarkdownV2')
    context.user_data.clear()
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    chat_type = update.effective_chat.type
    
    welcome_msg = (
        f"Study Bot \\(Group: {chat_id}\\)\n\n"
        f"*Homework System \\(Dual Mode\\)*\n"
        f"📚 `/hw_add Subject \\| Task \\| Date` \\- *Quick Add* in one line\\.\n"
        f"   _Date can be `tomorrow`, `DD\\-MM`, `YYYY\\-MM\\-DD`, or `TBD`_\\.\n"
        f"   _Example: `/hw_add Python \\| Finish exercise 5 \\| 20\\-11`_\n"
        f"✍️ `/hw_long_add` \\- *Interactive Add* with step\\-by\\-step guidance\\.\n\n"
        f"*Timetable Management \\(Group\\-Specific\\)*\n"
        f"📅 `/timetable` \\- Show today's schedule for this group\\.\n"
        f"🗓️ `/full_timetable` \\- Show the full weekly schedule for this group\\.\n" 
        f"📌 `/set_timetable` \\- Start the process to set a new timetable for this group\\.\n\n"
        f"*Other Commands:*\n"
        f"/hw\\_list, /hw\\_remove, /hw\\_today, /hw\\_overdue, /hw\\_stats, /hw\\_clean, /next, /motivate, /kys"
    )
    await update.message.reply_text(welcome_msg, parse_mode='MarkdownV2')

async def hw_quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "⚠️ *Invalid format\\.* \n"
            "Use: `/hw_add Subject \\| Task \\| Date`\n\n"
            "Example:\n"
            "/hw_add Python \\| Create API client \\| TBD\n\n"
            "For step-by-step guidance, use `/hw_long_add`", 
            parse_mode='MarkdownV2'
        )
        return
    
    full_text = " ".join(context.args)
    full_text_clean = full_text.replace('\\|', '|').strip()
    parts = [p.strip() for p in full_text_clean.split('|')]
    
    if len(parts) < 3:
        await update.message.reply_text(
            "⚠️ *Format:* `Subject \\| Task \\| Date`\n"
            "Please use the vertical bar `\\|` to separate the 3 parts\\.",
            parse_mode='MarkdownV2'
        )
        return
    
    subject, task, date_str = parts[0], parts[1], parts[2]
    
    try:
        due_date_or_tbd = parse_flexible_date(date_str)
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ *Invalid date:* {escape_markdown_v2(date_str)}\n"
            f"Error: {escape_markdown_v2(str(e))}\n"
            "Use: `tomorrow`, `+N`, `DD\\-MM`, `YYYY\\-MM\\-DD`, or *`TBD`*",
            parse_mode='MarkdownV2'
        )
        return
    
    if due_date_or_tbd == "TBD":
        due_iso = "TBD"
        due_display = "*Undefined*"
    else:
        due_iso = due_date_or_tbd.isoformat()
        due_display = due_date_or_tbd.strftime('%Y-%m-%d (%A)')
    
    hw = load_homework(chat_id)
    hw_item = {
        "task": task,
        "due": due_iso,
        "added": datetime.date.today().isoformat()
    }
    
    hw.setdefault(subject, []).append(hw_item)
    save_homework(chat_id, hw)
    
    task_preview = task[:100] + "..." if len(task) > 100 else task
    
    await update.message.reply_text(
        f"✅ *Homework added\\!*\n\n"
        f"*{escape_markdown_v2(subject)}*\n"
        f"Task: {escape_markdown_v2(task_preview)}\n"
        f"Due: {escape_markdown_v2(due_display)}", 
        parse_mode='MarkdownV2'
    )
    logger.info(f"Quickly added homework in group {chat_id}: {subject} - {task[:50]}...")

async def hw_long_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 *Starting Interactive Homework Add*\\.\nWhat is the *Subject* of the homework\\? \\(e\\.g\\. Python, Math, History\\)\nSend /cancel to stop\\.", parse_mode='MarkdownV2')
    return LONG_ADDING_SUBJECT

async def get_subject_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = update.message.text.strip()
    context.user_data['temp_subject'] = subject
    
    if context.args:
         context.args.clear()
         
    await update.message.reply_text(
        f"✅ Subject set to *{escape_markdown_v2(subject)}*\\.\n\n"
        "Now, what is the *Task*\\? \\(e\\.g\\. Finish exercise 5, Read chapter 2\\)", 
        parse_mode='MarkdownV2'
    )
    return LONG_ADDING_TASK

async def get_task_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = update.message.text.strip()
    context.user_data['temp_task'] = task
    
    if context.args:
         context.args.clear()
         
    await update.message.reply_text(
        "✅ Task saved\\.\n\n"
        "Finally, what is the *Due Date*\\?\n"
        "Use formats like: `tomorrow`, `+3 days`, `15\\-10`, `2025\\-10\\-15` or *`TBD`* for an undefined date\\.",
        parse_mode='MarkdownV2'
    )
    return LONG_ADDING_DATE

async def get_date_and_save_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    
    if context.args:
         context.args.clear()
         
    if 'temp_subject' not in context.user_data or 'temp_task' not in context.user_data:
        await update.message.reply_text("⚠️ An error occurred\\. Please start over with /hw_long_add\\.", parse_mode='MarkdownV2')
        context.user_data.clear()
        return ConversationHandler.END

    subject = context.user_data['temp_subject']
    task = context.user_data['temp_task']
    chat_id = get_chat_id(update)

    try:
        due_date_or_tbd = parse_flexible_date(date_str)
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ *Invalid date format:* {escape_markdown_v2(date_str)}\n"
            f"Error: {escape_markdown_v2(str(e))}\n"
            "Please try again with a valid date format \\(e\\.g\\. `tomorrow`, `15\\-10`, `+5`, *`TBD`*\\) or /cancel\\.",
            parse_mode='MarkdownV2'
        )
        return LONG_ADDING_DATE

    if due_date_or_tbd == "TBD":
        due_iso = "TBD"
        due_display = "*Undefined*"
    else:
        due_iso = due_date_or_tbd.isoformat()
        due_display = due_date_or_tbd.strftime('%Y-%m-%d (%A)')

    hw = load_homework(chat_id)
    hw_item = {
        "task": task,
        "due": due_iso,
        "added": datetime.date.today().isoformat()
    }
    
    hw.setdefault(subject, []).append(hw_item)
    save_homework(chat_id, hw)
    
    task_preview = task[:50] + "..." if len(task) > 50 else task
    await update.message.reply_text(
        f"🎉 *Homework Saved Successfully!* \n\n"
        f"*{escape_markdown_v2(subject)}*\n"
        f"Task: {escape_markdown_v2(task_preview)}\n"
        f"Due: {escape_markdown_v2(due_display)}",
        parse_mode='MarkdownV2'
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def hw_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_chat_id(update)
        hw = load_homework(chat_id)
        
        if not hw:
            await update.message.reply_text("No homework logged", parse_mode='MarkdownV2')
            return
        
        total = sum(len(tasks) for tasks in hw.values())
        overdue = due_today = due_tomorrow = tbd_count = 0
        today = datetime.date.today()
        tomorrow = today + datetime.timedelta(days=1)
        
        for tasks in hw.values():
            for task in tasks:
                if task["due"] == "TBD":
                    tbd_count += 1
                    continue
                
                try:
                    due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due_date < today:
                        overdue += 1
                    elif due_date == today:
                        due_today += 1
                    elif due_date == tomorrow:
                        due_tomorrow += 1
                except ValueError:
                    pass
        
        msg = (f"📊 *Homework Statistics:*\n\n"
               f"Total: {total}\n"
               f"Overdue: {overdue}\n"
               f"Due today: {due_today}\n"
               f"Due tomorrow: {due_tomorrow}\n"
               f"Undefined Date: {tbd_count}")
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in hw_stats: {e}")
        await update.message.reply_text("Error getting statistics", parse_mode='MarkdownV2')

async def hw_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_chat_id(update)
        hw = load_homework(chat_id)
        
        if not hw:
            await update.message.reply_text("No homework found", parse_mode='MarkdownV2')
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
        msg = f"🧹 Cleaned {cleaned} old assignments" if cleaned > 0 else "Nothing to clean"
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in hw_clean: {e}")
        await update.message.reply_text("Error cleaning homework", parse_mode='MarkdownV2')

async def hw_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_chat_id(update)
        hw = load_homework(chat_id)
        today = datetime.date.today().isoformat()
        
        today_hw = [(s, t) for s, tasks in hw.items() for t in tasks if t["due"] == today]
        
        if not today_hw:
            await update.message.reply_text("No homework due today", parse_mode='MarkdownV2')
            return
        
        msg = "📅 *Due today:*\n\n"
        for i, (subj, task) in enumerate(today_hw, 1):
            preview = task['task'][:80] + "..." if len(task['task']) > 80 else task['task']
            msg += f"{i}\\. *{escape_markdown_v2(subj)}*: {escape_markdown_v2(preview)}\n\n"
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in hw_today: {e}")
        await update.message.reply_text("Error getting today's homework", parse_mode='MarkdownV2')

async def hw_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_chat_id(update)
        hw = load_homework(chat_id)
        
        if not hw:
            await update.message.reply_text("No homework", parse_mode='MarkdownV2')
            return
        
        today = datetime.date.today()
        overdue = []
        
        for subj, tasks in hw.items():
            for task in tasks:
                if not task.get("due") or task["due"] == "TBD":
                    continue
                
                try:
                    due = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due < today:
                        days = (today - due).days
                        overdue.append((subj, task, days, due))
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid date format in homework: {task.get('due')} - {e}")
                    continue
        
        if not overdue:
            await update.message.reply_text("✅ No overdue homework", parse_mode='MarkdownV2')
            return
        
        overdue.sort(key=lambda x: x[3])
        msg = f"⚠️ *Overdue \\({len(overdue)}\\):*\n\n"
        
        for i, (subj, task, days, _) in enumerate(overdue, 1):
            task_text = task.get('task', 'No description')
            preview = task_text[:60] + "..." if len(task_text) > 60 else task_text
            due_date = task.get('due', 'Unknown')
            msg += f"{i}\\. *{escape_markdown_v2(subj)}*: {escape_markdown_v2(preview)}\n   {escape_markdown_v2(due_date)} \\({days}d overdue\\)\n\n"
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in hw_overdue: {e}", exc_info=True)
        await update.message.reply_text("Error getting overdue homework", parse_mode='MarkdownV2')

async def hw_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_chat_id(update)
        hw = load_homework(chat_id)
        
        if not hw:
            await update.message.reply_text("No homework logged", parse_mode='MarkdownV2')
            return
        
        msg = "📚 *Homework:*\n\n"
        today = datetime.date.today()
        
        for idx, subj in enumerate(sorted(hw.keys()), 1):
            msg += f"*{idx}\\. {escape_markdown_v2(subj)}*:\n"
            
            tasks_info = []
            for i, task in enumerate(hw[subj], 1):
                due_date_str = task["due"]
                
                if due_date_str == "TBD":
                    status = "*Undefined*"
                    due_date_obj = None
                else:
                    try:
                        due_date_obj = datetime.datetime.strptime(due_date_str, "%Y-%m-%d").date()
                        days = (due_date_obj - today).days
                        
                        if days < 0:
                            status = f"OVERDUE \\({abs(days)}d\\)"
                        elif days == 0:
                            status = "DUE TODAY"
                        elif days == 1:
                            status = "DUE TOMORROW"
                        else:
                            status = f"\\({days}d left\\)"
                    except ValueError:
                        status = "Invalid date format"
                        due_date_obj = None
                
                tasks_info.append((i, task, status, due_date_obj, due_date_str))
            
            tasks_info.sort(key=lambda x: x[3] if x[3] else datetime.date.max) 
            
            for i, task, status, _, due_date_str in tasks_info:
                preview = task['task'][:100] + "..." if len(task['task']) > 100 else task['task']
                
                if due_date_str == "TBD":
                    due_line = "Due *Undefined*"
                else:
                    due_line = f"Due {escape_markdown_v2(due_date_str)} {status}"
                
                msg += f"   {i}\\. {escape_markdown_v2(preview)}\n      {due_line}\n"
            msg += "\n"
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in hw_list: {e}")
        await update.message.reply_text("Error listing homework", parse_mode='MarkdownV2')

async def hw_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_chat_id(update)
        
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: `/hw_remove Subject index`\n"
                "Example: `/hw_remove Python 1`",
                parse_mode='MarkdownV2'
            )
            return

        subj_input, idx_str = context.args[0], context.args[1]
        
        try:
            hw_idx = int(idx_str) - 1
        except ValueError:
            await update.message.reply_text("Index must be a number", parse_mode='MarkdownV2')
            return

        hw = load_homework(chat_id)
        if not hw:
            await update.message.reply_text("No homework found", parse_mode='MarkdownV2')
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
            await update.message.reply_text(f"Subject not found: {escape_markdown_v2(subj_input)}", parse_mode='MarkdownV2')
            return
        
        if hw_idx < 0 or hw_idx >= len(hw[subject]):
            await update.message.reply_text(f"Invalid index for {escape_markdown_v2(subject)}", parse_mode='MarkdownV2')
            return

        removed = hw[subject].pop(hw_idx)
        if not hw[subject]:
            del hw[subject]
        
        save_homework(chat_id, hw)
        
        task_preview = removed['task'][:80] + "..." if len(removed['task']) > 80 else removed['task']
        await update.message.reply_text(
            f"✅ Removed from *{escape_markdown_v2(subject)}*:\n{escape_markdown_v2(task_preview)}", 
            parse_mode='MarkdownV2'
        )
    except Exception as e:
        logger.error(f"Error in hw_remove: {e}")
        await update.message.reply_text("Error removing homework", parse_mode='MarkdownV2')

async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_chat_id(update)
        schedule = load_group_timetable(chat_id)
        
        if not schedule:
            await update.message.reply_text(
                "No timetable set for this group\\. Use /set\\_timetable to create one\\.",
                parse_mode='MarkdownV2'
            )
            return
        
        today = datetime.date.today()
        day_name = today.strftime('%A')
        
        if day_name not in schedule or not schedule[day_name]:
            await update.message.reply_text(
                f"📅 *{escape_markdown_v2(day_name)}*: No lessons", 
                parse_mode='MarkdownV2'
            )
            return
        
        week_type = get_week_type(today)
        msg = f"📅 *{escape_markdown_v2(day_name)}* \\({week_type}\\):\n\n"
        
        displayed_count = 0
        for i, lesson in enumerate(schedule[day_name], 1):
            if not is_lesson_this_week(lesson, today):
                continue
            
            subj = lesson.get("subject", "").strip()
            room = lesson.get("room", "").strip()
            ltype = lesson.get("type", "").strip()
            
            if not subj:
                continue
            
            displayed_count += 1
            msg += f"{i}\\. *{escape_markdown_v2(subj)}*"
            
            if ltype:
                msg += f" \\({escape_markdown_v2(ltype)}\\)"
            if room:
                msg += f" \\- {escape_markdown_v2(room)}"
            
            msg += "\n"
        
        if displayed_count == 0:
            await update.message.reply_text(
                f"📅 *{escape_markdown_v2(day_name)}*: No lessons this week \\({week_type}\\)", 
                parse_mode='MarkdownV2'
            )
            return
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in timetable: {e}")
        await update.message.reply_text("Error displaying timetable", parse_mode='MarkdownV2')

async def full_timetable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_chat_id(update)
        schedule = load_group_timetable(chat_id)
        
        if not schedule:
            await update.message.reply_text(
                "No timetable set\\. Use /set\\_timetable\\.",
                parse_mode='MarkdownV2'
            )
            return
        
        today = datetime.date.today()
        week_type = get_week_type(today)
        msg = f"📅 *Full Weekly Timetable* \\({week_type}\\):\n\n"
        
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        
        for day in days:
            if day not in schedule or not schedule[day]:
                msg += f"*{escape_markdown_v2(day)}*: No lessons\n\n"
                continue
            
            msg += f"*{escape_markdown_v2(day)}*:\n"
            
            for i, lesson in enumerate(schedule[day], 1):
                subj = lesson.get("subject", "").strip()
                room = lesson.get("room", "").strip()
                ltype = lesson.get("type", "").strip()
                week = lesson.get("week", "").strip()
                
                if not subj:
                    continue
                
                msg += f"   {i}\\. {escape_markdown_v2(subj)}"
                
                if ltype:
                    msg += f" \\({escape_markdown_v2(ltype)}\\)"
                if room:
                    msg += f" \\- {escape_markdown_v2(room)}"
                if week:
                    msg += f" \\[{escape_markdown_v2(week)}\\]"
                
                msg += "\n"
            msg += "\n"
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in full_timetable: {e}")
        await update.message.reply_text("Error displaying full timetable", parse_mode='MarkdownV2')

async def set_timetable_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Set from JSON", callback_data='timetable_json')],
        [InlineKeyboardButton("Cancel", callback_data='timetable_cancel')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📅 *Timetable Setup*\n\n"
        "Choose how to set the timetable:",
        reply_markup=reply_markup,
        parse_mode='MarkdownV2'
    )
    return SETTING_TIMETABLE

async def timetable_json_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "Please send the timetable as a JSON object\\.\n\n"
        "Example format:\n"
        "```json\n"
        "{\n"
        '  "Monday": [\n'
        '    {"subject": "Math", "room": "101", "type": "л"},\n'
        '    {"subject": "Physics", "room": "202", "type": "пр"}\n'
        '  ],\n'
        '  "Tuesday": []\n'
        "}\n"
        "```\n"
        "Send /cancel to abort\\.",
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
            await update.message.reply_text("Invalid format\\. Must be a JSON object\\.", parse_mode='MarkdownV2')
            return SETTING_TIMETABLE
        
        save_group_timetable(chat_id, new_schedule)
        
        await update.message.reply_text(
            f"✅ Timetable updated for group {chat_id}\\!",
            parse_mode='MarkdownV2'
        )
        return ConversationHandler.END
        
    except json.JSONDecodeError as e:
        await update.message.reply_text(
            f"⚠️ Invalid JSON: {escape_markdown_v2(str(e))}\nPlease try again or /cancel\\.",
            parse_mode='MarkdownV2'
        )
        return SETTING_TIMETABLE

async def timetable_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Timetable setup cancelled\\.", parse_mode='MarkdownV2')
    return ConversationHandler.END

async def next_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_chat_id(update)
        schedule = load_group_timetable(chat_id)
        
        if not schedule:
            await update.message.reply_text("No timetable set", parse_mode='MarkdownV2')
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
                        
                        week_type = get_week_type(check_date)
                        msg = f"📚 *Next lesson:*\n\n"
                        msg += f"*{escape_markdown_v2(subj)}*"
                        
                        if ltype:
                            msg += f" \\({escape_markdown_v2(ltype)}\\)"
                        if room:
                            msg += f"\n📍 {escape_markdown_v2(room)}"
                        
                        if offset == 0:
                            msg += f"\n📅 Today \\({week_type}\\)"
                        elif offset == 1:
                            msg += f"\n📅 Tomorrow \\({week_type}\\)"
                        else:
                            msg += f"\n📅 {escape_markdown_v2(check_day)} \\({week_type}\\)"
                        
                        await update.message.reply_text(msg, parse_mode='MarkdownV2')
                        return
        
        await update.message.reply_text("No upcoming lessons found", parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in next_lesson: {e}")
        await update.message.reply_text("Error finding next lesson", parse_mode='MarkdownV2')

async def motivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quotes = [
        "Success is not final, failure is not fatal",
        "Dream big, work hard, stay focused",
        "The expert in anything was once a beginner",
        "Education is the passport to the future",
        "Don't watch the clock; do what it does. Keep going",
        "Study while others are sleeping",
        "The only way to learn is to live",
        "Knowledge is power",
    ]
    
    quote = random.choice(quotes)
    await update.message.reply_text(f"💪 {escape_markdown_v2(quote)}", parse_mode='MarkdownV2')

async def kys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_chat_id(update)
    
    keyboard = [
        [InlineKeyboardButton("Yes, delete everything", callback_data=f'kys_confirm_{chat_id}')],
        [InlineKeyboardButton("No, cancel", callback_data='kys_cancel')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⚠️ *WARNING*\n\n"
        "This will DELETE ALL homework and timetable data for this group\\.\n"
        "Are you sure\\?",
        reply_markup=reply_markup,
        parse_mode='MarkdownV2'
    )

async def kys_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    chat_id = int(query.data.split('_')[-1])
    
    try:
        hw_file = get_homework_file(chat_id)
        config_file = get_config_file(chat_id)
        
        if os.path.exists(hw_file):
            os.remove(hw_file)
        if os.path.exists(config_file):
            os.remove(config_file)
        
        await query.edit_message_text("🗑️ All data deleted for this group\\.", parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in kys_confirm: {e}")
        await query.edit_message_text("Error deleting data", parse_mode='MarkdownV2')

async def kys_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Deletion cancelled\\.", parse_mode='MarkdownV2')

async def send_reminder_to_group(app: Application, chat_id: int, message: str):
    try:
        await app.bot.send_message(chat_id=chat_id, text=message, parse_mode='MarkdownV2')
        logger.info(f"Sent reminder to group {chat_id}")
    except Exception as e:
        logger.error(f"Failed to send reminder to {chat_id}: {e}")

async def check_and_send_reminders():
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
            
            chat_id = int(filename.replace("config_", "").replace(".json", ""))
            config = load_group_config(chat_id)
            
            if not config.get("reminders_enabled", True):
                continue
            
            morning_time = config.get("morning_reminder", "08:00")
            evening_time = config.get("evening_reminder", "16:00")
            
            reminder_key = f"{chat_id}_{current_time}_{today.isoformat()}"
            
            if reminder_key in last_reminder_data:
                continue
            
            if current_time == morning_time:
                schedule = load_group_timetable(chat_id)
                day_name = today.strftime('%A')
                
                if day_name in schedule and schedule[day_name]:
                    lessons_today = []
                    for lesson in schedule[day_name]:
                        if is_lesson_this_week(lesson, today):
                            subj = lesson.get("subject", "").strip()
                            if subj:
                                lessons_today.append(subj)
                    
                    if lessons_today:
                        msg = f"🌅 *Good morning\\!*\n\nToday's lessons:\n"
                        for i, subj in enumerate(lessons_today, 1):
                            msg += f"{i}\\. {escape_markdown_v2(subj)}\n"
                        
                        await send_reminder_to_group(app, chat_id, msg)
                        last_reminder_data[reminder_key] = True
            
            elif current_time == evening_time:
                hw = load_homework(chat_id)
                
                tomorrow_hw = []
                for subj, tasks in hw.items():
                    for task in tasks:
                        if task["due"] == tomorrow.isoformat():
                            tomorrow_hw.append((subj, task))
                
                if tomorrow_hw:
                    msg = f"⏰ *Reminder\\!*\n\nHomework due tomorrow:\n\n"
                    for i, (subj, task) in enumerate(tomorrow_hw, 1):
                        preview = task['task'][:80] + "..." if len(task['task']) > 80 else task['task']
                        msg += f"{i}\\. *{escape_markdown_v2(subj)}*: {escape_markdown_v2(preview)}\n\n"
                    
                    await send_reminder_to_group(app, chat_id, msg)
                    last_reminder_data[reminder_key] = True
        
        keys_to_remove = [k for k in last_reminder_data.keys() if k.split('_')[-1] != today.isoformat()]
        for k in keys_to_remove:
            del last_reminder_data[k]
    
    except Exception as e:
        logger.error(f"Error in check_and_send_reminders: {e}")

async def reminder_loop():
    while not shutdown_event.is_set():
        try:
            await check_and_send_reminders()
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in reminder loop: {e}")
            await asyncio.sleep(60)

def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    shutdown_event.set()

async def post_init(application: Application):
    global app, reminder_task
    app = application
    
    commands = [
        BotCommand("start", "Show help and available commands"),
        BotCommand("hw_add", "Quick add homework (Subject | Task | Date)"),
        BotCommand("hw_long_add", "Interactive homework add"),
        BotCommand("hw_list", "List all homework"),
        BotCommand("hw_remove", "Remove homework (Subject index)"),
        BotCommand("hw_today", "Show homework due today"),
        BotCommand("hw_overdue", "Show overdue homework"),
        BotCommand("hw_stats", "Show homework statistics"),
        BotCommand("hw_clean", "Clean old homework (30+ days)"),
        BotCommand("timetable", "Show today's schedule"),
        BotCommand("full_timetable", "Show full weekly schedule"),
        BotCommand("set_timetable", "Set new timetable for this group"),
        BotCommand("next", "Show next lesson"),
        BotCommand("motivate", "Get a motivational quote"),
        BotCommand("kys", "Delete all group data"),
    ]
    
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set successfully")
    
    reminder_task = asyncio.create_task(reminder_loop())
    logger.info("Reminder system started")

async def post_shutdown(application: Application):
    global reminder_task
    
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
        logger.error("Another instance is already running. Exiting.")
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
        app.add_handler(CommandHandler("kys", kys_command))
        
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
        
        app.add_handler(CallbackQueryHandler(kys_confirm, pattern='^kys_confirm_'))
        app.add_handler(CallbackQueryHandler(kys_cancel, pattern='^kys_cancel'))
        
        logger.info("Bot started successfully")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        release_lock()
        logger.info("Bot stopped")

if __name__ == "__main__":
    main()