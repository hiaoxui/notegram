from collections import defaultdict
import time

try:
    from telegram.ext import CallbackContext, CommandHandler, ApplicationBuilder, ContextTypes
    from telegram import Update
except ImportError:
    pass

from notifier.util import load_config, async_partial, logger
from notifier.db import Storage


class TelegramBot:
    def __init__(self, cfg_path=None):
        self.cfg = load_config(cfg_path)
        self.db = Storage(**self.cfg['db'])
        self.application = ApplicationBuilder().token(self.cfg['telegram']['token']).build()

        link_handler = CommandHandler('link', self.link)
        self.application.add_handler(link_handler)
        self.application.add_handler(CommandHandler('ping', self.ping))
        self.add_pull_handlers()

        self.message_count = defaultdict(int)
        self.last_send = 0

    @staticmethod
    async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_message(update.effective_chat.id, 'pong')

    def add_pull_handlers(self):
        pull_handler = CommandHandler('pull', self.pull)
        self.application.add_handler(pull_handler)

        for level_name, level in zip(['debug', 'info', 'warning', 'error', 'critical'], range(10, 60, 10)):
            handler = CommandHandler(level_name, async_partial(self.pull, level_range=[level, level]))
            self.application.add_handler(handler)

    async def link(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        chat_id = update.effective_chat.id
        if len(args) < 2 or args[1] != self.cfg['telegram']['secret']:
            await context.bot.send_message(chat_id=chat_id, text='Error')
            return
        did, cid, is_new = self.db.link_chat(args[0], chat_id)
        if is_new:
            await context.bot.send_message(chat_id=chat_id, text='Received')
            freq = self.cfg['telegram'].get('freq', 10)
            cron_partial = async_partial(self.cron, domain=did, cid=cid, level=30, freq=freq)
            self.application.job_queue.run_repeating(cron_partial, freq)
        else:
            await context.bot.send_message(chat_id=chat_id, text='Already exits')

    async def pull(self, update: Update, context: CallbackContext, level_range=None):
        if level_range is None:
            level_range = [10, 100]
        logger.warning('Pulling')
        args = context.args
        chat_id = update.effective_chat.id
        past = int(args[0]) if len(args) >= 1 else 3600
        domains = self.db.cid2domain(chat_id)
        for domain in domains:
            for msg in self.messages_to_log(domain, chat_id, level_range, past, True):
                await context.bot.send_message(chat_id, msg, 'Markdown')
        logger.warning('Pulling done')

    def run(self):
        all_tg = self.db.fetch_tg()
        freq = int(self.cfg['telegram'].get('freq', 10))
        for job_idx, (cid, domain, report_level, _) in enumerate(all_tg):
            cron_partial = async_partial(self.cron, domain=domain, cid=cid, level=report_level, freq=freq)
            self.application.job_queue.run_repeating(cron_partial, freq, first=job_idx)
        self.application.run_polling()

    def messages_to_log(self, domain, cid, level_range, past, repeat=False):
        messages = self.db.pull(domain, level_range, past)
        for mid, content in messages:
            if self.message_count[(cid, mid)] > 0 and not repeat:
                continue
            yield content
            self.message_count[(cid, mid)] += 1
            time.sleep(max(0., self.last_send + 1.0 - time.time()))
            self.last_send = time.time()

    async def cron(self, context: ContextTypes.DEFAULT_TYPE, domain, cid, level, freq):
        for msg in self.messages_to_log(domain, cid, [level, 100], max(10, freq*10), False):
            await context.bot.send_message(cid, msg, 'Markdown')


if __name__ == '__main__':
    tb = TelegramBot()
    tb.run()
