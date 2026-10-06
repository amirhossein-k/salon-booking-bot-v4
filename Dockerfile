FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home salon
WORKDIR /app
COPY core.py bot.py jalali.py dashboard.py dashboard.html dashboard-mobile.html /app/
RUN mkdir /app/data && chown salon:salon /app/data
USER salon
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
