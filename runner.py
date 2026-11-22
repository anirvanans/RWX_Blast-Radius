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


def main(ids):
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
                return
            failing.add(tid)

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
