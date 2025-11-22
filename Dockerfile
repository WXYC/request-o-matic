FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./
COPY artwork/ ./artwork/
COPY library/ ./library/

# Copy the pre-built SQLite database
COPY library.db .

# Railway sets PORT env var
ENV PORT=8000

EXPOSE 8000

# Run with uvicorn
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
