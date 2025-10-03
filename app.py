import json
import datetime
import asyncio
import logging
import os
import fcntl
import random
import pytz
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
from typing import Dict

# ====== CONFIG ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8466519086:AAFAMxZobhCtNldHC3CwF4EuU9gnwoMnT5A")
YOUR_GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "-1003007240886")
HOMEWORK_FILE = "homework.json"
LOCK_FILE = "bot.lock"
ARMENIA_TZ = pytz.timezone('Asia/Yerevan')

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")
if not YOUR_GROUP_CHAT_ID:
    raise ValueError("GROUP_CHAT_ID environment variable is required")

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

# ====== CONVERSATION STATES ======
SUBJECT, TASK, DUE_DATE, CONFIRM = range(4)

# ====== TIMETABLE ======
TIMETABLE = {
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

# ====== GLOBAL VARIABLES ======
app = None
reminder_task = None
shutdown_event = asyncio.Event()
lock_file = None

# ====== DATA HELPERS ======
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

def load_homework():
    return load_json_file(HOMEWORK_FILE)

def save_homework(hw):
    save_json_file(HOMEWORK_FILE, hw)

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

# ====== UTILITY FUNCTIONS ======
def get_week_type(date: datetime.date = None) -> str:
    """Get week type for a specific date (defaults to today)"""
    if date is None:
        date = datetime.date.today()
    week_num = date.isocalendar()[1]
    return "ч/н" if week_num % 2 == 0 else "н/ч"

def is_lesson_this_week(lesson: Dict, date: datetime.date = None) -> bool:
    """Check if a lesson happens on the given date (defaults to today)"""
    if "week" not in lesson:
        return True
    week_type = get_week_type(date)
    return lesson["week"] == week_type

def parse_flexible_date(date_str: str) -> datetime.date:
    """Parse flexible date formats"""
    today = datetime.date.today()
    date_lower = date_str.lower().strip()
    
    if date_lower == "today":
        return today
    elif date_lower == "tomorrow":
        return today + datetime.timedelta(days=1)
    elif date_lower == "next week":
        return today + datetime.timedelta(days=7)
    elif date_lower.startswith('+'):
        try:
            days = int(date_lower[1:])
            return today + datetime.timedelta(days=days)
        except ValueError:
            raise ValueError(f"Invalid relative date: {date_str}")
    else:
        # Try parsing as YYYY-MM-DD
        return datetime.datetime.strptime(date_str, '%Y-%m-%d').date()

# ====== BASIC COMMANDS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    welcome_msg = (
        "Study Bot\n\n"
        "New Homework System:\n"
        "/hw_add - Step-by-step (best for complex tasks)\n"
        "/hw_quick - Quick one-liner\n\n"
        "Homework Management:\n"
        "/hw_list - List all homework\n"
        "/hw_remove - Remove homework\n"
        "/hw_today - Show today's homework\n"
        "/hw_overdue - Show overdue homework\n"
        "/hw_stats - Show statistics\n"
        "/hw_clean - Clean old homework\n\n"
        "Schedule:\n"
        "/schedule - Today's schedule\n"
        "/next - Next class\n\n"
        "Motivation:\n"
        "/motivate - type of motivation you always needed\n"
        "/kys - )))"
    )
    await update.message.reply_text(welcome_msg)

# ====== IMPROVED HOMEWORK SYSTEM ======
# Method 1: Conversational Flow

async def hw_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start conversational homework addition"""
    await update.message.reply_text(
        "Add homework!\n\n"
        "What subject is this for?\n"
        "(or /cancel to stop)"
    )
    return SUBJECT

async def hw_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store subject and ask for task"""
    context.user_data['hw_subject'] = update.message.text
    
    await update.message.reply_text(
        f"Subject: {update.message.text}\n\n"
        "Now, describe the homework task.\n"
        "You can write as much as you need - multiple lines are okay!\n\n"
        "When done, send /done"
    )
    context.user_data['hw_task_parts'] = []
    return TASK

