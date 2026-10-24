import os
import re
import json
import sqlite3
import asyncio
import urllib.parse
import httpx
from datetime import datetime
from dotenv import load_dotenv
from pydantic import BaseModel
from openai import AsyncOpenAI
from playwright.async_api import async_playwright

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "manual")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

llm_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
)

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

def is_post_processed(post_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM leads WHERE post_id = ?", (post_id,))
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
# DANH SÁCH TỪ KHÓA QUÉT ĐẦY ĐỦ THEO YÊU CẦU CỦA BẠN
# =====================================================================
SEARCH_QUERIES = [
    "Hoa Tuoi Saigon",
    "cần đặt hoa",
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

# Từ khóa loại trừ ngay lập tức (Các tỉnh khác hoặc câu rao bán đặc trưng của các shop hoa)
NON_HCM_BLACKLIST = [
    "hà nội", "hanoi", "cầu giấy", "đống đa", "hoàn kiếm", "tây hồ", "thanh xuân",
    "hai bà trưng", "long biên", "hà đông", "đà nẵng", "hải phòng", "cần thơ",
    "nhận đặt hoa", "bên mình nhận", "tuyển thợ cắm hoa", "sỉ hoa", "tuyển nhân viên",
    "nhận ship", "freeship", "mình sẵn hoa", "em sẵn hoa", "nhà em sẵn", "shop em sẵn"
]

# Tín hiệu nhận diện khu vực Sài Gòn / TP.HCM và phương ngữ miền Nam
HCM_SIGNALS = [
    "hcm", "tphcm", "sài gòn", "saigon", "sg", "thủ đức", "thảo điền",
    "bình thạnh", "phú nhuận", "tân bình", "tân phú", "gò vấp", "bình tân",
    "quận 1", "quận 2", "quận 3", "quận 4", "quận 5", "quận 6", "quận 7",
    "quận 8", "quận 9", "quận 10", "quận 11", "quận 12", "q1", "q2", "q3",
    "q4", "q5", "q6", "q7", "q8", "q9", "q10", "q11", "q12",
    "nhà bè", "hóc môn", "bình chánh", "hồ thị kỷ", "sốp nào", "shop nào", "bông"
]

class LeadEvaluation(BaseModel):
    is_buyer_seeking_flowers: bool
    location_verdict: str
    confidence_score: float
    reasoning: str
    suggested_comment: str

async def analyze_post_with_ai(post_text: str, username: str, query_used: str) -> LeadEvaluation | None:
    text_lower = post_text.lower()
    if any(bad in text_lower for bad in NON_HCM_BLACKLIST):
        return None

    has_hcm_kw = (
        any(sig in text_lower for sig in HCM_SIGNALS)
        or ("saigon" in query_used.lower())
        or ("sài gòn" in query_used.lower())
        or ("hcm" in query_used.lower())
    )

    system_prompt = """
    Bạn là Trợ lý AI săn tìm khách hàng đặt hoa tại TP. Hồ Chí Minh trên mạng xã hội Threads.
    1. Trả về `is_buyer_seeking_flowers = True` CHỈ KHI người đăng là KHÁCH HÀNG đang có nhu cầu mua/đặt hoa: hỏi "shop nào/sốp nào sẵn hoa", "cần hoa gấp", tìm mua "hoa tốt nghiệp", "hoa cưới", "hoa ly", "hoa hồng", hỏi shop nào đang "sale hoa", hoặc xin gợi ý (rcm) tiệm hoa.
       - NẾU người đăng là CHỦ SHOP HOA đang đăng bài rao bán, xả kho, báo giá, khoe mẫu hoa của tiệm mình -> BẮT BUỘC trả về `is_buyer_seeking_flowers = False`.
       - NẾU chỉ là bài đăng tâm sự, thơ văn, khoe ảnh chụp chơi có chữ hoa hồng/hoa ly mà không có ý định tìm mua hoa -> Trả về `False`.
    2. Chọn 'CONFIRMED_HCM' hoặc 'LIKELY_HCM' nếu trong bài hoặc chủ đề (Topic) có chữ Saigon/Sài Gòn/HCM/Quận..., hoặc bài viết không nhắc tới tỉnh thành khác (như Hà Nội/Đà Nẵng) và dùng từ ngữ tự nhiên phù hợp tệp khách miền Nam/Sài Gòn ('sốp nào còn thức', 'sốp nào sẵn hoa', 'cần hoa gấp'...).
    3. Soạn `suggested_comment` ấm áp, nhiệt tình, tự nhiên kiểu Sài Gòn (dưới 250 ký tự) đúng theo loại hoa/nhu cầu khách đang hỏi (ví dụ: hoa gấp, hoa tốt nghiệp, hoa cưới, hoa hồng, hoa ly...), tuyệt đối không chèn link hay số điện thoại.
    """
    res = await llm_client.beta.chat.completions.parse(
        model="gemini-2.5-flash",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User: @{username} | Từ khóa tìm: {query_used} | Tín hiệu HCM: {has_hcm_kw}\nToàn bộ bài & Topic: {post_text}"}
        ],
        response_format=LeadEvaluation,
        temperature=0.3
    )
    return res.choices[0].message.parsed

async def send_telegram_alert(username: str, post_text: str, permalink: str, draft_comment: str, query_used: str):
    msg = (
        f"🌸 <b>PHÁT HIỆN KHÁCH TÌM HOA TẠI TP.HCM!</b>\n"
        f"🔎 <i>Từ khóa: {query_used}</i>\n\n"
        f"👤 <b>Khách hàng:</b> @{username}\n"
        f"📝 <b>Nội dung bài:</b>\n<i>\"{post_text}\"</i>\n\n"
        f"💬 <b>Câu trả lời AI soạn sẵn (Chạm nhẹ vào chữ dưới để Copy):</b>\n"
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
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "reply_markup": keyboard}
        )

