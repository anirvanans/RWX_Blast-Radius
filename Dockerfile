FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# pristine sqlglot at eb11aec (sources, tests, fixtures): patches are applied to it
COPY tree /app/tree
COPY selector.py spans.py local.py runner.py /app/
COPY maps /app/maps
RUN python -m compileall -q /app/spans.py && chmod -R a+rX /app

CMD ["python", "-S", "/app/selector.py"]
