FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV LIVIQO_HEADLESS=true
ENV LIVIQO_PROFILE_DIR=/data/dubizzle_profile
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
