# Build the React app, then serve it from the FastAPI process so the published app
# is a single origin: one hostname, one auth cookie, no CORS.

FROM node:22-alpine AS web
WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY --from=web /build/dist ./web/dist
# The presenter sheet ships alongside the UI so it is reachable at /presenter-sheet.html,
# behind the same sign-in. The backend serves any real file in web/dist.
COPY docs/presenter-sheet.html ./web/dist/presenter-sheet.html

# Container Apps sets PORT; default matches the local dev port.
ENV PORT=8000
EXPOSE 8000

# Run as a non-root user.
RUN useradd --create-home --uid 1001 app && chown -R app:app /app
USER app

CMD ["sh", "-c", "uvicorn app:app --app-dir backend --host 0.0.0.0 --port ${PORT}"]
