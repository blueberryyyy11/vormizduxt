import json
import datetime
import asyncio
import logging
import os
import fcntl
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
import signal
import sys
from typing import Dict

# ====== CONFIG ======
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8466519086:AAFAMxZobhCtNldHC3CwF4EuU9gnwoMnT5A")
YOUR_GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "-1003007240886")
HOMEWORK_FILE = "homework.json"
LOCK_FILE = "bot.lock"

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
    """Acquire a file lock to ensure only one instance runs"""
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
    """Release the file lock"""
    global lock_file
    if lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            os.unlink(LOCK_FILE)
        except (IOError, OSError):
            pass

# ====== UTILITY FUNCTIONS ======
def get_week_type() -> str:
    """Determine if it's четная (even) or нечетная (odd) week"""
    week_num = datetime.date.today().isocalendar()[1]
    return "ч/н" if week_num % 2 == 0 else "н/ч"

def is_lesson_this_week(lesson: Dict) -> bool:
    """Check if a lesson happens this week"""
    if "week" not in lesson:
        return True
    current_week = get_week_type()
    return lesson["week"] == current_week

# ====== COMMANDS ======
async def hw_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show homework statistics"""
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
            msg += f"\nYou have {overdue} overdue assignments"
        elif due_today > 0:
            msg += f"\nYou have {due_today} assignments due today"
        else:
            msg += f"\nYou're on track"
        
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in hw_stats: {e}")
        await update.message.reply_text("Error getting homework statistics")

async def hw_clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove all overdue homework older than 30 days"""
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
                    # Keep tasks with invalid dates for manual review
                    tasks_to_keep.append(task)
            
            if tasks_to_keep:
                hw[subject] = tasks_to_keep
            else:
                del hw[subject]
        
        save_homework(hw)
        
        if cleaned_count > 0:
            await update.message.reply_text(f"Cleaned {cleaned_count} old overdue assignments")
        else:
            await update.message.reply_text("No old assignments to clean")
    except Exception as e:
        logger.error(f"Error in hw_clean: {e}")
        await update.message.reply_text("Error cleaning homework")

async def hw_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show homework due today"""
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
        
        msg = "Homework due today:\n\n"
        for i, (subject, task) in enumerate(today_hw, 1):
            msg += f"{i}. {subject}: {task['task']}\n"
        
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in hw_today: {e}")
        await update.message.reply_text("Error getting today's homework")

async def schedule_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's schedule"""
    try:
        day = datetime.date.today().strftime("%A")
        lessons = TIMETABLE.get(day, [])
        
        if not lessons:
            await update.message.reply_text("No classes today")
            return
        
        msg = f"Today's schedule ({day}):\n\n"
        
        lesson_count = 0
        for lesson in lessons:
            if lesson["subject"] and is_lesson_this_week(lesson):
                lesson_count += 1
                type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                msg += f"{lesson_count}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
        
        if lesson_count == 0:
            await update.message.reply_text("No classes this week")
        else:
            await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Error in schedule_today: {e}")
        await update.message.reply_text("Error getting today's schedule")

