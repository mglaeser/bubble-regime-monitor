# Fully qualified so rootless Podman resolves it without unqualified-search registries
FROM docker.io/library/python:3.12-slim
# R runtime for the GSADF (exuber) subprocess.
# - build-essential/gfortran: slim has no toolchain and CRAN installs compile from source
# - r-cran-*: Debian binary builds of the heavy dependencies (Rcpp, ggplot2, ...)
#   so install.packages only compiles exuber and stragglers
# - r-cran-jsonlite: needed by r/gsadf.R itself (not an exuber dependency)
# - stopifnot: install.packages does NOT error on failure; fail the build instead
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        r-base r-base-dev build-essential gfortran \
        r-cran-jsonlite r-cran-rcpp r-cran-rcpparmadillo r-cran-ggplot2 \
        r-cran-dplyr r-cran-tidyr r-cran-purrr r-cran-lubridate \
        r-cran-foreach r-cran-doparallel r-cran-glue r-cran-rlang \
        r-cran-cli r-cran-tibble r-cran-generics && \
    R -e "install.packages('exuber', repos='https://cloud.r-project.org', Ncpus=max(1L, parallel::detectCores()-1L)); stopifnot('exuber' %in% rownames(installed.packages()))" && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
# pyarrow is an optional extra: pandas imports it EAGERLY when present
# (pandas/compat/pyarrow.py), and pyarrow's Arrow C++ wheels require SSE4.2 —
# on older CPUs that is an uncatchable SIGILL at pandas import, i.e. at
# service boot. The build host is the run host for self-hosted deploys, so
# probe here: keep pyarrow where it works, remove it where it would crash
# (Parquet export then disables itself via its own runtime probe), and fail
# the build loudly if pandas still cannot import.
RUN pip install --no-cache-dir ".[parquet]" && \
    (python -c "import pandas" 2>/dev/null || \
     (echo "pyarrow unusable on this CPU; removing (Parquet export will disable itself)" && \
      pip uninstall -y pyarrow)) && \
    python -c "import numpy, pandas; print('core numeric stack OK:', numpy.__version__, pandas.__version__)"
COPY . .
# B-12: run as a non-root user (defence in depth on top of rootless Podman).
# /data is the SQLite volume mount-point; make it writable by the app user.
RUN useradd --system --uid 10001 --home-dir /app --no-create-home appuser \
    && mkdir -p /data && chown -R appuser:appuser /app /data
USER appuser
ENV TZ=UTC PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
