import os
import re
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
# DANH SÁCH TỪ KHÓA QUÉT
# =====================================================================
SEARCH_QUERIES = [
    "Hoa Tuoi Saigon",
    "cần đặt hoa",
    "đặt hoa gấp",
    "đặt hoa sài gòn",
    "đặt hoa hcm",
    "shop nào sẵn hoa",
    "sốp nào sẵn hoa",
    "cần hoa gấp",
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
    "hai bà trưng", "long biên", "hà đông", "đà nẵng", "hải phòng", "cần thơ",
    "nhận đặt hoa", "bên mình nhận", "tuyển thợ cắm hoa", "sỉ hoa", "tuyển nhân viên",
    "mình sẵn hoa", "em sẵn hoa", "nhà em sẵn", "shop em sẵn", "sẵn sale"
]

HCM_SIGNALS = [
    "hcm", "tphcm", "sài gòn", "saigon", "sg", "thủ đức", "thảo điền",
    "bình thạnh", "phú nhuận", "tân bình", "tân phú", "gò vấp", "bình tân",
    "quận 1", "quận 2", "quận 3", "quận 4", "quận 5", "quận 6", "quận 7",
    "quận 8", "quận 9", "quận 10", "quận 11", "quận 12", "q1", "q2", "q3",
    "q4", "q5", "q6", "q7", "q8", "q9", "q10", "q11", "q12",
    "nhà bè", "hóc môn", "bình chánh", "hồ thị kỷ", "sốp nào", "shop nào", "bông"
]

