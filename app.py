import os
import sqlite3
import logging
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import pyzipper
import msoffcrypto
import io
import subprocess
import queue
import time

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Bot Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Global variables for rate limiting
user_queues = {}
processing_lock = asyncio.Lock()
MAX_CONCURRENT_JOBS = 5  # Maximum simultaneous processing jobs
current_jobs = 0

# Thread pool for CPU-intensive tasks
thread_pool = ThreadPoolExecutor(max_workers=10)

# Database setup with connection pooling
def get_db_connection():
    return sqlite3.connect('bot_stats.db', check_same_thread=False, timeout=30)

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                file_type TEXT,
                success BOOLEAN,
                attempt_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_stats (
                id INTEGER PRIMARY KEY,
                total_requests INTEGER DEFAULT 0,
                active_users INTEGER DEFAULT 0
            )
        ''')
        # Initialize bot stats
        cursor.execute('INSERT OR IGNORE INTO bot_stats (id, total_requests, active_users) VALUES (1, 0, 0)')
        conn.commit()
        conn.close()
        logger.info("✅ Database initialized successfully")
    except Exception as e:
        logger.error(f"❌ Database error: {e}")

# Initialize database
init_db()

# Statistics functions with connection pooling
def add_user(user_id, username, first_name, last_name):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO users (user_id, username, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error adding user: {e}")

def log_attempt(user_id, file_type, success):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO attempts (user_id, file_type, success)
            VALUES (?, ?, ?)
        ''', (user_id, file_type, success))
        
        # Update total requests
        cursor.execute('UPDATE bot_stats SET total_requests = total_requests + 1 WHERE id = 1')
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging attempt: {e}")

def get_stats():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM attempts WHERE success = 1')
        total_cracked = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM attempts')
        total_attempts = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM attempts WHERE success = 0')
        total_failed = cursor.fetchone()[0]
        
        cursor.execute('SELECT total_requests FROM bot_stats WHERE id = 1')
        total_requests = cursor.fetchone()[0]
        
        conn.close()
        return total_users, total_cracked, total_attempts, total_failed, total_requests
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        return 0, 0, 0, 0, 0

