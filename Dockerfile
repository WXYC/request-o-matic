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

# Copy application code
COPY --chown=appuser:appuser main.py ./
COPY --chown=appuser:appuser config/ ./config/
COPY --chown=appuser:appuser core/ ./core/
COPY --chown=appuser:appuser artwork/ ./artwork/
COPY --chown=appuser:appuser library/ ./library/
COPY --chown=appuser:appuser routers/ ./routers/
COPY --chown=appuser:appuser services/ ./services/
COPY --chown=appuser:appuser library.db ./

# Ensure appuser owns the /app directory and database has correct permissions
RUN chown -R appuser:appuser /app && \
    chmod 755 /app && \
    chmod 644 /app/library.db

# Switch to non-root user
USER appuser

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
