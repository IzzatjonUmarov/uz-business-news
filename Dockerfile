FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY config ./config
ENV PYTHONUNBUFFERED=1
# Проверка источников каждые 15 минут + курсы ЦБ раз в день
CMD ["python", "-m", "bot", "run", "--loop", "900", "--with-rates"]
