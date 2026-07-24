# Hermies hub — container alternative to the VPS script.
# Build from the REPO ROOT:  docker build -f deploy/Dockerfile -t hermies-hub .
# Run:  docker run -d --name hermies -p 8787:8787 -v hermies-data:/data hermies-hub
FROM python:3.12-slim

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .

ENV HERMIES_DB=/data/hermies.db
VOLUME /data
EXPOSE 8787

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
