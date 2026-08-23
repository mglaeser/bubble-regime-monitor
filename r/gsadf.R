# GSADF via exuber (JSS 103(10), doi:10.18637/jss.v103.i10); exuber >= 1.1.0.
# Contract: JSON {"series": [...], "params": {...}} on stdin -> on stdout
#   {"gsadf","cv90","cv95"}                     the sup over all endpoints (REPORTED)
#   {"bsadf","bsadf_cv90","bsadf_cv95"}         the endpoint read (SCORED since v4.0)
#   {"bsadf_n","bsadf_argmax","bsadf_n_finite"} sequence provenance
#   {"bsadf_cv_route"}  which route produced the endpoint CVs, or "unavailable"
# Any of the bsadf_* fields may be null; Python then floors s4 rather than guessing.
# NEVER hard-code the blog value 1.49 — that is a SADF critical value, not GSADF.
#
# v3.3.1: CACHED MONTE-CARLO critical values (radf_mc_cv) instead of the v3.3.0
# per-call wild bootstrap (radf_wb_cv). radf_mc_cv depends ONLY on (n, minw) —
# NOT on the observed data — so it is computed once and cached to disk; the wild
# bootstrap re-ran a full recursive right-tailed regression set every call and
# timed out on the Intel Atom N2800 at T~329 (s4 regressed to NULL). s4 is
# CONTESTED and capped at 0.25 regardless, so this is a reliability fix, not a
# score change. The statistic itself (rob$gsadf) is unchanged.
library(exuber); library(jsonlite)

inp <- fromJSON(file("stdin"))
y   <- inp$series                      # numeric vector, monthly log prices

# F-01/L-07: lag / MC_NREP / MC_SEED are supplied by the caller from the canonical
# frozen_methodology.json so R does not independently own these constants. The
# literals below are kept ONLY as a defensive fallback if params are absent; the
# service always passes them.
GSADF_LAG <- if (!is.null(inp$params$lag))     as.integer(inp$params$lag)     else 0L
MC_NREP   <- if (!is.null(inp$params$mc_nrep)) as.integer(inp$params$mc_nrep) else 2000L
MC_SEED   <- if (!is.null(inp$params$mc_seed)) as.integer(inp$params$mc_seed) else 20260711L

r   <- radf(y, lag = GSADF_LAG)        # minw defaults to 0.01 + 1.8/sqrt(T)

# Monte-Carlo CVs are data-independent (function of n + the default minw for that
# n), so cache them. The cache KEY includes nrep and seed (v3.7.4/G-01): those
# change the simulated CVs, so a value cached under a different (nrep, seed)
# must NOT be reused. Changing either constant simply lands a new cache file.
n         <- length(y)
cache_dir <- Sys.getenv("GSADF_CV_CACHE", "/data/cv_cache")
cv_path   <- file.path(cache_dir, sprintf("mc_cv_n%d_nrep%d_seed%d.rds", n, MC_NREP, MC_SEED))
cv <- NULL
if (file.exists(cv_path)) {
  cv <- tryCatch(readRDS(cv_path), error = function(e) NULL)
}
if (is.null(cv)) {
  set.seed(MC_SEED)
  cv <- radf_mc_cv(n = n, nrep = MC_NREP)
  tryCatch({
    dir.create(cache_dir, recursive = TRUE, showWarnings = FALSE)
    saveRDS(cv, cv_path)
  }, error = function(e) invisible(NULL))   # cache-write failure must not break the run
}

# Extract the GSADF statistic and simulated CVs across exuber API variants.
extract_stat <- function(obj) {
  if (!is.null(obj$gsadf)) return(as.numeric(obj$gsadf)[1])
  td <- tidy(obj)
  as.numeric(td$gsadf)[1]
}
extract_cv <- function(obj, level) {
  key <- paste0(level, "%")
  if (!is.null(obj$gsadf_cv)) return(as.numeric(obj$gsadf_cv[key]))
  td <- tidy(obj)
  if ("sig" %in% names(td)) return(as.numeric(td$gsadf[td$sig == level])[1])
  as.numeric(obj$gsadf[key])
}

