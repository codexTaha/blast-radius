# Blast Radius Production Container
# Pinned official Python 3.13-slim image digest
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

WORKDIR /app

# Copy production selector engine, library modules, and runtime artifacts
COPY selector.py /app/selector.py
COPY src /app/src
COPY artifacts /app/artifacts

# Enforce world-readable permissions for user nobody (UID 65534) and precompile marshal index
RUN python3 -c "import json, marshal; open('/app/artifacts/runtime_index.marshal', 'wb').write(marshal.dumps(json.load(open('/app/artifacts/runtime_index.json'))))" && chmod -R 755 /app

# Unbuffered stdout/stderr
ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/selector.py"]
