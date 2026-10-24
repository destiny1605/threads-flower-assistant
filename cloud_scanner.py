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

def is_post_notified(post_id: str) -> bool:
    """Chỉ bỏ qua những bài ĐÃ gửi thông báo khách hàng để tránh báo trùng."""
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

NON_HCM_BLACKLIST = [
    "hà nội", "hanoi", "cầu giấy", "đống đa", "hoàn kiếm", "tây hồ", "thanh xuân",
    "hai bà trưng", "long biên", "hà đông", "đà nẵng", "hải phòng", "cần thơ",
    "nhận đặt hoa", "bên mình nhận", "tuyển thợ cắm hoa", "sỉ hoa", "tuyển nhân viên",
    "mình sẵn hoa", "em sẵn hoa", "nhà em sẵn", "shop em sẵn"
]

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
    for bad in NON_HCM_BLACKLIST:
        if bad in text_lower:
            return LeadEvaluation(
                is_buyer_seeking_flowers=False,
                location_verdict="BLACKLISTED",
                confidence_score=0.0,
                reasoning=f"Chứa từ khóa loại trừ: '{bad}'",
                suggested_comment=""
            )

    has_hcm_kw = (
        any(sig in text_lower for sig in HCM_SIGNALS)
        or ("saigon" in query_used.lower())
        or ("sài gòn" in query_used.lower())
        or ("hcm" in query_used.lower())
    )

    system_prompt = """
    Bạn là Trợ lý AI săn tìm khách hàng đặt hoa tại TP. Hồ Chí Minh trên mạng xã hội Threads.
    1. Trả về `is_buyer_seeking_flowers = True` KHI người đăng là KHÁCH HÀNG đang tìm mua/đặt hoa: hỏi "shop nào/sốp nào sẵn hoa", "sốp nào còn thức", "cần đặt hoa", "cần hoa gấp", tìm mua "hoa tốt nghiệp", "hoa cưới", "hoa ly", "hoa hồng", hỏi chỗ "sale hoa", hoặc xin gợi ý (rcm) tiệm hoa.
       - Nếu là CHỦ SHOP HOA đăng bài rao bán, báo giá, xả hoa của tiệm mình -> Trả về `False`.
    2. Chọn 'CONFIRMED_HCM' hoặc 'LIKELY_HCM' nếu trong bài hoặc chủ đề (Topic) có chữ Saigon/Sài Gòn/HCM/Quận..., HOẶC bài viết không ghi tỉnh thành khác (như Hà Nội/Đà Nẵng) và dùng từ ngữ hỏi mua hoa tự nhiên ('sốp nào còn thức', 'sốp nào sẵn hoa', 'cần hoa gấp'...).
    3. Soạn `suggested_comment` ấm áp, nhiệt tình kiểu Sài Gòn (dưới 250 ký tự) đúng theo nhu cầu khách hỏi, không chèn link hay số điện thoại.
    """
    try:
        res = await llm_client.beta.chat.completions.parse(
            model="gemini-2.5-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"User: @{username} | Từ khóa tìm: {query_used} | Tín hiệu HCM: {has_hcm_kw}\nToàn bộ bài & Topic: {post_text}"}
            ],
            response_format=LeadEvaluation,
            temperature=0.2
        )
        return res.choices[0].message.parsed
    except Exception as e:
        print(f"   [!] Lỗi gọi AI Gemini: {e}")
        return None