def check_post_freshness(raw_text: str) -> tuple[bool, str]:
    """
    Kiểm tra thời gian đăng bài:
    - Chấp nhận bài mới đăng tính bằng giây (s/giây), phút (m/phút), hoặc tối đa 6 giờ (h/giờ) gần nhất.
    - Loại bỏ ngay các bài từ '1 ngày', '2 ngày', tuần trước, hoặc có ngày tháng cũ (dd/mm/yyyy).
    """
    lines = [l.strip().lower() for l in raw_text.splitlines() if l.strip()]
    for line in lines[:5]:
        # Loại bài có định dạng ngày tháng cũ như 14/10/2026, 01/10/2026
        if re.search(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", line):
            return False, f"Bài cũ ({line})"
        # Loại bài tính bằng ngày / tuần / tháng
        if re.search(r"^\d+\s*(ngày|d|tuần|w|tháng|năm)$", line):
            return False, f"Bài cũ ({line})"
        # Kiểm tra bài tính bằng giờ (chỉ lấy <= 6 giờ gần nhất)
        m_hour = re.match(r"^(\d+)\s*(giờ|h)$", line)
        if m_hour:
            hours = int(m_hour.group(1))
            if hours > 6:
                return False, f"Bài từ {hours} giờ trước (chỉ lấy <= 6 giờ)"
            return True, f"{hours} giờ trước"
        # Bài vừa đăng tính bằng giây / phút -> Rất mới!
        m_min = re.match(r"^(\d+)\s*(phút|m|giây|s)$", line)
        if m_min or line in ["vừa xong", "now", "just now"]:
            return True, f"Mới đăng ({line})"
    return True, "Mới"

async def analyze_post_with_ai(post_text: str, username: str, query_used: str) -> dict:
    text_lower = post_text.lower()
    for bad in NON_HCM_BLACKLIST:
        if bad in text_lower:
            return {
                "is_buyer_seeking_flowers": False,
                "location_verdict": "BLACKLISTED",
                "confidence_score": 0.0,
                "reasoning": f"Chứa từ khóa loại trừ (Shop/Khác tỉnh): '{bad}'",
                "suggested_comment": ""
            }

    has_hcm_kw = (
        any(sig in text_lower for sig in HCM_SIGNALS)
        or ("saigon" in query_used.lower())
        or ("sài gòn" in query_used.lower())
        or ("hcm" in query_used.lower())
    )

    prompt = f"""
Bạn là Trợ lý AI săn tìm khách hàng đặt hoa tại TP. Hồ Chí Minh trên mạng xã hội Threads.
Hãy phân tích bài đăng dưới đây và trả về đúng định dạng JSON (không bọc trong markdown):

Thông tin bài đăng:
- Người đăng: @{username}
- Từ khóa tìm thấy: {query_used}
- Có tín hiệu Sài Gòn/HCM: {has_hcm_kw}
- Nội dung: {post_text}

Quy tắc chấm điểm:
1. "is_buyer_seeking_flowers": true NẾU người đăng là KHÁCH HÀNG đang hỏi mua hoa, cần đặt hoa gấp, hỏi sốp/shop nào còn thức, shop nào sẵn hoa, tìm chỗ làm hoa mẫu, hỏi ai sale hoa tươi, tìm mua hoa tốt nghiệp/hoa cưới/hoa hồng/hoa ly.
   "is_buyer_seeking_flowers": false NẾU người đăng là CHỦ SHOP HOA đang rao bán, báo giá (ví dụ '100k', '159k', 'inbox cho tiệm'), xả hoa của tiệm mình, hoặc bài đăng không liên quan mua hoa.
2. "location_verdict": "CONFIRMED_HCM" hoặc "LIKELY_HCM" nếu bài viết/chủ đề có chữ Saigon/Sài Gòn/HCM/Gò Vấp/Quận..., HOẶC bài viết không nhắc tới tỉnh khác và mang văn phong tìm hoa tại Sài Gòn ('sốp nào còn thức', 'cần đặt hoa gấp'...). Ngược lại là "OUTSIDE".
3. "confidence_score": số từ 0.0 đến 1.0.
4. "reasoning": giải thích ngắn gọn 1 câu bằng tiếng Việt vì sao chọn hoặc loại.
5. "suggested_comment": câu bình luận phản hồi ấm áp, nhiệt tình kiểu Sài Gòn (dưới 240 ký tự), đúng nhu cầu của khách, không kèm link hay số điện thoại.

Trả về JSON mẫu:
{{"is_buyer_seeking_flowers": true, "location_verdict": "CONFIRMED_HCM", "confidence_score": 0.95, "reasoning": "Khách tìm đặt hoa gấp sáng mai tại Sài Gòn", "suggested_comment": "Dạ tụi mình vẫn còn thức nhận đơn hoa gấp cho sáng mai ở Sài Gòn nè bạn ơi, bạn nhắn tụi mình mẫu hoa bạn thích nha!"}}
"""

    # Gọi trực tiếp REST API chuẩn của Gemini (tương thích cả AI Studio Key và Vertex Express Key AQ.A...)
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={OPENAI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json"
        }
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(api_url, json=payload)
            if resp.status_code != 200:
                # Dự phòng tự động chấm bằng Bộ quy tắc thông minh nếu API Key bị giới hạn
                return fallback_rule_evaluation(post_text, query_used, has_hcm_kw)
            data = resp.json()
            raw_json = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw_json)
    except Exception:
        return fallback_rule_evaluation(post_text, query_used, has_hcm_kw)

