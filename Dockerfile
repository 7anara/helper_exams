FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY req.txt .
RUN pip install --no-cache-dir -r req.txt

COPY app ./app

CMD ["python", "app/main.py"]