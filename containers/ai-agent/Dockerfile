# Build context: containers/ (so that _shared/ is reachable alongside ai-agent/)

# Stage 1: Builder
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build-time dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY ai-agent/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local
COPY --from=builder /app/requirements.txt .

# Ensure the local bin is in PATH
ENV PATH=/root/.local/bin:$PATH

# Copy shared module (imported as `_shared.github_mcp`)
COPY _shared/ ./_shared/
# Copy application code
COPY ai-agent/ .

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
