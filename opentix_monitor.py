#!/usr/bin/env python3
"""OPENTIX 餘票監控 → Discord 推播

做法：進入節目頁 → 點「電腦配位」→ 讀取各票區是否「完售」（忽略輪椅席）→ 有票就通知。

用法：
  python opentix_monitor.py --test-notify # 測試通知是否設定成功
  python opentix_monitor.py --probe       # 探測模式：存下頁面內容，供校正判斷規則
  python opentix_monitor.py               # 持續監控（Ctrl+C 結束）
  python opentix_monitor.py --once        # 只檢查一次（給排程工具使用）
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import random
import re
import sys
import time

import requests
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

load_dotenv()

URL = os.getenv("TARGET_URL", "https://www.opentix.life/event/2079132570898259969")
SHOW_NAME = os.getenv("SHOW_NAME", "阿格麗希音樂節在台灣-高雄場")
SHOW_INFO = os.getenv("SHOW_INFO", "2026/11/10(二) 19:30 衛武營音樂廳")

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_MENTION_ID = os.getenv("DISCORD_MENTION_USER_ID", "")  # 選填：通知時 @ 你，手機才會跳通知

INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))  # 檢查間隔（秒）
RENOTIFY_MIN = int(os.getenv("RENOTIFY_MINUTES", "15"))  # 持續有票時的重複提醒間隔
UNKNOWN_ALERT_AFTER = int(os.getenv("UNKNOWN_ALERT_AFTER", "10"))  # 連續讀不到幾次就通知
ALERT_COOLDOWN_HR = int(os.getenv("ALERT_COOLDOWN_HOURS", "6"))  # 異常通知冷卻時間


def _split(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


# 票區名稱含有這些字就忽略（你沒資格買的票區）
IGNORE_ZONE_WORDS = _split("IGNORE_ZONE_WORDS", "輪椅")
# 票區文字含有這些字，代表該票區已售完
ZONE_SOLD_WORDS = _split("ZONE_SOLD_WORDS", "完售,售完,已售完,已完售,暫無")
# 節目頁上「完全等於」這些字的標籤，代表整場售完（備用判斷）
SOLD_TEXTS = _split("SOLD_TEXTS", "售完,已售完,完售,已完售,暫無票券,暫無餘票,SoldOut,Soldout")

SEAT_BUTTON_TEXT = os.getenv("SEAT_BUTTON_TEXT", "電腦配位")

STATE_FILE = pathlib.Path("state.json")
PROBE_DIR = pathlib.Path("probe_output")


# ---------- 通知（Discord） ----------
def notify(text: str) -> bool:
    if not DISCORD_WEBHOOK:
        print("[通知] 尚未設定 DISCORD_WEBHOOK_URL，略過推播")
        return False
    if DISCORD_MENTION_ID:
        text = f"<@{DISCORD_MENTION_ID}> {text}"
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": text[:1900]}, timeout=15)
        if r.status_code not in (200, 204):
            print(f"[Discord] 推播失敗 {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[Discord] 推播例外：{e}")
        return False


# ---------- 狀態 ----------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 判斷邏輯 ----------
PRICE_RE = re.compile(r"\$\s?\d[\d,]{2,}")


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def parse_zones(text: str):
    """從票區頁文字解析出票區清單；找不到「選擇票區」回傳 None。
    每個票區：{name, text}，text 為該票區卡片內的所有文字。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    start = next((i for i, l in enumerate(lines) if "選擇票區" in l), None)
    if start is None:
        return None
    zones, cur, prev = [], None, ""
    for l in lines[start + 1:]:
        if PRICE_RE.search(l):
            name = PRICE_RE.sub("", l).strip() or prev  # 名稱可能和價格同一行，也可能在上一行
            cur = {"name": name, "text": l}
            zones.append(cur)
        elif cur is not None:
            cur["text"] += "\n" + l
        prev = l
    return [z for z in zones if "區" in z["name"] or "席" in z["name"]]