async def next_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show next upcoming class"""
    try:
        today = datetime.date.today()
        current_day = today.strftime("%A")
        
        # Check remaining classes today first
        lessons_today = TIMETABLE.get(current_day, [])
        
        if lessons_today:
            msg = f"Remaining classes today ({current_day}):\n\n"
            lesson_count = 0
            for lesson in lessons_today:
                if lesson["subject"] and is_lesson_this_week(lesson):
                    lesson_count += 1
                    type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                    week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                    msg += f"{lesson_count}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
            
            if lesson_count > 0:
                await update.message.reply_text(msg)
                return
        
        # Find next day with classes
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        current_day_idx = days.index(current_day)
        
        for i in range(1, 8):  # Check next 7 days
            next_day_idx = (current_day_idx + i) % 7
            next_day = days[next_day_idx]
            next_date = today + datetime.timedelta(days=i)
            
            lessons = TIMETABLE.get(next_day, [])
            if lessons and any(lesson["subject"] and is_lesson_this_week(lesson) for lesson in lessons):
                msg = f"Next classes ({next_day} {next_date.strftime('%m-%d')}):\n\n"
                lesson_count = 0
                for lesson in lessons:
                    if lesson["subject"] and is_lesson_this_week(lesson):
                        lesson_count += 1
                        type_info = f" ({lesson['type']})" if lesson.get('type') else ""
                        week_info = f" [{lesson['week']}]" if lesson.get('week') else ""
                        msg += f"{lesson_count}. {lesson['subject']} - {lesson['room']}{type_info}{week_info}\n"
                await update.message.reply_text(msg)
                return
        
        await update.message.reply_text("No upcoming classes found")
    except Exception as e:
        logger.error(f"Error in next_class: {e}")
        await update.message.reply_text("Error getting next class")

async def kys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        import random
        messages = [
            "nigga?",
            "hambal",
            "es el qez em sirum", 
            "poshol naxuy",
        ]
        
        message = random.choice(messages)
        await update.message.reply_text(message)
    except Exception as e:
        logger.error(f"Error in kys: {e}")

async def motivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        import random
        messages = [
            "soberis tryapka",
            "ուզում ես մոդուլը գա վատ գրես նեղվես հետո նոր ուշքի գաս հա արա՞՞՞՞՞՞՞",
            "ape heraxosd shprti dasd ara", 
            "hishi vor mard ka qeznic poqr a u arden senior a",
            "es im vaxtov jamy 4in ei zartnum vor matanaliz anei",
            "porsche es uzum? de sovori (iharke eskortnicayi tarberaky misht ka bayc du sovori)",
        ]
        
        message = random.choice(messages)
        await update.message.reply_text(message)
    except Exception as e:
        logger.error(f"Error in motivate: {e}")

async def hw_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add homework"""
    try:
        if len(context.args) < 3:
            await update.message.reply_text(
                "Usage: /hw_add Subject Task YYYY-MM-DD\n"
                "Example: /hw_add Diffur kaxvel:) 2025-01-15"
            )
            return
        
        subject = context.args[0]
        due_date = context.args[-1]
        task = " ".join(context.args[1:-1])
        
        # Validate date format
        try:
            datetime.datetime.strptime(due_date, '%Y-%m-%d')
        except ValueError:
            await update.message.reply_text("Invalid date format. Use YYYY-MM-DD")
            return
        
        hw = load_homework()
        hw_item = {
            "task": task,
            "due": due_date,
            "added": datetime.date.today().isoformat()
        }
        
        hw.setdefault(subject, []).append(hw_item)
        save_homework(hw)
        
        await update.message.reply_text(f"Added homework for {subject}: {task} (due {due_date})")
        logger.info(f"User added HW: {subject} - {task} ({due_date})")
    except Exception as e:
        logger.error(f"Error in hw_add: {e}")
        await update.message.reply_text("Error adding homework")

def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown"""
    special_chars = ['*', '_', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

async def hw_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all homework grouped by subject with subject indexes"""
    try:
        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework logged")
            return
        
        msg = "Current Homework:\n\n"
        
        # Sort subjects alphabetically and add indexes
        sorted_subjects = sorted(hw.keys())
        for subject_idx, subject in enumerate(sorted_subjects, 1):
            msg += f"{subject_idx}. *{escape_markdown(subject)}*:\n"
            tasks = hw[subject]
            
            # Sort tasks by due date within each subject
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
            
            # Sort by due date (handle None values)
            tasks_with_status.sort(key=lambda x: x[3] if x[3] else datetime.date.max)
            
            for i, task, status, _ in tasks_with_status:
                safe_task = escape_markdown(task['task'])
                msg += f"   {i}\\. {safe_task} \\- Due {task['due']} \\({status}\\)\n"
            msg += "\n"
        
        # Try markdown first, fallback to plain text if it fails
        try:
            await update.message.reply_text(msg, parse_mode='MarkdownV2')
        except Exception as markdown_error:
            logger.warning(f"MarkdownV2 failed, trying plain text: {markdown_error}")
            # Create plain text version
            plain_msg = "Current Homework:\n\n"
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
                    plain_msg += f"   {i}. {task['task']} - Due {task['due']} ({status})\n"
                plain_msg += "\n"
            
            await update.message.reply_text(plain_msg)
            
    except Exception as e:
        logger.error(f"Error in hw_list: {e}")
        await update.message.reply_text("Error listing homework")

