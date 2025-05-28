# Use a slim Python base image
ARG build_image="python:3.8-slim"
FROM ${build_image}

# Set working directory
WORKDIR /app

# Copy requirements (explicitly or just install inline)
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Expose the port the app runs on
EXPOSE 5000

# Run the Flask app
CMD ["python", "webhook.py"]