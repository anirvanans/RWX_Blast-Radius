"""Evaluation only: for commits where slow tests were skipped, run those slow
tests on the patched tree and report any that fail."""
import os, re, shutil, subprocess, sys
TREE, DATA = sys.argv[1], sys.argv[2]
env = dict(os.environ, BR_DATA=DATA, BR_TREE=TREE)
p = subprocess.run([sys.executable, "selector.py"], env=env, capture_output=True, text=True)
sys.path.insert(0, ".")
import selector
for line in p.stderr.splitlines():
    if not line.startswith("skipped-slow"):
        continue
    _, cid, *ids = line.split()
    text = open(os.path.join(DATA, "commits", cid + ".patch")).read()
    sel = selector.Selector(selector.Maps())
    sel.select(text)
    w = "/tmp/gc/" + cid
    shutil.rmtree(w, ignore_errors=True)
    shutil.copytree(TREE, w, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    for path, t in sel.edits.items():
        open(os.path.join(w, path), "w").write(t)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *ids], cwd=w,
                       env=dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=w), capture_output=True, text=True)
    print(cid, "slow tests:", len(ids), "->", r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:])
