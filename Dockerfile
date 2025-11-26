# Multi-stage build for optimized image size
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies in a virtual environment
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Final stage
FROM python:3.12-slim

WORKDIR /app

# Create non-root user for security
RUN useradd -m -u 1000 appuser

# Copy installed packages from builder
COPY --from=builder /root/.local /home/appuser/.local

# Copy application code (read-only)
COPY main.py ./
COPY config/ ./config/
COPY core/ ./core/
COPY artwork/ ./artwork/
COPY library/ ./library/
COPY routers/ ./routers/
COPY services/ ./services/

# Create data directory for writable files (database, logs)
RUN mkdir -p /app/data /app/logs && \
    chown -R appuser:appuser /app/data /app/logs

# Copy database to data directory with proper ownership
COPY --chown=appuser:appuser library.db /app/data/library.db

# Switch to non-root user
USER appuser

# Set environment variable for database path
ENV LIBRARY_DB_PATH=/app/data/library.db

# Add local bin to PATH
ENV PATH=/home/appuser/.local/bin:$PATH

# Railway sets PORT env var
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')"

# Run with uvicorn
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
