"""
UFC DraftKings Friday Graphic Generator
"""

import streamlit as st
import pandas as pd
import io
import glob
import subprocess
import re
from PIL import Image, ImageDraw, ImageFont


@st.cache_resource(show_spinner=False)
def ensure_playwright():
    try:
        subprocess.run(["playwright", "install", "chromium"],
                       capture_output=True, text=True, timeout=180)
        return True
    except Exception:
        return False


# ── Constants ────────────────────────────────────────────────────────────────
BG       = "#1a1a1a"
ORANGE   = "#F6770E"
GREEN    = "#61B50E"
WHITE    = "#FFFFFF"
SHADE_B  = "#1d2535"   # highlighted fight rows (odd fight numbers)
DIV_LINE = "#2e2e2e"

BOOK_PRIORITY = ["BetOnline","DraftKings","FanDuel","BetMGM","Caesars","BetRivers","Bovada"]
EMPTY_VALS    = {"","\u2013","\u2014","-","N/A","n/a","pk","PK","even","EVEN"}
PROP_KW       = ["inside distance","over 1","over 2","over 3","over 4"]
BOOK_KW       = ["betonline","bovada","betmgm","caesars","fanduel",
                 "draftkings","betrivers","mybookie","pinnacle"]


# ── Helpers ──────────────────────────────────────────────────────────────────
def get_font(size):
    hits = glob.glob("/usr/**/DejaVuSans-Bold.ttf", recursive=True)
    if hits:
        return ImageFont.truetype(hits[0], size)
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except Exception:
        return ImageFont.load_default()


def fmt_odds(val) -> str:
    s = str(val).strip() if val is not None else ""
    if s in ("","n/a","N/A","nan","None","\u2013","\u2014"):
        return "n/a"
    try:
        n = int(s.replace("+","").replace(" ",""))
        return f"+{n}" if n > 0 else str(n)
    except Exception:
        return s


def pick_odds(by_book: dict) -> str:
    for book in BOOK_PRIORITY:
        v = str(by_book.get(book,"")).strip()
        if v and v not in EMPTY_VALS:
            return v
    for v in by_book.values():
        v = str(v).strip()
        if v and v not in EMPTY_VALS:
            return v
    return ""


# ── CSV Parser ───────────────────────────────────────────────────────────────
def parse_dk_csv(f) -> pd.DataFrame:
    df = pd.read_csv(f)
    df.columns = [c.strip() for c in df.columns]
    nc = next((c for c in df.columns if c.lower() == "name"),   None)
    sc = next((c for c in df.columns if c.lower() == "salary"), None)
    if not nc or not sc:
        raise ValueError(f"Need 'Name' and 'Salary' columns. Found: {list(df.columns)}")
    n = len(df)
    return pd.DataFrame({
        "Fight":   [float("nan")] * n,          # float so NumberColumn sorts numerically
        "Fighter": df[nc].astype(str).str.strip(),
        "Salary":  pd.to_numeric(df[sc], errors="coerce").fillna(0).astype(int),
        "Win":     [""] * n,
        "ITD":     [""] * n,
        "Rds":     [""] * n,
        "O/U":     [""] * n,
    })


