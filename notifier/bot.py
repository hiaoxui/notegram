from collections import defaultdict
import time
import logging
from threading import Thread
from functools import partial

from telegram.ext import Updater, CallbackContext, CommandHandler
from telegram import Update, Bot

from notifier.util import load_config
from notifier.db import Storage

logger = logging.getLogger('notifier')


class TelegramBot:
    def __init__(self, cfg_path=None):
        self.cfg = load_config(cfg_path)
        self.db = Storage(**self.cfg['db'])
        self.bot = Bot(self.cfg['telegram']['token'])
        self.updater = Updater(token=self.cfg['telegram']['token'], use_context=True)
        link_handler = CommandHandler('link', self.link)
        self.updater.dispatcher.add_handler(link_handler)
        self.add_pull_handlers()
        self.message_count = defaultdict(int)
        self.last_send = 0

    def add_pull_handlers(self):
        pull_handler = CommandHandler('pull', self.pull)
        self.updater.dispatcher.add_handler(pull_handler)
        for level_name, level in zip(['debug', 'info', 'warning', 'error', 'critical'], range(10, 60, 10)):
            handler = CommandHandler(level_name, partial(self.pull, level=level))
            self.updater.dispatcher.add_handler(handler)

    def link(self, update: Update, context: CallbackContext):
        args = context.args
        chat_id = update.effective_chat.id
        if len(args) < 2 or args[1] != self.cfg['telegram']['secret']:
            context.bot.send_message(chat_id=chat_id, text='Error')
            return
        self.db.link_chat(args[0], chat_id)
        context.bot.send_message(chat_id=chat_id, text='Received')

    def pull(self, update: Update, context: CallbackContext, level=None):
        logger.warning('Pulling')
        args = context.args
        chat_id = update.effective_chat.id
        past = int(args[0]) if len(args) >= 1 else 3600
        domains = self.db.cid2domain(chat_id)
        for domain in domains:
            self.log(domain, chat_id, level, past, True)
        logger.warning('Pulling done')

    def run(self):
        all_tg = self.db.fetch_tg()
        for cid, domain, report_level, _ in all_tg:
            t = Thread(target=self.cron, args=(domain, cid, report_level))
            t.start()
        self.updater.start_polling()

    def log(self, domain, cid, level, past, repeat=False):
        messages = self.db.pull(domain, level, past)
        for mid, content in messages:
            if self.message_count[(cid, mid)] > 0 and not repeat:
                continue
            self.bot.send_message(text=content, chat_id=cid)
            self.message_count[(cid, mid)] += 1
            time.sleep(max(0., self.last_send + 1.0 - time.time()))
            self.last_send = time.time()

    def cron(self, domain, cid, level):
        freq = int(self.cfg['telegram'].get('freq', 10))
        while True:
            try:
                self.log(domain, cid, level, max(10, freq*2), False)
                time.sleep(freq)
            except Exception as e:
                logger.error(e)
            time.sleep(freq)


if __name__ == '__main__':
    tb = TelegramBot()
    tb.run()
