FROM python:3.11-slim

WORKDIR /app

# System deps for scikit-learn / numpy wheels (slim image is missing libgomp).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY frontend ./frontend
COPY data ./data
COPY run_api.py .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

EXPOSE 8000

CMD ["python", "run_api.py"]
