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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import spans as S  # noqa: E402
DATA = os.environ.get("BR_DATA", "/data")
MAPS = os.path.join(HERE, "maps")
SRC = os.path.join(HERE, "src")  # pristine sqlglot sources (.py only)

ALL = "ALL"
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

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
                with open(os.path.join(MAPS, "f", key + ".json")) as f:
                    e = json.load(f)
                self._files[path] = e
        return self._files[path]

    def names(self):
        if self._names is None:
            with open(os.path.join(MAPS, "names.json")) as f:
                self._names = json.load(f)
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
    deleted, added = [], []
    new_no_nl = False
    for os_, ol, ns, nl, body in hunks:
        start = os_ - 1 if ol > 0 else os_
        if start < pos:
            raise ValueError("overlapping hunks")
        out.extend(old[pos:start])
        pos = start
        last = None
        for kind, txt in body:
            if kind == " ":
                out.append(old[pos] if pos < len(old) else txt)
                pos += 1
            elif kind == "-":
                deleted.append(pos + 1)
                pos += 1
            elif kind == "+":
                out.append(txt)
                added.append(len(out))
            elif kind == "\\":
                if last == "+":
                    new_no_nl = True
                elif last == " ":
                    new_no_nl = True
            if kind != "\\":
                last = kind
    out.extend(old[pos:])
    text = "\n".join(out)
    if not new_no_nl:
        text += "\n"
    return text, deleted, added


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
            return 0

        old_text = load_src(path)
        if old_text is None:
            return ALL
        new_text, deleted, added = apply_hunks(old_text, d["hunks"])
        try:
            tree = ast.parse(new_text)
        except SyntaxError:
            # the module cannot be imported: every test that imports it fails
            return int(entry["imp"], 16) | int(entry["file"], 16) | int(entry["dial"], 16)
        if hashlib.sha1(ast.dump(tree).encode()).hexdigest() == entry["ast"]:
            return 0  # same code: only comments or layout changed

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
            i = S.innermost(funcs, ln)
            if i >= 0:
                bits |= int(funcs[i][3], 16)
            else:
                j = S.innermost(stmts, ln)
                scopes.append(tuple(stmts[j][2:5]) if j >= 0 else ("module", False, []))
        if added:
            nsp = S.function_spans(tree)
            nst = None
            nstr = S.string_ranges(tree)
            by_q = {f[2]: i for i, f in enumerate(funcs)}
            new_lines = new_text.split("\n")
            for ln in added:
                txt = new_lines[ln - 1] if ln - 1 < len(new_lines) else ""
                if trivial(txt) and not S.in_ranges(nstr, ln):
                    continue
                i = S.innermost(nsp, ln)
                if i >= 0 and nsp[i][2] in by_q:
                    bits |= int(funcs[by_q[nsp[i][2]]][3], 16)
                    continue
                if nst is None:
                    nst = S.statements(tree)
                if i >= 0:
                    # a new function or lambda: it matters through the
                    # statement that holds it
                    j = S.innermost(nst, nsp[i][0])
                else:
                    j = S.innermost(nst, ln)
                scopes.append(tuple(nst[j][2:5]) if j >= 0 else ("module", False, []))
        if scopes:
            bits |= int(entry["file"], 16) | int(entry["dial"], 16)
            if is_test or any(not safe for _, safe, _ in scopes):
                bits |= int(entry["imp"], 16)
            if not entry.get("family"):
                names = m.names()
                for scope, safe, nms in scopes:
                    if scope != "module":
                        continue
                    for nm in nms:
                        h = names.get(nm)
                        if h:
                            bits |= int(h, 16)
        return bits

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
        if d.get("bad"):
            return ALL
        if path in INERT_FILES or path.startswith(INERT_PREFIXES):
            return 0
        if path.startswith("sqlglotc/"):
            # tests/test_sqlglotc.py runs sqlglotc/setup.py
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


def main():
    with open(os.path.join(DATA, "tests.json")) as f:
        tj = json.load(f)
    tests = tj["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    maps = Maps()
    canon = maps.index["ids"]
    num_of = {t["id"]: t["number"] for t in tests}
    remap = [num_of.get(i) for i in canon]
    known = {n for n in remap if n is not None}
    unknown = [t["number"] for t in tests if t["number"] not in known]
    sel = Selector(maps)
    cdir = os.path.join(DATA, "commits")
    out = []
    for name in sorted(os.listdir(cdir)):
        if not name.endswith(".patch"):
            continue
        cid = name[: -len(".patch")]
        try:
            with open(os.path.join(cdir, name), encoding="utf-8", errors="replace") as f:
                text = f.read()
            bits = sel.select(text)
        except Exception as e:  # noqa: BLE001
            log("error", cid, repr(e))
            bits = ALL
        if bits == ALL:
            nums = [t["number"] for t in tests]
        else:
            nums = []
            i = 0
            while bits:
                if bits & 1 and i < len(remap) and remap[i] is not None:
                    nums.append(remap[i])
                bits >>= 1
                i += 1
            if nums:
                nums += unknown
        workers = pack(nums, ms)
        out.append(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        log(cid, len(nums), "tests")
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