def fallback_rule_evaluation(post_text: str, query_used: str, has_hcm_kw: bool) -> dict:
    """Bộ lọc thông minh dự phòng: Không bao giờ bị 'Lỗi phân tích' ngay cả khi mạng/API chập chờn."""
    t = post_text.lower()
    buyer_patterns = [
        "cần đặt hoa", "cần hoa gấp", "sốp nào", "shop nào", "tiệm nào",
        "có ai", "cho mình hỏi", "cho em hỏi", "tìm shop", "tìm tiệm",
        "rcm", "recommend", "còn thức không", "chỗ nào bán", "mua ở đâu"
    ]
    seller_patterns = ["100k", "150k", "159k", "200k", "ib ngay", "nhắn sốp nha", "sẵn ạ", "freeship"]

    if any(sp in t for sp in seller_patterns):
        return {
            "is_buyer_seeking_flowers": False,
            "location_verdict": "SELLER",
            "confidence_score": 0.0,
            "reasoning": "Bài đăng bán hàng của shop hoa",
            "suggested_comment": ""
        }

    if any(bp in t for bp in buyer_patterns):
        return {
            "is_buyer_seeking_flowers": True,
            "location_verdict": "CONFIRMED_HCM" if has_hcm_kw else "LIKELY_HCM",
            "confidence_score": 0.85,
            "reasoning": "Khách đang hỏi tìm nơi đặt hoa (Phát hiện qua từ khóa câu hỏi)",
            "suggested_comment": "Dạ tiệm tụi mình ở Sài Gòn vẫn đang nhận đơn và có sẵn nhiều mẫu hoa tươi xinh lắm ạ. Bạn ghé trang tụi mình hoặc nhắn tụi mình tư vấn liền nha! 🌸"
        }

    return {
        "is_buyer_seeking_flowers": False,
        "location_verdict": "UNKNOWN",
        "confidence_score": 0.2,
        "reasoning": "Không có câu hỏi tìm mua hoa rõ ràng",
        "suggested_comment": ""
    }

async def send_telegram_alert(username: str, post_text: str, permalink: str, draft_comment: str, query_used: str, reasoning: str, time_label: str):
    msg = (
        f"🌸 <b>PHÁT HIỆN KHÁCH ĐẶT HOA MỚI ({time_label})!</b>\n"
        f"🔎 <b>Từ khóa:</b> <code>{query_used}</code>\n"
        f"👤 <b>Khách hàng:</b> <a href='{permalink}'>@{username}</a>\n\n"
        f"📝 <b>Nội dung bài đăng:</b>\n<i>\"{post_text}\"</i>\n\n"
        f"🤖 <b>Đánh giá:</b> {reasoning}\n\n"
        f"💬 <b>Câu trả lời soạn sẵn (Chạm nhẹ vào chữ dưới để Copy):</b>\n"
        f"<code>{draft_comment}</code>"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "📱 Mở bài này trong App Threads để dán Comment", "url": permalink}]
        ]
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": keyboard
            }
        )

async def send_telegram_debug(text: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
        )

