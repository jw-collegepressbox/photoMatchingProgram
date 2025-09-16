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
    """
    Normalize names to your conventions, including removing nicknames.
    """
    name = name.lower()
    
    # This specific regex will handle the Bob "Dylan" Johnson case
    # It removes any text enclosed in double quotes or apostrophes.
    name = re.sub(r'["“”‘’].*?["“”‘’]', '', name)
    
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


def scrape_player_names(url: str):
    """
    Scrape player names from a roster page by trying multiple common HTML patterns.
    Handles names with nicknames in quotes.
    """
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        found_names = set()

        # Strategy 1: Find all links to full player bios.
        for a_tag in soup.find_all('a', href=re.compile(r'/roster/.*/\d+')):
            # Prioritize the aria-label if it exists
            if a_tag.has_attr('aria-label') and " - View Full Bio" in a_tag['aria-label']:
                name = a_tag['aria-label'].split(' - View Full Bio')[0].strip()
                if "coach" not in name.lower() and "staff" not in name.lower():
                    found_names.add(name)
            else:
                name = a_tag.get_text(" ", strip=True)
                parent_li = a_tag.find_parent('li')
                if parent_li and 'sidearm-roster-coach' in parent_li.get('class', []):
                    continue
                if name and "coach" not in name.lower() and "staff" not in name.lower():
                    found_names.add(name)
        
        # Strategy 2: Look for common player name classes
        common_player_selectors = [
            ".s-text-regular-bold",  
            ".roster-list-item__title",
            ".player-name",
            "td.sidearm-table-player-name"
        ]
        
        for element in soup.select(", ".join(common_player_selectors)):
            name = element.get_text(" ", strip=True)
            if "coach" not in name.lower() and "staff" not in name.lower():
                found_names.add(name)
        
        # --- NEW LOGIC: Handle names with nicknames ---
        final_names = set()
        for name in found_names:
            # Regex to capture "First", "Nickname", and "Last"
            match = re.search(r'(\S+)\s+["“”‘’](.+?)["“”‘’]\s+(.+)', name)
            if match:
                first_name, nickname, last_name = match.groups()
                # Add the name using the first name
                final_names.add(normalize(f"{first_name} {last_name}"))
                # Add the name using the nickname
                final_names.add(normalize(f"{nickname} {last_name}"))
            else:
                final_names.add(normalize(name))

        return list(final_names)

    except Exception as e:
        st.error(f"Error scraping player names from URL: {e}")
        return []

def scrape_staff_names(url: str):
    """
    Scrape staff names from a roster page, targeting the 'sidearm-roster-coach' class.
    """
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        found_names = set()
        
        # Select all list items that are coaches
        staff_items = soup.select('li.sidearm-roster-coach')
        
        for item in staff_items:
            name_tag = item.find('div', class_='sidearm-roster-coach-name')
            if name_tag:
                name = name_tag.find('p').get_text(" ", strip=True)
                found_names.add(normalize(name))
                    
        return list(found_names)
        
    except Exception as e:
        st.error(f"Error scraping staff names from URL: {e}")
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

def check_mismatches(parsed_files, official_player_names, official_staff_names, school_prefix):
    """
    Checks filenames against both player and staff rosters.
    """
    data = []
    
    valid_player_names = [n for n in official_player_names if isinstance(n, str)]
    valid_staff_names = [n for n in official_staff_names if isinstance(n, str)]
    
    player_keys = {normalize(n): n for n in valid_player_names}
    staff_keys = {normalize(n): n for n in valid_staff_names}

    for entry in parsed_files:
        filename = entry.get("filename")
        fmt_ok = entry.get("format_valid", False)
        reason = ""
        suggestion = None

        if not fmt_ok:
            reason = entry.get("format_msg", "Invalid filename format")
        else:
            school = entry["school"]
            raw_last = entry["last"] or ""
            raw_first = entry["first"] or ""
            last = raw_last.replace("_", " ").strip()
            first = raw_first.replace("_", " ").strip()

            canonical = normalize(f"{first} {last}")
            with_nickname = normalize(f"{first} {raw_first.split('_')[1]} {last}") if '_' in raw_first else ""
            
            #flipped_canonical = normalize(f"{last} {first}")
            #flipped_with_nickname = normalize(f"{last} {raw_first.split('_')[1]} {first}") if '_' in raw_first else ""

            if school != school_prefix.lower():
                reason = "❌ School prefix mismatch"
            elif canonical in player_keys or with_nickname in player_keys:
                reason = "✅"
            elif canonical in staff_keys or with_nickname in staff_keys:
                reason = "OK (Staff)"
            else:
                reason = "❌ Name not in roster"
                if valid_player_names:
                    roster_name = valid_player_names[0]
                    
                    # --- FIX START ---
                    # Check if the string can be split into at least two parts
                    if " " in roster_name:
                        fn, ln = roster_name.split(" ", 1)
                        suggestion = f"{school_prefix}_{ln.lower()}_{fn.lower()}.png"
                    # --- FIX END ---
        
        data.append({
            "filename": filename,
            "school": school,
            "last": raw_last,
            "first": raw_first,
            "status": reason
        })

    return pd.DataFrame(data)

# --- Streamlit UI (main script) ---

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
            official_player_names = scrape_player_names(school_url)
            official_staff_names = scrape_staff_names(school_url)
            
            if not official_player_names and not official_staff_names:
                st.warning("No names detected from the roster page. Try another URL or check the site structure.")
            else:
                df = check_mismatches(parsed_files, official_player_names, official_staff_names, school_prefix)
                st.subheader("File Check Results")
                st.dataframe(df)
