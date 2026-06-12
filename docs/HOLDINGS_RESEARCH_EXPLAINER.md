# Cross-BDC Holdings & Mark Comparison — Plain-English Explainer

*A guide for colleagues. No technical background needed.*

---

## 1. The question we set out to answer

A **BDC** (Business Development Company) is, in plain terms, a publicly-reported fund that lends
money to mid-sized private companies. Each BDC has to disclose, in its SEC filings, a full
loan-by-loan list of what it owns — the borrower, the loan terms, and **how much that loan is
currently worth on the fund's books**.

That last number is the interesting one. These are private loans with no public trading price, so
each fund's managers have to **estimate** what each loan is worth. They express it as a "**mark**" —
a price as cents on the dollar of the loan's face value:

- A mark of **100** = the fund values the loan at full face value.
- A mark of **90** = the fund has written it down 10% (it thinks it's worth less, often a sign of
  trouble at the borrower).

Here's the key fact: **the same loan is often held by many different BDCs at once.** Big private
loans are split among a handful of lenders (a "club deal"). So we can line up the same loan across
all the funds that hold it and ask:

> **Do different funds value the identical loan the same way — and when they disagree, who's the
> outlier, and is a credit quietly being marked down before the others catch up?**

That disagreement is the signal. It can reveal which managers are aggressive vs. cautious, and it
can be an **early warning** when one holder marks a loan down before the rest of the market does.

---

## 2. Why this is hard (and why it needed real work)

You'd think you could just match loans by the borrower's name. You can't, because the filings are
messy in three ways:

1. **The same company is written a dozen different ways.** One fund writes "Anaplan, Inc.", another
   writes "Anaplan", another buries it inside a long category heading like *"Non-Controlled
   Investments – Software – Anaplan – First Lien Term Loan."* To a computer these look like
   different companies.
2. **One company can have several different loans.** A borrower might have a main loan, a backup
   credit line (revolver), and a delayed-draw loan — each valued differently. Comparing a fund's
   main loan against another fund's backup line would be apples-to-oranges.
3. **The raw numbers have traps.** For example, a backup credit line that's only partly drawn can
   look like it's been written down 80% when really it's just mostly unused.

So the work was: **clean up the mess, group the same company together, separate its different loans,
and only then compare the values — carefully.**

---

## 3. What we did, stage by stage

We built this in five stages. Think of it as turning a pile of 375,000 messy loan entries into a
clean, comparable picture.

### Stage 1 — Gather and clean ("read every filing into one big table")
We pulled the loan lists out of every BDC's filings — **375,530 individual loan entries across 74
funds** — into one table. Then we cleaned each entry: pulled the real company name out of the messy
text, figured out the loan type (main loan vs. revolver, senior vs. junior), and calculated each
loan's mark (value as cents on the dollar). *Result: we could read a usable company name on **97%**
of entries.*

### Stage 2 — Group the same company together ("a smart spell-checker for company names")
We taught the system that "Anaplan, Inc.", "Anaplan", and the buried-in-a-heading version are all
the **same borrower**. This collapsed the 26,759 different spellings down to **15,149 distinct
companies**. We checked it against a list of well-known widely-held loans (Anaplan, Flexera,
Finastra, etc.) to confirm it grouped them correctly and didn't accidentally merge unrelated
companies.

### Stage 3 — Separate each company's different loans ("tell the loans apart")
Within each company, we separated the distinct loans using the things that stay fixed for a given
loan — its seniority (who gets paid back first) and its interest-rate spread. We also used the
maturity date where filings provided it. Each distinct loan got a **confidence rating** (how sure we
are the match is clean). *Result: **123,961 distinct loans**, of which **~23,000 are held by two or
more funds** — those are the ones we can actually compare.*

### Stage 4 — Compare the values ("line up who marks it where")
For each loan held by multiple funds, we lined up every holder's mark side by side, found the
typical (median) value, and measured how far apart the funds are. We were careful to throw out the
data traps (like the partly-drawn-credit-line problem) so a glitch couldn't masquerade as a real
disagreement.

### Stage 5 — Package it for use ("the report")
We assembled everything into one Excel workbook (described below), including coverage statistics, a
sample for spot-checking, and a quarter-by-quarter trend view to spot loans being marked down over
time.

---

## 4. Does it actually work? (Real examples it found)

The proof is that it surfaced **real, recognizable situations** straight from the raw filings:

- **Pluralsight** — Ares Capital valued this loan at **73.5** while four Blue Owl funds held it at
  **97.7**. This was a well-known real case of lenders disagreeing sharply before the company
  restructured. The tool found it with no prior knowledge.