async def hw_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect task description (can be multiple messages)"""
    if update.message.text == '/done':
        if not context.user_data.get('hw_task_parts'):
            await update.message.reply_text("You haven't entered any task description yet!")
            return TASK
        
        full_task = "\n".join(context.user_data['hw_task_parts'])
        context.user_data['hw_task'] = full_task
        
        # Show quick date buttons
        keyboard = [
            [
                InlineKeyboardButton("Today", callback_data="date_today"),
                InlineKeyboardButton("Tomorrow", callback_data="date_tomorrow"),
            ],
            [
                InlineKeyboardButton("In 3 days", callback_data="date_3days"),
                InlineKeyboardButton("In 1 week", callback_data="date_week"),
            ],
            [
                InlineKeyboardButton("Custom date", callback_data="date_custom")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        preview = full_task[:100] + "..." if len(full_task) > 100 else full_task
        await update.message.reply_text(
            f"Task saved!\n\n"
            f"Preview: {preview}\n\n"
            f"When is this due?",
            reply_markup=reply_markup
        )
        return DUE_DATE
    else:
        # Accumulate task description
        context.user_data['hw_task_parts'].append(update.message.text)
        part_count = len(context.user_data['hw_task_parts'])
        await update.message.reply_text(
            f"Part {part_count} added.\n"
            f"Continue writing or send /done when finished."
        )
        return TASK

async def hw_date_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quick date selection"""
    query = update.callback_query
    await query.answer()
    
    today = datetime.date.today()
    
    if query.data == "date_today":
        due_date = today
    elif query.data == "date_tomorrow":
        due_date = today + datetime.timedelta(days=1)
    elif query.data == "date_3days":
        due_date = today + datetime.timedelta(days=3)
    elif query.data == "date_week":
        due_date = today + datetime.timedelta(days=7)
    elif query.data == "date_custom":
        await query.edit_message_text(
            "Enter the due date in YYYY-MM-DD format\n"
            "Example: 2025-10-15\n\n"
            "Or use: tomorrow, today, next week, +N (days)"
        )
        return DUE_DATE
    else:
        return DUE_DATE
    
    context.user_data['hw_due'] = due_date.isoformat()
    
    # Show confirmation
    await show_homework_confirmation(query, context)
    return CONFIRM

