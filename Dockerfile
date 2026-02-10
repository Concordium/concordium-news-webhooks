# Use a slim Python base image
ARG build_image="python:3.12-slim"
FROM ${build_image}

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only application code
COPY src/ ./src/

ENV PYTHONUNBUFFERED=1

# Optional: document the Flask port used by discourse_webhook service
EXPOSE 5000
