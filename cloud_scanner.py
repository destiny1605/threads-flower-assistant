import os
import re
import html
import json
import sqlite3
import asyncio
import urllib.parse
import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "manual")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

DB_PATH = "threads_flower_leads.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            post_id TEXT PRIMARY KEY,
            username TEXT,
            post_text TEXT,
            permalink TEXT,
            draft_comment TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_post_notified(post_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM leads WHERE post_id = ? AND status = 'NOTIFIED'", (post_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def save_lead(post_id: str, username: str, post_text: str, permalink: str, draft_comment: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO leads (post_id, username, post_text, permalink, draft_comment, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (post_id, username, post_text, permalink, draft_comment, status))
    conn.commit()
    conn.close()

# =====================================================================
# BỘ TỪ KHÓA KHOANH VÙNG ĐỘC QUYỀN TP. HỒ CHÍ MINH / SÀI GÒN
# =====================================================================
SEARCH_QUERIES = [
    "Hoa Tuoi Saigon",
    "đặt hoa sài gòn",
    "đặt hoa hcm",
    "cần hoa gấp tối nay",
    "cần hoa gấp hcm",
    "hoa tốt nghiệp",
    "hoa tốt nghiệp hcm",
    "shop nào sẵn hoa sài gòn",
    "sốp nào sẵn hoa sài gòn",
    "sẵn hoa sài gòn",
    "sẵn hoa hcm",
    "hoa gấp sài gòn",
    "sale hoa sài gòn",
    "hoa cưới sài gòn",
    "hoa hồng sài gòn",
    "hoa ly sài gòn",
    "rcm shop hoa sài gòn",
    "tìm tiệm hoa sài gòn"
]

# Từ khóa loại trừ 100% các tỉnh thành khác trên cả nước
OTHER_PROVINCES_BLACKLIST = [
    "hà nội", "hanoi", "hn", "cầu giấy", "đống đa", "hoàn kiếm", "tây hồ", "thanh xuân",
    "hai bà trưng", "long biên", "hà đông", "ba đình", "cầu diễn", "mỹ đình", "gia lâm",
    "đà nẵng", "hải phòng", "cần thơ", "nha trang", "đà lạt", "vũng tàu", "biên hòa",
    "đồng nai", "bình dương", "huế", "quy nhơn", "vinh", "nghệ an", "thanh hóa",
    "bắc ninh", "hải dương", "quảng ninh", "hạ long", "buôn ma thuột", "rạch giá"
]

# Các câu rao bán đặc trưng của chủ shop hoa (để loại bỏ shop đối thủ)
SELLER_BLACKLIST = [
    "nhận đặt hoa", "bên mình nhận", "tuyển thợ cắm hoa", "sỉ hoa", "tuyển nhân viên",
    "mình sẵn hoa", "em sẵn hoa", "nhà em sẵn", "shop em sẵn", "shop sẵn", "sẵn sale",
    "sẵn tại sốp", "ship liền tay", "bác nào đang cần hoa nhắn", "ib ngay cho tiệm",
    "bọn em bán hoa", "bơi vào đây với shop"
]

# BẮT BUỘC bài viết hoặc Topic phải chứa ít nhất 1 trong các từ khóa địa danh TP.HCM này
HCM_REQUIRED_SIGNALS = [
    "hcm", "tphcm", "tp.hcm", "tp hcm", "hồ chí minh", "sài gòn", "saigon", "sg",
    "thủ đức", "thảo điền", "bình thạnh", "phú nhuận", "tân bình", "tân phú",
    "gò vấp", "bình tân", "quận 1", "quận 2", "quận 3", "quận 4", "quận 5",
    "quận 6", "quận 7", "quận 8", "quận 9", "quận 10", "quận 11", "quận 12",
    "q1", "q2", "q3", "q4", "q5", "q6", "q7", "q8", "q9", "q10", "q11", "q12",
    "nhà bè", "hóc môn", "bình chánh", "củ chi", "hồ thị kỷ", "đầm sen", "bến thành"
]

def build_cross_domain_storage() -> str | None:
    if not os.path.exists("threads_session.json"):
        return None
    try:
        with open("threads_session.json", "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return None
        data = json.loads(raw)
        orig_cookies = data.get("cookies", [])
        new_cookies = list(orig_cookies)
        for c in orig_cookies:
            dom = c.get("domain", "")
            if "instagram.com" in dom:
                for target_dom in [".threads.com", ".threads.net"]:
                    clone = dict(c)
                    clone["domain"] = target_dom
                    new_cookies.append(clone)
        data["cookies"] = new_cookies
        merged_path = "threads_session_merged.json"
        with open(merged_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return merged_path
    except Exception as e:
        print(f"[!] Lỗi xử lý session: {e}")
        return "threads_session.json"

def check_post_freshness(raw_text: str) -> tuple[bool, str, int]:
    """
    Trả về (is_fresh, label, age_in_minutes).
    - Ưu tiên số 1: Bài từ 1 - 30 phút (age_in_minutes <= 30).
    - Chấp nhận bài trong ngày hôm nay (<= 18 giờ) nếu Threads chưa mở tab Recent, nhưng loại sạch bài từ nhiều ngày trước.
    """
    lines = [l.strip().lower() for l in raw_text.splitlines() if l.strip()]
    for line in lines[:6]:
        if line in ["vừa xong", "now", "just now"] or re.match(r"^(\d+)\s*(giây|s)$", line):
            return True, f"Vừa đăng ({line})", 1

        m_min = re.match(r"^(\d+)\s*(phút|m)$", line)
        if m_min:
            mins = int(m_min.group(1))
            return (mins <= 30), f"{mins} phút trước", mins

        m_hour = re.match(r"^(\d+)\s*(giờ|h)$", line)
        if m_hour:
            hours = int(m_hour.group(1))
            # Nếu quá 16 giờ hoặc thuộc ngày cũ -> Loại
            return (hours <= 16), f"{hours} giờ trước", hours * 60

        if re.match(r"^(\d+)\s*(ngày|d|tuần|w|tháng|năm)$", line):
            return False, f"Bài cũ ({line})", 99999
        if re.search(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", line):
            return False, f"Bài cũ ({line})", 99999

    return False, "Không rõ thời gian", 99999

def evaluate_hcm_flower_lead(post_text: str) -> dict:
    """
    Bộ lọc Khoanh vùng Nghiêm ngặt TP.HCM / Sài Gòn:
    1. Loại thẳng nếu nhắc đến bất kỳ tỉnh thành nào khác.
    2. BẮT BUỘC trong bài hoặc Topic phải có từ khóa địa điểm TP.HCM / Sài Gòn.
    3. Phân biệt chính xác Khách hỏi mua vs Shop rao bán.
    """
    t = post_text.lower()

    # 1. Kiểm tra tỉnh thành khác -> Loại ngay lập tức
    for prov in OTHER_PROVINCES_BLACKLIST:
        if re.search(rf"\b{re.escape(prov)}\b", t):
            return {
                "is_buyer": False,
                "reason": f"Thuộc tỉnh/thành khác ({prov})"
            }

    # 2. Kiểm tra BẮT BUỘC phải có địa danh Sài Gòn / TP.HCM / Quận Huyện HCM
    matched_hcm = None
    for sig in HCM_REQUIRED_SIGNALS:
        if re.search(rf"\b{re.escape(sig)}\b", t):
            matched_hcm = sig
            break

    if not matched_hcm:
        return {
            "is_buyer": False,
            "reason": "Không ghi địa điểm tại TP.HCM / Sài Gòn"
        }

    # 3. Kiểm tra xem có phải bài rao bán của Shop hoa khác không
    for seller_kw in SELLER_BLACKLIST:
        if seller_kw in t:
            return {
                "is_buyer": False,
                "reason": f"Bài quảng cáo của shop hoa ('{seller_kw}')"
            }

    # Nếu có báo giá tiền cụ thể kèm từ rao bán (100k, 150k, 159k...) mà không có dấu chấm hỏi -> Là shop bán
    if re.search(r"\b\d{2,3}k\b", t) and "?" not in t and "không" not in t and "tìm" not in t:
        return {
            "is_buyer": False,
            "reason": "Shop đăng báo giá bán hoa"
        }

    # 4. Kiểm tra các mẫu câu KHÁCH HÀNG ĐANG HỎI MUA / TÌM SHOP tại TP.HCM
    buyer_signals = [
        "cần đặt hoa", "cần hoa gấp", "cần tìm hoa", "đặt hoa gấp",
        "sốp nào", "shop nào", "tiệm nào", "chỗ nào", "ở đâu",
        "có ai sale hoa", "có ai bán", "có ai nhận", "có shop nào",
        "cho mình hỏi", "cho em hỏi", "tìm shop", "tìm tiệm",
        "rcm", "recommend", "còn thức không", "sẵn hoa không",
        "cần mua hoa", "mua hoa tốt nghiệp", "đặt hoa tốt nghiệp", "hoa cưới"
    ]

    for bs in buyer_signals:
        if bs in t:
            return {
                "is_buyer": True,
                "hcm_location": matched_hcm.upper(),
                "reason": f"Khách tại [{matched_hcm.upper()}] đang hỏi mua hoa ('{bs}')",
                "suggested_comment": (
                    "Dạ tiệm tụi mình ở Sài Gòn đang có sẵn nhiều mẫu hoa tươi xinh lắm và nhận giao gấp liền trong ngày ạ. "
                    "Bạn nhắn tụi mình gửi mẫu cho bạn chọn nha! 🌸"
                )
            }

    return {
        "is_buyer": False,
        "reason": "Bài cảm thán/tâm sự, không có câu hỏi tìm mua hoa"
    }

async def send_telegram_alert(username: str, post_text: str, permalink: str, draft_comment: str, query_used: str, reasoning: str, time_label: str):
    safe_user = html.escape(username)
    safe_text = html.escape(post_text)
    safe_draft = html.escape(draft_comment)
    safe_reason = html.escape(reasoning)
    safe_query = html.escape(query_used)

    msg = (
        f"🌸 <b>KHÁCH TÌM HOA TẠI TP.HCM ({html.escape(time_label)})!</b>\n"
        f"🔎 <b>Từ khóa:</b> <code>{safe_query}</code>\n"
        f"👤 <b>Khách hàng:</b> @{safe_user}\n\n"
        f"📝 <b>Nội dung bài đăng:</b>\n<i>\"{safe_text}\"</i>\n\n"
        f"🎯 <b>Phân tích:</b> {safe_reason}\n\n"
        f"💬 <b>Câu trả lời soạn sẵn (Chạm nhẹ vào chữ dưới để Copy):</b>\n"
        f"<code>{safe_draft}</code>"
    )
    keyboard = {"inline_keyboard": [[{"text": "📱 Mở bài này trong App Threads để dán Comment", "url": permalink}]]}
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": keyboard}
        )

async def send_telegram_debug(text: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        )

async def run_single_scan_batch():
    init_db()
    storage = build_cross_domain_storage()
    total_posts_seen = 0
    fresh_30m_count = 0
    today_posts_count = 0
    total_leads_found = 0
    hcm_logs = []
    global_seen_codes = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            storage_state=storage,
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        scan_targets = [
            ("Kiểm tra @tenten.5990", "https://www.threads.net/@tenten.5990"),
            ("Kiểm tra @lynk.bbq", "https://www.threads.net/@lynk.bbq")
        ]
        for q in SEARCH_QUERIES:
            enc = urllib.parse.quote(q)
            scan_targets.append((q, f"https://www.threads.net/search?q={enc}&serp_type=recent&filter=recent"))

        for label, url in scan_targets:
            print(f"\n[*] Đang quét: '{label}'")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(2800)

                await page.mouse.wheel(0, 700)
                await page.wait_for_timeout(1000)

                links = await page.locator('a[href*="/post/"]').all()
                for link in links:
                    href = await link.get_attribute("href")
                    if not href:
                        continue
                    match = re.search(r"/@([^/]+)/post/([A-Za-z0-9_-]+)", href)
                    if not match:
                        continue
                    username, post_code = match.group(1), match.group(2)
                    if post_code in global_seen_codes:
                        continue
                    global_seen_codes.add(post_code)
                    total_posts_seen += 1

                    container = link.locator("xpath=ancestor::div[contains(@data-pressable-container, 'true') or @role='article'][1]")
                    raw_text = await container.inner_text() if await container.count() > 0 else await link.locator("xpath=../../..").inner_text()

                    is_fresh, time_label, age_mins = check_post_freshness(raw_text)
                    if not is_fresh:
                        continue

                    if age_mins <= 30:
                        fresh_30m_count += 1
                    today_posts_count += 1

                    clean_lines = [
                        line.strip() for line in raw_text.splitlines()
                        if len(line.strip()) >= 2 and line.strip() not in [username, "Translate", "Dịch"]
                    ]
                    post_text = " | ".join(clean_lines)[:450]
                    short_preview = html.escape(post_text[:85])
                    safe_user = html.escape(username)
                    permalink = f"https://www.threads.net/@{username}/post/{post_code}"

                    # Đánh giá Khoanh vùng chặt TP.HCM / Sài Gòn
                    eval_res = evaluate_hcm_flower_lead(post_text)

                    if eval_res["is_buyer"]:
                        if is_post_notified(post_code):
                            hcm_logs.append(f"☑️ @{safe_user} ({html.escape(time_label)}): Đã báo khách này trước đó.")
                            continue
                        total_leads_found += 1
                        draft = eval_res["suggested_comment"]
                        reason = eval_res["reason"]
                        save_lead(post_code, username, post_text, permalink, draft, "NOTIFIED")
                        await send_telegram_alert(username, post_text, permalink, draft, label, reason, time_label)
                        hcm_logs.append(f"✅ @{safe_user} (<b>{html.escape(time_label)}</b>): \"{short_preview}...\"\n   ↳ <b>CHỐT KHÁCH:</b> {html.escape(reason)}")
                    else:
                        reason = eval_res["reason"]
                        # Chỉ ghi vào báo cáo những bài mới (<= 30 phút) hoặc có chữ Sài Gòn/HCM để bạn dễ theo dõi
                        if age_mins <= 30 or "TP.HCM" not in reason:
                            if len(hcm_logs) < 10:
                                hcm_logs.append(f"❌ @{safe_user} (<b>{html.escape(time_label)}</b>): \"{short_preview}...\"\n   ↳ <i>Loại vì: {html.escape(reason)}</i>")

            except Exception as e:
                print(f"[!] Lỗi khi quét '{label}': {e}")

        await browser.close()

    if EVENT_NAME == "workflow_dispatch":
        log_section = "\n\n".join(hcm_logs[:10]) if hcm_logs else "<i>Chưa có bài mới nào tại khu vực TP.HCM / Sài Gòn.</i>"
        report_msg = (
            f"📍 <b>BÁO CÁO KHOANH VÙNG ĐỘC QUYỀN TP.HCM / SÀI GÒN:</b>\n"
            f"• <b>Tổng bài lướt qua:</b> {total_posts_seen} bài\n"
            f"• <b>Bài vừa đăng (1 - 30 phút):</b> {fresh_30m_count} bài\n"
            f"• <b>Khách đặt hoa TP.HCM phát hiện mới:</b> {total_leads_found} khách\n\n"
            f"📋 <b>Chi tiết kiểm tra khu vực TP.HCM:</b>\n\n"
            f"{log_section}"
        )
        await send_telegram_debug(report_msg[:3900])

if __name__ == "__main__":
    asyncio.run(run_single_scan_batch())
