# 轻量生产镜像：应用只读 tar 元数据，不需要系统级编译工具。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 非 root 运行；/srv/data 是命名卷挂载点（存放初始化好的持久卷），
# 提前建好后 chown，Docker 会用镜像里的属主初始化新命名卷。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/data/volumes \
    && chown -R appuser:appuser /srv/data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