async def send_telegram_debug(text: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        )

async def run_single_scan_batch():
    init_db()
    storage = "threads_session.json" if os.path.exists("threads_session.json") else None
    total_posts_seen = 0
    total_leads_found = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=storage,
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        for query in SEARCH_QUERIES:
            print(f"\n[*] Đang quét từ khóa: '{query}'")
            encoded_q = urllib.parse.quote(query)
            url = f"https://www.threads.net/search?q={encoded_q}&serp_type=default&filter=recent"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(4500)

                # Cuộn nhẹ trang để kích hoạt tải danh sách bài viết
                await page.mouse.wheel(0, 800)
                await page.wait_for_timeout(1500)

                links = await page.locator('a[href*="/post/"]').all()
                print(f" -> Tìm thấy {len(links)} thẻ link bài viết cho từ khóa '{query}'")
                seen_codes = set()

                for link in links:
                    href = await link.get_attribute("href")
                    if not href:
                        continue
                    match = re.search(r"/@([^/]+)/post/([A-Za-z0-9_-]+)", href)
                    if not match:
                        continue
                    username, post_code = match.group(1), match.group(2)
                    if post_code in seen_codes:
                        continue
                    seen_codes.add(post_code)
                    total_posts_seen += 1

                    if is_post_processed(post_code):
                        continue

                    container = link.locator("xpath=ancestor::div[contains(@data-pressable-container, 'true') or @role='article'][1]")
                    raw_text = await container.inner_text() if await container.count() > 0 else await link.locator("xpath=../../..").inner_text()

                    # Giữ lại cả các dòng ngắn như Topic tag ("Hoa Tuoi Saigon")
                    clean_lines = [
                        line.strip() for line in raw_text.splitlines()
                        if len(line.strip()) >= 3 and line.strip() not in [username, "Translate", "Dịch"]
                    ]
                    post_text = " | ".join(clean_lines)[:600]

                    if not post_text:
                        continue

                    print(f"   + Đang phân tích @{username}: {post_text[:80]}...")
                    permalink = f"https://www.threads.net/@{username}/post/{post_code}"
                    evaluation = await analyze_post_with_ai(post_text, username, query)

                    if (
                        evaluation
                        and evaluation.is_buyer_seeking_flowers
                        and evaluation.location_verdict in ["CONFIRMED_HCM", "LIKELY_HCM"]
                        and evaluation.confidence_score >= 0.65
                    ):
                        print(f"   🎯 [CHỐT KHÁCH HCM] @{username}!")
                        total_leads_found += 1
                        save_lead(post_code, username, post_text, permalink, evaluation.suggested_comment, "NOTIFIED")
                        await send_telegram_alert(username, post_text, permalink, evaluation.suggested_comment, query)
                    else:
                        reason = evaluation.reasoning if evaluation else "Nằm trong danh sách loại trừ (Shop bán hàng / Khác tỉnh)"
                        print(f"   - [Bỏ qua] @{username}: {reason}")
                        save_lead(post_code, username, post_text, permalink, "", "IGNORED")

                    await asyncio.sleep(1.5)
            except Exception as e:
                print(f"[!] Lỗi khi quét '{query}': {e}")

        await browser.close()

    # Gửi báo cáo tổng kết về Telegram khi bấm nút Run workflow bằng tay
    if EVENT_NAME == "workflow_dispatch":
        await send_telegram_debug(
            f"📊 <b>Báo cáo lượt chạy thử nghiệm từ GitHub Cloud:</b>\n"
            f"• Số từ khóa đã quét: <b>{len(SEARCH_QUERIES)} từ khóa</b>\n"
            f"• Tổng số bài viết đọc được trên Threads: <b>{total_posts_seen} bài</b>\n"
            f"• Số khách đặt hoa TP.HCM phát hiện được: <b>{total_leads_found} khách</b>"
        )

if __name__ == "__main__":
    asyncio.run(run_single_scan_batch())
