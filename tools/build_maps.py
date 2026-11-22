"""Turn the recordings from collect.py into the maps the selector reads.

    python build_maps.py SQLGLOT_ROOT TESTS_JSON OUT_DIR PREFIX [PREFIX ...]

Every map is built from the pinned sqlglot commit only: which tests run which
function, which tests open which fixture, which dialect classes and modules a
test uses. Nothing here looks at a patch.
"""
import ast
import hashlib
import json
import os
import re
import sys
import zlib
from collections import defaultdict

import coverage

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import spans as S  # noqa: E402

ROOT, TESTS_JSON, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
PREFIXES = sys.argv[4:]

tests = json.load(open(TESTS_JSON))["tests"]
NUM = {t["id"]: t["number"] for t in tests}
BY_FILE = defaultdict(int)
BY_CLASS = defaultdict(int)
for t in tests:
    BY_FILE[t["file"]] |= 1 << t["number"]
    BY_CLASS[t["id"].rsplit("::", 1)[0]] |= 1 << t["number"]
ALL = 0
for t in tests:
    ALL |= 1 << t["number"]


def ctx_tests(ctx):
    """Bitset of tests a coverage/recording context stands for, and its kind."""
    kind, _, rest = ctx.partition("|")
    rest = rest.split("#", 1)[0]
    if kind == "call":
        n = NUM.get(rest)
        return (1 << n if n is not None else 0), "test"
    if kind in ("setup", "teardown"):
        return BY_CLASS.get(rest.rsplit("::", 1)[0], 0), "test"
    if kind == "collect":
        return BY_FILE.get(rest, 0), "collect"
    return 0, "other"


def rel(p):
    return os.path.relpath(p, ROOT)


def py_files():
    out = []
    for base in ("sqlglot", "tests"):
        for d, dirs, files in os.walk(os.path.join(ROOT, base)):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", "fixtures")]
            for f in files:
                if f.endswith(".py"):
                    out.append(rel(os.path.join(d, f)))
    return sorted(out)


def module_name(path):
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def exec_lines(src, p):
    from coverage.python import PythonParser

    try:
        pp = PythonParser(text=src, filename=p)
        pp.parse_source()
        return set(pp.statements)
    except Exception:
        return set()


files = py_files()
info = {}
for p in files:
    src = open(os.path.join(ROOT, p), encoding="utf-8").read()
    tree = ast.parse(src)
    sp = S.function_spans(tree)
    info[p] = {
        "tree": tree,
        "spans": sp,
        "stmts": S.statements(tree),
        "nlines": src.count("\n") + 1,
        "ast": hashlib.sha1(ast.dump(tree).encode()).hexdigest(),
        "strings": S.string_ranges(tree),
        "docs": S.doc_ranges(tree),
        "astnd": hashlib.sha1(ast.dump(S.strip_docstrings(ast.parse(src))).encode()).hexdigest(),
        "istmts": S.inner_statements(tree),
        "dicts": S.dict_literals(tree),
        "exec": exec_lines(src, p),
    }


def innermost_index(p):
    """line -> index into spans (innermost function), as a list."""
    sp = info[p]["spans"]
    arr = [-1] * (info[p]["nlines"] + 2)
    # wider spans first, narrower overwrite
    for i in sorted(range(len(sp)), key=lambda i: -(sp[i][1] - sp[i][0])):
        s, e = sp[i][0], sp[i][1]
        for ln in range(s, min(e, len(arr) - 1) + 1):
            arr[ln] = i
    return arr


inner = {p: innermost_index(p) for p in files}
SPLIT = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
}
SPLIT_NUM = {NUM[t]: t for t in SPLIT if t in NUM}
subl = {p: defaultdict(lambda: defaultdict(set)) for p in files}  # line -> test -> units ("*" = all)


def split_units(ctx):
    """[(test number, unit or "*")] that a context stands for among the split tests."""
    kind, _, rest = ctx.partition("|")
    out = []
    if kind == "call":
        nodeid, _, k = rest.partition("#")
        n = NUM.get(nodeid)
        if n in SPLIT_NUM:
            out.append((n, int(k) if k else "*"))
        return out
    bits, _ = ctx_tests(ctx)
    for n in SPLIT_NUM:
        if bits >> n & 1:
            out.append((n, "*"))
    return out
