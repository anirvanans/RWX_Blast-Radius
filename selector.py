"""Blast Radius selector.

For each patch in /data/commits, pick the tests that can fail and split them
across workers. Everything heavy was worked out ahead of time from the pinned
sqlglot commit (see tools/); at run time this only reads maps and the patch.
Logs go to stderr.
"""
import ast
import hashlib
import json
import os
import re
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import spans as S  # noqa: E402
DATA = os.environ.get("BR_DATA", "/data")
MAPS = os.path.join(HERE, "maps")
TREE = os.environ.get("BR_TREE", os.path.join(HERE, "tree"))  # pristine sqlglot checkout with tests
SRC = TREE
LIGHT_MS = 1000  # tests at most this long may be run here
LOCAL_BUDGET_MS = 4000  # most recorded test time to run here for one commit
GAMBLE_SHARE = float(os.environ.get("BR_GAMBLE_SHARE", "0"))
SPLIT_SHARE = float(os.environ.get("BR_SPLIT_SHARE", "0.6"))
PLAIN_MARGIN_MS = int(os.environ.get("BR_PLAIN_MARGIN", "2500"))
WALL_BUDGET = 120
# classes whose set-up here costs far more than their recorded time (data loading)
SLOW_CLASSES = {"tests/test_executor.py::TestExecutor"}  # seconds of the batch's wall time to spend running tests

ALL = "ALL"
CONST = re.compile(r"^(?:[A-Z][A-Z0-9_]*|arg_types)$")

# Paths that cannot change what a test sees.
INERT_PREFIXES = ("docs/", "posts/", "benchmarks/", ".github/", "sqlglotrs/", "pdoc/", "sqlglot-integration-tests/")
INERT_SUFFIXES = (".md", ".rst", ".txt", ".png", ".svg", ".jpg", ".gif", ".html", ".css", ".rs", ".jinja2")
INERT_FILES = {
    "LICENSE", "Makefile", "CHANGELOG.md", "README.md", ".gitignore", ".gitmodules", ".gitpod.yml",
    ".pre-commit-config.yaml", "Cargo.lock", "MANIFEST.in", "setup.cfg", "sqlglot.png",
    "sqlglot-integration-tests",
}


def log(*a):
    print(*a, file=sys.stderr)


class Maps:
    def __init__(self):
        with open(os.path.join(MAPS, "index.json")) as f:
            self.index = json.load(f)
        self._files = {}
        self._names = None

    def file(self, path):
        if path not in self._files:
            key = self.index["files"].get(path)
            if key is None:
                self._files[path] = None
            else:
                with open(os.path.join(MAPS, "f", key + ".z"), "rb") as f:
                    e = json.loads(zlib.decompress(f.read()))
                B = e.pop("B")
                for fn in e["funcs"]:
                    fn[3] = B[fn[3]]
                for st in e["istmts"]:
                    st[2] = None if st[2] is None else B[st[2]]
                for k in ("file", "imp", "dial"):
                    e[k] = B[e[k]]
                e["cls"] = {q: B[i] for q, i in e["cls"].items()}
                self._files[path] = e
        return self._files[path]

    def dicts(self):
        if not hasattr(self, "_dicts"):
            try:
                with open(os.path.join(MAPS, "dicts.z"), "rb") as f:
                    d = json.loads(zlib.decompress(f.read()))
                d["safe_set"] = set(d["safe"])
                d["wrapped_set"] = set(d["wrapped"])
                d["lit"] = {k: set(v) for k, v in d["lit"].items()}
                self._dicts = d
            except Exception:  # noqa: BLE001
                self._dicts = None
        return self._dicts

    def names(self):
        if self._names is None:
            with open(os.path.join(MAPS, "names.z"), "rb") as f:
                d = json.loads(zlib.decompress(f.read()))
            self._names = d["bits"]
            self._unsafe = set(d["unsafe"])
        return self._names


