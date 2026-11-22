FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

WORKDIR /app
COPY selector.py spans.py /app/
COPY maps /app/maps
COPY src /app/src
RUN python -m compileall -q /app/spans.py && chmod -R a+rX /app

CMD ["python", "-S", "/app/selector.py"]
