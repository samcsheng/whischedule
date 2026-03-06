"""
Vercel serverless function — scrapes the Whistler Blackcomb instructor portal.
POST /api/scrape  { "passNumber": "...", "password": "..." }
Returns JSON array of lesson objects covering the full season.
"""

from http.server import BaseHTTPRequestHandler
import json, requests
from bs4 import BeautifulSoup
from datetime import date, timedelta

BASE_URL  = "https://instructor.snow.com"
LOGIN_URL = f"{BASE_URL}/snow/instructorTools.asp"

SEASON_START = date(2025, 11, 15)
SEASON_END   = date(2026,  4, 19)
WINDOW_DAYS  = 21

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": LOGIN_URL,
}


def login(session, pass_number, password):
    for attempt in range(2):
        r = session.get(LOGIN_URL, timeout=15)
        rnd = BeautifulSoup(r.text, "html.parser").find("input", {"name": "rnd"})["value"]
        payload = {
            "rnd": rnd, "action": "LogIn",
            "passNumber": pass_number, "password": password,
            "userAction.x": "33", "userAction.y": "7",
        }
        resp = session.post(LOGIN_URL, data=payload, timeout=15, allow_redirects=True)
        soup = BeautifulSoup(resp.text, "html.parser")
        if soup.find("a", string=lambda t: t and "Logout" in t):
            return True
        err = soup.find(class_="crErrorMessage")
        if err:
            raise ValueError(err.get_text(strip=True))
    raise ValueError("Login failed after 2 attempts")


def fetch_window(session, start_dt):
    date_str = start_dt.strftime("%-m/%-d/%Y")
    r = session.get(LOGIN_URL, timeout=15)
    rnd = BeautifulSoup(r.text, "html.parser").find("input", {"name": "rnd"})
    rnd_val = rnd["value"] if rnd else "rnd=0"
    payload = {
        "rnd": rnd_val, "action": "GetSchedule",
        "arrivalDate": date_str,
        "refresh.x": "47", "refresh.y": "10",
    }
    resp = session.post(LOGIN_URL, data=payload, timeout=15, allow_redirects=True)
    return parse_lessons(BeautifulSoup(resp.text, "html.parser"))


def parse_lessons(soup):
    lessons = []
    for anchor in soup.find_all("a", attrs={"name": lambda v: v and v.startswith("lesson")}):
        row = anchor.find_next("tr")
        if not row:
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        lesson = {
            "date":       cells[0].get_text(strip=True),
            "hours":      cells[1].get_text(strip=True),
            "activity":   cells[2].get_text(strip=True),
            "assignment": cells[3].get_text(strip=True),
            "client":     cells[4].get_text(strip=True),
        }
        priv = anchor.find_next("div", id=lambda i: i and i.startswith("privateDetails"))
        if priv:
            for td in priv.find_all("td"):
                t = td.get_text(strip=True)
                if t and "/" in t and ":" in t:
                    lesson["start_datetime"] = t
                    break
            else:
                lesson["start_datetime"] = ""
        else:
            lesson["start_datetime"] = ""
        lessons.append(lesson)
    return lessons


def scrape_season(pass_number, password):
    session = requests.Session()
    session.headers.update(HEADERS)
    login(session, pass_number, password)

    all_lessons = {}
    current = SEASON_START
    while current <= SEASON_END:
        lessons = fetch_window(session, current)
        for l in lessons:
            key = (l["date"], l["activity"])
            if key not in all_lessons:
                all_lessons[key] = l
        current += timedelta(days=WINDOW_DAYS)

    return sorted(all_lessons.values(), key=lambda l: l["date"])


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        pass_number = body.get("passNumber", "").strip()
        password    = body.get("password", "").strip()

        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")

        if not pass_number or not password:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"error": "passNumber and password are required"}).encode())
            return

        try:
            lessons = scrape_season(pass_number, password)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"lessons": lessons}).encode())
        except ValueError as e:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Scrape failed: {str(e)}"}).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
