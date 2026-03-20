# Stage 1: Builder
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies if needed (none specified, but multi-stage is good practice for clean layering)
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Install runtime system utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    net-tools \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /root/.local /root/.local
COPY --from=builder /app/requirements.txt .

# Ensure the local bin is in PATH
ENV PATH=/root/.local/bin:$PATH

# Copy application code
COPY . .

# Ensure /var/log is accessible
RUN chmod 755 /var/log

# Run the application
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]

