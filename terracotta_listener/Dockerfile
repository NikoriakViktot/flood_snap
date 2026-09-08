FROM python:3.11-slim
RUN pip install --no-cache-dir redis requests
WORKDIR /app
COPY listener.py manifest_validator.py /app/
CMD ["python3", "/app/listener.py"]
