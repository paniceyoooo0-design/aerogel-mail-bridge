#!/usr/bin/env python3
"""
claude-mail-bridge - 给你的 AI 一个邮箱
Lightweight email bridge: IMAP poll → API call → SMTP reply.
No MCP, no framework, just Python stdlib + requests.
"""

import json
import time
import imaplib
import smtplib
import email
import logging
import sys
import signal
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from email.utils import formataddr
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("需要 requests 库: pip install requests")
    sys.exit(1)

# ── Load Config ────────────────────────────────────────────────

def load_config(path: str = "config.json") -> dict:
    p = Path(path)
    if not p.exists():
        print(f"找不到 {path}，请复制 config.example.json 为 config.json 并填写配置")
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = load_config()
EMAIL_CFG = CONFIG["email"]
API_CFG = CONFIG["api"]
BRIDGE_CFG = CONFIG.get("bridge", {})

# ── Logging ────────────────────────────────────────────────────

log_file = BRIDGE_CFG.get("log_file", "bridge.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
log = logging.getLogger("bridge")

# ── Rate Limiting ──────────────────────────────────────────────

from collections import defaultdict

seen_uids: set = set()
running = True

# {sender: [timestamp, timestamp, ...]}
_sender_history: dict[str, list[float]] = defaultdict(list)
_daily_count = 0
_daily_reset: float = 0.0


def _check_rate_limit(sender: str) -> str | None:
    """Returns reason string if blocked, None if OK."""
    global _daily_count, _daily_reset

    now = time.time()
    daily_limit = BRIDGE_CFG.get("daily_limit", 50)
    sender_limit = BRIDGE_CFG.get("sender_hourly_limit", 5)

    # reset daily counter
    if now - _daily_reset > 86400:
        _daily_count = 0
        _daily_reset = now

    if _daily_count >= daily_limit:
        return f"今日已达 {daily_limit} 次上限"

    cutoff = now - 3600
    _sender_history[sender] = [t for t in _sender_history[sender] if t > cutoff]
    if len(_sender_history[sender]) >= sender_limit:
        return f"{sender} 一小时内已发 {sender_limit} 封"

    return None


def _record_call(sender: str):
    global _daily_count
    _daily_count += 1
    _sender_history[sender].append(time.time())

def graceful_exit(sig, frame):
    global running
    log.info("收到退出信号，正在停止...")
    running = False

signal.signal(signal.SIGINT, graceful_exit)
signal.signal(signal.SIGTERM, graceful_exit)


def decode_header_value(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def get_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def extract_email_addr(from_header: str) -> str:
    """Extract bare email from 'Name <addr>' format."""
    if "<" in from_header and ">" in from_header:
        return from_header.split("<")[1].split(">")[0].strip()
    return from_header.strip()


# ── IMAP ───────────────────────────────────────────────────────

def connect_imap() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(EMAIL_CFG["imap_host"], EMAIL_CFG.get("imap_port", 993))
    conn.login(EMAIL_CFG["address"], EMAIL_CFG["password"])
    return conn


def fetch_unseen() -> list[dict]:
    """Fetch all unseen emails, return list of {uid, from, subject, body}."""
    try:
        conn = connect_imap()
        conn.select("INBOX")
        status, data = conn.uid("search", None, "UNSEEN")
        if status != "OK" or not data[0]:
            conn.logout()
            return []

        uids = data[0].split()
        allowed = BRIDGE_CFG.get("allowed_senders", [])
        max_chars = BRIDGE_CFG.get("max_body_chars", 3000)
        results = []

        for uid in uids:
            uid_str = uid.decode()
            if uid_str in seen_uids:
                continue

            status, msg_data = conn.uid("fetch", uid, "(BODY[])")
            if status != "OK":
                continue

            raw = None
            for item in msg_data:
                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                    raw = item[1]
                    break
            if not raw:
                continue

            msg = email.message_from_bytes(raw)
            from_header = decode_header_value(msg.get("From", ""))
            from_addr = extract_email_addr(from_header)

            # allowlist check
            if allowed and from_addr not in allowed:
                log.info(f"跳过未授权发件人: {from_addr}")
                seen_uids.add(uid_str)
                # mark as seen
                conn.uid("store", uid, "+FLAGS", "\\Seen")
                continue

            body = get_body(msg)[:max_chars]
            subject = decode_header_value(msg.get("Subject", ""))

            results.append({
                "uid": uid_str,
                "from": from_header,
                "from_addr": from_addr,
                "subject": subject,
                "body": body,
            })
            seen_uids.add(uid_str)

        conn.logout()
        return results
    except Exception as e:
        log.error(f"IMAP 错误: {e}")
        return []


# ── API Call ───────────────────────────────────────────────────

def call_api(sender: str, subject: str, body: str) -> str:
    """Call OpenAI-compatible API and return response text."""
    url = API_CFG["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_CFG['api_key']}",
    }

    user_content = f"来自: {sender}\n主题: {subject}\n\n{body}"

    payload = {
        "model": API_CFG["model"],
        "messages": [
            {"role": "system", "content": API_CFG.get("system_prompt", "你是一个友善的AI助手。")},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": API_CFG.get("max_tokens", 1000),
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"API 错误: {e}")
        return ""


# ── SMTP ───────────────────────────────────────────────────────

def send_reply(to_addr: str, subject: str, body: str):
    """Send reply via SMTP."""
    try:
        msg = MIMEMultipart()
        display_name = EMAIL_CFG.get("display_name", "Claude")
        msg["From"] = formataddr((display_name, EMAIL_CFG["address"]))
        msg["To"] = to_addr
        msg["Subject"] = f"Re: {subject}" if not subject.startswith("Re:") else subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(EMAIL_CFG["smtp_host"], EMAIL_CFG.get("smtp_port", 587)) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_CFG["address"], EMAIL_CFG["password"])
            server.sendmail(EMAIL_CFG["address"], [to_addr], msg.as_string())

        # save to Sent
        try:
            imap = imaplib.IMAP4_SSL(EMAIL_CFG["imap_host"])
            imap.login(EMAIL_CFG["address"], EMAIL_CFG["password"])
            imap.select('"Sent Messages"')
            imap.append(
                '"Sent Messages"',
                "\\Seen",
                imaplib.Time2Internaldate(datetime.now(timezone.utc)),
                msg.as_bytes(),
            )
            imap.logout()
        except Exception:
            pass  # sent folder save is best-effort

        log.info(f"已回复 {to_addr}: {subject}")
    except Exception as e:
        log.error(f"SMTP 错误: {e}")


# ── Main Loop ──────────────────────────────────────────────────

def main():
    interval = BRIDGE_CFG.get("poll_interval", 10)
    log.info(f"🚀 claude-mail-bridge 启动")
    log.info(f"   邮箱: {EMAIL_CFG['address']}")
    log.info(f"   模型: {API_CFG['model']}")
    log.info(f"   轮询间隔: {interval}s")

    allowed = BRIDGE_CFG.get("allowed_senders", [])
    if allowed:
        log.info(f"   白名单: {allowed}")
    else:
        log.info(f"   白名单: 无限制（所有人可发）")

    # init: mark all current emails as seen so we don't reply to old mail
    try:
        conn = connect_imap()
        conn.select("INBOX")
        status, data = conn.uid("search", None, "ALL")
        if status == "OK" and data[0]:
            for uid in data[0].split():
                seen_uids.add(uid.decode())
        conn.logout()
        log.info(f"   已标记 {len(seen_uids)} 封历史邮件")
    except Exception as e:
        log.warning(f"初始化标记失败: {e}")

    while running:
        emails = fetch_unseen()
        for mail in emails:
            log.info(f"📨 收到邮件: {mail['from']} - {mail['subject']}")
            blocked = _check_rate_limit(mail["from_addr"])
            if blocked:
                log.warning(f"⚠️ 限流: {blocked}，跳过")
                continue
            reply = call_api(mail["from"], mail["subject"], mail["body"])
            if reply:
                _record_call(mail["from_addr"])
                send_reply(mail["from_addr"], mail["subject"], reply)
            else:
                log.warning(f"API 无回复，跳过")
        time.sleep(interval)

    log.info("👋 bridge 已停止")


if __name__ == "__main__":
    main()
