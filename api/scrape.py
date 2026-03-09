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
from time import sleep

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


def _new_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _extract_rnd_value(soup):
    inp = soup.find("input", {"name": "rnd"})
    if inp and inp.get("value"):
        return inp["value"]
    return "rnd=0"


def _has_logout(soup):
    return soup.find("a", string=lambda t: t and "Logout" in t) is not None


def _auth_error_text(soup):
    text = soup.get_text(" ", strip=True).lower()
    if "cannot be authenticated" in text:
        return "cannot be authenticated"
    err = soup.find(class_="crErrorMessage")
    if err:
        return err.get_text(strip=True)
    return "Login failed"


def _extract_instructor_name(soup):
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


def login(pass_number, password, max_attempts=5):
    for attempt in range(1, max_attempts + 1):
        session = _new_session()
        try:
            r = session.get(LOGIN_URL, timeout=20)
            r.raise_for_status()
            # Login page JS sets this cookie, so emulate it for requests.
            session.cookies.set("cookietest", "nothing", domain="instructor.snow.com", path="/")

            soup_login = BeautifulSoup(r.text, "html.parser")
            rnd = _extract_rnd_value(soup_login)
            resp = session.post(LOGIN_URL, timeout=20, allow_redirects=True, data={
                "rnd": rnd,
                "action": "LogIn",
                "passNumber": pass_number,
                "password": password,
                "userAction.x": "33",
                "userAction.y": "7",
            })
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            if _has_logout(soup):
                return session, _extract_instructor_name(soup) or ""
        except requests.RequestException:
            # Retry on transient network/server issues.
            pass
        sleep(min(1.5 * attempt, 5))

    raise ValueError(_auth_error_text(soup if "soup" in locals() else BeautifulSoup("", "html.parser")))


def _post_schedule(session, start_dt):
    r = session.get(LOGIN_URL, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")
    resp = session.post(LOGIN_URL, timeout=20, allow_redirects=True, data={
        "rnd": _extract_rnd_value(soup),
        "action": "GetSchedule",
        "arrivalDate": f"{start_dt.month}/{start_dt.day}/{start_dt.year}",
        "refresh.x": "47", "refresh.y": "10",
    })
    return BeautifulSoup(resp.text, "html.parser")


def fetch_window(session, start_dt, pass_number=None, password=None):
    soup = _post_schedule(session, start_dt)
    if (not _has_logout(soup)) or ("cannot be authenticated" in soup.get_text(" ", strip=True).lower()):
        if not pass_number or not password:
            raise ValueError("Session expired and credentials were not provided for re-login")
        session, _ = login(pass_number, password)
        soup = _post_schedule(session, start_dt)

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
    return lessons, session


def scrape_season(pass_number, password):
    session, instructor_name = login(pass_number, password)
    all_lessons = {}
    cur = SEASON_START
    while cur <= SEASON_END:
        try:
            lessons, session = fetch_window(session, cur, pass_number, password)
            for l in lessons:
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
