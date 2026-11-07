"""Compatibility helpers; importing Notegram never configures application logging."""

import inspect
import logging
from functools import wraps

from .config import load_config as load_config

logger = logging.getLogger("notegram")


def async_partial(function, **kwargs):
    @wraps(function)
    async def call(*args, **extra):
        result = function(*args, **(kwargs | extra))
        return await result if inspect.isawaitable(result) else result

    return call


def get_logger():
    return logger
