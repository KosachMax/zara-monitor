FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .
COPY zara_monitor ./zara_monitor

# -u для unbuffered output — логи сразу видны в docker logs
CMD ["python", "-u", "monitor.py"]
