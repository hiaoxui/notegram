import logging
from notifier import NotifierHandler

logger = logging.getLogger('notifier_test')
logger.addHandler(NotifierHandler())
logger.addHandler(logging.StreamHandler())
logger.warning('A test message is sent!')
