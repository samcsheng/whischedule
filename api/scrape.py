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
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def login(pass_number, password, max_attempts=5):
    soup = BeautifulSoup("", "lxml")
    for attempt in range(1, max_attempts + 1):
        session = _new_session()
        try:
            r = session.get(LOGIN_URL, timeout=20)
            r.raise_for_status()
            session.cookies.set("cookietest", "nothing", domain="instructor.snow.com", path="/")

            soup_login = BeautifulSoup(r.text, "lxml")
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
            soup = BeautifulSoup(resp.text, "lxml")
            if _has_logout(soup):
                return session, _extract_instructor_name(soup) or ""
        except requests.RequestException:
            pass
        sleep(min(1.5 * attempt, 5))

    raise ValueError(_auth_error_text(soup))


def _post_schedule(session, start_dt):
    # Skip the preflight GET — "rnd=0" is accepted by the portal and removes
    # one round-trip per window (~11 fewer HTTP requests across a full scrape).
    resp = session.post(LOGIN_URL, timeout=20, allow_redirects=True, data={
        "rnd": "rnd=0",
        "action": "GetSchedule",
        "arrivalDate": f"{start_dt.month}/{start_dt.day}/{start_dt.year}",
        "refresh.x": "47", "refresh.y": "10",
    })
    return BeautifulSoup(resp.text, "lxml")


def _clean_text(value):
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ").replace("\ufffd", " ")).strip()


def _parse_private_details(priv_div):
    details = {
        "reservationId": "",
        "guestName": "",
        "cityState": "",
        "skillLevel": "",
        "startDateTime": "",
        "operatorId": "",
        "startLocation": "",
        "lessonComments": "",
        "scheduleComments": "",
    }
    table = priv_div.find("table")
    if not table:
        return details

    rows = table.find_all("tr")
    data_row_idx = None
    for idx, row in enumerate(rows):
        txt = _clean_text(row.get_text(" ", strip=True)).lower()
        if "reservation id" in txt and "guest name" in txt and "start location" in txt:
            data_row_idx = idx + 1
            break

    if data_row_idx is not None and data_row_idx < len(rows):
        vals = [_clean_text(td.get_text(" ", strip=True)) for td in rows[data_row_idx].find_all("td")]
        while len(vals) < 7:
            vals.append("")
        details["reservationId"] = vals[0]
        details["guestName"]     = vals[1]
        details["cityState"]     = vals[2]
        details["skillLevel"]    = vals[3]
        details["startDateTime"] = vals[4]
        details["operatorId"]    = vals[5]
        details["startLocation"] = vals[6]

    comments_row = None
    for row in rows:
        txt = _clean_text(row.get_text(" ", strip=True)).lower()
        if "lesson comments:" in txt and "schedule comments:" in txt:
            comments_row = row
            break
    if comments_row:
        cells = comments_row.find_all("td")
        if len(cells) >= 2:
            lesson_txt   = _clean_text(cells[0].get_text(" ", strip=True))
            schedule_txt = _clean_text(cells[1].get_text(" ", strip=True))
            details["lessonComments"]   = re.sub(r"^lesson comments:\s*",   "", lesson_txt,   flags=re.I).strip()
            details["scheduleComments"] = re.sub(r"^schedule comments:\s*", "", schedule_txt, flags=re.I).strip()

    return details


def fetch_window(session, start_dt, pass_number=None, password=None):
    soup = _post_schedule(session, start_dt)
    if (not _has_logout(soup)) or ("cannot be authenticated" in soup.get_text(" ", strip=True).lower()):
        if not pass_number or not password:
            raise ValueError("Session expired and credentials were not provided for re-login")
        session, _ = login(pass_number, password)
        soup = _post_schedule(session, start_dt)

    lessons = []
    for row in soup.find_all("tr", class_=lambda c: c in {"row1", "row2"}):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        first_text = _clean_text(cells[0].get_text(" ", strip=True))
        if "," not in first_text:
            continue

        l = {
            "date":           first_text,
            "hours":          _clean_text(cells[1].get_text(" ", strip=True)),
            "activity":       _clean_text(cells[2].get_text(" ", strip=True)),
            "assignment":     _clean_text(cells[3].get_text(" ", strip=True)),
            "client":         _clean_text(cells[4].get_text(" ", strip=True)),
            "start_datetime": "",
        }

        marker = row.find(id=lambda i: i and (i.startswith("privateDetailsText") or i.startswith("detailsText")))
        lesson_idx = None
        if marker and marker.get("id"):
            m = re.search(r"(\d+)$", marker.get("id", ""))
            if m:
                lesson_idx = m.group(1)
        priv = soup.find("div", id=f"privateDetails{lesson_idx}") if lesson_idx else None
        if not priv:
            priv = row.find_next("div", id=lambda i: i and i.startswith("privateDetails"))

        if priv:
            details = _parse_private_details(priv)
            if details.get("startDateTime"):
                l["start_datetime"] = details["startDateTime"]
            else:
                for td in priv.find_all("td"):
                    t = _clean_text(td.get_text(" ", strip=True))
                    if t and "/" in t and ":" in t:
                        l["start_datetime"] = t
                        break
            if l["start_datetime"] and not details.get("startDateTime"):
                details["startDateTime"] = l["start_datetime"]
            if l.get("client") and not details.get("guestName"):
                details["guestName"] = l["client"]
            l["private_details"] = details

        lessons.append(l)
    return lessons, session


def _session_from_cookies(cookies):
    """Spin up a fresh requests.Session pre-loaded with cookies from a prior login.
    Each thread gets its own Session object so there's no shared mutable state,
    but we only hit the login endpoint once for the entire scrape.
    """
    s = _new_session()
    for cookie in cookies:
        s.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
    return s


def _fetch_window_with_cookies(start_dt, cookies):
    """Fetch one window using pre-built cookies. No login needed."""
    session = _session_from_cookies(cookies)
    lessons, _ = fetch_window(session, start_dt)
    return lessons


def scrape_season(pass_number, password):
    # Single login — extract cookies and instructor name, then share cookies
    # across all parallel window fetches instead of logging in per-thread.
    auth_session, instructor_name = login(pass_number, password)
    cookies = list(auth_session.cookies)

    windows = []
    cur = SEASON_START
    while cur <= SEASON_END:
        windows.append(cur)
        cur += timedelta(days=WINDOW_DAYS)

    all_lessons = {}

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(_fetch_window_with_cookies, w, cookies): w for w in windows}
        for future in as_completed(futures):
            try:
                lessons = future.result()
                for l in lessons:
                    priv = l.get("private_details") or {}
                    key = (
                        l.get("date", ""),
                        l.get("activity", ""),
                        l.get("assignment", ""),
                        l.get("client", ""),
                        priv.get("reservationId", ""),
                        priv.get("startDateTime", ""),
                        l.get("start_datetime", ""),
                    )
                    if key not in all_lessons:
                        all_lessons[key] = l
            except Exception:
                pass

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
