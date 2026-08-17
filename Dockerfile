FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libcups2 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install chromium
COPY . .
CMD uvicorn app:app --host 0.0.0.0 --port $PORT
