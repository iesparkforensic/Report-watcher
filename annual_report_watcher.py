import html
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


KEYWORDS = [
    "annual report",
    "integrated annual report",
    "integrated report",
    "annual report and notice of agm",
    "annual report alongwith notice of agm",
    "annual report along with notice of agm",
    "annual report along with the notice of agm",
    "notice of annual general meeting",
    "business responsibility and sustainability report",
    "brsr",
    "sustainability report",
    "esg report",
]

EXCLUDE_KEYWORDS = [
    "annual secretarial compliance report",
    "secretarial audit report",
    "annual performance review",
    "annual information memorandum",
    "proceedings of the annual general meeting",
    "proceedings of annual general meeting",
    "proceedings of agm",
    "voting results",
    "scrutinizer report",
    "scrutinizer's report",
    "outcome of agm",
    "outcome of the agm",
]


def matches_keyword(text: str) -> bool:
    t = (text or "").lower()
    if any(neg in t for neg in EXCLUDE_KEYWORDS):
        return False
    return any(k in t for k in KEYWORDS)


BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_PDF_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_ANN_PAGE = "https://www.bseindia.com/corporates/ann.html"
IST = timezone(timedelta(hours=5, minutes=30))
SEEN_PATH = Path(__file__).parent / "seen.json"
SEEN_LIMIT = 5000

BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}


def fetch_bse_announcements():
    now_ist = datetime.now(IST)
    # Look back two days to cover hourly runs that overlap midnight IST.
    prev = (now_ist - timedelta(days=1)).strftime("%Y%m%d")
    today = now_ist.strftime("%Y%m%d")
    params = {
        "pageno": 1,
        "strCat": "-1",
        "strPrevDate": prev,
        "strScrip": "",
        "strSearch": "P",
        "strToDate": today,
        "strType": "C",
        "subcategory": "-1",
    }
    r = requests.get(BSE_API, params=params, headers=BSE_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("Table") or []


def load_seen():
    if SEEN_PATH.exists():
        try:
            return list(json.loads(SEEN_PATH.read_text()))
        except Exception:
            return []
    return []


def save_seen(seen):
    seen = seen[-SEEN_LIMIT:]
    SEEN_PATH.write_text(json.dumps(seen, indent=2) + "\n")


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        },
        timeout=30,
    )
    r.raise_for_status()


def format_message(item):
    company = item.get("SLONGNAME") or item.get("COMPANYNAME") or ""
    scrip = item.get("SCRIP_CD") or ""
    headline = item.get("HEADLINE") or item.get("NEWSSUB") or item.get("NEWS_SUBJECT") or ""
    category = item.get("CATEGORYNAME") or ""
    news_dt = item.get("NEWS_DT") or ""
    attachment = item.get("ATTACHMENTNAME") or ""
    pdf = f"{BSE_PDF_BASE}{attachment}" if attachment else BSE_ANN_PAGE
    return (
        f"<b>{html.escape(str(company))}</b> ({html.escape(str(scrip))})\n"
        f"{html.escape(str(category))} • {html.escape(str(news_dt))}\n\n"
        f"{html.escape(str(headline))[:1500]}\n\n"
        f"<a href=\"{html.escape(pdf, quote=True)}\">PDF</a>"
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()
    seen_set = set(map(str, seen))

    items = fetch_bse_announcements()
    print(f"Fetched {len(items)} BSE announcements")

    new_alerts = 0
    for item in items:
        news_id = str(item.get("NEWSID") or "")
        if not news_id or news_id in seen_set:
            continue
        blob = " ".join(
            str(item.get(k) or "")
            for k in ("HEADLINE", "NEWSSUB", "NEWS_SUBJECT", "MORE", "CATEGORYNAME")
        )
        if not matches_keyword(blob):
            continue
        try:
            send_telegram(token, chat_id, format_message(item))
        except Exception as e:
            print(f"Telegram send failed for {news_id}: {e}", file=sys.stderr)
            continue
        seen.append(news_id)
        seen_set.add(news_id)
        new_alerts += 1

    save_seen(seen)
    print(f"Sent {new_alerts} new alerts")


if __name__ == "__main__":
    main()
