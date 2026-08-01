FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml /app/
RUN pip install --no-cache-dir .
COPY apps/api /app/apps/api
COPY packages/core /app/packages/core
COPY packages/runtime /app/packages/runtime
ENV PYTHONPATH=/app/apps/api:/app/packages/core:/app/packages/runtime
CMD ["uvicorn", "alios_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
