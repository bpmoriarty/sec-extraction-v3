"""
managers.py — Fund -> parent asset-manager mapping for the holdings research.

The marking-bias study rolls every BDC vehicle up to its parent manager (e.g. all five Blue Owl
funds -> "Blue Owl"), so this mapping is the linchpin: a wrong assignment would silently corrupt a
manager's rankings. It is therefore curated BY CIK (stable, unambiguous) and emitted for human
review (`--review` writes data/dataset/fund_manager_map.csv and prints the groupings).

Entries flagged in VERIFY are ones where the "parent manager" is debatable — advised-by
relationships, joint ventures, or platforms I'm not certain about. Please confirm/correct these.

Run:  uv run python src/analysis/managers.py --review
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONSOLIDATED = PROJECT_ROOT / "data" / "dataset" / "holdings_consolidated.csv"
OUT = PROJECT_ROOT / "data" / "dataset" / "fund_manager_map.csv"

# CIK (10-digit, zero-padded) -> parent manager.
MANAGER_BY_CIK: dict[str, str] = {
    "0001982701": "AllianceBernstein",       # AB Private Lending Fund
    "0001287750": "Ares",                     # Ares Capital Corp
    "0001918712": "Ares",                     # Ares Strategic Income Fund
    "0001822523": "Advanced Flower Capital",  # Advanced Flower Capital (AFC Gamma)
    "0001976336": "Antares",                  # Antares Private Credit Fund
    "0001837532": "Apollo",                   # Apollo Debt Solutions BDC
    "0001372807": "BC Partners",              # BCP Investment Corp  *** VERIFY ***
    "0001899017": "Bain Capital",             # Bain Capital Private Credit
    "0001655050": "Bain Capital",             # Bain Capital Specialty Finance
    "0001379785": "Barings",                  # Barings BDC
    "0001902649": "BlackRock",                # BlackRock Private Credit Fund
    "0001370755": "BlackRock",                # BlackRock TCP Capital Corp
    "0001803498": "Blackstone",               # Blackstone Private Credit Fund (BCRED)
    "0001736035": "Blackstone",               # Blackstone Secured Lending Fund (BXSL)
    "0001655888": "Blue Owl",                 # Blue Owl Capital Corp
    "0001655887": "Blue Owl",                 # Blue Owl Capital Corp II
    "0001812554": "Blue Owl",                 # Blue Owl Credit Income Corp
    "0001747777": "Blue Owl",                 # Blue Owl Technology Finance Corp
    "0001869453": "Blue Owl",                 # Blue Owl Technology Income Corp
    "0000017313": "Capital Southwest",        # Capital Southwest Corp (internally managed)
    "0001534254": "CION",                     # CION Investment Corp
    "0001544206": "Carlyle",                  # Carlyle Secured Lending
    "0001843162": "Chicago Atlantic",         # Chicago Atlantic BDC
    "0001633336": "Crescent",                 # Crescent Capital BDC
    "0001954360": "Crescent",                 # Crescent Private Credit Income Corp
    "0001513363": "Fidus",                    # Fidus Investment Corp
    "0001422183": "FS/KKR",                   # FS KKR Capital Corp (FS Investments + KKR JV)
    "0001899996": "Fidelity",                 # Fidelity Private Credit Co LLC
    "0001890107": "First Eagle",              # First Eagle Private Credit Fund (FEAC, ex-THL Credit)
    "0001495584": "Firsthand",                # Firsthand Technology Value Fund
    "0001825248": "Franklin BSP",             # Franklin BSP Capital (Benefit Street Partners)
    "0001143513": "Gladstone",                # Gladstone Capital Corp
    "0001321741": "Gladstone",                # Gladstone Investment Corp
    "0001476765": "Golub",                    # Golub Capital BDC
    "0001572694": "Goldman Sachs",            # Goldman Sachs BDC
    "0001930087": "Golub",                    # Golub Capital Private Credit Fund
    "0001675033": "Great Elm",                # Great Elm Capital Corp
    "0001838126": "HPS",                      # HPS Corporate Lending Fund
    "0001280784": "Hercules",                 # Hercules Capital
    "0001487428": "Horizon Technology",       # Horizon Technology Finance Corp
    "0001578348": "Investcorp",               # Investcorp Credit Management BDC
    "0001987221": "John Hancock",             # John Hancock Comvest (Manulife + Comvest) — Brian: "John Hancock"
    "0001747172": "Kayne Anderson",           # Kayne Anderson BDC
    "0001911321": "Kennedy Lewis",            # Kennedy Lewis Capital Co
    "0001535778": "Main Street Capital",      # MSC Income Fund (advised by Main Street)  *** VERIFY ***
    "0001396440": "Main Street Capital",      # Main Street Capital Corp
    "0001278752": "Apollo",                   # MidCap Financial Investment (advised by Apollo)  *** VERIFY ***
    "0001782524": "Morgan Stanley",           # Morgan Stanley Direct Lending Fund
    "0001496099": "New Mountain",             # New Mountain Finance Corp
    "0001737924": "Nuveen Churchill",         # Nuveen Churchill Direct Lending
    "0001911066": "Nuveen Churchill",         # Nuveen Churchill Private Capital Income Fund
    "0001487918": "OFS",                      # OFS Capital Corp
    "0001414932": "Oaktree",                  # Oaktree Specialty Lending
    "0001872371": "Oaktree",                  # Oaktree Strategic Credit Fund
    "0001259429": "Oxford Square",            # Oxford Square Capital Corp (ex-TICC)
    "0001383414": "PennantPark",              # PennantPark Investment Corp
    "0001923622": "PGIM",                     # PGIM Private Credit Fund
    "0000845385": "Princeton",                # Princeton Capital Corp
    "0001287032": "Prospect",                 # Prospect Capital Corp
    "0001794776": "Palmer Square",            # Palmer Square Capital BDC
    "0001504619": "PennantPark",              # PennantPark Floating Rate Capital
    "0001490349": "PhenixFIN",                # PhenixFIN Corp (ex-Medley)
    "0001521945": "Prospect",                 # Prospect Floating Rate & Alternative Income Fund
    "0000081955": "Rand",                     # Rand Capital Corp
    "0001653384": "Runway Growth",            # Runway Growth Finance Corp
    "0001377936": "Saratoga",                 # Saratoga Investment Corp
    "0001418076": "SLR",                      # SLR Investment Corp (Solar/SLR Capital Partners)
    "0001508655": "Sixth Street",             # Sixth Street Specialty Lending
    "0001551901": "Stellus",                  # Stellus Capital Investment Corp
    "0001901164": "T. Rowe Price / OHA",      # T. Rowe Price OHA Select (Oak Hill Advisors)
    "0001913724": "TPG Twin Brook",           # TPG Twin Brook Capital Income Fund — Brian: "TPG Twin Brook"
    "0001786108": "Trinity Capital",          # Trinity Capital Inc
    "0001580345": "TriplePoint",              # TriplePoint Venture Growth BDC
    "0001552198": "WhiteHorse (H.I.G.)",      # WhiteHorse Finance (H.I.G. Capital)

    # --- Deregistered BDCs — added session 15 (2026-07-02), curated by CIK. ---
    # These 26 dead BDCs were folded into the universe in session 14. Mapping them lets their
    # holdings contribute to their parent manager's marking-bias estimate. 18 have holdings (marked
    # * below); the other 8 have no schedule-of-investments XBRL, so they never enter the study.
    # Most roll into an existing manager; the three *** VERIFY *** cases are judgment calls (see VERIFY).
    "0001326003": "BlackRock",                # BlackRock Capital Investment Corp *  (merged into BlackRock TCP Capital)
    "0001807427": "Blue Owl",                 # Blue Owl Capital Corp III *
    "0001889668": "Blue Owl",                 # Blue Owl Technology Finance Corp. II *
    "0001851277": "Carlyle",                  # Carlyle Secured Lending III *
    "0001490927": "Franklin BSP",             # Franklin BSP Lending Corp *
    "0002018545": "Franklin BSP",             # Franklin BSP Real Estate Debt BDC *
    "0001865174": "Goldman Sachs",            # Goldman Sachs Middle Market Lending Corp. II *
    "0001715268": "Golub",                    # Golub Capital BDC 3, Inc. *
    "0001948565": "Investcorp",               # Investcorp US Institutional Private Credit Fund *
    "0001512931": "Monroe Capital",           # Monroe Capital Corp *
    "0001781870": "New Mountain",             # New Mountain Guardian III BDC, L.L.C. *
    "0002071136": "Nuveen Churchill",         # Nuveen Churchill BDC V *
    "0002022625": "Nuveen Churchill",         # Nuveen Churchill Private Credit Fund *
    "0001825590": "Morgan Stanley",           # SL Investment Corp * (merged into North Haven Private Income, an MS fund)
    "0001870267": "Redwood Capital Management",  # Redwood Enhanced Income Corp * (adviser: Redwood Capital Management LLC)
    "0001642862": "Western Technology Investment",  # Venture Lending & Leasing VIII, Inc. * (WTI / Westech Investment Advisors)
    "0001571329": "BC Partners",              # Logan Ridge Finance Corp * (Mount Logan Mgmt, a BC Partners affiliate; merged into BCP Investment Corp)  *** VERIFY ***
    "0001501729": "FS/KKR",                   # FS Specialty Lending Fund * (ex-FS Energy & Power Fund, FS/KKR-advised)  *** VERIFY ***
    "0001666384": "TPG Twin Brook",           # AG Twin Brook BDC (Twin Brook platform; Angelo Gordon-advised at exit, later TPG)  *** VERIFY ***
    "0001464963": "First Eagle",              # First Eagle Alternative Capital BDC (ex-THL Credit; no holdings)
    "0001618697": "Guggenheim",               # Guggenheim Credit Income Fund (no holdings)
    "0001618694": "Guggenheim",               # Guggenheim Credit Income Fund 2016 T (no holdings)
    "0001618696": "Guggenheim",               # Guggenheim Credit Income Fund 2019 (no holdings)
    "0001587987": "Newtek",                   # Newtek Business Services Corp (internally managed; became NewtekOne bank; no holdings)
    "0001922947": "Wellings Capital",         # Wellings Real Estate Income Fund (no holdings)
    "0001999538": "X1 Capital",               # X1 Capital Inc. (no holdings)
}

# The five originally-debatable cases were confirmed by Brian (2026-06-13):
#   BCP Investment Corp -> BC Partners; MidCap Financial -> keep Apollo; MSC Income -> Main Street;
#   John Hancock Comvest -> "John Hancock"; TPG Twin Brook -> "TPG Twin Brook".
# Session 15 (2026-07-02) added the 26 deregistered BDCs; three are debatable-parent judgment calls
# below (the rest are unambiguous). Please confirm/correct.
VERIFY: dict[str, str] = {
    "0001571329": "Logan Ridge Finance Corp -> BC Partners. Adviser is Mount Logan Management LLC, "
                  "an affiliate of BC Partners Advisors; LRFC merged into BCP Investment Corp (already "
                  "mapped to BC Partners). This ADDS to the existing BC Partners bias estimate — has holdings.",
    "0001501729": "FS Specialty Lending Fund -> FS/KKR. Formerly FS Energy & Power Fund, advised by the "
                  "FS/KKR JV. Adds to the FS/KKR estimate — has holdings.",
    "0001666384": "AG Twin Brook BDC -> TPG Twin Brook. Twin Brook lending platform; Angelo Gordon-advised "
                  "at its 2023 exit (pre TPG/Angelo Gordon deal). No holdings, so no marking-bias impact.",
}


def manager_of(cik: str) -> str:
    """Parent manager for a CIK (10-digit zero-padded). Falls back to the first word of nothing —
    unknown CIKs return 'UNMAPPED' so they're visible, never silently bucketed."""
    return MANAGER_BY_CIK.get(str(cik).zfill(10), "UNMAPPED")


