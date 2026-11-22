"""Evaluation only: make random small edits to sqlglot, find what really fails,
and check the selector against it. Nothing here is used at run time.

    python mutate_eval.py SQLGLOT_ROOT TESTS_JSON OUT_DIR N SEED
"""
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

ROOT, TESTS_JSON, OUT, N, SEED = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
HEAVY = 3000  # ms: too slow to run for every edit
rng = random.Random(SEED)

SWAPS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" > ", " >= "), (r" <= ", " < "),
    (r" >= ", " > "), (r" and ", " or "), (r" or ", " and "), (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"), (r" is not ", " is "), (r" is None", " is not None"),
    (r"\+ 1\b", "+ 2"), (r"- 1\b", "- 2"), (r"\bnot ", ""), (r"\[0\]", "[1]"), (r"\[-1\]", "[0]"),
    (r'"([A-Z_]{3,})"', r'"\1X"'), (r"\breturn ([a-z_]+)$", r"return None"),
]

files = []
for d, _, fs in os.walk(os.path.join(ROOT, "sqlglot")):
    if "__pycache__" in d:
        continue
    for f in fs:
        if f.endswith(".py"):
            files.append(os.path.relpath(os.path.join(d, f), ROOT))
files.sort()


FIXTURES = []
for d, _, fs in os.walk(os.path.join(ROOT, "tests", "fixtures")):
    for f in fs:
        if f.endswith(".sql"):
            FIXTURES.append(os.path.relpath(os.path.join(d, f), ROOT))
TESTFILES = []
for d, _, fs in os.walk(os.path.join(ROOT, "tests")):
    if "fixtures" in d or "__pycache__" in d:
        continue
    for f in fs:
        if f.startswith("test_") and f.endswith(".py"):
            TESTFILES.append(os.path.relpath(os.path.join(d, f), ROOT))


def as_patch(p, old_t, new_t):
    diff = "".join(difflib.unified_diff(old_t.splitlines(True), new_t.splitlines(True), "a/" + p, "b/" + p, n=3))
    return "diff --git a/%s b/%s\n%s" % (p, p, diff)


def other_kind():
    kind = rng.choice(["del", "ins_return", "fixture", "fixture_comment", "testedit", "swap"])
    if kind == "swap":
        return None
    if kind in ("fixture", "fixture_comment"):
        p = rng.choice(FIXTURES)
        lines = open(os.path.join(ROOT, p), encoding="utf-8").read().split("\n")
        idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#")]
        if not idx:
            return None
        i = rng.choice(idx)
        nl = lines[:]
        if kind == "fixture":
            m = list(re.finditer(r"[a-z]", nl[i]))
            if not m:
                return None
            j = rng.choice(m).start()
            nl[i] = nl[i][:j] + ("x" if nl[i][j] != "x" else "y") + nl[i][j + 1:]
        else:
            nl.insert(i, "-- a note")
        old_t, new_t = "\n".join(lines), "\n".join(nl)
        return p, new_t, as_patch(p, old_t, new_t)
    if kind == "testedit":
        p = rng.choice(TESTFILES)
        lines = open(os.path.join(ROOT, p), encoding="utf-8").read().split("\n")
        idx = [i for i, l in enumerate(lines) if re.search(r'"[A-Za-z ]{3,}', l) and l.startswith("        ")]
        if not idx:
            return None
        i = rng.choice(idx)
        nl = lines[:]
        nl[i] = re.sub(r'"([A-Za-z])', r'"Z\1', nl[i], count=1)
        old_t, new_t = "\n".join(lines), "\n".join(nl)
        return p, new_t, as_patch(p, old_t, new_t)
    p = rng.choice(files)
    src = open(os.path.join(ROOT, p), encoding="utf-8").read()
    lines = src.split("\n")
    import ast as _ast
    tree = _ast.parse(src)
    funcs = [n for n in _ast.walk(tree) if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]
    if not funcs:
        return None
    f = rng.choice(funcs)
    nl = lines[:]
    if kind == "ins_return":
        first = f.body[0]
        if isinstance(first, _ast.Expr) and isinstance(getattr(first, "value", None), _ast.Constant) and len(f.body) > 1:
            first = f.body[1]
        indent = lines[first.lineno - 1][: len(lines[first.lineno - 1]) - len(lines[first.lineno - 1].lstrip())]
        nl.insert(first.lineno - 1, indent + "return None")
    else:
        simple = [st for st in _ast.walk(f) if isinstance(st, (_ast.Assign, _ast.Expr, _ast.AugAssign)) and st.lineno == st.end_lineno]
        if not simple:
            return None
        st = rng.choice(simple)
        del nl[st.lineno - 1]
    new_t = "\n".join(nl)
    try:
        compile(new_t, p, "exec")
    except SyntaxError:
        return None
    return p, new_t, as_patch(p, src, new_t)


