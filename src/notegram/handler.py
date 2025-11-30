from logging import handlers, Handler, LogRecord, warning, getLogger
from threading import Thread

from notegram.db import Storage
from notegram.util import load_config

logger = getLogger('notifier')


class NotifierHandler(Handler):
    def __init__(self, cfg_path=None):
        super().__init__()
        config = load_config(cfg_path)
        self.db: Storage = None # type: ignore
        if config is not None:
            self.db = Storage(**config['db'])
        else:
            warning('DB config not found. Notifier disabled.')

    def emit(self, record: LogRecord) -> None:
        def post_message():
            try:
                self.db.post(level=record.levelno, domain=record.name, message=record.getMessage())
            except Exception as e:
                logger.error('Exception for handler. msg=' + str(e))

        if self.db is not None:
            t = Thread(target=post_message, args=())
            t.start()
