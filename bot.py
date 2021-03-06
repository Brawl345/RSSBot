#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import gettext
import html
import logging
import re
import sys
from configparser import ConfigParser
from json import loads
from urllib.parse import urlparse

import feedparser
import redis
import telegram
from telegram.error import Unauthorized
from telegram.ext import CommandHandler, Updater
from telegram.ext.dispatcher import run_async

import utils

config = ConfigParser()
try:
    config.read_file(open("config.ini"))
except FileNotFoundError:
    logging.critical("config.ini not found")
    sys.exit(1)

# Language
try:
    lang = config["DEFAULT"]["lang"]
except KeyError:
    lang = "en"

el = gettext.translation("base", localedir="locales", languages=[lang, "en"])
el.install()
_ = el.gettext

# Logging
try:
    logging_conf = config["LOGGING"]
    logging_level = logging_conf.get("level", "INFO")
    logging_format = logging_conf.get("format", "%(asctime)s - %(levelname)s: %(message)s", raw=True)
    if logging_level not in ["DEBUG", "INFO", "CRITICAL", "ERROR", "WARNING"]:
        logging.warning(_("Logging Level invalid. Will be changed to INFO"))
        logging.basicConfig(format=logging_format, level=logging.INFO, datefmt="%d.%m.%Y %H:%M:%S")
    else:
        logging.basicConfig(format=logging_format,
                            level=eval("logging.{0}".format(logging_level.upper())),
                            datefmt="%d.%m.%Y %H:%M:%S")
except KeyError:
    logging.basicConfig(format="%(asctime)s - %(levelname)s: %(message)s",
                        level=logging.INFO,
                        datefmt="%d.%m.%Y %H:%M:%S")
logger = logging.getLogger(__name__)

# Bot token
try:
    bot_token = config["DEFAULT"]["token"]
except KeyError:
    logger.error(_("Bot token is missing, check config.ini."))
    sys.exit(1)
if not bot_token:
    logger.error(_("Bot token is missing, check config.ini."))
    sys.exit(1)

# Admins
try:
    admins = loads(config["ADMIN"]["id"])
except KeyError:
    logger.error(_("No admin IDs are set, check config.ini."))
    sys.exit(1)
if not admins:
    logger.error(_("No admin IDs are set, check config.ini."))
    sys.exit(1)

for admin in admins:
    if not isinstance(admin, int):
        logger.error(_("Admin IDs need to be integers."))
        sys.exit(1)

# Redis
redis_conf = config["REDIS"]
redis_db = redis_conf.get("db", 0)
redis_host = redis_conf.get("host", "127.0.0.1")
redis_port = redis_conf.get("port", 6379)
redis_socket = redis_conf.get("socket_path")
if redis_socket:
    r = redis.Redis(unix_socket_path=redis_socket, db=int(redis_db), decode_responses=True)
else:
    r = redis.Redis(host=redis_host, port=int(redis_port), db=int(redis_db), decode_responses=True)

if not r.ping():
    logging.getLogger("Redis").critical(_("Failed to connect to Redis server."))
    sys.exit(1)

feed_hash = "pythonbot:rss:{0}"


@run_async
def start(update, context):
    if not utils.can_use_bot(update):
        return
    update.message.reply_text(
        text=_("<b>Welcome to the RSS Bot!</b>\nSend /help to get started."),
        parse_mode=telegram.ParseMode.HTML
    )


@run_async
def help_text(update, context):
    if not utils.can_use_bot(update):
        return
    update.message.reply_text(
        text=_("<b>/rss</b> <i>[Chat]</i>: Show subbed feeds\n"
               "<b>/sub</b> <i>Feed URL</i> <i>[Chat]</i>: Sub to feed\n"
               "<b>/del</b> <i>n</i> <i>[Chat]</i>: Unsubscribe feed\n"
               "<i>[Chat]</i> is an optional argument with the @Channelname."),
        parse_mode=telegram.ParseMode.HTML
    )


@run_async
def list_feeds(update, context):
    if not utils.can_use_bot(update):
        return
    if context.args:
        chat_name = context.args[0]
        try:
            resp = context.bot.getChat(chat_name)
        except telegram.error.BadRequest:
            update.message.reply_text("❌ " + _("This channel does not exist."))
            return
        chat_id = str(resp.id)
        chat_title = resp.title
    else:
        chat_id = str(update.message.chat.id)
        if update.message.chat.type == "private":
            chat_title = update.message.chat.first_name
        else:
            chat_title = update.message.chat.title

    subs = r.smembers(feed_hash.format(chat_id))
    if not subs:
        text = "❌ " + _("There are no feeds.")
    else:
        text = _("<b>{0}</b> is subscribed to:\n").format(html.escape(chat_title))
        for n, feed in enumerate(subs):
            text += "<b>" + str(n + 1) + ")</b> " + feed + "\n"

    update.message.reply_text(
        text=text,
        parse_mode=telegram.ParseMode.HTML
    )


