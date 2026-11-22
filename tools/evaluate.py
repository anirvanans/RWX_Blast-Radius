"""Run the selector on a sample-style folder and score it, with per-commit detail."""
import json, os, subprocess, sys, resource, time
d = sys.argv[1]
env = dict(os.environ, BR_DATA=d)
b = resource.getrusage(resource.RUSAGE_CHILDREN)
t = time.time()
p = subprocess.run([sys.executable, "selector.py"], capture_output=True, text=True, env=env)
a = resource.getrusage(resource.RUSAGE_CHILDREN)
cpu = a.ru_utime - b.ru_utime + a.ru_stime - b.ru_stime
if p.returncode:
    print(p.stderr); sys.exit(1)
tests = json.load(open(os.path.join(d, "tests.json")))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
ans = json.load(open(os.path.join(d, "answers.json")))["commits"]
tot = 0; misses = 0; rows = []
for line in p.stdout.splitlines():
    o = json.loads(line)
    ws = [w for w in o["workers"] if w]
    cost = (max(sum(ms[n] for n in w) for w in ws) + 2000 * len(ws)) if ws else 0
    sel = {n for w in ws for n in w}
    fail = set(ans[o["commit"]]["failing"])
    miss = bool(fail - sel)
    misses += miss
    tot += cost + (300000 if miss else 0)
    rows.append((o["commit"], ans[o["commit"]]["kind"], len(fail), len(sel), cost / 1000, "MISS " + str(sorted(fail - sel)) if miss else ""))
for r in sorted(rows, key=lambda r: -r[4]):
    print("%-5s %-8s fail=%-3d sel=%-5d %7.3f %s" % r)
print("simulated %.3f s  misses %d  cpu %.3f s  wall %.2f s" % (tot / 1000, misses, cpu, time.time() - t))
