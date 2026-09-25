# Pinned by digest, not just tag - `python:3.12-slim` is a mutable tag that can point at a
# different image tomorrow with no record of what actually got built. Digest is for the multi-arch
# manifest list (resolved 2026-09-25 from python:3.12-slim), so this still builds correctly on
# amd64 and arm64. Bump both together when Renovate (or a manual check) finds a newer digest.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# The previous unquoted `kopf>=1.37 kubernetes>=31.0 requests>=2.32` here was a real bug, not just
# a style nit: RUN executes under a shell, and `>=1.37`/`>=31.0`/`>=2.32` are each parsed as an
# output redirection (`>` truncates/creates a file named `=1.37` etc.), not a version constraint -
# confirmed by reproducing it outside Docker: the command that actually ran was
# `pip install --no-cache-dir kopf kubernetes requests`, completely unpinned, with three stray
# empty files left behind. requirements.txt (hash-locked via pip-tools from requirements.in) is
# what actually pins and verifies these dependencies now.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# A bare `USER 1000` (no matching /etc/passwd entry) is not enough - kopf's own peering-identity
# detection calls getpass.getuser() -> pwd.getpwuid(os.getuid()), which raises KeyError: uid not
# found for a UID with no passwd entry, crashing the container on every startup even under
# --standalone. useradd creates a real entry so that lookup succeeds.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app

COPY operator/ /app/operator/
WORKDIR /app

USER 1000

CMD ["kopf", "run", "--standalone", "--liveness=http://0.0.0.0:8080/healthz", "operator/handlers.py"]
