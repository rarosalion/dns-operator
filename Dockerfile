FROM python:3.12-slim

RUN pip install --no-cache-dir kopf>=1.37 kubernetes>=31.0 requests>=2.32

COPY operator/ /app/operator/
WORKDIR /app

CMD ["kopf", "run", "--standalone", "operator/handlers.py"]
