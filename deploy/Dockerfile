# Hermix hub — container alternative to the VPS script.
# Build from the REPO ROOT:  docker build -f deploy/Dockerfile -t hermix-hub .
# Run:  docker run -d --name hermix -p 8787:8787 -v hermix-data:/data hermix-hub
FROM python:3.12-slim

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .

ENV HERMIX_DB=/data/hermix.db
VOLUME /data
EXPOSE 8787

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
