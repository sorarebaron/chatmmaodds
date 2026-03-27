"""
UFC DraftKings Friday Graphic Generator
Streamlit app: upload DK salary CSV → scrape odds → edit table → generate PNGs
"""

import streamlit as st
import pandas as pd
import io
import glob
import subprocess
import re
from PIL import Image, ImageDraw, ImageFont

# ──────────────────────────────────────────────────────────────────────────────
# PLAYWRIGHT SETUP  (cached → runs once per container lifetime)
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def ensure_playwright():
    """Install Playwright Chromium once.  Deps handled via packages.txt."""
    try:
        r = subprocess.run(
            ["playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=180
        )
        return r.returncode == 0
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────
BG       = "#1a1a1a"
ORANGE   = "#F6770E"
GREEN    = "#61B50E"
WHITE    = "#FFFFFF"
SHADE_A  = "#1a1a1a"   # even fight pairs — same as background
SHADE_B  = "#1d2535"   # odd fight pairs — subtle dark navy tint
DIV_LINE = "#2e2e2e"

BOOK_PRIORITY = [
    "BetOnline", "DraftKings", "FanDuel",
    "BetMGM", "Caesars", "BetRivers", "Bovada",
]

EMPTY_VALS = {"", "\u2013", "\u2014", "-", "N/A", "n/a", "pk", "PK", "even", "EVEN"}
PROP_KW    = ["inside distance", "over 1", "over 2", "over 3", "over 4"]
BOOK_KW    = ["betonline", "bovada", "betmgm", "caesars", "fanduel",
              "draftkings", "betrivers", "mybookie", "pinnacle"]


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def get_font(size: int):
    for pat in ["/usr/**/DejaVuSans-Bold.ttf"]:
        hits = glob.glob(pat, recursive=True)
        if hits:
            return ImageFont.truetype(hits[0], size)
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except Exception:
        return ImageFont.load_default()


def fmt_odds(val) -> str:
    """Normalize American odds to +NNN / -NNN or 'n/a'."""
    s = str(val).strip() if val is not None else ""
    if s in ("", "n/a", "N/A", "nan", "None", "\u2013", "\u2014"):
        return "n/a"
    try:
        n = int(s.replace("+", "").replace(" ", ""))
        return f"+{n}" if n > 0 else str(n)
    except Exception:
        return s


def pick_odds(by_book: dict) -> str:
    """Return best available odds using bookmaker priority."""
    for book in BOOK_PRIORITY:
        v = str(by_book.get(book, "")).strip()
        if v and v not in EMPTY_VALS:
            return v
    for v in by_book.values():
        v = str(v).strip()
        if v and v not in EMPTY_VALS:
            return v
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# CSV PARSER
# ──────────────────────────────────────────────────────────────────────────────
def parse_dk_csv(f) -> pd.DataFrame:
    df = pd.read_csv(f)
    df.columns = [c.strip() for c in df.columns]
    nc = next((c for c in df.columns if c.lower() == "name"),   None)
    sc = next((c for c in df.columns if c.lower() == "salary"), None)
    if not nc or not sc:
        raise ValueError(
            f"CSV must have 'Name' and 'Salary' columns.  Found: {list(df.columns)}"
        )
    return pd.DataFrame({
        "Fight":   [""] * len(df),
        "Fighter": df[nc].astype(str).str.strip(),
        "Salary":  pd.to_numeric(df[sc], errors="coerce").fillna(0).astype(int),
        "Win":     [""] * len(df),
        "ITD":     [""] * len(df),
        "Rds":     [""] * len(df),
        "O/U":     [""] * len(df),
    })


# ──────────────────────────────────────────────────────────────────────────────
# SCRAPER
# ──────────────────────────────────────────────────────────────────────────────
def scrape_fightodds(dk_names: list) -> tuple:
    """
    Scrape fightodds.io for the next UFC event.
    Fight 1 = main event (first listed on page).
    Returns (results_dict, status_str).
    """
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
                    "--single-process",
                    "--disable-extensions",
                ]
            )
            page = browser.new_page(user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/122.0.0.0 Safari/537.36"
            ))

            # ── Find the next UFC event ──────────────────────────────────────
            # Use domcontentloaded (faster than networkidle for React SPAs)
            page.goto(
                "https://fightodds.io/upcoming-mma-events/ufc",
                timeout=30000
            )
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(4000)  # let React render

            links = page.locator("a[href*='/events/']").all()
            if not links:
                raise RuntimeError("No event links found on upcoming-events page")
            href = links[0].get_attribute("href") or ""
            event_url = (
                f"https://fightodds.io{href}" if href.startswith("/") else href
            )

            # ── Load event page ──────────────────────────────────────────────
            page.goto(event_url, timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(5000)  # let React render odds table

            # Try to expand any props sections
            for sel in [
                "button[aria-label*='prop']",
                "[class*='prop'] button",
                "button:has-text('Props')",
                "[class*='expand']",
            ]:
                try:
                    for btn in page.locator(sel).all()[:60]:
                        try:
                            btn.click(timeout=600)
                        except Exception:
                            pass
                except Exception:
                    pass
            page.wait_for_timeout(2000)

            # ── Extract ALL table rows via JS in page order ──────────────────
            raw = page.evaluate("""
() => {
    const out = { headers: [], rows: [] };

    for (const tbl of document.querySelectorAll('table')) {
        const ths = [...tbl.querySelectorAll('th')]
            .map(t => t.textContent.trim());
        const hasBooks = ths.some(h =>
            ['BetOnline','Bovada','DraftKings','FanDuel'].some(b => h.includes(b))
        );
        if (!hasBooks) continue;
        out.headers = ths;
        for (const row of tbl.querySelectorAll('tbody tr')) {
            const cells = [...row.querySelectorAll('td')]
                .map(c => c.textContent.trim());
            if (cells.length > 1)
                out.rows.push(cells);
        }
        break;
    }

    if (out.rows.length === 0) {
        const nameEls = document.querySelectorAll(
            '[class*="fighter"] [class*="name"], [class*="fighterName"]'
        );
        nameEls.forEach(el => { out.rows.push([el.textContent.trim()]); });
    }

    return out;
}
""")
            browser.close()

        headers  = raw.get("headers", [])
        all_rows = raw.get("rows",    [])

        if not all_rows:
            raise RuntimeError("No rows extracted from fightodds.io event page")

        # ── Map bookmaker name → column index ────────────────────────────────
        book_col: dict = {}
        for i, h in enumerate(headers):
            for b in BOOK_PRIORITY + ["Bovada", "MyBookie", "BetUS", "Pinnacle"]:
                if b.lower() in h.lower() and b not in book_col:
                    book_col[b] = i

        # ── Process rows sequentially into fight buckets ──────────────────────
        fight_buckets: dict = {}
        fight_num = 0
        in_fight  = 0

        for row in all_rows:
            if not row or not row[0]:
                continue
            label   = row[0]
            label_l = label.lower()
            if any(bk in label_l for bk in BOOK_KW):
                continue
            is_prop = any(kw in label_l for kw in PROP_KW)
            if is_prop:
                if fight_num > 0:
                    fight_buckets[fight_num]["props"].append(row)
                continue
            if len(label) < 2:
                continue
            if in_fight == 0:
                fight_num += 1
                fight_buckets[fight_num] = {"fighters": [], "props": []}
            fight_buckets[fight_num]["fighters"].append(row)
            in_fight = (in_fight + 1) % 2

        if not fight_buckets:
            raise RuntimeError("Could not identify any fights from extracted rows")

        # ── Extract odds per fight ─────────────────────────────────────────────
        raw_fighters = []

        for fn, bucket in fight_buckets.items():
            for row in bucket["fighters"]:
                odds_map = {b: row[c] for b, c in book_col.items() if c < len(row)}
                raw_fighters.append({
                    "raw_name": row[0], "fight_num": fn,
                    "win": pick_odds(odds_map), "itd": "", "rds": "", "ou": "",
                })

            itd_for_fight: dict = {}
            ou_candidates: list = []

            for row in bucket["props"]:
                lbl   = row[0]
                lbl_l = lbl.lower()
                odds_map = {b: row[c] for b, c in book_col.items() if c < len(row)}
                val = pick_odds(odds_map)
                if "inside distance" in lbl_l:
                    fp = re.sub(
                        r"\s*wins inside distance.*", "", lbl,
                        flags=re.IGNORECASE
                    ).strip().lower()
                    itd_for_fight[fp] = val
                elif "over" in lbl_l and val:
                    rds = 1.5
                    if "2" in lbl:   rds = 2.5
                    elif "3" in lbl: rds = 3.5
                    elif "4" in lbl: rds = 4.5
                    try:
                        n = int(val.replace("+", ""))
                        ou_candidates.append((rds, val, abs(n + 110)))
                    except Exception:
                        ou_candidates.append((rds, val, 999))

            best_ou = min(ou_candidates, key=lambda x: x[2]) if ou_candidates else None

            for entry in raw_fighters:
                if entry["fight_num"] != fn:
                    continue
                name_l = entry["raw_name"].lower()
                if itd_for_fight:
                    best_itd = max(
                        itd_for_fight.keys(),
                        key=lambda k: sum(1 for w in k.split() if w in name_l),
                        default=None,
                    )
                    if best_itd and any(w in name_l for w in best_itd.split()):
                        entry["itd"] = itd_for_fight[best_itd]
                if best_ou:
                    entry["rds"] = str(best_ou[0])
                    entry["ou"]  = best_ou[1]

        # ── Fuzzy-match raw names → DK CSV names ──────────────────────────────
        matched = 0
        for entry in raw_fighters:
            m = fzp.extractOne(
                entry["raw_name"], dk_names,
                scorer=fuzz.token_sort_ratio, score_cutoff=85,
            )
            if not m:
                continue
            dk_name = m[0]
            results[dk_name] = {
                "fight_num": entry["fight_num"],
                "win":       fmt_odds(entry["win"]) if entry["win"] else "",
                "itd":       fmt_odds(entry["itd"]) if entry["itd"] else "",
                "rds":       entry["rds"],
                "ou":        fmt_odds(entry["ou"]) if entry["ou"] else "",
            }
            matched += 1

        total    = len(dk_names)
        miss_itd = sum(1 for v in results.values() if not v["itd"])
        miss_ou  = sum(1 for v in results.values() if not v["ou"])
        parts    = [f"\u2705 Scraped {matched}/{total} fighters"]
        if miss_itd: parts.append(f"{miss_itd} ITD missing")
        if miss_ou:  parts.append(f"{miss_ou} O/U missing")
        status = " \u2014 ".join(parts)
        if miss_itd or miss_ou:
            status += " (shown blank)"

    except Exception as exc:
        status = f"\u26a0\ufe0f Scrape failed ({exc!s}) \u2014 please fill in manually"

    return results, status


# ──────────────────────────────────────────────────────────────────────────────
# GRAPHICS
# ──────────────────────────────────────────────────────────────────────────────
FONT_HDR  = 20
FONT_BODY = 19
ROW_H     = 42
HDR_H     = 52
PAD_X     = 28
PAD_Y     = 18

COL_COLOR = {
    "Fight":   ORANGE,
    "Fighter": ORANGE,
    "Salary":  GREEN,
    "Win":     GREEN,
    "ITD":     GREEN,
    "Rds":     GREEN,
    "O/U":     GREEN,
}


def _render(rows: list, columns: list, widths: dict, shade_groups: list) -> bytes:
    """
    shade_groups: list of ints (one per row).
      Even value  → SHADE_A (base dark)
      Odd value   → SHADE_B (dark navy tint)
    """
    cw = PAD_X * 2 + sum(widths[c] for c in columns)
    ch = PAD_Y * 2 + HDR_H + len(rows) * ROW_H

    img  = Image.new("RGB", (cw, ch), BG)
    draw = ImageDraw.Draw(img)
    hf   = get_font(FONT_HDR)
    bf   = get_font(FONT_BODY)

    # Header
    x = PAD_X
    for col in columns:
        w = widths[col]
        draw.text(
            (x + w // 2, PAD_Y + HDR_H // 2), col,
            font=hf, fill=COL_COLOR.get(col, WHITE), anchor="mm"
        )
        x += w
    draw.line(
        [(PAD_X, PAD_Y + HDR_H), (cw - PAD_X, PAD_Y + HDR_H)],
        fill="#444444", width=1
    )

    # Data rows
    for i, row in enumerate(rows):
        ry = PAD_Y + HDR_H + i * ROW_H
        sg = shade_groups[i] if i < len(shade_groups) else i
        if sg % 2 == 1:
            draw.rectangle(
                [(PAD_X, ry), (cw - PAD_X, ry + ROW_H)],
                fill=SHADE_B
            )
        x = PAD_X
        for col in columns:
            w   = widths[col]
            val = row.get(col, "")
            if val is None or str(val).strip() in ("", "nan"):
                val = "n/a" if col not in ("Fight", "Fighter", "Salary") else ""
            txt = str(val)
            if col == "Salary":
                try:
                    txt = f"${int(val):,}"
                except Exception:
                    pass
            if col == "Fighter":
                draw.text(
                    (x + 8, ry + ROW_H // 2), txt,
                    font=bf, fill=WHITE, anchor="lm"
                )
            else:
                draw.text(
                    (x + w // 2, ry + ROW_H // 2), txt,
                    font=bf, fill=WHITE, anchor="mm"
                )
            x += w
        draw.line(
            [(PAD_X, ry + ROW_H), (cw - PAD_X, ry + ROW_H)],
            fill=DIV_LINE, width=1
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(150, 150))
    return buf.getvalue()


def make_g1(df: pd.DataFrame) -> bytes:
    """Graphic 1 — card order, 7 columns, shaded by fight pair."""
    d = df.copy()
    d["_fn"] = pd.to_numeric(d["Fight"], errors="coerce").fillna(999).astype(int)
    d = d.sort_values(["_fn", "Salary"], ascending=[True, False]).reset_index(drop=True)
    # Alternate shade by fight number: fight 1 = group 0, fight 2 = group 1, etc.
    shade_groups = [
        (int(row["_fn"]) - 1) % 2 if row["_fn"] != 999 else i % 2
        for i, (_, row) in enumerate(d.iterrows())
    ]
    d = d.drop(columns="_fn")
    widths = {
        "Fight": 65, "Fighter": 250, "Salary": 90,
        "Win": 85, "ITD": 85, "Rds": 70, "O/U": 80,
    }
    return _render(
        d.to_dict("records"),
        ["Fight", "Fighter", "Salary", "Win", "ITD", "Rds", "O/U"],
        widths, shade_groups,
    )


def make_g2(df: pd.DataFrame) -> bytes:
    """Graphic 2 — salary descending, 5 columns, alternating row shade."""
    d = df.sort_values("Salary", ascending=False).copy().reset_index(drop=True)
    shade_groups = list(range(len(d)))  # every row alternates
    widths = {
        "Fight": 65, "Fighter": 260, "Salary": 90,
        "Win": 90, "ITD": 90,
    }
    return _render(
        d.to_dict("records"),
        ["Fight", "Fighter", "Salary", "Win", "ITD"],
        widths, shade_groups,
    )


# ──────────────────────────────────────────────────────────────────────────────
# STREAMLIT UI
# ──────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="UFC DK Graphic Generator",
    page_icon="\U0001f94a",
    layout="wide",
)
st.title("UFC DraftKings Friday Graphic Generator")

# Kick off Playwright Chromium install (cached — blocks until done on first run)
ensure_playwright()

# ── 1 · Upload CSV ───────────────────────────────────────────────────────────
st.markdown("### 1 \u00b7 Upload DraftKings Salary CSV")
uploaded = st.file_uploader("Choose DKSalaries.csv", type=["csv"])

if uploaded:
    if (
        "df" not in st.session_state
        or st.session_state.get("_fname") != uploaded.name
    ):
        try:
            st.session_state.df     = parse_dk_csv(uploaded)
            st.session_state._fname = uploaded.name
            # Clear downstream state so editor reinitialises with fresh data
            for k in ("scrape_status", "g1", "g2", "data_editor"):
                st.session_state.pop(k, None)
        except Exception as e:
            st.error(f"CSV error: {e}")
            st.stop()

# ── 2 · Scrape ───────────────────────────────────────────────────────────────
if "df" in st.session_state:
    st.markdown("### 2 \u00b7 Scrape Odds from FightOdds.io")
    st.caption(
        "Scrapes BetOnline moneyline (Win), ITD, and O/U odds for the next UFC event. "
        "Results auto-fill the table below \u2014 edit anything that looks wrong."
    )

    if st.button("\U0001f50d Scrape Odds", type="primary"):
        with st.spinner("Launching Chromium \u00b7 scraping fightodds.io\u2026"):
            odds, status = scrape_fightodds(
                st.session_state.df["Fighter"].tolist()
            )
        st.session_state.scrape_status = status

        df = st.session_state.df.copy()
        for i, fighter in df["Fighter"].items():
            if fighter in odds:
                o = odds[fighter]
                df.at[i, "Fight"] = str(o["fight_num"]) if o["fight_num"] else ""
                df.at[i, "Win"]   = o["win"]
                df.at[i, "ITD"]   = o["itd"]
                df.at[i, "Rds"]   = o["rds"]
                df.at[i, "O/U"]   = o["ou"]
        # Update base data AND force editor to reinitialise with scraped values
        st.session_state.df = df
        st.session_state.pop("data_editor", None)

    if s := st.session_state.get("scrape_status"):
        (st.success if "\u2705" in s else st.warning)(s)

# ── 3 · Editable Table  +  4 · Generate Graphics (same block so `edited` is in scope)
# ─────────────────────────────────────────────────────────────────────────────
# ROOT CAUSE OF REVERT BUG (now fixed):
#   Previously we did `st.session_state.df = edited` after every edit.
#   Streamlit interprets any change to the data passed into st.data_editor as
#   "new data" and reinitialises the widget — discarding the user's sort and
#   any pending keystrokes.  Fix: never reassign session_state.df during normal
#   editing.  The editor owns its own state (via key="data_editor").  We only
#   read `edited` (the live snapshot) when generating graphics.
# ──────────────────────────────────────────────────────────────────────────────
if "df" in st.session_state:
    st.markdown("### 3 \u00b7 Review & Edit")
    st.caption(
        "Fight 1 = main event, Fight 2 = co-main, etc.  "
        "Click any column header to sort.  Edits and sort order persist between reruns."
    )

    # Pass st.session_state.df as the stable base; do NOT overwrite it here.
    # The editor tracks all user edits internally via key="data_editor".
    edited = st.data_editor(
        st.session_state.df,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "Fight":   st.column_config.TextColumn("Fight",   width=70),
            "Fighter": st.column_config.TextColumn("Fighter", disabled=True, width=200),
            "Salary":  st.column_config.NumberColumn(
                           "Salary", disabled=True, width=90, format="%d"),
            "Win":     st.column_config.TextColumn("Win",  width=80),
            "ITD":     st.column_config.TextColumn("ITD",  width=80),
            "Rds":     st.column_config.TextColumn("Rds",  width=70),
            "O/U":     st.column_config.TextColumn("O/U",  width=70),
        },
        key="data_editor",
    )
    # `edited` is always the current live state of the table (base + all edits).
    # We use it directly for generation — no session_state reassignment needed.

    st.markdown("### 4 \u00b7 Generate Graphics")

    if st.button("\U0001f3a8 Generate Graphics", type="primary"):
        df = edited.copy()          # snapshot everything the user has entered
        for col in ("Win", "ITD", "O/U"):
            df[col] = df[col].apply(fmt_odds)
        with st.spinner("Rendering images\u2026"):
            st.session_state.g1 = make_g1(df)
            st.session_state.g2 = make_g2(df)

    if "g1" in st.session_state:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Graphic 1 \u2014 Card Order")
            st.image(st.session_state.g1)
            st.download_button(
                "\u2b07 Download Graphic 1",
                data=st.session_state.g1,
                file_name="ufc_salaries_card_order.png",
                mime="image/png",
                key="dl1",
            )
        with c2:
            st.subheader("Graphic 2 \u2014 Sorted by Salary")
            st.image(st.session_state.g2)
            st.download_button(
                "\u2b07 Download Graphic 2",
                data=st.session_state.g2,
                file_name="ufc_salaries_by_salary.png",
                mime="image/png",
                key="dl2",
            )
