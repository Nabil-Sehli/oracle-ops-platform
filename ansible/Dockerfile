# Ansible has no Windows control node, so it runs from this container.
FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "ansible-core>=2.18,<2.19"
WORKDIR /work
