#!/usr/bin/env python3
"""Daily Excel report of all companies notified about, emailed as an attachment.

Builds a bifurcated workbook (BSE 500 vs non-BSE 500) from seen.json — today's
companies plus the all-time company lists, sorted earliest-first on first
notification — and emails it via Gmail SMTP.

Required env vars:
  GMAIL_ADDRESS      sender Gmail account
  GMAIL_APP_PASSWORD Gmail app password (not the account password)
  REPORT_EMAIL_TO    comma-separated recipients (defaults to GMAIL_ADDRESS)
"""
import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).parent
IST = timezone(timedelta(hours=5, minutes=30))

DETAIL_API = "https://api.bseindia.com/BseIndiaAPI/api/CorpAnnouncementDTNewDataBeta/w"
PDF_BASE = "https://www.bseindia.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}
SLUG_RE = re.compile(r"/stock-share-price/([^/]+)/")

HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(bold=True, color="FFFFFF")
NON_FILL = PatternFill("solid", fgColor="843C0C")


def slug_name(nsurl, fallback):
    if nsurl:
        m = SLUG_RE.search(nsurl)
        if m:
            return m.group(1).replace("-", " ").strip().title()
    return fallback or ""


def fetch_one(session, bse500, bse500_set, nid):
    for attempt in range(3):
        try:
            r = session.get(DETAIL_API, params={"newsid": nid}, timeout=20)
            r.raise_for_status()
            rows = (r.json() or {}).get("Table") or []
            if not rows:
                return {"NewsID": nid, "_status": "not_found"}
            row = rows[0]
            scrip = str(row.get("Scrip_cd") or "").strip()
            pdf = row.get("PDF_Link") or ""
            return {
                "NewsID": nid,
                "scrip": scrip,
                "bse500": scrip in bse500_set,
                "company": bse500.get(scrip)
                or slug_name(row.get("NSUrl") or "", row.get("CompanyName") or ""),
                "category": (row.get("CATEGORYNAME") or "").strip(),
                "subject": (row.get("NewsSub") or "").strip(),
                "date": (row.get("News_dt") or "").replace("T", " ")[:19],
                "pdf": (PDF_BASE + pdf) if pdf else "",
                "_status": "ok",
            }
        except Exception as e:
            if attempt == 2:
                return {"NewsID": nid, "_status": f"error: {e}"}
            time.sleep(1.5 * (attempt + 1))


def aggregate(records):
    """Group per company; sorted earliest-first on first-notified time."""
    by = {}
    for r in records:
        key = r["scrip"] or f"name:{r['company']}"
        g = by.setdefault(
            key,
            {"company": r["company"], "scrip": r["scrip"], "count": 0, "first": None},
        )
        g["count"] += 1
        d = r["date"]
        if d:
            g["first"] = d if g["first"] is None or d < g["first"] else g["first"]
    return sorted(by.values(), key=lambda x: x["first"] or "9999")


def company_sheet(wb, title, companies, fill):
    s = wb.create_sheet(title)
    s.append(["Company", "Scrip Code", "Notifications", "First notified"])
    for c in s[1]:
        c.fill = fill
        c.font = HEAD_FONT
    for g in companies:
        s.append([g["company"], g["scrip"], g["count"], None])
        cell = s.cell(row=s.max_row, column=4)
        if g["first"]:
            # Real Excel date (numeric serial under the hood), not text.
            cell.value = datetime.strptime(g["first"][:10], "%Y-%m-%d").date()
            cell.number_format = "dd-mmm-yyyy"
    for i, w in enumerate([46, 12, 14, 16], 1):
        s.column_dimensions[get_column_letter(i)].width = w
    s.freeze_panes = "A2"
    for row in s.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def detail_sheet(wb, title, records):
    s = wb.create_sheet(title)
    s.append(["Group", "Company", "Scrip", "Category", "Date (IST)", "Subject", "PDF", "NewsID"])
    for c in s[1]:
        c.fill = HEAD_FILL
        c.font = HEAD_FONT
    for r in sorted(records, key=lambda x: x["date"], reverse=True):
        s.append([
            "BSE 500" if r["bse500"] else "Non-BSE 500",
            r["company"], r["scrip"], r["category"], r["date"], r["subject"], r["pdf"], r["NewsID"],
        ])
    for i, w in enumerate([13, 42, 10, 24, 20, 60, 58, 38], 1):
        s.column_dimensions[get_column_letter(i)].width = w
    s.freeze_panes = "A2"
    for row in s.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def build_workbook(out_path):
    newsids = json.loads((ROOT / "seen.json").read_text())
    bse500 = json.loads((ROOT / "bse500.json").read_text())["constituents"]
    bse500_set = set(bse500)
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    print(f"Resolving {len(newsids)} NEWSIDs...", file=sys.stderr)

    session = requests.Session()
    session.headers.update(HEADERS)
    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_one, session, bse500, bse500_set, n): n for n in newsids}
        for f in as_completed(futs):
            rows.append(f.result())

    ok = [r for r in rows if r.get("_status") == "ok"]
    today_rows = [r for r in ok if r["date"][:10] == today_str]
    bse_rows = [r for r in ok if r["bse500"]]
    non_rows = [r for r in ok if not r["bse500"]]
    bse_c, non_c = aggregate(bse_rows), aggregate(non_rows)
    today_bse_c = aggregate([r for r in today_rows if r["bse500"]])
    today_non_c = aggregate([r for r in today_rows if not r["bse500"]])

    wb = Workbook()
    company_sheet(wb, f"Today BSE 500 ({len(today_bse_c)})", today_bse_c, HEAD_FILL)
    company_sheet(wb, f"Today non-BSE 500 ({len(today_non_c)})", today_non_c, NON_FILL)
    company_sheet(wb, f"BSE 500 companies ({len(bse_c)})", bse_c, HEAD_FILL)
    company_sheet(wb, f"Non-BSE 500 companies ({len(non_c)})", non_c, NON_FILL)
    detail_sheet(wb, "All announcements", ok)
    wb.remove(wb["Sheet"])  # drop the default empty sheet

    wb.save(out_path)
    return {
        "today": len(today_rows),
        "total": len(ok),
        "companies": len(bse_c) + len(non_c),
        "bse500_companies": len(bse_c),
        "non_companies": len(non_c),
        "date": today_str,
    }


def send_email(xlsx_path, stats):
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("REPORT_EMAIL_TO") or sender
    if not sender or not password:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set", file=sys.stderr)
        sys.exit(1)

    msg = EmailMessage()
    msg["Subject"] = f"BSE Annual Report Watcher — daily report {stats['date']}"
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(
        f"Daily report for {stats['date']} (IST).\n\n"
        f"Announcements today: {stats['today']}\n"
        f"Total to date: {stats['total']} announcements across "
        f"{stats['companies']} companies "
        f"({stats['bse500_companies']} BSE 500, {stats['non_companies']} non-BSE 500).\n\n"
        f"Full details in the attached Excel.\n\n"
        f"— Annual Report Watcher"
    )
    msg.add_attachment(
        Path(xlsx_path).read_bytes(),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"bse-report-{stats['date']}.xlsx",
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    print(f"Emailed {to}: today={stats['today']}, total={stats['total']}")


def main():
    out = ROOT / "daily_report.xlsx"
    stats = build_workbook(out)
    send_email(out, stats)


if __name__ == "__main__":
    main()
