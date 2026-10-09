FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir "pydantic-ai>=1.62.0" "fastapi>=0.115" "uvicorn>=0.34"
COPY app.py .
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
