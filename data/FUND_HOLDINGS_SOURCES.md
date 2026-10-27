# Fund holdings snapshot: report

Generated 2026-10-27. Every OK fund uses the AMC's own September 2026 monthly disclosure (as of 2026-09-30). That was the latest month-end file on each AMC site checked. All disclosures are at scheme level, so the Direct and Regular plans share one portfolio. No third-party data was used.

## 1. Parag Parikh Flexi Cap Fund: OK
- Source: amc.ppfas.com portfolio-disclosure page, file `PPFCF_PPFAS_Monthly_Portfolio_Report_September_30_2026.xlsx`
- Holdings: 125 rows. That is 33 equity, 23 arbitrage equity legs, 4 foreign equity, 3 REITs, 36 CD/CP/T-bills, 1 liquid fund, TREPS, net receivables, and 23 short stock futures.
- Weight sum: 98.55. The rows above the Grand Total add up to exactly 100. The short futures (-1.45) are hedges of the arbitrage legs and appear below the Grand Total in the file, so I kept them.
- The file stores % as a fraction, so I multiplied by 100. Eight rows are disclosed as "$0.00%" (under 0.01%) and recorded with a null weight.

## 2. Motilal Oswal Midcap Fund: OK
- Source: motilaloswalmf.com/downloads, combined workbook `Scheme Portfolio Details September 2026.xlsx`, sheet YO07
- Holdings: 32 (30 equity, TREPS, net receivables). Weight sum: 100.0

## 3. Kotak Small Cap Fund: UNAVAILABLE
- kotakmf.com blocks automated downloads with a bot-challenge, so I never got the file. The site does list a September 2026 folder for the scheme (named "Kotak Smallcap Fund").

## 4. Tata Digital India Fund: OK
- Source: tatamutualfund.com portfolio page, combined workbook `Monthly Portfolio as on 30th September 2026 (1).xlsx`, sheet TDIF
- Holdings: 54 (51 equity, TREPS, Repo, cash/net current assets). Weight sum: 100.0. The file states no derivative positions.

## 5. Kotak Nifty 200 Momentum 30 Index Fund: UNAVAILABLE
- Same Kotak bot-challenge block as #3.

## 6. SBI Nifty Smallcap 250 Index Fund: OK
- Source: sbimf.com/portfolios, file `sbi-nifty-smallcap-250-index-fund-monthly-portfolio---september-2026.xlsx`
- Holdings: 253 (249 equity, 2 REITs, TREPS, net payable). Weight sum: 100.0. No derivatives.

## 7. JioBlackRock Flexi Cap Fund: OK
- A full monthly disclosure does exist for this fund. Source: jioblackrockamc.com monthly-portfolio-disclosure page, file `JioBlackRock Flexi Cap Fund-Monthly-Portfolio-30-09-2026.xlsx`
- Holdings: 139 (137 equity, TREPS, net payables). Weight sum: 100.0. No open derivatives at month end.

## 8. Axis Global Equity Alpha FoF: OK, no look-through
- Source: axismf.com statutory-disclosures, combined workbook `Monthly_Portfolio_30_09_2026.xlsx`, sheet AXISGEA
- Holdings: 3. These are Schroder ISF Global Equity Alpha Class X1 Acc (LU2225036040) at 99.05%, TREPS at 1.05% and net payables at -0.10. Weight sum: 100.0
- I found no official full-holdings list for the Schroders fund, only a factsheet. Look-through is set to false and no index was substituted.

## 9. Motilal Oswal Nifty India Defence Index Fund: OK
- Source: the same Motilal workbook as #2, sheet YO52 (not YO55, which is the ETF)
- Holdings: 24 (22 equity, TREPS, net payables). Weight sum: 100.0. No derivatives.

## 10. ICICI Prudential Manufacturing Fund: UNAVAILABLE
- The ICICI disclosure page redirects to archive.icicipruamc.com, which this environment's network refuses. The file API host also resets connections. The latest month is not confirmed.

## Checks
All 10 funds appear once. Every ISIN matches the format check. No ISIN repeats within a fund. The JSON parses.

**Summary: 7 of 10 OK. Kotak (#3, #5) and ICICI (#10) are UNAVAILABLE because the official sites could not be reached from here.**
