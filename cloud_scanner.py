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
MAX_AGE_MINUTES = 120  # Chỉ lấy bài viết mới từ 1 phút đến 120 phút (2 giờ)

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
# 1. DANH SÁCH CÁC TOPIC TAG TRỌNG ĐIỂM (Quét cả Top Tag & Recent Tag)
# =====================================================================
PRIORITY_TAGS = [
    "hoatuoisaigon",
    "Hoa Tuoi Saigon",
    "hoa tuoi saigon",
    "Flowers of threads",
    "hoatuoihcm",
    "hoa tươi sài gòn",
    "shophoasaigon",
    "tiemhoasaigon"
]

# =====================================================================
# 2. DANH SÁCH TỪ KHÓA TÌM KIẾM BỔ TRỢ
# =====================================================================
SEARCH_QUERIES = [
    "tìm gấp hoa",
    "xin in4 shop hoa",
    "tìm chỗ bán hoa",
    "tìm bó hoa",
    "cần hoa tặng",
    "cần đặt hoa",
    "cần hoa gấp",
    "đặt hoa sài gòn",
    "đặt hoa hcm",
    "hoa tốt nghiệp",
    "shop nào sẵn hoa",
    "sốp nào sẵn hoa",
    "sẵn hoa",
    "hoa gấp",
    "sale hoa",
    "hoa cưới",
    "hoa ly",
    "hoa hồng"
]

# Loại trừ các tỉnh thành ngoài TP.HCM
OTHER_PROVINCES_BLACKLIST = [
    "hà nội", "hanoi", "hn", "cầu giấy", "đống đa", "hoàn kiếm", "tây hồ", "thanh xuân",
    "hai bà trưng", "long biên", "hà đông", "ba đình", "cầu diễn", "mỹ đình", "gia lâm",
    "đà nẵng", "hải phòng", "cần thơ", "nha trang", "đà lạt", "vũng tàu",
    "huế", "quy nhơn", "vinh", "nghệ an", "thanh hóa", "bắc ninh", "hải dương", "quảng ninh"
]

# Tài khoản nội bộ của bạn
OWN_ACCOUNTS = ["tiemhoaconmeo_meoflowers", "lynk.bbq", "tenten.5990"]

# Từ khóa loại trừ Shop bán hàng & Bài tương tác chéo (chéo fl)
SELLER_BLACKLIST = [
    "nhận đặt hoa", "bên mình nhận", "tuyển thợ cắm hoa", "sỉ hoa", "tuyển nhân viên",
    "mình sẵn hoa", "em sẵn hoa", "nhà em sẵn", "shop em sẵn", "shop sẵn", "sẵn sale",
    "sẵn tại sốp", "ship liền tay", "bác nào đang cần hoa nhắn", "ib ngay cho tiệm",
    "bọn em bán hoa", "bơi vào đây với shop", "nhận theo yêu cầu làm mẫu", "tham khảo sốp",
    "sẵn mụt bó", "sẵn 1 bó", "sẵn một bó", "giá order", "chéo fl", "chéo follow",
    "fl chéo", "khởi nghiệp bán hoa", "trả đủ ạ", "fl lại liền"
]

