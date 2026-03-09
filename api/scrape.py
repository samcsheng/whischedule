"""
Vercel serverless function — scrapes the Whistler Blackcomb instructor portal.
POST /api/scrape  { "passNumber": "...", "password": "..." }
"""

import json
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler
import re

BASE_URL  = "https://instructor.snow.com"
LOGIN_URL = f"{BASE_URL}/snow/instructorTools.asp"
SEASON_START = date(2025, 11, 1)
SEASON_END   = date(2026,  5, 31)
WINDOW_DAYS  = 21

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": LOGIN_URL,
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
}


def login(session, pass_number, password):
    for _ in range(4):
        r = session.get(LOGIN_URL, timeout=20)
        inp = BeautifulSoup(r.text, "html.parser").find("input", {"name": "rnd"})
        if not inp:
            raise ValueError("Could not load login page")
        resp = session.post(LOGIN_URL, timeout=20, allow_redirects=True, data={
            "rnd": inp["value"], "action": "LogIn",
            "passNumber": pass_number, "password": password,
            "userAction.x": "33", "userAction.y": "7",
        })
        soup = BeautifulSoup(resp.text, "html.parser")
        if soup.find("a", string=lambda t: t and "Logout" in t):
            # Scrape instructor name
            def _norm(s: str) -> str:
                return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

            def _is_label(s: str) -> bool:
                t = _norm(s).lower()
                if not t:
                    return False
                if t.endswith(":"):
                    return True
                return t in {"pod", "level", "discipline", "cert", "start date", "pass", "pass number"}

            name = ""
            for td in soup.find_all("td"):
                strings = [_norm(s) for s in td.stripped_strings]
                if not strings:
                    continue

                start_idx = None
                for i, s in enumerate(strings):
                    if re.match(r"^instructor\s*:?\s*$", s, flags=re.I):
                        start_idx = i + 1
                        break
                    if re.match(r"^instructor\s*:\s*.+$", s, flags=re.I):
                        candidate = _norm(re.sub(r"^instructor\s*:\s*", "", s, flags=re.I))
                        if candidate:
                            name = candidate
                            start_idx = None
                        break

                if name:
                    break
                if start_idx is None:
                    continue

                parts = []
                for s in strings[start_idx:]:
                    if _is_label(s):
                        break
                    parts.append(s)
                candidate = _norm(" ".join(parts))
                if candidate:
                    name = candidate
                    break

            return name
        err = soup.find(class_="crErrorMessage")
        raise ValueError(err.get_text(strip=True) if err else "Login failed")


def extract_private_details(details_div):
    """Extract all private lesson details from the hidden privateDetails div."""
    if not details_div:
        return None
    
    table = details_div.find("table")
    if not table:
        return None
    
    rows = table.find_all("tr")
    if len(rows) < 2:
        return None
    
    # Data row (second row)
    data_row = rows[1]
    data_cells = data_row.find_all("td")
    
    if len(data_cells) < 7:
        return None
    
    # Comments row (third row)
    lesson_comments = ""
    schedule_comments = ""
    if len(rows) > 2:
        comment_row = rows[2]
        comment_cells = comment_row.find_all("td")
        if len(comment_cells) >= 2:
            lesson_comments = comment_cells[0].get_text(strip=True).replace("Lesson Comments:", "").strip()
            schedule_comments = comment_cells[1].get_text(strip=True).replace("Schedule Comments:", "").strip()
    
    return {
        "reservationId": data_cells[0].get_text(strip=True),
        "guestName": data_cells[1].get_text(strip=True),
        "cityState": data_cells[2].get_text(strip=True),
        "skillLevel": int(data_cells[3].get_text(strip=True)) if data_cells[3].get_text(strip=True).isdigit() else 0,
        "startDateTime": data_cells[4].get_text(strip=True),
        "operatorId": data_cells[5].get_text(strip=True),
        "startLocation": data_cells[6].get_text(strip=True),
        "lessonComments": lesson_comments,
        "scheduleComments": schedule_comments,
    }


def fetch_window(session, start_dt):
    r = session.get(LOGIN_URL, timeout=20)
    inp = BeautifulSoup(r.text, "html.parser").find("input", {"name": "rnd"})
    resp = session.post(LOGIN_URL, timeout=20, allow_redirects=True, data={
        "rnd": inp["value"] if inp else "rnd=0",
        "action": "GetSchedule",
        "arrivalDate": start_dt.strftime("%-m/%-d/%Y"),
        "refresh.x": "47", "refresh.y": "10",
    })
    soup = BeautifulSoup(resp.text, "html.parser")
    lessons = []
    
    for anchor in soup.find_all("a", attrs={"name": lambda v: v and v.startswith("lesson")}):
        row = anchor.find_next("tr")
        if not row:
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        
        l = {
            "date":           cells[0].get_text(strip=True),
            "hours":          cells[1].get_text(strip=True),
            "activity":       cells[2].get_text(strip=True),
            "assignment":     cells[3].get_text(strip=True),
            "client":         cells[4].get_text(strip=True),
            "privateDetails": None,
        }
        
        # Extract private details from hidden div
        priv = anchor.find_next("div", id=lambda i: i and i.startswith("privateDetails"))
        if priv:
            private_details = extract_private_details(priv)
            if private_details:
                l["privateDetails"] = private_details
        
        lessons.append(l)
    
    return lessons


def scrape_season(pass_number, password):
    session = requests.Session()
    session.headers.update(HEADERS)
    instructor_name = login(session, pass_number, password) or ""
    all_lessons = {}
    cur = SEASON_START
    while cur <= SEASON_END:
        try:
            for l in fetch_window(session, cur):
                key = (l["date"], l["activity"])
                if key not in all_lessons:
                    all_lessons[key] = l
        except Exception:
            pass
        cur += timedelta(days=WINDOW_DAYS)
    return sorted(all_lessons.values(), key=lambda l: l["date"]), instructor_name


class handler(BaseHTTPRequestHandler):

    def _send(self, status, body):
        self.send_response(status)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "Invalid JSON"})

        pn = (body.get("passNumber") or "").strip()
        pw = (body.get("password")   or "").strip()

        if not pn or not pw:
            return self._send(400, {"error": "passNumber and password required"})

        try:
            lessons, name = scrape_season(pn, pw)
            self._send(200, {"lessons": lessons, "instructorName": name})
        except ValueError as e:
            self._send(401, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"Scrape failed: {str(e)}"})