FROM python:3.11-slim

# Install dependencies
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*
# Install SQLite3 CLI for debugging
RUN apt-get update && apt-get install -y sqlite3 && rm -rf /var/lib/apt/lists/*


# Create app structure with proper permissions
RUN mkdir -p /app/data && \
    chown -R 99:100 /app

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py .
COPY templates/ /app/templates/
COPY static/ ./static/

# Set Unraid user/group (nobody:users)
USER 99:100

CMD ["python", "app.py"]

