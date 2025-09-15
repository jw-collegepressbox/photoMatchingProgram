import streamlit as st
import os
import unicodedata
import re
import requests
from bs4 import BeautifulSoup
import pandas as pd
from urllib.parse import urlparse, parse_qs


# --- Filename parsing ---

def parse_filenames(folder_or_files):
    """
    Strict parse: only accept filenames with exactly TWO underscores: team_last_first.png
    Keeps invalid-format files too so they show up in results.
    """
    parsed = []
    for file in folder_or_files:
        name = os.path.basename(file)
        if not name.lower().endswith(".png"):
            continue

        base = name[:-4]
        # require exactly two underscores
        if base.count("_") != 2:
            parsed.append({
                "filename": name,
                "school": None,
                "last": None,
                "first": None,
                "format_valid": False,
                "format_msg": "Must have exactly two underscores: team_last_first.png"
            })
            continue

        parts = base.split("_")
        # now parts length should be exactly 3
        if len(parts) != 3:
            parsed.append({
                "filename": name,
                "school": None,
                "last": None,
                "first": None,
                "format_valid": False,
                "format_msg": "Unexpected parsing result"
            })
            continue

        school = parts[0].lower()
        last = parts[1].lower()
        first = parts[2].lower()

        parsed.append({
            "filename": name,
            "school": school,
            "last": last,
            "first": first,
            "format_valid": True,
            "format_msg": None
        })
    return parsed

# --- Normalization / roster scraping ---

def normalize(name: str) -> str:
    """Normalize names to your conventions."""
    name = name.lower()
    # remove suffixes
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    # remove accents
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    # remove punctuation except hyphens and spaces
    name = re.sub(r"[^\w\s-]", "", name)
    # collapse spaces
    name = re.sub(r"\s+", " ", name).strip()
    return name

def scrape_names(url: str):
    """
    Scrape roster names from Sidearm / common college sports websites.
    Returns a list of normalized 'first last' strings.
    Handles data-sort attributes and avoids pronunciation widgets.
    """
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        found = []

        # --- Step 1: Look for Sidearm table cells with player names ---
        for td in soup.select("td.sidearm-table-player-name"):
            # Prefer data-sort if available (clean "Last, First")
            if td.has_attr("data-sort"):
                try:
                    last, first = [s.strip() for s in td["data-sort"].split(",", 1)]
                    found.append(f"{first} {last}")
                except Exception:
                    continue
            else:
                # Fallback: get text of the <a> link (ignore child <img> pronunciation)
                a_tag = td.find("a")
                if a_tag:
                    found.append(a_tag.get_text(" ", strip=True))

        # --- Step 2: If none found, try generic roster links / anchors ---
        if not found:
            for a in soup.select("a"):
                txt = a.get_text(" ", strip=True)
                if txt and len(txt.split()) <= 4:  # heuristic for names
                    found.append(txt)

        # --- Step 3: Deduplicate and normalize ---
        unique = []
        seen = set()
        for nm in found:
            # Handle "Last, First" if somehow missed
            if "," in nm:
                parts = [p.strip() for p in nm.split(",", 1)]
                if len(parts) == 2:
                    nm = f"{parts[1]} {parts[0]}"
            cleaned = normalize(nm)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique.append(cleaned)

        return unique

    except Exception as e:
        st.error(f"Error scraping roster URL: {e}")
        return []


# --- Google Drive folder helpers (no API) ---

def _extract_drive_folder_id(url: str) -> str | None:
    """
    Extracts the folder ID from a Google Drive folder URL.
    Supports URLs like:
      - https://drive.google.com/drive/folders/<FOLDER_ID>?usp=sharing
      - https://drive.google.com/drive/u/0/folders/<FOLDER_ID>
      - https://drive.google.com/open?id=<FOLDER_ID>
    """
    try:
        parsed = urlparse(url)
        # path-based
        m = re.search(r"/folders/([a-zA-Z0-9_-]+)", parsed.path)
        if m:
            return m.group(1)
        # query-based (?id=)
        qs = parse_qs(parsed.query or "")
        if "id" in qs and len(qs["id"]) > 0:
            return qs["id"][0]
    except:
        pass
    return None

