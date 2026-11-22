"""Measure the fixed CPU cost of running one test of each class here (imports, setUpClass)."""
import json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
TREE, TESTS_JSON, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runner.py")
tests = json.load(open(TESTS_JSON))["tests"]
best = {}
for t in tests:
    c = t["id"].rsplit("::", 1)[0]
    if c not in best or t["seconds"] < best[c]["seconds"]:
        best[c] = t
env = {"PATH": os.environ["PATH"], "HOME": "/tmp", "SKIP_INTEGRATION": "0", "PYTHONDONTWRITEBYTECODE": "1"}

def cpu(ids):
    p = subprocess.Popen([sys.executable, "-E", RUNNER, *ids], cwd=TREE, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = p.stdout.read()
    _, status, ru = os.wait4(p.pid, 0)
    p.returncode = status
    return ru.ru_utime + ru.ru_stime, out

base = min(cpu([])[0] for _ in range(3))
def one(c):
    t = best[c]
    v, out = cpu([t["id"]])
    return c, max(0, round((v - base) * 1000) - round(t["seconds"] * 1000)), out.decode()[-200:]
with ThreadPoolExecutor(2) as ex:
    res = list(ex.map(one, sorted(best)))
setup = {c: v for c, v, _ in res}
json.dump({"base": round(base * 1000), "class": setup}, open(OUT, "w"))
for c, v, o in sorted(res, key=lambda r: -r[1])[:15]:
    print(v, c, o)
print("base", base)
