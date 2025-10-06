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

# ====== CONFIG ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8466519086:AAFKIpz3d30irZH5UedMwWyIIF62QeoNJvk")

# The group ID for which the current TIMETABLE will be saved (placeholder for your group)
# Assuming your group ID is available from a previous context.
# I'll use a negative number typical for group IDs.
DEFAULT_GROUP_ID = -123456789 # <--- Set your actual group ID here!

# Validate required environment variables
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required. Set it with: export TELEGRAM_BOT_TOKEN='your_token_here'")

DATA_DIR = "group_data"
LOCK_FILE = "bot.lock"
ARMENIA_TZ = pytz.timezone('Asia/Yerevan')

# Create data directory if it doesn't exist
os.makedirs(DATA_DIR, exist_ok=True)

# ====== LOGGING ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)

logger = logging.getLogger(__name__)

# ====== CONVERSATION STATES (MODIFIED) ======
SETTING_TIMETABLE = 0 
# New states for the multi-step homework addition (/hw_long_add)
LONG_ADDING_SUBJECT, LONG_ADDING_TASK, LONG_ADDING_DATE = range(1, 4) 

# ====== GLOBAL VARIABLES ======
app = None
reminder_task = None
shutdown_event = asyncio.Event()
lock_file = None
last_reminder_data = {}  # Stores last reminder times per group

# --- INITIAL TIMETABLE (To be saved to DEFAULT_GROUP_ID's config) ---
# NOTE: This global variable is only used ONCE to initialize the default group's config.
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

# ====== MARKDOWN V2 UTILITY FUNCTION (FIXED) ======
def escape_markdown_v2(text: str) -> str:
    """Escape special characters for MarkdownV2 formatting, especially the backslash."""
    
    # FIX: Manually escape the backslash character first, which is the cause of most MarkdownV2 parsing errors.
    text = text.replace('\\', '\\\\')

    # List of special characters that need to be escaped in MarkdownV2 (excluding \ now)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    
    # Escape characters that are commonly used in user input and dates but are NOT used for formatting
    # The | character is used as a separator in /hw_add, so we need to escape it for display.
    # We escape it manually to prevent double-escaping from the regex if the backslash logic changed the text.
    text = text.replace('|', '\|')
    
    # Escape the rest
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# ====== GROUP-SPECIFIC FILE PATHS (UNCHANGED) ======
def get_homework_file(chat_id: int) -> str:
    """Get homework file path for specific group"""
    return os.path.join(DATA_DIR, f"homework_{chat_id}.json")

def get_config_file(chat_id: int) -> str:
    """Get config file path for specific group"""
    return os.path.join(DATA_DIR, f"config_{chat_id}.json")

# ====== DATA HELPERS (UNCHANGED) ======
def load_json_file(filename: str) -> Dict:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        # Added filename to the log for better debugging
        logger.error(f"Error loading {filename} (File corruption/Invalid JSON): {e}", exc_info=True)
        return {}

def save_json_file(filename: str, data: Dict):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving {filename}: {e}")

def load_homework(chat_id: int):
    """Load homework for specific group"""
    return load_json_file(get_homework_file(chat_id))

def save_homework(chat_id: int, hw: Dict):
    """Save homework for specific group"""
    save_json_file(get_homework_file(chat_id), hw)

def load_group_config(chat_id: int) -> Dict[str, Any]:
    """Load group configuration, ensuring timetable is set if missing"""
    config = load_json_file(get_config_file(chat_id))
    
    # Check for minimal config and set defaults if missing
    if not config or any(key not in config for key in ["reminders_enabled", "morning_reminder"]):
        config = {
            "reminders_enabled": True,
            "morning_reminder": "08:00",
            "evening_reminder": "18:00",
            "timezone": "Asia/Yerevan",
        }

    # NEW: Initialize timetable for the current group if it's the default and not set
    # Or, if it's any new group, initialize it as empty.
    if "timetable" not in config:
        if chat_id == DEFAULT_GROUP_ID:
            config["timetable"] = INITIAL_TIMETABLE
            logger.info(f"Initialized timetable for DEFAULT_GROUP_ID ({chat_id})")
        else:
            config["timetable"] = {}
            logger.info(f"Initialized empty timetable for new group {chat_id}")

    # Save the updated config (with defaults/initial timetable)
    # This ensures that a new or corrupted file is fixed with defaults immediately.
    save_group_config(chat_id, config)
    return config

def save_group_config(chat_id: int, config: Dict[str, Any]):
    """Save group configuration"""
    save_json_file(get_config_file(chat_id), config)

# NEW: Helper for group-specific timetable
def load_group_timetable(chat_id: int) -> Dict[str, List[Dict[str, str]]]:
    """Load the timetable for a specific group"""
    config = load_group_config(chat_id)
    # Return the timetable or an empty dict if load_group_config somehow failed
    return config.get("timetable", {})