def get_drive_folder_png_filenames(folder_url: str) -> list[str]:
    """
    Fetch PNG filenames from a PUBLIC Google Drive folder without Google API.
    Uses the 'embeddedfolderview' endpoint, which returns parseable HTML.
    """
    folder_id = _extract_drive_folder_id(folder_url)
    if not folder_id:
        st.warning("Could not recognize a Google Drive folder ID from the URL.")
        return []

    # Try embedded folder view first (most reliable for scraping)
    candidates = [
        f"https://drive.google.com/embeddedfolderview?id={folder_id}#list",
        f"https://drive.google.com/embeddedfolderview?id={folder_id}#grid",
        # Fallbacks (may not contain names, but try anyway)
        f"https://drive.google.com/drive/folders/{folder_id}",
        f"https://drive.google.com/drive/u/0/folders/{folder_id}",
    ]

    for url in candidates:
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # Strategy: collect all visible text nodes and filter for *.png
            texts = [t.strip() for t in soup.stripped_strings if t.strip()]
            pngs = [t for t in texts if t.lower().endswith(".png")]

            # Deduplicate while preserving order
            seen = set()
            out = []
            for x in pngs:
                if x not in seen:
                    seen.add(x)
                    out.append(x)

            if out:
                return out
        except Exception:
            continue

    # If nothing found, give a hint
    st.info(
        "No .png names found. Make sure the folder is set to 'Anyone with the link' (Viewer). "
        "Then reload and try again."
    )
    return []

# --- Comparison logic ---

def check_mismatches(parsed_files, official_names, school_prefix):
    """
    For each parsed file:
      - if filename format invalid -> 'Invalid filename format'
      - else if school slug mismatch -> 'School prefix mismatch'
      - else if canonical (first last) matches roster -> OK
      - else if flipped (last first) matches roster -> 'First/last name order flipped' (suggest correction from roster)
      - else -> 'Name not in roster'
    Suggestion now uses the *official roster name* as scraped from the school URL.
    """
    data = []
    official_keys = {normalize(n): n for n in official_names}  # map normalized -> original roster spelling

    for entry in parsed_files:
        filename = entry.get("filename")
        fmt_ok = entry.get("format_valid", False)
        reason = ""
        suggestion = None

        if not fmt_ok:
            reason = entry.get("format_msg", "Invalid filename format")
            data.append({
                "filename": filename,
                "school": entry.get("school"),
                "last": entry.get("last"),
                "first": entry.get("first"),
                "reason": reason,
                "suggestion": suggestion
            })
            continue

        school = entry["school"]
        raw_last = entry["last"] or ""
        raw_first = entry["first"] or ""

        last = raw_last.replace("_", " ").strip()
        first = raw_first.replace("_", " ").strip()

        canonical = normalize(f"{first} {last}")   # expected
        flipped = normalize(f"{last} {first}")

        # School slug check
        if school != school_prefix.lower():
            reason = "School prefix mismatch"
        else:
            if canonical in official_keys:
                reason = ""  # OK
            elif flipped in official_keys:
                reason = "First/last name order flipped"
                roster_name = official_keys[flipped]
                fn, ln = roster_name.split(" ", 1)
                suggestion = f"{school_prefix}_{ln.lower()}_{fn.lower()}.png"
            else:
                reason = "Name not in roster"
                # Fallback: suggest based on first valid roster name if any
                if official_names:
                    roster_name = official_names[0]
                    fn, ln = roster_name.split(" ", 1)
                    suggestion = f"{school_prefix}_{ln.lower()}_{fn.lower()}.png"

        data.append({
            "filename": filename,
            "school": school,
            "last": raw_last,
            "first": raw_first,
            "reason": reason,
            "suggestion": suggestion
        })

    return pd.DataFrame(data)

# --- Streamlit UI ---

st.title("School Roster Photo Name Checker (Local or Google Drive Folder)")

source = st.radio("Where are the images stored?", ["Local folder", "Google Drive folder"])

image_files: list[str] = []

if source == "Local folder":
    folder_path = st.text_input("Paste the path to your image folder here:")
    if folder_path and os.path.exists(folder_path):
        image_files = [f for f in os.listdir(folder_path) if f.lower().endswith(".png")]
        st.caption(f"Found {len(image_files)} .png files locally.")
    elif folder_path:
        st.error("Folder path does not exist.")

elif source == "Google Drive folder":
    drive_folder_url = st.text_input("Paste the PUBLIC Google Drive folder URL here:")
    if drive_folder_url:
        image_files = get_drive_folder_png_filenames(drive_folder_url)
        if image_files:
            st.success(f"Found {len(image_files)} .png files in the Drive folder.")
            with st.expander("Show detected filenames"):
                st.write(image_files)

school_prefix = st.text_input("Enter the school prefix (e.g., cal, oregon):")
school_url = st.text_input("Paste the school roster URL here:")

if st.button("Check Files"):
    if not image_files:
        st.error("No image files detected yet. Make sure your source and link/path are correct.")
    elif not school_prefix or not school_url:
        st.error("Please fill in both the school prefix and the roster URL.")
    else:
        parsed_files = parse_filenames(image_files)
        if not parsed_files:
            st.warning(
                "No filenames matched the expected pattern 'teamabbr_lastname_firstname.png'. "
                "Double-check the files in the folder."
            )
        else:
            official_names = scrape_names(school_url)
            if not official_names:
                st.warning("No names detected from the roster page. Try another URL or check the site structure.")
            else:
                df = check_mismatches(parsed_files, official_names, school_prefix)
                st.subheader("File Check Results")
                st.dataframe(df)

