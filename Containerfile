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
RUN mkdir -p /data
# B-12 hardening note (audit): an in-image `USER appuser` (uid 10001) was tried
# and REVERTED — under rootless Podman the /data bind mount is owned by the
# invoking host user, and container-uid 10001 maps to a subordinate uid with no
# write access ("attempt to write a readonly database" on the WAL pragma; the
# 2026-07-15 deploy failed on it and auto-rolled back). Container-root under
# rootless Podman already maps to the UNPRIVILEGED host user, so the escape
# blast radius is unchanged; the defence-in-depth is provided instead by
# --cap-drop=ALL --security-opt no-new-privileges at run time (deploy.sh /
# compose.yml), which does not fight the bind-mount ownership.
ENV TZ=UTC PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
