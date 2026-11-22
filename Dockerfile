# The scorer builds this on linux/amd64 and runs it with no network. The base
# digest is the kit's pinned image, the one every commit was labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The labeling environment's exact packages, from PyPI.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit, from its own GitHub repository, checked against
# a committed manifest hash, with bytecode compiled ahead of time.
COPY build/fetch_sqlglot.py /app/build/fetch_sqlglot.py
RUN python /app/build/fetch_sqlglot.py /opt/sqlglot \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests

# Maps built offline from the pinned commit (tools/covmap/), and the selector.
COPY maps /app/maps
COPY br /app/br
COPY selector.py /app/selector.py
RUN python -m compileall -q /app && chmod -R a+rX /app /opt/sqlglot

ENV SKIP_INTEGRATION=0
CMD ["python", "/app/selector.py"]