line_bits = {p: defaultdict(int) for p in files}
func_bits = {p: [0] * len(info[p]["spans"]) for p in files}
file_bits = defaultdict(int)  # any line executed during a test phase
imp_bits = defaultdict(int)  # module imported during a test (or its file's collection)
dial_bits = defaultdict(int)  # dialect classes from this module used by a test
fix_bits = defaultdict(int)
cls_bits = defaultdict(int)  # "module:Qual.Name" -> tests that used that class or a subclass

SPLIT_PREFIX = os.environ.get("SPLIT_PREFIX")
for prefix in PREFIXES:
    data = coverage.CoverageData(prefix + ".coverage")
    data.read()
    ctx_cache = {}
    if SPLIT_PREFIX and prefix != SPLIT_PREFIX:
        # split tests are taken only from the run that recorded their units
        for t in SPLIT:
            ctx_cache["call|" + t] = (0, "other")
    for f in data.measured_files():
        p = rel(f)
        if p not in info:
            continue
        arr = inner[p]
        for ln, ctxs in data.contexts_by_lineno(f).items():
            fi = arr[ln] if ln < len(arr) else -1
            for c in ctxs:
                if c not in ctx_cache:
                    ctx_cache[c] = ctx_tests(c)
                bits, kind = ctx_cache[c]
                if not bits:
                    continue
                for n, k in split_units(c):
                    subl[p][ln][n].add(k)
                if kind == "test":
                    file_bits[p] |= bits
                    if fi >= 0:
                        func_bits[p][fi] |= bits
                        line_bits[p][ln] |= bits
                elif kind == "collect" and fi >= 0 and not info[p]["spans"][fi][3]:
                    # function bodies run while a test file is imported:
                    # if they break, the whole file fails to import
                    func_bits[p][fi] |= bits
                    line_bits[p][ln] |= bits
    rec = json.load(open(prefix + ".json"))
    for ctx, mods in rec["dialects"].items():
        bits, kind = ctx_tests(ctx)
        for m in mods:
            dial_bits[m.replace(".", "/") + ".py"] |= bits
    for ctx, mods in rec["imported"].items():
        bits, kind = ctx_tests(ctx)
        for m in mods:
            path = m.replace(".", "/")
            path = path + "/__init__.py" if os.path.isdir(os.path.join(ROOT, path)) else path + ".py"
            imp_bits[path] |= bits
    for ctx, keys in rec.get("classes", {}).items():
        bits, kind = ctx_tests(ctx)
        if kind != "test":
            continue
        for k in keys:
            cls_bits[k] |= bits
    for ctx, paths in rec["opened"].items():
        bits, kind = ctx_tests(ctx)
        for q in paths:
            fix_bits[q] |= bits

FAMILIES = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")


def is_family(p):
    return p.startswith(FAMILIES) and not p.endswith("__init__.py")


def path_of(mod):
    q = mod.replace(".", "/")
    if os.path.isdir(os.path.join(ROOT, q)):
        return q + "/__init__.py"
    return q + ".py"


importers = defaultdict(set)  # module path -> paths that import it
for p in files:
    if not p.startswith("sqlglot/"):
        continue
    pkg = module_name(p) if p.endswith("__init__.py") else module_name(p).rsplit(".", 1)[0]
    for n in ast.walk(info[p]["tree"]):
        if isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = pkg.split(".")
                parts = parts[: len(parts) - (n.level - 1)]
                base = ".".join(parts + ([base] if base else []))
            cands = [base] + [base + "." + a.name for a in n.names]
        elif isinstance(n, ast.Import):
            cands = [a.name for a in n.names]
        else:
            continue
        for c in cands:
            q = path_of(c)
            if q in info and q != p:
                importers[q].add(p)
changed = True
while changed:
    changed = False
    for q in files:
        if not is_family(q):
            continue
        b = dial_bits.get(q, 0)
        for imp in importers.get(q, ()):
            if is_family(imp):
                b |= dial_bits.get(imp, 0)
        if b != dial_bits.get(q, 0):
            dial_bits[q] = b
            changed = True

