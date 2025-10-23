#!/usr/bin/env python3
"""
Reddit to Telegram Monitor Bot - Simplified Version
Monitors specific subreddits for keywords
"""

import os
import json
import time
import logging
import asyncio
import re
from typing import Set, Optional
from datetime import datetime

import asyncpraw
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class RedditTelegramBot:
    def __init__(self):
        # Config
        self.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = int(os.getenv('TELEGRAM_CHAT_ID'))
        
        self.reddit_client_id = os.getenv('REDDIT_CLIENT_ID')
        self.reddit_client_secret = os.getenv('REDDIT_CLIENT_SECRET')
        self.reddit_user_agent = os.getenv('REDDIT_USER_AGENT', 'TelegramBot:v1.0')
        self.reddit_username = os.getenv('REDDIT_USERNAME', '')
        self.reddit_password = os.getenv('REDDIT_PASSWORD', '')
        
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '300'))
        self.search_limit = int(os.getenv('SEARCH_LIMIT', '50'))
        
        # Simple data storage
        self.keywords: Set[str] = set()
        self.subreddits: Set[str] = set(['all'])  # Default to r/all
        self.processed_items: Set[str] = set()
        self.enabled = True
        
        # State for menu navigation
        self.menu_state = None  # 'adding_keywords', 'removing_keywords', 'adding_subs', 'removing_subs'
        
        self.data_file = 'bot_data.json'
        self.reddit: Optional[asyncpraw.Reddit] = None
        
        # Monitoring control
        self.is_monitoring = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.stream_task: Optional[asyncio.Task] = None
        
        self.load_data()

    def load_data(self):
        """Load keywords, subreddits and processed items"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r') as f:
                    data = json.load(f)
                    self.keywords = set(data.get('keywords', []))
                    self.subreddits = set(data.get('subreddits', ['all']))
                    self.processed_items = set(data.get('processed_items', []))
                    self.enabled = data.get('enabled', True)
                    logger.info(f"Loaded {len(self.keywords)} keywords, {len(self.subreddits)} subreddits")
            else:
                logger.info("No data file found, starting fresh")
        except Exception as e:
            logger.error(f"Error loading data: {e}")

    def save_data(self):
        """Save data to file"""
        try:
            # Keep only recent 5000 processed items
            if len(self.processed_items) > 10000:
                self.processed_items = set(list(self.processed_items)[-5000:])
            
            data = {
                'keywords': list(self.keywords),
                'subreddits': list(self.subreddits),
                'processed_items': list(self.processed_items),
                'enabled': self.enabled
            }
            
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving data: {e}")

    async def setup_reddit(self):
        """Initialize Reddit client"""
        try:
            if self.reddit:
                await self.reddit.close()
            
            if self.reddit_username and self.reddit_password:
                self.reddit = asyncpraw.Reddit(
                    client_id=self.reddit_client_id,
                    client_secret=self.reddit_client_secret,
                    user_agent=self.reddit_user_agent,
                    username=self.reddit_username,
                    password=self.reddit_password
                )
            else:
                self.reddit = asyncpraw.Reddit(
                    client_id=self.reddit_client_id,
                    client_secret=self.reddit_client_secret,
                    user_agent=self.reddit_user_agent
                )
            
            logger.info("Reddit client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Reddit: {e}")
            raise

    def contains_keyword(self, text: str, keyword: str) -> bool:
        """Check if text contains keyword (case-insensitive word boundary match)"""
        if not text or not keyword:
            return False
        pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
        return bool(re.search(pattern, text.lower()))

    def format_notification(self, item, keyword: str, item_type: str) -> str:
        """Format notification message"""
        try:
            if item_type == "post":
                title = item.title[:200] + "..." if len(item.title) > 200 else item.title
                content = ""
                if hasattr(item, 'selftext') and item.selftext:
                    content = item.selftext[:300] + "..." if len(item.selftext) > 300 else item.selftext
                
                msg = f" Keyword: {keyword}\n\n"
                msg += f" {title}\n"
                msg += f" u/{item.author} |  r/{item.subreddit}\n"
                if content:
                    msg += f"\n{content}\n"
                msg += f"\n https://reddit.com{item.permalink}"
            else:  # comment
                content = ""
                if hasattr(item, 'body') and item.body:
                    content = item.body[:300] + "..." if len(item.body) > 300 else item.body
                
                msg = f" Keyword: {keyword}\n\n"
                msg += f" Comment by u/{item.author}\n"
                msg += f" r/{item.subreddit}\n"
                if content:
                    msg += f"\n{content}\n"
                msg += f"\n https://reddit.com{item.permalink}"
            
            return msg
        except Exception as e:
            logger.error(f"Error formatting notification: {e}")
            return f" Keyword: {keyword}\n\nError formatting notification"

    async def send_message(self, text: str):
        """Send message to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    'chat_id': self.telegram_chat_id,
                    'text': text,
                    'disable_web_page_preview': True
                }) as response:
                    if response.status != 200:
                        logger.error(f"Failed to send message: {await response.text()}")
        except Exception as e:
            logger.error(f"Error sending message: {e}")

    async def search_posts(self, keyword: str):
        """Search posts in monitored subreddits"""
        try:
            found_count = 0
            
            for sub_name in self.subreddits:
                if not self.is_monitoring:
                    break
                
                try:
                    subreddit = await self.reddit.subreddit(sub_name)
                    
                    async for post in subreddit.search(keyword, sort='new', time_filter='day', limit=self.search_limit):
                        if not self.is_monitoring:
                            break
                        
                        if post.id in self.processed_items:
                            continue
                        
                        title_match = self.contains_keyword(post.title, keyword)
                        body_match = False
                        if hasattr(post, 'selftext') and post.selftext:
                            body_match = self.contains_keyword(post.selftext, keyword)
                        
                        if title_match or body_match:
                            message = self.format_notification(post, keyword, "post")
                            await self.send_message(message)
                            self.processed_items.add(post.id)
                            found_count += 1
                            await asyncio.sleep(2)  # Rate limit
                        
                        await asyncio.sleep(0.1)
                
                except Exception as e:
                    logger.error(f"Error searching r/{sub_name}: {e}")
                    continue
            
            if found_count > 0:
                logger.info(f"Found {found_count} posts for '{keyword}'")
        
        except Exception as e:
            logger.error(f"Error searching posts for '{keyword}': {e}")

    async def search_comments(self, keyword: str):
        """Search comments in monitored subreddits"""
        try:
            found_count = 0
            
            for sub_name in self.subreddits:
                if not self.is_monitoring:
                    break
                
                try:
                    subreddit = await self.reddit.subreddit(sub_name)
                    
                    async for comment in subreddit.comments(limit=self.search_limit):
                        if not self.is_monitoring:
                            break
                        
                        if comment.id in self.processed_items:
                            continue
                        
                        if hasattr(comment, 'body') and self.contains_keyword(comment.body, keyword):
                            message = self.format_notification(comment, keyword, "comment")
                            await self.send_message(message)
                            self.processed_items.add(comment.id)
                            found_count += 1
                            await asyncio.sleep(2)
                        
                        await asyncio.sleep(0.1)
                
                except Exception as e:
                    logger.error(f"Error searching comments in r/{sub_name}: {e}")
                    continue
            
            if found_count > 0:
                logger.info(f"Found {found_count} comments for '{keyword}'")
        
        except Exception as e:
            logger.error(f"Error searching comments for '{keyword}': {e}")

    async def stream_comments(self):
        """Stream real-time comments"""
        logger.info("Starting comment stream...")
        
        while self.is_monitoring:
            try:
                if not self.reddit:
                    await self.setup_reddit()
                
                # Stream from all monitored subreddits
                sub_string = '+'.join(self.subreddits)
                subreddit = await self.reddit.subreddit(sub_string)
                
                async for comment in subreddit.stream.comments(skip_existing=True):
                    if not self.is_monitoring:
                        break
                    
                    if comment.id in self.processed_items:
                        continue
                    
                    for keyword in self.keywords:
                        if hasattr(comment, 'body') and self.contains_keyword(comment.body, keyword):
                            message = self.format_notification(comment, keyword, "comment")
                            await self.send_message(message)
                            self.processed_items.add(comment.id)
                            logger.info(f"Stream found comment for '{keyword}'")
                            break
            
            except Exception as e:
                logger.error(f"Error in comment stream: {e}")
                await asyncio.sleep(30)

    async def monitor_loop(self):
        """Main monitoring loop"""
        while self.is_monitoring:
            try:
                if not self.enabled or not self.keywords:
                    logger.info("Monitoring paused (disabled or no keywords)")
                    await asyncio.sleep(60)
                    continue
                
                logger.info(f"Monitoring {len(self.keywords)} keywords in {len(self.subreddits)} subreddits")
                
                for keyword in list(self.keywords):
                    if not self.is_monitoring:
                        logger.info("Monitoring stopped by user")
                        break
                    
                    await self.search_posts(keyword)
                    if not self.is_monitoring:
                        break
                    
                    await self.search_comments(keyword)
                    if not self.is_monitoring:
                        break
                
                if self.is_monitoring:
                    self.save_data()
                    logger.info(f"Cycle complete. Sleeping {self.check_interval}s...")
                    await asyncio.sleep(self.check_interval)
                
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")
                await asyncio.sleep(60)

    # ============= COMMAND HANDLERS =============
    
    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command"""
        help_text = """
 Keyword Monitor Bot

