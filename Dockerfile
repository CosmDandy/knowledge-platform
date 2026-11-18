FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml ./
COPY knowledge ./knowledge
RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app

# 0.0.0.0 is the container's own interface; the host publishes the port on one address only.
ENV KNOWLEDGE_VAULT=/vault \
    KNOWLEDGE_DB=/data/knowledge.db \
    KNOWLEDGE_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1
EXPOSE 7234
CMD ["knowledge-mcp-http"]
