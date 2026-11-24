# Fully qualified so rootless Podman resolves it without unqualified-search registries.
# By digest: the tag moves (a CPython patch release, in time the next Debian
# release and with it another R), and what a build got depended on the day its
# cache was filled. This is the index the tag named when production's base was
# pulled: the image of 2026-08-05, CPython 3.12.13 on Debian trixie. The pin
# makes nothing older - the release builds without --pull and still built on
# that image on 2026-11-01 - it makes the base a line of this file. Moving it
# is a change of its own: CI's image job builds it, and the first release
# after it rebuilds every layer.
FROM docker.io/library/python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
# R runtime for the GSADF (exuber) subprocess.
# - build-essential/gfortran: slim has no toolchain and CRAN installs compile from source
# - r-cran-*: Debian binary builds of the heavy dependencies (Rcpp, ggplot2, ...)
#   so install.packages only compiles exuber and stragglers
# - r-cran-jsonlite: needed by r/gsadf.R itself (not an exuber dependency)
# - stopifnot: install.packages does NOT error on failure; fail the build
#   instead (packageVersion raises when the package is not there)
# - exuber is S4's engine, and no gate runs it (the suite skips its R tests
#   where R is absent, and the release's smoke boot fits nothing). It comes
#   from a dated CRAN snapshot (Posit Package Manager; Posit also sponsors the
#   cloud.r-project.org mirror this line named before, and R checks no
#   signature on a package from either), so the nine packages CRAN adds with
#   it are that day's too - doRNG and rngtools carry the random streams of
#   radf_mc_cv - and the build stops unless exuber is the version S4 was
#   measured with (app/references.py, "s4"). Production's R layer was built
#   on Sunday 2026-08-09 from CRAN's newest and has run since; the date is the
#   Friday before it. That layer holds exuber 1.1.0, doRNG 1.8.6.3, doSNOW
#   1.0.20, rngtools 1.5.2, snow 0.4-4, progress 1.2.3, hms 1.1.4, prettyunits
#   1.2.0, crayon 1.5.3 and digest 0.6.39; the newest of the ten was published
#   on 2026-02-05, so every date from the day after it to that build names the
#   same ten. Moving the date or the version changes S4's engine: a change of
#   its own, with S4's readings before and after, and with the cached critical
#   values (r/gsadf.R keys them by n, nrep and seed, not by the engine)
#   re-keyed in that change.
# - apt installs what Debian trixie serves on the build day: the point and
#   security releases of one stable release, which the pinned base holds to.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        r-base r-base-dev build-essential gfortran \
        r-cran-jsonlite r-cran-rcpp r-cran-rcpparmadillo r-cran-ggplot2 \
        r-cran-dplyr r-cran-tidyr r-cran-purrr r-cran-lubridate \
        r-cran-foreach r-cran-doparallel r-cran-glue r-cran-rlang \
        r-cran-cli r-cran-tibble r-cran-generics && \
    R -e "install.packages('exuber', repos='https://packagemanager.posit.co/cran/2026-08-07', Ncpus=max(1L, parallel::detectCores()-1L)); stopifnot(packageVersion('exuber') == '1.1.0')" && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml requirements.lock ./
# The packages come from the lock, hash-checked (`make lock` writes it from
# pyproject.toml); the project itself installs without resolving anything.
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && \
    pip install --no-cache-dir --no-deps . && \
    python -c "import numpy, pandas, pyarrow; print('numeric stack OK:', numpy.__version__, pandas.__version__, pyarrow.__version__)"
COPY . .
RUN mkdir -p /data
# B-12 hardening note (audit): an in-image `USER appuser` (uid 10001) was tried
# and REVERTED — under rootless Podman the /data bind mount is owned by the
# invoking host user, and container-uid 10001 maps to a subordinate uid with no
# write access ("attempt to write a readonly database" on the WAL pragma; the
# 2026-07-15 deploy failed on it and auto-rolled back). Container-root under
# rootless Podman already maps to the UNPRIVILEGED host user, so the escape
# blast radius is unchanged; the defence-in-depth is provided instead by
# --cap-drop=ALL --security-opt no-new-privileges at run time (the Quadlet
# unit deploy/quadlet/bubblegauge.container / compose.yml), which does not
# fight the bind-mount ownership.
# PYTHONPATH: the project is installed before the source is copied, so that
# the package layer stays cached across releases; the installed distribution
# is therefore the `bubblegauge` command and no package. A console script's
# first import path is its own directory (/usr/local/bin), not the working
# directory, and without this line the documented command (README, "the CLI
# in the container"; docs/ALERT_SYSTEM.md, "Operating it") ended in
# "ModuleNotFoundError: No module named 'app'". `python -m` and uvicorn put
# the working directory on the path themselves, so the service, the release
# and the watchdog never met it, and /app is already first on their path:
# nothing new becomes importable. CI's image job runs the command in the
# built image.
ENV TZ=UTC PYTHONUNBUFFERED=1 PYTHONPATH=/app
EXPOSE 8000
# --no-access-log: uvicorn's error log (startup, shutdown, the traceback of a
# request or a boot that failed) is on; its access log would add a line per
# request with the client's address - 2,880 a day from the health check alone.
# A reader's address is third-party personal data, which the service does not
# collect or store (SECURITY.md). The production journal of 2026-10-05 to
# 2026-11-10 holds no request line either: Alembic's setup at boot disabled
# this logger.
# --ws none: the API serves no WebSocket. With uvicorn's WebSocket support on,
# its error log refused an upgrade request with a 403 and an INFO line that
# names the client (executed); off, the request is answered as the plain GET
# it also is, and uvicorn writes two warnings that name no one ("Unsupported
# upgrade request." and "No supported WebSocket library detected": none is
# used). tests/test_boot_logging.py runs this command on the loopback.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--ws", "none"]
