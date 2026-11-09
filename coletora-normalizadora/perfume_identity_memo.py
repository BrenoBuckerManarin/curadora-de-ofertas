"""Memoize pure identity parsing only within one synchronous ranking pass."""
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps

_CURRENT: ContextVar[dict | None] = ContextVar('perfume_identity_memo', default=None)

@contextmanager
def identity_memo_scope():
    token = _CURRENT.set({})
    try:
        yield
    finally:
        _CURRENT.reset(token)

def scoped_identity_memo(function):
    @wraps(function)
    def wrapped(value):
        cache = _CURRENT.get()
        if cache is None:
            return function(value)
        key = (function.__name__, value)
        if key not in cache:
            if len(cache) >= 16384:
                cache.clear()
            cache[key] = function(value)
        return cache[key]
    return wrapped