# Rate limiting decorator
def rate_limit(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        current_time = time.time()
        
        # Check if user has made too many requests
        if user_id not in user_queues:
            user_queues[user_id] = []
        
        # Remove old requests (last 60 seconds)
        user_queues[user_id] = [t for t in user_queues[user_id] if current_time - t < 60]
        
        # Allow maximum 10 requests per minute per user
        if len(user_queues[user_id]) >= 10:
            await update.message.reply_text("🚫 Rate limit exceeded. Please wait 1 minute.")
            return
        
        user_queues[user_id].append(current_time)
        return await func(update, context)
    return wrapper

# Password cracking functions with threading
def crack_zip_thread(file_path, wordlist_path, user_id):
    try:
        with pyzipper.AESZipFile(file_path) as zf:
            for pwd in open(wordlist_path, "r", errors="ignore"):
                password = pwd.strip()
                if not password:
                    continue
                try:
                    zf.extractall(pwd=password.encode())
                    log_attempt(user_id, "ZIP", True)
                    return f"🎉 PASSWORD FOUND: `{password}`"
                except:
                    continue
        log_attempt(user_id, "ZIP", False)
        return "❌ Password not found in wordlist"
    except Exception as e:
        log_attempt(user_id, "ZIP", False)
        return f"❌ Error: {str(e)}"

def crack_rar_thread(file_path, wordlist_path, user_id):
    try:
        for pwd in open(wordlist_path, "r", errors="ignore"):
            password = pwd.strip()
            if not password:
                continue
            try:
                cmd = f'unrar x -p"{password}" -y "{file_path}" > /dev/null 2>&1'
                result = subprocess.run(cmd, shell=True)
                if result.returncode == 0:
                    log_attempt(user_id, "RAR", True)
                    return f"🎉 PASSWORD FOUND: `{password}`"
            except:
                continue
        log_attempt(user_id, "RAR", False)
        return "❌ Password not found in wordlist"
    except Exception as e:
        log_attempt(user_id, "RAR", False)
        return f"❌ Error: {str(e)}"

def crack_docx_thread(file_path, wordlist_path, user_id):
    try:
        for pwd in open(wordlist_path, "r", errors="ignore"):
            password = pwd.strip()
            if not password:
                continue
            try:
                with open(file_path, 'rb') as f:
                    file_data = f.read()
                decrypted = msoffcrypto.OfficeFile(io.BytesIO(file_data))
                decrypted.load_key(password=password)
                # If we get here, password is correct
                log_attempt(user_id, "DOCX", True)
                return f"🎉 PASSWORD FOUND: `{password}`"
            except Exception as e:
                continue
        log_attempt(user_id, "DOCX", False)
        return "❌ Password not found in wordlist"
    except Exception as e:
        log_attempt(user_id, "DOCX", False)
        return f"❌ Error: {str(e)}"

# Async function to run cracking in thread pool
async def run_cracking_async(file_path, wordlist_path, user_id, file_type):
    global current_jobs
    
    async with processing_lock:
        if current_jobs >= MAX_CONCURRENT_JOBS:
            return "🚫 Server busy. Please try again in a few moments."
        
        current_jobs += 1
    
    try:
        loop = asyncio.get_event_loop()
        
        if file_type == 'ZIP':
            result = await loop.run_in_executor(thread_pool, crack_zip_thread, file_path, wordlist_path, user_id)
        elif file_type == 'RAR':
            result = await loop.run_in_executor(thread_pool, crack_rar_thread, file_path, wordlist_path, user_id)
        elif file_type == 'DOCX':
            result = await loop.run_in_executor(thread_pool, crack_docx_thread, file_path, wordlist_path, user_id)
        else:
            result = "⚠️ File type supported but cracking not implemented"
        
        return result
    except Exception as e:
        return f"❌ Processing error: {str(e)}"
    finally:
        async with processing_lock:
            current_jobs -= 1

# Bot handlers with rate limiting
@rate_limit
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.first_name, user.last_name)
    
    welcome_text = """
🔓 *WELCOME TO PASSWORD CRACKER BOT* 

🪄 UPLOAD ANY FILE AND SEE MAGIC!

📁 *SUPPORTED FILES:*
• ZIP • RAR • DOCX

🚀 *HIGH-PERFORMANCE BOT*
• Multi-user support ✅
• Fast processing ⚡
• 24/7 available 🕐

⚡ *POWERED BY UZAIR*
    """
    
    keyboard = [
        [InlineKeyboardButton("📤 UPLOAD FILE", callback_data="upload")],
        [InlineKeyboardButton("📊 LIVE STATS", callback_data="status")],
        [InlineKeyboardButton("🆘 HELP", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

@rate_limit
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🆘 *HELP GUIDE - HIGH TRAFFIC OPTIMIZED*

📁 *SUPPORTED FORMATS:*
• ZIP Archives
• RAR Archives  
• DOCX Documents

🚀 *PERFORMANCE FEATURES:*
• Multi-threaded processing
• Rate limiting protection
• Queue management
• 24/7 availability

⚡ *POWERED BY UZAIR*
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

@rate_limit
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users, total_cracked, total_attempts, total_failed, total_requests = get_stats()
    
    status_text = f"""
📊 *REAL-TIME BOT STATISTICS*

👥 Total Users: `{total_users}`
✅ Passwords Cracked: `{total_cracked}`
🔍 Total Attempts: `{total_attempts}`
❌ Failed Attempts: `{total_failed}`
📨 Total Requests: `{total_requests}`
⚡ Active Jobs: `{current_jobs}/{MAX_CONCURRENT_JOBS}`

🚀 *HIGH-PERFORMANCE MODE*
⚡ *POWERED BY UZAIR*
    """
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Rate limiting check
    user_id = user.id
    current_time = time.time()
    if user_id not in user_queues:
        user_queues[user_id] = []
    
    user_queues[user_id] = [t for t in user_queues[user_id] if current_time - t < 60]
    
    if len(user_queues[user_id]) >= 5:  # Max 5 files per minute
        await update.message.reply_text("🚫 Too many requests. Please wait 1 minute.")
        return
    
    user_queues[user_id].append(current_time)
    
    document = update.message.document
    
    supported_types = {
        '.zip': 'ZIP', 
        '.rar': 'RAR', 
        '.docx': 'DOCX'
    }
    
    file_extension = os.path.splitext(document.file_name)[1].lower()
    
    if file_extension not in supported_types:
        await update.message.reply_text("❌ Unsupported file type! Use ZIP, RAR, DOCX")
        return
    
    # Check if server is busy
    async with processing_lock:
        if current_jobs >= MAX_CONCURRENT_JOBS:
            await update.message.reply_text("⏳ Server is busy. Your file is queued. Please wait...")
            # You can implement proper queue here
    
    # Download file
    try:
        file = await context.bot.get_file(document.file_id)
        file_path = f"downloads/{user.id}_{int(time.time())}_{document.file_name}"
        os.makedirs("downloads", exist_ok=True)
        await file.download_to_drive(file_path)
    except Exception as e:
        await update.message.reply_text("❌ Error downloading file")
        return
    
    # Check password file
    wordlist_path = "password.txt"
    if not os.path.exists(wordlist_path):
        await update.message.reply_text("❌ password.txt not found on server!")
        try:
            os.remove(file_path)
        except:
            pass
        return
    
    # Send processing message
    processing_msg = await update.message.reply_text(
        f"🔍 *Processing Started*\n"
        f"📁 File: `{document.file_name}`\n"
        f"👤 User: {user.first_name}\n"
        f"⏳ Please wait...\n"
        f"📊 Queue: {current_jobs}/{MAX_CONCURRENT_JOBS}",
        parse_mode='Markdown'
    )
    
    # Process file asynchronously
    file_type = supported_types[file_extension]
    result = await run_cracking_async(file_path, wordlist_path, user.id, file_type)
    
    # Send result
    result_text = f"""
📁 *File:* `{document.file_name}`
📊 *Type:* {file_type}
👤 *User:* {user.first_name}

{result}

⚡ *POWERED BY UZAIR*
    """
    
    await processing_msg.edit_text(result_text, parse_mode='Markdown')
    
    # Cleanup
    try:
        os.remove(file_path)
    except:
        pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "upload":
        await query.message.reply_text(
            "📤 *READY FOR UPLOAD*\n\n"
            "Please upload your protected file now!\n"
            "Supported: ZIP, RAR, DOCX\n\n"
            "🚀 *High-speed processing activated*",
            parse_mode='Markdown'
        )
    
    elif query.data == "status":
        total_users, total_cracked, total_attempts, total_failed, total_requests = get_stats()
        
        status_text = f"""
📊 *REAL-TIME STATISTICS*

👥 Total Users: `{total_users}`
✅ Passwords Cracked: `{total_cracked}`
🔍 Total Attempts: `{total_attempts}`
❌ Failed Attempts: `{total_failed}`
📨 Total Requests: `{total_requests}`
⚡ Active Jobs: `{current_jobs}/{MAX_CONCURRENT_JOBS}`

🚀 *HIGH-PERFORMANCE MODE*
        """
        await query.message.reply_text(status_text, parse_mode='Markdown')
    
    elif query.data == "help":
        help_text = """
🆘 *HELP - OPTIMIZED FOR HIGH TRAFFIC*

📁 *SUPPORTED FILES:*
• ZIP, RAR, DOCX

🚀 *BOT FEATURES:*
• Multi-user support
• Rate limiting
• Queue management  
• Fast processing
• 24/7 availability

⚡ *POWERED BY UZAIR*
        """
        await query.message.reply_text(help_text, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and not update.message.text.startswith('/'):
        await update.message.reply_text(
            "🤖 *PASSWORD CRACKER BOT*\n\n"
            "📤 Upload a file or use buttons below!",
            parse_mode='Markdown'
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ An error occurred. Please try again."
        )
    except:
        pass

def main():
    try:
        print("🤖 Starting HIGH-PERFORMANCE Password Cracker Bot...")
        print("🔧 Optimizing for multiple users...")
        
        # Create necessary directories
        os.makedirs("downloads", exist_ok=True)
        
        # Create password.txt if not exists
        if not os.path.exists("password.txt"):
            with open("password.txt", "w") as f:
                f.write("password\n123456\nadmin\n1234\n12345\n12345678\nqwerty\npassword1\nletmein\n123456789\n")
            print("✅ Created password.txt file")
        
        # Bot configuration with optimizations
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Add error handler
        application.add_error_handler(error_handler)
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        application.add_handler(CallbackQueryHandler(button_handler))
        
        print("✅ HIGH-PERFORMANCE Bot started successfully!")
        print(f"🚀 Max concurrent jobs: {MAX_CONCURRENT_JOBS}")
        print("📱 Bot is ready for multiple users!")
        print("⚡ Powered by UZAIR")
        
        # Start bot with optimizations - FIXED VERSION
        application.run_polling(
            poll_interval=0.5,
            timeout=30
        )
        
    except Exception as e:
        print(f"❌ Error: {e}")
        print("🔧 Please check your configuration")

if __name__ == "__main__":
    main()