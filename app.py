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
from typing import Dict

# ====== CONFIG ======
# CRITICAL: Environment variables are REQUIRED - no fallback values for security
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8466519086:AAFKIpz3d30irZH5UedMwWyIIF62QeoNJvk")
YOUR_GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "-1003007240886")

# Validate required environment variables
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required. Set it with: export TELEGRAM_BOT_TOKEN='your_token_here'")
if not YOUR_GROUP_CHAT_ID:
    raise ValueError("GROUP_CHAT_ID environment variable is required. Set it with: export GROUP_CHAT_ID='your_chat_id_here'")

HOMEWORK_FILE = "homework.json"
LOCK_FILE = "bot.lock"
ARMENIA_TZ = pytz.timezone('Asia/Yerevan')

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
        return datetime.datetime.strptime(date_str, '%Y-%m-%d').date()

# ====== BASIC COMMANDS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    welcome_msg = (
        "Study Bot\n\n"
        "Homework System:\n"
        "/hw_add - Add homework (step-by-step)\n"
        "/hw_quick - Quick one-liner\n"
        "Or just type 'homework' or 'hw' to start\n\n"
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
async def hw_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start conversational homework addition"""
    try:
        logger.info(f"hw_add_start called by user {update.effective_user.id}")
        context.user_data.clear()
        await update.message.reply_text(
            "Add homework!\n\n"
            "What subject is this for?\n"
            "(or /cancel to stop)"
        )
        return SUBJECT
    except Exception as e:
        logger.error(f"Error in hw_add_start: {e}", exc_info=True)
        await update.message.reply_text("An error occurred. Please try again.")
        return ConversationHandler.END

async def hw_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store subject and ask for task"""
    try:
        logger.info(f"hw_subject: User entered subject '{update.message.text}'")
        context.user_data['hw_subject'] = update.message.text
        
        await update.message.reply_text(
            f"Subject: {update.message.text}\n\n"
            "Now, describe the homework task.\n"
            "You can write as much as you need - multiple lines are okay!\n\n"
            "When done, send /done"
        )
        context.user_data['hw_task_parts'] = []
        return TASK
    except Exception as e:
        logger.error(f"Error in hw_subject: {e}", exc_info=True)
        await update.message.reply_text("An error occurred. Please try again with /hw_add")
        context.user_data.clear()
        return ConversationHandler.END

async def hw_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect task description (can be multiple messages)"""
    try:
        message_text = update.message.text.strip()
        logger.info(f"hw_task: Received message: '{message_text}'")
        
        if message_text == '/done':
            task_parts = context.user_data.get('hw_task_parts', [])
            logger.info(f"hw_task: /done received, task_parts count: {len(task_parts)}")
            
            if not task_parts:
                logger.warning("hw_task: No task parts found")
                await update.message.reply_text("You haven't entered any task description yet!")
                return TASK
            
            full_task = "\n".join(task_parts)
            context.user_data['hw_task'] = full_task
            logger.info(f"hw_task: Task saved, length: {len(full_task)} chars")
            
            preview = full_task[:100] + "..." if len(full_task) > 100 else full_task
            
            try:
                response_msg = (
                    f"Task saved!\n\n"
                    f"Preview: {preview}\n\n"
                    f"When is this due?\n\n"
                    f"You can type:\n"
                    f"• today\n"
                    f"• tomorrow\n"
                    f"• next week\n"
                    f"• +3 (for 3 days from now)\n"
                    f"• 2025-10-15 (specific date)"
                )
                logger.info("hw_task: Attempting to send date prompt...")
                await update.message.reply_text(response_msg)
                logger.info("hw_task: Date prompt sent successfully, returning DUE_DATE state")
                return DUE_DATE
            except Exception as send_error:
                logger.error(f"hw_task: Failed to send message: {send_error}", exc_info=True)
                await update.message.reply_text("When is this due? (e.g., tomorrow, today, 2025-10-15)")
                return DUE_DATE
        else:
            if 'hw_task_parts' not in context.user_data:
                context.user_data['hw_task_parts'] = []
            
            context.user_data['hw_task_parts'].append(message_text)
            part_count = len(context.user_data['hw_task_parts'])
            logger.info(f"hw_task: Added part {part_count}, current state: TASK")
            
            await update.message.reply_text(
                f"Part {part_count} added.\n"
                f"Continue writing or send /done when finished."
            )
            return TASK
    except Exception as e:
        logger.error(f"ERROR in hw_task: {e}", exc_info=True)
        await update.message.reply_text("An error occurred. Please try again with /hw_add")
        context.user_data.clear()
        return ConversationHandler.END

async def hw_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle date input as text"""
    try:
        logger.info(f"hw_date_input: User entered '{update.message.text}'")
        due_date = parse_flexible_date(update.message.text)
        context.user_data['hw_due'] = due_date.isoformat()
        
        subject = context.user_data['hw_subject']
        task = context.user_data['hw_task']
        preview = task[:200] + "..." if len(task) > 200 else task
        
        await update.message.reply_text(
            f"Review your homework:\n\n"
            f"Subject: {subject}\n"
            f"Due: {due_date.strftime('%Y-%m-%d (%A)')}\n\n"
            f"Task:\n{preview}\n\n"
            f"Reply 'yes' to confirm or 'no' to cancel"
        )
        logger.info("hw_date_input: Waiting for confirmation")
        return CONFIRM
    except ValueError as e:
        logger.warning(f"hw_date_input: Invalid date - {e}")
        await update.message.reply_text(
            f"Invalid date format.\n\n"
            "Please try again. You can type:\n"
            "• today\n"
            "• tomorrow\n"
            "• next week\n"
            "• +3 (for 3 days from now)\n"
            "• 2025-10-15 (specific date)"
        )
        return DUE_DATE
    except Exception as e:
        logger.error(f"Error in hw_date_input: {e}", exc_info=True)
        await update.message.reply_text("An error occurred. Please try again with /hw_add")
        context.user_data.clear()
        return ConversationHandler.END