def transforms_kind():
    p = rng.choice([f for f in files if f.startswith("sqlglot/generators/") or f == "sqlglot/generator.py"])
    lines = open(os.path.join(ROOT, p), encoding="utf-8").read().split("\n")
    idx = [i for i, l in enumerate(lines) if re.match(r"\s+exp\.\w+: ", l)]
    if not idx:
        return None
    i = rng.choice(idx)
    nl = lines[:]
    if re.search(r'"[A-Za-z_]+"', nl[i]) and rng.random() < 0.6:
        nl[i] = re.sub(r'"([A-Za-z_]+)"', r'"\1X"', nl[i], count=1)
    else:
        m = re.match(r"(\s+exp\.)(\w+)(: .*)", nl[i])
        others = [re.match(r"\s+exp\.(\w+): ", l).group(1) for l in lines if re.match(r"\s+exp\.\w+: ", l)]
        nl[i] = m.group(1) + rng.choice(others) + m.group(3) if rng.random() < 0.5 else ""
    old_t, new_t = "\n".join(lines), "\n".join(nl)
    if old_t == new_t:
        return None
    try:
        compile(new_t, p, "exec")
    except SyntaxError:
        return None
    return p, new_t, as_patch(p, old_t, new_t)


def red_kind():
    import ast as _ast
    kind = rng.choice(["default", "decorator", "override", "import", "delmethod", "helpers", "multi"])
    if kind == "helpers":
        p = "tests/helpers.py"
        lines = open(os.path.join(ROOT, p), encoding="utf-8").read().split("\n")
        idx = [i for i, l in enumerate(lines) if re.search(r'(==|!=|"|\b\d+\b)', l) and l.startswith("    ")]
        if not idx:
            return None
        i = rng.choice(idx)
        nl = lines[:]
        new = re.sub(r"==", "!=", nl[i], count=1)
        if new == nl[i]:
            new = re.sub(r'"([^"]*)"', r'"\1Z"', nl[i], count=1)
        nl[i] = new
        old_t, new_t = "\n".join(lines), "\n".join(nl)
        if old_t == new_t:
            return None
        return p, new_t, as_patch(p, old_t, new_t)
    if kind == "multi":
        a = mutate_simple()
        fx = "tests/fixtures/optimizer/redteam_new.sql"
        add = "SELECT 1;\nSELECT 1;\n"
        patch = a[2] + "diff --git a/%s b/%s\nnew file mode 100644\n--- /dev/null\n+++ b/%s\n@@ -0,0 +1,2 @@\n+SELECT 1;\n+SELECT 1;\n" % (fx, fx, fx)
        return a[0], a[1], patch
    p = rng.choice(files)
    src = open(os.path.join(ROOT, p), encoding="utf-8").read()
    lines = src.split("\n")
    tree = _ast.parse(src)
    nl = lines[:]
    if kind == "default":
        fs = [f for f in _ast.walk(tree) if isinstance(f, _ast.FunctionDef) and any(isinstance(d, _ast.Constant) for d in f.args.defaults + [x for x in f.args.kw_defaults if x])]
        if not fs:
            return None
        f = rng.choice(fs)
        d = rng.choice([d for d in f.args.defaults + [x for x in f.args.kw_defaults if x] if isinstance(d, _ast.Constant)])
        if d.lineno != d.end_lineno:
            return None
        v = d.value
        rep = {True: "False", False: "True", None: "0"}.get(v, repr(v + 1) if isinstance(v, int) else '"x"')
        ln = nl[d.lineno - 1]
        nl[d.lineno - 1] = ln[: d.col_offset] + rep + ln[d.end_col_offset:]
    elif kind == "decorator":
        ds = [(f, d) for f in _ast.walk(tree) if isinstance(f, (_ast.FunctionDef, _ast.ClassDef)) for d in f.decorator_list]
        if not ds:
            return None
        f, d = rng.choice(ds)
        if d.lineno != d.end_lineno:
            return None
        del nl[d.lineno - 1]
    elif kind == "override":
        if not (p.startswith("sqlglot/generators/") or p.startswith("sqlglot/parsers/")):
            return None
        cs = [c for c in tree.body if isinstance(c, _ast.ClassDef) and c.body]
        if not cs:
            return None
        c = rng.choice(cs)
        name = rng.choice(["cast_sql", "alias_sql", "column_sql", "table_sql", "select_sql", "literal_sql", "_parse_primary", "_parse_column", "_parse_table"])
        if name.endswith("_sql") != p.startswith("sqlglot/generators/"):
            return None
        body = c.body[-1]
        ind = " " * (c.col_offset + 4)
        nl.insert(body.end_lineno, "")
        nl.insert(body.end_lineno + 1, ind + "def %s(self, *args, **kwargs):" % name)
        nl.insert(body.end_lineno + 2, ind + "    return super().%s(*args, **kwargs) if False else None" % name)
    elif kind == "import":
        im = [n for n in tree.body if isinstance(n, _ast.ImportFrom) and n.lineno == n.end_lineno and n.module and n.module.startswith("sqlglot")]
        if not im:
            return None
        del nl[rng.choice(im).lineno - 1]
    elif kind == "delmethod":
        ms = [(c, m) for c in _ast.walk(tree) if isinstance(c, _ast.ClassDef) for m in c.body if isinstance(m, _ast.FunctionDef) and not m.name.startswith("__")]
        if not ms:
            return None
        c, m = rng.choice(ms)
        start = min([m.lineno] + [d.lineno for d in m.decorator_list])
        del nl[start - 1 : m.end_lineno]
    new_t = "\n".join(nl)
    if new_t == src:
        return None
    try:
        compile(new_t, p, "exec")
    except SyntaxError:
        return None
    return p, new_t, as_patch(p, src, new_t)


