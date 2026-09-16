FROM python:3.12-slim

RUN pip install --no-cache-dir kopf>=1.37 kubernetes>=31.0 requests>=2.32

# A bare `USER 1000` (no matching /etc/passwd entry) is not enough - kopf's own peering-identity
# detection calls getpass.getuser() -> pwd.getpwuid(os.getuid()), which raises KeyError: uid not
# found for a UID with no passwd entry, crashing the container on every startup even under
# --standalone. useradd creates a real entry so that lookup succeeds.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app

COPY operator/ /app/operator/
WORKDIR /app

USER 1000

CMD ["kopf", "run", "--standalone", "operator/handlers.py"]
