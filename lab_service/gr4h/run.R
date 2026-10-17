# CemaNeige-GR4J (daily, the default) or CemaNeige-GR4H (hourly) runs for one basin (airGR), called by
# lab_service.gr4h.runner in the memory-limited job child:
#   Rscript run.R <work_dir>
# Reads  forcing.csv  time_utc (ISO, one row per step: daily at 00:00 UTC or hourly, whole UTC days), P_mm, T_C, [PET_mm], [block_id]
#        config.json  also "time_step": "1d" | "1h"
#        params.json  {"sets": [{"id","X1","X2","X3","X4","CNX1","CNX2"}, …]}   set 0 = the reference
#        config.json  {"lat", "z_mean_m", "hypso_m"[101], "area_km2", "warmup_end", "run_start", "run_end",
#                      "members": {"mode": "reference"|"ensemble", "alphas", "n_per_alpha", "depth_factors", "seed",
#                                  "param_sets": "reference"|"behavioural"}}
# Writes qsim.csv (time_utc + one column per member, mm/h), members.csv (member, set, hyeto, alpha, depth),
#        state.json (CemaNeige-GR4H states at warmup_end for the reference set: the initial state of a next run),
#        versions.json. Rain is redistributed only inside the run window and only within a block (the 12-h or 24-h
#        total of a block is conserved exactly), as in geohydroai-storms mode A.
suppressMessages(library(airGR))
options(warn = -1)
args <- commandArgs(TRUE)
wd <- args[1]
f <- read.csv(file.path(wd, "forcing.csv"), stringsAsFactors = FALSE)
cfg <- jsonlite::fromJSON(file.path(wd, "config.json"))
ps <- jsonlite::fromJSON(file.path(wd, "params.json"))$sets
daily <- identical(cfg$time_step, "1d")
step_s <- if (daily) 86400 else 3600
dates <- as.POSIXct(f$time_utc, tz = "UTC", format = "%Y-%m-%dT%H:%M:%SZ")
if (any(is.na(dates))) dates <- as.POSIXct(f$time_utc, tz = "UTC")
stopifnot(!any(is.na(dates)), all(diff(as.numeric(dates)) == step_s))
Tm <- f$T_C; Tm[is.na(Tm)] <- mean(Tm, na.rm = TRUE)
if (!is.null(f$PET_mm) && !all(is.na(f$PET_mm))) {
  PE <- f$PET_mm; PE[is.na(PE)] <- 0
} else {
  day <- as.Date(dates); Td <- tapply(Tm, day, mean); JD <- as.POSIXlt(as.Date(names(Td)))$yday + 1
  PE <- PE_Oudin(JD = JD, Temp = as.numeric(Td), Lat = cfg$lat, LatUnit = "deg", TimeStepIn = "daily", TimeStepOut = if (daily) "daily" else "hourly")
  stopifnot(length(PE) == length(dates))
}
FUN <- if (daily) RunModel_CemaNeigeGR4J else RunModel_CemaNeigeGR4H
MODEL <- if (daily) "CemaNeigeGR4J" else "CemaNeigeGR4H"
mk <- function(P) CreateInputsModel(FUN_MOD = FUN, DatesR = dates, Precip = P, PotEvap = PE, TempMean = Tm,
                                    HypsoData = cfg$hypso_m, ZInputs = cfg$z_mean_m, NLayers = 5, verbose = FALSE)
at <- function(a, b) which(dates >= as.POSIXct(a, tz = "UTC") & dates <= as.POSIXct(b, tz = "UTC"))
warm <- at(cfg$warmup_start, cfg$warmup_end); run <- at(cfg$run_start, cfg$run_end)
stopifnot(length(warm) >= (if (daily) 30 else 24 * 30), length(run) >= 1)
param <- function(s) c(s$X1, s$X2, s$X3, s$X4, s$CNX1, s$CNX2)