Commands:
/start - Show this help
/menu - Open interactive menu
/status - Show current status
/toggle - Enable/disable monitoring

/addkw <keyword> - Add keyword
/removekw <keyword> - Remove keyword
/listkw - List keywords
/clearkw - Clear all keywords

/addsub <subreddit> - Add subreddit (without r/)
/removesub <subreddit> - Remove subreddit
/listsub - List subreddits
/clearsub - Clear subreddits (resets to r/all)

/startmon - Start monitoring
/stopmon - Stop monitoring

Examples:
/addkw pain killer
/addsub wallstreetbets
        """
        await update.message.reply_text(help_text.strip())

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show interactive menu"""
        keyboard = [
            [InlineKeyboardButton(" Add Keywords", callback_data="add_kw"),
             InlineKeyboardButton(" Remove Keywords", callback_data="remove_kw")],
            [InlineKeyboardButton(" List Keywords", callback_data="list_kw"),
             InlineKeyboardButton(" Clear Keywords", callback_data="clear_kw")],
            [InlineKeyboardButton(" Add Subreddits", callback_data="add_sub"),
             InlineKeyboardButton(" Remove Subreddits", callback_data="remove_sub")],
            [InlineKeyboardButton(" List Subreddits", callback_data="list_sub"),
             InlineKeyboardButton(" Reset Subreddits", callback_data="clear_sub")],
            [InlineKeyboardButton(" Status", callback_data="status"),
             InlineKeyboardButton(f" {'Disable' if self.enabled else 'Enable'}", callback_data="toggle")],
            [InlineKeyboardButton(f"{' Stop' if self.is_monitoring else ' Start'} Monitoring", callback_data="toggle_mon")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(" Bot Menu:", reply_markup=reply_markup)

    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == "add_kw":
            self.menu_state = "adding_keywords"
            await query.edit_message_text(
                f"Current keywords: {', '.join(sorted(self.keywords)) if self.keywords else 'None'}\n\n"
                f"Send keywords separated by commas:\nExample: pain killer, crypto news"
            )
        
        elif data == "remove_kw":
            if not self.keywords:
                await query.edit_message_text("No keywords to remove")
                return
            self.menu_state = "removing_keywords"
            await query.edit_message_text(
                f"Current keywords:\n{chr(10).join(sorted(self.keywords))}\n\n"
                f"Send keywords to remove (comma-separated):"
            )
        
        elif data == "list_kw":
            kw_list = "\n".join(sorted(self.keywords)) if self.keywords else "None"
            await query.edit_message_text(f"Keywords ({len(self.keywords)}):\n{kw_list}")
        
        elif data == "clear_kw":
            count = len(self.keywords)
            self.keywords.clear()
            self.save_data()
            await query.edit_message_text(f"Cleared {count} keywords")
        
        elif data == "add_sub":
            self.menu_state = "adding_subs"
            await query.edit_message_text(
                f"Current subreddits: {', '.join(sorted(self.subreddits))}\n\n"
                f"Send subreddit names (without r/) separated by commas:\n"
                f"Example: wallstreetbets, technology"
            )
        
        elif data == "remove_sub":
            if not self.subreddits:
                await query.edit_message_text("No subreddits configured")
                return
            self.menu_state = "removing_subs"
            await query.edit_message_text(
                f"Current subreddits:\n{chr(10).join(sorted(self.subreddits))}\n\n"
                f"Send subreddit names to remove (comma-separated):"
            )
        
        elif data == "list_sub":
            sub_list = "\n".join(sorted(self.subreddits))
            await query.edit_message_text(f"Subreddits ({len(self.subreddits)}):\n{sub_list}")
        
        elif data == "clear_sub":
            self.subreddits = {'all'}
            self.save_data()
            await query.edit_message_text("Reset to r/all")
        
        elif data == "status":
            status = f"Status: {'Enabled' if self.enabled else 'Disabled'}\n"
            status += f"Monitoring: {'Active' if self.is_monitoring else 'Stopped'}\n"
            status += f"Keywords: {len(self.keywords)}\n"
            status += f"Subreddits: {len(self.subreddits)}\n"
            status += f"Processed items: {len(self.processed_items)}"
            await query.edit_message_text(status)
        
        elif data == "toggle":
            self.enabled = not self.enabled
            self.save_data()
            await query.edit_message_text(f"Bot {'enabled' if self.enabled else 'disabled'}")
        
        elif data == "toggle_mon":
            if self.is_monitoring:
                await self.stop_monitoring()
                await query.edit_message_text(" Monitoring stopped")
            else:
                await self.start_monitoring()
                await query.edit_message_text(" Monitoring started")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text input for adding/removing items"""
        if not self.menu_state:
            return
        
        text = update.message.text.strip()
        items = [item.strip().lower() for item in text.split(',') if item.strip()]
        
        if not items:
            await update.message.reply_text("No valid items found")
            return
        
        if self.menu_state == "adding_keywords":
            added = [kw for kw in items if kw not in self.keywords]
            self.keywords.update(added)
            self.save_data()
            await update.message.reply_text(f"Added {len(added)} keywords:\n{', '.join(added)}")
        
        elif self.menu_state == "removing_keywords":
            removed = [kw for kw in items if kw in self.keywords]
            for kw in removed:
                self.keywords.remove(kw)
            self.save_data()
            await update.message.reply_text(f"Removed {len(removed)} keywords:\n{', '.join(removed)}")
        
        elif self.menu_state == "adding_subs":
            added = [sub for sub in items if sub not in self.subreddits]
            self.subreddits.update(added)
            if 'all' in self.subreddits and len(self.subreddits) > 1:
                self.subreddits.remove('all')
            self.save_data()
            await update.message.reply_text(f"Added {len(added)} subreddits:\n{', '.join(added)}")
        
        elif self.menu_state == "removing_subs":
            removed = [sub for sub in items if sub in self.subreddits]
            for sub in removed:
                self.subreddits.remove(sub)
            if not self.subreddits:
                self.subreddits = {'all'}
            self.save_data()
            await update.message.reply_text(f"Removed {len(removed)} subreddits:\n{', '.join(removed)}")
        
        self.menu_state = None

    async def start_monitoring(self):
        """Start monitoring tasks"""
        if self.is_monitoring:
            return
        
        self.is_monitoring = True
        await self.setup_reddit()
        
        self.monitor_task = asyncio.create_task(self.monitor_loop())
        self.stream_task = asyncio.create_task(self.stream_comments())
        logger.info("Monitoring started")

    async def stop_monitoring(self):
        """Stop monitoring immediately"""
        self.is_monitoring = False
        
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        
        if self.stream_task:
            self.stream_task.cancel()
            try:
                await self.stream_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Monitoring stopped")

    async def startmon_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start monitoring"""
        await self.start_monitoring()
        await update.message.reply_text(" Monitoring started")

    async def stopmon_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stop monitoring"""
        await self.stop_monitoring()
        await update.message.reply_text(" Monitoring stopped")

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show status"""
        status = f" Bot Status\n\n"
        status += f"Enabled: {'Yes' if self.enabled else 'No'}\n"
        status += f"Monitoring: {'Active' if self.is_monitoring else 'Stopped'}\n"
        status += f"Keywords: {len(self.keywords)}\n"
        status += f"Subreddits: {len(self.subreddits)}\n"
        status += f"Check interval: {self.check_interval}s\n"
        status += f"Processed items: {len(self.processed_items)}"
        await update.message.reply_text(status)

    async def run(self):
        """Run the bot"""
        app = Application.builder().token(self.telegram_token).build()
        
        # Commands
        app.add_handler(CommandHandler("start", self.start_cmd))
        app.add_handler(CommandHandler("menu", self.menu))
        app.add_handler(CommandHandler("status", self.status_cmd))
        app.add_handler(CommandHandler("startmon", self.startmon_cmd))
        app.add_handler(CommandHandler("stopmon", self.stopmon_cmd))
        
        # Callbacks
        app.add_handler(CallbackQueryHandler(self.callback_handler))
        
        # Text handler
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        
        # Start bot
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        logger.info("Bot started")
        
        # Auto-start monitoring if keywords exist
        if self.keywords:
            await self.start_monitoring()
        
        # Keep running
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutting down...")
            await self.stop_monitoring()
            if self.reddit:
                await self.reddit.close()

def main():
    bot = RedditTelegramBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Bot stopped")

if __name__ == "__main__":

    main()

