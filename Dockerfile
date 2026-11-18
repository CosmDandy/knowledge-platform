FROM python:3.13-slim

WORKDIR /app

# Heavy, rarely changing layers first: dependencies, then the embedding model, then the code.
RUN pip install --no-cache-dir "mcp>=2" "fastembed>=0.7" "numpy>=2"

# The model is baked into the image: the container's root is read-only and needs no network at runtime.
# embeddings.py alone is copied so editing other code does not download the model again.
ENV KNOWLEDGE_MODEL_DIR=/opt/models
COPY knowledge/embeddings.py /build/embeddings.py
RUN cd /build && python -c "import embeddings; embeddings.Embedder('/opt/models').embed(['warm up'])" \
    && chmod -R a+rX /opt/models && rm -rf /build

COPY pyproject.toml ./
COPY knowledge ./knowledge
RUN pip install --no-cache-dir --no-deps .

RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app

# 0.0.0.0 is the container's own interface; the host publishes the port on one address only.
ENV KNOWLEDGE_VAULT=/vault \
    KNOWLEDGE_DB=/data/knowledge.db \
    KNOWLEDGE_HOST=0.0.0.0 \
    HF_HUB_OFFLINE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 7234
CMD ["knowledge-mcp-http"]