# the reference: states at the end of the warm-up (initial state of a future run), then the run itself
IM <- mk(f$P_mm)
RO_w <- CreateRunOptions(FUN_MOD = FUN, InputsModel = IM, IndPeriod_WarmUp = 0L, IndPeriod_Run = warm, Outputs_Sim = "StateEnd", verbose = FALSE)
OM_w <- FUN(InputsModel = IM, RunOptions = RO_w, Param = param(ps[1, ]))
st <- OM_w$StateEnd
# airGR's StateEnd (class OutputsModel states): nested lists and numeric vectors — serialised as is, so a later run
# can rebuild it with CreateIniStates / jsonlite::fromJSON whatever the airGR layout is
state <- list(schema = "geohydroai.gr4h_state/v1", model = MODEL, time_step = cfg$time_step, at = cfg$warmup_end, param_set = ps$id[1],
              airgr = as.character(packageVersion("airGR")), states = unclass(st))
writeLines(jsonlite::toJSON(state, auto_unbox = TRUE, digits = 10), file.path(wd, "state.json"))

# members
m <- cfg$members
set.seed(if (is.null(m$seed)) 20261017L else as.integer(m$seed))
rdirichlet <- function(n, a) { g <- rgamma(n, shape = a); if (sum(g) == 0) rep(1 / n, n) else g / sum(g) }
blocks <- if (!is.null(f$block_id)) f$block_id else format(dates, "%Y-%m-%d")
hyetos <- list(list(id = "even", alpha = NA, P = f$P_mm[run]))
if (identical(m$mode, "ensemble") && !daily) {
  for (a in m$alphas) for (k in seq_len(m$n_per_alpha)) {
    P <- f$P_mm
    for (b in unique(blocks[run])) { h <- run[blocks[run] == b]; tot <- sum(P[h]); if (tot > 0) P[h] <- tot * rdirichlet(length(h), a) }
    hyetos[[length(hyetos) + 1]] <- list(id = sprintf("a%g_%02d", a, k), alpha = a, P = P[run])
  }
}
depths <- if (identical(m$mode, "ensemble")) m$depth_factors else 1.0
sets <- if (identical(m$mode, "ensemble") && identical(m$param_sets, "behavioural")) seq_len(nrow(ps)) else 1L
Q <- list(); rows <- list(); n <- 0
for (d in depths) for (hy in hyetos) {
  P <- f$P_mm; P[run] <- hy$P * d
  IMm <- mk(P)
  ROm <- CreateRunOptions(FUN_MOD = FUN, InputsModel = IMm, IndPeriod_WarmUp = warm, IndPeriod_Run = run, verbose = FALSE)
  for (s in sets) {
    OM <- FUN(InputsModel = IMm, RunOptions = ROm, Param = param(ps[s, ]))
    n <- n + 1
    id <- sprintf("m%04d", n - 1)
    Q[[id]] <- round(OM$Qsim, 5)
    rows[[n]] <- data.frame(member = id, set = ps$id[s], hyeto = hy$id, alpha = hy$alpha, depth = d)
  }
}
out <- data.frame(time_utc = format(dates[run], "%Y-%m-%dT%H:%M:%SZ"))
for (id in names(Q)) out[[id]] <- Q[[id]]
write.csv(out, file.path(wd, "qsim.csv"), row.names = FALSE)
write.csv(do.call(rbind, rows), file.path(wd, "members.csv"), row.names = FALSE)
writeLines(jsonlite::toJSON(list(R = R.version.string, airGR = as.character(packageVersion("airGR")), members = n,
                                 model = MODEL, pet = if (!is.null(f$PET_mm) && !all(is.na(f$PET_mm))) "forcing" else if (daily) "Oudin(daily mean T)" else "Oudin(daily mean T → 24 h)"),
                            auto_unbox = TRUE), file.path(wd, "versions.json"))
cat(sprintf("members %d, run steps %d (%s)\n", n, length(run), cfg$time_step))
