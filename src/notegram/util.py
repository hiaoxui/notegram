from typing import List
import logging
from copy import deepcopy
from pathlib import Path
from typing import Dict
import tomllib

import asyncio


def async_partial(f, **kwargs):
    async def f2(*args2, **kwargs2):
        result = f(*args2, **kwargs, **kwargs2)
        if asyncio.iscoroutinefunction(f):
            result = await result
        return result
    return f2


CONFIG_PATHS: List[Path] = [
    Path('./').resolve(),
    Path('~/.config/').expanduser(),
    Path('~/walless.config.d/').expanduser(),
    Path('/etc/'),
]


def load_config(path=None) -> Dict: # type: ignore
    tries = deepcopy(CONFIG_PATHS)
    if path is not None:
        tries.insert(0, path)
    for p in tries:
        if not p.is_dir():
            continue
        for f in p.glob('*.toml'):
            if 'notegram' in f.name:
                with f.open('r') as fp:
                    return tomllib.loads(fp.read())['notegram']


def get_logger():
    _logger = logging.getLogger('notegram')
    fmt = logging.Formatter('%(asctime)s - %(message)s')
    stm_hdl = logging.StreamHandler()
    stm_hdl.setFormatter(fmt)
    _logger.addHandler(stm_hdl)
    path = Path('~/.var/log/notegram.log').expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    f_hdl = logging.FileHandler(path)
    f_hdl.setFormatter(fmt)
    _logger.addHandler(f_hdl)
    return _logger


logger = get_logger()
