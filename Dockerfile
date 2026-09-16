FROM python:3.12-slim

RUN pip install --no-cache-dir kopf>=1.37 kubernetes>=31.0 requests>=2.32

COPY operator/ /app/operator/
WORKDIR /app

# Runs as a fixed non-root UID so the pod can satisfy the cluster's "restricted" PodSecurity
# policy (runAsNonRoot) without needing an explicit runAsUser override in the chart - COPY already
# leaves these files world-readable, which is all a non-root process needs here.
USER 1000

CMD ["kopf", "run", "--standalone", "operator/handlers.py"]
