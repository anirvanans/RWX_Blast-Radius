"""Apply a patch on top of the pristine sqlglot tree and run a few tests there.

The patched tree is a folder of symlinks in /tmp: only the folders that hold a
changed file are real, and their own __pycache__ is linked back so unchanged
modules load from their compiled files.
"""
import json
import os
import resource
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.environ.get("BR_TREE", "/opt/sqlglot")
RUNNER = os.path.join(HERE, "runner.py")


def make_overlay(root, files):
    """files: {relative path: new text, or None to delete}."""
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    real = {""}
    for p in files:
        parts = p.split("/")[:-1]
        for i in range(len(parts)):
            real.add("/".join(parts[: i + 1]))

    def fill(rel):
        src = os.path.join(TREE, rel) if rel else TREE
        dst = os.path.join(root, rel) if rel else root
        os.makedirs(dst, exist_ok=True)
        try:
            names = os.listdir(src)
        except FileNotFoundError:
            names = []
        for n in names:
            r = (rel + "/" + n) if rel else n
            if r in real or r in files:
                continue
            os.symlink(os.path.join(src, n), os.path.join(dst, n))

    for rel in sorted(real, key=len):
        fill(rel)
    for p, text in files.items():
        dst = os.path.join(root, p)
        if text is None:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text)
    return root


def run_tests(root, ids, timeout):
    """Run test ids in the patched tree. Returns a set of failing ids, or None if unsure."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp",
        "SKIP_INTEGRATION": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    try:
        p = subprocess.run(
            [sys.executable, "-S" if False else "-E", RUNNER, *ids],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if p.returncode != 0:
        return None
    try:
        return set(json.loads(p.stdout.strip().splitlines()[-1]))
    except Exception:  # noqa: BLE001
        return None


def cpu_children():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime
