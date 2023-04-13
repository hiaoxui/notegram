import os
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