async def hw_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove homework by subject index/name and homework index"""
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /hw_remove Subject homework_index\n"
                "       /hw_remove subject_index homework_index\n"
                "Examples: /hw_remove Diffur 1\n"
            )
            return

        subject_input = context.args[0]
        try:
            homework_index = int(context.args[1]) - 1
        except ValueError:
            await update.message.reply_text("Homework index must be a number")
            return

        hw = load_homework()
        if not hw:
            await update.message.reply_text("No homework found")
            return

        # Check if first argument is a number (subject index) or subject name
        subject = None
        try:
            subject_idx = int(subject_input) - 1
            sorted_subjects = sorted(hw.keys())
            if 0 <= subject_idx < len(sorted_subjects):
                subject = sorted_subjects[subject_idx]
            else:
                await update.message.reply_text(f"Invalid subject index. Available subjects: 1-{len(sorted_subjects)}")
                return
        except ValueError:
            # It's a subject name
            if subject_input in hw:
                subject = subject_input
            else:
                await update.message.reply_text(f"Subject '{subject_input}' not found")
                return
        
        if homework_index < 0 or homework_index >= len(hw[subject]):
            await update.message.reply_text(f"Invalid homework index. {subject} has {len(hw[subject])} homework items")
            return

        # Remove the homework item
        removed_task = hw[subject].pop(homework_index)
        
        # Remove subject if no tasks left
        if not hw[subject]:
            del hw[subject]
        
        save_homework(hw)
        await update.message.reply_text(f"Removed: {subject} - {removed_task['task']}")
    except Exception as e:
        logger.error(f"Error in hw_remove: {e}")
        await update.message.reply_text("Error removing homework")

# ====== REMINDERS ======
async def send_daily_reminder():
    """Send daily class reminders at midnight and morning"""
    try:
        if not app:
            return
        
        now = datetime.datetime.now()
        
        # Midnight reminder (00:00)
        if now.hour == 0 and now.minute == 0:
            await send_midnight_reminder()
        
        # Morning reminder (08:00)
        elif now.hour == 8 and now.minute == 0:
            await send_morning_reminder()
        
        # Evening homework reminder (20:00)
        elif now.hour == 20 and now.minute == 0:
            await send_homework_reminder()
            
    except Exception as e:
        logger.error(f"Error in daily reminder: {e}")

async def send_midnight_reminder():
    """Send today's classes at midnight"""
    try:
        day = datetime.date.today().strftime("%A")
        lessons = TIMETABLE.get(day, [])
        
        if not lessons:
            return
        
        msg = f"Today ({day}) classes:\n\n"
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
        logger.error(f"Error sending midnight reminder: {e}")

async def send_morning_reminder():
    """Send today's classes at 8 AM"""
    try:
        day = datetime.date.today().strftime("%A")
        lessons = TIMETABLE.get(day, [])
        
        if not lessons:
            return
        
        msg = f"Good morning! Today's classes:\n\n"
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