def save_group_timetable(chat_id: int, timetable: Dict[str, List[Dict[str, str]]]):
    """Save the timetable for a specific group"""
    config = load_group_config(chat_id)
    config["timetable"] = timetable
    save_group_config(chat_id, config)

def get_chat_id(update: Update) -> int:
    """Get chat ID from update"""
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

# ====== UTILITY FUNCTIONS (UPDATED) ======
def get_week_type(date: datetime.date = None) -> str:
    """Get week type for a specific date (defaults to today)"""
    if date is None:
        date = datetime.date.today()
    week_num = date.isocalendar()[1]
    # Assuming "ч/н" (четная неделя) is even number
    return "ч/н" if week_num % 2 == 0 else "н/ч"

def is_lesson_this_week(lesson: Dict, date: datetime.date = None) -> bool:
    """Check if a lesson happens on the given date (defaults to today)"""
    if "week" not in lesson:
        return True
    
    # Note: the week value in the timetable is a string like "ч/н" or "н/ч"
    # The week type calculated is also a string like "ч/н" or "н/ч"
    week_type = get_week_type(date)
    return lesson["week"] == week_type

def parse_flexible_date(date_str: str) -> datetime.date | str:
    """Parse flexible date formats or return 'TBD' for undefined dates."""
    today = datetime.date.today()
    date_lower = date_str.lower().strip()
    
    # NEW: Handle undefined date keywords
    if date_lower in ["none", "tbd", "n/a", "undefined", "-"]:
        return "TBD"
    
    if date_lower in ["today", "սյօր", "сегодня"]:
        return today
    elif date_lower in ["tomorrow", "վաղը", "завтра"]:
        return today + datetime.timedelta(days=1)
    elif date_lower in ["next week", "հաջորդ շաբաթ", "на след неделе"]:
        # Find next Monday (or next same day 7 days later)
        return today + datetime.timedelta(days=7)
    elif re.match(r'^\+\d+$', date_lower):
        try:
            days = int(date_lower[1:])
            return today + datetime.timedelta(days=days)
        except ValueError:
            raise ValueError(f"Invalid relative date: {date_str}")
    else:
        # Check for DD-MM format (simplified for user-friendliness)
        match_dd_mm = re.match(r'^(\d{1,2})[-/](\d{1,2})$', date_lower)
        if match_dd_mm:
            day, month = map(int, match_dd_mm.groups())
            try:
                # Assume current year, but if the date is in the past, assume next year
                target_date = datetime.date(today.year, month, day)
                if target_date < today:
                    target_date = datetime.date(today.year + 1, month, day)
                return target_date
            except ValueError:
                raise ValueError(f"Invalid date: {date_str}. Day or month out of range.")
        
        # Fallback to standard YYYY-MM-DD
        return datetime.datetime.strptime(date_str, '%Y-%m-%d').date()

# ====== GENERIC CONVERSATION CANCEL (UNCHANGED) ======
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic cancel command for any active conversation."""
    await update.message.reply_text("❌ Operation cancelled\\.", parse_mode='MarkdownV2')
    context.user_data.clear()
    return ConversationHandler.END

# ====== BASIC COMMANDS (UPDATED /start MESSAGE) ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    chat_id = get_chat_id(update)
    chat_type = update.effective_chat.type
    
    # MarkdownV2 formatting applied + FIX: Escaped parentheses
    welcome_msg = (
        f"Study Bot \\(Group: {chat_id}\\)\n\n"
        f"*Homework System \\(Dual Mode\\)*\n"
        f"📚 `/hw_add Subject \\| Task \\| Date` \\- *Quick Add* in one line\\.\n"
        f"   _Date can be `tomorrow`, `DD\\-MM`, `YYYY\\-MM\\-DD`, or `TBD`_\\.\n" # UPDATED HINT
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

# ====== HOMEWORK ADDITION (UPDATED) ======

# --- 1. Quick Add (One-Line) ---
async def hw_quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restored single-line homework add: /hw_add Subject | Task | Date, with confirmation."""
    chat_id = get_chat_id(update)
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "⚠️ *Invalid format\\.* \n"
            "Use: `/hw_add Subject \\| Task \\| Date`\n\n"
            "Example:\n"
            "/hw_add Python \\| Create API client \\| TBD\n\n" # UPDATED HINT
            "For step-by-step guidance, use `/hw_long_add`", 
            parse_mode='MarkdownV2'
        )
        return
    
    full_text = " ".join(context.args)
    # Ensure escaping of | for MarkdownV2 doesn't interfere with parsing
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
        # Use a more descriptive variable name
        due_date_or_tbd = parse_flexible_date(date_str)
    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ *Invalid date:* {escape_markdown_v2(date_str)}\n"
            f"Error: {escape_markdown_v2(str(e))}\n"
            "Use: `tomorrow`, `+N`, `DD\\-MM`, `YYYY\\-MM\\-DD`, or *`TBD`*", # UPDATED HINT
            parse_mode='MarkdownV2'
        )
        return
    
    # Determine the saved value (ISO format or "TBD") and the display value
    if due_date_or_tbd == "TBD":
        due_iso = "TBD"
        due_display = "*Undefined*" # Highlight TBD in display
    else:
        due_iso = due_date_or_tbd.isoformat()
        due_display = due_date_or_tbd.strftime('%Y-%m-%d (%A)')
    
    hw = load_homework(chat_id)
    hw_item = {
        "task": task,
        "due": due_iso, # Use the determined ISO or "TBD" string
        "added": datetime.date.today().isoformat()
    }
    
    hw.setdefault(subject, []).append(hw_item)
    save_homework(chat_id, hw)
    
    task_preview = task[:100] + "..." if len(task) > 100 else task
    
    # Confirmation message
    await update.message.reply_text(
        f"✅ *Homework added\\!*\n\n"
        f"*{escape_markdown_v2(subject)}*\n"
        f"Task: {escape_markdown_v2(task_preview)}\n"
        f"Due: {escape_markdown_v2(due_display)}", 
        parse_mode='MarkdownV2'
    )
    logger.info(f"Quickly added homework in group {chat_id}: {subject} - {task[:50]}...")