unit_cpu_all = {}
for prefix in PREFIXES:
    rec = json.load(open(prefix + ".json"))
    for t, d in rec.get("unit_cpu", {}).items():
        unit_cpu_all[t] = d

# ---- dict key readers -------------------------------------------------------
dk_readers = defaultdict(int)  # (owner, key) -> tests
dk_wrapped, dk_own, dk_mro = set(), {}, {}
for prefix in PREFIXES:
    rec = json.load(open(prefix + ".json"))
    if "dict_keys" not in rec:
        continue
    dk_wrapped |= set(rec["dict_wrapped"])
    dk_own.update(rec["dict_own"])
    dk_mro.update(rec["class_mro"])
    for ctx, pairs in rec["dict_keys"].items():
        bits, kind = ctx_tests(ctx)
        if not bits:
            continue
        for owner, key in pairs:
            dk_readers[(owner, key)] |= bits


META_FUNCS = {"__new__", "__init_subclass__", "__set_name__", "__class_getitem__", "__prepare__"}


def hx(b):
    return format(b, "x")


def dict_safety():
    """Upper-case attributes whose dicts are only read key by key at run time:
    no metaclass or class-creation use other than copying entries."""
    bad, copiers = set(), defaultdict(set)
    for p in files:
        if not p.startswith("sqlglot/"):
            continue
        tree = info[p]["tree"]
        parents = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parents[c] = n

        def enclosing(n):
            x = parents.get(n)
            while x is not None:
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    return x
                x = parents.get(x)
            return None

        def owner_class(n):
            x = parents.get(n)
            names = []
            while x is not None:
                if isinstance(x, ast.ClassDef):
                    names.append(x.name)
                x = parents.get(x)
            return ".".join(reversed(names))

        def is_copy(n):
            p_ = parents.get(n)
            if isinstance(p_, ast.Dict) and any(k is None and v is n for k, v in zip(p_.keys, p_.values)):
                return True
            if isinstance(p_, ast.Attribute) and p_.attr in ("copy", "items"):
                if p_.attr == "copy":
                    return True
                c = parents.get(p_)
                g = parents.get(c)
                return isinstance(c, ast.Call) and isinstance(g, ast.comprehension) and isinstance(parents.get(g), ast.DictComp)
            if isinstance(p_, ast.Call) and isinstance(p_.func, ast.Name) and p_.func.id == "dict" and n in p_.args:
                return True
            return False

        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                nm = n.attr
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                nm = n.id
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                nm = n.value
            else:
                continue
            if not nm.isupper():
                continue
            fn = enclosing(n)
            if fn is None:
                if isinstance(n, ast.Constant):
                    continue
                if is_copy(n):
                    oc = owner_class(n)
                    if oc:
                        copiers[nm].add(module_name(p) + ":" + oc)
                else:
                    bad.add(nm)
            elif isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cls = parents.get(fn)
                meta = isinstance(cls, ast.ClassDef) and any(
                    isinstance(b, ast.Name) and b.id == "type" for b in cls.bases
                )
                if meta or fn.name in META_FUNCS:
                    bad.add(nm)
    return bad, copiers


dk_bad, dk_copiers = dict_safety()
subs = defaultdict(list)
for ck, mro in dk_mro.items():
    for b in mro:
        subs[b].append(ck)
lit_keys = {}
for p in files:
    for a, b, q, at, ents, dyn in info[p]["dicts"]:
        lit_keys[module_name(p) + ":" + q + "|" + at] = sorted({e[0] for e in ents})
# TRANSFORMS: the metaclass only pops given keys, and the dispatch copy is tracked
safe_attrs = sorted({o.split("|")[1] for o in dk_wrapped} - (dk_bad - {"TRANSFORMS"}))
rd = defaultdict(dict)
table, tpos = [], {}
for (owner, key), b in dk_readers.items():
    if owner.split("|")[1] not in safe_attrs:
        continue
    h = hx(b)
    if h not in tpos:
        tpos[h] = len(table)
        table.append(h)
    rd[owner][key] = tpos[h]
dicts_out = {
    "readers": rd,
    "B": table,
    "safe": safe_attrs,
    "wrapped": sorted(dk_wrapped),
    "own": dk_own,
    "subs": {k: sorted(set(v)) for k, v in subs.items()},
    "copiers": {k: sorted(v) for k, v in dk_copiers.items()},
    "lit": lit_keys,
}

