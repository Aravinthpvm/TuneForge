# 1. Build the frontend React app
FROM node:22-bookworm-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend ./
RUN npm run build

# 2. Main python app runtime
FROM ghcr.io/zhaarey/apple-music-downloader:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install python3 pip, venv, tini and cleanup
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3-pip \
      python3-venv \
      tini \
      curl \
      && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create a virtual environment and install requirements
COPY requirements.txt ./
RUN python3 -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend files
COPY manage.py ./
COPY tuneforge ./tuneforge
COPY api ./api

# Copy static frontend assets built in the first stage
COPY --from=frontend-build /app/tuneforge/static ./tuneforge/static

# Run django checks and static collection
RUN python3 manage.py collectstatic --no-input

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "python3 manage.py migrate && python3 manage.py runserver 0.0.0.0:8000"]
