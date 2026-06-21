"""
bdc_churn.py — BDC churn & survival analysis (Phase 0 census + Phase 1 descriptive churn).

Phase 0: assemble a one-row-per-BDC census from the EDGAR birth/death pulls
(N-54A = election, N-54C = withdrawal), joined to manager + vehicle_type where known.
Phase 1: descriptive churn — active-fund count over time, births/deaths/net per year,
annual churn (attrition) rate, long-run hazard rate, and observed median lifespan.

Later phases (mechanism classification, survival curves, family churn, workbook) build on
the census written here.

Reads : data/churn_births_raw.csv, data/churn_deaths_raw.csv (from churn_sizing.py),
        data/fund_universe.csv, data/dataset/fund_manager_map.csv
Writes: data/dataset/bdc_churn_census.csv

Run: uv run python src/analysis/bdc_churn.py
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BIRTHS = ROOT / "data" / "churn_births_raw.csv"
DEATHS = ROOT / "data" / "churn_deaths_raw.csv"
UNIVERSE = ROOT / "data" / "fund_universe.csv"
MANAGERS = ROOT / "data" / "dataset" / "fund_manager_map.csv"
CENSUS_OUT = ROOT / "data" / "dataset" / "bdc_churn_census.csv"
ENRICHED = ROOT / "data" / "dataset" / "bdc_churn_census_enriched.csv"
OUT_WB = ROOT / "data" / "dataset" / "bdc_churn.xlsx"
BDC_VEHICLES = {"Unlisted BDC", "Listed BDC"}   # confirmed-real universe tags

TODAY = date(2026, 6, 20)
WINDOW_START = date(1996, 1, 1)   # EDGAR electronic-filing floor


def _d(s):
    """Parse an ISO date string to a date, or None."""
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def build_census() -> pd.DataFrame:
    b = pd.read_csv(BIRTHS, dtype={"cik": str})
    d = pd.read_csv(DEATHS, dtype={"cik": str})
    b["cik"] = b["cik"].str.zfill(10)
    d["cik"] = d["cik"].str.zfill(10)

    # birth = EARLIEST N-54A per CIK; death = LATEST N-54C per CIK
    birth = b.sort_values("date").drop_duplicates("cik", keep="first").set_index("cik")
    death = d.sort_values("date").drop_duplicates("cik", keep="last").set_index("cik")

    ciks = sorted(set(birth.index) | set(death.index))
    rows = []
    for cik in ciks:
        bd = _d(birth.loc[cik, "date"]) if cik in birth.index else None
        dd = _d(death.loc[cik, "date"]) if cik in death.index else None
        name = (birth.loc[cik, "company"] if cik in birth.index
                else death.loc[cik, "company"])
        # alive unless it has a withdrawal that post-dates any re-election
        dead = dd is not None and (bd is None or dd >= bd)
        status = "dead" if dead else "alive"
        birth_censored = dead and bd is None      # died but born pre-EDGAR
        # lifespan / age (years)
        if dead and bd is not None:
            life = (dd - bd).days / 365.25
        elif dead and bd is None:
            life = None                            # unknown start
        else:  # alive
            life = (TODAY - bd).days / 365.25 if bd else None
        rows.append({"cik": cik, "fund_name": name, "status": status,
                     "birth_date": bd.isoformat() if bd else "",
                     "death_date": dd.isoformat() if dead and dd else "",
                     "birth_censored": birth_censored,
                     "lifespan_years": round(life, 2) if life is not None else "",
                     "censored_alive": status == "alive"})
    census = pd.DataFrame(rows)

    # Join manager + vehicle_type where known (coverage is partial — our curated set)
    uni = pd.read_csv(UNIVERSE, dtype={"cik": str})
    uni["cik"] = uni["cik"].fillna("").str.strip().str.zfill(10)
    vt = uni.drop_duplicates("cik").set_index("cik")["vehicle_type"]
    census["vehicle_type"] = census["cik"].map(vt).fillna("")
    if MANAGERS.exists():
        mg = pd.read_csv(MANAGERS, dtype={"cik": str})
        mg["cik"] = mg["cik"].str.zfill(10)
        mmap = mg.drop_duplicates("cik").set_index("cik")["manager"]
        census["manager"] = census["cik"].map(mmap).fillna("")
    else:
        census["manager"] = ""
    return census


def phase1(census: pd.DataFrame) -> None:
    dead = census[census["status"] == "dead"]
    alive = census[census["status"] == "alive"]
    n_total = len(census)
    n_dead, n_alive = len(dead), len(alive)
    n_lcens = int(census["birth_censored"].sum())

    print("=" * 64)
    print("PHASE 1 — DESCRIPTIVE CHURN")
    print(f"  Distinct BDCs ever seen: {n_total}  | alive {n_alive}  | dead {n_dead}")
    print(f"  Crude survival rate: {n_alive / n_total:.1%}")
    print(f"  Left-censored deaths (born pre-EDGAR): {n_lcens}")

    # Per-year births / deaths / active count / churn rate
    by = census.copy()
    by["birth_yr"] = by["birth_date"].str[:4]
    by["death_yr"] = by["death_date"].str[:4]
    births_y = by[by["birth_yr"] != ""].groupby("birth_yr").size()
    deaths_y = by[by["death_yr"] != ""].groupby("death_yr").size()

    init_stock = n_lcens            # funds alive at the window start (left-censored set)
    print(f"\n  Initial active stock at {WINDOW_START.year} (left-censored alive-at-start): {init_stock}")
    print(f"\n  {'Year':4s} {'Births':>6s} {'Deaths':>6s} {'Net':>5s} {'Active':>7s} {'Churn%':>7s}")
    active = init_stock
    total_deaths = 0
    for yr in range(1996, TODAY.year + 1):
        ys = str(yr)
        bi = int(births_y.get(ys, 0))
        de = int(deaths_y.get(ys, 0))
        start = active
        active = active + bi - de
        total_deaths += de
        rate = (de / start) if start > 0 else 0.0
        print(f"  {ys:4s} {bi:6d} {de:6d} {bi-de:5d} {active:7d} {rate:7.1%}")

    # Long-run hazard = deaths / total fund-years of exposure in [WINDOW_START, TODAY]
    fund_years = 0.0
    for _, r in census.iterrows():
        bd = _d(r["birth_date"]) or WINDOW_START
        start = max(bd, WINDOW_START)
        end = _d(r["death_date"]) if r["status"] == "dead" else TODAY
        if end is None:
            end = TODAY
        fund_years += max((end - start).days, 0) / 365.25
    hazard = total_deaths / fund_years if fund_years else 0.0
    print(f"\n  Total fund-years of exposure (1996+): {fund_years:,.0f}")
    print(f"  Long-run hazard (deaths / fund-years): {hazard:.2%} per year")
    print(f"  Implied mean lifespan (1/hazard): {1/hazard:,.1f} years" if hazard else "")

    # Observed lifespan among funds with a full record (born & died on EDGAR)
    full = dead[dead["birth_censored"] == False]
    lifes = pd.to_numeric(full["lifespan_years"], errors="coerce").dropna()
    if len(lifes):
        print(f"\n  Observed lifespan (n={len(lifes)} born-and-died on record):")
        print(f"    median {lifes.median():.1f} yrs | mean {lifes.mean():.1f} | "
              f"min {lifes.min():.1f} | max {lifes.max():.1f}")
        print("    (descriptive only — excludes survivors & left-censored; KM survival in Phase 3)")

    # Coverage of the descriptive joins
    print(f"\n  Join coverage: vehicle_type known {int((census['vehicle_type']!='').sum())}/{n_total}"
          f" | manager known {int((census['manager']!='').sum())}/{n_total}")


# ----- Kaplan-Meier survival (dependency-free, handles right-censoring) -----

def _km(rows):
    """rows = list of (duration_years, event 1/0). Returns the step curve [(t, S), ...]."""
    times = sorted({d for d, e in rows if e == 1})
    S, curve = 1.0, []
    for t in times:
        at_risk = sum(1 for d, _ in rows if d >= t)
        d_t = sum(1 for d, e in rows if d == t and e == 1)
        if at_risk:
            S *= 1 - d_t / at_risk
        curve.append((t, S))
    return curve


def _median(curve):
    for t, S in curve:
        if S <= 0.5:
            return t
    return None


def _surv_at(curve, h):
    s = 1.0
    for t, S in curve:
        if t <= h:
            s = S
        else:
            break
    return s


def _dur_event(r):
    """(duration in years, event) for a census row with a known birth date."""
    bd = _d(r["birth_date"])
    if r["status"] == "dead" and r["death_date"]:
        return (_d(r["death_date"]) - bd).days / 365.25, 1
    return (TODAY - bd).days / 365.25, 0


def phase3(bona: pd.DataFrame) -> None:
    print("=" * 64)
    print("PHASE 3 — SURVIVAL ANALYSIS (Kaplan-Meier, time since BDC election)")
    sub = bona[bona["birth_date"] != ""].copy()
    print(f"  n={len(sub)} bona-fide BDCs with a known birth date "
          f"(excluded {len(bona) - len(sub)} left-censored)")
    curve = _km([_dur_event(r) for _, r in sub.iterrows()])
    med = _median(curve)
    print(f"  median survival: {f'{med:.1f} yrs' if med else 'not reached (>50% still alive)'}")
    print("  " + "  ".join(f"S({h})={_surv_at(curve, h):.0%}" for h in (3, 5, 10, 15, 20)))
    print("\n  by listing status:")
    for lv in ("listed", "unlisted"):
        s = sub[sub["listed"] == lv]
        if len(s) < 5:
            continue
        c = _km([_dur_event(r) for _, r in s.iterrows()])
        m = _median(c)
        print(f"    {lv:9s} n={len(s):3d}  median={f'{m:.1f}y' if m else 'n/r':>5s}  "
              f"S(5)={_surv_at(c, 5):.0%}  S(10)={_surv_at(c, 10):.0%}  S(15)={_surv_at(c, 15):.0%}")
    print("\n  by launch cohort (birth decade):")
    sub["decade"] = sub["birth_date"].str[:3] + "0s"
    for dec in sorted(sub["decade"].unique()):
        s = sub[sub["decade"] == dec]
        if len(s) < 5:
            continue
        c = _km([_dur_event(r) for _, r in s.iterrows()])
        m = _median(c)
        print(f"    {dec:6s} n={len(s):3d}  median={f'{m:.1f}y' if m else 'n/r':>5s}  "
              f"S(5)={_surv_at(c, 5):.0%}  S(10)={_surv_at(c, 10):.0%}")


def phase4(bona: pd.DataFrame) -> None:
    print("=" * 64)
    print("PHASE 4 — FAMILY (MANAGER) CHURN")
    fam = (bona.groupby("manager_family")
           .apply(lambda d: pd.Series({
               "launched": len(d),
               "alive": int((d["status"] == "alive").sum()),
               "dead": int((d["status"] == "dead").sum()),
               "mergers": int((d["mechanism"] == "merger").sum()),
           }), include_groups=False)
           .reset_index())
    fam["survival"] = fam["alive"] / fam["launched"]
    multi = fam[fam["launched"] >= 2].sort_values(["launched", "survival"], ascending=[False, True])
    print(f"  families: {len(fam)} | multi-fund (>=2): {len(multi)} | "
          f"single-fund: {int((fam['launched'] == 1).sum())}")
    print(f"  intra-family mergers (a merger inside a >=2-fund family): "
          f"{int(multi['mergers'].sum())} — the consolidation/rollup pattern")
    print("\n  top families by launches (serial launchers):")
    print(f"    {'family':26s} {'launched':>8s} {'alive':>5s} {'dead':>4s} {'merged':>6s} {'surv':>5s}")
    for _, r in multi.head(18).iterrows():
        print(f"    {str(r['manager_family'])[:26]:26s} {int(r.launched):8d} {int(r.alive):5d} "
              f"{int(r.dead):4d} {int(r.mergers):6d} {r.survival:5.0%}")


# ----- Curated canonical manager families (fixes name-token splits/misgroupings) -----
# Applied by regex on the fund name; takes precedence over the heuristic manager_family.
# Consolidates same-manager funds the tokenizer split (BlackRock/BlackRock Kelso/TCP;
# FS Investment/FS Energy/Corporate Capital Trust -> FS/KKR; Owl Rock -> Blue Owl).
import re as _re
CURATED_FAMILY = [
    (r"owl rock|blue owl", "Blue Owl"),
    (r"fs investment|fs energy|fs kkr|fs specialty|fs series|corporate capital trust", "FS/KKR"),
    (r"blackrock|tcp capital", "BlackRock"),
    (r"\bares\b", "Ares"),
    (r"apollo", "Apollo"),
    (r"bain capital", "Bain Capital"),
    (r"blackstone|\bgso\b", "Blackstone"),
    (r"business development corp(oration)? of america|franklin bsp|benefit street", "Franklin BSP"),
    (r"carey credit", "Carey Credit"),
    (r"crescent", "Crescent"),
    (r"golub", "Golub"),
    (r"gladstone", "Gladstone"),
    (r"kayne anderson", "Kayne Anderson"),
    (r"main street|hms income|msc income", "Main Street"),
    (r"new mountain", "New Mountain"),
    (r"nuveen churchill|churchill", "Nuveen Churchill"),
    (r"oaktree|fifth street", "Oaktree"),
    (r"pennantpark", "PennantPark"),
    (r"prospect capital", "Prospect"),
    (r"thl credit|thl investment", "THL Credit"),
    (r"allied capital|allied investment", "Allied Capital"),
    (r"berthel", "Berthel"),
    (r"excelsior", "Excelsior"),
    (r"goldman sachs", "Goldman Sachs"),
    (r"morgan stanley", "Morgan Stanley"),
    (r"hercules", "Hercules"),
    (r"barings", "Barings"),
    (r"sierra income|medley", "Medley"),
]
_CURATED_RX = [(_re.compile(rx, _re.I), name) for rx, name in CURATED_FAMILY]


def canonical_family(name: str, fallback: str) -> str:
    for rx, fam in _CURATED_RX:
        if rx.search(str(name)):
            return fam
    return fallback or str(name)


# ----- Phase 5: data tables + workbook -----

def yearly_table(census: pd.DataFrame) -> pd.DataFrame:
    by = census.copy()
    by["birth_yr"] = by["birth_date"].str[:4]
    by["death_yr"] = by["death_date"].str[:4]
    births_y = by[by["birth_yr"] != ""].groupby("birth_yr").size()
    deaths_y = by[by["death_yr"] != ""].groupby("death_yr").size()
    active = int(census["birth_censored"].sum())
    rows = []
    for yr in range(1996, TODAY.year + 1):
        ys = str(yr)
        bi, de = int(births_y.get(ys, 0)), int(deaths_y.get(ys, 0))
        start = active
        active += bi - de
        rows.append({"year": yr, "births": bi, "deaths": de, "net": bi - de,
                     "active": active, "churn_pct": round(de / start, 4) if start else 0.0})
    return pd.DataFrame(rows)


def km_curve_df(sub: pd.DataFrame) -> pd.DataFrame:
    curve = _km([_dur_event(r) for _, r in sub.iterrows()])
    return pd.DataFrame(curve, columns=["years", "survival"])


def mechanism_table(bona: pd.DataFrame) -> pd.DataFrame:
    dead = bona[bona["status"] == "dead"]
    t = pd.crosstab(dead["mechanism"], dead["listed"])
    t["total"] = t.sum(axis=1)
    return t.reset_index()


def family_table(bona: pd.DataFrame) -> pd.DataFrame:
    fam = (bona.groupby("manager_family")
           .apply(lambda d: pd.Series({
               "launched": len(d),
               "alive": int((d["status"] == "alive").sum()),
               "dead": int((d["status"] == "dead").sum()),
               "mergers": int((d["mechanism"] == "merger").sum()),
           }), include_groups=False)
           .reset_index())
    fam["survival"] = (fam["alive"] / fam["launched"]).round(3)
    return fam.sort_values(["launched", "survival"], ascending=[False, True])


def _fig_png(fig):
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    buf.seek(0)
    return buf


def build_workbook(census: pd.DataFrame, bona: pd.DataFrame) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.utils.dataframe import dataframe_to_rows

    yt = yearly_table(bona)
    mech = mechanism_table(bona)
    fam = family_table(bona)
    sub = bona[bona["birth_date"] != ""].copy()
    km_all = km_curve_df(sub)
    km_l = km_curve_df(sub[sub["listed"] == "listed"])
    km_u = km_curve_df(sub[sub["listed"] == "unlisted"])

    n_alive = int((bona["status"] == "alive").sum())
    n_dead = int((bona["status"] == "dead").sum())
    overview = pd.DataFrame({
        "metric": ["Bona-fide BDCs (N-2 or universe)", "Alive", "Dead", "Crude survival rate",
                   "KM median lifespan (yrs)", "Long-run hazard (per yr)",
                   "Deaths: merger", "Deaths: liquidation", "Deaths: conversion",
                   "Deaths: scheduled wind-down", "Deaths: unknown (verify)",
                   "Universe note"],
        "value": [len(bona), n_alive, n_dead, f"{n_alive/len(bona):.1%}",
                  _median(list(km_all.itertuples(index=False, name=None))) or "not reached",
                  f"{n_dead / max(yearly_table(bona)['active'].mean(),1):.1%} (approx)",
                  int((bona['mechanism'] == 'merger').sum()),
                  int((bona['mechanism'] == 'liquidation').sum()),
                  int((bona['mechanism'] == 'conversion').sum()),
                  int((bona['mechanism'] == 'scheduled_winddown').sum()),
                  int((bona['mechanism'] == 'unknown').sum()),
                  "Bona-fide = filed Form N-2 (investment-company registration) or a "
                  "confirmed universe BDC. Births N-54A, deaths N-54C; left-censored pre-1996."]})

    census_cols = ["cik", "fund_name", "manager_family", "listed", "status", "birth_date",
                   "death_date", "lifespan_years", "mechanism", "mech_confidence", "has_n2"]
    with pd.ExcelWriter(OUT_WB, engine="openpyxl") as xw:
        overview.to_excel(xw, sheet_name="Overview", index=False)
        yt.to_excel(xw, sheet_name="ByYear", index=False)
        mech.to_excel(xw, sheet_name="Mechanism", index=False)
        fam.to_excel(xw, sheet_name="Family", index=False)
        km_all.to_excel(xw, sheet_name="Survival", index=False, startcol=0)
        km_l.to_excel(xw, sheet_name="Survival", index=False, startcol=3)
        km_u.to_excel(xw, sheet_name="Survival", index=False, startcol=6)
        bona[census_cols].to_excel(xw, sheet_name="Census", index=False)

        ch = xw.book.create_sheet("Charts")
        # 1) active count + births/deaths over time
        fig, ax = plt.subplots(figsize=(8, 3.5))
        ax.bar(yt["year"], yt["births"], color="#3a7", label="births", alpha=.7)
        ax.bar(yt["year"], -yt["deaths"], color="#c44", label="deaths", alpha=.7)
        ax2 = ax.twinx(); ax2.plot(yt["year"], yt["active"], "k-", lw=2, label="active count")
        ax.set_title("BDC births / deaths and active count"); ax.legend(loc="upper left")
        ax2.legend(loc="lower right")
        ch.add_image(XLImage(_fig_png(fig)), "A1")
        # 2) churn rate over time
        fig, ax = plt.subplots(figsize=(8, 3))
        ax.plot(yt["year"], yt["churn_pct"] * 100, "o-", color="#c44")
        ax.set_title("Annual churn rate (%)"); ax.set_ylabel("%")
        ch.add_image(XLImage(_fig_png(fig)), "A20")
        # 3) KM survival listed vs unlisted
        fig, ax = plt.subplots(figsize=(8, 3.5))
        for d, lab, c in [(km_all, "all", "k"), (km_l, "listed", "#3a7"), (km_u, "unlisted", "#c44")]:
            if len(d):
                ax.step(d["years"], d["survival"], where="post", label=lab, color=c, lw=2)
        ax.set_title("Kaplan-Meier survival (time since BDC election)")
        ax.set_xlabel("years"); ax.set_ylabel("survival"); ax.set_ylim(0, 1); ax.legend()
        ch.add_image(XLImage(_fig_png(fig)), "A40")
        # 4) death mechanism
        fig, ax = plt.subplots(figsize=(6, 3))
        md = bona[bona["status"] == "dead"]["mechanism"].value_counts()
        ax.bar(md.index, md.values, color="#47a"); ax.set_title("Death mechanism (bona-fide)")
        plt.xticks(rotation=20, ha="right")
        ch.add_image(XLImage(_fig_png(fig)), "A60")

    print(f"Wrote workbook: {OUT_WB}")


def main() -> None:
    if not ENRICHED.exists():
        # bootstrap: raw census + descriptive churn only (run churn_enrich.py for the rest)
        census = build_census()
        CENSUS_OUT.parent.mkdir(parents=True, exist_ok=True)
        census.to_csv(CENSUS_OUT, index=False)
        print(f"Wrote census: {CENSUS_OUT}  ({len(census)} BDCs)\n")
        phase1(census)
        print("\n(enriched census not found — run churn_enrich.py for Phases 2-4)")
        return

    census = pd.read_csv(ENRICHED, dtype={"cik": str})
    census["cik"] = census["cik"].str.zfill(10)
    for c in ("birth_date", "death_date", "vehicle_type", "listed", "manager_family", "mechanism"):
        census[c] = census[c].fillna("") if c in census else ""
    census["birth_censored"] = census["birth_censored"].astype(str).isin(["True", "true", "1"])
    # bona-fide = filed Form N-2 OR a confirmed universe BDC (recovers non-offering affiliates)
    hn2 = census["has_n2"].astype(str).isin(["True", "true", "1"])
    census["bona_fide"] = hn2 | census["vehicle_type"].isin(BDC_VEHICLES)
    # canonical manager family (consolidate name-token splits / misgroupings)
    census["manager_family"] = census.apply(
        lambda r: canonical_family(r["fund_name"], r["manager_family"]), axis=1)
    census.to_csv(ENRICHED, index=False)   # persist bona_fide + cleaned family

    bona = census[census["bona_fide"]].copy()
    print(f"Enriched census: {len(census)} BDCs | bona-fide (N-2 or universe BDC): {len(bona)}\n")
    phase1(bona)
    print()
    phase3(bona)
    print()
    phase4(bona)
    print()
    build_workbook(census, bona)


if __name__ == "__main__":
    main()