# --- BSADF AT THE LAST OBSERVATION (the current-regime read) ---
# GSADF is sup over ALL endpoints r2, so it answers "was there ever an explosive
# episode anywhere in this window?" -- a historical question whose answer stays
# REJECTED long after the episode ends. Measured on native Nasdaq-100 from 1986
# (exuber 1.1.0, T=487): GSADF 2.6189 > cv95 2.2604 -> rejects, and the sup is
# attained at a window ending 2000-02, while the BSADF at the 2026-07 endpoint is
# 0.7562 against an endpoint cv90 of 1.1769.
#
# BOUND ON THAT NUMBER: gsadf.series_months_max = 360 caps every runtime fit, so
# T=487 is an offline measurement -- the service never fits it. On the 360-month
# tail it does fit, the same series gives GSADF 1.3234 vs cv95 2.2099 (no
# rejection, sup dated 2021-08) while the endpoint is 0.7562 under BOTH. The sup
# changes its verdict with the window length it is handed; the endpoint does not.
# That is why a live regime gauge reads the endpoint, not the sample maximum.
# Both are emitted; Python scores one of them per frozen gsadf.statistic.
extract_bsadf_cv <- function(r, cv, bs) {
  # ONE route, stated plainly. The endpoint CV is the last row of cv$bsadf_cv,
  # accepted only when that matrix is row-aligned with the BSADF sequence and
  # carries the expected level names -- a mismatch means the CVs were simulated
  # under a different (n, minw) than the fit, and comparing them would be a
  # silent category error.
  #
  # There is deliberately NO second route. extract_cv() above defends the GSADF
  # critical value with a tidy() fallback, so a fallback was attempted here too:
  # tidy() exposes only the summary CVs (adf/sadf/gsadf), and augment_join(),
  # which does expose per-index CVs, reads the SAME cv$bsadf_cv field -- measured,
  # it returns "unavailable" in every case that defeats the direct route
  # (renamed level columns, field removed, empty object). It agrees with the
  # direct route when the direct route works, which is not what a fallback is
  # for. A second path that fails whenever the first one does is dead code
  # pretending to be redundancy, so it is not shipped.
  #
  # What IS shipped is the diagnosis: losing this pair floors s4 AND disables
  # red flag #1, and "unavailable" says so, distinguishing a CV-extraction
  # failure from a degenerate series. Both floor; they are not the same fault.
  bcv <- cv$bsadf_cv
  if (!is.null(bcv) && is.matrix(bcv) && nrow(bcv) == length(bs) &&
      all(c("90%", "95%") %in% colnames(bcv))) {
    return(list(cv90 = as.numeric(bcv[nrow(bcv), "90%"]),
                cv95 = as.numeric(bcv[nrow(bcv), "95%"]), route = "bsadf_cv"))
  }
  list(cv90 = NA, cv95 = NA, route = "unavailable")
}

bsadf <- NA; bsadf_cv90 <- NA; bsadf_cv95 <- NA; bsadf_n <- NA
bsadf_argmax <- NA; bsadf_n_finite <- NA; bsadf_cv_route <- NA
bs <- if (!is.null(r$bsadf)) as.numeric(r$bsadf) else NULL
if (!is.null(bs) && length(bs) > 0L) {
  bsadf_n        <- length(bs)
  bsadf_n_finite <- sum(is.finite(bs))

  # The SCORED statistic is a SINGLE endpoint, so it is gated on its OWN
  # finiteness. Requiring the WHOLE sequence to be finite discarded a perfectly
  # computable current-regime read whenever any historical sub-window was
  # degenerate. Measured: a series with a stale/flat quote run early in the
  # sample yields 179/210 finite BSADF values with a valid endpoint of -0.6482,
  # while exuber's own gsadf (which skips NAs) reports 10.2127 against cv95
  # 2.1357 at production nrep=2000 -- a loud rejection. Under the whole-history
  # gate s4 threw the -0.6482
  # away and floored at the imputed 0.25. Letting the distant past veto a
  # measurable present is precisely what scoring the endpoint exists to avoid.
  if (is.finite(bs[length(bs)])) {
    bsadf <- bs[length(bs)]
  }
  # The endpoint CV is the LAST row, and ONLY if the CV matrix is row-aligned with
  # the BSADF sequence. A mismatch means the CVs were simulated under a different
  # (n, minw) than the fit; comparing them would be a silent category error, so
  # leave them NA and let Python floor s4 at the contested 0.25 instead.
  #
  # Losing this pair is expensive -- it floors s4 AND disables red flag #1 -- so
  # the route taken (or "unavailable") is reported. See extract_bsadf_cv above
  # for why there is no second route.
  if (is.finite(bsadf)) {
    got <- extract_bsadf_cv(r, cv, bs)
    bsadf_cv90 <- got$cv90; bsadf_cv95 <- got$cv95; bsadf_cv_route <- got$route
  }

  # The argmax IS a claim about the supremum over the WHOLE history, so it does
  # need the whole history: with holes in the sequence there is no honest
  # date-stamp, and which.max would silently report the max of the finite subset.
  if (all(is.finite(bs))) bsadf_argmax <- which.max(bs)
}

cat(toJSON(list(gsadf = extract_stat(r),
                cv90  = extract_cv(cv, 90),
                cv95  = extract_cv(cv, 95),
                bsadf      = bsadf,
                bsadf_cv90 = bsadf_cv90,
                bsadf_cv95 = bsadf_cv95,
                bsadf_n        = bsadf_n,
                bsadf_argmax   = bsadf_argmax,
                bsadf_n_finite = bsadf_n_finite,
                bsadf_cv_route = bsadf_cv_route),
           auto_unbox = TRUE, na = "null"))