- **YA Intermediate** — Blackstone funds marked it at **59**, HPS at **89**, T. Rowe at **99** — a
  40-point disagreement among managers on the very same loan.
- **First Brands** — the trend view caught its mark falling **54 points** over successive quarters,
  and **Naviga** sliding steadily from 100 down to 64 — exactly the early-warning pattern we wanted.
- And as a sanity check, healthy widely-held loans (Anaplan, Avalara, Integrity Marketing) all show
  up at full value with the funds in tight agreement — which is what *should* happen.

---

## 5. The "holdings" files — what each one is

These all live in the project's `data` folder. From rawest to most finished:

| File | What it is | Who uses it |
|---|---|---|
| **`data/holdings/` (many CSVs)** | The **raw** loan lists, one file per SEC filing, straight from the filings with almost no cleanup. The original source material. | Rarely opened directly; it's the input. |
| **`data/dataset/holdings_consolidated.csv`** | All those raw files **combined into one table and cleaned** — with the parsed company name, the grouped-company label, loan type, and the calculated mark added as columns. (375,530 rows.) | Analysts who want every loan entry with the cleaned-up fields. |
| **`data/dataset/holdings_matched.csv`** | The same table, but with each entry **tagged to the specific loan it belongs to** and labeled debt / equity / undrawn. Lets you trace any comparison back to the exact underlying entries. | Anyone auditing how a particular comparison was built. |
| **`data/dataset/issues.csv`** | **One row per distinct loan** (not per entry) — the summary level. Shows the company, loan terms, how many funds hold it, the typical mark, and how far apart the holders are. (123,961 rows.) | The natural starting point for analysis — sort it to find disagreements. |
| **`data/dataset/holdings_marks_comparison.xlsx`** | **The finished report** — a 9-tab Excel workbook (next section). | The deliverable to share and present. |

*(You may also see `holdings_clustered.csv` — that's an earlier working snapshot, now superseded by
`holdings_consolidated.csv`. Ignore it.)*

---

## 6. The report workbook — what each tab does

`holdings_marks_comparison.xlsx` — open this one. Marks are shown in **points of par** (100 = full
value).

| Tab | What it shows | How to use it |
|---|---|---|
| **Overview** | Plain-language description, the caveats, and headline counts. | Read first. |
| **Dispersion** | The payoff: loans where the funds **disagree most** on value, ranked. | Start here to find interesting situations. |
| **Consensus** | Widely-held loans where the funds **agree** (tight values). | The "market consensus" view; also a sanity check. |
| **HolderDetail** | For the disagreed-on loans, **each individual fund's mark** vs. the group's typical value, flagged "Rich" (marks high) or "Cheap" (marks low). | The actionable "who's the outlier" view. |
| **IssuerSummary** | A roll-up **by company** (rather than by individual loan) — the broader view of a borrower across all its loans. | When you care about a company overall, not one loan. |
| **Anchors** | Well-known widely-held loans, shown for **validation** — proof the method groups and prices them correctly. | Use to build trust in the numbers. |
| **Coverage** | The **stats**: how much of the data we could match, how confident, and how the disagreements are distributed. | For methodology questions ("how complete is this?"). |
| **ReviewSample** | A **spot-check sample** across confidence levels, with a blank "Verdict" column. | Hand-verify a sample to confirm quality. |
| **Trend** | Each company's **median mark** (the middle value across the funds holding it, shown only when **≥3 funds** hold it) **quarter by quarter** — sorted to put the biggest declines on top. We use the median, not the average, so one bad data point can't fake a decline. | The **early-warning** view; this is where First Brands / Naviga surfaced. |

---

## 7. What this is — and what it isn't (please state these when presenting)

- It is **best-effort matching with confidence levels**, not an exact, audited reconciliation. The
  payoff is surfacing disagreements for a human to investigate — not a claim that every loan is
  perfectly matched.
- Marks are **managers' estimates** of illiquid private loans. Some disagreement is normal and
  legitimate (different timing, different information); the goal is to spot the *unusual* gaps.
- We only compare funds reporting on the **same date**. Funds with different fiscal quarter-ends
  won't line up against each other — a coverage limit, not an error.
- Coverage is partial and honestly reported: about **41% of the loans with usable prices** land in a
  clean, multi-fund comparison. Entries we couldn't confidently match are **left out and counted**,
  never forced.

---

*Built from public SEC filings. Underlying method and code: `src/analysis/holdings_compare.py`;
full technical plan: `docs/HOLDINGS_COMPARISON_PLAN.md`.*