async def send_telegram_alert(username: str, post_text: str, permalink: str, draft_comment: str, query_used: str, reasoning: str):
    msg = (
        f"🌸 <b>PHÁT HIỆN KHÁCH ĐẶT HOA TẠI TP.HCM!</b>\n"
        f"🔎 <b>Từ khóa bắt trúng:</b> <code>{query_used}</code>\n"
        f"👤 <b>Khách hàng:</b> <a href='{permalink}'>@{username}</a>\n\n"
        f"📝 <b>Nội dung bài đăng:</b>\n<i>\"{post_text}\"</i>\n\n"
        f"🤖 <b>AI đánh giá:</b> {reasoning}\n\n"
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

def prepare_threads_cookies():
    """Sao chép cookie đăng nhập từ .instagram.com sang .threads.com và .threads.net để mở khóa tìm kiếm bài mới nhất."""
    if not os.path.exists("threads_session.json"):
        return None
    try:
        with open("threads_session.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        extra_cookies = []
        for c in cookies:
            domain = c.get("domain", "")
            if "instagram.com" in domain:
                c_net = dict(c)
                c_net["domain"] = domain.replace("instagram.com", "threads.net")
                extra_cookies.append(c_net)
                c_com = dict(c)
                c_com["domain"] = domain.replace("instagram.com", "threads.com")
                extra_cookies.append(c_com)
        data["cookies"].extend(extra_cookies)
        with open("threads_session_merged.json", "w", encoding="utf-8") as f:
            json.dump(data, f)
        return "threads_session_merged.json"
    except Exception as e:
        print(f"[!] Lỗi gộp cookie: {e}")
        return "threads_session.json"

async def run_single_scan_batch():
    init_db()
    storage = prepare_threads_cookies()
    total_posts_seen = 0
    total_leads_found = 0
    detailed_logs = []
    global_seen_codes = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            storage_state=storage,
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Bước khởi động: Kiểm tra đăng nhập SSO từ Instagram sang Threads
        try:
            await page.goto("https://www.threads.com/login", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)
            sso_btn = page.locator("text=/Continue with Instagram|Tiếp tục bằng Instagram/i")
            if await sso_btn.count() > 0:
                print("[*] Đang bấm nút Tiếp tục bằng Instagram để đồng bộ phiên...")
                await sso_btn.first.click()
                await page.wait_for_timeout(5000)
        except Exception as e:
            print(f"[*] Bỏ qua bước SSO: {e}")

        for query in SEARCH_QUERIES:
            print(f"\n[*] Đang quét từ khóa: '{query}'")
            encoded_q = urllib.parse.quote(query)
            url = f"https://www.threads.com/search?q={encoded_q}&serp_type=default&filter=recent"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(4000)

                # Nếu có tab "Recent" / "Gần đây" trên giao diện thì bấm vào để lấy bài vừa đăng
                recent_tab = page.locator('a[href*="filter=recent"], div[role="tab"]:has-text("Recent"), div[role="tab"]:has-text("Gần đây")')
                if await recent_tab.count() > 0:
                    try:
                        await recent_tab.first.click(timeout=2000)
                        await page.wait_for_timeout(2500)
                    except Exception:
                        pass

                await page.mouse.wheel(0, 1000)
                await page.wait_for_timeout(1500)

                links = await page.locator('a[href*="/post/"]').all()
                print(f" -> Tìm thấy {len(links)} thẻ link bài viết cho từ khóa '{query}'")

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

                    clean_lines = [
                        line.strip() for line in raw_text.splitlines()
                        if len(line.strip()) >= 3 and line.strip() not in [username, "Translate", "Dịch"]
                    ]
                    post_text = " | ".join(clean_lines)[:500]

                    if not post_text:
                        continue

                    permalink = f"https://www.threads.com/@{username}/post/{post_code}"
                    evaluation = await analyze_post_with_ai(post_text, username, query)

                    short_preview = post_text.replace("<", "").replace(">", "")[:90]

                    if (
                        evaluation
                        and evaluation.is_buyer_seeking_flowers
                        and evaluation.location_verdict in ["CONFIRMED_HCM", "LIKELY_HCM"]
                        and evaluation.confidence_score >= 0.60
                    ):
                        total_leads_found += 1
                        save_lead(post_code, username, post_text, permalink, evaluation.suggested_comment, "NOTIFIED")
                        await send_telegram_alert(username, post_text, permalink, evaluation.suggested_comment, query, evaluation.reasoning)
                        detailed_logs.append(f"✅ <a href='{permalink}'>@{username}</a> (<i>{query}</i>): \"{short_preview}...\"\n   ↳ <b>ĐẠT:</b> {evaluation.reasoning}")
                    else:
                        reason = evaluation.reasoning if evaluation else "Lỗi phân tích"
                        save_lead(post_code, username, post_text, permalink, "", "IGNORED")
                        if len(detailed_logs) < 12:
                            detailed_logs.append(f"❌ <a href='{permalink}'>@{username}</a> (<i>{query}</i>): \"{short_preview}...\"\n   ↳ <i>Loại vì: {reason}</i>")

                    await asyncio.sleep(1.2)
            except Exception as e:
                print(f"[!] Lỗi khi quét '{query}': {e}")

        await browser.close()

    # Luôn gửi báo cáo chi tiết kèm danh sách từng bài đọc được khi bạn bấm chạy thử
    if EVENT_NAME == "workflow_dispatch":
        log_section = "\n\n".join(detailed_logs[:10]) if detailed_logs else "<i>Không trích xuất được bài viết nào (Cần kiểm tra lại phiên đăng nhập Threads).</i>"
        report_msg = (
            f"📊 <b>BÁO CÁO CHI TIẾT LƯỢT QUÉT ĐÁM MÂY:</b>\n"
            f"• <b>Số từ khóa đã quét:</b> {len(SEARCH_QUERIES)} từ khóa\n"
            f"• <b>Tổng số bài viết đọc được:</b> {total_posts_seen} bài\n"
            f"• <b>Số khách chốt được:</b> {total_leads_found} khách\n\n"
            f"📋 <b>Chi tiết các bài viết vừa đọc qua (Tối đa 10 bài đầu):</b>\n\n"
            f"{log_section}"
        )
        await send_telegram_debug(report_msg[:4000])

if __name__ == "__main__":
    asyncio.run(run_single_scan_batch())
