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
# Rolling dedup window. Kept modest because daily_report.py re-fetches every ID
# here from BSE on each run (~1200/min); a much larger cap would push that job
# past its time budget. For a truly unbounded archive, see the persistent
# announcements-database approach rather than raising this further.
SEEN_LIMIT = 10000
SKIPPED_PATH = Path(__file__).parent / ".heartbeat" / "skipped_runs.json"
SKIPPED_LIMIT = 200
NON_BSE500_PATH = Path(__file__).parent / ".heartbeat" / "non_bse500.json"
NON_BSE500_LIMIT = 1000

# BSE 500 constituents — index code 17 on bseindices.com. Fetched live each run
# (so it tracks the ~biannual reconstitution) with the committed bse500.json as
# a fallback when the live list is unreachable.
BSE500_PATH = Path(__file__).parent / "bse500.json"
BSE500_URL = "https://www.bseindices.com/AsiaIndexAPI/api/Codewise_Indices/w"
BSE500_CODE = "17"
BSE500_MIN_EXPECTED = 400  # sanity floor; a live list smaller than this is rejected
BSE500_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindices.com/",
    "Origin": "https://www.bseindices.com",
}

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


def load_skipped():
    """Timestamps of quiet runs (no new alerts) since the last alert was sent."""
    if SKIPPED_PATH.exists():
        try:
            return list(json.loads(SKIPPED_PATH.read_text()))
        except Exception:
            return []
    return []


def save_skipped(skipped):
    skipped = skipped[-SKIPPED_LIMIT:]
    SKIPPED_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKIPPED_PATH.write_text(json.dumps(skipped, indent=2) + "\n")


def load_non_bse500():
    """Return (items, last_flush_iso). Items are non-BSE500 matches banked since
    the digest was last flushed (via a BSE500 alert or the 3-day periodic flush)."""
    if NON_BSE500_PATH.exists():
        try:
            data = json.loads(NON_BSE500_PATH.read_text())
            if isinstance(data, dict):
                return list(data.get("items") or []), str(data.get("last_flush") or "")
            if isinstance(data, list):  # legacy format
                return list(data), ""
        except Exception:
            pass
    return [], ""


def save_non_bse500(items, last_flush):
    payload = {"last_flush": last_flush, "items": items[-NON_BSE500_LIMIT:]}
    NON_BSE500_PATH.parent.mkdir(parents=True, exist_ok=True)
    NON_BSE500_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _group_non_bse500_by_company(rows):
    """Preserve insertion order; group successive (and non-successive) rows
    sharing scrip+company so a company with N filings becomes one bullet."""
    groups = {}  # key -> {"company": str, "scrip": str, "filings": [{"title","pdf"}]}
    order = []
    for r in rows:
        scrip = (r.get("scrip") or "").strip()
        company = r.get("company") or "(unknown)"
        key = scrip or f"name:{company}"
        if key not in groups:
            groups[key] = {"company": company, "scrip": scrip, "filings": []}
            order.append(key)
        groups[key]["filings"].append({
            "title": (r.get("title") or "").strip(),
            "pdf": r.get("pdf") or BSE_ANN_PAGE,
        })
    return [groups[k] for k in order]


def format_non_bse500_bullets(rows):
    """Return one bullet block per company. A company with multiple filings gets
    sub-bullets indented under its name."""
    out = []
    for g in _group_non_bse500_by_company(rows):
        head = (
            f"• <b>{html.escape(str(g['company']))}</b> "
            f"({html.escape(str(g['scrip']))})"
        )
        if len(g["filings"]) == 1:
            f0 = g["filings"][0]
            title_disp = html.escape(f0["title"][:200]) if f0["title"] else "PDF"
            out.append(
                f"{head} — <a href=\"{html.escape(f0['pdf'], quote=True)}\">{title_disp}</a>"
            )
        else:
            out.append(head)
            for f in g["filings"]:
                title_disp = html.escape(f["title"][:200]) if f["title"] else "PDF"
                out.append(
                    f"    – <a href=\"{html.escape(f['pdf'], quote=True)}\">{title_disp}</a>"
                )
    return out


def _load_bse500_file():
    if BSE500_PATH.exists():
        try:
            return dict(json.loads(BSE500_PATH.read_text()).get("constituents") or {})
        except Exception:
            return {}
    return {}


