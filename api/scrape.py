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
SEASON_START = date(2025, 11, 15)
SEASON_END   = date(2026,  4, 19)
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
    for _ in range(2):
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
            # Scrape instructor name — take the text immediately following the "Instructor:" label.
            # The page uses <br> separators and other <b> labels ("POD:", etc), so avoid
            # flattening the entire <td> which can leak metadata into the name.
            def _norm(s: str) -> str:
                return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()

            def _is_label(s: str) -> bool:
                # Labels are typically like "POD:" / "Level:" etc (sometimes with extra spaces)
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

                # Find the "Instructor" label in this cell.
                start_idx = None
                for i, s in enumerate(strings):
                    if re.match(r"^instructor\s*:?\s*$", s, flags=re.I):
                        start_idx = i + 1
                        break
                    if re.match(r"^instructor\s*:\s*.+$", s, flags=re.I):
                        # Sometimes the label and the value can be in the same string.
                        candidate = _norm(re.sub(r"^instructor\s*:\s*", "", s, flags=re.I))
                        if candidate:
                            name = candidate
                            start_idx = None
                        break

                if name:
                    break
                if start_idx is None:
                    continue

                # Collect subsequent strings until the next label ("POD:", etc).
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
            "start_datetime": "",
        }
        priv = anchor.find_next("div", id=lambda i: i and i.startswith("privateDetails"))
        if priv:
            for td in priv.find_all("td"):
                t = td.get_text(strip=True)
                if t and "/" in t and ":" in t:
                    l["start_datetime"] = t
                    break
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