# --- 2. Interactive Add (Conversation) ---

async def hw_long_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate the homework adding conversation (/hw_long_add)."""
    await update.message.reply_text("📚 *Starting Interactive Homework Add*\\.\nWhat is the *Subject* of the homework\\? \\(e\\.g\\. Python, Math, History\\)\nSend /cancel to stop\\.", parse_mode='MarkdownV2')
    return LONG_ADDING_SUBJECT

async def get_subject_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get subject and ask for task."""
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
    """Get task and ask for due date. (UPDATED PROMPT)"""
    task = update.message.text.strip()
    context.user_data['temp_task'] = task
    
    if context.args:
         context.args.clear()
         
    await update.message.reply_text(
        "✅ Task saved\\.\n\n"
        "Finally, what is the *Due Date*\\?\n"
        "Use formats like: `tomorrow`, `+3 days`, `15\\-10`, `2025\\-10\\-15` or *`TBD`* for an undefined date\\.", # UPDATED HINT
        parse_mode='MarkdownV2'
    )
    return LONG_ADDING_DATE

async def get_date_and_save_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get date, validate, and save the homework entry for the long add. (UPDATED)"""
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
        return LONG_ADDING_DATE # Stay in the same state

    # Determine the saved value (ISO format or "TBD") and the display value
    if due_date_or_tbd == "TBD":
        due_iso = "TBD"
        due_display = "*Undefined*" # Highlight TBD in display
    else:
        due_iso = due_date_or_tbd.isoformat()
        due_display = due_date_or_tbd.strftime('%Y-%m-%d (%A)')

    # Save logic
    hw = load_homework(chat_id)
    hw_item = {
        "task": task,
        "due": due_iso, # Use the determined ISO or "TBD" string
        "added": datetime.date.today().isoformat()
    }
    
    hw.setdefault(subject, []).append(hw_item)
    save_homework(chat_id, hw)
    
    # Confirmation message
    task_preview = task[:50] + "..." if len(task) > 50 else task
    await update.message.reply_text(
        f"🎉 *Homework Saved Successfully!* \n\n"
        f"*{escape_markdown_v2(subject)}*\n"
        f"Task: {escape_markdown_v2(task_preview)}\n"
        f"Due: {escape_markdown_v2(due_display)}", # Use the calculated display string
        parse_mode='MarkdownV2'
    )
    
    context.user_data.clear()
    return ConversationHandler.END

# ====== HOMEWORK MANAGEMENT (hw_stats, hw_clean, etc. remain UPDATED for TBD) ======

async def hw_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show homework statistics (UPDATED for TBD)"""
    try:
        chat_id = get_chat_id(update)
        hw = load_homework(chat_id)
        
        if not hw:
            await update.message.reply_text("No homework logged", parse_mode='MarkdownV2')
            return
        
        total = sum(len(tasks) for tasks in hw.values())
        overdue = due_today = due_tomorrow = tbd_count = 0 # Added tbd_count
        today = datetime.date.today()
        tomorrow = today + datetime.timedelta(days=1)
        
        for tasks in hw.values():
            for task in tasks:
                if task["due"] == "TBD":
                    tbd_count += 1
                    continue # Skip TBD for date comparison
                
                try:
                    due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due_date < today:
                        overdue += 1
                    elif due_date == today:
                        due_today += 1
                    elif due_date == tomorrow:
                        due_tomorrow += 1
                except ValueError:
                    pass # Ignore invalid date formats
        
        msg = (f"📊 *Homework Statistics:*\n\n"
               f"Total: {total}\n"
               f"Overdue: {overdue}\n"
               f"Due today: {due_today}\n"
               f"Due tomorrow: {due_tomorrow}\n"
               f"Undefined Date: {tbd_count}") # Display TBD count
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in hw_stats: {e}")
        await update.message.reply_text("Error getting statistics", parse_mode='MarkdownV2')