def _save_bse500_file(constituents, rebalance_date=""):
    payload = {
        "index": "BSE 500",
        "source": f"{BSE500_URL}?code={BSE500_CODE}",
        "rebalance_date": rebalance_date,
        "count": len(constituents),
        "constituents": dict(sorted(constituents.items())),
    }
    BSE500_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def load_bse500():
    """Return (set_of_scrip_codes, name_map). Tries the live index, refreshing the
    committed file when the membership changes; falls back to the committed file;
    returns (None, {}) only if both are unavailable (caller then notifies for all)."""
    session = requests.Session()
    session.headers.update(BSE500_HEADERS)
    live = {}
    rebalance_date = ""
    try:
        data = _get_with_retry(session, BSE500_URL, {"code": BSE500_CODE})
        for r in (data or {}).get("Table") or []:
            code = str(r.get("SCRIP_CODE") or "").strip()
            if code:
                live[code] = (r.get("SCRIPNAME") or "").strip()
            if not rebalance_date:
                rebalance_date = str(r.get("TransDate") or "")[:10]
    except Exception as e:
        print(f"BSE500 live fetch failed: {e}", file=sys.stderr)

    if len(live) >= BSE500_MIN_EXPECTED:
        if set(live) != set(_load_bse500_file()):
            _save_bse500_file(live, rebalance_date)
            print(f"BSE500 list refreshed: {len(live)} constituents", file=sys.stderr)
        return set(live), live

    committed = _load_bse500_file()
    if committed:
        print(f"Using committed BSE500 fallback ({len(committed)})", file=sys.stderr)
        return set(committed), committed

    print("BSE500 list unavailable; notifying for ALL companies this run", file=sys.stderr)
    return None, {}


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


def send_telegram_chunked(token, chat_id, text, limit=3900):
    """Send text as one message, or split on line boundaries if it exceeds
    Telegram's ~4096-char cap (e.g. a very long non-BSE500 digest)."""
    if len(text) <= limit:
        send_telegram(token, chat_id, text)
        return
    chunk, length = [], 0
    for line in text.split("\n"):
        if length + len(line) + 1 > limit and chunk:
            send_telegram(token, chat_id, "\n".join(chunk))
            chunk, length = [], 0
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        send_telegram(token, chat_id, "\n".join(chunk))


