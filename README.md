# chatmmaodds
DraftKings Salary and Odds Graphics App
Final Plan for Claude Code: UFC DraftKings Friday Salary + Odds Graphic App
Overview
A Streamlit app that uploads a DraftKings MMA salary CSV, scrapes all needed odds from fightodds.io using Playwright (the only source with complete fight coverage, BetOnline odds, and all prop types), presents an editable table for review, then generates two downloadable PNG graphics in the dark DraftKings orange/green style.

Why fightodds.io over bestfightodds.com
bestfightodds.com only lists ~7 fights for UFC events vs. the full 13 on fightodds.io. The user also specifically uses BetOnline odds from fightodds.io as their primary source. fightodds.io is JavaScript-rendered and requires Playwright, but it's the correct and complete source.

Reference Files to Provide Claude Code
* DKSalaries.csv — raw DraftKings salary export
* DK05-salary-odds.png — column layout: Fight, Fighter, Salary, Win, ITD, Rds, O/U
* DK06-sorted-salary-odds.png — column layout for graphic #2: Fight, Fighter, Salary, Win, ITD (no Rds/O/U)
* DK04-new-ownership-graphic.png — visual style: dark background, orange fighter/fight headers, green data column headers, white body text
* https://github.com/sorarebaron/sports-hype — reference for Streamlit deployment patterns

Step 1: Parse the DraftKings Salary CSV
* Extract Name and Salary columns; store salary as integer (strip $/commas if present)
* Initialize DataFrame: Fight (blank int), Fighter, Salary, Win (blank), ITD (blank), Rds (blank), O/U (blank)

Step 2: Scrape fightodds.io with Playwright
fightodds.io is a React/JS single-page app — requests + BeautifulSoup will not work. Playwright with headless Chromium is required.
2a — Find the upcoming UFC event
* Launch headless Chromium via Playwright
* Navigate to https://fightodds.io/upcoming-mma-events/ufc
* Wait for the fight list to render (wait for a CSS selector like a fighter name or odds cell to appear)
* Find the next upcoming UFC event and extract its URL (e.g. /events/ufc-seattle-adesanya-vs-pyfer)
* Navigate to that event page
2b — Extract Win odds (moneyline)
From the main event odds table visible in Image 2 (screenshot the user provided):
* The table has fighter names in rows and sportsbook columns: BetOnline, Bovada, MyBookie, BetUS, Bet105, Bookmaker, DraftKings, FanDuel, 4Cx, BetAnything, Circa, BetRivers, HardRockBet, BetMGM, Caesars, Polymarket, Pinnacle
* Bookmaker priority for Win odds: BetOnline → DraftKings → FanDuel → BetMGM → Caesars → first available
* Extract the moneyline (Win) for each fighter using BetOnline column first, falling back down the priority list if empty
2c — Extract ITD odds
* Each fighter row has a props/details section (the ≡ icon on the right side in Image 2, or a clickable expand)
* Inside, look for "[Fighter] wins inside distance" — extract the Yes/first odds value
* Same bookmaker priority: BetOnline first, then fallback
2d — Extract O/U rounds
* In the props section, look for "Over 1½ rounds" and "Over 2½ rounds" rows
* Extract odds for both lines from the same bookmaker priority
* Line selection logic: If only one line exists, use it. If both exist, select the line whose over odds are closest to -110 (the "main" market line). Store as: Rds = 1.5 or 2.5, O/U = the over odds for that line.
2e — Fight card order
* Fights are listed on the fightodds.io event page in card order (prelims first, main card last, or vice versa — note which direction and assign fight numbers accordingly, with Fight 1 = first listed)
* Assign sequential fight numbers as fights appear on page
2f — Name matching
* Use rapidfuzz to match fightodds.io names to DK CSV names (≥85% = auto-fill)
* Below threshold: leave blank, visible in editable table
* No separate matching screen
Graceful failure is mandatory: Wrap entire scraping block in try/except. On any failure, show a warning banner and leave all odds blank and manually editable. Never crash the app.

Step 3: Central Editable Table
st.data_editor table with these columns:
Fight	Fighter	Salary	Win	ITD	Rds	O/U
	•	Fight — editable integer; pre-populated from page order; user edits to reorder