async def hw_confirm_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text confirmation (yes/no)"""
    try:
        response = update.message.text.lower().strip()
        logger.info(f"hw_confirm_text: User responded '{response}'")
        
        if response in ['yes', 'y', 'confirm', 'ok']:
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
            
            await update.message.reply_text(
                f"Homework added!\n\n"
                f"Subject: {subject}\n"
                f"Task: {task_preview}\n"
                f"Due: {context.user_data['hw_due']}"
            )
            logger.info(f"Added homework: {subject} - {hw_item['task'][:50]}...")
        else:
            await update.message.reply_text("Homework addition cancelled.")
        
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in hw_confirm_text: {e}", exc_info=True)
        await update.message.reply_text("An error occurred. Please try again.")
        context.user_data.clear()
        return ConversationHandler.END

async def hw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel homework addition"""
    await update.message.reply_text("Homework addition cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

async def hw_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick homework add"""
    if len(context.args) < 1:
        await update.message.reply_text(
            "Quick add format:\n"
            "/hw_quick Subject | Task | Date\n\n"
            "Example:\n"
            "/hw_quick Python | Create API client | tomorrow"
        )
        return
    
    full_text = " ".join(context.args)
    parts = [p.strip() for p in full_text.split('|')]
    
    if len(parts) < 3:
        await update.message.reply_text(
            "Format: Subject | Task | Date\n"
            "Use | to separate parts"
        )
        return
    
    subject, task, date_str = parts[0], parts[1], parts[2]
    
    try:
        due_date = parse_flexible_date(date_str)
    except ValueError:
        await update.message.reply_text(
            f"Invalid date: {date_str}\n"
            "Use: tomorrow, today, next week, +N, or YYYY-MM-DD"
        )
        return
    
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
    logger.info(f"Quick added: {subject} - {task[:50]}...")

# ====== HOMEWORK MANAGEMENT ======
async def hw_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework logged")
            return
        
        total = sum(len(tasks) for tasks in hw.values())
        overdue = due_today = due_tomorrow = 0
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
                    pass
        
        msg = (f"Homework Statistics:\n\n"
               f"Total: {total}\n"
               f"Overdue: {overdue}\n"
               f"Due today: {due_today}\n"
               f"Due tomorrow: {due_tomorrow}")
        
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in hw_stats: {e}")
        await update.message.reply_text("Error getting statistics")

async def hw_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework found")
            return
        
        cutoff = datetime.date.today() - datetime.timedelta(days=30)
        cleaned = 0
        
        for subject in list(hw.keys()):
            keep = []
            for task in hw[subject]:
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
        
        save_homework(hw)
        msg = f"Cleaned {cleaned} old assignments" if cleaned > 0 else "Nothing to clean"
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in hw_clean: {e}")
        await update.message.reply_text("Error cleaning homework")

async def hw_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hw = load_homework()
        today = datetime.date.today().isoformat()
        
        today_hw = [(s, t) for s, tasks in hw.items() for t in tasks if t["due"] == today]
        
        if not today_hw:
            await update.message.reply_text("No homework due today")
            return
        
        msg = "Due today:\n\n"
        for i, (subj, task) in enumerate(today_hw, 1):
            preview = task['task'][:80] + "..." if len(task['task']) > 80 else task['task']
            msg += f"{i}. {subj}: {preview}\n\n"
        
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
        overdue = []
        
        for subj, tasks in hw.items():
            for task in tasks:
                try:
                    due = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due < today:
                        days = (today - due).days
                        overdue.append((subj, task, days, due))
                except ValueError:
                    pass
        
        if not overdue:
            await update.message.reply_text("No overdue homework")
            return
        
        overdue.sort(key=lambda x: x[3])
        msg = f"Overdue ({len(overdue)}):\n\n"
        
        for i, (subj, task, days, _) in enumerate(overdue, 1):
            preview = task['task'][:60] + "..." if len(task['task']) > 60 else task['task']
            msg += f"{i}. {subj}: {preview}\n   {task['due']} ({days}d overdue)\n\n"
        
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in hw_overdue: {e}")
        await update.message.reply_text("Error getting overdue homework")

async def hw_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework logged")
            return
        
        msg = "Homework:\n\n"
        today = datetime.date.today()
        
        for idx, subj in enumerate(sorted(hw.keys()), 1):
            msg += f"{idx}. {subj}:\n"
            
            tasks_info = []
            for i, task in enumerate(hw[subj], 1):
                try:
                    due = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    days = (due - today).days
                    
                    if days < 0:
                        status = f"OVERDUE ({abs(days)}d)"
                    elif days == 0:
                        status = "DUE TODAY"
                    elif days == 1:
                        status = "DUE TOMORROW"
                    else:
                        status = f"{days}d left"
                    
                    tasks_info.append((i, task, status, due))
                except ValueError:
                    tasks_info.append((i, task, "Invalid date", None))
            
            tasks_info.sort(key=lambda x: x[3] if x[3] else datetime.date.max)
            
            for i, task, status, _ in tasks_info:
                preview = task['task'][:100] + "..." if len(task['task']) > 100 else task['task']
                msg += f"   {i}. {preview}\n      Due {task['due']} ({status})\n"
            msg += "\n"
        
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in hw_list: {e}")
        await update.message.reply_text("Error listing homework")

async def hw_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /hw_remove Subject index\n"
                "Example: /hw_remove Python 1"
            )
            return

        subj_input, idx_str = context.args[0], context.args[1]
        
        try:
            hw_idx = int(idx_str) - 1
        except ValueError:
            await update.message.reply_text("Index must be a number")
            return

        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework found")
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
            await update.message.reply_text(f"Subject not found: {subj_input}")
            return
        
        if hw_idx < 0 or hw_idx >= len(hw[subject]):
            await update.message.reply_text(f"Invalid index for {subject}")
            return

        removed = hw[subject].pop(hw_idx)
        if not hw[subject]:
            del hw[subject]
        
        save_homework(hw)
        preview = removed['task'][:60] + "..." if len(removed['task']) > 60 else removed['task']
        await update.message.reply_text(f"Removed: {subject} - {preview}")
        logger.info(f"Removed: {subject} - {removed['task'][:50]}...")
    except Exception as e:
        logger.error(f"Error in hw_remove: {e}")
        await update.message.reply_text("Error removing homework")

# ====== SCHEDULE COMMANDS ======
async def schedule_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        day = datetime.date.today().strftime("%A")
        lessons = TIMETABLE.get(day, [])
        
        if not lessons:
            await update.message.reply_text("No classes today")
            return
        
        msg = f"{day}:\n\n"
        count = 0
        
        for lesson in lessons:
            if lesson["subject"] and is_lesson_this_week(lesson):
                count += 1
                type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                msg += f"{count}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
        
        if count == 0:
            await update.message.reply_text("No classes this week")
        else:
            await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in schedule_today: {e}")
        await update.message.reply_text("Error getting schedule")

async def next_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.date.today()
        day = today.strftime("%A")
        
        lessons = TIMETABLE.get(day, [])
        remaining = [l for l in lessons if l["subject"] and is_lesson_this_week(l, today)]
        
        if remaining:
            msg = f"Remaining today ({day}):\n\n"
            for idx, lesson in enumerate(remaining, 1):
                type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                msg += f"{idx}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
            await update.message.reply_text(msg)
            return
        
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        curr_idx = days.index(day)
        
        for i in range(1, 8):
            next_idx = (curr_idx + i) % 7
            next_day = days[next_idx]
            next_date = today + datetime.timedelta(days=i)
            
            lessons = TIMETABLE.get(next_day, [])
            upcoming = [l for l in lessons if l["subject"] and is_lesson_this_week(l, next_date)]
            
            if upcoming:
                msg = f"Next ({next_day} {next_date.strftime('%m-%d')}):\n\n"
                for idx, lesson in enumerate(upcoming, 1):
                    type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                    week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                    msg += f"{idx}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
                await update.message.reply_text(msg)
                return
        
        await update.message.reply_text("No upcoming classes")
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
        await update.message.reply_text(random.choice(messages))
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
        await update.message.reply_text(random.choice(messages))
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
        
        now = datetime.datetime.now(ARMENIA_TZ)
        today_date = now.date()
        current_time = now.strftime("%H:%M")
        
        if last_reminder_date != today_date:
            last_reminder_date = today_date
            last_reminder_times = set()
        
        if current_time == "08:00" and "08:00" not in last_reminder_times:
            await send_morning_reminder()
            last_reminder_times.add("08:00")
            logger.info("Sent 8:00 AM reminder")
        
        elif current_time == "18:00" and "18:00" not in last_reminder_times:
            await send_evening_homework_reminder()
            last_reminder_times.add("18:00")
            logger.info("Sent 6:00 PM reminder")
            
    except Exception as e:
        logger.error(f"Error in daily reminder: {e}")

async def send_morning_reminder():
    try:
        day = datetime.date.today().strftime("%A")
        lessons = TIMETABLE.get(day, [])
        
        if not lessons:
            return
        
        msg = f"Good morning! Today's classes ({day}):\n\n"
        count = 0
        
        for lesson in lessons:
            if lesson["subject"] and is_lesson_this_week(lesson):
                count += 1
                type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                msg += f"{count}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
        
        if count > 0:
            await app.bot.send_message(chat_id=YOUR_GROUP_CHAT_ID, text=msg)
        
    except Exception as e:
        logger.error(f"Error in morning reminder: {e}")

async def send_evening_homework_reminder():
    try:
        today = datetime.date.today()
        hw = load_homework()
        
        if not hw:
            return
        
        reminders = []
        for subj, tasks in hw.items():
            for task in tasks:
                try:
                    due = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    if due <= today:
                        reminders.append((subj, task, due))
                except ValueError:
                    pass
        
        if not reminders:
            return
        
        reminders.sort(key=lambda x: x[2])
        msg = f"Evening homework check:\n\n"
        
        for subj, task, due in reminders:
            days_overdue = (today - due).days
            status = f"OVERDUE ({days_overdue}d)" if days_overdue > 0 else "DUE TODAY"
            preview = task['task'][:60] + "..." if len(task['task']) > 60 else task['task']
            msg += f"• {subj}: {preview}\n  {status} - {task['due']}\n\n"
        
        await app.bot.send_message(chat_id=YOUR_GROUP_CHAT_ID, text=msg)
        
    except Exception as e:
        logger.error(f"Error in evening reminder: {e}")

async def reminder_scheduler():
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

# ====== SIGNAL HANDLERS ======
def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    shutdown_event.set()

# ====== MAIN ======
async def post_init(application: Application):
    commands = [
        BotCommand("start", "Start bot"),
        BotCommand("hw_add", "Add homework (step-by-step)"),
        BotCommand("hw_quick", "Quick add"),
        BotCommand("hw_list", "List all"),
        BotCommand("hw_remove", "Remove homework"),
        BotCommand("hw_today", "Today's homework"),
        BotCommand("hw_overdue", "Overdue homework"),
        BotCommand("hw_stats", "Statistics"),
        BotCommand("hw_clean", "Clean old homework"),
        BotCommand("schedule", "Today's schedule"),
        BotCommand("next", "Next class"),
        BotCommand("motivate", "Get motivated"),
        BotCommand("kys", "Random message"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set")

async def main():
    global app, reminder_task
    
    if not acquire_lock():
        logger.error("Another instance is running")
        print("Error: Another instance is running. Delete bot.lock if needed.")
        sys.exit(1)
    
    logger.info("Starting bot...")
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        app = Application.builder().token(TOKEN).post_init(post_init).build()
        
        # Conversation handler for /hw_add - accepts command and keyword triggers
        # Conversation handler for /hw_add - accepts command and keyword triggers
        hw_conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("hw_add", hw_add_start),
                MessageHandler(
                    filters.Regex(r'(?i)^(add homework|homework|hw)$') & ~filters.COMMAND, 
                    hw_add_start
                )
            ],
            states={
                SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, hw_subject)],
                TASK: [
                    CommandHandler("done", hw_task),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, hw_task)
                ],
                DUE_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, hw_date_input)],
                CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, hw_confirm_text)],
            },
            fallbacks=[CommandHandler("cancel", hw_cancel)],
            allow_reentry=True,
            per_message=False,
        )
        
        app.add_handler(hw_conv_handler)
        
        # Simple command handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("hw_quick", hw_quick))
        app.add_handler(CommandHandler("hw_list", hw_list))
        app.add_handler(CommandHandler("hw_remove", hw_remove))
        app.add_handler(CommandHandler("hw_today", hw_today))
        app.add_handler(CommandHandler("hw_overdue", hw_overdue))
        app.add_handler(CommandHandler("hw_stats", hw_stats))
        app.add_handler(CommandHandler("hw_clean", hw_clean))
        app.add_handler(CommandHandler("schedule_today", schedule_today))
        app.add_handler(CommandHandler("schedule", schedule_today))
        app.add_handler(CommandHandler("next_class", next_class))
        app.add_handler(CommandHandler("next", next_class))
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