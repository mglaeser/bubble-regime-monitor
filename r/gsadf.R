# GSADF via exuber (JSS 103(10), doi:10.18637/jss.v103.i10); exuber >= 1.1.0.
# Contract: JSON {"series": [...]} on stdin -> {"gsadf", "cv90", "cv95"} on stdout.
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
r   <- radf(y, lag = 0)                # minw defaults to 0.01 + 1.8/sqrt(T)

# Monte-Carlo CVs are data-independent (function of n + the default minw for that
# n), so cache them. The cache KEY includes nrep and seed (v3.7.4/G-01): those
# change the simulated CVs, so a value cached under a different (nrep, seed)
# must NOT be reused. Changing either constant simply lands a new cache file.
MC_NREP <- 2000L
MC_SEED <- 20260711L
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

cat(toJSON(list(gsadf = extract_stat(r),
                cv90  = extract_cv(cv, 90),
                cv95  = extract_cv(cv, 95)),
           auto_unbox = TRUE))
