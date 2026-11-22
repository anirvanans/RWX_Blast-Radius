"""Record, for every pytest test of sqlglot at the pinned commit, what it touches.

Run from the sqlglot checkout root:
    SKIP_INTEGRATION=0 python /path/to/collect.py OUT_PREFIX tests/test_a.py tests/test_b.py ...

Writes OUT_PREFIX.coverage (coverage data with one context per phase) and
OUT_PREFIX.json (fixture files opened, dialects used, modules imported).
Process pools are replaced by an in-process executor so the code they run is
measured too.
"""
import builtins
import concurrent.futures
import concurrent.futures.process
import io
import json
import os
import sys

ROOT = os.getcwd()
OUT = sys.argv[1]
FILES = sys.argv[2:]
sys.path.insert(0, ROOT)


import pickle  # noqa: E402
from multiprocessing.reduction import ForkingPickler  # noqa: E402


def _roundtrip(obj):
    # a real process pool pickles arguments and results; do the same so the
    # pickling code (sqlglot.serde via __reduce__) is measured too
    return pickle.loads(ForkingPickler.dumps(obj))


class InlineExecutor(concurrent.futures.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer, initargs = _roundtrip((initializer, initargs))
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            fn, args, kwargs = _roundtrip((fn, args, kwargs))
            f.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


concurrent.futures.ProcessPoolExecutor = InlineExecutor
concurrent.futures.process.ProcessPoolExecutor = InlineExecutor

import coverage  # noqa: E402

cov = coverage.Coverage(
    data_file=OUT + ".coverage",
    source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")],
    omit=[os.path.join(ROOT, "tests", "fixtures", "*")],
)
cov.start()

state = {"ctx": "boot"}
opened = {}  # ctx -> set(paths)
dialects = {}  # ctx -> set(modules)
imported = {}  # ctx -> set(modules)
FIX = os.path.join(ROOT, "tests", "fixtures") + os.sep

_open = builtins.open


def rec_open(file, *a, **k):
    try:
        p = os.path.abspath(os.fspath(file))
        if p.startswith(FIX):
            opened.setdefault(state["ctx"], set()).add(os.path.relpath(p, ROOT))
    except Exception:
        pass
    return _open(file, *a, **k)


builtins.open = rec_open
io.open = rec_open
import gzip  # noqa: E402

_gzopen = gzip.open


def rec_gzopen(filename, *a, **k):
    rec_open.__wrapped_check__ = True
    try:
        p = os.path.abspath(os.fspath(filename))
        if p.startswith(FIX):
            opened.setdefault(state["ctx"], set()).add(os.path.relpath(p, ROOT))
    except Exception:
        pass
    return _gzopen(filename, *a, **k)


gzip.open = rec_gzopen


def set_ctx(c):
    state["ctx"] = c
    cov.switch_context(c)


def snapshot():
    return {m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.")}


import pytest  # noqa: E402


class Plugin:
    def __init__(self):
        self.before = snapshot()

    def _mods(self, ctx):
        now = snapshot()
        new = now - self.before
        if new:
            imported.setdefault(ctx, set()).update(new)
        self.before = now

    @pytest.hookimpl(hookwrapper=True)
    def pytest_collect_file(self, file_path, parent):
        yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_pycollect_makemodule(self, module_path, parent):
        yield

    @pytest.hookimpl(tryfirst=True)
    def pytest_collectstart(self, collector):
        p = getattr(collector, "path", None)
        if p is not None and str(p).endswith(".py"):
            self._mods(state["ctx"])
            set_ctx("collect|" + os.path.relpath(str(p), ROOT))

    @pytest.hookimpl(hookwrapper=True)
    def pytest_collection_modifyitems(self, session, config, items):
        self._mods(state["ctx"])
        set_ctx("after-collect")
        hook_dialects()
        yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        set_ctx("setup|" + item.nodeid)
        yield
        self._mods("setup|" + item.nodeid)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        set_ctx("call|" + item.nodeid)
        yield
        self._mods("call|" + item.nodeid)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item, nextitem):
        set_ctx("teardown|" + item.nodeid)
        yield
        self._mods("teardown|" + item.nodeid)
        set_ctx("between")


def hook_dialects():
    from sqlglot.dialects.dialect import Dialect

    orig_init = Dialect.__init__
    cache = {}

    def mods_of(cls):
        if cls not in cache:
            mods = set()
            for c in cls.__mro__:
                mods.add(c.__module__)
            for name in dir(cls):
                try:
                    v = getattr(cls, name)
                except Exception:
                    continue
                if isinstance(v, type):
                    for c in v.__mro__:
                        mods.add(c.__module__)
            cache[cls] = {m for m in mods if m and m.startswith("sqlglot")}
        return cache[cls]

    def init(self, *a, **k):
        try:
            dialects.setdefault(state["ctx"], set()).update(mods_of(type(self)))
        except Exception:
            pass
        return orig_init(self, *a, **k)

    Dialect.__init__ = init


set_ctx("boot")
rc = pytest.main(["-q", "-p", "no:cacheprovider", "-p", "no:randomly", *FILES], plugins=[Plugin()])
cov.stop()
cov.save()
with _open(OUT + ".json", "w") as f:
    json.dump(
        {
            "rc": int(rc),
            "opened": {k: sorted(v) for k, v in opened.items()},
            "dialects": {k: sorted(v) for k, v in dialects.items()},
            "imported": {k: sorted(v) for k, v in imported.items()},
        },
        f,
    )
print("done", rc, file=sys.stderr)
