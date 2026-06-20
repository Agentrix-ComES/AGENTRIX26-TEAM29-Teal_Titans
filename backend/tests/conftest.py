"""Shared pytest configuration.

Provides a zero-dependency runner for ``@pytest.mark.asyncio`` coroutine tests so
the M5 vision/storage suites execute even when ``pytest-asyncio`` is not installed
(e.g. offline CI). If ``pytest-asyncio`` *is* installed it owns these tests and this
fallback steps aside.
"""
from __future__ import annotations

import asyncio
import inspect
import sys

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: run the test in a fresh asyncio event loop")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    # Defer to pytest-asyncio when it is available.
    if "pytest_asyncio" in sys.modules:
        return None
    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(func(**kwargs))
    return True
