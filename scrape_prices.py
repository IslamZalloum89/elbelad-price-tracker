"""
Darbiy / Elbelad Price Tracker
يفتح كل قسم من موقع lavender-herbs.com بمتصفح آلي حقيقي (Playwright)،
يقرأ المنتجات والأسعار المعروضة فعليًا، يقارنها بآخر نسخة محفوظة بجوجل شيت،
يحدّث الشيت، ويرسل إشعار واتساب عند أي تغيير.
"""

import os
import re
import json
import time
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright
import requests

# ============================================================
# الإعدادات - تُقرأ من GitHub Secrets (متغيرات بيئة)
# ============================================================

GOOGLE_CREDS_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]  # محتوى ملف JSON كامل كنص
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]                       # معرف الشيت من رابطه
CALLMEBOT_PHONE = os.environ["CALLMEBOT_PHONE"]                 # رقمك بصيغة دولية بدون +
CALLMEBOT_APIKEY = os.environ["CALLMEBOT_APIKEY"]               # المفتاح اللي ترجعه CallMeBot بعد التفعيل

BASE_URL = "https://lavender-herbs.com/collections/"

# قائمة الأقسام (الـ slugs) اللي جمعناها من الموقع - عدّل/أضف حسب الحاجة
CATEGORY_SLUGS = [
    "egnite", "egnitefood", "natural-herbs", "rich-care-products", "soap",
    "wax", "fragrace", "siliconmolds", "empty-containers", "natural-extracts",
    "seeds", "spices", "food-flavors", "grocery-and-legumes",
    "collection-146626", "collection-3825526", "collection-662258",
    "collection-2101334", "hair-care", "colors-and-dyes",
]

SHEET_TAB_NAME = "current_prices"
LOG_TAB_NAME = "change_log"

# ============================================================
# 1. الاتصال بجوجل شيت
# ============================================================

def connect_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID)


def get_or_create_tab(spreadsheet, title, headers):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=len(headers))
        ws.append_row(headers)
        return ws


# ============================================================
# 2. سحب منتجات قسم واحد بمتصفح حقيقي
# ============================================================

def scrape_category(page, slug):
    url = BASE_URL + slug
    products = []
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        # انتظار إضافي بسيط لضمان اكتمال تحميل مكونات Vue المخصصة
        page.wait_for_timeout(2000)

        # كل بطاقة منتج بحسب القالب اللي وجدناه: div.product-card
        cards = page.query_selector_all(".product-card")

        for card in cards:
            text = card.inner_text().strip()
            if not text:
                continue

            # استخراج اسم المنتج (أول سطر غالبًا) والسعر (أرقام بالنص)
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            name = lines[0] if lines else "غير معروف"

            price_match = re.search(r"(\d+[.,]?\d*)\s*(دينار|ريال|JOD|SAR|AED|\$)?", text)
            price = price_match.group(0).strip() if price_match else "غير محدد"

            products.append({
                "category": slug,
                "name": name,
                "price": price,
                "raw_text": text[:200],  # نحتفظ بجزء من النص الخام للمراجعة اليدوية عند الحاجة
            })

    except Exception as e:
        print(f"[تحذير] فشل سحب القسم {slug}: {e}")

    return products


def scrape_all_categories():
    all_products = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="ar")
        for slug in CATEGORY_SLUGS:
            print(f"جاري سحب: {slug}")
            products = scrape_category(page, slug)
            print(f"  -> {len(products)} منتج")
            all_products.extend(products)
            time.sleep(1.5)  # فاصل لطيف بين الطلبات حتى لا نثقل سيرفر الموقع الشريك
        browser.close()
    return all_products


# ============================================================
# 3. مقارنة النتائج الجديدة بالقديمة (المحفوظة بالشيت)
# ============================================================

def load_previous(ws):
    records = ws.get_all_records()
    # مفتاح المقارنة: القسم + اسم المنتج
    return {(r["category"], r["name"]): r["price"] for r in records}


