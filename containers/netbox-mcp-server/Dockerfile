# Stage 1: Builder
FROM python:3.12-slim AS builder

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local
COPY --from=builder /app/requirements.txt .

# Ensure the local bin is in PATH
ENV PATH=/root/.local/bin:$PATH

# Copy application code
COPY . .

# Run the application
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8002"]