HCM_SIGNALS = [
    "hcm", "tphcm", "tp.hcm", "tp hcm", "hồ chí minh", "sài gòn", "saigon", "sg",
    "thủ đức", "thảo điền", "bình thạnh", "phú nhuận", "tân bình", "tân phú",
    "gò vấp", "bình tân", "quận 1", "quận 2", "quận 3", "quận 4", "quận 5",
    "quận 6", "quận 7", "quận 8", "quận 9", "quận 10", "quận 11", "quận 12",
    "q1", "q2", "q3", "q4", "q5", "q6", "q7", "q8", "q9", "q10", "q11", "q12",
    "nhà bè", "hóc môn", "bình chánh", "củ chi", "hồ thị kỷ", "hoa tuoi saigon", "hoatuoisaigon"
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

def is_within_1_to_120_minutes(raw_text: str) -> tuple[bool, str, int]:
    """
    Chỉ chấp nhận bài viết trong khung thời gian từ 1 phút đến 120 phút (2 giờ):
    - 'vừa xong', '... giây', '1 phút' -> '59 phút'
    - '1 giờ' (60 phút), '2 giờ' (120 phút)
    - Từ '3 giờ' trở lên hoặc ngày/tuần/tháng (như 2/8/26, 11 giờ, 9 giờ) -> Loại bỏ.
    """
    lines = [l.strip().lower() for l in raw_text.splitlines() if l.strip()]
    for line in lines[:8]:
        if line in ["vừa xong", "now", "just now"] or re.match(r"^(\d+)\s*(giây|s)$", line):
            return True, f"Vừa đăng ({line})", 1

        m_min = re.match(r"^(\d+)\s*(phút|m)$", line)
        if m_min:
            mins = int(m_min.group(1))
            if mins <= MAX_AGE_MINUTES:
                return True, f"{mins} phút trước", mins
            return False, f"Quá 120 phút ({mins} phút trước)", mins

        m_hour = re.match(r"^(\d+)\s*(giờ|h)$", line)
        if m_hour:
            hours = int(m_hour.group(1))
            mins = hours * 60
            if mins <= MAX_AGE_MINUTES:
                return True, f"{hours} giờ trước", mins
            return False, f"Quá 120 phút ({hours} giờ trước)", mins

        if re.match(r"^(\d+)\s*(ngày|d|tuần|w|tháng|năm)$", line):
            return False, f"Bài cũ ({line})", 99999
        if re.search(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", line):
            return False, f"Bài cũ ({line})", 99999

    return False, "Không rõ mốc thời gian", 99999

def evaluate_post(post_text: str, username: str, source_label: str) -> dict:
    if username.lower() in OWN_ACCOUNTS:
        return {"is_buyer": False, "reason": "Tài khoản nội bộ"}

    t = post_text.lower()

    # 1. Phải có ngữ cảnh liên quan đến Hoa (trong bài viết hoặc nằm trong Tag Hoa)
    flower_keywords = [
        "hoa", "bó", "bông", "lẵng", "giỏ", "tulip", "cẩm tú cầu", "hướng dương",
        "tiệm", "shop", "sốp", "florist", "hoatuoisaigon", "flowers of threads"
    ]
    if not any(fk in t for fk in flower_keywords) and "hoa" not in source_label.lower() and "flower" not in source_label.lower():
        return {"is_buyer": False, "reason": "Không liên quan đến hoa tươi"}

    # 2. Loại trừ ngay nếu bài viết ở Hà Nội hoặc tỉnh thành khác
    for prov in OTHER_PROVINCES_BLACKLIST:
        if re.search(rf"\b{re.escape(prov)}\b", t):
            return {"is_buyer": False, "reason": f"Khách ở tỉnh khác ({prov})"}

    # 3. Loại trừ ngay bài bán hàng của shop hoặc bài chéo follow
    for seller_kw in SELLER_BLACKLIST:
        if seller_kw in t:
            return {"is_buyer": False, "reason": f"Shop bán hàng / Tương tác chéo ('{seller_kw}')"}

    # Nếu bài đăng báo giá kiểu '• 280.000' hoặc '280k' kèm địa chỉ '📍' mà không có từ hỏi mua -> Là shop bán
    if ("📍" in t or "•" in t) and not any(q in t for q in ["tìm", "cần", "xin", "hỏi", "không", "?"]):
        return {"is_buyer": False, "reason": "Bài đăng bán mẫu hoa có sẵn của shop"}

    # 4. Nhận diện đầy đủ các mẫu câu Khách hàng tìm mua hoa thực tế (bám sát ảnh chụp)
    buyer_signals = [
        "tìm chỗ bán", "tìm bó hoa", "tìm hoa", "tìm gấp hoa", "tìm shop", "tìm tiệm",
        "xin in4", "xin info", "in4 shop", "in4 các shop", "xin giá", "báo giá", "công khai giá",
        "cần tìm", "cần hoa", "cần đặt hoa", "cần mua hoa", "cần 1 bó", "cần 2 bó", "cần 3 bó",
        "đặt hoa", "mua được bó", "mua bó", "mua hoa", "mẫu hoa",
        "budget", "tc tầm", "tài chính", "100k-150k", "100k - 150k", "200k", "300k", "500k", "1tr",
        "sốp nào", "shop nào", "tiệm nào", "chỗ nào", "ở đâu", "gần q", "kv thủ đức", "kv:",
        "giao hoả tốc", "giao hỏa tốc", "giao liền", "ship liền",
        "có ai sale", "có ai bán", "có ai nhận", "cho mình hỏi", "cho em hỏi", "tớ cần",
        "rcm", "recommend", "còn thức không", "sẵn hoa", "hoa gấp", "hoa tốt nghiệp"
    ]

    matched_signal = None
    for bs in buyer_signals:
        if bs in t:
            matched_signal = bs
            break

    if not matched_signal:
        return {"is_buyer": False, "reason": "Có chữ hoa nhưng không phải câu hỏi tìm mua"}

    # 5. Kiểm tra tín hiệu khu vực TP.HCM / Sài Gòn
    matched_hcm = None
    for sig in HCM_SIGNALS:
        if re.search(rf"\b{re.escape(sig)}\b", t):
            matched_hcm = sig.upper()
            break

    if not matched_hcm and ("saigon" in source_label.lower() or "hcm" in source_label.lower()):
        matched_hcm = "TAG HOA TUOI SAIGON"

    loc_info = matched_hcm if matched_hcm else "TP.HCM / Bảng tin"

    return {
        "is_buyer": True,
        "reason": f"Khách mua hoa [{loc_info}] - Khớp tín hiệu: '{matched_signal}'",
        "suggested_comment": (
            "Dạ bạn tham khảo sốp tụi mình ở Sài Gòn nha, tụi mình có nhận làm mẫu theo đúng ngân sách và gu bạn thích, "
            "giá mềm và có giao liền khắp các quận HCM ạ! 🌸"
        )
    }

async def send_telegram_alert(username: str, post_text: str, permalink: str, draft_comment: str, source_label: str, reasoning: str, time_label: str):
    safe_user = html.escape(username)
    safe_text = html.escape(post_text)
    safe_draft = html.escape(draft_comment)
    safe_reason = html.escape(reasoning)
    safe_source = html.escape(source_label)

    msg = (
        f"🌸 <b>KHÁCH TÌM MUA HOA MỚI ({html.escape(time_label)})!</b>\n"
        f"🏷️ <b>Nguồn / Tag:</b> <code>{safe_source}</code>\n"
        f"👤 <b>Khách hàng:</b> @{safe_user}\n\n"
        f"📝 <b>Nội dung bài đăng:</b>\n<i>\"{safe_text}\"</i>\n\n"
        f"🎯 <b>Phân tích:</b> {safe_reason}\n\n"
        f"💬 <b>Câu trả lời gợi ý (Chạm nhẹ để Copy):</b>\n"
        f"<code>{safe_draft}</code>"
    )
    keyboard = {"inline_keyboard": [[{"text": "📱 Mở bài này trong App Threads để Comment", "url": permalink}]]}
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

async def scan_page_stream(page, source_label: str, scroll_rounds: int, global_seen_codes: set, logs: list):
    seen_count = 0
    fresh_120m_count = 0
    leads_count = 0

    for _ in range(scroll_rounds):
        links = await page.locator('a[href*="/post/"]').all()
        for link in links:
            try:
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
                seen_count += 1

                container = link.locator("xpath=ancestor::div[contains(@data-pressable-container, 'true') or @role='article'][1]")
                raw_text = await container.inner_text() if await container.count() > 0 else await link.locator("xpath=../../..").inner_text()

                is_fresh, time_label, age_mins = is_within_1_to_120_minutes(raw_text)
                if not is_fresh:
                    continue

                fresh_120m_count += 1

                clean_lines = [
                    line.strip() for line in raw_text.splitlines()
                    if len(line.strip()) >= 2 and line.strip() not in [username, "Translate", "Dịch", "Cuộc trò chuyện đầu tiên"]
                ]
                post_text = " | ".join(clean_lines)[:450]
                short_preview = html.escape(post_text[:90])
                safe_user = html.escape(username)
                permalink = f"https://www.threads.net/@{username}/post/{post_code}"

                eval_res = evaluate_post(post_text, username, source_label)

                if eval_res["is_buyer"]:
                    if is_post_notified(post_code):
                        logs.append(f"☑️ @{safe_user} ({html.escape(time_label)}): Đã báo khách này trước đó.")
                        continue
                    leads_count += 1
                    draft = eval_res["suggested_comment"]
                    reason = eval_res["reason"]
                    save_lead(post_code, username, post_text, permalink, draft, "NOTIFIED")
                    await send_telegram_alert(username, post_text, permalink, draft, source_label, reason, time_label)
                    logs.append(f"✅ @{safe_user} (<b>{html.escape(time_label)}</b> - <i>{html.escape(source_label)}</i>):\n   \"{short_preview}...\"\n   ↳ <b>CHỐT KHÁCH:</b> {html.escape(reason)}")
                else:
                    if "Không liên quan" not in eval_res["reason"] and len(logs) < 12:
                        logs.append(f"❌ @{safe_user} (<b>{html.escape(time_label)}</b> - <i>{html.escape(source_label)}</i>): \"{short_preview}...\"\n   ↳ <i>Loại vì: {html.escape(eval_res['reason'])}</i>")
            except Exception:
                continue

        await page.mouse.wheel(0, 850)
        await page.wait_for_timeout(1300)

    return seen_count, fresh_120m_count, leads_count

async def run_single_scan_batch():
    init_db()
    storage = build_cross_domain_storage()
    total_seen = 0
    total_fresh_120m = 0
    total_leads = 0
    logs = []
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

        # =================================================================
        # 1. RÀ SOÁT BẢNG TIN TRANG CHỦ & CÁC KHÁCH MỚI TRONG ẢNH CHỤP
        # =================================================================
        home_targets = [
            ("Bảng tin Trang chủ", "https://www.threads.net/"),
            ("Tag Hoa Tuoi Saigon (@imaii7_x)", "https://www.threads.net/@imaii7_x"),
            ("Tag hoa tuoi saigon (@__hopele_)", "https://www.threads.net/@__hopele_")
        ]
        for label, url in home_targets:
            print(f"\n[*] Đang rà: {label}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(3000)
                rounds = 12 if "Trang chủ" in label else 2
                s, f, l = await scan_page_stream(page, label, rounds, global_seen_codes, logs)
                total_seen += s
                total_fresh_120m += f
                total_leads += l
            except Exception as e:
                print(f"[!] Lỗi quét {label}: {e}")

        # =================================================================
        # 2. RÀ SOÁT CHUYÊN SÂU CÁC TOPIC TAG (hoatuoisaigon, Hoa Tuoi Saigon, Flowers of threads...)
        # Quét cả chế độ Mặc định của Tag (serp_type=default) VÀ chế độ Gần đây (filter=recent)
        # =================================================================
        for tag in PRIORITY_TAGS:
            enc_tag = urllib.parse.quote(tag)
            tag_urls = [
                (f"Tag [{tag}] (Hàng đầu)", f"https://www.threads.net/search?q={enc_tag}&serp_type=default"),
                (f"Tag [{tag}] (Gần đây)", f"https://www.threads.net/search?q={enc_tag}&serp_type=recent&filter=recent")
            ]
            for label, url in tag_urls:
                print(f"\n[*] Đang rà {label}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    await page.wait_for_timeout(2800)
                    s, f, l = await scan_page_stream(page, label, 5, global_seen_codes, logs)
                    total_seen += s
                    total_fresh_120m += f
                    total_leads += l
                except Exception as e:
                    print(f"[!] Lỗi quét {label}: {e}")

        # =================================================================
        # 3. RÀ SOÁT CÁC TỪ KHÓA MỞ RỘNG (Chế độ Gần đây)
        # =================================================================
        for q in SEARCH_QUERIES:
            print(f"\n[*] Đang rà từ khóa: '{q}'")
            enc = urllib.parse.quote(q)
            url = f"https://www.threads.net/search?q={enc}&serp_type=recent&filter=recent"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(2500)
                s, f, l = await scan_page_stream(page, f"Từ khóa: {q}", 3, global_seen_codes, logs)
                total_seen += s
                total_fresh_120m += f
                total_leads += l
            except Exception as e:
                print(f"[!] Lỗi quét từ khóa '{q}': {e}")

        await browser.close()

    if EVENT_NAME == "workflow_dispatch":
        log_section = "\n\n".join(logs[:12]) if logs else "<i>Trong 1 - 120 phút qua chưa có bài đăng mới nào.</i>"
        report_msg = (
            f"🏷️ <b>BÁO CÁO RÀ SOÁT TAG & BẢNG TIN (1 - 120 PHÚT):</b>\n"
            f"• <b>Tổng số bài đã lướt:</b> {total_seen} bài\n"
            f"• <b>Bài mới trong khung 1 - 120 phút:</b> {total_fresh_120m} bài\n"
            f"• <b>Khách tìm mua hoa chốt được:</b> {total_leads} khách\n\n"
            f"📋 <b>Chi tiết xử lý (1 - 120 phút):</b>\n\n"
            f"{log_section}"
        )
        await send_telegram_debug(report_msg[:3900])

if __name__ == "__main__":
    asyncio.run(run_single_scan_batch())