# test modules imported by other test files (static), and test files themselves
test_mod_importers = defaultdict(int)
for p in files:
    if not p.startswith("tests/"):
        continue
    for n in ast.walk(info[p]["tree"]):
        mods = []
        if isinstance(n, ast.ImportFrom) and n.module:
            mods.append(n.module)
        elif isinstance(n, ast.Import):
            mods += [a.name for a in n.names]
        for m in mods:
            if m.startswith("tests"):
                q = m.replace(".", "/")
                q = q + "/__init__.py" if os.path.isdir(os.path.join(ROOT, q)) else q + ".py"
                test_mod_importers[q] |= BY_FILE.get(p, 0)
for p in files:
    if p.startswith("tests/"):
        imp_bits[p] |= BY_FILE.get(p, 0) | test_mod_importers.get(p, 0)


# static import closure: modules a file loads when it is imported
def top_imports(p):
    tree = info[p]["tree"]
    pkg = module_name(p) if p.endswith("__init__.py") else module_name(p).rsplit(".", 1)[0]
    out = set()

    def visit(node):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(ch, ast.ImportFrom):
                base = ch.module or ""
                if ch.level:
                    parts = pkg.split(".")
                    parts = parts[: len(parts) - (ch.level - 1)]
                    base = ".".join(parts + ([base] if base else []))
                cands = [base]
                for a in ch.names:
                    cands += [base + "." + a.name, base + "." + a.name.lower()]
            elif isinstance(ch, ast.Import):
                cands = [a.name for a in ch.names]
            else:
                visit(ch)
                continue
            for c in cands:
                parts = c.split(".")
                for k in range(1, len(parts) + 1):  # importing a.b.c imports a and a.b
                    q = path_of(".".join(parts[:k]))
                    if q in info:
                        out.add(q)

    visit(tree)
    out.discard(p)
    return out


direct = {p: top_imports(p) for p in files}
closure = {}


def close(p):
    if p in closure:
        return closure[p]
    seen, stack = set(), [p]
    while stack:
        q = stack.pop()
        for r in direct.get(q, ()):
            if r not in seen:
                seen.add(r)
                stack.append(r)
    closure[p] = seen
    return seen


for tf in files:
    if not tf.startswith("tests/") or tf not in BY_FILE:
        continue
    b = BY_FILE[tf]
    for q in close(tf):
        imp_bits[q] |= b

# names assigned at module or class level -> tests running functions that read them
assigned = set()
for p in files:
    if not p.startswith("sqlglot/"):
        continue

    def scan(body):
        for st in body:
            if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            assigned.add(n.id)
            elif isinstance(st, ast.ClassDef):
                scan(st.body)
            elif isinstance(st, (ast.If, ast.Try)):
                scan(st.body)
                scan(getattr(st, "orelse", []))

    scan(info[p]["tree"].body)

META_FUNCS = {"__new__", "__init_subclass__", "__set_name__", "__class_getitem__", "__prepare__"}


def loads_in(node, skip_functions):
    out = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if skip_functions and n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            out.add(n.value)
        stack.extend(ast.iter_child_nodes(n))
    return out


unsafe = set()
for p in files:
    if not p.startswith("sqlglot/"):
        continue
    tree = info[p]["tree"]
    # read while modules and classes are being built
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in st.decorator_list + st.args.defaults:
                unsafe |= loads_in(d, True)
            continue
        unsafe |= loads_in(st, True)
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            meta = any(isinstance(b, ast.Name) and b.id == "type" for b in n.bases)
            for st in n.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if meta or st.name in META_FUNCS:
                        unsafe |= loads_in(st, False)
                elif not isinstance(st, ast.ClassDef):
                    unsafe |= loads_in(st, True)

name_bits = defaultdict(int)
for p in files:
    for i, (s, e, q, is_lambda, node) in enumerate(info[p]["spans"]):
        b = func_bits[p][i]
        if not b:
            continue
        seen = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                seen.add(n.id)
            elif isinstance(n, ast.Attribute):
                seen.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                seen.add(n.value)
        for nm in seen & assigned:
            name_bits[nm] |= b


