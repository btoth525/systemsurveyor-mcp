FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && apt-get update >/dev/null 2>&1 && apt-get install -y --no-install-recommends fonts-liberation >/dev/null 2>&1; rm -rf /var/lib/apt/lists/*
COPY ssapi.py server.py quote.py plan.py reports.py ./
ENV SS_TOKEN_FILE=/data/tokens.json DATA_DIR=/data MCP_TRANSPORT=http PORT=8797 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8797
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8797/health',timeout=4).status==200 else 1)"
CMD ["python","server.py"]
