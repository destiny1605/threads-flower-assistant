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
MAX_AGE_MINUTES = 30  # Chỉ lấy bài viết mới từ 1 phút đến tối đa 30 phút đổ lại

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

SEARCH_QUERIES = [
    "cần đặt hoa gấp",
    "sốp nào còn thức",
    "Hoa Tuoi Saigon",
    "cần đặt hoa",
    "shop nào sẵn hoa",
    "sốp nào sẵn hoa",
    "cần hoa gấp",
    "đặt hoa sài gòn",
    "đặt hoa hcm",
    "sẵn hoa",
    "hoa gấp",
    "hoa tốt nghiệp",
    "sale hoa",
    "hoa ly",
    "hoa hồng",
    "hoa cưới",
    "rcm shop hoa",
    "tìm tiệm hoa"
]

NON_HCM_BLACKLIST = [
    "hà nội", "hanoi", "cầu giấy", "đống đa", "hoàn kiếm", "tây hồ", "thanh xuân",
    "hai bà trưng", "long biên", "hà đông", "ba đình", "cầu diễn", "đà nẵng", "hải phòng",
    "nhận đặt hoa", "bên mình nhận", "tuyển thợ cắm hoa", "sỉ hoa", "tuyển nhân viên",
    "mình sẵn hoa", "em sẵn hoa", "nhà em sẵn", "shop em sẵn", "shop sẵn"
]

