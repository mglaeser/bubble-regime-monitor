# GSADF via exuber (JSS 103(10), doi:10.18637/jss.v103.i10); exuber >= 1.1.0.
# Contract: JSON {"series": [...]} on stdin -> {"gsadf", "cv90", "cv95"} on stdout.
# NEVER hard-code the blog value 1.49 — that is a SADF critical value, not GSADF.
# v3.3.0: WILD-BOOTSTRAP critical values (radf_wb_cv, Harvey et al. 2016) instead
# of Monte-Carlo CVs — robust to serial correlation / non-stationary volatility,
# which severely oversize PWY/PSY (Pedersen & Schutte 2020). CVs are computed on
# the actual series, and the caller now feeds an extended monthly history (QQQ
# from 1999, T ~ 329) so the statistic is calibrated (PSY tables start at T=100).
library(exuber); library(jsonlite)

inp <- fromJSON(file("stdin"))
y   <- inp$series                      # numeric vector, monthly log prices
r   <- radf(y, lag = 0)                # minw defaults to 0.01 + 1.8/sqrt(T)
cv  <- radf_wb_cv(y, nboot = 999)      # wild-bootstrap, data-dependent CVs

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
