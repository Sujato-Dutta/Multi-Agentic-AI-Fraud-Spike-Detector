FROM python:3.12-slim
WORKDIR /app
COPY requirements /app/requirements
RUN pip install --no-cache-dir -r requirements/base.txt -r requirements/ml.txt -r requirements/agents.txt -r requirements/monitoring.txt
COPY backend /app/backend
COPY infrastructure /app/infrastructure
COPY evaluation /app/evaluation
COPY scripts /app/scripts
COPY models /app/models
COPY data /app/data
ENV PYTHONPATH=/app
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