def parse_patch(text):
    """Return a list of file diffs: dict(old, new, hunks, binary)."""
    diffs = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            cur = {"old": None, "new": None, "hunks": [], "binary": False}
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            cur["hdr_old"], cur["hdr_new"] = cur["old"], cur["new"]
            diffs.append(cur)
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        if ln.startswith("--- "):
            p = ln[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            i += 1
            continue
        if ln.startswith("+++ "):
            p = ln[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            i += 1
            continue
        if ln.startswith("new file mode"):
            cur["created"] = True
        elif ln.startswith("deleted file mode"):
            cur["deleted"] = True
        elif ln.startswith("rename from "):
            cur["old"] = ln[len("rename from "):]
        elif ln.startswith("rename to "):
            cur["new"] = ln[len("rename to "):]
        elif ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
            cur["binary"] = True
        elif ln.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
            if not m:
                cur["bad"] = True
                i += 1
                continue
            os_, ol, ns, nl = int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1)
            body = []
            i += 1
            co, cn = 0, 0
            while i < len(lines) and (co < ol or cn < nl or (i < len(lines) and lines[i].startswith("\\"))):
                b = lines[i]
                if b.startswith("\\"):
                    body.append(("\\", b))
                elif b.startswith("+"):
                    body.append(("+", b[1:]))
                    cn += 1
                elif b.startswith("-"):
                    body.append(("-", b[1:]))
                    co += 1
                elif b.startswith(" ") or b == "":
                    body.append((" ", b[1:]))
                    co += 1
                    cn += 1
                else:
                    break
                i += 1
            cur["hunks"].append((os_, ol, ns, nl, body))
            continue
        i += 1
    for d in diffs:
        if d.get("created"):
            d["old"] = None
        if d.get("deleted"):
            d["new"] = None
        if d["old"] is None and d["new"] is None:
            d["old"] = d.get("hdr_old")
            d["new"] = d.get("hdr_new")
    return diffs


def apply_hunks(old_text, hunks):
    """Apply hunks to old text. Returns (new_text, deleted_old_lines, added_new_lines)."""
    old = old_text.split("\n") if old_text else []
    trailing_nl = old_text.endswith("\n") if old_text else True
    if old and old[-1] == "" and trailing_nl:
        old = old[:-1]
    out = []
    pos = 0  # index into old (0-based)
    deleted, added, pairs = [], [], {}
    new_no_nl = False
    for os_, ol, ns, nl, body in hunks:
        start = os_ - 1 if ol > 0 else os_
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old[pos:start])
        pos = start
        last = None
        run_del, run_add = [], []

        def flush():
            if run_del and len(run_del) == len(run_add):
                for a, b in zip(run_del, run_add):
                    pairs[b] = a
            run_del.clear()
            run_add.clear()

        for kind, txt in body:
            if kind == " ":
                flush()
                out.append(old[pos] if pos < len(old) else txt)
                pos += 1
            elif kind == "-":
                if run_add:
                    flush()
                deleted.append(pos + 1)
                run_del.append(pos + 1)
                pos += 1
            elif kind == "+":
                out.append(txt)
                added.append(len(out))
                run_add.append(len(out))
            elif kind == "\\":
                if last == "+":
                    new_no_nl = True
                elif last == " ":
                    new_no_nl = True
            if kind != "\\":
                last = kind
        flush()
    out.extend(old[pos:])
    text = "\n".join(out)
    if not new_no_nl:
        text += "\n"
    return text, deleted, added, pairs


def load_src(path):
    p = os.path.join(SRC, path)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return f.read()


def trivial(line):
    s = line.strip()
    return s == "" or s.startswith("#")