def add_manager(df: pd.DataFrame, cik_col: str = "cik") -> pd.DataFrame:
    df = df.copy()
    df["manager"] = df[cik_col].astype(str).str.zfill(10).map(manager_of)
    return df


def _review() -> None:
    funds = (pd.read_csv(CONSOLIDATED, usecols=["cik", "fund_name"], dtype=str, low_memory=False)
             .drop_duplicates("cik"))
    funds["cik"] = funds["cik"].str.zfill(10)
    funds["manager"] = funds["cik"].map(manager_of)
    funds["verify"] = funds["cik"].map(lambda c: VERIFY.get(c, ""))
    funds = funds.sort_values(["manager", "fund_name"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    funds.to_csv(OUT, index=False, encoding="utf-8")

    n_mgr = funds["manager"].nunique()
    multi = funds.groupby("manager")["cik"].count()
    print(f"{len(funds)} funds -> {n_mgr} managers  ({(multi >= 2).sum()} multi-fund managers)\n")
    print("MULTI-FUND MANAGERS (where the rollup matters most):")
    for mgr, n in multi[multi >= 2].sort_values(ascending=False).items():
        names = funds.loc[funds["manager"] == mgr, "fund_name"].tolist()
        print(f"  {mgr:24} ({n})  {names}")
    unmapped = funds[funds["manager"] == "UNMAPPED"]
    if len(unmapped):
        print(f"\nUNMAPPED ({len(unmapped)}):")
        for _, r in unmapped.iterrows():
            print(f"  {r['cik']}  {r['fund_name']}")
    print("\n*** PLEASE VERIFY these (manager is debatable) ***")
    for cik, note in VERIFY.items():
        mgr = manager_of(cik)
        print(f"  [{mgr}]  {note}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    _review()
