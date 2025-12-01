from collections import defaultdict
import time
from typing import Tuple

try:
    from telegram.ext import CallbackContext, CommandHandler, ApplicationBuilder, ContextTypes
    from telegram import Update
except ImportError:
    pass

from notegram.util import load_config, async_partial, logger
from notegram.db import Storage


class TelegramBot:
    def __init__(self, cfg_path=None):
        self.cfg = load_config(path=cfg_path)
        self.db = Storage(**self.cfg['db']) # type: ignore
        self.application = ApplicationBuilder().token(self.cfg['telegram']['token']).build()

        link_handler = CommandHandler('link', self.link)
        self.application.add_handler(handler=link_handler)
        self.application.add_handler(handler=CommandHandler('ping', self.ping))
        self.add_pull_handlers()

        self.message_count = defaultdict(int)
        self.last_send = 0

    @staticmethod
    async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
        assert update.effective_chat is not None
        await context.bot.send_message(chat_id=update.effective_chat.id, text='pong')

    def add_pull_handlers(self):
        # usage: /<level_name> [past_seconds]; pull messages from the specified levels in the past `past_seconds` seconds
        pull_handler = CommandHandler('pull', async_partial(self.pull, level_range=(10, 100)))
        self.application.add_handler(handler=pull_handler)

        for level_name, level in zip(['debug', 'info', 'warning', 'error', 'critical'], range(10, 60, 10)):
            handler = CommandHandler(level_name, async_partial(self.pull, level_range=(level, level)))
            self.application.add_handler(handler=handler)

    async def link(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        chat_id: int = update.effective_chat.id # type: ignore
        if len(args) < 2 or args[1] != self.cfg['telegram']['secret']: # type: ignore
            await context.bot.send_message(chat_id=chat_id, text='Error')
            return
        did, cid, is_new = self.db.link_chat(args[0], chat_id) # type: ignore
        if is_new:
            await context.bot.send_message(chat_id=chat_id, text='Received')
            freq = self.cfg['telegram'].get('freq', 10)
            cron_partial = async_partial(self.cron, domain=did, cid=cid, level=30, freq=freq)
            self.application.job_queue.run_repeating(cron_partial, freq) # type: ignore
        else:
            await context.bot.send_message(chat_id=chat_id, text='Already exits')

    async def pull(self, update: Update, context: CallbackContext, level_range: Tuple[int, int]):
        # handler function for /<level_name> [past_seconds]; pull messages from the specified levels in the past `past_seconds` seconds
        if level_range is None:
            level_range = [10, 100]
        logger.warning('Pulling')
        args = context.args
        chat_id: int = update.effective_chat.id # type: ignore
        past = int(args[0]) if len(args) >= 1 else 3600 # type: ignore
        domains = self.db.cid2domain(chat_id)
        for domain in domains:
            for content, markdown in self.messages_to_log(domain=domain, cid=chat_id, level_range=level_range, past=past, repeat=True):
                await context.bot.send_message(chat_id=chat_id, text=content, parse_mode='Markdown' if markdown else 'HTML')
        logger.warning('Pulling done')

    def run(self):
        all_tg = self.db.fetch_tg()
        freq = int(self.cfg['telegram'].get('freq', 10))
        for job_idx, (cid, domain, report_level, _) in enumerate(all_tg):
            assert self.application.job_queue is not None
            self.application.job_queue.run_repeating(callback=async_partial(self.cron, domain=domain, cid=cid, level=report_level, freq=freq), interval=freq, first=job_idx) # type: ignore
        self.application.run_polling()

    def messages_to_log(self, domain: int | str, cid: int, level_range: Tuple[int, int], past: int, repeat=False):
        messages = self.db.pull(domain=domain, level_range=level_range, past=past)
        for mid, content, markdown in messages:
            if self.message_count[(cid, mid)] > 0 and not repeat:
                continue
            yield content, markdown
            self.message_count[(cid, mid)] += 1
            time.sleep(max(0., self.last_send + 1.0 - time.time()))
            self.last_send = time.time()

    async def cron(self, context: ContextTypes.DEFAULT_TYPE, domain: int | str, cid: int, level: int, freq: int):
        for content, markdown in self.messages_to_log(domain=domain, cid=cid, level_range=(level, 100), past=max(10, freq*10), repeat=False):
            await context.bot.send_message(chat_id=cid, text=content, parse_mode='Markdown' if markdown else 'HTML')


if __name__ == '__main__':
    tb = TelegramBot()
    tb.run()
