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
    name = re.sub(r'["“”‘’].*?["“”‘’]', '', name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def scrape_player_names(url: str):
    """
    Scrape player names and nicknames from a roster page.
    Returns two dictionaries:
    - primary_names: {normalized_first_last: original_name}
    - nickname_names: {normalized_nickname_last: original_name}
    """
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        found_names = set()
        
        # Strategy 1: Find all links to full player bios.
        for a_tag in soup.find_all('a', href=re.compile(r'/roster/.*/\d+')):
            name = a_tag.get_text(" ", strip=True)
            if "coach" not in name.lower() and "staff" not in name.lower():
                found_names.add(name)
        
        # Strategy 2: Look for common player name classes
        common_player_selectors = [
            ".s-text-regular-bold",  
            ".roster-list-item__title",
            ".player-name",
            "td.sidearm-table-player-name",
            ".roster-list__item-name"
        ]
        
        for element in soup.select(", ".join(common_player_selectors)):
            name = element.get_text(" ", strip=True)
            if "coach" not in name.lower() and "staff" not in name.lower():
                found_names.add(name)

        primary_names = {}
        nickname_names = {}

        for name in found_names:
            match = re.search(r'(\S+)\s+["“”‘’](.+?)["“”‘’]\s+(.+)', name)
            if match:
                first_name, nickname, last_name = match.groups()
                # Store the correct name in the primary dictionary
                primary_names[normalize(f"{first_name} {last_name}")] = name
                # Store the nickname in the nickname dictionary
                nickname_names[normalize(f"{nickname} {last_name}")] = name
            else:
                primary_names[normalize(name)] = name
        
        return primary_names, nickname_names

    except Exception as e:
        st.error(f"Error scraping player names from URL: {e}")
        return {}, {}

def scrape_staff_names(url: str):
    """
    Scrape staff names and titles from a roster page.
    Returns a dictionary: {normalized_name: {"name": original_name, "title": title}}
    """
    staff_dict = {}
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Staff list items (schools often use a specific class)
        staff_items = soup.select('li.sidearm-roster-coach, .roster-list-item.staff')

        for item in staff_items:
            # Name
            name_tag = item.select_one('.sidearm-roster-coach-name p, .roster-list-item__title')
            if not name_tag:
                continue
            name = name_tag.get_text(" ", strip=True)
            
            # Title / position
            title_tag = item.select_one('.sidearm-roster-coach-title span, .roster-list-item__profile-field--position')
            title = title_tag.get_text(" ", strip=True) if title_tag else "Staff"

            staff_dict[normalize(name)] = {"name": name, "title": title}
        return staff_dict

    except Exception as e:
        st.error(f"Error scraping staff names: {e}")
        return {}

def generate_expected_filenames(player_keys, school_prefix):
    """
    Only generate expected filenames for players (ignore staff).
    """
    expected_files = []
    for normalized_name, original_name in player_keys.items():
        parts = original_name.split(" ")
        if len(parts) >= 2:
            first = parts[0].lower()
            last = parts[-1].lower()
            expected_filename = f"{school_prefix}_{last}_{first}.png"
            expected_files.append(expected_filename)
    return expected_files


def find_missing_players(parsed_files, player_keys, staff_dict, school_prefix):
    """
    Returns a list of missing player files with their expected filename.
    Staff files are ignored completely.
    """
    # Only consider actual file names that are NOT staff
    existing_player_files = set()
    for entry in parsed_files:
        if not entry.get("format_valid", False):
            continue
        normalized_name = normalize(f"{entry.get('first','')} {entry.get('last','')}")
        if normalized_name not in staff_dict:  # ignore staff files
            existing_player_files.add(normalized_name)

    missing_players = []

    for normalized_name, roster_name in player_keys.items():
        if normalized_name not in existing_player_files:
            parts = roster_name.split(" ", 1)
            if len(parts) == 2:
                first, last = parts
            else:
                first = parts[0]
                last = ""
            expected_filename = f"{school_prefix}_{last.lower()}_{first.lower()}.png" if last else f"{school_prefix}_{first.lower()}.png"
            missing_players.append({
                "filename": None,
                "first": first.lower(),
                "last": last.lower() if last else "",
                "status": "⚠️ Missing",
                "suggestion": expected_filename,
                "name": roster_name
            })

    return missing_players


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

# --- Comparison logic ---

def check_mismatches_and_missing(parsed_files, player_keys, nickname_keys, staff_dict, school_prefix):
    data = []

    # --- Step 1: existing files ---
    for entry in parsed_files:
        filename = entry.get("filename")
        fmt_ok = entry.get("format_valid", False)
        raw_last = entry.get("last") or ""
        raw_first = entry.get("first") or ""
        suggestion = None
        roster_name = None

        if not fmt_ok:
            status = entry.get("format_msg", "Invalid filename format")
        else:
            school = entry["school"]
            normalized_filename_name = normalize(f"{raw_first} {raw_last}")

            # Staff first
            if normalized_filename_name in staff_dict:
                staff_info = staff_dict[normalized_filename_name]
                status = f"❌ Not a Player ({staff_info.get('title','Staff')})"

            elif school != school_prefix.lower():
                status = "❌ School prefix mismatch"

            elif normalized_filename_name in player_keys:
                status = "✅"
                roster_name = player_keys[normalized_filename_name]

            elif normalized_filename_name in nickname_keys:
                original_roster_name = nickname_keys[normalized_filename_name]
                match = re.search(r'(\S+)\s+["“”‘’]', original_roster_name)
                if match:
                    first_name = normalize(match.group(1))
                    status = "❌ Nickname used instead of First Name"
                    suggestion = f"{school_prefix}_{raw_last}_{first_name}.png"
                    roster_name = original_roster_name
                else:
                    status = "❌ Nickname used instead of First Name"
                    roster_name = original_roster_name
            else:
                status = "❌ Name not in roster"

        data.append({
            "filename": filename,
            "first": raw_first,
            "last": raw_last,
            "status": status,
            "name": roster_name
        })

    # --- Step 2: missing players (ignore staff) ---
    missing_players = find_missing_players(parsed_files, player_keys, staff_dict, school_prefix)
    data.extend(missing_players)

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
        st.error("No image files detected yet.")
    elif not school_prefix or not school_url:
        st.error("Please fill in both the school prefix and the roster URL.")
    else:
        parsed_files = parse_filenames(image_files)
        player_keys, nickname_keys = scrape_player_names(school_url)
        staff_dict = scrape_staff_names(school_url)

        if not player_keys:
            st.warning("No players detected from the roster page.")
        else:
            df = check_mismatches_and_missing(parsed_files, player_keys, nickname_keys, staff_dict, school_prefix)
            st.subheader("Roster Photo Check")
            st.dataframe(df)
