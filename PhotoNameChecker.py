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
    Parse image filenames following the convention:
    teamabbr_lastname_firstname.png
    Handles compound first/last names (everything after second underscore is first name).
    """
    parsed = []
    for file in folder_or_files:
        name = os.path.basename(file)
        if not name.lower().endswith(".png"):
            continue
        parts = name[:-4].split("_")
        if len(parts) < 3:
            continue

        school = parts[0].lower()
        last = parts[1].lower()
        first = "_".join(parts[2:]).lower()

        parsed.append({
            "school": school,
            "last": last,
            "first": first,
            "filename": name
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
    Scrape names from roster pages in a provider-agnostic way.
    Regex captures hyphens, apostrophes, and multi-part names.
    """
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        page_text = response.text

        # e.g., "E'Lla Boykin", "Pape Abdoulaye Sy", "Lin-Manuel Miranda"
        name_pattern = r"\b[A-Z][a-zA-Z'-.]+(?: [A-Z][a-zA-Z'-.]+)+\b"
        matches = re.findall(name_pattern, page_text)

        seen = set()
        names = []
        for nm in matches:
            cleaned = normalize(nm)
            if cleaned not in seen:
                seen.add(cleaned)
                names.append(cleaned)
        return names
    except Exception as e:
        st.error(f"Error scraping the URL: {e}")
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
    """Check filenames against official roster names."""
    data = []
    official_keys = [normalize(n) for n in official_names]

    for entry in parsed_files:
        full_name = f"{entry['first']} {entry['last']}"
        flipped_name = f"{entry['last']} {entry['first']}"
        reason = ""
        suggestion = None

        if entry['school'] != school_prefix.lower():
            reason = "School prefix mismatch"
            suggestion = f"{school_prefix}_{entry['last']}_{entry['first']}.png"
        elif normalize(full_name) not in official_keys and normalize(flipped_name) not in official_keys:
            reason = "Name not in roster"

        data.append({
            "filename": entry['filename'],
            "school": entry['school'],
            "last": entry['last'],
            "first": entry['first'],
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

