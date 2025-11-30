import logging
from notegram import NotegramHandler

logger = logging.getLogger('NoteGram Test')
logger.addHandler(NotegramHandler())
logger.addHandler(logging.StreamHandler())
logger.warning('A test message is sent!')
