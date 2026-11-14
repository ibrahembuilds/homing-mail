FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CAMPAIGNS_DATA=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY wsgi.py .
# Bytecode built once here; the running containers never write it.
RUN python -m compileall -q app wsgi.py

RUN useradd --system --uid 1000 --home-dir /data --shell /usr/sbin/nologin campaigns \
 && mkdir -p /data && chown campaigns /data
USER campaigns
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/campaigns/health', timeout=3)"

# One process with threads: the agent holds a request open while the model works.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "330", \
     "--graceful-timeout", "30", "--keep-alive", "5", "--worker-tmp-dir", "/dev/shm", \
     "--access-logfile", "-", "wsgi:app"]