def mutate_simple():
    while True:
        r = None
        p = rng.choice(files)
        lines = open(os.path.join(ROOT, p), encoding="utf-8").read().split("\n")
        cands = [(i, pat, rep) for i, ln in enumerate(lines) if ln.strip() and not ln.strip().startswith("#") for pat, rep in SWAPS if re.search(pat, ln)]
        if not cands:
            continue
        i, pat, rep = rng.choice(cands)
        nl = lines[:]
        nl[i] = re.sub(pat, rep, nl[i], count=1)
        old_t, new_t = "\n".join(lines), "\n".join(nl)
        try:
            compile(new_t, p, "exec")
        except SyntaxError:
            continue
        if old_t != new_t:
            return p, new_t, as_patch(p, old_t, new_t)


def mutate():
    if os.environ.get("RED"):
        while True:
            r = red_kind()
            if r:
                return r
    if os.environ.get("TRANSFORMS"):
        while True:
            r = transforms_kind()
            if r:
                return r
    if os.environ.get("MIX"):
        while True:
            r = other_kind()
            if r:
                return r
            if rng.random() < 0.3:
                break
    while True:
        p = rng.choice(files)
        lines = open(os.path.join(ROOT, p), encoding="utf-8").read().split("\n")
        cands = []
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s or s.startswith("#") or s.startswith('"""'):
                continue
            for pat, rep in SWAPS:
                if re.search(pat, ln):
                    cands.append((i, pat, rep))
        if not cands:
            continue
        i, pat, rep = rng.choice(cands)
        new = re.sub(pat, rep, lines[i], count=1)
        if new == lines[i]:
            continue
        nl = lines[:]
        nl[i] = new
        old_t, new_t = "\n".join(lines), "\n".join(nl)
        try:
            compile(new_t, p, "exec")
        except SyntaxError:
            continue
        diff = "".join(
            difflib.unified_diff(
                old_t.splitlines(True), new_t.splitlines(True), "a/" + p, "b/" + p, n=3
            )
        )
        return p, new_t, "diff --git a/%s b/%s\n%s" % (p, p, diff)


tests = json.load(open(TESTS_JSON))["tests"]
NUM = {t["id"]: t["number"] for t in tests}
heavy = [t["id"] for t in tests if t["seconds"] * 1000 > HEAVY]

os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
shutil.copy(TESTS_JSON, os.path.join(OUT, "tests.json"))
jobs = []
for k in range(N):
    p, new_t, patch = mutate()
    cid = "m%02d%s" % (k, SEED)
    with open(os.path.join(OUT, "commits", cid + ".patch"), "w") as f:
        f.write(patch)
    jobs.append((cid, p, new_t))


def truth(job):
    cid, p, new_t = job
    w = os.path.join("/tmp/mt", cid)
    if os.path.exists(w):
        shutil.rmtree(w)
    os.makedirs(w)
    for d in ("sqlglot", "tests"):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(w, d), ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(w, p), "w", encoding="utf-8") as f:
        f.write(new_t)
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x" if False else "-q",
            "--junitxml", os.path.join(w, "r.xml"), "tests"]
    for h in heavy:
        args += ["--deselect", h]
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=w)
    subprocess.run(args, cwd=w, env=env, capture_output=True, timeout=900)
    failing = set()
    try:
        root = ET.parse(os.path.join(w, "r.xml")).getroot()
    except Exception:
        return cid, None
    for tc in root.iter("testcase"):
        if any(ch.tag in ("failure", "error") for ch in tc):
            cls = tc.get("classname", "")
            name = re.split(r"[ \[]", tc.get("name", ""))[0]
            parts = cls.split(".")
            tid = "/".join(parts[:-1]) + ".py::" + parts[-1] + "::" + name
            if tid in NUM:
                failing.add(NUM[tid])
            else:
                # collection error of a whole file
                fpath = "/".join(parts) + ".py"
                failing.update(n for i, n in NUM.items() if i.startswith(fpath + "::"))
    shutil.rmtree(w, ignore_errors=True)
    return cid, sorted(failing)


with ThreadPoolExecutor(2) as ex:
    res = dict(ex.map(truth, jobs))
answers = {"commits": {cid: {"failing": res[cid] or [], "kind": "mutant"} for cid, _, _ in jobs}}
with open(os.path.join(OUT, "answers.json"), "w") as f:
    json.dump(answers, f)
print(json.dumps({c: len(v or []) for c, v in res.items()}))