@run_async
def subscribe(update, context):
    if not utils.can_use_bot(update):
        return
    if not context.args:
        update.message.reply_text("❌ " + _("No feed URL given."))
        return
    feed_url = context.args[0]
    if not re.match("^http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&~+]|[!*(),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+$", feed_url):
        update.message.reply_text("❌ " + _("That doesn't look like an URL."))
        return

    # Get Chat ID from name if given
    if len(context.args) > 1:
        chat_name = context.args[1]
        try:
            resp = context.bot.getChat(chat_name)
        except telegram.error.BadRequest:
            update.message.reply_text("❌ " + _("This channel does not exist."))
            return
        chat_id = str(resp.id)
        resp = context.bot.getChatMember(chat_id, context.bot.id)
        if resp.status != "administrator":
            update.message.reply_text("❌ " + _("You need to add the bot as an administrator to the channel."))
            return
    else:
        chat_id = str(update.message.chat.id)

    context.bot.sendChatAction(update.message.chat.id, action=telegram.ChatAction.TYPING)
    data = feedparser.parse(feed_url)
    if "link" not in data.feed:
        update.message.reply_text("❌ " + _("Not a valid feed."))
        return
    feed_url = data.href  # Follow all redirects
    if r.sismember(feed_hash.format(chat_id), feed_url):
        update.message.reply_text("✅ " + _("Already subscribed to this feed."))
        return

    if "title" not in data.feed:
        feed_title = feed_url
    else:
        feed_title = html.escape(data.feed["title"])

    # Save the last entry in Redis, if it doesn't exist
    if data.entries:
        last_entry_hash = feed_hash.format(feed_url + ":last_entry")
        if not r.exists(last_entry_hash):
            if "id" not in data.entries[0]:
                last_entry = data.entries[0]["link"]
            else:
                last_entry = data.entries[0]["id"]
            r.set(last_entry_hash, last_entry)

    r.sadd(feed_hash.format(feed_url + ":subs"), chat_id)
    r.sadd(feed_hash.format(chat_id), feed_url)
    update.message.reply_text(
        text="✅ " + _("<b>{0}</b> added!").format(feed_title),
        parse_mode=telegram.ParseMode.HTML
    )


@run_async
def unsubscribe(update, context):
    if not utils.can_use_bot(update):
        return
    if not context.args:
        update.message.reply_text("❌ " + _("No number given."))
        return

    # Get Chat ID from name if given
    if len(context.args) > 1:
        chat_name = context.args[1]
        try:
            resp = context.bot.getChat(chat_name)
        except telegram.error.BadRequest:
            update.message.reply_text("❌ " + _("This channel does not exist."))
            return
        chat_id = str(resp.id)
    else:
        chat_id = str(update.message.chat.id)

    try:
        n = int(context.args[0])
    except ValueError:
        update.message.reply_text("❌ " + _("No number given."))
        return

    chat_hash = feed_hash.format(chat_id)
    subs = r.smembers(chat_hash)
    if n < 1:
        update.message.reply_text("❌ " + _("Number must be bigger than 0!"))
        return
    elif n > len(subs):
        update.message.reply_text("❌ " + _("Feed ID too high."))
        return

    feed_url = list(subs)[n - 1]
    sub_hash = feed_hash.format(feed_url + ":subs")
    r.srem(chat_hash, feed_url)
    r.srem(sub_hash, chat_id)
    if not r.smembers(sub_hash):  # no one subscribed, remove it
        r.delete(feed_hash.format(feed_url + ":last_entry"))

    update.message.reply_text(
        text="✅ " + _("<b>{0}</b> removed!").format(feed_url),
        parse_mode=telegram.ParseMode.HTML
    )


