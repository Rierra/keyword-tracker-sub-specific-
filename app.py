#!/usr/bin/env python3
"""
Reddit to Telegram Monitor Bot - Simplified Version
Monitors specific subreddits for keywords
Supports Multiple Telegram Groups
"""

import os
import json
import time
import logging
import asyncio
import re
from typing import Set, Optional, Dict, Any
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
        # Default/Admin chat ID (optional fallback)
        self.default_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        self.reddit_client_id = os.getenv('REDDIT_CLIENT_ID')
        self.reddit_client_secret = os.getenv('REDDIT_CLIENT_SECRET')
        self.reddit_user_agent = os.getenv('REDDIT_USER_AGENT', 'TelegramBot:v1.0')
        self.reddit_username = os.getenv('REDDIT_USERNAME', '')
        self.reddit_password = os.getenv('REDDIT_PASSWORD', '')
        
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '300'))
        self.search_limit = int(os.getenv('SEARCH_LIMIT', '50'))
        
        # Data storage
        # Structure: { str(chat_id): { 'keywords': set(), 'subreddits': set(), 'processed_items': set(), 'enabled': bool } }
        self.groups: Dict[str, Dict[str, Any]] = {}
        
        # State for menu navigation per chat
        self.menu_states: Dict[str, str] = {}  
        
        self.data_file = 'bot_data.json'
        self.reddit: Optional[asyncpraw.Reddit] = None
        
        # Monitoring control
        self.is_monitoring = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.stream_task: Optional[asyncio.Task] = None
        
        self.load_data()

    def load_data(self):
        """Load group data"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r') as f:
                    data = json.load(f)
                    
                    # Check if migration is needed (old format had 'keywords' at top level)
                    if 'keywords' in data and 'groups' not in data:
                        logger.info("Migrating data to multi-group format...")
                        default_id = str(self.default_chat_id) if self.default_chat_id else "default"
                        self.groups = {
                            default_id: {
                                'keywords': set(data.get('keywords', [])),
                                'subreddits': set(data.get('subreddits', ['all'])),
                                'processed_items': set(data.get('processed_items', [])),
                                'enabled': data.get('enabled', True)
                            }
                        }
                    else:
                        # Load new format
                        raw_groups = data.get('groups', {})
                        self.groups = {}
                        for chat_id, config in raw_groups.items():
                            self.groups[chat_id] = {
                                'keywords': set(config.get('keywords', [])),
                                'subreddits': set(config.get('subreddits', ['all'])),
                                'processed_items': set(config.get('processed_items', [])),
                                'enabled': config.get('enabled', True)
                            }
                        
                    logger.info(f"Loaded config for {len(self.groups)} groups")
            else:
                logger.info("No data file found, starting fresh")
        except Exception as e:
            logger.error(f"Error loading data: {e}")

    def save_data(self):
        """Save data to file"""
        try:
            serializable_groups = {}
            for chat_id, config in self.groups.items():
                # Keep only recent 5000 processed items per group
                if len(config['processed_items']) > 5000:
                    config['processed_items'] = set(list(config['processed_items'])[-2000:])
                
                serializable_groups[chat_id] = {
                    'keywords': list(config['keywords']),
                    'subreddits': list(config['subreddits']),
                    'processed_items': list(config['processed_items']),
                    'enabled': config['enabled']
                }
            
            data = {'groups': serializable_groups}
            
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving data: {e}")

    def get_group_config(self, chat_id: int) -> Dict[str, Any]:
        """Get or create config for a chat ID"""
        str_id = str(chat_id)
        if str_id not in self.groups:
            self.groups[str_id] = {
                'keywords': set(),
                'subreddits': {'all'},
                'processed_items': set(),
                'enabled': True
            }
        return self.groups[str_id]

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
                
                msg = f"🔍 Keyword: {keyword}\n\n"
                msg += f"📝 {title}\n"
                msg += f"👤 u/{item.author} | 📍 r/{item.subreddit}\n"
                if content:
                    msg += f"\n{content}\n"
                msg += f"\n🔗 https://reddit.com{item.permalink}"
            else:  # comment
                content = ""
                if hasattr(item, 'body') and item.body:
                    content = item.body[:300] + "..." if len(item.body) > 300 else item.body
                
                msg = f"🔍 Keyword: {keyword}\n\n"
                msg += f"💬 Comment by u/{item.author}\n"
                msg += f"📍 r/{item.subreddit}\n"
                if content:
                    msg += f"\n{content}\n"
                msg += f"\n🔗 https://reddit.com{item.permalink}"
            
            return msg
        except Exception as e:
            logger.error(f"Error formatting notification: {e}")
            return f"🔍 Keyword: {keyword}\n\nError formatting notification"

    async def send_message(self, chat_id: str, text: str):
        """Send message to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    'chat_id': chat_id,
                    'text': text,
                    'disable_web_page_preview': True
                }) as response:
                    if response.status != 200:
                        logger.error(f"Failed to send message to {chat_id}: {await response.text()}")
        except Exception as e:
            logger.error(f"Error sending message: {e}")

    async def search_for_group(self, chat_id: str, config: Dict[str, Any]):
        """Perform search for a specific group"""
        if not config['enabled'] or not config['keywords']:
            return

        keywords = config['keywords']
        subreddits = config['subreddits']
        processed = config['processed_items']
        
        # 1. Search Posts
        for sub_name in subreddits:
            if not self.is_monitoring: break
            try:
                subreddit = await self.reddit.subreddit(sub_name)
                for keyword in keywords:
                    if not self.is_monitoring: break
                    async for post in subreddit.search(keyword, sort='new', time_filter='day', limit=self.search_limit):
                        if not self.is_monitoring: break
                        
                        if post.id in processed:
                            continue
                        
                        title_match = self.contains_keyword(post.title, keyword)
                        body_match = False
                        if hasattr(post, 'selftext') and post.selftext:
                            body_match = self.contains_keyword(post.selftext, keyword)
                        
                        if title_match or body_match:
                            message = self.format_notification(post, keyword, "post")
                            await self.send_message(chat_id, message)
                            processed.add(post.id)
                            await asyncio.sleep(2)
                        
                        await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Error searching r/{sub_name} for group {chat_id}: {e}")
                continue

        # 2. Search Comments
        for sub_name in subreddits:
            if not self.is_monitoring: break
            try:
                subreddit = await self.reddit.subreddit(sub_name)
                for keyword in keywords:
                    if not self.is_monitoring: break
                    async for comment in subreddit.comments(limit=self.search_limit):
                        if not self.is_monitoring: break
                        if comment.id in processed: continue
                        
                        if hasattr(comment, 'body') and self.contains_keyword(comment.body, keyword):
                            message = self.format_notification(comment, keyword, "comment")
                            await self.send_message(chat_id, message)
                            processed.add(comment.id)
                            await asyncio.sleep(2)
                        await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Error searching comments in r/{sub_name} for group {chat_id}: {e}")

    async def stream_comments(self):
        """Stream real-time comments for ALL groups"""
        logger.info("Starting comment stream...")
        
        while self.is_monitoring:
            try:
                if not self.reddit:
                    await self.setup_reddit()
                
                # Aggregate all unique subreddits from all enabled groups
                all_subs = set()
                for config in self.groups.values():
                    if config['enabled']:
                        all_subs.update(config['subreddits'])
                
                if not all_subs:
                    await asyncio.sleep(30)
                    continue

                sub_string = '+'.join(all_subs)
                try:
                    subreddit = await self.reddit.subreddit(sub_string)
                except Exception as e:
                    logger.error(f"Error creating subreddit stream object: {e}")
                    await asyncio.sleep(30)
                    continue
                
                logger.info(f"Streaming from combined subreddits: {len(all_subs)}")
                
                async for comment in subreddit.stream.comments(skip_existing=True):
                    if not self.is_monitoring:
                        break
                    
                    # For each new comment, check which groups should receive it
                    # This is slightly inefficient O(N_groups * M_keywords) but ok for small scale
                    
                    sub_name = comment.subreddit.display_name.lower()
                    comment_body = comment.body if hasattr(comment, 'body') else ""
                    
                    for chat_id, config in self.groups.items():
                        if not config['enabled']: continue
                        
                        # Check if this group is watching this subreddit
                        # Handle 'all' separately or assume specific subs
                        if 'all' not in config['subreddits'] and sub_name not in config['subreddits']:
                            continue
                            
                        # Check if comment has a keyword this group wants
                        for keyword in config['keywords']:
                            if self.contains_keyword(comment_body, keyword):
                                # Check duplicate per group
                                if comment.id not in config['processed_items']:
                                    message = self.format_notification(comment, keyword, "comment")
                                    await self.send_message(chat_id, message)
                                    config['processed_items'].add(comment.id)
                                    # Don't break, multiple keywords might match? 
                                    # Actually better to send once per comment per group
                                    break 
            
            except Exception as e:
                logger.error(f"Error in comment stream: {e}")
                await asyncio.sleep(30)

    async def monitor_loop(self):
        """Main monitoring loop"""
        while self.is_monitoring:
            try:
                if not self.groups:
                    logger.info("No groups configured yet")
                    await asyncio.sleep(60)
                    continue
                
                logger.info(f"Starting cycle for {len(self.groups)} groups")
                
                for chat_id, config in list(self.groups.items()):
                    if not self.is_monitoring: break
                    await self.search_for_group(chat_id, config)
                
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
        chat_id = update.effective_chat.id
        self.get_group_config(chat_id) # Ensure initialized
        self.save_data()
        
        help_text = """
🤖 Reddit to Telegram Monitor Bot (Multi-Group Supported)

Commands work for THIS group/chat only:
/start - Show this help
/menu - Open interactive menu
/status - Show current status
/toggle - Enable/disable monitoring for this group

/addkw <keyword> - Add keyword
/removekw <keyword> - Remove keyword
/listkw - List keywords
/clearkw - Clear all keywords

/addsub <subreddit> - Add subreddit (without r/)
/removesub <subreddit> - Remove subreddit
/listsub - List subreddits
/clearsub - Clear subreddits (resets to r/all)

/startmon - Start global monitoring (Admin)
/stopmon - Stop global monitoring (Admin)
        """
        await update.message.reply_text(help_text.strip())

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show interactive menu"""
        chat_id = update.effective_chat.id
        config = self.get_group_config(chat_id)
        
        keyboard = [
            [InlineKeyboardButton("➕ Add Keywords", callback_data="add_kw"),
             InlineKeyboardButton("➖ Remove Keywords", callback_data="remove_kw")],
            [InlineKeyboardButton("📋 List Keywords", callback_data="list_kw"),
             InlineKeyboardButton("🗑️ Clear Keywords", callback_data="clear_kw")],
            [InlineKeyboardButton("➕ Add Subreddits", callback_data="add_sub"),
             InlineKeyboardButton("➖ Remove Subreddits", callback_data="remove_sub")],
            [InlineKeyboardButton("📋 List Subreddits", callback_data="list_sub"),
             InlineKeyboardButton("🗑️ Reset Subreddits", callback_data="clear_sub")],
            [InlineKeyboardButton("📊 Status", callback_data="status"),
             InlineKeyboardButton(f"🔄 {'Disable' if config['enabled'] else 'Enable'}", callback_data="toggle")],
             # Global monitor toggle removed from individual menu to avoid confusion, 
             # or kept if any user can stop the bot. Let's keep it but label it "Global"
            [InlineKeyboardButton(f"{'⏹️ Stop' if self.is_monitoring else '▶️ Start'} Global Monitor", callback_data="toggle_mon")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("🤖 Bot Menu (For this group):", reply_markup=reply_markup)

    async def callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        chat_id = update.effective_chat.id
        str_id = str(chat_id)
        config = self.get_group_config(chat_id)
        
        if data == "add_kw":
            self.menu_states[str_id] = "adding_keywords"
            await query.edit_message_text(
                f"Current keywords: {', '.join(sorted(config['keywords'])) if config['keywords'] else 'None'}\n\n"
                f"Send keywords separated by commas:\nExample: pain killer, crypto news"
            )
        
        elif data == "remove_kw":
            if not config['keywords']:
                await query.edit_message_text("No keywords to remove")
                return
            self.menu_states[str_id] = "removing_keywords"
            await query.edit_message_text(
                f"Current keywords:\n{chr(10).join(sorted(config['keywords']))}\n\n"
                f"Send keywords to remove (comma-separated):"
            )
        
        elif data == "list_kw":
            kw_list = "\n".join(sorted(config['keywords'])) if config['keywords'] else "None"
            await query.edit_message_text(f"Keywords ({len(config['keywords'])}):\n{kw_list}")
        
        elif data == "clear_kw":
            count = len(config['keywords'])
            config['keywords'].clear()
            self.save_data()
            await query.edit_message_text(f"Cleared {count} keywords")
        
        elif data == "add_sub":
            self.menu_states[str_id] = "adding_subs"
            await query.edit_message_text(
                f"Current subreddits: {', '.join(sorted(config['subreddits']))}\n\n"
                f"Send subreddit names (without r/) separated by commas:\n"
                f"Example: wallstreetbets, technology"
            )
        
        elif data == "remove_sub":
            if not config['subreddits']:
                await query.edit_message_text("No subreddits configured")
                return
            self.menu_states[str_id] = "removing_subs"
            await query.edit_message_text(
                f"Current subreddits:\n{chr(10).join(sorted(config['subreddits']))}\n\n"
                f"Send subreddit names to remove (comma-separated):"
            )
        
        elif data == "list_sub":
            sub_list = "\n".join(sorted(config['subreddits']))
            await query.edit_message_text(f"Subreddits ({len(config['subreddits'])}):\n{sub_list}")
        
        elif data == "clear_sub":
            config['subreddits'] = {'all'}
            self.save_data()
            await query.edit_message_text("Reset to r/all")
        
        elif data == "status":
            status = f"Status for Group {chat_id}:\n"
            status += f"Enabled: {'Yes' if config['enabled'] else 'No'}\n"
            status += f"Global Monitoring: {'Active' if self.is_monitoring else 'Stopped'}\n"
            status += f"Keywords: {len(config['keywords'])}\n"
            status += f"Subreddits: {len(config['subreddits'])}\n"
            status += f"Processed items: {len(config['processed_items'])}"
            await query.edit_message_text(status)
        
        elif data == "toggle":
            config['enabled'] = not config['enabled']
            self.save_data()
            await query.edit_message_text(f"Bot {'enabled' if config['enabled'] else 'disabled'} for this group")
        
        elif data == "toggle_mon":
            if self.is_monitoring:
                await self.stop_monitoring()
                await query.edit_message_text("⏹️ Global Monitoring stopped")
            else:
                await self.start_monitoring()
                await query.edit_message_text("▶️ Global Monitoring started")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text input for adding/removing items"""
        chat_id = update.effective_chat.id
        str_id = str(chat_id)
        
        if str_id not in self.menu_states or not self.menu_states[str_id]:
            return
            
        config = self.get_group_config(chat_id)
        state = self.menu_states[str_id]
        
        text = update.message.text.strip()
        items = [item.strip().lower() for item in text.split(',') if item.strip()]
        
        if not items:
            await update.message.reply_text("No valid items found")
            return
        
        if state == "adding_keywords":
            added = [kw for kw in items if kw not in config['keywords']]
            config['keywords'].update(added)
            self.save_data()
            await update.message.reply_text(f"Added {len(added)} keywords:\n{', '.join(added)}")
        
        elif state == "removing_keywords":
            removed = [kw for kw in items if kw in config['keywords']]
            for kw in removed:
                config['keywords'].remove(kw)
            self.save_data()
            await update.message.reply_text(f"Removed {len(removed)} keywords:\n{', '.join(removed)}")
        
        elif state == "adding_subs":
            added = [sub for sub in items if sub not in config['subreddits']]
            config['subreddits'].update(added)
            if 'all' in config['subreddits'] and len(config['subreddits']) > 1:
                config['subreddits'].remove('all')
            self.save_data()
            await update.message.reply_text(f"Added {len(added)} subreddits:\n{', '.join(added)}")
        
        elif state == "removing_subs":
            removed = [sub for sub in items if sub in config['subreddits']]
            for sub in removed:
                config['subreddits'].remove(sub)
            if not config['subreddits']:
                config['subreddits'] = {'all'}
            self.save_data()
            await update.message.reply_text(f"Removed {len(removed)} subreddits:\n{', '.join(removed)}")
        
        self.menu_states[str_id] = None

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
        await update.message.reply_text("▶️ Global Monitoring started")

    async def stopmon_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stop monitoring"""
        await self.stop_monitoring()
        await update.message.reply_text("⏹️ Global Monitoring stopped")

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show status"""
        chat_id = update.effective_chat.id
        config = self.get_group_config(chat_id)
        
        status = f"📊 Bot Status for Group {chat_id}\n\n"
        status += f"Enabled: {'Yes' if config['enabled'] else 'No'}\n"
        status += f"Global Monitoring: {'Active' if self.is_monitoring else 'Stopped'}\n"
        status += f"Keywords: {len(config['keywords'])}\n"
        status += f"Subreddits: {len(config['subreddits'])}\n"
        status += f"Check interval: {self.check_interval}s\n"
        status += f"Processed items: {len(config['processed_items'])}"
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
        
        # Auto-start monitoring if any group has keywords
        any_keywords = any(g['keywords'] for g in self.groups.values())
        if any_keywords:
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