def classify_zones(zones: list[dict]) -> tuple[str, list[str]]:
    """回傳 (狀態, 票區名稱)。available 有票 / soldout 全部完售 / unknown 讀不到"""
    mine = [z for z in zones if not any(w in z["name"] for w in IGNORE_ZONE_WORDS)]
    if not mine:
        return "unknown", []
    open_zones = [z["name"] for z in mine if not any(w in z["text"] for w in ZONE_SOLD_WORDS)]
    if open_zones:
        return "available", open_zones
    return "soldout", [z["name"] for z in mine]


def classify_labels(items: list[dict], page_text: str = "") -> tuple[str, list[str]]:
    """備用：節目頁上若出現「售完」標籤（按鈕或單獨一行文字）才判定為售完，
    其餘一律視為 unknown（避免誤報有票）"""
    sold_set = {norm(t) for t in SOLD_TEXTS}
    hits = [norm(i["text"]) for i in items if not i.get("in_chrome") and norm(i.get("text", "")) in sold_set]
    hits += [norm(l) for l in page_text.splitlines() if norm(l) in sold_set]
    return ("soldout", hits) if hits else ("unknown", [])


JS_COLLECT = """
els => els.map(e => ({
  text: (e.innerText || '').trim(),
  disabled: e.disabled === true
            || e.getAttribute('aria-disabled') === 'true'
            || /disabled|sold/i.test(e.className || ''),
  in_chrome: !!e.closest('header, nav, footer'),
}))
"""


# ---------- 頁面操作 ----------
def load_page(page) -> None:
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)


def dismiss_cookie(page) -> None:
    try:
        btn = page.get_by_text("同意", exact=True)
        if btn.count():
            btn.first.click(timeout=3000)
            page.wait_for_timeout(500)
    except Exception:
        pass


def open_seat_page(page):
    """點「電腦配位」進入票區頁；回傳票區頁（可能是同一個分頁或新分頁），找不到按鈕回傳 None。"""
    loc = page.get_by_text(SEAT_BUTTON_TEXT, exact=True)
    if loc.count() == 0:
        return None
    target = page
    try:
        with page.context.expect_page(timeout=4000) as info:
            loc.first.click(timeout=10000)
        target = info.value
        target.wait_for_load_state("domcontentloaded", timeout=30000)
    except PWTimeout:
        target = page  # 沒有開新分頁，就是在原分頁跳轉
    try:
        target.get_by_text("選擇票區").first.wait_for(timeout=20000)
    except PWTimeout:
        return target
    target.wait_for_timeout(1500)
    refresh = target.get_by_text("更新票況")
    if refresh.count():
        try:
            refresh.first.click(timeout=3000)
            target.wait_for_timeout(2000)
        except Exception:
            pass
    return target


def check_once(browser) -> tuple[str, list[str]]:
    ctx = browser.new_context(locale="zh-TW", viewport={"width": 1280, "height": 1800})
    try:
        page = ctx.new_page()
        load_page(page)
        dismiss_cookie(page)
        seat = open_seat_page(page)
        if seat is not None:
            zones = parse_zones(seat.inner_text("body"))
            if zones:
                return classify_zones(zones)
        items = page.eval_on_selector_all("button, a, [role=button]", JS_COLLECT)
        return classify_labels(items, page.inner_text("body"))
    finally:
        ctx.close()