@run_async
def check_feed(bot, key):
    feed_url = re.match("^" + feed_hash.format("(.+):subs$"), key).group(1)
    logger.info(feed_url)
    data = feedparser.parse(feed_url)
    if "link" not in data.feed:
        if "status" in data and data["status"] != 200:
            logger.warning(_("{0} - Not a valid feed, got HTTP Code {1}").format(feed_url, data["status"]))
        else:
            logger.warning(_("{0} - Not a valid feed: {1}").format(feed_url, str(data.bozo_exception)))
        return None
    if "title" not in data.feed:
        feed_title = data.feed["link"]
    else:
        feed_title = data.feed["title"]
    last_entry_hash = feed_hash.format(feed_url + ":last_entry")
    last_entry = r.get(last_entry_hash)
    new_entries = utils.get_new_entries(data.entries, last_entry)
    for entry in reversed(new_entries):
        if "title" not in entry:
            post_title = _("No title")
        else:
            post_title = utils.remove_html_tags(entry["title"]).strip()
            post_title = post_title.replace("<", "&lt;").replace(">", "&gt;")
        if "link" not in entry:
            post_link = data.link
            link_name = post_link
        else:
            post_link = entry.link
            feedproxy = re.search("^https?://feedproxy\.google\.com/~r/(.+?)/.*", post_link)  # feedproxy.google.com
            if feedproxy:
                link_name = feedproxy.group(1)
            else:
                link_name = urlparse(post_link).netloc
        link_name = re.sub("^www\d?\.", "", link_name)  # remove www.
        if "content" in entry:
            content = utils.get_content(entry.content[0]["value"])
        elif "summary" in entry:
            content = utils.get_content(entry.summary)
        else:
            content = ""
        text = "<b>{post_title}</b>\n<i>{feed_title}</i>\n{content}".format(
            post_title=post_title,
            feed_title=feed_title,
            content=content
        )
        readmore = _("Read more on {0}").format(link_name)
        text += "\n<a href=\"{post_link}\">{readmore}</a>\n".format(
            post_link=post_link,
            readmore=readmore
        )
        for member in r.smembers(key):
            try:
                bot.sendMessage(
                    chat_id=member,
                    text=text,
                    parse_mode=telegram.ParseMode.HTML,
                    disable_web_page_preview=True
                )
            except telegram.error.Unauthorized:
                logger.warning(_("Chat {0} doesn't exist anymore, will be deleted.").format(member))
                r.srem(key, member)
                r.delete(feed_hash.format(member))
            except telegram.error.ChatMigrated as new_chat:
                new_chat_id = new_chat.new_chat_id
                logger.info(_("Chat migrated: ") + member + " -> " + str(new_chat_id))
                r.srem(key, member)
                r.sadd(key, new_chat_id)
                r.rename(feed_hash.format(member), feed_hash.format(new_chat_id))
                bot.sendMessage(
                    chat_id=member,
                    text=text,
                    parse_mode=telegram.ParseMode.HTML,
                    disable_web_page_preview=True
                )
            except telegram.error.TimedOut:
                pass
            except telegram.error.BadRequest as exception:
                logger.error(exception)

    if not r.exists(key):
        r.delete(last_entry_hash)
        return

    # Set the new last entry if there are any
    if new_entries:
        if "id" not in new_entries[0]:
            new_last_entry = new_entries[0].link
        else:
            new_last_entry = new_entries[0].id
        r.set(last_entry_hash, new_last_entry)


@run_async
def run_job(context):
    logger.info("================================")
    keys = r.keys(feed_hash.format("*:subs"))
    for key in keys:
        check_feed(context.bot, key)


def run_job_manually(update, context):
    run_job(context)


def onerror(update, context):
    logger.error(_("Update \"%s\" caused error \"%s\""), update, context.error)


# Main function
def main():
    # Setup the updater and show bot info
    updater = Updater(token=bot_token, use_context=True)
    try:
        logger.info(
            _("Starting {0}, AKA @{1} ({2})").format(updater.bot.first_name, updater.bot.username, updater.bot.id))
    except Unauthorized:
        logger.critical(_("Logging in failed, check bot token."))
        sys.exit(1)

    # Register Handlers
    handlers = [
        CommandHandler("start", start),
        CommandHandler("help", help_text),
        CommandHandler("rss", list_feeds, pass_args=True),
        CommandHandler("sub", subscribe, pass_args=True),
        CommandHandler("del", unsubscribe, pass_args=True),
        CommandHandler("sync", run_job_manually)
    ]
    for handler in handlers:
        updater.dispatcher.add_handler(handler)

    # Hide "Error while getting Updates" because it's not our fault
    updater.logger.addFilter((lambda log: not log.msg.startswith("Error while getting Updates:")))

    # Fix for Python <= 3.5
    updater.dispatcher.add_error_handler(onerror)

    updater.job_queue.run_repeating(
        run_job,
        interval=60.0,
        first=2.0
    )

    # Start this thing!
    updater.start_polling(
        clean=True,
        bootstrap_retries=-1,
        allowed_updates=["message"]
    )

    # Run Bot until CTRL+C is pressed or a SIGINIT,
    # SIGTERM or SIGABRT is sent.
    updater.idle()


if __name__ == "__main__":
    main()
