"""
UFC DraftKings Friday Graphic Generator
"""

import streamlit as st
import pandas as pd
import io
import glob
import re
from PIL import Image, ImageDraw, ImageFont


# ── Constants ────────────────────────────────────────────────────────────────
BG       = "#1a1a1a"
ORANGE   = "#F6770E"
GREEN    = "#61B50E"
WHITE    = "#FFFFFF"
SHADE_B  = "#1d2535"   # highlighted fight rows
DIV_LINE = "#2e2e2e"

# the-odds-api.com bookmaker key priority (low index = highest priority)
BOOK_PRIORITY_API = [
    "betonline", "draftkings", "fanduel", "betmgm",
    "caesars", "betrivers", "bovada", "mybookieag", "pinnacle",
]
EMPTY_VALS = {"", "–", "—", "-", "N/A", "n/a", "pk", "PK", "even", "EVEN"}


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
    if s in ("", "n/a", "N/A", "nan", "None", "–", "—"):
        return "n/a"
    try:
        n = int(s.replace("+", "").replace(" ", ""))
        return f"+{n}" if n > 0 else str(n)
    except Exception:
        return s


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
        "Fight":   [None] * n,           # None → blank in NumberColumn
        "Fighter": df[nc].astype(str).str.strip(),
        "Salary":  pd.to_numeric(df[sc], errors="coerce").fillna(0).astype(int),
        "Win":     [""] * n,
        "ITD":     [""] * n,
        "Rds":     [""] * n,
        "O/U":     [""] * n,
    })


# ── The-Odds-API fetcher ──────────────────────────────────────────────────────
def _pick_by_book_priority(by_book: dict, priority: list):
    """Return value from highest-priority bookmaker that has a non-empty value."""
    for key in priority:
        v = by_book.get(key)
        if v is not None and str(v).strip() not in EMPTY_VALS:
            return v
    # fallback: any non-empty value
    for v in by_book.values():
        if v is not None and str(v).strip() not in EMPTY_VALS:
            return v
    return None


