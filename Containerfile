# Fully qualified so rootless Podman resolves it without unqualified-search registries
FROM docker.io/library/python:3.12-slim
# R runtime for the GSADF (exuber) subprocess.
# - build-essential/gfortran: slim has no toolchain and CRAN installs compile from source
# - r-cran-*: Debian binary builds of the heavy dependencies (Rcpp, ggplot2, ...)
#   so install.packages only compiles exuber and stragglers
# - r-cran-jsonlite: needed by r/gsadf.R itself (not an exuber dependency)
# - stopifnot: install.packages does NOT error on failure; fail the build instead
RUN apt-get update && apt-get install -y --no-install-recommends \
        r-base r-base-dev build-essential gfortran \
        r-cran-jsonlite r-cran-rcpp r-cran-rcpparmadillo r-cran-ggplot2 \
        r-cran-dplyr r-cran-tidyr r-cran-purrr r-cran-lubridate \
        r-cran-foreach r-cran-doparallel r-cran-glue r-cran-rlang \
        r-cran-cli r-cran-tibble r-cran-generics && \
    R -e "install.packages('exuber', repos='https://cloud.r-project.org', Ncpus=max(1L, parallel::detectCores()-1L)); stopifnot('exuber' %in% rownames(installed.packages()))" && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY . .
ENV TZ=UTC PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
