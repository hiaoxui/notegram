from logging import handlers, Handler, LogRecord, warning
from threading import Thread

from notifier.db import Storage
from notifier.util import load_config


class NotifierHandler(Handler):
    def __init__(self, cfg_path=None):
        super().__init__()
        config = load_config(cfg_path)
        self.db = None
        if config is not None:
            self.db = Storage(**config['db'])
        else:
            warning('DB config not found. Notifier disabled.')

    def emit(self, record: LogRecord) -> None:
        def post_message():
            try:
                self.db.post(record.levelno, record.name, record.getMessage())
            except:
                pass

        if self.db is not None:
            t = Thread(target=post_message, args=())
            t.start()