def compute_changes(previous, current_products):
    changes = []
    seen_keys = set()

    for p in current_products:
        key = (p["category"], p["name"])
        seen_keys.add(key)
        old_price = previous.get(key)

        if old_price is None:
            changes.append({"type": "new_product", "category": p["category"],
                             "name": p["name"], "old_price": "-", "new_price": p["price"]})
        elif old_price != p["price"]:
            changes.append({"type": "price_change", "category": p["category"],
                             "name": p["name"], "old_price": old_price, "new_price": p["price"]})

    # منتجات كانت موجودة وصارت غير موجودة (احتمال إزالتها من الموقع الشريك)
    for (category, name) in previous:
        if (category, name) not in seen_keys:
            changes.append({"type": "removed_product", "category": category,
                             "name": name, "old_price": previous[(category, name)], "new_price": "-"})

    return changes


# ============================================================
# 4. تحديث الشيت
# ============================================================

def update_current_prices(ws, products):
    ws.clear()
    headers = ["category", "name", "price", "raw_text", "last_checked"]
    ws.append_row(headers)
    timestamp = time.strftime("%Y-%m-%d %H:%M")
    rows = [[p["category"], p["name"], p["price"], p["raw_text"], timestamp] for p in products]
    if rows:
        ws.append_rows(rows)


def append_change_log(ws, changes):
    timestamp = time.strftime("%Y-%m-%d %H:%M")
    rows = [[timestamp, c["type"], c["category"], c["name"], c["old_price"], c["new_price"]] for c in changes]
    if rows:
        ws.append_rows(rows)


# ============================================================
# 5. إشعار واتساب عبر CallMeBot
# ============================================================

def send_whatsapp_notification(changes):
    if not changes:
        return

    lines = [f"🔔 تحديثات أسعار الموقع الشريك ({len(changes)}):"]
    for c in changes[:15]:  # حد أقصى لطول الرسالة
        if c["type"] == "new_product":
            lines.append(f"🆕 {c['name']} ({c['category']}) - سعر جديد: {c['new_price']}")
        elif c["type"] == "price_change":
            lines.append(f"💲 {c['name']} ({c['category']}): {c['old_price']} ← {c['new_price']}")
        elif c["type"] == "removed_product":
            lines.append(f"❌ {c['name']} ({c['category']}) لم يعد متوفرًا")

    if len(changes) > 15:
        lines.append(f"... و{len(changes) - 15} تغييرات إضافية، راجع الشيت للتفاصيل كاملة.")

    message = "\n".join(lines)

    url = "https://api.callmebot.com/whatsapp.php"
    params = {"phone": CALLMEBOT_PHONE, "text": message, "apikey": CALLMEBOT_APIKEY}
    try:
        r = requests.get(url, params=params, timeout=15)
        print(f"إشعار واتساب: {r.status_code} - {r.text[:200]}")
    except Exception as e:
        print(f"[تحذير] فشل إرسال إشعار واتساب: {e}")


# ============================================================
# التشغيل الرئيسي
# ============================================================

def main():
    spreadsheet = connect_sheet()
    prices_ws = get_or_create_tab(
        spreadsheet, SHEET_TAB_NAME,
        ["category", "name", "price", "raw_text", "last_checked"]
    )
    log_ws = get_or_create_tab(
        spreadsheet, LOG_TAB_NAME,
        ["timestamp", "type", "category", "name", "old_price", "new_price"]
    )

    previous = load_previous(prices_ws)
    current_products = scrape_all_categories()

    if not current_products:
        print("[خطأ] لم يتم جلب أي منتجات - لن يتم تحديث الشيت تجنبًا لمسح البيانات القديمة بالخطأ.")
        return

    changes = compute_changes(previous, current_products)

    update_current_prices(prices_ws, current_products)
    if changes:
        append_change_log(log_ws, changes)
        send_whatsapp_notification(changes)
    else:
        print("لا توجد تغييرات هذه المرة.")

    print(f"تم. إجمالي المنتجات: {len(current_products)}، التغييرات: {len(changes)}")


if __name__ == "__main__":
    main()