def hx(b):
    return format(b, "x")


def istmts_of(p):
    """[start, header_end, bitset or None]; None when coverage cannot see the statement."""
    out = []
    ex = info[p]["exec"]
    lb = line_bits[p]
    for s_, he in info[p]["istmts"]:
        lines = range(s_, he + 1)
        if not any(ln in ex for ln in lines):
            out.append([s_, he, None])
            continue
        b = 0
        for ln in lines:
            b |= lb.get(ln, 0)
        out.append([s_, he, hx(b)])
    return out


os.makedirs(os.path.join(OUT, "f"), exist_ok=True)
index = {}
for p in files:
    sp = info[p]["spans"]
    entry = {
        "ast": info[p]["ast"],
        "funcs": [[s, e, q, hx(func_bits[p][i])] for i, (s, e, q, _, _) in enumerate(sp)],
        "stmts": [list(x) for x in info[p]["stmts"]],
        "docs": info[p]["docs"],
        "dicts": [[a, b, q, at, [list(e) for e in ents], dyn] for a, b, q, at, ents, dyn in info[p]["dicts"]],
        "astnd": info[p]["astnd"],
        "istmts": istmts_of(p),
        "family": is_family(p),
        "subl": {
            str(ln): {str(n): ("*" if "*" in ks else format(sum(1 << k for k in ks), "x")) for n, ks in d.items()}
            for ln, d in subl[p].items()
        },
        "cls": {k.split(":", 1)[1]: hx(b) for k, b in cls_bits.items() if k.split(":", 1)[0] == module_name(p)},
        "file": hx(file_bits.get(p, 0)),
        "imp": hx(imp_bits.get(p, 0)),
        "dial": hx(dial_bits.get(p, 0)),
        "strings": info[p]["strings"],
        "module": module_name(p),
    }
    # one table of distinct bitsets per file; everything else refers to it
    table, pos = [], {}

    def ref(h):
        if h is None:
            return None
        if h not in pos:
            pos[h] = len(table)
            table.append(h)
        return pos[h]

    for f in entry["funcs"]:
        f[3] = ref(f[3])
    for st in entry["istmts"]:
        st[2] = ref(st[2])
    for k in ("file", "imp", "dial"):
        entry[k] = ref(entry[k])
    entry["cls"] = {q: ref(h) for q, h in entry["cls"].items()}
    entry["B"] = table
    key = p.replace("/", "__")
    with open(os.path.join(OUT, "f", key + ".z"), "wb") as f:
        f.write(zlib.compress(json.dumps(entry, separators=(",", ":")).encode(), 9))
    index[p] = key

fix_dirs = defaultdict(int)
for q, b in fix_bits.items():
    fix_dirs[os.path.dirname(q)] |= b
fixtures = sorted(
    rel(os.path.join(d, f))
    for d, _, fs in os.walk(os.path.join(ROOT, "tests", "fixtures"))
    for f in fs
)
with open(os.path.join(OUT, "index.json"), "w") as f:
    json.dump(
        {
            "ids": [t["id"] for t in tests],
            "files": index,
            "fix": {q: hx(b) for q, b in fix_bits.items()},
            "fixdir": {q: hx(b) for q, b in fix_dirs.items()},
            "fixtures": fixtures,
            "testfile": {p: hx(b) for p, b in BY_FILE.items()},
            "split": {str(n): t for n, t in SPLIT_NUM.items()},
            "unit_cpu": unit_cpu_all,
        },
        f,
        separators=(",", ":"),
    )
with open(os.path.join(OUT, "dicts.z"), "wb") as f:
    f.write(zlib.compress(json.dumps(dicts_out, separators=(",", ":")).encode(), 9))
print("dict attrs safe", len(dicts_out["safe"]), "owners", len(rd), file=sys.stderr)
with open(os.path.join(OUT, "names.z"), "wb") as f:
    f.write(zlib.compress(json.dumps({"bits": {k: hx(v) for k, v in name_bits.items()}, "unsafe": sorted(unsafe & set(name_bits))}, separators=(",", ":")).encode(), 9))
print("files", len(files), "names", len(name_bits), "fixtures opened", len(fix_bits), file=sys.stderr)
