FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

ENTRYPOINT ["python", "main.py"]
CMD ["test-proxy", "--retries", "12", "--retry-delay", "5"]