async def run_single_scan_batch():
    init_db()
    storage = "threads_session.json" if os.path.exists("threads_session.json") else None
    total_posts_seen = 0
    fresh_posts_count = 0
    total_leads_found = 0
    detailed_logs = []
    global_seen_codes = set()
    is_logged_in = False

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

        # Kiểm tra xem phiên đăng nhập trên threads.com đã có hiệu lực thực sự chưa
        try:
            await page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(3000)
            login_links = await page.locator('a[href*="/login"]').count()
            is_logged_in = (login_links == 0)
            print(f"[*] Trạng thái đăng nhập Threads.com: {'ĐÃ ĐĂNG NHẬP ✅' if is_logged_in else 'CHƯA ĐĂNG NHẬP ⚠️ (Cần lấy cookie trên threads.com)'}")
        except Exception as e:
            print(f"[*] Kiểm tra trang chủ: {e}")

        for query in SEARCH_QUERIES:
            print(f"\n[*] Đang quét từ khóa: '{query}'")
            encoded_q = urllib.parse.quote(query)
            # filter=recent ép Threads hiển thị bài mới đăng theo thời gian thực
            url = f"https://www.threads.com/search?q={encoded_q}&serp_type=default&filter=recent"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3500)

                # Bấm trực tiếp vào tab "Gần đây" / "Recent" trên giao diện nếu có
                recent_tab = page.locator('div[role="tab"]:has-text("Gần đây"), div[role="tab"]:has-text("Recent"), a[href*="filter=recent"]')
                if await recent_tab.count() > 0:
                    try:
                        await recent_tab.first.click(timeout=2000)
                        await page.wait_for_timeout(2500)
                    except Exception:
                        pass

                await page.mouse.wheel(0, 800)
                await page.wait_for_timeout(1500)

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

                    if is_post_notified(post_code):
                        continue

                    container = link.locator("xpath=ancestor::div[contains(@data-pressable-container, 'true') or @role='article'][1]")
                    raw_text = await container.inner_text() if await container.count() > 0 else await link.locator("xpath=../../..").inner_text()

                    # BỘ LỌC THỜI GIAN: Loại bỏ ngay bài từ nhiều ngày trước hoặc > 6 giờ trước
                    is_fresh, time_label = check_post_freshness(raw_text)
                    clean_lines = [
                        line.strip() for line in raw_text.splitlines()
                        if len(line.strip()) >= 2 and line.strip() not in [username, "Translate", "Dịch"]
                    ]
                    post_text = " | ".join(clean_lines)[:500]
                    short_preview = post_text.replace("<", "").replace(">", "")[:85]
                    permalink = f"https://www.threads.com/@{username}/post/{post_code}"

                    if not is_fresh:
                        if len(detailed_logs) < 10:
                            detailed_logs.append(f"⏳ <a href='{permalink}'>@{username}</a>: \"{short_preview}...\"\n   ↳ <i>Bỏ qua vì: {time_label}</i>")
                        continue

                    fresh_posts_count += 1
                    evaluation = await analyze_post_with_ai(post_text, username, query)

                    if (
                        evaluation.get("is_buyer_seeking_flowers")
                        and evaluation.get("location_verdict") in ["CONFIRMED_HCM", "LIKELY_HCM"]
                        and evaluation.get("confidence_score", 0) >= 0.60
                    ):
                        total_leads_found += 1
                        draft = evaluation.get("suggested_comment", "")
                        reason = evaluation.get("reasoning", "")
                        save_lead(post_code, username, post_text, permalink, draft, "NOTIFIED")
                        await send_telegram_alert(username, post_text, permalink, draft, query, reason, time_label)
                        detailed_logs.append(f"✅ <a href='{permalink}'>@{username}</a> (<b>{time_label}</b> - <i>{query}</i>): \"{short_preview}...\"\n   ↳ <b>CHỐT KHÁCH:</b> {reason}")
                    else:
                        reason = evaluation.get("reasoning", "Không phải khách mua hoa")
                        save_lead(post_code, username, post_text, permalink, "", "IGNORED")
                        if len(detailed_logs) < 12:
                            detailed_logs.append(f"❌ <a href='{permalink}'>@{username}</a> (<b>{time_label}</b>): \"{short_preview}...\"\n   ↳ <i>Loại vì: {reason}</i>")

                    await asyncio.sleep(0.8)
            except Exception as e:
                print(f"[!] Lỗi khi quét '{query}': {e}")

        await browser.close()

    if EVENT_NAME == "workflow_dispatch":
        auth_status = "✅ Đã đăng nhập (Mở khóa bài mới nhất)" if is_logged_in else "⚠️ Chưa nhận phiên đăng nhập trên threads.com (Đang chỉ thấy bài nổi tiếng cũ)"
        log_section = "\n\n".join(detailed_logs[:10]) if detailed_logs else "<i>Không có bài viết nào.</i>"
        report_msg = (
            f"📊 <b>BÁO CÁO QUÉT BÀI MỚI NHẤT (<= 6 GIỜ):</b>\n"
            f"• <b>Trạng thái Cookie Threads:</b> {auth_status}\n"
            f"• <b>Tổng bài lướt qua:</b> {total_posts_seen} bài (Trong đó <b>{fresh_posts_count} bài mới đăng</b>)\n"
            f"• <b>Khách đặt hoa TP.HCM chốt được:</b> {total_leads_found} khách\n\n"
            f"📋 <b>Chi tiết xử lý:</b>\n\n"
            f"{log_section}"
        )
        await send_telegram_debug(report_msg[:4000])

if __name__ == "__main__":
    asyncio.run(run_single_scan_batch())
