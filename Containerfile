# Fully qualified so rootless Podman resolves it without unqualified-search registries
FROM docker.io/library/python:3.12-slim
# R runtime for the GSADF (exuber) subprocess
RUN apt-get update && apt-get install -y --no-install-recommends r-base && \
    R -e "install.packages('exuber', repos='https://cloud.r-project.org')" && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY . .
ENV TZ=UTC PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