async def hw_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clean old homework entries (UPDATED for TBD)"""
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
                    keep.append(task) # Always keep TBD homework
                    continue

                try:
                    due = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due >= cutoff:
                        keep.append(task)
                    else:
                        cleaned += 1
                except ValueError:
                    keep.append(task) # Keep invalid dates (user may need to fix them)
            
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
    """Show homework due today (UPDATED for TBD)"""
    try:
        chat_id = get_chat_id(update)
        hw = load_homework(chat_id)
        today = datetime.date.today().isoformat()
        
        # Filter out TBD and only select tasks due today
        today_hw = [(s, t) for s, tasks in hw.items() for t in tasks if t["due"] == today]
        
        if not today_hw:
            await update.message.reply_text("No homework due today", parse_mode='MarkdownV2')
            return
        
        msg = "📅 *Due today:*\n\n"
        for i, (subj, task) in enumerate(today_hw, 1):
            preview = task['task'][:80] + "..." if len(task['task']) > 80 else task['task']
            # Escaping user-provided data, and using * for bold
            msg += f"{i}\\. *{escape_markdown_v2(subj)}*: {escape_markdown_v2(preview)}\n\n"
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in hw_today: {e}")
        await update.message.reply_text("Error getting today's homework", parse_mode='MarkdownV2')

async def hw_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show overdue homework (UPDATED for TBD)"""
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
                if task["due"] == "TBD":
                    continue # Skip TBD homework
                
                try:
                    due = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due < today:
                        days = (today - due).days
                        overdue.append((subj, task, days, due))
                except ValueError:
                    pass
        
        if not overdue:
            await update.message.reply_text("✅ No overdue homework", parse_mode='MarkdownV2')
            return
        
        overdue.sort(key=lambda x: x[3])
        msg = f"⚠️ *Overdue ({len(overdue)}):*\n\n"
        
        for i, (subj, task, days, _) in enumerate(overdue, 1):
            preview = task['task'][:60] + "..." if len(task['task']) > 60 else task['task']
            # Escaping user-provided data, and using * for bold
            msg += f"{i}\\. *{escape_markdown_v2(subj)}*: {escape_markdown_v2(preview)}\n   {escape_markdown_v2(task['due'])} \\({days}d overdue\\)\n\n"
        
        await update.message.reply_text(msg, parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in hw_overdue: {e}")
        await update.message.reply_text("Error getting overdue homework", parse_mode='MarkdownV2')

async def hw_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all pending homework (UPDATED for TBD and better sorting)"""
    try:
        chat_id = get_chat_id(update)
        hw = load_homework(chat_id)
        
        if not hw:
            await update.message.reply_text("No homework logged", parse_mode='MarkdownV2')
            return
        
        msg = "📚 *Homework:*\n\n"
        today = datetime.date.today()
        
        for idx, subj in enumerate(sorted(hw.keys()), 1):
            # Escaping subject and using * for bold
            msg += f"*{idx}\\. {escape_markdown_v2(subj)}*:\n"
            
            tasks_info = []
            for i, task in enumerate(hw[subj], 1):
                due_date_str = task["due"]
                
                if due_date_str == "TBD":
                    status = "*Undefined*"
                    due_date_obj = None # Use None for sorting
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
                
                # Append a tuple with (index, task_dict, status_string, due_date_object, due_date_string)
                tasks_info.append((i, task, status, due_date_obj, due_date_str))
            
            # Sort: defined dates first, then invalid/TBD (using max date for None)
            tasks_info.sort(key=lambda x: x[3] if x[3] else datetime.date.max) 
            
            for i, task, status, _, due_date_str in tasks_info:
                preview = task['task'][:100] + "..." if len(task['task']) > 100 else task['task']
                
                # Display Logic
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
    # ... (hw_remove implementation remains the same)
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
        preview = removed['task'][:60] + "..." if len(removed['task']) > 60 else removed['task']
        await update.message.reply_text(f"🗑️ Removed: *{escape_markdown_v2(subject)}* \\- {escape_markdown_v2(preview)}", parse_mode='MarkdownV2')
        logger.info(f"Removed from group {chat_id}: {subject} - {removed['task'][:50]}...")
    except Exception as e:
        logger.error(f"Error in hw_remove: {e}")
        await update.message.reply_text("Error removing homework", parse_mode='MarkdownV2')


# ====== TIMETABLE UTILITY FUNCTION (FIXED: Date escaping) ======
def format_day_schedule(day: str, lessons: List[Dict[str, str]], date: datetime.date = None) -> str:
    """Formats a single day's schedule for MarkdownV2."""
    
    # Calculate current week type if date is provided
    if date:
        current_week_type = get_week_type(date)
        # FIX: Escape the date string to handle the unescaped hyphen '-' (e.g., '10-06')
        escaped_date = escape_markdown_v2(date.strftime('%m-%d'))
        header = f"*{escape_markdown_v2(day)} \\({escaped_date}\\):*\n"
    else:
        # Used for /full_timetable where date isn't always relevant
        header = f"*{escape_markdown_v2(day)}:*\n"
        current_week_type = None

    schedule_lines = []
    count = 0
    
    for lesson in lessons:
        # Check if the lesson is relevant for the specified date/week type
        if lesson.get("subject") and is_lesson_this_week(lesson, date):
            count += 1
            
            # Format lesson details
            type_info = f" \\({escape_markdown_v2(lesson.get('type'))}\\)" if lesson.get('type') else ""
            
            # Show week type only if it's conditional
            lesson_week = lesson.get('week')
            week_info = ""
            if lesson_week:
                # Highlight if the class is happening this week
                if current_week_type and lesson_week == current_week_type:
                    week_info = f" \\[*{escape_markdown_v2(lesson_week)}*\\]"
                else:
                    week_info = f" \\[\\({escape_markdown_v2(lesson_week)}\\)\\]"
            
            # Escaping subject and room, and using * for bold
            schedule_lines.append(
                f"{count}\\. *{escape_markdown_v2(lesson['subject'])}* \\- {escape_markdown_v2(lesson['room'])}{type_info}{week_info}"
            )
            
    if count == 0 and lessons:
        # Only show "No classes" if the list isn't completely empty, but lessons were filtered out
        return f"*{escape_markdown_v2(day)}:*\n   _No classes this {current_week_type} week_\n" if current_week_type else f"*{escape_markdown_v2(day)}:*\n   _No classes scheduled_\n"
    elif count == 0 and not lessons:
        # If the day's lesson list is truly empty in the timetable JSON
        return f"*{escape_markdown_v2(day)}:*\n   _No classes scheduled_\n"

    return header + "\n".join(schedule_lines) + "\n"

# ====== TIMETABLE CONVERSATION HANDLERS (UNCHANGED) ======

async def set_timetable_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user to send the new timetable structure."""
    chat_id = get_chat_id(update)
    logger.info(f"set_timetable_start called in group {chat_id}")
    
    context.user_data['chat_id'] = chat_id
    
    await update.message.reply_text(
        "📝 *Send the new timetable now\\.*\n\n"
        "You need to provide a single JSON object\\. This is technical but allows for reminders and detailed schedule display \\(See example below\\)\\.\n\n"
        "*Example Format:*\n"
        "```json\n"
        "{\n"
        '  "Monday": [\n'
        '    {"subject": "Math", "room": "301", "type": "л"},\n'
        '    {"subject": "Physics", "room": "322", "type": "пр"}\n'
        '  ],\n'
        '  "Tuesday": [\n'
        '    {"subject": "Chem", "room": "201", "week": "н/ч"}\n'
        '  ]\n'
        "}\n"
        "```\n"
        "Please ensure there are no trailing commas or comments outside the double quotes\\.\n"
        "Send /cancel to stop\\.",
        parse_mode='MarkdownV2'
    )
    return SETTING_TIMETABLE

async def set_timetable_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save the received JSON as the new timetable."""
    chat_id = context.user_data.get('chat_id', get_chat_id(update))
    message_text = update.message.text.strip()
    logger.info(f"set_timetable_save called in group {chat_id} with text length: {len(message_text)}")

    try:
        new_timetable = json.loads(message_text)
        
        if not isinstance(new_timetable, dict):
            await update.message.reply_text("⚠️ *Invalid format\\.* The timetable must be a JSON object \\(dictionary\\)\\. Please try again\\.", parse_mode='MarkdownV2')
            return SETTING_TIMETABLE
        
        # Simple validation: ensure keys are strings and values are lists/arrays
        for day, lessons in new_timetable.items():
            if not isinstance(day, str) or not isinstance(lessons, list):
                await update.message.reply_text(f"⚠️ *Validation Error\\.* Key `{escape_markdown_v2(day)}` must map to a list of lessons\\. Please check your JSON format\\.", parse_mode='MarkdownV2')
                return SETTING_TIMETABLE

        save_group_timetable(chat_id, new_timetable)
        
        await update.message.reply_text("✅ *Timetable updated successfully\\!* Use `/timetable` to view it\\.", parse_mode='MarkdownV2')
        
        context.user_data.clear()
        return ConversationHandler.END
        
    except json.JSONDecodeError as e:
        logger.error(f"set_timetable_save: JSON decode error in group {chat_id}: {e}", exc_info=True)
        await update.message.reply_text(
            f"⚠️ *Invalid JSON format\\.* Error: {escape_markdown_v2(str(e))}\n\n"
            "Please ensure your message is a single, valid JSON object \\(no comments, no trailing commas\\) and try again or /cancel\\.",
            parse_mode='MarkdownV2'
        )
        return SETTING_TIMETABLE
    except Exception as e:
        logger.error(f"Error in set_timetable_save for group {chat_id}: {e}", exc_info=True)
        await update.message.reply_text(f"An unexpected error occurred while saving the timetable: {escape_markdown_v2(str(e))}\\. Please try again or /cancel\\.", parse_mode='MarkdownV2')
        context.user_data.clear()
        return ConversationHandler.END

# ====== TIMETABLE DISPLAY COMMANDS (UNCHANGED) ======

async def schedule_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's schedule"""
    try:
        chat_id = get_chat_id(update)
        timetable = load_group_timetable(chat_id)
        
        today = datetime.date.today()
        day = today.strftime("%A")
        lessons = timetable.get(day, [])
        
        if not lessons:
            await update.message.reply_text(
                f"No classes today \\({day}\\)\\.\n"
                f"You can set the timetable for this group using /set_timetable\\.",
                parse_mode='MarkdownV2'
            )
            return
        
        # Use the utility function to format the schedule
        msg = format_day_schedule(day, lessons, today)
        
        if msg.strip().endswith("No classes scheduled"):
             week_type = get_week_type(today)
             await update.message.reply_text(f"No classes this *{escape_markdown_v2(week_type)}* week\\.", parse_mode='MarkdownV2')
        else:
            # Add the header and send the message
            header = f"📅 *Today's Schedule \\({day}\\):*\n"
            await update.message.reply_text(header + msg, parse_mode='MarkdownV2')

    except Exception as e:
        logger.error(f"Error in schedule_today for group {chat_id}: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ Error getting schedule: {escape_markdown_v2(str(e))}", parse_mode='MarkdownV2')

async def full_timetable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the full weekly timetable, including all days."""
    try:
        chat_id = get_chat_id(update)
        timetable = load_group_timetable(chat_id)
        
        if not timetable:
            await update.message.reply_text(
                "The timetable for this group is empty or not set\\. Use `/set_timetable` to add one\\.",
                parse_mode='MarkdownV2'
            )
            return
            
        days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        full_msg = "🗓️ *Full Weekly Timetable:*\n\n"
        
        for day in days_order:
            lessons = timetable.get(day, [])
            
            # Use the utility function to format the day. No date passed, so it shows all conditional classes.
            day_schedule = format_day_schedule(day, lessons)
            
            full_msg += day_schedule
        
        await update.message.reply_text(full_msg, parse_mode='MarkdownV2')

    except Exception as e:
        logger.error(f"Error in full_timetable for group {chat_id}: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ Error getting full timetable: {escape_markdown_v2(str(e))}", parse_mode='MarkdownV2')

async def next_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the next class or next day's schedule. (FIXED: Date escaping in header)"""
    try:
        chat_id = get_chat_id(update)
        timetable = load_group_timetable(chat_id)
        
        today = datetime.date.today()
        day = today.strftime("%A")
        
        lessons = timetable.get(day, [])
        # Get only the classes happening today/this week
        remaining_lessons = [l for l in lessons if l.get("subject") and is_lesson_this_week(l, today)]
        
        if remaining_lessons:
            # Use utility function to format the remaining classes
            # Note: We pass the full lesson list to format_day_schedule for consistency
            msg = f"📍 *Remaining today \\({day}\\):*\n\n"
            msg += format_day_schedule(day, lessons, today)
            await update.message.reply_text(msg, parse_mode='MarkdownV2')
            return
        
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        curr_idx = days.index(day)
        
        for i in range(1, 8):
            next_idx = (curr_idx + i) % 7
            next_day = days[next_idx]
            next_date = today + datetime.timedelta(days=i)
            
            lessons = timetable.get(next_day, [])
            upcoming = [l for l in lessons if l.get("subject") and is_lesson_this_week(l, next_date)]
            
            if upcoming:
                # FIX: Escape the date string to handle the unescaped hyphen '-'
                escaped_date = escape_markdown_v2(next_date.strftime('%m-%d'))
                msg = f"➡️ *Next \\({next_day} {escaped_date}\\):*\n\n"
                
                msg += format_day_schedule(next_day, lessons, next_date)
                await update.message.reply_text(msg, parse_mode='MarkdownV2')
                return
        
        await update.message.reply_text("No upcoming classes", parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in next_class: {e}")
        await update.message.reply_text("Error getting next class", parse_mode='MarkdownV2')


# ====== FUN COMMANDS (UNCHANGED) ======
async def kys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (kys implementation remains the same)
    try:
        messages = [
            "nigga?",
            "hambal",
            "а ты не только зашел???",
            "likvid.",
            "es el qez em sirum", 
            "poshol naxuy",
        ]
        await update.message.reply_text(escape_markdown_v2(random.choice(messages)), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in kys: {e}")

async def motivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (motivate implementation remains the same)
    try:
        messages = [
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
        await update.message.reply_text(escape_markdown_v2(random.choice(messages)), parse_mode='MarkdownV2')
    except Exception as e:
        logger.error(f"Error in motivate: {e}")

# ====== REMINDERS (UPDATED for TBD) ======

async def send_daily_reminder():
    """Send daily reminders to all groups that have them enabled"""
    global last_reminder_data
    
    try:
        if not app:
            return
        
        now = datetime.datetime.now(ARMENIA_TZ)
        today_date = now.date()
        current_time = now.strftime("%H:%M")
        
        # Get all group data files
        group_files = [f for f in os.listdir(DATA_DIR) if f.startswith("config_")]
        
        for config_file in group_files:
            try:
                # Extract chat_id from filename
                chat_id_str = config_file.replace("config_", "").replace(".json", "")
                chat_id = int(chat_id_str)
                
                # Initialize reminder tracking for this group
                if chat_id not in last_reminder_data:
                    last_reminder_data[chat_id] = {
                        'date': None,
                        'times': set()
                    }
                
                # Reset times if it's a new day
                if last_reminder_data[chat_id]['date'] != today_date:
                    last_reminder_data[chat_id]['date'] = today_date
                    last_reminder_data[chat_id]['times'] = set()
                
                # Load group config and timetable
                config = load_group_config(chat_id)
                
                if not config.get('reminders_enabled', True):
                    continue
                
                morning_time = config.get('morning_reminder', '08:00')
                evening_time = config.get('evening_reminder', '18:00')
                
                # Send morning reminder
                if current_time == morning_time and morning_time not in last_reminder_data[chat_id]['times']:
                    await send_morning_reminder(chat_id, config.get('timetable', {})) # Pass timetable
                    last_reminder_data[chat_id]['times'].add(morning_time)
                    logger.info(f"Sent morning reminder to group {chat_id}")
                
                # Send evening reminder
                elif current_time == evening_time and evening_time not in last_reminder_data[chat_id]['times']:
                    await send_evening_homework_reminder(chat_id)
                    last_reminder_data[chat_id]['times'].add(evening_time)
                    logger.info(f"Sent evening reminder to group {chat_id}")
                    
            except Exception as e:
                logger.error(f"Error processing reminders for group {chat_id_str}: {e}")
                continue
                
    except Exception as e:
        logger.error(f"Error in daily reminder: {e}")

async def send_morning_reminder(chat_id: int, timetable: Dict):
    """Send morning schedule reminder to specific group"""
    try:
        day = datetime.date.today().strftime("%A")
        today = datetime.date.today()
        lessons = timetable.get(day, []) # Use group-specific timetable
        
        if not lessons:
            return
        
        # Use the format utility function
        schedule_msg = format_day_schedule(day, lessons, today)
        
        # Check if the message is substantial (not just "No classes scheduled")
        if "No classes scheduled" not in schedule_msg and "No classes this" not in schedule_msg:
            # This was already correct, using \( \)
            msg = f"🌅 *Good morning\\! Today's classes \\({day}\\):*\n\n"
            msg += schedule_msg
            await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='MarkdownV2')
        
    except Exception as e:
        logger.error(f"Error in morning reminder for group {chat_id}: {e}")

async def send_evening_homework_reminder(chat_id: int):
    """Send evening reminder for homework due today or overdue (UPDATED for TBD)"""
    try:
        today = datetime.date.today()
        hw = load_homework(chat_id)
        
        if not hw:
            return
        
        reminders = []
        for subj, tasks in hw.items():
            for task in tasks:
                if task["due"] == "TBD":
                    continue # Skip TBD homework in evening reminder
                    
                try:
                    due = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due <= today:
                        reminders.append((subj, task, due))
                except ValueError:
                    pass
        
        if not reminders:
            return
        
        reminders.sort(key=lambda x: x[2])
        msg = f"🌙 Evening homework check:\n\n"
        
        for subj, task, due in reminders:
            days_overdue = (today - due).days
            status = f"⚠️ OVERDUE \\({days_overdue}d\\)" if days_overdue > 0 else "📅 DUE TODAY"
            preview = task['task'][:60] + "..." if len(task['task']) > 60 else task['task']
            # Escaping subject, task, and date, and using * for bold
            msg += f"• *{escape_markdown_v2(subj)}*: {escape_markdown_v2(preview)}\n  {status} \\- {escape_markdown_v2(task['due'])}\n\n"
        
        await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode='MarkdownV2')
        
    except Exception as e:
        logger.error(f"Error in evening reminder for group {chat_id}: {e}")

async def reminder_scheduler():
    """Main reminder scheduler loop"""
    logger.info("Reminder scheduler started")
    while not shutdown_event.is_set():
        try:
            await send_daily_reminder()
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            logger.info("Reminder scheduler cancelled")
            break
        except Exception as e:
            logger.error(f"Error in scheduler: {e}")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                continue

# ====== SIGNAL HANDLERS (UNCHANGED) ======
def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    shutdown_event.set()

# ====== MAIN (UPDATED HANDLERS) ======
async def post_init(application: Application):
    commands = [
        BotCommand("start", "Start bot"),
        BotCommand("hw_add", "Quickly add homework (Subject | Task | Date)"), 
        BotCommand("hw_long_add", "Start the interactive process to add homework"), 
        BotCommand("hw_list", "List all pending homework"),
        BotCommand("hw_remove", "Remove homework by Subject and index"),
        BotCommand("hw_today", "Show homework due today"),
        BotCommand("hw_overdue", "Show overdue homework"),
        BotCommand("hw_stats", "Show homework statistics"),
        BotCommand("hw_clean", "Clean old homework entries"),
        BotCommand("timetable", "Show the timetable for this group (Today)"), 
        BotCommand("full_timetable", "Show the full weekly timetable"), 
        BotCommand("set_timetable", "Set a new timetable for this group (JSON required)"), 
        BotCommand("schedule", "Show today's class schedule"),
        BotCommand("next", "Show next class or next day's schedule"),
        BotCommand("cancel", "Cancel any active conversation (e.g., adding homework)"), 
        BotCommand("motivate", "Get a random motivational quote/joke"),
        BotCommand("kys", "Get a random message"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set")

async def main():
    global app, reminder_task
    
    if not acquire_lock():
        logger.error("Another instance is running")
        print("Error: Another instance is running. Delete bot.lock if needed.")
        sys.exit(1)
    
    logger.info("Starting multi-group bot...")
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        app = Application.builder().token(TOKEN).post_init(post_init).build()
        
        # Conversation handler for /set_timetable 
        timetable_conv_handler = ConversationHandler(
            entry_points=[CommandHandler("set_timetable", set_timetable_start)],
            states={
                SETTING_TIMETABLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_timetable_save)],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            allow_reentry=True,
            per_message=False,
        )
        app.add_handler(timetable_conv_handler)
        
        # New Conversation handler for /hw_long_add (interactive)
        hw_long_add_conv_handler = ConversationHandler(
            entry_points=[CommandHandler("hw_long_add", hw_long_add_start)],
            states={
                LONG_ADDING_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_subject_long)],
                LONG_ADDING_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_long)],
                LONG_ADDING_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date_and_save_long)],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            allow_reentry=True,
            per_message=False,
        )
        app.add_handler(hw_long_add_conv_handler)
        
        # Simple command handlers
        app.add_handler(CommandHandler("start", start))
        # ADDED BACK: Handler for quick one-line /hw_add
        app.add_handler(CommandHandler("hw_add", hw_quick_add))
        
        app.add_handler(CommandHandler("hw_list", hw_list))
        app.add_handler(CommandHandler("hw_remove", hw_remove))
        app.add_handler(CommandHandler("hw_today", hw_today))
        app.add_handler(CommandHandler("hw_overdue", hw_overdue))
        app.add_handler(CommandHandler("hw_stats", hw_stats))
        app.add_handler(CommandHandler("hw_clean", hw_clean))
        
        # Schedule commands 
        app.add_handler(CommandHandler("timetable", schedule_today)) 
        app.add_handler(CommandHandler("full_timetable", full_timetable))
        app.add_handler(CommandHandler("schedule", schedule_today))
        app.add_handler(CommandHandler("schedule_today", schedule_today))
        
        # Next class commands 
        app.add_handler(CommandHandler("next", next_class))
        app.add_handler(CommandHandler("next_class", next_class))
        
        app.add_handler(CommandHandler("motivate", motivate))
        app.add_handler(CommandHandler("kys", kys))
        
        logger.info("Handlers registered")
        
        await app.initialize()
        await app.start()
        
        logger.info("Bot started - polling")
        
        reminder_task = asyncio.create_task(reminder_scheduler())
        logger.info("Reminder task created")
        
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        logger.info("Bot running. Press Ctrl+C to stop.")
        
        await shutdown_event.wait()
        
    except Exception as e:
        logger.error(f"Error in main: {e}", exc_info=True)
    finally:
        logger.info("Shutting down...")
        
        if reminder_task:
            reminder_task.cancel()
            try:
                await reminder_task
            except asyncio.CancelledError:
                logger.info("Reminder task cancelled")
        
        if app:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
                logger.info("App shutdown complete")
            except Exception as e:
                logger.error(f"Error stopping: {e}")
        
        release_lock()
        logger.info("Lock released")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)