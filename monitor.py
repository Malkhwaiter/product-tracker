#!/usr/bin/env python3
"""
Generalized cloud monitor (uploaded to the user's GitHub repo by the app).

Self-contained: reads config.json (all trackers exported from the desktop app),
checks each product's availability on its page, and sends a Telegram alert when
a tracked selection flips sold-out -> available. With SEND_SUMMARY=1 it sends a
single summary of all trackers. State is kept in state/state.json and committed
back by the workflow so the next run knows what changed.

Runs on GitHub Actions on a schedule and via repository_dispatch (cron-job.org).
"""
from __future__ import annotations

import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CONFIG_FILE = Path("config.json")
STATE_FILE = Path("state/state.json")
SUMMARY_STATE_FILE = Path("state/summary.json")
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
KSA = timezone(timedelta(hours=3))

HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
           "Accept-Language": "ar,en-US;q=0.9,en;q=0.8"}
OPTIONS_RE = re.compile(r'<salla-product-options\s+options="(.*?)"', re.S)
LDJSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
SIZE_GROUP_NAMES = {"القياس", "المقاس", "مقاس", "size", "Size", "الحجم", "المقاسات"}


# ---------- detection (self-contained Salla reader) ----------

def fetch(url: str, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(2 ** i)
    raise RuntimeError(f"fetch failed {url}: {last}")


def parse_groups(doc: str):
    m = OPTIONS_RE.search(doc)
    if not m:
        return None
    try:
        raw = json.loads(html_mod.unescape(m.group(1)))
    except json.JSONDecodeError:
        return None
    groups = []
    for g in raw:
        vals = [{"name": (v.get("name") or "").strip(), "available": not bool(v.get("is_out", True))}
                for v in g.get("details", []) if (v.get("name") or "").strip()]
        if vals:
            groups.append({"label": (g.get("name") or "option").strip(), "values": vals})
    return groups or None


def product_available(doc: str):
    for m in LDJSON_RE.finditer(doc):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for n in (nodes if isinstance(nodes, list) else [nodes]):
            if isinstance(n, dict) and n.get("@type") == "Product":
                off = n.get("offers") or {}
                return "InStock" in str(off.get("availability", "")) if isinstance(off, dict) else None
    return None


def availability_for(tracker: dict) -> str:
    """Return 'available' | 'sold_out' | 'error' for a tracker right now."""
    try:
        doc = fetch(tracker["url"])
    except Exception:  # noqa: BLE001
        return "error"
    mode = tracker.get("watch_mode") or ("size" if tracker.get("selection") else "product")
    if mode == "product":
        pa = product_available(doc)
        if pa is True:
            return "available"
        if pa is False:
            return "sold_out"
        groups = parse_groups(doc)
        if groups:
            return "available" if any(v["available"] for g in groups for v in g["values"]) else "sold_out"
        return "error"
    groups = parse_groups(doc)
    if not groups:
        return "error"
    by = {g["label"]: {v["name"]: v["available"] for v in g["values"]} for g in groups}
    known = True
    all_av = True
    for s in tracker.get("selection", []):
        a = by.get(s.get("label", ""), {}).get(s.get("value", ""))
        if a is None:
            known = False
        else:
            all_av = all_av and a
    if not known:
        return "error"
    return "available" if all_av else "sold_out"


# ---------- telegram + state ----------

def send_telegram(token: str, chat: str, text: str) -> bool:
    try:
        r = requests.post(TELEGRAM_API.format(token=token),
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=30)
        return r.ok
    except Exception:  # noqa: BLE001
        return False


def load_json(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def save_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def selection_text(t: dict) -> str:
    return " · ".join(s.get("value", "") for s in t.get("selection", [])) or "المنتج كامل"


def run_once(token, chat, trackers, send_alerts=True):
    state = load_json(STATE_FILE, {})
    newly = []
    for t in trackers:
        tid = str(t.get("id"))
        status = availability_for(t)
        prev = state.get(tid, "unknown")
        if status == "available" and prev != "available" and not t.get("paused"):
            newly.append(t)
            if send_alerts:
                msg = ("🎉 <b>رجع متوفر!</b>\n\n"
                       f"<b>{t.get('title', t['url'])}</b>\n"
                       f"طلبك: <b>{selection_text(t)}</b> صار متوفر الحين.\n{t['url']}")
                send_telegram(token, chat, msg)
        if status != "error":
            state[tid] = status
        print(f"  [{tid}] {t.get('title','')[:30]} -> {status}")
        time.sleep(0.4)
    save_json(STATE_FILE, state)
    return state, newly


def build_summary(trackers, state):
    now = datetime.now(KSA).strftime("%Y-%m-%d %H:%M")
    lines = ["📋 <b>ملخص — متتبّع المنتجات</b>", f"🕒 وقت الفحص: {now} (توقيت السعودية)", ""]
    for t in trackers:
        st = state.get(str(t.get("id")), "unknown")
        mark = {"available": "✅ متوفّر", "sold_out": "❌ نافد",
                "error": "⚠️ تعذّر", "unknown": "… قيد الفحص"}.get(st, st)
        lines.append(f"• <b>{t.get('title', t['url'])}</b> — {selection_text(t)}: {mark}")
    lines.append("\nالبوت شغّال ✅")
    return "\n".join(lines)


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("ERROR: telegram secrets missing", file=sys.stderr)
        return 2
    cfg = load_json(CONFIG_FILE, {})
    trackers = [t for t in cfg.get("trackers", []) if not t.get("paused")]
    if not trackers:
        print("no active trackers")
        return 0

    want_summary = os.environ.get("SEND_SUMMARY", "").strip().lower() in {"1", "true", "yes"}
    checks = max(1, int(os.environ.get("CHECKS_PER_RUN", "4") or 4))
    interval = max(0, int(os.environ.get("CHECK_INTERVAL_SECONDS", "900") or 900))
    print(f"{len(trackers)} tracker(s), {checks} check(s) every {interval}s, summary={want_summary}")

    state = {}
    for i in range(checks):
        if i:
            print(f"-- sleep {interval}s --", flush=True)
            time.sleep(interval)
        print(f"== check {i+1}/{checks} ==", flush=True)
        state, _ = run_once(token, chat, trackers)

    if want_summary:
        send_telegram(token, chat, build_summary(trackers, state))
        print("summary sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