class Selector:
    def __init__(self, maps):
        self.maps = maps
        self.units = []  # one bitset for every changed place
        self.edits = {}  # path -> new text (None: deleted), to rebuild the patched tree
        self.local_ok = True
        self.sub = {}  # split test (canonical number, as text) -> unit mask or "*"
        self.sub_all = False

    def add_sub(self, entry, a, b):
        subl = entry.get("subl") or {}
        for ln in range(a, b + 1):
            d = subl.get(str(ln))
            if not d:
                continue
            for n, v in d.items():
                if v == "*" or self.sub.get(n) == "*":
                    self.sub[n] = "*"
                else:
                    self.sub[n] = self.sub.get(n, 0) | int(v, 16)

    def unit(self, b):
        self.units.append(b)
        return b

    def all_tests(self):
        return ALL

    def py_change(self, d):
        """Tests for a change to a Python file under sqlglot/ or tests/."""
        m = self.maps
        old_path, new_path = d["old"], d["new"]
        path = old_path or new_path
        entry = m.file(path) if old_path else None
        is_test = path.startswith("tests/")

        if old_path and new_path and old_path != new_path:
            return ALL  # renamed module: imports of the old name break
        if new_path is None:
            # deleted module or test file
            self.local_ok = False
            if is_test:
                e = m.file(old_path)
                return int(e["imp"], 16) if e else ALL
            return ALL
        if old_path is None or entry is None:
            # new file: harmless unless something refers to it by name
            mod = new_path[:-3].replace("/", ".").removesuffix(".__init__")
            base = mod.rsplit(".", 1)[-1]
            if os.path.basename(new_path) in ("conftest.py", "__init__.py"):
                return ALL
            if self.referenced(base, mod):
                return ALL
            self.edits[new_path] = apply_hunks("", d["hunks"])[0]
            return 0

        old_text = load_src(path)
        if old_text is None:
            return ALL
        new_text, deleted, added, pairs = apply_hunks(old_text, d["hunks"])
        self.edits[path] = new_text
        try:
            tree = ast.parse(new_text)
        except SyntaxError:
            # the module cannot be imported: every test that imports it fails
            self.sub_all = True
            return self.unit(int(entry["imp"], 16) | int(entry["file"], 16) | int(entry["dial"], 16))
        if hashlib.sha1(ast.dump(tree).encode()).hexdigest() == entry["ast"]:
            return 0  # same code: only comments or layout changed
        if self.docstring_only(entry, old_text, new_text, deleted, added, tree):
            # nothing reads docstrings, except tests/test_typing.py, which
            # compares function code objects (a docstring is one of their constants)
            if path.startswith("sqlglot/typing/"):
                return self.unit(int(m.index["testfile"].get("tests/test_typing.py", "0"), 16))
            return 0

        funcs = entry["funcs"]
        stmts = entry["stmts"]
        old_strings = entry["strings"]
        bits = 0
        scopes = []  # (scope, safe, names) of statements changed outside functions
        old_lines = old_text.split("\n")
        for ln in deleted:
            txt = old_lines[ln - 1] if 0 < ln <= len(old_lines) else ""
            if trivial(txt) and not S.in_ranges(old_strings, ln):
                continue
            dk = self.dict_key_tests(path, entry.get("dicts") or [], ln)
            i = S.innermost(funcs, ln)
            if dk is not None:
                bits |= self.unit(dk)
                if i >= 0:
                    bits |= self.unit(self.in_function(entry, i, ln))
                continue
            if i >= 0:
                bits |= self.unit(self.in_function(entry, i, ln))
            else:
                j = S.innermost(stmts, ln)
                scopes.append(tuple(stmts[j][2:6]) if j >= 0 else ("module", False, [], None))
        nst = None
        ndl = None
        # functions removed or renamed: their callers by name break
        dset = set(deleted)
        removed = [f for f in funcs if f[0] in dset or (f[0] <= min(dset or [0]) and False)]
        if removed:
            alive = {sp[2] for sp in S.function_spans(tree)}
            for f in removed:
                if "<lambda" in f[2] or f[2] in alive:
                    continue
                q = f[2]
                if "." in q:
                    owner, name = q.rsplit(".", 1)
                    if "<locals>" in owner:
                        continue
                    scopes.append(("class", True, [name], owner))
                else:
                    scopes.append(("module", True, [q], None))
        if added:
            nsp = S.function_spans(tree)
            nstr = S.string_ranges(tree)
            by_q = {f[2]: i for i, f in enumerate(funcs)}
            new_lines = new_text.split("\n")
            for ln in added:
                txt = new_lines[ln - 1] if ln - 1 < len(new_lines) else ""
                if trivial(txt) and not S.in_ranges(nstr, ln):
                    continue
                if ndl is None:
                    ndl = S.dict_literals(tree)
                dk = self.dict_key_tests(path, ndl, ln)
                if dk is not None:
                    bits |= self.unit(dk)
                    i = S.innermost(nsp, ln)
                    if i >= 0 and nsp[i][3] and nsp[i][2] in by_q:
                        # a lambda value that existed before: what ran it
                        bits |= self.unit(int(funcs[by_q[nsp[i][2]]][3], 16))
                    continue
                i = S.innermost(nsp, ln)
                if i >= 0 and nsp[i][2] in by_q:
                    fi = by_q[nsp[i][2]]
                    o = pairs.get(ln)
                    if o is not None and S.innermost(funcs, o) == fi:
                        # a line replaced in place: what runs it is what ran the old line
                        bits |= self.unit(self.in_function(entry, fi, o))
                    else:
                        self.add_sub(entry, funcs[fi][0], funcs[fi][1])
                        bits |= self.unit(int(funcs[fi][3], 16))
                    continue
                if nst is None:
                    nst = S.statements(tree)
                if i >= 0:
                    # a new function or lambda: it matters through the
                    # statement that holds it
                    j = S.innermost(nst, nsp[i][0])
                else:
                    j = S.innermost(nst, ln)
                scopes.append(tuple(nst[j][2:6]) if j >= 0 else ("module", False, [], None))
        if scopes or is_test:
            self.sub_all = True
        if scopes:
            bits |= self.unit(self.scope_tests(entry, scopes, is_test))
            # a module-level name that is gone breaks every "from x import name"
            gone = {nm for sc, _, nms, _ in scopes if sc == "module" for nm in nms}
            if gone:
                if nst is None:
                    nst = S.statements(tree)
                now = {nm for st in nst if st[2] == "module" for nm in st[4]}
                if gone - now:
                    bits |= self.unit(int(entry["imp"], 16))
            if is_test and path.endswith("__init__.py"):
                # every test module under this package imports it
                pkg = os.path.dirname(path) + "/"
                for tf, h in m.index["testfile"].items():
                    if tf.startswith(pkg):
                        bits |= int(h, 16)
        return bits

    @staticmethod
    def docstring_only(entry, old_text, new_text, deleted, added, tree):
        """Are all changed lines inside docstrings, with the rest of the code identical?"""
        olds = entry.get("docs") or []
        if not olds and not deleted:
            return False
        ol = old_text.split("\n")
        for ln in deleted:
            if not S.in_ranges(olds, ln) and not trivial(ol[ln - 1] if 0 < ln <= len(ol) else ""):
                return False
        news = S.doc_ranges(tree)
        nl = new_text.split("\n")
        for ln in added:
            if not S.in_ranges(news, ln) and not trivial(nl[ln - 1] if 0 < ln <= len(nl) else ""):
                return False
        if not entry.get("astnd"):
            return False
        stripped = S.strip_docstrings(tree)
        return hashlib.sha1(ast.dump(stripped).encode()).hexdigest() == entry["astnd"]

    def dict_key_tests(self, path, lits, ln):
        """Tests for a changed entry of a tracked class dict, or None to fall back."""
        if os.environ.get("BR_DICTKEYS", "1") != "1" or not path.startswith("sqlglot/"):
            return None
        hit = None
        for lit in lits:
            if lit[0] <= ln <= lit[1]:
                hit = lit
        if hit is None:
            return None
        _, _, q, attr, ents, _ = hit
        key = None
        for k, ks, ke in ents:
            if ks <= ln <= ke:
                key = k
        if key is None:
            return None
        d = self.maps.dicts()
        if d is None or attr not in d["safe_set"]:
            return None
        mod = path[:-3].replace("/", ".").removesuffix(".__init__")
        ck = mod + ":" + q
        if ck + "|" + attr not in d["wrapped_set"]:
            return None
        targets = set(d["subs"].get(ck, [ck])) | set(d["copiers"].get(attr, []))
        B = d["B"]
        bits = 0
        for D in targets:
            o = D + "|" + attr
            if D != ck and key in d["lit"].get(o, ()):
                continue  # this class writes its own entry for the key
            if attr not in d["own"].get(D, ()):
                continue  # no dict of its own: reads go to a base's dict
            if o not in d["wrapped_set"]:
                return None
            r = d["readers"].get(o, {})
            if key in r:
                bits |= int(B[r[key]], 16)
            if "*" in r:
                bits |= int(B[r["*"]], 16)
        return bits

    def scope_tests(self, entry, scopes, is_test):
        """Tests for changes at class or module level."""
        m = self.maps
        dial = int(entry["dial"], 16)
        bits = 0
        broad = False
        for scope, safe, nms, owner in scopes:
            if not safe:
                bits |= int(entry["imp"], 16)
            use = 0
            q = owner
            while q and not use:  # the class itself, else the class holding it
                h = entry["cls"].get(q)
                use = int(h, 16) if h else 0
                q = q.rsplit(".", 1)[0] if "." in q else None
            if scope == "class" and use and not is_test:
                base = use | dial
                names = m.names()
                parts = 0
                for nm in nms:
                    h = names.get(nm)
                    if h and nm not in m._unsafe and CONST.match(nm):
                        parts |= base & int(h, 16)
                    else:
                        parts = base
                        break
                bits |= parts if nms else base
                continue
            broad = True
            bits |= use
            if scope == "module" and not entry.get("family"):
                names = m.names()
                for nm in nms:
                    h = names.get(nm)
                    if h:
                        bits |= int(h, 16)
        if broad:
            bits |= int(entry["file"], 16) | dial
            if is_test:
                bits |= int(entry["imp"], 16)
        return bits

    def in_function(self, entry, fi, ln):
        """Tests for an old line inside function fi: its statement if visible, else the function."""
        ist = entry["istmts"]
        j = S.innermost(ist, ln)
        f = entry["funcs"][fi]
        if j >= 0:
            s_, he, h = ist[j]
            if h is not None and f[0] <= s_ and he <= f[1]:
                self.add_sub(entry, s_, he)
                return int(h, 16)
        self.add_sub(entry, f[0], f[1])
        return int(f[3], 16)

    def referenced(self, base, mod):
        """Does any pristine source mention this new module?"""
        pat = re.compile(r"\b" + re.escape(base) + r"\b")
        for d, _, fs in os.walk(SRC):
            for f in fs:
                if f.endswith(".py"):
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        t = fh.read()
                    if mod in t or (pat.search(t) and ("import_module" in t or "__getattr__" in t)):
                        return True
        return False

    def fixture_change(self, d):
        self.sub_all = True
        b = self.fixture_tests(d)
        path = d["new"] or d["old"]
        if d["binary"] or d["old"] != d["new"]:
            self.local_ok = False
        else:
            orig = os.path.join(TREE, path)
            try:
                with open(orig, encoding="utf-8") as f:
                    self.edits[path] = apply_hunks(f.read(), d["hunks"])[0]
            except Exception:  # noqa: BLE001
                self.local_ok = False
        return self.unit(b)

    def fixture_tests(self, d):
        m = self.maps
        path = d["old"] or d["new"]
        fix = m.index["fix"]
        bits = 0
        for p in {d["old"], d["new"]}:
            if p and p in fix:
                bits |= int(fix[p], 16)
        if bits:
            return bits
        # never opened by name: fall back to tests that read that folder
        dirp = os.path.dirname(path)
        fd = m.index["fixdir"]
        while dirp and dirp != "tests":
            if dirp in fd:
                return int(fd[dirp], 16)
            dirp = os.path.dirname(dirp)
        if path in m.index["fixtures"]:
            return 0  # an existing file no test opens
        return 0

    def change(self, d):
        path = d["new"] or d["old"] or ""
        if not (path.startswith("sqlglot/") and path.endswith(".py")):
            self.sub_all = True
        if d.get("bad"):
            return ALL
        if path in INERT_FILES or path.startswith(INERT_PREFIXES):
            return 0
        if path.startswith("sqlglotc/"):
            # tests/test_sqlglotc.py runs sqlglotc/setup.py
            self.local_ok = False
            return int(self.maps.index["testfile"].get("tests/test_sqlglotc.py", "0"), 16) or ALL
        if path == "sqlglot/py.typed":
            return 0
        if path.startswith("tests/fixtures/"):
            return self.fixture_change(d)
        if d["binary"]:
            return ALL
        if path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
            if os.path.basename(path) == "conftest.py":
                return ALL
            return self.py_change(d)
        if path.endswith(INERT_SUFFIXES) and not path.startswith(("sqlglot/", "tests/")):
            return 0
        return ALL

    def select(self, patch_text):
        self.units, self.edits, self.local_ok = [], {}, True
        self.sub, self.sub_all = {}, False
        diffs = parse_patch(patch_text)
        if not diffs:
            return 0 if not patch_text.strip() else ALL
        bits = 0
        for d in diffs:
            try:
                b = self.change(d)
            except Exception as e:  # noqa: BLE001
                log("error", d.get("new") or d.get("old"), repr(e))
                b = ALL
            if b == ALL:
                return ALL
            bits |= b
        return bits


