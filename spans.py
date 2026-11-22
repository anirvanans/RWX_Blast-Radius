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
    """[(start, end, scope, safe, names)] for statements outside functions.

    scope is "module" or "class". A ClassDef is listed in the scope that holds
    it; the statements of its body are listed with scope "class".
    """
    out = []

    def visit(body, scope):
        for st in body:
            start = st.lineno
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([st.lineno] + [d.lineno for d in st.decorator_list])
            safe = isinstance(st, SAFE_STMTS) or (
                isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
            )
            out.append((start, st.end_lineno, scope, safe, bound_names(st)))
            if isinstance(st, ast.ClassDef):
                visit(st.body, "class")
            elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                inner = []
                for f in ("body", "orelse", "finalbody"):
                    inner += getattr(st, f, []) or []
                for h in getattr(st, "handlers", []) or []:
                    inner += h.body
                visit(inner, scope)

    visit(tree.body, "module")
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
