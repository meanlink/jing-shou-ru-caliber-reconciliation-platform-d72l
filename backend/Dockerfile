FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ELIM_DB_PATH=/data/elimination.db \
    HOST=0.0.0.0 \
    PORT=8000
# 零 pip 依赖，标准库即可运行
COPY . .
RUN mkdir -p /data
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
CMD ["python", "-I", "run_server.py"]
