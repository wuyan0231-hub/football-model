#!/usr/bin/env Rscript
# JSON input/output bridge to exact official goalmodel 0.6.4 source functions.
# No fixed_params/offset path. Sources are independently fingerprinted before
# sourcing; G0/G1 and optional DC have independent fits and complete matrices.

args <- commandArgs(trailingOnly=TRUE)
if (length(args) != 2L) stop("usage: Rscript goalmodel_bridge.R input.json output.json")
if (!requireNamespace("jsonlite", quietly=TRUE)) stop("jsonlite is required")
source_commit <- "84ecd6c2bbad3ccb967abf88ef49e5bcd074e545"
upstream_version <- "0.6.4"
input <- jsonlite::fromJSON(args[[1]], simplifyVector=FALSE)
if (!identical(input$schema_version, 1L) || !identical(input$source_commit, source_commit) ||
    !identical(input$upstream_version, upstream_version)) stop("pinned source protocol mismatch")
source_md5 <- c(goalmodel_fit.R="bd4c4427b16b830182895a5e1e0f0541",
                goalmodel_predict.R="f7878b84f9cda3395bf016fa592cb5a3")
upstream <- new.env(parent=globalenv())
for (name in names(source_md5)) {
  path <- input$source_paths[[name]]
  if (is.null(path) || !file.exists(path) ||
      !identical(unname(tools::md5sum(path)), unname(source_md5[[name]]))) {
    stop(paste("pinned upstream source hash mismatch", name))
  }
  sys.source(path, envir=upstream)
}
records <- input$training_matches
fixtures <- input$fixtures
cutoff <- as.POSIXct(input$cutoff, format="%Y-%m-%dT%H:%M:%OSZ", tz="UTC")
if (is.na(cutoff)) stop("cutoff needs canonical UTC timestamp")
for (record in records) {
  kickoff <- as.POSIXct(record$kickoff, format="%Y-%m-%dT%H:%M:%OSZ", tz="UTC")
  if (is.na(kickoff) || kickoff + 4*3600 > cutoff) stop("result too recent or after cutoff")
  for (goal in c(record$home_goals, record$away_goals)) {
    if (!is.numeric(goal) || length(goal)!=1 || !is.finite(goal) || goal<0 || goal!=floor(goal)) {
      stop("only normalized nonnegative integer regulation-time goals are accepted")
    }
  }
}
for (fixture in fixtures) {
  kickoff <- as.POSIXct(fixture$kickoff, format="%Y-%m-%dT%H:%M:%OSZ", tz="UTC")
  if (is.na(kickoff) || kickoff < cutoff) stop("fixture before cutoff")
}
feature_names <- unlist(input$feature_names, use.names=FALSE)
if (is.null(feature_names)) feature_names <- character()
if (anyDuplicated(feature_names) || any(startsWith(feature_names, "__"))) stop("bad feature names")
column_names <- c("__home_field", feature_names)
extract <- function(items, field, mode="numeric") {
  vapply(items, function(item) item[[field]], switch(mode, numeric=0, character="", logical=FALSE))
}
make_x <- function(items, features, side) {
  x <- matrix(0, nrow=length(items), ncol=length(column_names),
              dimnames=list(NULL, column_names))
  for (i in seq_along(items)) {
    # Model's hfa=FALSE; this explicit covariate supports per-match neutral.
    x[i, "__home_field"] <- if (side == "home" && !isTRUE(items[[i]]$neutral)) 1 else 0
    if (length(feature_names)) {
      row <- features[[as.character(items[[i]]$match_id)]][[side]]
      if (is.null(row) || !setequal(names(row), feature_names)) stop("missing/asymmetric covariate columns")
      x[i, feature_names] <- vapply(feature_names, function(n) as.numeric(row[[n]]), 0)
    }
  }
  if (any(!is.finite(x))) stop("nonfinite covariate")
  x
}
weights <- as.numeric(unlist(input$training_weights, use.names=FALSE))
if (length(weights) != length(records) || any(!is.finite(weights)) || any(weights <= 0)) stop("invalid weights")
expected_weights <- vapply(records, function(record) {
  kickoff <- as.POSIXct(record$kickoff, format="%Y-%m-%dT%H:%M:%OSZ", tz="UTC")
  exp(-log(2)*as.numeric(difftime(cutoff, kickoff, units="days"))/180)
}, 0)
if (any(abs(weights - expected_weights)>1e-12)) stop("supplied weights differ from Python 180-day protocol")
x1 <- make_x(records, input$training_features, "home")
x2 <- make_x(records, input$training_features, "away")
warnings <- character()
fit <- withCallingHandlers(upstream$goalmodel(
  goals1=extract(records, "home_goals"), goals2=extract(records, "away_goals"),
  team1=as.character(extract(records, "home_id")),
  team2=as.character(extract(records, "away_id")),
  x1=x1, x2=x2, hfa=FALSE, dc=isTRUE(input$dc),
  rs=FALSE, hurdle=FALSE, model="poisson", weights=weights),
  warning=function(w) { warnings <<- c(warnings, conditionMessage(w)); invokeRestart("muffleWarning") })
