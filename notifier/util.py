import os
from copy import deepcopy
import json


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