HCM_SIGNALS = [
    "hcm", "tphcm", "sài gòn", "saigon", "sg", "thủ đức", "thảo điền",
    "bình thạnh", "phú nhuận", "tân bình", "tân phú", "gò vấp", "bình tân",
    "quận 1", "quận 2", "quận 3", "quận 4", "quận 5", "quận 6", "quận 7",
    "quận 8", "quận 9", "quận 10", "quận 11", "quận 12", "q1", "q2", "q3",
    "q4", "q5", "q6", "q7", "q8", "q9", "q10", "q11", "q12",
    "nhà bè", "hóc môn", "bình chánh", "hồ thị kỷ", "sốp nào", "shop nào", "bông"
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

def is_within_30_minutes(raw_text: str) -> tuple[bool, str]:
    """
    Bộ lọc Nghiêm ngặt: CHỈ CHẤP NHẬN bài viết vừa đăng từ 1 giây đến tối đa 30 phút trước.
    Loại bỏ 100% bài tính bằng giờ (h/giờ), ngày (d/ngày), tuần, hoặc ngày tháng (dd/mm/yyyy).
    """
    lines = [l.strip().lower() for l in raw_text.splitlines() if l.strip()]
    for line in lines[:6]:
        # 1. Nếu là vừa xong hoặc tính bằng giây -> ĐẠT NGAY
        if line in ["vừa xong", "now", "just now"] or re.match(r"^(\d+)\s*(giây|s)$", line):
            return True, f"Vừa đăng ({line})"

        # 2. Nếu tính bằng phút -> Chỉ nhận từ 1 đến 30 phút
        m_min = re.match(r"^(\d+)\s*(phút|m)$", line)
        if m_min:
            mins = int(m_min.group(1))
            if mins <= MAX_AGE_MINUTES:
                return True, f"{mins} phút trước"
            else:
                return False, f"Quá 30 phút ({mins} phút trước)"

        # 3. Nếu tính bằng giờ, ngày, tuần, hoặc ngày/tháng/năm -> LOẠI NGAY LẬP TỨC
        if re.match(r"^(\d+)\s*(giờ|h|ngày|d|tuần|w|tháng|năm)$", line):
            return False, f"Bài cũ ({line})"
        if re.search(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", line):
            return False, f"Bài cũ ({line})"

    return False, "Không rõ mốc thời gian mới"

def fallback_rule_evaluation(post_text: str, has_hcm_kw: bool) -> dict:
    t = post_text.lower()
    seller_patterns = [
        "100k", "120k", "150k", "159k", "200k", "380", "ib ngay",
        "nhắn sốp nha", "sẵn ạ", "freeship", "đang có sẵn tại sốp", "bọn em bán hoa"
    ]
    if any(sp in t for sp in seller_patterns):
        return {
            "is_buyer_seeking_flowers": False,
            "location_verdict": "SELLER",
            "confidence_score": 0.0,
            "reasoning": "Bài rao bán hoa của shop",
            "suggested_comment": ""
        }

    buyer_patterns = [
        "cần đặt hoa", "cần hoa gấp", "sốp nào", "shop nào", "tiệm nào",
        "có ai", "cho mình hỏi", "cho em hỏi", "tìm shop", "tìm tiệm",
        "rcm", "recommend", "còn thức không", "chỗ nào bán", "mua ở đâu", "hoa gấp"
    ]
    if any(bp in t for bp in buyer_patterns):
        return {
            "is_buyer_seeking_flowers": True,
            "location_verdict": "CONFIRMED_HCM" if has_hcm_kw else "LIKELY_HCM",
            "confidence_score": 0.90,
            "reasoning": "Khách đang hỏi tìm shop đặt hoa mới tức thì",
            "suggested_comment": "Dạ tiệm tụi mình ở Sài Gòn vẫn đang trực nhận đơn hoa gấp và có sẵn nhiều mẫu hoa tươi xinh lắm ạ. Bạn nhắn tụi mình tư vấn liền nha! 🌸"
        }

    return {
        "is_buyer_seeking_flowers": False,
        "location_verdict": "UNKNOWN",
        "confidence_score": 0.2,
        "reasoning": "Không phải câu hỏi tìm mua hoa",
        "suggested_comment": ""
    }

async def analyze_post_with_ai(post_text: str, username: str, query_used: str) -> dict:
    text_lower = post_text.lower()
    for bad in NON_HCM_BLACKLIST:
        if bad in text_lower:
            return {
                "is_buyer_seeking_flowers": False,
                "location_verdict": "BLACKLISTED",
                "confidence_score": 0.0,
                "reasoning": f"Loại trừ từ khóa: '{bad}'",
                "suggested_comment": ""
            }

    has_hcm_kw = (
        any(sig in text_lower for sig in HCM_SIGNALS)
        or ("saigon" in query_used.lower())
        or ("sài gòn" in query_used.lower())
        or ("hcm" in query_used.lower())
    )

    prompt = f"""
Phân tích bài đăng Threads dưới đây và trả về JSON:
- Người đăng: @{username}
- Từ khóa: {query_used}
- Nội dung: {post_text}

Trả về đúng định dạng JSON:
{{"is_buyer_seeking_flowers": true/false, "location_verdict": "CONFIRMED_HCM" hoặc "LIKELY_HCM" hoặc "OUTSIDE", "confidence_score": 0.9, "reasoning": "lý do ngắn gọn", "suggested_comment": "câu trả lời dưới 220 ký tự"}}
"""
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={OPENAI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(api_url, json=payload)
            if resp.status_code != 200:
                return fallback_rule_evaluation(post_text, has_hcm_kw)
            data = resp.json()
            raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw_json)
    except Exception:
        return fallback_rule_evaluation(post_text, has_hcm_kw)

async def send_telegram_alert(username: str, post_text: str, permalink: str, draft_comment: str, query_used: str, reasoning: str, time_label: str):
    safe_user = html.escape(username)
    safe_text = html.escape(post_text)
    safe_draft = html.escape(draft_comment)
    safe_reason = html.escape(reasoning)
    safe_query = html.escape(query_used)

    msg = (
        f"🌸 <b>PHÁT HIỆN KHÁCH ĐẶT HOA MỚI ({html.escape(time_label)})!</b>\n"
        f"🔎 <b>Nguồn quét:</b> <code>{safe_query}</code>\n"
        f"👤 <b>Khách hàng:</b> @{safe_user}\n\n"
        f"📝 <b>Nội dung bài đăng:</b>\n<i>\"{safe_text}\"</i>\n\n"
        f"🤖 <b>Đánh giá:</b> {safe_reason}\n\n"
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
    old_skipped_count = 0
    total_leads_found = 0
    fresh_logs = []
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
            ("Trang test @tenten.5990", "https://www.threads.net/@tenten.5990")
        ]
        for q in SEARCH_QUERIES:
            enc = urllib.parse.quote(q)
            scan_targets.append((q, f"https://www.threads.net/search?q={enc}&serp_type=recent&filter=recent"))

        for label, url in scan_targets:
            print(f"\n[*] Đang quét: '{label}'")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_timeout(3000)

                # Bấm vào tab "Gần đây" / "Recent" nếu hiện trên giao diện
                recent_tab = page.locator('div[role="tab"]:has-text("Gần đây"), div[role="tab"]:has-text("Recent"), a[href*="filter=recent"]')
                if await recent_tab.count() > 0:
                    try:
                        await recent_tab.first.click(timeout=1500)
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass

                await page.mouse.wheel(0, 700)
                await page.wait_for_timeout(1200)

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

                    # CHỈ CHẤP NHẬN BÀI TRONG VÒNG 1 - 30 PHÚT ĐỔ LẠI
                    is_fresh, time_label = is_within_30_minutes(raw_text)
                    if not is_fresh:
                        old_skipped_count += 1
                        continue

                    fresh_30m_count += 1

                    if is_post_notified(post_code):
                        fresh_logs.append(f"☑️ @{html.escape(username)} ({html.escape(time_label)}): Đã gửi thông báo khách này ở lượt trước.")
                        continue

                    clean_lines = [
                        line.strip() for line in raw_text.splitlines()
                        if len(line.strip()) >= 2 and line.strip() not in [username, "Translate", "Dịch"]
                    ]
                    post_text = " | ".join(clean_lines)[:450]
                    short_preview = html.escape(post_text[:85])
                    safe_user = html.escape(username)
                    permalink = f"https://www.threads.net/@{username}/post/{post_code}"

                    evaluation = await analyze_post_with_ai(post_text, username, label)

                    if (
                        evaluation.get("is_buyer_seeking_flowers")
                        and evaluation.get("location_verdict") in ["CONFIRMED_HCM", "LIKELY_HCM"]
                        and evaluation.get("confidence_score", 0) >= 0.60
                    ):
                        total_leads_found += 1
                        draft = evaluation.get("suggested_comment", "")
                        reason = evaluation.get("reasoning", "")
                        save_lead(post_code, username, post_text, permalink, draft, "NOTIFIED")
                        await send_telegram_alert(username, post_text, permalink, draft, label, reason, time_label)
                        fresh_logs.append(f"✅ @{safe_user} (<b>{html.escape(time_label)}</b>): \"{short_preview}...\"\n   ↳ <b>CHỐT KHÁCH:</b> {html.escape(reason)}")
                    else:
                        reason = evaluation.get("reasoning", "Không phải khách mua hoa")
                        save_lead(post_code, username, post_text, permalink, "", "IGNORED")
                        fresh_logs.append(f"❌ @{safe_user} (<b>{html.escape(time_label)}</b>): \"{short_preview}...\"\n   ↳ <i>Loại vì: {html.escape(reason)}</i>")

                    await asyncio.sleep(0.4)
            except Exception as e:
                print(f"[!] Lỗi khi quét '{label}': {e}")

        await browser.close()

    if EVENT_NAME == "workflow_dispatch":
        log_section = "\n\n".join(fresh_logs[:10]) if fresh_logs else "<i>Trong 30 phút vừa qua chưa có bài viết mới nào chứa bộ từ khóa trên.</i>"
        report_msg = (
            f"⏱️ <b>BÁO CÁO QUÉT BÀI MỚI (TỪ 1 - 30 PHÚT ĐỔ LẠI):</b>\n"
            f"• <b>Bài mới (1 - 30 phút):</b> {fresh_30m_count} bài\n"
            f"• <b>Bài cũ (> 30 phút) đã tự động ẩn:</b> {old_skipped_count} bài\n"
            f"• <b>Khách đặt hoa TP.HCM mới phát hiện:</b> {total_leads_found} khách\n\n"
            f"📋 <b>Danh sách bài mới đăng trong 30 phút qua:</b>\n\n"
            f"{log_section}"
        )
        await send_telegram_debug(report_msg[:3900])

if __name__ == "__main__":
    asyncio.run(run_single_scan_batch())
