FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY AgroCast/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY AgroCast/ ./

ENV AGROCAST_STATE_DIR=/app/data
ENV AGROCAST_WORLD_DIR=/app/world
ENV PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /app/data

EXPOSE 8501

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
  CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8501/health/live', timeout=8); sys.exit(0 if r.status==200 else 1)"

CMD ["uvicorn", "agrocast.serve.product:create_app", "--factory", "--host", "0.0.0.0", "--port", "8501"]