async def hw_date_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom date input"""
    try:
        due_date = parse_flexible_date(update.message.text)
        context.user_data['hw_due'] = due_date.isoformat()
        
        # Show confirmation
        subject = context.user_data['hw_subject']
        task = context.user_data['hw_task']
        preview = task[:200] + "..." if len(task) > 200 else task
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="confirm_yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="confirm_no"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"Review your homework:\n\n"
            f"Subject: {subject}\n"
            f"Due: {due_date.strftime('%Y-%m-%d (%A)')}\n\n"
            f"Task:\n{preview}\n\n"
            f"Is this correct?",
            reply_markup=reply_markup
        )
        return CONFIRM
    except ValueError as e:
        await update.message.reply_text(
            f"Invalid date format: {str(e)}\n\n"
            "Please use one of these formats:\n"
            "• YYYY-MM-DD (e.g., 2025-10-15)\n"
            "• tomorrow\n"
            "• today\n"
            "• next week\n"
            "• +N (e.g., +3 for 3 days from now)"
        )
        return DUE_DATE

async def show_homework_confirmation(query, context: ContextTypes.DEFAULT_TYPE):
    """Show homework confirmation message"""
    subject = context.user_data['hw_subject']
    task = context.user_data['hw_task']
    due_date = datetime.datetime.strptime(context.user_data['hw_due'], '%Y-%m-%d').date()
    preview = task[:200] + "..." if len(task) > 200 else task
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data="confirm_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="confirm_no"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"Review your homework:\n\n"
        f"Subject: {subject}\n"
        f"Due: {due_date.strftime('%Y-%m-%d (%A)')}\n\n"
        f"Task:\n{preview}\n\n"
        f"Is this correct?",
        reply_markup=reply_markup
    )

async def hw_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save homework after confirmation"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_yes":
        # Save homework
        hw = load_homework()
        subject = context.user_data['hw_subject']
        
        hw_item = {
            "task": context.user_data['hw_task'],
            "due": context.user_data['hw_due'],
            "added": datetime.date.today().isoformat()
        }
        
        hw.setdefault(subject, []).append(hw_item)
        save_homework(hw)
        
        task_preview = hw_item['task'][:80] + "..." if len(hw_item['task']) > 80 else hw_item['task']
        
        await query.edit_message_text(
            f"Homework added successfully!\n\n"
            f"Subject: {subject}\n"
            f"Task: {task_preview}\n"
            f"Due: {context.user_data['hw_due']}"
        )
        logger.info(f"Added homework: {subject} - {hw_item['task'][:50]}...")
    else:
        await query.edit_message_text("Homework addition cancelled.")
    
    # Clear user data
    context.user_data.clear()
    return ConversationHandler.END

async def hw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel homework addition"""
    await update.message.reply_text("Homework addition cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# Method 2: Quick one-liner

async def hw_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick homework add - just type everything naturally"""
    if len(context.args) < 1:
        await update.message.reply_text(
            "⚡ Quick add format:\n"
            "/hw_quick Subject | Task description | due date\n\n"
            "Examples:\n"
            "• /hw_quick Diffur | Solve problems 1-10 from chapter 3 | tomorrow\n"
            "• /hw_quick Python | Create web scraper with error handling | 2025-10-15\n"
            "• /hw_quick Physics | Lab report on electromagnetic induction | next week\n"
            "• /hw_quick Database | Complete assignment on normalization | +5\n\n"
            "Date formats:\n"
            "• tomorrow, today, next week\n"
            "• YYYY-MM-DD (e.g., 2025-10-15)\n"
            "• +N (e.g., +3 for 3 days from now)"
        )
        return
    
    full_text = " ".join(context.args)
    parts = [p.strip() for p in full_text.split('|')]
    
    if len(parts) < 3:
        await update.message.reply_text(
            "Format: Subject | Task | Due date\n"
            "Use | to separate parts\n\n"
            "Example:\n"
            "/hw_quick Python | Create API client | tomorrow"
        )
        return
    
    subject = parts[0]
    task = parts[1]
    date_str = parts[2]
    
    # Parse flexible date formats
    try:
        due_date = parse_flexible_date(date_str)
    except ValueError as e:
        await update.message.reply_text(
            f"Invalid date: {date_str}\n\n"
            "Use: tomorrow, today, next week, +N (days), or YYYY-MM-DD"
        )
        return
    
    # Save homework
    hw = load_homework()
    hw_item = {
        "task": task,
        "due": due_date.isoformat(),
        "added": datetime.date.today().isoformat()
    }
    
    hw.setdefault(subject, []).append(hw_item)
    save_homework(hw)
    
    task_preview = task[:100] + "..." if len(task) > 100 else task
    
    await update.message.reply_text(
        f"Added homework:\n\n"
        f"{subject}\n"
        f"{task_preview}\n"
        f"Due: {due_date.strftime('%Y-%m-%d (%A)')}"
    )
    logger.info(f"Quick added homework: {subject} - {task[:50]}...")

# ====== HOMEWORK MANAGEMENT ======
async def hw_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework logged")
            return
        
        total = sum(len(tasks) for tasks in hw.values())
        overdue = 0
        due_today = 0
        due_tomorrow = 0
        
        today = datetime.date.today()
        tomorrow = today + datetime.timedelta(days=1)
        
        for tasks in hw.values():
            for task in tasks:
                try:
                    due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due_date < today:
                        overdue += 1
                    elif due_date == today:
                        due_today += 1
                    elif due_date == tomorrow:
                        due_tomorrow += 1
                except ValueError:
                    logger.error(f"Invalid date format in homework: {task}")
        
        msg = f"Homework Statistics:\n\n"
        msg += f"Total pending: {total}\n"
        msg += f"Overdue: {overdue}\n"
        msg += f"Due today: {due_today}\n"
        msg += f"Due tomorrow: {due_tomorrow}\n"
        
        if overdue > 0:
            msg += f"\n⚠️ You have {overdue} overdue assignments"
        elif due_today > 0:
            msg += f"\nYou have {due_today} assignments due today"
        else:
            msg += f"\nYou're on track"
        
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in hw_stats: {e}")
        await update.message.reply_text("Error getting homework statistics")

async def hw_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework found")
            return
        
        cutoff_date = datetime.date.today() - datetime.timedelta(days=30)
        cleaned_count = 0
        
        for subject in list(hw.keys()):
            tasks_to_keep = []
            for task in hw[subject]:
                try:
                    due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due_date >= cutoff_date:
                        tasks_to_keep.append(task)
                    else:
                        cleaned_count += 1
                except ValueError:
                    tasks_to_keep.append(task)
            
            if tasks_to_keep:
                hw[subject] = tasks_to_keep
            else:
                del hw[subject]
        
        save_homework(hw)
        
        if cleaned_count > 0:
            await update.message.reply_text(f"Cleaned {cleaned_count} old assignments")
        else:
            await update.message.reply_text("Nothing to clean")
    except Exception as e:
        logger.error(f"Error in hw_clean: {e}")
        await update.message.reply_text("Error cleaning homework")

async def hw_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hw = load_homework()
        today = datetime.date.today().isoformat()
        
        today_hw = []
        for subject, tasks in hw.items():
            for task in tasks:
                if task["due"] == today:
                    today_hw.append((subject, task))
        
        if not today_hw:
            await update.message.reply_text("No homework due today")
            return
        
        msg = "Due today:\n\n"
        for i, (subject, task) in enumerate(today_hw, 1):
            task_preview = task['task'][:80] + "..." if len(task['task']) > 80 else task['task']
            msg += f"{i}. {subject}: {task_preview}\n\n"
        
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in hw_today: {e}")
        await update.message.reply_text("Error getting today's homework")

async def hw_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework")
            return
        
        today = datetime.date.today()
        overdue_hw = []
        
        for subject, tasks in hw.items():
            for task in tasks:
                try:
                    due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due_date < today:
                        days_overdue = (today - due_date).days
                        overdue_hw.append((subject, task, days_overdue, due_date))
                except ValueError:
                    logger.error(f"Invalid date format in homework: {task}")
        
        if not overdue_hw:
            await update.message.reply_text("No overdue homework")
            return
        
        overdue_hw.sort(key=lambda x: x[3])
        
        msg = f"⚠️ Overdue ({len(overdue_hw)}):\n\n"
        
        for i, (subject, task, days_overdue, due_date) in enumerate(overdue_hw, 1):
            task_preview = task['task'][:60] + "..." if len(task['task']) > 60 else task['task']
            msg += f"{i}. {subject}: {task_preview}\n"
            msg += f"   {task['due']} ({days_overdue}d overdue)\n\n"
        
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in hw_overdue: {e}")
        await update.message.reply_text("Error getting overdue homework")

def escape_markdown(text: str) -> str:
    special_chars = ['*', '_', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

async def hw_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework logged")
            return
        
        msg = "Homework:\n\n"
        
        sorted_subjects = sorted(hw.keys())
        for subject_idx, subject in enumerate(sorted_subjects, 1):
            msg += f"{subject_idx}. *{escape_markdown(subject)}*:\n"
            tasks = hw[subject]
            
            tasks_with_status = []
            for i, task in enumerate(tasks, 1):
                try:
                    due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    today = datetime.date.today()
                    days_left = (due_date - today).days
                    
                    if days_left < 0:
                        status = f"OVERDUE \\({abs(days_left)} days\\)"
                    elif days_left == 0:
                        status = "DUE TODAY"
                    elif days_left == 1:
                        status = "DUE TOMORROW"
                    else:
                        status = f"{days_left} days left"
                    
                    tasks_with_status.append((i, task, status, due_date))
                except ValueError:
                    tasks_with_status.append((i, task, "Invalid date", None))
            
            tasks_with_status.sort(key=lambda x: x[3] if x[3] else datetime.date.max)
            
            for i, task, status, _ in tasks_with_status:
                task_text = task['task'][:100] + "..." if len(task['task']) > 100 else task['task']
                safe_task = escape_markdown(task_text)
                safe_due = escape_markdown(task['due'])
                msg += f"   {i}\\. {safe_task}\n      Due {safe_due} \\({status}\\)\n"
            msg += "\n"
        
        try:
            await update.message.reply_text(msg, parse_mode='MarkdownV2')
        except Exception as markdown_error:
            logger.warning(f"MarkdownV2 failed, trying plain text: {markdown_error}")
            # Fallback to plain text
            plain_msg = "📚 Homework:\n\n"
            for subject_idx, subject in enumerate(sorted_subjects, 1):
                plain_msg += f"{subject_idx}. {subject}:\n"
                tasks = hw[subject]
                
                tasks_with_status = []
                for i, task in enumerate(tasks, 1):
                    try:
                        due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                        today = datetime.date.today()
                        days_left = (due_date - today).days
                        
                        if days_left < 0:
                            status = f"OVERDUE ({abs(days_left)} days)"
                        elif days_left == 0:
                            status = "DUE TODAY"
                        elif days_left == 1:
                            status = "DUE TOMORROW"
                        else:
                            status = f"{days_left} days left"
                        
                        tasks_with_status.append((i, task, status, due_date))
                    except ValueError:
                        tasks_with_status.append((i, task, "Invalid date", None))
                
                tasks_with_status.sort(key=lambda x: x[3] if x[3] else datetime.date.max)
                
                for i, task, status, _ in tasks_with_status:
                    task_text = task['task'][:100] + "..." if len(task['task']) > 100 else task['task']
                    plain_msg += f"   {i}. {task_text}\n      Due {task['due']} ({status})\n"
                plain_msg += "\n"
            
            await update.message.reply_text(plain_msg)
            
    except Exception as e:
        logger.error(f"Error in hw_list: {e}")
        await update.message.reply_text("Error listing homework")

async def hw_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /hw_remove Subject homework_index\n"
                "       /hw_remove subject_index homework_index\n"
                "Examples:\n"
                "• /hw_remove Diffur 1\n"
                "• /hw_remove 2 1"
            )
            return

        subject_input = context.args[0]
        try:
            homework_index = int(context.args[1]) - 1
        except ValueError:
            await update.message.reply_text("❌ Homework index must be a number")
            return

        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework found")
            return

        subject = None
        try:
            subject_idx = int(subject_input) - 1
            sorted_subjects = sorted(hw.keys())
            if 0 <= subject_idx < len(sorted_subjects):
                subject = sorted_subjects[subject_idx]
            else:
                await update.message.reply_text(f"❌ Invalid subject index. Available subjects: 1-{len(sorted_subjects)}")
                return
        except ValueError:
            if subject_input in hw:
                subject = subject_input
            else:
                await update.message.reply_text(f"❌ Subject '{subject_input}' not found")
                return
        
        if homework_index < 0 or homework_index >= len(hw[subject]):
            await update.message.reply_text(f"❌ Invalid homework index. {subject} has {len(hw[subject])} homework items")
            return

        removed_task = hw[subject].pop(homework_index)
        
        if not hw[subject]:
            del hw[subject]
        
        save_homework(hw)
        task_preview = removed_task['task'][:60] + "..." if len(removed_task['task']) > 60 else removed_task['task']
        await update.message.reply_text(f"✅ Removed: {subject} - {task_preview}")
        logger.info(f"Removed homework: {subject} - {removed_task['task'][:50]}...")
    except Exception as e:
        logger.error(f"Error in hw_remove: {e}")
        await update.message.reply_text("Error removing homework")

# ====== SCHEDULE COMMANDS ======
async def schedule_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        day = datetime.date.today().strftime("%A")
        lessons = TIMETABLE.get(day, [])
        
        if not lessons:
            await update.message.reply_text("📅 No classes today")
            return
        
        msg = f"{day}:\n\n"
        
        lesson_count = 0
        for lesson in lessons:
            if lesson["subject"] and is_lesson_this_week(lesson):
                lesson_count += 1
                type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                msg += f"{lesson_count}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
        
        if lesson_count == 0:
            await update.message.reply_text("📅 No classes this week")
        else:
            await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in schedule_today: {e}")
        await update.message.reply_text("Error getting schedule")

async def next_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.date.today()
        current_day = today.strftime("%A")
        
        # Check remaining classes today
        lessons_today = TIMETABLE.get(current_day, [])
        
        if lessons_today:
            remaining_lessons = []
            for lesson in lessons_today:
                if lesson["subject"] and is_lesson_this_week(lesson, today):
                    remaining_lessons.append(lesson)
            
            if remaining_lessons:
                msg = f"📍 Remaining today ({current_day}):\n\n"
                for idx, lesson in enumerate(remaining_lessons, 1):
                    type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                    week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                    msg += f"{idx}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
                await update.message.reply_text(msg)
                return
        
        # Find next day with classes
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        current_day_idx = days.index(current_day)
        
        for i in range(1, 8):
            next_day_idx = (current_day_idx + i) % 7
            next_day = days[next_day_idx]
            next_date = today + datetime.timedelta(days=i)
            
            lessons = TIMETABLE.get(next_day, [])
            upcoming_lessons = []
            
            for lesson in lessons:
                if lesson["subject"] and is_lesson_this_week(lesson, next_date):
                    upcoming_lessons.append(lesson)
            
            if upcoming_lessons:
                msg = f"📍 Next ({next_day} {next_date.strftime('%m-%d')}):\n\n"
                for idx, lesson in enumerate(upcoming_lessons, 1):
                    type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                    week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                    msg += f"{idx}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
                await update.message.reply_text(msg)
                return
        
        await update.message.reply_text("📅 No upcoming classes")
    except Exception as e:
        logger.error(f"Error in next_class: {e}")
        await update.message.reply_text("Error getting next class")

# ====== FUN COMMANDS ======
async def kys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        messages = [
            "nigga?",
            "hambal",
            "а ты не только зашел???",
            "likvid.",
            "es el qez em sirum", 
            "poshol naxuy",
        ]
        
        message = random.choice(messages)
        await update.message.reply_text(message)
    except Exception as e:
        logger.error(f"Error in kys: {e}")

async def motivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        
        message = random.choice(messages)
        await update.message.reply_text(message)
    except Exception as e:
        logger.error(f"Error in motivate: {e}")

# ====== REMINDERS ======
last_reminder_date = None
last_reminder_times = set()

async def send_daily_reminder():
    global last_reminder_date, last_reminder_times
    
    try:
        if not app:
            return
        
        # Get current time in Armenia timezone
        now = datetime.datetime.now(ARMENIA_TZ)
        today_date = now.date()
        current_time = now.strftime("%H:%M")
        
        # Reset tracking at midnight
        if last_reminder_date != today_date:
            last_reminder_date = today_date
            last_reminder_times = set()
        
        # 8:00 AM - Morning schedule reminder
        if current_time == "08:00" and "08:00" not in last_reminder_times:
            await send_morning_reminder()
            last_reminder_times.add("08:00")
            logger.info("Sent 8:00 AM morning reminder")
        
        # 6:00 PM (18:00) - Evening homework reminder
        elif current_time == "18:00" and "18:00" not in last_reminder_times:
            await send_evening_homework_reminder()
            last_reminder_times.add("18:00")
            logger.info("Sent 6:00 PM evening homework reminder")
            
    except Exception as e:
        logger.error(f"Error in daily reminder: {e}")

async def send_morning_reminder():
    """8:00 AM - Remind about today's schedule"""
    try:
        day = datetime.date.today().strftime("%A")
        lessons = TIMETABLE.get(day, [])
        
        if not lessons:
            return
        
        msg = f"☀️ Good morning! Today's classes ({day}):\n\n"
        lesson_count = 0
        
        for lesson in lessons:
            if lesson["subject"] and is_lesson_this_week(lesson):
                lesson_count += 1
                type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                msg += f"{lesson_count}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
        
        if lesson_count > 0:
            await app.bot.send_message(chat_id=YOUR_GROUP_CHAT_ID, text=msg)
        
    except Exception as e:
        logger.error(f"Error sending morning reminder: {e}")

async def send_evening_homework_reminder():
    """6:00 PM - Remind about homework due today or overdue"""
    try:
        today = datetime.date.today()
        hw = load_homework()
        
        if not hw:
            return
        
        homework_reminders = []
        
        # Find homework due today or overdue
        for subject, tasks in hw.items():
            for task in tasks:
                try:
                    due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due_date <= today:
                        homework_reminders.append((subject, task, due_date))
                except ValueError:
                    logger.error(f"Invalid date in evening homework reminder: {task}")
        
        if not homework_reminders:
            return
        
        # Sort by due date (oldest first)
        homework_reminders.sort(key=lambda x: x[2])
        
        msg = f"📚 Evening homework check:\n\n"
        
        for subject, task, due_date in homework_reminders:
            days_overdue = (today - due_date).days
            
            if days_overdue > 0:
                status = f"⚠️ OVERDUE ({days_overdue} days)"
            else:
                status = "📌 DUE TODAY"
            
            task_preview = task['task'][:60] + "..." if len(task['task']) > 60 else task['task']
            msg += f"• {subject}: {task_preview}\n  {status} - {task['due']}\n\n"
        
        await app.bot.send_message(chat_id=YOUR_GROUP_CHAT_ID, text=msg)
        
    except Exception as e:
        logger.error(f"Error sending evening homework reminder: {e}")

async def reminder_scheduler():
    """Background task that runs every minute to check for scheduled reminders"""
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
            logger.error(f"Error in reminder scheduler: {e}")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                continue

# ====== SIGNAL HANDLERS ======
def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    logger.info(f"Received signal {signum}, shutting down...")
    shutdown_event.set()

# ====== MAIN ======
async def post_init(application: Application):
    """Set up bot commands after initialization"""
    commands = [
        BotCommand("start", "Start the bot and see available commands"),
        BotCommand("hw_add", "Add homework (step-by-step)"),
        BotCommand("hw_quick", "Quick add: Subject | Task | Date"),
        BotCommand("hw_list", "List all homework"),
        BotCommand("hw_remove", "Remove homework: /hw_remove Subject index"),
        BotCommand("hw_today", "Show today's homework"),
        BotCommand("hw_overdue", "Show overdue homework"),
        BotCommand("hw_stats", "Show homework statistics"),
        BotCommand("hw_clean", "Clean old homework (30+ days)"),
        BotCommand("schedule", "Show today's schedule"),
        BotCommand("next", "Show next class"),
        BotCommand("motivate", "Get motivated"),
        BotCommand("kys", "Random message"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set successfully")

async def main():
    """Main function to run the bot"""
    global app, reminder_task
    
    # Acquire lock to prevent multiple instances
    if not acquire_lock():
        logger.error("Another instance of the bot is already running. Exiting...")
        print("Error: Another instance of the bot is already running.")
        print("If you're sure no other instance is running, delete the 'bot.lock' file and try again.")
        sys.exit(1)
    
    logger.info("Starting study bot...")
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Build application
        app = Application.builder().token(TOKEN).post_init(post_init).build()
        
        # Register conversation handler for hw_add
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("hw_add", hw_add_start)],
            states={
                SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, hw_subject)],
                TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, hw_task)],
                DUE_DATE: [
                    CallbackQueryHandler(hw_date_button, pattern="^date_"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, hw_date_custom)
                ],
                CONFIRM: [CallbackQueryHandler(hw_confirm, pattern="^confirm_")]
            },
            fallbacks=[CommandHandler("cancel", hw_cancel)]
        )
        
        app.add_handler(conv_handler)
        
        # Register other command handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("hw_quick", hw_quick))
        app.add_handler(CommandHandler("hw_list", hw_list))
        app.add_handler(CommandHandler("hw_remove", hw_remove))
        app.add_handler(CommandHandler("hw_today", hw_today))
        app.add_handler(CommandHandler("hw_overdue", hw_overdue))
        app.add_handler(CommandHandler("hw_stats", hw_stats))
        app.add_handler(CommandHandler("hw_clean", hw_clean))
        app.add_handler(CommandHandler("schedule_today", schedule_today))
        app.add_handler(CommandHandler("schedule", schedule_today))  # Alias
        app.add_handler(CommandHandler("next_class", next_class))
        app.add_handler(CommandHandler("next", next_class))  # Alias
        app.add_handler(CommandHandler("motivate", motivate))
        app.add_handler(CommandHandler("kys", kys))
        
        logger.info("All command handlers registered")
        
        # Initialize the bot
        await app.initialize()
        await app.start()
        
        logger.info("Bot started successfully - polling for updates")
        
        # Start reminder scheduler in background
        reminder_task = asyncio.create_task(reminder_scheduler())
        logger.info("Reminder scheduler task created")
        
        # Start polling
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        logger.info("Updater started - bot is now running")
        
        # Keep the bot running
        logger.info("Bot is running. Press Ctrl+C to stop.")
        await shutdown_event.wait()
        
    except Exception as e:
        logger.error(f"Error in main: {e}", exc_info=True)
    finally:
        logger.info("Shutting down bot...")
        
        # Cancel reminder task
        if reminder_task:
            reminder_task.cancel()
            try:
                await reminder_task
            except asyncio.CancelledError:
                logger.info("Reminder task cancelled successfully")
        
        # Stop the bot
        if app:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                    logger.info("Updater stopped")
                await app.stop()
                logger.info("Application stopped")
                await app.shutdown()
                logger.info("Application shutdown complete")
            except Exception as e:
                logger.error(f"Error stopping bot: {e}")
        
        # Release lock
        release_lock()
        logger.info("Lock released, exiting")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, exiting...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)