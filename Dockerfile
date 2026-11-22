FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# the environment the tests were labeled in, to run a few of them here
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# pristine sqlglot at eb11aec (sources, tests, fixtures), compiled once
COPY tree /app/tree
RUN python -m compileall -q /app/tree/sqlglot /app/tree/tests

COPY selector.py spans.py local.py runner.py /app/
COPY maps /app/maps
RUN python -m compileall -q /app/spans.py /app/local.py /app/runner.py && chmod -R a+rX /app

CMD ["python", "-S", "/app/selector.py"]
