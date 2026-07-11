# GSADF via exuber (JSS 103(10), doi:10.18637/jss.v103.i10).
# Contract: JSON {"series": [...]} on stdin -> {"gsadf", "cv90", "cv95"} on stdout.
# NEVER hard-code the blog value 1.49 — that is a SADF critical value, not
# GSADF; finite-sample GSADF CVs are simulated below (~1.9-2.1 depending on T).
library(exuber); library(jsonlite)

inp <- fromJSON(file("stdin"))
y   <- inp$series                      # numeric vector, monthly log prices
r   <- radf(y, lag = 1)                # minw defaults to 0.01 + 1.8/sqrt(T)
cv  <- radf_mc_cv(n = length(y), nrep = 2000, seed = 123)

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
