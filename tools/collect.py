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


# tests whose work is split into independent units (one per query): each unit
# gets its own coverage context "call|<test>#<k>"
SPLIT = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
}
unit_state = {"test": None, "k": 0}
unit_cpu = {}  # test -> {k: cpu seconds}


def _unit_ctx(k):
    t = unit_state["test"]
    if t is not None:
        set_ctx("call|%s#%d" % (t, k))


class InlineExecutor(concurrent.futures.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer, initargs = _roundtrip((initializer, initargs))
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        import time as _t

        f = concurrent.futures.Future()
        k = unit_state["k"]
        _unit_ctx(k)
        c0 = _t.process_time()
        try:
            fn, args, kwargs = _roundtrip((fn, args, kwargs))
            f.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        if unit_state["test"] is not None:
            d = unit_cpu.setdefault(unit_state["test"], {})
            d[k] = d.get(k, 0) + _t.process_time() - c0
            unit_state["k"] = k + 1
            _unit_ctx(k + 1)
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
types_used = {}  # ctx -> set(classes instantiated or looked up)
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
        if item.nodeid in SPLIT:
            unit_state["test"], unit_state["k"] = item.nodeid, 0
            _unit_ctx(0)
        yield
        unit_state["test"] = None
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

    # which classes each test instantiates (with their bases): expressions,
    # dialects, tokenizers, parsers, generators
    from sqlglot import tokens, parser, generator
    from sqlglot.expressions.core import Expr

    def wrap_init(klass):
        orig = klass.__init__

        def __init__(self, *a, **k):
            s = types_used.get(state["ctx"])
            if s is None:
                s = types_used[state["ctx"]] = set()
            s.add(type(self))
            return orig(self, *a, **k)

        klass.__init__ = __init__

    for klass in (Expr, tokens.Tokenizer, parser.Parser, generator.Generator):
        wrap_init(klass)
    orig_dinit = Dialect.__init__

    def dinit(self, *a, **k):
        types_used.setdefault(state["ctx"], set()).add(type(self))
        return orig_dinit(self, *a, **k)

    Dialect.__init__ = dinit


import contextlib  # noqa: E402
import re as _re  # noqa: E402
import unittest  # noqa: E402

_orig_subtest = unittest.TestCase.subTest


@contextlib.contextmanager
def _subtest(self, msg=None, **params):
    t = unit_state["test"]
    m = _re.match(r"tpc-(?:h|ds) (\d+)$", str(msg)) if t else None
    if m:
        import time as _t

        k = int(m.group(1)) - 1
        _unit_ctx(k)
        c0 = _t.process_time()
        try:
            with _orig_subtest(self, msg, **params):
                yield
        finally:
            d = unit_cpu.setdefault(t, {})
            d[k] = d.get(k, 0) + _t.process_time() - c0
            set_ctx("call|" + t)
    else:
        with _orig_subtest(self, msg, **params):
            yield


unittest.TestCase.subTest = _subtest

set_ctx("boot")
rc = pytest.main(["-q", "-p", "no:cacheprovider", "-p", "no:randomly", *FILES], plugins=[Plugin()])
cov.stop()
cov.save()

def class_keys(types):
    keys = set()
    for t in types:
        try:
            mro = list(t.__mro__)
            for name in dir(t):
                if name.startswith("__"):
                    continue
                try:
                    v = getattr(t, name)
                except Exception:
                    continue
                if isinstance(v, type) and t.__module__.startswith("sqlglot.dialects"):
                    mro += list(v.__mro__)
        except Exception:
            continue
        for c in mro:
            m = getattr(c, "__module__", "") or ""
            if m.startswith("sqlglot"):
                keys.add(m + ":" + c.__qualname__)
    return keys


key_cache = {}
classes = {}
for ctx, ts in types_used.items():
    ks = set()
    for t in ts:
        if t not in key_cache:
            key_cache[t] = class_keys([t])
        ks |= key_cache[t]
    classes[ctx] = sorted(ks)
with _open(OUT + ".json", "w") as f:
    json.dump(
        {
            "rc": int(rc),
            "opened": {k: sorted(v) for k, v in opened.items()},
            "dialects": {k: sorted(v) for k, v in dialects.items()},
            "imported": {k: sorted(v) for k, v in imported.items()},
            "classes": classes,
            "unit_cpu": {t: {str(k): v for k, v in d.items()} for t, d in unit_cpu.items()},
        },
        f,
    )
print("done", rc, file=sys.stderr)
