import streamlit as st
import os
import unicodedata
import re
import requests
from bs4 import BeautifulSoup
import pandas as pd
from urllib.parse import urlparse, parse_qs
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time

# --- Filename parsing ---

def parse_filenames(folder_or_files):
    """
    Strict parse: only accept filenames in the format team_last_first.png.
    Marks files invalid if they are missing parts or improperly formatted.
    """
    parsed = []
    for file in folder_or_files:
        name = os.path.basename(file)
        if not name.lower().endswith(".png"):
            continue

        base = name[:-4]

        # Must contain exactly two underscores
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

        parts = base.split("_")  # should be [school, last, first]
        if len(parts) != 3 or not parts[1].strip() or not parts[2].strip():
            # Empty last or first name
            parsed.append({
                "filename": name,
                "school": None,
                "last": None,
                "first": None,
                "format_valid": False,
                "format_msg": "Missing last or first name"
            })
            continue

        school = parts[0].lower().strip()
        last = parts[1].lower().strip()
        first = parts[2].lower().strip()

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
    Handles nicknames in single or double quotes.
    """
    name = name.lower()
    # Remove nicknames in single or double quotes: "nickname" or 'nickname'
    name = re.sub(r'(["“”‘’\']).*?\1', '', name)
    # Remove suffixes like Jr, Sr, II, III
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    # Remove accents
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    # Remove non-word characters except spaces/hyphens
    name = re.sub(r"[^\w\s-]", "", name)
    # Collapse multiple spaces and trim
    name = re.sub(r"\s+", " ", name).strip()
    return name

def scrape_baylor_players(url: str):
    primary_names = {}
    nickname_names = {}

    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Baylor uses sidearm-roster-list-item-name > a for players
        for a in soup.select("li.sidearm-roster-list-item div.sidearm-roster-player-name a"):
            full_name = a.get_text(" ", strip=True)
            if full_name:
                primary_names[normalize(full_name)] = full_name

        return primary_names, nickname_names

    except Exception as e:
        st.error(f"Error scraping Baylor player names: {e}")
        return {}, {}


def scrape_player_names(url: str):
    """
    Scrape player names and nicknames from a roster page.
    """
    is_baylor = "baylorbears.com" in url.lower()
    found_names = set()

    # Keywords to filter out non-player entries
    invalid_keywords = [
        #"news", "schedule", "statistics", "videos",
        #"links", "gameday", "staff", "coach", "bio", "media",
        #"ireland", "tarheels2ireland", "central", "additional",
        #"more", "results", "events", "©", "menu", "25fb", "2025",
        #"photo"
    ]

    try:
        if is_baylor:
            return scrape_baylor_players(url)
        else:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            common_player_selectors = [
                ".s-text-regular-bold",
                ".roster-list-item__title",
                ".player-name",
                "td.sidearm-table-player-name",
                ".roster-list__item-name",
                "a.table__roster-name",
                "td.sidearm-roster-table-data a[title]",  # ✅ catches Baylor player links
                "td > a[href*='/roster/season/']",
                "a.table__roster-name span",
                'div[data-test-id="s-person-details__personal-single-line"] h3',
                'a[href*="/player/"]'
            ]

            for element in soup.select(", ".join(common_player_selectors)):
                name = element.get_text(" ", strip=True)
                lower_name = name.lower()

                # Add this new check to skip invalid keywords
                if any(word in lower_name for word in invalid_keywords):
                    continue

                if name and not re.search(r'(coach|staff|bio|view|jersey|number)', name, re.I):
                    found_names.add(name)

        primary_names = {}
        nickname_names = {}

        for name in found_names:
            match = re.search(r'(\S+)\s+["“”‘’](.+?)["“”‘’]\s+(.+)', name)
            if match:
                first_name, nickname, last_name = match.groups()
                primary_names[normalize(f"{first_name} {last_name}")] = name
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

        # Selectors for various staff list formats
        staff_items = soup.select('li.sidearm-roster-coach, .roster-list-item.staff, tr[data-v-7436a2c8]')
        
        # New selector for h3 name format
        h3_staff_names = soup.select('a[href*="/roster/staff/"] h3')

        # Added selector for Clemson's staff table format
        staff_rows = soup.select('tr.person__item')

        # Added selector for Georgia Tech's staff table format
        gt_staff_rows = soup.select('tr:has(td > a[href*="/coaches/"])')

        # New selector for Stanford's staff format
        stanford_staff_links = soup.select('a.table__roster-name[href*="/staff/"]')
        
        # New selector for Virginia Tech staff (using the same class as players, but filtering by URL)
        vt_staff_links = soup.select('a.roster-list-item__title[href*="/staff/"]')

        # New selector for Virginia coaches
        uva_coach_links = soup.select('a[href*="/coach/"]')

        # Process standard staff list formats
        for item in staff_items:
            # Check for the UNC-specific format within the table row
            if 'tr' in item.name and item.has_attr('data-v-7436a2c8'):
                name_tag = item.select_one('td:first-of-type .s-text-regular-bold')
                title_tag = item.select_one('td:last-of-type span')
            else:
                name_tag = item.select_one('.sidearm-roster-coach-name p, .roster-list-item__title')
                title_tag = item.select_one('.sidearm-roster-coach-title span, .roster-list-item__profile-field--position')

            if not name_tag:
                continue

            name = name_tag.get_text(" ", strip=True)
            title = title_tag.get_text(" ", strip=True) if title_tag else "Staff"

            if "bio" in name.lower() or "view" in name.lower():
                continue

            staff_dict[normalize(name)] = {"name": name, "title": title}
        
        # Process new h3 name format
        for name_h3 in h3_staff_names:
            name = name_h3.get_text(" ", strip=True)
            staff_dict[normalize(name)] = {"name": name, "title": "Staff"}

        # Process the new Clemson staff table format
        for row in staff_rows:
            name_tag = row.select_one('td:first-of-type a')
            title_tag = row.select_one('td:nth-of-type(2)')
            if name_tag and title_tag:
                name = name_tag.get_text(" ", strip=True)
                title = title_tag.get_text(" ", strip=True)
                staff_dict[normalize(name)] = {"name": name, "title": title}
        
        # Process the new Georgia Tech staff table format
        for row in gt_staff_rows:
            name_tag = row.select_one('td > a[href*="/coaches/"]')
            title_tag = name_tag.parent.find_next_sibling('td')
            if name_tag and title_tag:
                name = name_tag.get_text(" ", strip=True)
                title = title_tag.get_text(" ", strip=True)
                staff_dict[normalize(name)] = {"name": name, "title": title}

        # Process the new Stanford staff format
        for link in stanford_staff_links:
            name_span = link.select_one('span')
            if name_span:
                name = name_span.get_text(" ", strip=True)
                staff_dict[normalize(name)] = {"name": name, "title": "Staff"}

        # New selector for Syracuse staff
        syracuse_staff_links = soup.select('div[data-test-id="s-person-details__personal-single-line"] a[href*="/roster/staff/"]')
        for link in syracuse_staff_links:
            name_tag = link.select_one('h3')
            if name_tag:
                name = name_tag.get_text(" ", strip=True)
                staff_dict[normalize(name)] = {"name": name, "title": "Staff"}
        
        # Process new Virginia Tech staff format
        for link in vt_staff_links:
            name = link.get_text(" ", strip=True)
            staff_dict[normalize(name)] = {"name": name, "title": "Staff"}

        # New selector for Virginia coaches
        for link in uva_coach_links:
            name = link.get_text(" ", strip=True)
            staff_dict[normalize(name)] = {"name": name, "title": "Coach"}

        # Additional check for UNC format where coaches are listed in a separate table
        coach_names = soup.select('a[href*="/coaches/"] span.s-text-regular-bold')
        for name_span in coach_names:
            name = name_span.get_text(" ", strip=True)
            staff_dict[normalize(name)] = {"name": name, "title": "Coach"}

        # 🟢 Baylor-specific staff scraping — ADDITION
        for li in soup.select("li.sidearm-roster-staff-item"):
            name_tag = li.select_one(".sidearm-roster-staff-name")
            title_tag = li.select_one(".sidearm-roster-staff-title")
            if name_tag:
                name = name_tag.get_text(" ", strip=True)
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
    existing_player_files = set()
    for entry in parsed_files:
        if not entry.get("format_valid", False):
            continue
        normalized_name = normalize(f"{entry.get('first','')} {entry.get('last','')}")
        if normalized_name not in staff_dict:  # ignore staff files
            existing_player_files.add(normalized_name)

    missing_players = []

    for normalized_name, roster_name in player_keys.items():
        # 🆕 Add this check to explicitly skip staff members
        if normalized_name in staff_dict:
            continue

        # Skip any roster names that are clearly not real players
        if roster_name.lower() in ["full bio", "view full bio"]:
            continue  # skip fake entries

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
    # Inside check_mismatches_and_missing
    parsed_files = [f for f in parsed_files if f.get("format_valid", False)]

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

        # --- Scrape players and staff ---
        player_keys, nickname_keys = scrape_player_names(school_url)
        staff_dict = scrape_staff_names(school_url)

        # --- Remove staff accidentally scraped as players ---
        for staff_name in list(staff_dict.keys()):
            if staff_name in player_keys:
                del player_keys[staff_name]
            if staff_name in nickname_keys:
                del nickname_keys[staff_name]

        # --- Remove non-player entries using invalid keywords ---
        invalid_keywords = [
            "coach", "staff", "jersey", "number", "manager", "director",
            "head coach", "assistant", "trainer", "operations"
        ]
        for key in list(player_keys.keys()):
            lower_name = player_keys[key].lower()
            if any(word in lower_name for word in invalid_keywords):
                del player_keys[key]
                if key in nickname_keys:
                    del nickname_keys[key]

        if not player_keys:
            st.warning("No players detected from the roster page.")
        else:
            df = check_mismatches_and_missing(
                parsed_files, player_keys, nickname_keys, staff_dict, school_prefix
            )
            st.subheader("Roster Photo Check")
            st.dataframe(df)

st.subheader("Debug: Staff Dictionary Contents")
if 'staff_dict' in locals():
    st.write(staff_dict)