def _format_ist(dt_str: str) -> str:
    """BSE returns IST timestamps like '2026-05-22T11:06:06.753'. Render as HH:MM IST, DD Mon."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%H:%M IST, %d %b %Y")
    except ValueError:
        return dt_str


def format_company_alert(company, scrip, items):
    """One message covering all of a company's matching filings in this run."""
    lines = [f"<b>{html.escape(str(company))}</b> ({html.escape(str(scrip))})"]
    for it in items:
        category = it.get("CATEGORYNAME") or ""
        news_dt = _format_ist(it.get("NEWS_DT") or "")
        headline = it.get("HEADLINE") or it.get("NEWSSUB") or it.get("NEWS_SUBJECT") or ""
        attachment = it.get("ATTACHMENTNAME") or ""
        pdf = f"{BSE_PDF_BASE}{attachment}" if attachment else BSE_ANN_PAGE
        meta = " • ".join(p for p in (html.escape(str(category)), html.escape(news_dt)) if p)
        lines.append("")
        if meta:
            lines.append(meta)
        lines.append(html.escape(str(headline))[:1500])
        lines.append(f"<a href=\"{html.escape(pdf, quote=True)}\">PDF</a>")
    return "\n".join(lines)


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

        bse500_set, _bse500_names = load_bse500()
        non_bse500, last_flush = load_non_bse500()

        # Group BSE500 matches by SCRIP_CD so each company gets one Telegram message.
        # Insertion order is preserved (newest-first as returned by BSE).
        bse500_groups = {}  # scrip -> {"company": str, "items": [item, ...], "news_ids": [...]}
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

            scrip = str(item.get("SCRIP_CD") or "").strip()
            company = str(item.get("SLONGNAME") or item.get("COMPANYNAME") or "(unknown)")

            # Filter only when we actually have the BSE500 list AND a scrip code.
            # Unknown scrip or unavailable list => notify (never silently drop).
            if bse500_set is not None and scrip and scrip not in bse500_set:
                attachment = item.get("ATTACHMENTNAME") or ""
                non_bse500.append({
                    "company": company,
                    "scrip": scrip,
                    "title": str(item.get("HEADLINE") or item.get("NEWSSUB") or item.get("NEWS_SUBJECT") or ""),
                    "pdf": f"{BSE_PDF_BASE}{attachment}" if attachment else BSE_ANN_PAGE,
                })
                seen.append(news_id)
                seen_set.add(news_id)
                continue

            # BSE500 (or notify-all fallback) — bank for grouped send.
            # Use scrip as the dedup key when present; fall back to company name so
            # blank-scrip items still get an alert (and grouped together if same name).
            key = scrip or f"name:{company}"
            grp = bse500_groups.get(key)
            if grp is None:
                grp = {"company": company, "scrip": scrip, "items": [], "news_ids": []}
                bse500_groups[key] = grp
            grp["items"].append(item)
            grp["news_ids"].append(news_id)

        new_alerts = 0  # count of announcements actually delivered (not companies)
        send_errors = 0
        for grp in bse500_groups.values():
            try:
                send_telegram(
                    token, chat_id,
                    format_company_alert(grp["company"], grp["scrip"], grp["items"]),
                )
            except Exception as e:
                print(f"Telegram send failed for {grp['scrip']}: {e}", file=sys.stderr)
                send_errors += 1
                continue
            for nid in grp["news_ids"]:
                seen.append(nid)
                seen_set.add(nid)
            new_alerts += len(grp["items"])

        save_seen(seen)
        print(
            f"Sent {len(bse500_groups) - send_errors} grouped messages "
            f"covering {new_alerts} announcements ({send_errors} send errors); "
            f"{len(non_bse500)} non-BSE500 banked"
        )

        now_dt = datetime.now(IST)
        ts_ist = now_dt.strftime("%H:%M IST, %d %b %Y")
        # Start the 3-day flush clock the first time anything is banked.
        if not last_flush:
            last_flush = now_dt.isoformat()
        try:
            flush_due = bool(non_bse500) and (
                now_dt - datetime.fromisoformat(last_flush) >= timedelta(days=3)
            )
        except (ValueError, TypeError):
            flush_due = bool(non_bse500)

        skipped = load_skipped()
        alert = new_alerts > 0 or send_errors > 0

        # Nothing to report and the non-BSE500 digest isn't due yet: stay silent,
        # but bank this quiet run so the next alert can show the gap.
        if not alert and not flush_due:
            skipped.append(ts_ist)
            save_skipped(skipped)
            save_non_bse500(non_bse500, last_flush)
            print(f"No BSE500 alerts; Telegram suppressed. {len(skipped)} quiet run(s) pending.")
            return

        if alert:
            lines = [
                f"<b>Watcher run</b> • {ts_ist}",
                f"Fetched: {len(items)}",
                f"New alerts: {new_alerts}",
                f"Send errors: {send_errors}",
            ]
            if skipped:
                lines.append("")
                lines.append(f"Earlier runs with no new alerts ({len(skipped)}):")
                lines.extend(f"• {t}" for t in skipped)
            if non_bse500:
                bullets = format_non_bse500_bullets(non_bse500)
                n_companies = len(_group_non_bse500_by_company(non_bse500))
                lines.append("")
                lines.append(
                    f"Non-BSE500 filings since last alert "
                    f"({n_companies} compan{'y' if n_companies == 1 else 'ies'}, "
                    f"{len(non_bse500)} filing{'' if len(non_bse500) == 1 else 's'}):"
                )
                lines.extend(bullets)
        else:
            # 3-day periodic flush: non-BSE500 digest only. The quiet-run gap list
            # keeps riding the next real BSE500 alert.
            bullets = format_non_bse500_bullets(non_bse500)
            n_companies = len(_group_non_bse500_by_company(non_bse500))
            lines = [
                f"<b>Non-BSE500 digest</b> • {ts_ist}",
                "(periodic 3-day flush — no BSE500 alerts in this window)",
                "",
                f"Non-BSE500 filings "
                f"({n_companies} compan{'y' if n_companies == 1 else 'ies'}, "
                f"{len(non_bse500)} filing{'' if len(non_bse500) == 1 else 's'}):",
            ]
            lines.extend(bullets)

        try:
            send_telegram_chunked(token, chat_id, "\n".join(lines))
            # Clear the non-BSE500 bank + reset the 3-day clock on any successful send.
            save_non_bse500([], now_dt.isoformat())
            # The quiet-run gap list is cleared only when an actual alert reported it.
            if alert:
                save_skipped([])
        except Exception as e:
            print(f"Heartbeat failed: {e}", file=sys.stderr)
            save_non_bse500(non_bse500, last_flush)  # retain bank for next attempt
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