def fetch_odds_api(dk_names: list, api_key: str) -> tuple:
    import requests
    from rapidfuzz import process as fzp, fuzz

    results: dict = {}
    quota_info = ""

    try:
        resp = requests.get(
            "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds",
            params={
                "apiKey":     api_key,
                "regions":    "us",
                "markets":    "h2h,totals",
                "oddsFormat": "american",
            },
            timeout=15,
        )

        if resp.status_code == 401:
            raise RuntimeError("Invalid API key — check your the-odds-api.com key")
        if resp.status_code == 422:
            raise RuntimeError("API parameter error (markets/regions)")
        resp.raise_for_status()

        remaining = resp.headers.get("x-requests-remaining", "?")
        quota_info = f"(quota: {remaining} requests remaining)"

        events = resp.json()
        if not events:
            raise RuntimeError("No upcoming MMA events found from the-odds-api.com")

        # Build fighter_name_lower → {win, ou_line, ou_odds}
        fighter_data: dict = {}

        for event in events:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            bookmakers = event.get("bookmakers", [])

            # Collect h2h odds per book
            h2h_home: dict  = {}   # book_key → american price (int)
            h2h_away: dict  = {}
            # Collect totals per book: book_key → list of (line, over_price)
            totals: dict    = {}

            for book in bookmakers:
                bkey = book["key"]
                for market in book.get("markets", []):
                    if market["key"] == "h2h":
                        for outcome in market.get("outcomes", []):
                            price = outcome.get("price", 0)
                            if outcome["name"] == home:
                                h2h_home[bkey] = price
                            elif outcome["name"] == away:
                                h2h_away[bkey] = price
                    elif market["key"] == "totals":
                        for outcome in market.get("outcomes", []):
                            if outcome.get("name") == "Over":
                                totals.setdefault(bkey, []).append(
                                    (outcome.get("point", 1.5), outcome.get("price", 0))
                                )

            def _fmt(price) -> str:
                if price is None:
                    return ""
                return f"+{price}" if price > 0 else str(price)

            home_win_raw = _pick_by_book_priority(h2h_home, BOOK_PRIORITY_API)
            away_win_raw = _pick_by_book_priority(h2h_away, BOOK_PRIORITY_API)

            # Best totals line: pick book by priority, then line closest to -110
            ou_line_str = ou_odds_str = ""
            for bkey in BOOK_PRIORITY_API + list(totals.keys()):
                if bkey in totals and totals[bkey]:
                    best = min(totals[bkey], key=lambda x: abs(x[1] + 110))
                    ou_line_str  = str(best[0])
                    ou_odds_str  = _fmt(best[1])
                    break

            for fighter, win_raw in [(home, home_win_raw), (away, away_win_raw)]:
                if not fighter:
                    continue
                fighter_data[fighter.lower()] = {
                    "win": _fmt(win_raw) if win_raw is not None else "",
                    "ou":  ou_odds_str,
                    "rds": ou_line_str,
                }

        # Fuzzy-match API names to DK CSV names
        api_name_list = list(fighter_data.keys())
        matched = 0
        for dk_name in dk_names:
            m = fzp.extractOne(
                dk_name.lower(), api_name_list,
                scorer=fuzz.token_sort_ratio, score_cutoff=78,
            )
            if not m:
                continue
            o = fighter_data[m[0]]
            results[dk_name] = {
                "fight_num": None,
                "win":       o["win"],
                "itd":       "",          # not available via this API
                "rds":       o["rds"],
                "ou":        o["ou"],
            }
            matched += 1

        total    = len(dk_names)
        miss_win = sum(1 for v in results.values() if not v["win"])
        parts = [f"✅ Matched {matched}/{total} fighters {quota_info}"]
        if miss_win:
            parts.append(f"{miss_win} Win odds missing")
        parts.append("ITD not available via API — enter manually")
        status = " — ".join(parts)

    except Exception as exc:
        status = f"⚠️ API fetch failed ({exc!s}) — please fill in manually"

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

    x = PAD_X
    for col in columns:
        w = widths[col]
        draw.text((x + w // 2, PAD_Y + HDR_H // 2), col,
                  font=hf, fill=COL_COLOR.get(col, WHITE), anchor="mm")
        x += w
    draw.line([(PAD_X, PAD_Y + HDR_H), (cw - PAD_X, PAD_Y + HDR_H)],
              fill="#444444", width=1)

    for i, row in enumerate(rows):
        ry = PAD_Y + HDR_H + i * ROW_H
        sg = shade_groups[i] if i < len(shade_groups) else 0
        if sg % 2 == 1:
            draw.rectangle([(PAD_X, ry), (cw - PAD_X, ry + ROW_H)], fill=SHADE_B)
        x = PAD_X
        for col in columns:
            w   = widths[col]
            txt = _cell_txt(col, row.get(col, ""))
            if col == "Fighter":
                draw.text((x + 8, ry + ROW_H // 2), txt, font=bf, fill=WHITE, anchor="lm")
            else:
                draw.text((x + w // 2, ry + ROW_H // 2), txt, font=bf, fill=WHITE, anchor="mm")
            x += w
        draw.line([(PAD_X, ry + ROW_H), (cw - PAD_X, ry + ROW_H)],
                  fill=DIV_LINE, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(150, 150))
    return buf.getvalue()


def make_g1(df: pd.DataFrame) -> bytes:
    """Card order — 7 cols — fight 1 unshaded, fight 2 shaded, alternating."""
    d = df.copy()
    d["_fn"] = pd.to_numeric(d["Fight"], errors="coerce").fillna(999).astype(int)
    d = d.sort_values(["_fn", "Salary"], ascending=[True, False]).reset_index(drop=True)
    shade = [(int(r["_fn"]) - 1) % 2 if r["_fn"] != 999 else 0
             for _, r in d.iterrows()]
    d = d.drop(columns="_fn")
    widths = {"Fight": 65, "Fighter": 250, "Salary": 90,
              "Win": 85, "ITD": 85, "Rds": 70, "O/U": 80}
    return _render(d.to_dict("records"),
                   ["Fight", "Fighter", "Salary", "Win", "ITD", "Rds", "O/U"],
                   widths, shade)


def make_g2(df: pd.DataFrame) -> bytes:
    """Salary descending — 5 cols — no alternating shade."""
    d = df.sort_values("Salary", ascending=False).copy().reset_index(drop=True)
    shade = [0] * len(d)
    widths = {"Fight": 65, "Fighter": 260, "Salary": 90, "Win": 90, "ITD": 90}
    return _render(d.to_dict("records"),
                   ["Fight", "Fighter", "Salary", "Win", "ITD"],
                   widths, shade)


# ── Streamlit UI ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="UFC DK Graphic Generator", page_icon="🥊", layout="wide")
st.title("UFC DraftKings Friday Graphic Generator")

# ── Section 1: Upload CSV ─────────────────────────────────────────────────────
st.markdown("### 1 · Upload DraftKings Salary CSV")
uploaded = st.file_uploader("Choose DKSalaries.csv", type=["csv"])
if uploaded:
    if "df" not in st.session_state or st.session_state.get("_fname") != uploaded.name:
        try:
            st.session_state.df     = parse_dk_csv(uploaded)
            st.session_state._fname = uploaded.name
            for k in ("scrape_status", "g1", "g2", "data_editor"):
                st.session_state.pop(k, None)
        except Exception as e:
            st.error(f"CSV error: {e}")
            st.stop()

# ── Section 2: Fetch Odds ─────────────────────────────────────────────────────
if "df" in st.session_state:
    st.markdown("### 2 · Fetch Odds from The-Odds-API")
    st.caption(
        "Fetches Win moneyline and O/U round totals via [the-odds-api.com](https://the-odds-api.com). "
        "ITD odds are not available through this API — enter those manually."
    )

    # API key: prefer st.secrets, fall back to text input
    try:
        api_key = st.secrets["ODDS_API_KEY"]
        st.info("API key loaded from Streamlit secrets.", icon="🔑")
    except (KeyError, FileNotFoundError, AttributeError):
        api_key = st.text_input(
            "The-Odds-API key",
            type="password",
            placeholder="Paste your free API key from the-odds-api.com",
            key="api_key_input",
        )

    if st.button("📡 Fetch Odds", type="primary", disabled=not api_key):
        with st.spinner("Fetching odds from the-odds-api.com…"):
            odds, status = fetch_odds_api(
                st.session_state.df["Fighter"].tolist(), api_key
            )
        st.session_state.scrape_status = status
        df = st.session_state.df.copy()
        for i, fighter in df["Fighter"].items():
            if fighter in odds:
                o = odds[fighter]
                df.at[i, "Fight"] = o["fight_num"]
                df.at[i, "Win"]   = o["win"]
                df.at[i, "ITD"]   = o["itd"]
                df.at[i, "Rds"]   = o["rds"]
                df.at[i, "O/U"]   = o["ou"]
        st.session_state.df = df
        st.session_state.pop("data_editor", None)

    if s := st.session_state.get("scrape_status"):
        (st.success if "✅" in s else st.warning)(s)

# ── Section 3 & 4: Edit table + Generate graphics ────────────────────────────
if "df" in st.session_state:
    st.markdown("### 3 · Review & Edit")
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
            "Fight":   st.column_config.NumberColumn(
                "Fight", width=70, min_value=1, step=1, format="%d"
            ),
            "Fighter": st.column_config.TextColumn("Fighter", disabled=True, width=200),
            "Salary":  st.column_config.NumberColumn(
                "Salary", disabled=True, width=90, format="%d"
            ),
            "Win":     st.column_config.TextColumn("Win",  width=80),
            "ITD":     st.column_config.TextColumn("ITD",  width=80),
            "Rds":     st.column_config.TextColumn("Rds",  width=70),
            "O/U":     st.column_config.TextColumn("O/U",  width=70),
        },
        key="data_editor",
    )

    st.markdown("### 4 · Generate Graphics")
    if st.button("🎨 Generate Graphics", type="primary"):
        df = edited.copy()
        for col in ("Win", "ITD", "O/U"):
            df[col] = df[col].apply(fmt_odds)
        with st.spinner("Rendering…"):
            st.session_state.g1 = make_g1(df)
            st.session_state.g2 = make_g2(df)

    if "g1" in st.session_state:
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Graphic 1 — Card Order")
            st.image(st.session_state.g1)
            st.download_button(
                "⬇ Download Graphic 1", st.session_state.g1,
                "ufc_salaries_card_order.png", "image/png", key="dl1",
            )
        with c2:
            st.subheader("Graphic 2 — Sorted by Salary")
            st.image(st.session_state.g2)
            st.download_button(
                "⬇ Download Graphic 2", st.session_state.g2,
                "ufc_salaries_by_salary.png", "image/png", key="dl2",
            )
