import html
import json
import os
import sys
import time
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
BSE_CATEGORIES = [
    "AGM/EGM",
    "Board Meeting",
    "Company Update",
    "Corp. Action",
    "Insider Trading / SAST",
    "New Listing",
    "Result",
    "Integrated Filing",
    "Others",
]
BSE_PAGE_SIZE = 50
BSE_MAX_PAGES_PER_CATEGORY = 50
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


def _get_with_retry(session, url, params, attempts=4, base_delay=3):
    last_exc = None
    for i in range(attempts):
        try:
            r = session.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, str) and "no record" in data.lower():
                return {"Table": []}
            if not isinstance(data, dict):
                raise ValueError(
                    f"BSE returned non-dict JSON: {type(data).__name__}: {str(data)[:200]}"
                )
            return data
        except (requests.exceptions.RequestException, ValueError) as e:
            last_exc = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last_exc


def fetch_bse_announcements():
    now_ist = datetime.now(IST)
    prev = (now_ist - timedelta(days=3)).strftime("%Y%m%d")
    today = now_ist.strftime("%Y%m%d")
    session = requests.Session()
    session.headers.update(BSE_HEADERS)

    items = []
    seen_ids = set()
    for category in BSE_CATEGORIES:
        pageno = 1
        while True:
            params = {
                "pageno": pageno,
                "strCat": category,
                "strPrevDate": prev,
                "strScrip": "",
                "strSearch": "P",
                "strToDate": today,
                "strType": "C",
                "subcategory": "-1",
            }
            data = _get_with_retry(session, BSE_API, params)
            page = (data or {}).get("Table") or []
            if not page:
                break
            for it in page:
                nid = str(it.get("NEWSID") or "")
                if nid and nid in seen_ids:
                    continue
                if nid:
                    seen_ids.add(nid)
                items.append(it)
            table1 = (data or {}).get("Table1") or []
            row_count = (table1[0].get("ROWCNT") if table1 else 0) or 0
            if pageno * BSE_PAGE_SIZE >= row_count:
                break
            if pageno >= BSE_MAX_PAGES_PER_CATEGORY:
                break
            pageno += 1
    return items


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
    if not r.ok:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:500]}")


def _format_ist(dt_str: str) -> str:
    """BSE returns IST timestamps like '2026-05-22T11:06:06.753'. Render as HH:MM IST, DD Mon."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%H:%M IST, %d %b %Y")
    except ValueError:
        return dt_str


def format_message(item):
    company = item.get("SLONGNAME") or item.get("COMPANYNAME") or ""
    scrip = item.get("SCRIP_CD") or ""
    headline = item.get("HEADLINE") or item.get("NEWSSUB") or item.get("NEWS_SUBJECT") or ""
    category = item.get("CATEGORYNAME") or ""
    news_dt = _format_ist(item.get("NEWS_DT") or "")
    attachment = item.get("ATTACHMENTNAME") or ""
    pdf = f"{BSE_PDF_BASE}{attachment}" if attachment else BSE_ANN_PAGE
    return (
        f"<b>{html.escape(str(company))}</b> ({html.escape(str(scrip))})\n"
        f"{html.escape(str(category))} • {html.escape(news_dt)}\n\n"
        f"{html.escape(str(headline))[:1500]}\n\n"
        f"<a href=\"{html.escape(pdf, quote=True)}\">PDF</a>"
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        sys.exit(1)

    try:
        seen = load_seen()
        seen_set = set(map(str, seen))

        try:
            items = fetch_bse_announcements()
            print(f"Fetched {len(items)} BSE announcements")
        except (requests.exceptions.RequestException, ValueError) as e:
            # Transient BSE issue after retries (network timeout, bad JSON,
            # unexpected response shape). Don't alarm with a traceback — the
            # next run self-heals and seen.json means no report is ever missed.
            print(f"BSE fetch failed after retries: {e}", file=sys.stderr)
            ts_ist = datetime.now(IST).strftime("%H:%M IST, %d %b %Y")
            try:
                send_telegram(
                    token,
                    chat_id,
                    f"<b>Watcher run</b> • {ts_ist}\n"
                    f"⚠️ BSE temporarily unreachable; will retry next run.",
                )
            except Exception as send_err:
                print(f"Heartbeat failed: {send_err}", file=sys.stderr)
            return

        new_alerts = 0
        send_errors = 0
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
                send_errors += 1
                continue
            seen.append(news_id)
            seen_set.add(news_id)
            new_alerts += 1

        save_seen(seen)
        print(f"Sent {new_alerts} new alerts ({send_errors} send errors)")

        ts_ist = datetime.now(IST).strftime("%H:%M IST, %d %b %Y")
        summary = (
            f"<b>Watcher run</b> • {ts_ist}\n"
            f"Fetched: {len(items)}\n"
            f"New alerts: {new_alerts}\n"
            f"Send errors: {send_errors}"
        )
        try:
            send_telegram(token, chat_id, summary)
        except Exception as e:
            print(f"Heartbeat failed: {e}", file=sys.stderr)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            send_telegram(
                token,
                chat_id,
                f"<b>Watcher ERROR</b>\n<pre>{html.escape(tb)[:3500]}</pre>",
            )
        except Exception as send_err:
            print(f"Error heartbeat failed: {send_err}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