if (!isTRUE(fit$converged) || any(!is.finite(unlist(fit$parameters))) ||
    !is.finite(fit$loglikelihood)) stop("upstream fit did not converge to finite parameters")
px1 <- make_x(fixtures, input$prediction_features, "home")
px2 <- make_x(fixtures, input$prediction_features, "away")
forecasts <- list()
errors <- list()
for (i in seq_along(fixtures)) {
  fixture <- fixtures[[i]]
  h <- as.character(fixture$home_id)
  a <- as.character(fixture$away_id)
  if (!(h %in% fit$all_teams) || !(a %in% fit$all_teams)) {
    errors[[length(errors)+1L]] <- list(match_id=fixture$match_id, code="unsupported_team",
                                        message="team absent from this R training sample")
    next
  }
  expected <- upstream$predict_expg(fit, team1=h, team2=a,
                                    x1=px1[i,,drop=FALSE], x2=px2[i,,drop=FALSE])
  lh <- unname(expected$expg1[[1]])
  la <- unname(expected$expg2[[1]])
  maxgoal <- max(2, qpois(1-input$tail_tolerance/4, lambda=lh),
                 qpois(1-input$tail_tolerance/4, lambda=la))
  if (!is.finite(maxgoal) || maxgoal > 127 || !is.finite(lh) || !is.finite(la)) {
    errors[[length(errors)+1L]] <- list(match_id=fixture$match_id, code="invalid_rate_or_grid",
                                        message="unbounded rate/grid; no forecast published")
    next
  }
  score <- withCallingHandlers(upstream$predict_goals(fit, team1=h, team2=a,
      x1=px1[i,,drop=FALSE], x2=px2[i,,drop=FALSE], maxgoal=maxgoal)[[1]],
      warning=function(w) { warnings <<- c(warnings, conditionMessage(w)); invokeRestart("muffleWarning") })
  if (any(!is.finite(score)) || any(score < 0)) stop("invalid upstream score matrix")
  # DC factors sum to zero adjustment across the full 2x2 block; grid >= 2.
  tail_mass <- 1 - ppois(maxgoal, lh)*ppois(maxgoal, la)
  rho <- if (is.null(fit$parameters$rho)) 0 else unname(fit$parameters$rho)
  forecasts[[length(forecasts)+1L]] <- c(fixture, list(
    expected_home_goals=lh, expected_away_goals=la, rho=rho,
    score_matrix=lapply(seq_len(nrow(score)), function(row) unname(score[row,])),
    score_matrix_axes=list(rows="home_goals_from_0", columns="away_goals_from_0"),
    tail_mass_before_normalization=tail_mass,
    probabilities_1x2=list(home=sum(score[lower.tri(score)]), draw=sum(diag(score)),
                           away=sum(score[upper.tri(score)]))))
}
parameters <- fit$parameters
to_named_list <- function(values) if (is.null(names(values))) unname(values) else as.list(values)
output <- list(schema_version=1, status="ok", source_commit=source_commit,
               upstream_version=upstream_version, source_mode="pinned_official_R_sources",
               r_version=R.version.string, dc=isTRUE(input$dc),
               fit=list(converged=TRUE, ngames=fit$ngames, fitter=fit$fitter,
                        regularization="upstream unpenalized likelihood; differs from production ridge",
                        home_field_method="hfa_FALSE_explicit_same_column_covariate",
                        parameters=lapply(parameters, to_named_list),
                        loglikelihood=unname(fit$loglikelihood)),
               forecasts=forecasts, errors=errors, warnings=unique(warnings))
jsonlite::write_json(output, args[[2]], auto_unbox=TRUE, digits=NA,
                     pretty=TRUE, null="null", na="null")