def pack(numbers, ms):
    """Choose worker count and split to minimise busiest load + 2 s per worker."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: -ms[n])
    best = None
    for k in range(1, min(8, len(order)) + 1):
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads))
            loads[j] += ms[n]
            bins[j].append(n)
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return [b for b in best[1] if b]


def _group(nums, id_of):
    """Tests grouped by their class."""
    g = {}
    for n in nums:
        g.setdefault(id_of[n].rsplit("::", 1)[0], []).append(n)
    return list(g.values())


def cost_ms(numbers, ms):
    if not numbers:
        return 0
    order = sorted(numbers, key=lambda n: -ms[n])
    best = None
    for k in range(1, min(8, len(order)) + 1):
        loads = [0] * k
        for n in order:
            j = loads.index(min(loads))
            loads[j] += ms[n]
        c = max(loads) + 2000 * k
        best = c if best is None or c < best else best
    return best


def main():
    import time
    from concurrent.futures import ThreadPoolExecutor

    t0 = time.time()
    with open(os.path.join(DATA, "tests.json")) as f:
        tj = json.load(f)
    tests = tj["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    maps = Maps()
    canon = maps.index["ids"]
    num_of = {t["id"]: t["number"] for t in tests}
    remap = [num_of.get(i) for i in canon]
    known = {n for n in remap if n is not None}
    unknown = [t["number"] for t in tests if t["number"] not in known]
    def runnable(n):
        return ms[n] <= LIGHT_MS and id_of[n].rsplit("::", 1)[0] not in SLOW_CLASSES

    light_mask = 0
    for i, n in enumerate(remap):
        if n is not None and runnable(n):
            light_mask |= 1 << i
    can_run = os.path.isdir(os.path.join(TREE, "tests")) and os.environ.get("BR_LOCAL", "0") == "1"
    try:
        with open(os.path.join(MAPS, "setup.json")) as f:
            setup = json.load(f)
    except Exception:  # noqa: BLE001
        setup = {"base": 100, "class": {}}

    def est_cpu(light):
        """Expected CPU (ms) to run these tests here: start-up, each class's set-up, the tests."""
        heads = sorted((setup["class"].get(id_of[n].rsplit("::", 1)[0], 300) for n in {
            min(g) for g in _group(light, id_of)}), reverse=True)
        total = setup["base"] + sum(ms[n] for n in light)
        if heads:
            total += heads[0] + sum(max(h - 150, 20) for h in heads[1:])
        return total

    sel = Selector(maps)
    cdir = os.path.join(DATA, "commits")
    plans = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[: -len(".patch")]
        units, edits, local_ok, sub = [], {}, False, None
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as f:
                text = f.read()
            bits = sel.select(text)
            units, edits, local_ok = sel.units, sel.edits, sel.local_ok
            sub = None if sel.sub_all else dict(sel.sub)
        except Exception as e:  # noqa: BLE001
            log("error", cid, repr(e))
            bits = ALL
        if bits == ALL:
            nums = [t["number"] for t in tests]
            local_ok = False
        else:
            nums = []
            i = 0
            b = bits
            while b:
                if b & 1 and i < len(remap) and remap[i] is not None:
                    nums.append(remap[i])
                b >>= 1
                i += 1
            if nums:
                nums += unknown
        plan = {"cid": cid, "nums": nums, "run": None, "gamble": False, "split": None}
        if can_run and local_ok and sub and nums and not unknown:
            # slow tests made of independent queries: run only the queries the edit reaches
            subset, drop = {}, []
            for cn, tid in maps.index.get("split", {}).items():
                n = num_of.get(tid)
                if n is None or n not in nums:
                    continue
                v = sub.get(cn)
                if v is None or v == "*":
                    continue
                ks = [k for k in range(v.bit_length()) if v >> k & 1]
                subset[tid] = ks
                drop.append(n)
            if subset:
                ucpu = maps.index.get("unit_cpu", {})
                est = setup["class"].get("tests/test_executor.py::TestExecutor", 3000) + setup["base"]
                for tid, ks in subset.items():
                    d = ucpu.get(tid, {})
                    tot = sum(d.values()) or 1.0
                    est += sum(ms[num_of[tid]] * d.get(str(k), tot / max(len(d), 1)) / tot for k in ks)
                rest = [n for n in nums if n not in drop]
                saving = cost_ms(nums, ms) - cost_ms(rest, ms)
                if est < SPLIT_SHARE * saving:
                    plan.update(split=subset, rest=rest, edits=edits)
        if can_run and local_ok and nums and not unknown:
            light = [n for n in nums if runnable(n)]
            heavy = [n for n in nums if not runnable(n)]
            covered = all((u & light_mask) for u in units if u)
            spend = sum(ms[n] for n in light)
            if light and spend <= LOCAL_BUDGET_MS:
                est = est_cpu(light)
                full = cost_ms(nums, ms)
                if heavy and covered and est < GAMBLE_SHARE * full:
                    plan.update(run=light, heavy=heavy, gamble=True, edits=edits)
                elif not heavy and est < full - PLAIN_MARGIN_MS:
                    plan.update(run=light, heavy=[], edits=edits)
        plans.append(plan)

    todo = [p for p in plans if p["run"] or p["split"]]
    # exclude classes whose set-up alone is costly
    for p in todo if False else []:
        pass
    if todo:
        import local

        def go(p):
            if time.time() - t0 > WALL_BUDGET:
                return p, None
            root = os.path.join("/tmp", "o", p["cid"])
            if p["split"]:
                try:
                    local.make_overlay(root, p["edits"])
                    res = local.run_tests(root, list(p["split"]), timeout=60, failfast=True, subset=p["split"])
                except Exception as e:  # noqa: BLE001
                    log("local error", p["cid"], repr(e))
                    res = None
                return p, res
            try:
                local.make_overlay(root, p["edits"])
                spend = sum(ms[n] for n in p["run"])
                order = sorted(p["run"], key=lambda n: ms[n])
                res = local.run_tests(root, [id_of[n] for n in order], timeout=10 + 4 * spend / 1000, failfast=True)
            except Exception as e:  # noqa: BLE001
                log("local error", p["cid"], repr(e))
                res = None
            return p, res

        with ThreadPoolExecutor(2) as ex:
            for p, res in ex.map(go, todo):
                if res is None:
                    continue
                if p["split"]:
                    if not res:
                        log("split-pass", p["cid"], {t: len(k) for t, k in p["split"].items()})
                        p["nums"] = p["rest"]
                    else:
                        log("split-fail", p["cid"])
                    continue
                failed = [n for n in p["run"] if id_of[n] in res]
                if not failed:
                    # every test that runs the edit passes here: nothing to run
                    if p["gamble"]:
                        log("skipped-slow", p["cid"], " ".join(id_of[n] for n in p["heavy"]))
                    p["nums"] = []
                log(p["cid"], "ran", len(p["run"]), "failed", len(failed), "est", est_cpu(p["run"]))

    out = []
    for p in plans:
        workers = pack(p["nums"], ms)
        out.append(json.dumps({"commit": p["cid"], "workers": workers}, separators=(",", ":")))
        log(p["cid"], len(p["nums"]), "tests")
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
