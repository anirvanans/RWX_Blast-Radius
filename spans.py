"""Where code lives in a Python file: functions (with lambdas) and statements.

Shared by tools/build_maps.py (on the pinned sources) and selector.py (on the
patched file) so both name things the same way.
"""
import ast

SAFE_STMTS = (
    ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import, ast.ImportFrom,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Pass,
)


def function_spans(tree):
    """[(start, end, qualname, is_lambda, node)] for every def and lambda."""
    spans = []

    def walk(node, prefix, counter):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                q = prefix + ch.name
                spans.append((start, ch.end_lineno, q, False, ch))
                for d in ch.decorator_list:
                    walk(d, prefix, counter)
                walk_body(ch, q + ".<locals>.")
            elif isinstance(ch, ast.ClassDef):
                for b in ch.bases + ch.decorator_list + [k.value for k in ch.keywords]:
                    walk(b, prefix, counter)
                walk_body(ch, prefix + ch.name + ".")
            elif isinstance(ch, ast.Lambda):
                counter[0] += 1
                q = prefix + "<lambda%d>" % counter[0]
                spans.append((ch.lineno, ch.end_lineno, q, True, ch))
                walk(ch, q + ".", [0])
            else:
                walk(ch, prefix, counter)

    def walk_body(node, prefix):
        counter = [0]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for a in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                walk(a, prefix, counter)
            for st in node.body:
                walk(st, prefix, counter) if not isinstance(
                    st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
                ) else walk(_Wrap(st), prefix, counter)
        else:
            for st in node.body:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk(_Wrap(st), prefix, counter)
                else:
                    walk(st, prefix, counter)

    walk_body(tree, "")
    return spans


class _Wrap(ast.AST):
    """Lets walk() see a single statement as a child."""

    _fields = ("node",)

    def __init__(self, node):
        super().__init__()
        self.node = node


def bound_names(st):
    names = []
    if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = st.targets if isinstance(st, ast.Assign) else [st.target]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    names.append(n.id)
                elif isinstance(n, ast.Attribute):
                    names.append(n.attr)
    elif isinstance(st, (ast.Import, ast.ImportFrom)):
        for a in st.names:
            names.append((a.asname or a.name).split(".")[0])
    elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.append(st.name)
    else:
        for n in ast.walk(st):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.append(n.id)
    return names


def statements(tree):
    """[(start, end, scope, safe, names, owner)] for statements outside functions.

    scope is "module" or "class". A ClassDef is listed in the scope that holds
    it, with itself as owner; the statements of its body are listed with
    scope "class" and the class as owner.
    """
    out = []

    def visit(body, scope, owner):
        for st in body:
            start = st.lineno
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([st.lineno] + [d.lineno for d in st.decorator_list])
            safe = isinstance(st, SAFE_STMTS) or (
                isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
            )
            if isinstance(st, ast.ClassDef):
                q = (owner + "." if owner else "") + st.name
                out.append((start, st.end_lineno, scope, safe, bound_names(st), q))
                visit(st.body, "class", q)
                continue
            out.append((start, st.end_lineno, scope, safe, bound_names(st), owner))
            if isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                inner = []
                for f in ("body", "orelse", "finalbody"):
                    inner += getattr(st, f, []) or []
                for h in getattr(st, "handlers", []) or []:
                    inner += h.body
                visit(inner, scope, owner)

    visit(tree.body, "module", None)
    return out


def innermost(spans, line):
    best, width = -1, None
    for i, sp in enumerate(spans):
        s, e = sp[0], sp[1]
        if s <= line <= e and (width is None or e - s < width):
            best, width = i, e - s
    return best


def string_ranges(tree):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr) or (isinstance(n, ast.Constant) and isinstance(n.value, (str, bytes))):
            if n.end_lineno and n.end_lineno > n.lineno:
                out.append((n.lineno, n.end_lineno))
    return out


def in_ranges(ranges, line):
    for s, e in ranges:
        if s <= line <= e:
            return True
    return False


COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
if hasattr(ast, "TryStar"):
    COMPOUND = COMPOUND + (ast.TryStar,)


def inner_statements(tree):
    """[(start, header_end)] for statements inside function bodies.

    A compound statement stands for its header only (the test of an if, the
    iterator of a for); its body statements are listed on their own.
    """
    out = []

    def visit(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(st, COMPOUND) or (hasattr(ast, "Match") and isinstance(st, ast.Match)):
                first = None
                for f in ("body", "cases"):
                    b = getattr(st, f, None)
                    if b:
                        first = b[0]
                        break
                he = (first.lineno - 1) if first is not None else st.end_lineno
                out.append((st.lineno, max(st.lineno, he)))
                visit(getattr(st, "body", []) or [])
                visit(getattr(st, "orelse", []) or [])
                visit(getattr(st, "finalbody", []) or [])
                for h in getattr(st, "handlers", []) or []:
                    hb = h.body[0].lineno - 1 if h.body else h.end_lineno
                    out.append((h.lineno, max(h.lineno, hb)))
                    visit(h.body)
                for c in getattr(st, "cases", []) or []:
                    visit(c.body)
            else:
                out.append((st.lineno, st.end_lineno))

    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit(n.body)
    return out


def docstrings(tree):
    """Docstring nodes: the leading string of a module, class or function body."""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                out.append((n, b[0]))
    return out


def doc_ranges(tree):
    return [(d.lineno, d.end_lineno) for _, d in docstrings(tree)]


def strip_docstrings(tree):
    """Remove docstrings in place (a body left empty gets a pass)."""
    for owner, d in docstrings(tree):
        owner.body = owner.body[1:] or [ast.Pass()]
    return tree


def key_repr(node):
    """How a dict key written in source reads at run time (see collect.py _kr)."""
    if isinstance(node, ast.Constant):
        v = node.value
        return "s:" + v if isinstance(v, str) else "r:" + repr(v)
    parts = []
    n = node
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
    else:
        return None
    parts.reverse()
    last = parts[-1]
    if len(parts) >= 2 and parts[-2][:1].isupper() and last.isupper():
        return "e:%s.%s" % (parts[-2], last)
    if last[:1].isupper() and not last.isupper():
        return "c:" + last
    return None


def dict_literals(tree):
    """[(start, end, class qualname, attr, [(key, kstart, kend)], dynamic)] for
    upper-case dict attributes written as dict displays in class bodies."""
    out = []

    def visit(body, prefix):
        for st in body:
            if isinstance(st, ast.ClassDef):
                visit(st.body, prefix + st.name + ".")
                continue
            if not prefix:
                continue
            if isinstance(st, ast.Assign) and len(st.targets) == 1:
                tgt, val = st.targets[0], st.value
            elif isinstance(st, ast.AnnAssign) and st.value is not None:
                tgt, val = st.target, st.value
            else:
                continue
            if not (isinstance(tgt, ast.Name) and tgt.id.isupper() and isinstance(val, ast.Dict)):
                continue
            entries, dynamic = [], False
            for k, v in zip(val.keys, val.values):
                if k is None:
                    continue  # **base: copied entries belong to the base's literal
                r = key_repr(k)
                if r is None:
                    dynamic = True
                    continue
                entries.append((r, k.lineno, v.end_lineno))
            out.append((st.lineno, st.end_lineno, prefix[:-1], tgt.id, entries, dynamic))

    visit(tree.body, "")
    return out
