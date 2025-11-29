import os
import logging
from copy import deepcopy
import json
import asyncio


def async_partial(f, **kwargs):
    async def f2(*args2, **kwargs2):
        result = f(*args2, **kwargs, **kwargs2)
        if asyncio.iscoroutinefunction(f):
            result = await result
        return result
    return f2


CONFIG_PATHS = [
    './notifier.json',
    os.path.join(os.environ.get('HOME', '/home/hiaoxui'), '.config/notifier.json'),
    '/etc/notifier.json'
]


def load_config(path=None):
    tries = deepcopy(CONFIG_PATHS)
    if path is not None:
        tries.insert(0, path)
    for p in tries:
        if os.path.exists(p):
            return json.load(open(p))


def get_logger():
    _logger = logging.getLogger('notifier')
    fmt = logging.Formatter('%(asctime)s - %(message)s')
    stm_hdl = logging.StreamHandler()
    stm_hdl.setFormatter(fmt)
    _logger.addHandler(stm_hdl)
    path = os.path.join(os.environ.get('HOME'), '.var', 'log', 'notifier.log')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f_hdl = logging.FileHandler(path)
    f_hdl.setFormatter(fmt)
    _logger.addHandler(f_hdl)
    return _logger


logger = get_logger()
