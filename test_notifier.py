import logging
from notegram import NotegramHandler

logger = logging.getLogger('notegram_test')
logger.addHandler(NotegramHandler())
logger.addHandler(logging.StreamHandler())
logger.warning('A test message is sent!')