async def send_homework_reminder():
    """Send homework reminders for tomorrow's classes at 8 PM"""
    try:
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        tomorrow_name = tomorrow.strftime("%A")
        lessons = TIMETABLE.get(tomorrow_name, [])
        hw = load_homework()
        
        if not lessons:
            return
        
        # Get tomorrow's subjects
        tomorrow_subjects = []
        for lesson in lessons:
            if lesson["subject"] and is_lesson_this_week(lesson):
                tomorrow_subjects.append(lesson["subject"])
        
        # Check for homework for tomorrow's subjects
        homework_reminders = []
        for subject in set(tomorrow_subjects):  # Remove duplicates
            if subject in hw:
                for task in hw[subject]:
                    try:
                        due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                        # Show homework due tomorrow or overdue
                        if due_date <= tomorrow + datetime.timedelta(days=2):  # Due within 2 days
                            homework_reminders.append((subject, task))
                    except ValueError:
                        logger.error(f"Invalid date in homework reminder: {task}")
        
        if homework_reminders:
            msg = f"Homework reminder for tomorrow's classes ({tomorrow_name}):\n\n"
            
            for subject, task in homework_reminders:
                try:
                    due_date = datetime.datetime.strptime(task["due"], "%Y-%m-%d").date()
                    days_left = (due_date - tomorrow).days
                    
                    if days_left < 0:
                        status = f"OVERDUE ({abs(days_left)} days)"
                    elif days_left == 0:
                        status = "DUE TOMORROW"
                    else:
                        status = f"due {task['due']}"
                    
                    msg += f"{subject}: {task['task']} ({status})\n"
                except ValueError:
                    msg += f"{subject}: {task['task']} (invalid date)\n"
            
            await app.bot.send_message(chat_id=YOUR_GROUP_CHAT_ID, text=msg)
        
    except Exception as e:
        logger.error(f"Error sending homework reminder: {e}")

async def reminder_scheduler():
    """Main scheduler that runs every minute"""
    while not shutdown_event.is_set():
        try:
            await send_daily_reminder()
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            continue  # Normal timeout, continue loop
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
    logger.info(f"Received signal {signum}, shutting down...")
    global reminder_task
    if reminder_task:
        reminder_task.cancel()
    release_lock()
    shutdown_event.set()
    sys.exit(0)

# ====== MAIN ======
async def main():
    global app, reminder_task
    
    # Check if another instance is already running
    if not acquire_lock():
        logger.error("Another instance of the bot is already running. Exiting...")
        print("Error: Another instance of the bot is already running.")
        print("If you're sure no other instance is running, delete the 'bot.lock' file and try again.")
        sys.exit(1)
    
    logger.info("Starting simple study bot...")
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Create the Application
        app = Application.builder().token(TOKEN).build()

        # Add command handlers
        app.add_handler(CommandHandler("hw_add", hw_add))
        app.add_handler(CommandHandler("hw_list", hw_list))
        app.add_handler(CommandHandler("hw_remove", hw_remove))
        app.add_handler(CommandHandler("hw_stats", hw_stats))
        app.add_handler(CommandHandler("hw_clean", hw_clean))
        app.add_handler(CommandHandler("hw_today", hw_today))
        app.add_handler(CommandHandler("schedule", schedule_today))
        app.add_handler(CommandHandler("next", next_class))
        app.add_handler(CommandHandler("kys", kys))
        app.add_handler(CommandHandler("motivate", motivate))

        # Set commands for Telegram menu
        commands = [
            BotCommand("hw_add", "Add homework"),
            BotCommand("hw_list", "List homework"),
            BotCommand("hw_remove", "Remove homework by subject and homework index"),
            BotCommand("hw_stats", "Show homework statistics"),
            BotCommand("hw_clean", "Remove old overdue homework"),
            BotCommand("hw_today", "Show homework due today"),
            BotCommand("schedule", "Show today's schedule"),
            BotCommand("next", "Show next upcoming classes"),
            BotCommand("kys", "shh"),
            BotCommand("motivate", "the motivation u always needed"),
        ]
        
        await app.bot.set_my_commands(commands)

        # Initialize the bot
        await app.initialize()

        # Start reminder scheduler as a background task
        reminder_task = asyncio.create_task(reminder_scheduler())
        
        logger.info("Simple study bot started successfully")

        # Run the bot with proper polling
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        
        # Keep the bot running until shutdown
        await shutdown_event.wait()
        
    except Exception as e:
        logger.error(f"Error running bot: {e}")
        raise
    finally:
        logger.info("Shutting down bot...")
        if reminder_task:
            reminder_task.cancel()
            try:
                await reminder_task
            except asyncio.CancelledError:
                pass
        if app:
            await app.stop()
            await app.shutdown()
        release_lock()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        release_lock()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        release_lock()
        sys.exit(1)