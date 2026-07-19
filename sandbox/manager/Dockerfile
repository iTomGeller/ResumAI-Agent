FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    "fastapi==0.115.6" "uvicorn==0.32.1" "docker==7.1.0" "pydantic==2.10.4"

COPY sandbox/manager/app.py /app/app.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8070

HEALTHCHECK --interval=20s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8070/health', timeout=4)" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8070"]
