FROM python:3.11-slim

# Install system dependencies Chrome needs
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    wget \
    unzip \
    xvfb \
    libglib2.0-0 \
    libnss3 \
    libgconf-2-4 \
    libxss1 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libcups2 \
    libasound2 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Chrome path environment variables
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV CHROME_USER_DATA_DIR=/tmp/chrome-profile

# Create Chrome user data directory
RUN mkdir -p /tmp/chrome-profile

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scrape_commission.py .

# Start your script
CMD ["python", "scrape_commission.py"]
