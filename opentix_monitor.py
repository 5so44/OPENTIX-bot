#!/usr/bin/env python3
"""OPENTIX 餘票監控 → Discord（或 LINE）推播

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

LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")  # 選填：也想收 LINE 才需要
LINE_USER_ID = os.getenv("LINE_USER_ID", "")

INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))  # 檢查間隔（秒）
RENOTIFY_MIN = int(os.getenv("RENOTIFY_MINUTES", "15"))  # 持續有票時的重複提醒間隔
UNKNOWN_ALERT_AFTER = int(os.getenv("UNKNOWN_ALERT_AFTER", "10"))  # 連續讀不到幾次就通知
ALERT_COOLDOWN_HR = int(os.getenv("ALERT_COOLDOWN_HOURS", "6"))  # 異常通知冷卻時間


def _split(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


# 「有票」：頁面主體中，文字「完全等於」下列其中之一、且未被停用的按鈕／連結
BUY_TEXTS = _split("BUY_TEXTS", "立即購票,購票,訂購,選位購票,線上訂購,前往購票,選擇座位")
# 「售完」：文字完全等於下列其中之一的按鈕／標籤
SOLD_TEXTS = _split("SOLD_TEXTS", "售完,已售完,完售,已完售,暫無票券,暫無餘票,SoldOut,Soldout")

STATE_FILE = pathlib.Path("state.json")
PROBE_DIR = pathlib.Path("probe_output")


# ---------- 通知（Discord / LINE，有設定哪個就用哪個） ----------
def _send_discord(text: str) -> bool:
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


def notify(text: str) -> bool:
    results = []
    if DISCORD_WEBHOOK:
        results.append(_send_discord(text))
    if LINE_TOKEN and LINE_USER_ID:
        results.append(_send_line(text))
    if not results:
        print("[通知] 尚未設定 DISCORD_WEBHOOK_URL（或 LINE 金鑰），略過推播")
        return False
    return any(results)


def _send_line(text: str) -> bool:
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[LINE] 推播失敗 {r.status_code}: {r.text}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[LINE] 推播例外：{e}")
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
def norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def classify(items: list[dict]) -> tuple[str, list[str]]:
    """items: [{text, disabled, in_chrome}]；回傳 (狀態, 命中的文字)
    狀態：available 有票 / soldout 售完 / unknown 讀不到場次資訊"""
    buy_set = {norm(t) for t in BUY_TEXTS}
    sold_set = {norm(t) for t in SOLD_TEXTS}
    body = [i for i in items if not i.get("in_chrome") and norm(i.get("text", ""))]

    buy_hits = [norm(i["text"]) for i in body if norm(i["text"]) in buy_set and not i.get("disabled")]
    if buy_hits:
        return "available", buy_hits

    sold_hits = [norm(i["text"]) for i in body if norm(i["text"]) in sold_set]
    if sold_hits:
        return "soldout", sold_hits

    return "unknown", []


JS_COLLECT = """
els => els.map(e => ({
  text: (e.innerText || '').trim(),
  disabled: e.disabled === true
            || e.getAttribute('aria-disabled') === 'true'
            || /disabled|sold/i.test(e.className || ''),
  in_chrome: !!e.closest('header, nav, footer'),
}))
"""


def load_page(page) -> None:
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2500)


def check_once(browser) -> tuple[str, list[str]]:
    ctx = browser.new_context(locale="zh-TW", viewport={"width": 1280, "height": 1800})
    try:
        page = ctx.new_page()
        load_page(page)
        items = page.eval_on_selector_all("button, a, [role=button]", JS_COLLECT)
        return classify(items)
    finally:
        ctx.close()


# ---------- 探測模式 ----------
def probe() -> None:
    PROBE_DIR.mkdir(exist_ok=True)
    responses: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="zh-TW", viewport={"width": 1280, "height": 1800})
        page = ctx.new_page()

        def on_response(r):
            try:
                if "json" in r.headers.get("content-type", ""):
                    responses.append({"url": r.url, "status": r.status, "body": r.text()[:6000]})
            except Exception:
                pass

        page.on("response", on_response)
        load_page(page)
        page.screenshot(path=str(PROBE_DIR / "page.png"), full_page=True)
        (PROBE_DIR / "page_text.txt").write_text(page.inner_text("body"), encoding="utf-8")
        items = page.eval_on_selector_all("button, a, [role=button]", JS_COLLECT)
        (PROBE_DIR / "clickables.json").write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (PROBE_DIR / "json_responses.json").write_text(
            json.dumps(responses, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        browser.close()

    status, hits = classify(items)
    print(f"目前判斷：{status}  命中：{hits}")
    print(f"已將探測結果存到資料夾：{PROBE_DIR.resolve()}")
    print("請把 page.png、clickables.json、json_responses.json 傳給 Claude 校正。")


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
                f"🎫 有票釋出！\n{SHOW_NAME}\n{SHOW_INFO}\n\n馬上購票：\n{URL}\n（偵測時間 {now_str()}）"
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
    ap.add_argument("--test-notify", "--test-line", dest="test_notify", action="store_true",
                    help="送出一則測試通知")
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
