# Blast Radius selector

For each patch to sqlglot (commit `eb11aec`), print which tests can fail and how to split them across workers.

## How it decides

Everything heavy is worked out ahead of time from the pinned sqlglot commit, never from a patch:

- `tools/collect.py` runs sqlglot's tests under coverage with one context per test phase, records the fixture files each test opens, the dialect classes it uses and the modules it imports. Process pools run in-process (with pickling) so their code is measured.
- `tools/build_maps.py` turns that into `maps/`: tests per function and lambda, per file, per dialect module, per fixture, per module-level name.

At run time `selector.py` reads each patch and:

1. Python file whose AST is unchanged (comments, layout): nothing to run.
2. Python file that no longer parses: every test that imports it.
3. Lines inside a function or lambda: the tests that run that function.
4. Class or module level lines: tests that run any of the file, tests that use its dialect classes, and tests reading the module-level names it binds.
5. SQL fixtures: the tests that open that file.
6. Anything unrecognised: every test.

When the picked tests include slow ones and every changed place is also run by fast tests, or when only fast tests are picked and running them here is cheaper than a worker, `local.py` rebuilds the patched tree in `/tmp` (symlinks to `tree/`, the pristine sqlglot checkout) and `runner.py` runs the fast tests there, stopping at the first failure. If none fails, nothing is printed for that commit; otherwise the full pick stands. Its CPU time is charged, so this only happens when the expected saving is clearly larger.

Tests are then packed onto 1–8 workers, longest first, choosing the count with the lowest busiest-worker time plus 2 s per worker.

## Rebuilding the maps

```sh
git clone https://github.com/tobymao/sqlglot && cd sqlglot && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
pip install -r requirements.lock coverage
SKIP_INTEGRATION=0 python ../tools/collect.py /tmp/A tests/test_parser.py tests/test_executor.py
SKIP_INTEGRATION=0 python ../tools/collect.py /tmp/B <all other test files>
python ../tools/build_maps.py . tests.json ../maps /tmp/A /tmp/B
```

`tree/` holds sqlglot at that commit (MIT licensed: sources, tests, fixtures) so patches can be applied, parsed and tested without the network. `requirements.lock` is the kit's labeling environment.