* Fighter — read-only (from DK CSV)
* Salary — read-only (from DK CSV)
* Win — pre-populated from BetOnline (or fallback); user can override
* ITD — pre-populated from BetOnline (or fallback); user can override
* Rds — pre-populated (1.5 or 2.5); user can override
* O/U — pre-populated over odds; user can override
Within each fight number, sort the higher-salary fighter first. Table re-sorts when Fight values change.
Show a status summary above the table: "✅ Scraped 26/26 fighters — 3 ITD values missing (shown blank)" or "⚠️ Scrape failed — please fill in manually"

Step 4: Graphic Generation
Visual Style — match DK04 exactly
* Background: #1a1a1a
* "FIGHT" and "FIGHTER" header text: DraftKings orange (#e87722 approx — sample from DK04)
* "SALARY", "WIN", "ITD", "RDS", "O/U" header text: DraftKings green (#56ab2f approx — sample from DK04)
* Body text: White #ffffff, bold
* Font: Find DejaVu Sans Bold at runtime:


python
  import glob
  fonts = glob.glob("/usr/**/DejaVuSans-Bold.ttf", recursive=True)
  font_path = fonts[0] if fonts else None
  # Fall back to PIL default if not found
```
- **Row height:** ~38px; header slightly taller; column widths proportional to content
- **Canvas width:** 950px; height scales with fighter count

#### Graphic #1 — Fight Card Order
- Sort by Fight number asc; within each fight, higher salary first
- Columns: **FIGHT | FIGHTER | SALARY | WIN | ITD | RDS | O/U**
- Filename: `ufc_salaries_card_order.png`

#### Graphic #2 — Sorted by Salary
- All fighters sorted by Salary descending
- Columns: **FIGHT | FIGHTER | SALARY | WIN | ITD** (no RDS, no O/U — matching DK06)
- Filename: `ufc_salaries_by_salary.png`

---

### Step 5: App Layout
```
Title: "UFC DraftKings Friday Graphic Generator"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[1] Upload DraftKings Salary CSV
    → Parses Name + Salary
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[2] "Scrape Odds from FightOdds.io" button
    → Launches headless Chromium
    → Finds next UFC event
    → Extracts Win, ITD, O/U per fighter
    → Fuzzy-matches names to CSV
    → Shows scrape status summary
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[3] Editable Table (st.data_editor)
    → Review / correct all scraped values
    → Edit Fight numbers to set card order
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[4] "Generate Graphics" button
    → Preview + ⬇ Download Graphic #1
    → Preview + ⬇ Download Graphic #2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

### Tech Stack
```
streamlit
pandas
requests
Pillow
rapidfuzz
playwright
```

**`packages.txt`** (for Streamlit Cloud — installs system dependencies):
```
chromium-browser
chromium-driver
Post-install script or setup.sh for Playwright:


bash
playwright install chromium
On Streamlit Cloud this can be handled via a setup.sh or by calling subprocess.run(["playwright", "install", "chromium"]) on first run.

Critical Notes for Claude Code
1. fightodds.io is a React SPA — must use Playwright. requests will return an empty shell. Use page.wait_for_selector() after navigation to ensure content has loaded before parsing.
2. BetOnline is the primary bookmaker — column header in Image 2 is literally "BetOnline". Extract that column first for Win, ITD, and O/U. Fall back through: DraftKings → FanDuel → BetMGM → Caesars → BetRivers → first non-empty value.
3. O/U line selection — when both 1.5 and 2.5 are available, pick the one whose over odds are closest to -110. Store the line number in Rds and the over odds in O/U.
4. ITD prop label — on fightodds.io the prop is labeled "[Fighter name] wins inside distance" — the fighter name is embedded in the label text. Match it back to the correct fighter row by name.
5. Fight order — scrape and assign fight numbers in the order fights appear on the fightodds.io event page. The user can override in the editable table.
6. Playwright on Streamlit Cloud — this is the trickiest deployment issue. Claude Code should look at community solutions for running Playwright on Streamlit Cloud, specifically the packages.txt + setup.sh pattern. A working reference: add playwright install-deps chromium && playwright install chromium to the startup process.
7. Scraping is fragile — wrap everything in try/except. On failure, app stays functional with blank odds columns ready for manual input.
8. No API keys required — this is purely scraping-based.
