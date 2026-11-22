"""Run a few sqlglot tests in this process and report which fail.

    python runner.py TEST_ID [TEST_ID ...]

Run with the patched tree as the working directory and on sys.path. Prints
one JSON list of failing test ids on stdout. A test fails when it errors,
fails, has a failing subtest, or its file cannot be imported, as pytest counts.
"""
import importlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())
os.environ.setdefault("SKIP_INTEGRATION", "0")

import concurrent.futures  # noqa: E402
import concurrent.futures.process  # noqa: E402
import pickle  # noqa: E402
from multiprocessing.reduction import ForkingPickler  # noqa: E402


def _roundtrip(obj):
    # pickle like a process pool would, without starting processes
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


def main(ids):
    failfast = False
    if ids and ids[0] == "--failfast":
        failfast, ids = True, ids[1:]
    failing = set()
    by_module = {}
    for tid in ids:
        path, cls, meth = tid.split("::")
        mod = path[:-3].replace("/", ".")
        by_module.setdefault(mod, []).append((cls, meth, tid))

    suite = unittest.TestSuite()
    names = {}
    for mod, items in by_module.items():
        try:
            m = importlib.import_module(mod)
        except BaseException:  # noqa: BLE001
            failing.update(t for _, _, t in items)
            continue
        for cls, meth, tid in items:
            try:
                case = getattr(m, cls)(meth)
            except BaseException:  # noqa: BLE001
                failing.add(tid)
                continue
            names[id(case)] = tid
            suite.addTest(case)

    class Result(unittest.TestResult):
        def _mark(self, test):
            tid = names.get(id(test))
            if tid is None and hasattr(test, "test_case"):  # subtest
                tid = names.get(id(test.test_case))
            if tid is None:
                # class or module fixture failure: every test of that class
                desc = str(test)
                for case_id, t in names.items():
                    if t.split("::")[1] in desc:
                        failing.add(t)
                if failfast:
                    self.stop()
                return
            failing.add(tid)
            if failfast:
                self.stop()

        def addError(self, test, err):
            self._mark(test)

        def addFailure(self, test, err):
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test)

        def addUnexpectedSuccess(self, test):
            self._mark(test)

    out, err = sys.stdout, sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        suite.run(Result())
    finally:
        sys.stdout, sys.stderr = out, err
    out.write(json.dumps(sorted(failing)))
    out.flush()


if __name__ == "__main__":
    main(sys.argv[1:])