# ---------- 探測模式 ----------
def probe() -> None:
    PROBE_DIR.mkdir(exist_ok=True)
    responses: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="zh-TW", viewport={"width": 1280, "height": 1800})

        def on_response(r):
            try:
                if "json" in r.headers.get("content-type", ""):
                    responses.append({"url": r.url, "status": r.status, "body": r.text()[:6000]})
            except Exception:
                pass

        ctx.on("response", on_response)
        page = ctx.new_page()
        load_page(page)
        dismiss_cookie(page)
        page.screenshot(path=str(PROBE_DIR / "page.png"), full_page=True)
        (PROBE_DIR / "page_text.txt").write_text(page.inner_text("body"), encoding="utf-8")

        seat = open_seat_page(page)
        if seat is None:
            print(f"找不到「{SEAT_BUTTON_TEXT}」按鈕")
        else:
            print(f"票區頁網址：{seat.url}")
            seat.screenshot(path=str(PROBE_DIR / "seatpage.png"), full_page=True)
            text = seat.inner_text("body")
            (PROBE_DIR / "seatpage_text.txt").write_text(text, encoding="utf-8")
            (PROBE_DIR / "seatpage.html").write_text(seat.content(), encoding="utf-8")
            zones = parse_zones(text)
            if zones is None:
                print("票區頁：找不到「選擇票區」")
            else:
                print("解析到的票區：")
                for z in zones:
                    sold = any(w in z["text"] for w in ZONE_SOLD_WORDS)
                    ignored = any(w in z["name"] for w in IGNORE_ZONE_WORDS)
                    print(f"  - {z['name']} | {'完售' if sold else '有票?'}{' | 忽略' if ignored else ''}")
                status, hits = classify_zones(zones)
                print(f"目前判斷：{status}  票區：{hits}")

        (PROBE_DIR / "json_responses.json").write_text(
            json.dumps(responses, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        browser.close()
    print(f"已將探測結果存到資料夾：{PROBE_DIR.resolve()}")


# ---------- 主流程 ----------
def now_str() -> str:
    return dt.datetime.now().strftime("%m/%d %H:%M")


def run_check(browser, state: dict) -> None:
    now = time.time()
    try:
        status, hits = check_once(browser)
    except Exception as e:  # 網路或頁面錯誤
        status, hits = "error", [str(e)[:120]]

    print(f"[{now_str()}] 狀態：{status} {hits if hits else ''}")

    if status == "available":
        state["unknown_streak"] = 0
        due = now - state.get("last_notified", 0) >= RENOTIFY_MIN * 60
        if state.get("last_status") != "available" or due:
            ok = notify(
                f"🎫 有票釋出！\n{SHOW_NAME}\n{SHOW_INFO}\n"
                f"有票的票區：{'、'.join(hits)}\n\n馬上購票：\n{URL}\n（偵測時間 {now_str()}）"
            )
            if ok:
                state["last_notified"] = now
        state["last_status"] = "available"
    elif status == "soldout":
        state["unknown_streak"] = 0
        state["last_status"] = "soldout"
    else:  # unknown / error：不改變 last_status，避免誤判
        state["unknown_streak"] = state.get("unknown_streak", 0) + 1
        cooled = now - state.get("last_alert", 0) >= ALERT_COOLDOWN_HR * 3600
        if state["unknown_streak"] >= UNKNOWN_ALERT_AFTER and cooled:
            if notify(
                f"⚠️ 監控異常：已連續 {state['unknown_streak']} 次讀不到售票狀態。\n"
                "可能是網站改版或被擋，請檢查程式。"
            ):
                state["last_alert"] = now
    save_state(state)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-notify", dest="test_notify", action="store_true", help="送出一則測試通知")
    ap.add_argument("--probe", action="store_true", help="探測模式")
    ap.add_argument("--once", action="store_true", help="只檢查一次")
    args = ap.parse_args()

    if args.test_notify:
        ok = notify("✅ 測試成功：通知已設定完成。")
        print("已送出" if ok else "送出失敗，請檢查設定")
        return
    if args.probe:
        probe()
        return

    state = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            if args.once:
                run_check(browser, state)
                return
            print(f"開始監控：{SHOW_NAME}，每 {INTERVAL} 秒檢查一次（Ctrl+C 結束）")
            while True:
                run_check(browser, state)
                time.sleep(INTERVAL * random.uniform(0.8, 1.2))
        except KeyboardInterrupt:
            print("\n已停止")
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