# ── Scraper ──────────────────────────────────────────────────────────────────
def scrape_fightodds(dk_names: list) -> tuple:
    from playwright.sync_api import sync_playwright
    from rapidfuzz import process as fzp, fuzz

    results: dict = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-extensions",
                ]
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            # Mask webdriver flag to reduce bot detection
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page = ctx.new_page()

            # ── Find the next UFC event ──────────────────────────────────────
            page.goto("https://fightodds.io/upcoming-mma-events/ufc", timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            # Wait up to 10s for any <a href> to appear (React render)
            try:
                page.wait_for_selector("a[href]", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(3000)

            # Multi-strategy event link extraction
            event_url = None
            for sel in ["a[href*='/events/']", "a[href*='/event/']", "a[href*='ufc']"]:
                links = page.locator(sel).all()
                if links:
                    href = links[0].get_attribute("href") or ""
                    if href:
                        event_url = (
                            f"https://fightodds.io{href}"
                            if href.startswith("/") else href
                        )
                        break

            # JS fallback: get every <a href> and pick the first event-like one
            if not event_url:
                hrefs = page.evaluate("""
                    () => [...document.querySelectorAll('a[href]')]
                            .map(a => a.getAttribute('href'))
                            .filter(h => h && (h.includes('event') || h.includes('ufc')))
                """)
                if hrefs:
                    href = hrefs[0]
                    event_url = (
                        f"https://fightodds.io{href}"
                        if href.startswith("/") else href
                    )

            if not event_url:
                raise RuntimeError(
                    "Could not find a UFC event link on fightodds.io. "
                    "The site may be blocking the scraper — please fill in manually."
                )

            # ── Load event page ──────────────────────────────────────────────
            page.goto(event_url, timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(6000)   # give React time to render odds table

            # Try to expand props sections
            for sel in ["button[aria-label*='prop']","[class*='prop'] button",
                        "button:has-text('Props')","[class*='expand']"]:
                try:
                    for btn in page.locator(sel).all()[:60]:
                        try: btn.click(timeout=600)
                        except Exception: pass
                except Exception:
                    pass
            page.wait_for_timeout(2000)

            raw = page.evaluate("""
() => {
    const out = { headers: [], rows: [] };
    for (const tbl of document.querySelectorAll('table')) {
        const ths = [...tbl.querySelectorAll('th')].map(t => t.textContent.trim());
        const hasBooks = ths.some(h =>
            ['BetOnline','Bovada','DraftKings','FanDuel'].some(b => h.includes(b)));
        if (!hasBooks) continue;
        out.headers = ths;
        for (const row of tbl.querySelectorAll('tbody tr')) {
            const cells = [...row.querySelectorAll('td')].map(c => c.textContent.trim());
            if (cells.length > 1) out.rows.push(cells);
        }
        break;
    }
    return out;
}
""")
            browser.close()

        headers  = raw.get("headers", [])
        all_rows = raw.get("rows",    [])
        if not all_rows:
            raise RuntimeError("No odds table found on the event page.")

        book_col: dict = {}
        for i, h in enumerate(headers):
            for b in BOOK_PRIORITY + ["Bovada","MyBookie","BetUS","Pinnacle"]:
                if b.lower() in h.lower() and b not in book_col:
                    book_col[b] = i

        fight_buckets: dict = {}
        fight_num = 0
        in_fight  = 0
        for row in all_rows:
            if not row or not row[0]: continue
            label_l = row[0].lower()
            if any(bk in label_l for bk in BOOK_KW): continue
            if any(kw in label_l for kw in PROP_KW):
                if fight_num > 0:
                    fight_buckets[fight_num]["props"].append(row)
                continue
            if len(row[0]) < 2: continue
            if in_fight == 0:
                fight_num += 1
                fight_buckets[fight_num] = {"fighters": [], "props": []}
            fight_buckets[fight_num]["fighters"].append(row)
            in_fight = (in_fight + 1) % 2

        if not fight_buckets:
            raise RuntimeError("No fights identified in the scraped data.")

        raw_fighters = []
        for fn, bucket in fight_buckets.items():
            for row in bucket["fighters"]:
                odds_map = {b: row[c] for b, c in book_col.items() if c < len(row)}
                raw_fighters.append({"raw_name": row[0], "fight_num": fn,
                                     "win": pick_odds(odds_map),
                                     "itd": "", "rds": "", "ou": ""})
            itd_map: dict  = {}
            ou_cands: list = []
            for row in bucket["props"]:
                lbl_l    = row[0].lower()
                odds_map = {b: row[c] for b, c in book_col.items() if c < len(row)}
                val      = pick_odds(odds_map)
                if "inside distance" in lbl_l:
                    fp = re.sub(r"\s*wins inside distance.*", "",
                                row[0], flags=re.IGNORECASE).strip().lower()
                    itd_map[fp] = val
                elif "over" in lbl_l and val:
                    rds = 1.5
                    if "2" in row[0]:   rds = 2.5
                    elif "3" in row[0]: rds = 3.5
                    elif "4" in row[0]: rds = 4.5
                    try:
                        n = int(val.replace("+",""))
                        ou_cands.append((rds, val, abs(n + 110)))
                    except Exception:
                        ou_cands.append((rds, val, 999))
            best_ou = min(ou_cands, key=lambda x: x[2]) if ou_cands else None
            for entry in raw_fighters:
                if entry["fight_num"] != fn: continue
                name_l = entry["raw_name"].lower()
                if itd_map:
                    bi = max(itd_map, key=lambda k: sum(1 for w in k.split() if w in name_l),
                             default=None)
                    if bi and any(w in name_l for w in bi.split()):
                        entry["itd"] = itd_map[bi]
                if best_ou:
                    entry["rds"] = str(best_ou[0])
                    entry["ou"]  = best_ou[1]

        matched = 0
        for entry in raw_fighters:
            m = fzp.extractOne(entry["raw_name"], dk_names,
                               scorer=fuzz.token_sort_ratio, score_cutoff=85)
            if not m: continue
            results[m[0]] = {
                "fight_num": float(entry["fight_num"]),
                "win":  fmt_odds(entry["win"]) if entry["win"] else "",
                "itd":  fmt_odds(entry["itd"]) if entry["itd"] else "",
                "rds":  entry["rds"],
                "ou":   fmt_odds(entry["ou"])  if entry["ou"]  else "",
            }
            matched += 1

        total    = len(dk_names)
        miss_itd = sum(1 for v in results.values() if not v["itd"])
        miss_ou  = sum(1 for v in results.values() if not v["ou"])
        parts    = [f"\u2705 Scraped {matched}/{total} fighters"]
        if miss_itd: parts.append(f"{miss_itd} ITD missing")
        if miss_ou:  parts.append(f"{miss_ou} O/U missing")
        status = " \u2014 ".join(parts)
        if miss_itd or miss_ou: status += " (shown blank)"

    except Exception as exc:
        status = f"\u26a0\ufe0f Scrape failed ({exc!s}) \u2014 please fill in manually"

    return results, status


# ── Graphics ─────────────────────────────────────────────────────────────────
FONT_HDR  = 20
FONT_BODY = 19
ROW_H     = 42
HDR_H     = 52
PAD_X     = 28
PAD_Y     = 18

COL_COLOR = {
    "Fight": ORANGE, "Fighter": ORANGE,
    "Salary": GREEN, "Win": GREEN, "ITD": GREEN, "Rds": GREEN, "O/U": GREEN,
}


def _cell_txt(col, val) -> str:
    """Convert a cell value to display string."""
    # Treat NaN / None / empty as blank (non-odds cols) or n/a (odds cols)
    try:
        is_null = (val is None) or pd.isna(val)
    except Exception:
        is_null = False
    if is_null or str(val).strip() in ("", "nan", "<NA>", "None"):
        return "" if col in ("Fight", "Fighter", "Salary") else "n/a"
    if col == "Fight":
        try:
            return str(int(float(val)))
        except Exception:
            return str(val)
    if col == "Salary":
        try:
            return f"${int(val):,}"
        except Exception:
            return str(val)
    return str(val)


def _render(rows: list, columns: list, widths: dict, shade_groups: list) -> bytes:
    cw = PAD_X * 2 + sum(widths[c] for c in columns)
    ch = PAD_Y * 2 + HDR_H + len(rows) * ROW_H
    img  = Image.new("RGB", (cw, ch), BG)
    draw = ImageDraw.Draw(img)
    hf, bf = get_font(FONT_HDR), get_font(FONT_BODY)

    # Header
    x = PAD_X
    for col in columns:
        w = widths[col]
        draw.text((x + w//2, PAD_Y + HDR_H//2), col,
                  font=hf, fill=COL_COLOR.get(col, WHITE), anchor="mm")
        x += w
    draw.line([(PAD_X, PAD_Y + HDR_H), (cw - PAD_X, PAD_Y + HDR_H)],
              fill="#444444", width=1)

    # Rows
    for i, row in enumerate(rows):
        ry = PAD_Y + HDR_H + i * ROW_H
        sg = shade_groups[i] if i < len(shade_groups) else 0
        if sg % 2 == 1:                              # odd group → navy highlight
            draw.rectangle([(PAD_X, ry), (cw - PAD_X, ry + ROW_H)], fill=SHADE_B)
        x = PAD_X
        for col in columns:
            w   = widths[col]
            txt = _cell_txt(col, row.get(col, ""))
            if col == "Fighter":
                draw.text((x + 8, ry + ROW_H//2), txt, font=bf, fill=WHITE, anchor="lm")
            else:
                draw.text((x + w//2, ry + ROW_H//2), txt, font=bf, fill=WHITE, anchor="mm")
            x += w
        draw.line([(PAD_X, ry + ROW_H), (cw - PAD_X, ry + ROW_H)],
                  fill=DIV_LINE, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(150, 150))
    return buf.getvalue()


def make_g1(df: pd.DataFrame) -> bytes:
    """Card order — 7 cols — fight-pair shading (fight 1 = highlighted)."""
    d = df.copy()
    d["_fn"] = pd.to_numeric(d["Fight"], errors="coerce").fillna(999).astype(int)
    d = d.sort_values(["_fn","Salary"], ascending=[True, False]).reset_index(drop=True)
    # fight_num % 2: fight 1 → 1 (odd → highlighted), fight 2 → 0, fight 3 → 1 ...
    shade = [int(r["_fn"]) % 2 if r["_fn"] != 999 else 0
             for _, r in d.iterrows()]
    d = d.drop(columns="_fn")
    widths = {"Fight":65,"Fighter":250,"Salary":90,"Win":85,"ITD":85,"Rds":70,"O/U":80}
    return _render(d.to_dict("records"),
                   ["Fight","Fighter","Salary","Win","ITD","Rds","O/U"],
                   widths, shade)


def make_g2(df: pd.DataFrame) -> bytes:
    """Salary descending — 5 cols — NO alternating shade."""
    d = df.sort_values("Salary", ascending=False).copy().reset_index(drop=True)
    shade = [0] * len(d)   # all even → uniform base colour
    widths = {"Fight":65,"Fighter":260,"Salary":90,"Win":90,"ITD":90}
    return _render(d.to_dict("records"),
                   ["Fight","Fighter","Salary","Win","ITD"],
                   widths, shade)


# ── Streamlit UI ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="UFC DK Graphic Generator", page_icon="\U0001f94a", layout="wide")
st.title("UFC DraftKings Friday Graphic Generator")
ensure_playwright()

# 1 · Upload
st.markdown("### 1 \u00b7 Upload DraftKings Salary CSV")
uploaded = st.file_uploader("Choose DKSalaries.csv", type=["csv"])
if uploaded:
    if "df" not in st.session_state or st.session_state.get("_fname") != uploaded.name:
        try:
            st.session_state.df     = parse_dk_csv(uploaded)
            st.session_state._fname = uploaded.name
            for k in ("scrape_status","g1","g2","data_editor"):
                st.session_state.pop(k, None)
        except Exception as e:
            st.error(f"CSV error: {e}")
            st.stop()

# 2 · Scrape
if "df" in st.session_state:
    st.markdown("### 2 \u00b7 Scrape Odds from FightOdds.io")
    st.caption("Scrapes BetOnline Win / ITD / O\u2215U odds. Results fill the table — edit anything wrong.")
    if st.button("\U0001f50d Scrape Odds", type="primary"):
        with st.spinner("Launching Chromium \u00b7 scraping fightodds.io\u2026"):
            odds, status = scrape_fightodds(st.session_state.df["Fighter"].tolist())
        st.session_state.scrape_status = status
        df = st.session_state.df.copy()
        for i, fighter in df["Fighter"].items():
            if fighter in odds:
                o = odds[fighter]
                df.at[i,"Fight"] = o["fight_num"]   # stored as float for NumberColumn
                df.at[i,"Win"]   = o["win"]
                df.at[i,"ITD"]   = o["itd"]
                df.at[i,"Rds"]   = o["rds"]
                df.at[i,"O/U"]   = o["ou"]
        st.session_state.df = df
        st.session_state.pop("data_editor", None)   # reinit editor with scraped data
    if s := st.session_state.get("scrape_status"):
        (st.success if "\u2705" in s else st.warning)(s)

# 3 · Edit  +  4 · Generate  (same block so `edited` stays in scope)
# KEY DESIGN: we never overwrite st.session_state.df during normal editing.
# The editor owns its own state via key="data_editor".
# `edited` is always the live snapshot with every user change applied.
if "df" in st.session_state:
    st.markdown("### 3 \u00b7 Review & Edit")
    st.caption(
        "Fight 1 = main event, 2 = co-main, etc.  "
        "Click the Fight column header to sort numerically.  "
        "Edits persist between reruns."
    )
    edited = st.data_editor(
        st.session_state.df,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            # NumberColumn → sorts numerically (fixes 1,10,11 → 1,2,3)
            "Fight":   st.column_config.NumberColumn("Fight",   width=70,
                                                      min_value=1, step=1, format="%d"),
            "Fighter": st.column_config.TextColumn("Fighter",  disabled=True, width=200),
            "Salary":  st.column_config.NumberColumn("Salary",  disabled=True,
                                                      width=90,  format="%d"),
            "Win":     st.column_config.TextColumn("Win",  width=80),
            "ITD":     st.column_config.TextColumn("ITD",  width=80),
            "Rds":     st.column_config.TextColumn("Rds",  width=70),
            "O/U":     st.column_config.TextColumn("O/U",  width=70),
        },
        key="data_editor",
    )

    st.markdown("### 4 \u00b7 Generate Graphics")
    if st.button("\U0001f3a8 Generate Graphics", type="primary"):
        df = edited.copy()
        for col in ("Win","ITD","O/U"):
            df[col] = df[col].apply(fmt_odds)
        with st.spinner("Rendering\u2026"):
            st.session_state.g1 = make_g1(df)
            st.session_state.g2 = make_g2(df)

    if "g1" in st.session_state:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Graphic 1 \u2014 Card Order")
            st.image(st.session_state.g1)
            st.download_button("\u2b07 Download Graphic 1", st.session_state.g1,
                               "ufc_salaries_card_order.png", "image/png", key="dl1")
        with c2:
            st.subheader("Graphic 2 \u2014 Sorted by Salary")
            st.image(st.session_state.g2)
            st.download_button("\u2b07 Download Graphic 2", st.session_state.g2,
                               "ufc_salaries_by_salary.png", "image/png", key="dl2")
