FROM python:3.11-slim
WORKDIR /app
COPY . /app
CMD ["uvicorn", "alios_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
