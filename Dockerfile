FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Fail closed: without ENV=development the app will not start unless DATABASE_URL and SECRET_KEY are
# set, so a forgotten variable cannot leave the well-known development secret in use.
ENV ENV=production
WORKDIR /app

# tesseract reads the NERC tariff schedules, which are PDFs without a text layer
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY alembic ./alembic
COPY config ./config

RUN useradd --create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "africasignal.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
