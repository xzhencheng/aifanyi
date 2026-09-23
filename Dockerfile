FROM node:24-alpine AS chat
WORKDIR /build
COPY apps/chat/package*.json ./
RUN npm ci --no-audit --no-fund
COPY apps/chat/ ./
RUN npm run build

FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml alembic.ini ./
COPY migrations/ migrations/
COPY src/ src/
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home app && mkdir -p data && chown app:app data
COPY --from=chat /build/dist/ apps/chat/dist/
USER 10001
EXPOSE 8000
CMD ["uvicorn", "aifanyi.api:app", "--host", "0.0.0.0", "--port", "8000"]
