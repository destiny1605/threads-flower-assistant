import os
import re
import json
import sqlite3
import asyncio
import urllib.parse
import httpx
from datetime import datetime
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from openai import AsyncOpenAI
from playwright.async_api import async_playwright

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
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

SEARCH_QUERIES = [
    "đặt hoa sài gòn",
    "đặt hoa hcm",
    "rcm shop hoa",
    "recommend tiệm hoa",
    "tìm tiệm hoa tươi",
    "mua hoa sinh nhật hcm"
]

NON_HCM_BLACKLIST = [
    "hà nội", "hanoi", "cầu giấy", "đống đa", "hoàn kiếm", "tây hồ", "thanh xuân",
    "hai bà trưng", "long biên", "hà đông", "đà nẵng", "hải phòng", "cần thơ",
    "nhận đặt hoa", "bên mình nhận", "shop em có sẵn", "tuyển thợ cắm hoa", "sỉ hoa"
]

HCM_SIGNALS = [
    "hcm", "tphcm", "sài gòn", "saigon", "sg", "thủ đức", "thảo điền",
    "bình thạnh", "phú nhuận", "tân bình", "tân phú", "gò vấp", "bình tân",
    "quận 1", "quận 2", "quận 3", "quận 4", "quận 5", "quận 6", "quận 7",
    "quận 8", "quận 9", "quận 10", "quận 11", "quận 12", "q1", "q2", "q3",
    "q7", "q10", "nhà bè", "hóc môn", "bình chánh", "hồ thị kỷ"
]

class LeadEvaluation(BaseModel):
    is_buyer_seeking_flowers: bool
    location_verdict: str
    confidence_score: float
    reasoning: str
    suggested_comment: str

async def analyze_post_with_ai(post_text: str, username: str) -> LeadEvaluation | None:
    text_lower = post_text.lower()
    if any(bad in text_lower for bad in NON_HCM_BLACKLIST):
        return None

    has_hcm_kw = any(sig in text_lower for sig in HCM_SIGNALS)
    system_prompt = """
    Bạn là Trợ lý AI săn tìm khách hàng đặt hoa tại TP. Hồ Chí Minh trên mạng xã hội Threads.
    1. Chỉ trả về `is_buyer_seeking_flowers = True` nếu người đăng đang CẦN MUA HOA, hỏi chỗ đặt hoa, xin gợi ý (rcm) shop hoa. Nếu là shop hoa bán hàng -> False.
    2. Chỉ chọn 'CONFIRMED_HCM' hoặc 'LIKELY_HCM' nếu có dấu hiệu ở Sài Gòn / TP.HCM. Nếu ở Hà Nội/Đà Nẵng -> 'OUTSIDE_OR_UNKNOWN'.
    3. Soạn `suggested_comment` ấm áp, lịch sự kiểu Sài Gòn dưới 260 ký tự, không chèn link hay số điện thoại.
    """
    res = await llm_client.beta.chat.completions.parse(
        model="gemini-2.5-flash",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User: @{username} | Có từ khóa HCM: {has_hcm_kw}\nBài viết: {post_text}"}
        ],
        response_format=LeadEvaluation,
        temperature=0.3
    )
    return res.choices[0].message.parsed

async def send_telegram_alert(username: str, post_text: str, permalink: str, draft_comment: str):
    msg = (
        f"🌸 <b>PHÁT HIỆN KHÁCH TÌM ĐẶT HOA TẠI TP.HCM!</b>\n\n"
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

async def run_single_scan_batch():
    init_db()
    storage = "threads_session.json" if os.path.exists("threads_session.json") else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=storage,
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        for query in SEARCH_QUERIES:
            print(f"[*] Đang quét từ khóa: {query}")
            encoded_q = urllib.parse.quote(query)
            url = f"https://www.threads.com/search?q={encoded_q}&serp_type=default&filter=recent"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)
                links = await page.locator('a[href*="/post/"]').all()
                seen_codes = set()

                for link in links:
                    href = await link.get_attribute("href")
                    if not href:
                        continue
                    match = re.search(r"/@([^/]+)/post/([A-Za-z0-9_-]+)", href)
                    if not match:
                        continue
                    username, post_code = match.group(1), match.group(2)
                    if post_code in seen_codes or is_post_processed(post_code):
                        continue
                    seen_codes.add(post_code)

                    container = link.locator("xpath=ancestor::div[contains(@data-pressable-container, 'true') or @role='article'][1]")
                    raw_text = await container.inner_text() if await container.count() > 0 else await link.locator("xpath=../../..").inner_text()
                    clean_lines = [line.strip() for line in raw_text.splitlines() if len(line.strip()) > 15 and line.strip() != username]
                    post_text = " ".join(clean_lines)[:600]

                    if not post_text:
                        continue

                    permalink = f"https://www.threads.com/@{username}/post/{post_code}"
                    evaluation = await analyze_post_with_ai(post_text, username)
                    if (
                        evaluation
                        and evaluation.is_buyer_seeking_flowers
                        and evaluation.location_verdict in ["CONFIRMED_HCM", "LIKELY_HCM"]
                        and evaluation.confidence_score >= 0.70
                    ):
                        print(f" 🎯 [KHÁCH HCM] @{username}: {post_text[:50]}...")
                        save_lead(post_code, username, post_text, permalink, evaluation.suggested_comment, "NOTIFIED")
                        await send_telegram_alert(username, post_text, permalink, evaluation.suggested_comment)
                    else:
                        save_lead(post_code, username, post_text, permalink, "", "IGNORED")

                    await asyncio.sleep(3)
            except Exception as e:
                print(f"[!] Lỗi khi quét '{query}': {e}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_single_scan_batch())
