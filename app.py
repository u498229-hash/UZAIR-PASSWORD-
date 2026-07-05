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
import time

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Bot Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Global variables
user_queues = {}
processing_lock = asyncio.Lock()
MAX_CONCURRENT_JOBS = 5
current_jobs = 0
user_wordlists = {}

# Thread pool
thread_pool = ThreadPoolExecutor(max_workers=10)

# Database
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
        cursor.execute('INSERT OR IGNORE INTO bot_stats (id, total_requests, active_users) VALUES (1, 0, 0)')
        conn.commit()
        conn.close()
        logger.info("✅ Database initialized successfully")
    except Exception as e:
        logger.error(f"❌ Database error: {e}")

init_db()

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

def rate_limit(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        current_time = time.time()
        if user_id not in user_queues:
            user_queues[user_id] = []
        user_queues[user_id] = [t for t in user_queues[user_id] if current_time - t < 60]
        if len(user_queues[user_id]) >= 10:
            await update.message.reply_text("🚫 Rate limit exceeded. Please wait 1 minute.")
            return
        user_queues[user_id].append(current_time)
        return await func(update, context)
    return wrapper

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
                log_attempt(user_id, "DOCX", True)
                return f"🎉 PASSWORD FOUND: `{password}`"
            except:
                continue
        log_attempt(user_id, "DOCX", False)
        return "❌ Password not found in wordlist"
    except Exception as e:
        log_attempt(user_id, "DOCX", False)
        return f"❌ Error: {str(e)}"

async def run_cracking_async(file_path, wordlist_path, user_id, file_type):
    global current_jobs
    
    async with processing_lock:
        if current_jobs >= MAX_CONCURRENT_JOBS:
            return "🚫 Server busy. Please try again in a few moments."
        current_jobs += 1
    
    try:
        # ✅ FIX: Use asyncio.to_thread for Python 3.9+
        if file_type == 'ZIP':
            result = await asyncio.to_thread(crack_zip_thread, file_path, wordlist_path, user_id)
        elif file_type == 'RAR':
            result = await asyncio.to_thread(crack_rar_thread, file_path, wordlist_path, user_id)
        elif file_type == 'DOCX':
            result = await asyncio.to_thread(crack_docx_thread, file_path, wordlist_path, user_id)
        else:
            result = "⚠️ File type supported but cracking not implemented"
        
        return result
    except Exception as e:
        return f"❌ Processing error: {str(e)}"
    finally:
        async with processing_lock:
            current_jobs -= 1

@rate_limit
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.first_name, user.last_name)
    
    welcome_text = """
🔓 *WELCOME TO PASSWORD CRACKER BOT* 

📁 *SUPPORTED FILES:*
• ZIP • RAR • DOCX

📝 *CUSTOM WORDLIST:*
• Upload your own .txt password list

⚡ *POWERED BY UZAIR*
    """
    
    keyboard = [
        [InlineKeyboardButton("📤 UPLOAD FILE", callback_data="upload")],
        [InlineKeyboardButton("📝 UPLOAD WORDLIST", callback_data="wordlist")],
        [InlineKeyboardButton("📊 STATS", callback_data="status")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

@rate_limit
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🆘 *HELP*

📁 *SUPPORTED:*
• ZIP, RAR, DOCX

📝 *CUSTOM WORDLIST:*
• Send .txt file with passwords
• Each password on new line

⚡ *POWERED BY UZAIR*
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

@rate_limit
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users, total_cracked, total_attempts, total_failed, total_requests = get_stats()
    
    status_text = f"""
📊 *STATISTICS*

👥 Users: `{total_users}`
✅ Cracked: `{total_cracked}`
🔍 Attempts: `{total_attempts}`
❌ Failed: `{total_failed}`
⚡ Active: `{current_jobs}/{MAX_CONCURRENT_JOBS}`

⚡ *POWERED BY UZAIR*
    """
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def handle_wordlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    document = update.message.document
    
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please upload a .txt file!")
        return
    
    if document.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("❌ File too large! Max 5MB.")
        return
    
    try:
        file = await context.bot.get_file(document.file_id)
        wordlist_path = f"wordlists/{user.id}_{int(time.time())}.txt"
        os.makedirs("wordlists", exist_ok=True)
        await file.download_to_drive(wordlist_path)
        
        user_wordlists[user.id] = wordlist_path
        
        with open(wordlist_path, 'r', errors='ignore') as f:
            count = sum(1 for line in f if line.strip())
        
        await update.message.reply_text(
            f"✅ *Wordlist Uploaded!*\n\n"
            f"📁 File: `{document.file_name}`\n"
            f"🔢 Passwords: `{count}`\n\n"
            f"Now upload your protected file!",
            parse_mode='Markdown'
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    # Check if it's a wordlist (.txt)
    if update.message.document.file_name.endswith('.txt'):
        await handle_wordlist(update, context)
        return
    
    # Rate limiting
    current_time = time.time()
    if user_id not in user_queues:
        user_queues[user_id] = []
    user_queues[user_id] = [t for t in user_queues[user_id] if current_time - t < 60]
    if len(user_queues[user_id]) >= 5:
        await update.message.reply_text("🚫 Too many requests. Wait 1 minute.")
        return
    user_queues[user_id].append(current_time)
    
    document = update.message.document
    supported_types = {'.zip': 'ZIP', '.rar': 'RAR', '.docx': 'DOCX'}
    file_extension = os.path.splitext(document.file_name)[1].lower()
    
    if file_extension not in supported_types:
        await update.message.reply_text("❌ Unsupported! Use ZIP, RAR, DOCX")
        return
    
    try:
        file = await context.bot.get_file(document.file_id)
        file_path = f"downloads/{user.id}_{int(time.time())}_{document.file_name}"
        os.makedirs("downloads", exist_ok=True)
        await file.download_to_drive(file_path)
    except Exception as e:
        await update.message.reply_text("❌ Error downloading file")
        return
    
    # Check custom wordlist
    if user_id in user_wordlists and os.path.exists(user_wordlists[user_id]):
        wordlist_path = user_wordlists[user_id]
        wordlist_msg = "📝 Using your custom wordlist ✅"
    else:
        wordlist_path = "password.txt"
        wordlist_msg = "📝 Using default wordlist"
    
    if not os.path.exists(wordlist_path):
        await update.message.reply_text("❌ No wordlist found! Upload a .txt wordlist.")
        try:
            os.remove(file_path)
        except:
            pass
        return
    
    processing_msg = await update.message.reply_text(
        f"🔍 *Processing...*\n"
        f"📁 File: `{document.file_name}`\n"
        f"{wordlist_msg}\n"
        f"⏳ Please wait...",
        parse_mode='Markdown'
    )
    
    file_type = supported_types[file_extension]
    result = await run_cracking_async(file_path, wordlist_path, user.id, file_type)
    
    result_text = f"""
📁 *File:* `{document.file_name}`
📊 *Type:* {file_type}
{wordlist_msg}

{result}

⚡ *POWERED BY UZAIR*
    """
    
    await processing_msg.edit_text(result_text, parse_mode='Markdown')
    
    try:
        os.remove(file_path)
    except:
        pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "upload":
        await query.message.reply_text(
            "📤 *Upload your protected file!*\n\n"
            "Supported: ZIP, RAR, DOCX",
            parse_mode='Markdown'
        )
    elif query.data == "wordlist":
        await query.message.reply_text(
            "📝 *Upload .txt wordlist!*\n\n"
            "Each password on new line\n"
            "Max: 5MB",
            parse_mode='Markdown'
        )
    elif query.data == "status":
        total_users, total_cracked, total_attempts, total_failed, total_requests = get_stats()
        status_text = f"""
📊 *STATS*

👥 Users: `{total_users}`
✅ Cracked: `{total_cracked}`
⚡ Active: `{current_jobs}/{MAX_CONCURRENT_JOBS}`
        """
        await query.message.reply_text(status_text, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and not update.message.text.startswith('/'):
        await update.message.reply_text(
            "🤖 Send a file or use buttons!",
            parse_mode='Markdown'
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Error occurred. Please try again."
        )
    except:
        pass

# ✅ FIX: Main function with proper async handling
def main():
    try:
        print("🤖 Starting Bot...")
        
        os.makedirs("downloads", exist_ok=True)
        os.makedirs("wordlists", exist_ok=True)
        
        if not os.path.exists("password.txt"):
            with open("password.txt", "w") as f:
                f.write("password\n123456\nadmin\n1234\n12345\n")
            print("✅ Created password.txt")
        
        # ✅ FIX: Use asyncio.run() properly
        async def run_bot():
            application = Application.builder().token(BOT_TOKEN).build()
            
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(CommandHandler("status", status_command))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
            application.add_handler(CallbackQueryHandler(button_handler))
            application.add_error_handler(error_handler)
            
            print("✅ Bot started successfully!")
            print("⚡ Powered by UZAIR")
            
            # ✅ FIX: Initialize and start properly
            await application.initialize()
            await application.start()
            await application.updater.start_polling()
            
            # Keep running
            while True:
                await asyncio.sleep(1)
        
        asyncio.run(run_bot())
        
    except Exception as e:
        print(f"❌ Error: {e}")
        print("🔧 Check configuration")

if __name__ == "__main__":
    main()