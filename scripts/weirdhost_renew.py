#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# scripts/weirdhost_renew.py

import os
import sys
import time
import asyncio
import aiohttp
import base64
import random
import re
import subprocess
import json
from datetime import datetime, timedelta
from urllib.parse import unquote

from seleniumbase import SB

try:
    from nacl import encoding, public
    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False

sys.stdout.reconfigure(line_buffering=True)

BASE_URL = "https://hub.weirdhost.xyz/server/"
API_BASE_URL = "https://hub.weirdhost.xyz/api/client"
DOMAIN = "hub.weirdhost.xyz"
MAX_COOKIE_COUNT = 5

RENEWAL_BUTTON_SELECTORS = [
    "//button//span[contains(text(), '연장하기')]/parent::button",
    "//button[contains(text(), '연장하기')]",
    "//button//span[contains(text(), '시간추가')]/parent::button",
    "//button[contains(text(), '시간추가')]",
    "//button//span[contains(text(), '시간 추가')]/parent::button",
    "//button[contains(text(), '시간 추가')]",
]


# ============================================================
#  工具函数
# ============================================================

def mask_sensitive(text, show_chars=3):
    if not text:
        return "***"
    text = str(text)
    if len(text) <= show_chars * 2:
        return "*" * len(text)
    return text[:show_chars] + "*" * (len(text) - show_chars * 2) + text[-show_chars:]


def mask_email(email):
    if not email or "@" not in email:
        return mask_sensitive(email)
    local, domain = email.rsplit("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


def mask_server_id(server_id):
    if not server_id:
        return "***"
    if len(server_id) <= 4:
        return "*" * len(server_id)
    return server_id[:2] + "*" * (len(server_id) - 4) + server_id[-2:]


def random_delay(min_sec=0.5, max_sec=2.0):
    time.sleep(random.uniform(min_sec, max_sec))


def calculate_remaining_time(expiry_str):
    try:
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
            try:
                expiry_dt = datetime.strptime(expiry_str.strip(), fmt)
                diff = expiry_dt - datetime.now()
                if diff.total_seconds() < 0:
                    return "已过期"
                days = diff.days
                hours = diff.seconds // 3600
                minutes = (diff.seconds % 3600) // 60
                parts = []
                if days > 0:
                    parts.append(f"{days}天")
                if hours > 0:
                    parts.append(f"{hours}小时")
                if minutes > 0 and days == 0:
                    parts.append(f"{minutes}分钟")
                return " ".join(parts) if parts else "不到1分钟"
            except ValueError:
                continue
        return "无法解析"
    except:
        return "计算失败"


def parse_expiry_to_datetime(expiry_str):
    if not expiry_str or expiry_str == "Unknown":
        return None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
        try:
            return datetime.strptime(expiry_str.strip(), fmt)
        except ValueError:
            continue
    return None


def get_remaining_days(expiry_str):
    expiry_dt = parse_expiry_to_datetime(expiry_str)
    if not expiry_dt:
        return None
    diff = expiry_dt - datetime.now()
    return diff.total_seconds() / 86400


def format_remaining_days(rd):
    if rd is None:
        return "?"
    return f"{rd:.1f}"


def parse_weirdhost_cookie(cookie_str):
    if not cookie_str:
        return (None, None)
    cookie_str = cookie_str.strip()
    if "=" in cookie_str:
        parts = cookie_str.split("=", 1)
        if len(parts) == 2:
            return (parts[0].strip(), unquote(parts[1].strip()))
    return (None, None)


def build_server_url(server_id):
    if not server_id:
        return None
    server_id = server_id.strip()
    return server_id if server_id.startswith("http") else f"{BASE_URL}{server_id}"


# ============================================================
#  账号自动检测
# ============================================================

def parse_account_config(raw_value):
    """
    解析环境变量值
    格式: 备注-----remember_web_xxx=yyy
    兼容: remember_web_xxx=yyy (无备注)
    """
    if not raw_value:
        return None
    raw_value = raw_value.strip()

    remark = ""
    cookie_str = ""

    if "-----" in raw_value:
        parts = raw_value.split("-----", 1)
        remark = parts[0].strip()
        cookie_str = parts[1].strip() if len(parts) > 1 else ""
    else:
        cookie_str = raw_value

    if not cookie_str or "=" not in cookie_str:
        return None

    cookie_name, cookie_value = parse_weirdhost_cookie(cookie_str)
    if not cookie_name or not cookie_name.startswith("remember_web"):
        return None

    return {
        "remark": remark,
        "cookie_str": cookie_str,
        "cookie_name": cookie_name,
        "cookie_value": cookie_value,
    }


def detect_accounts():
    """自动扫描 WEIRDHOST_COOKIE_1 ~ WEIRDHOST_COOKIE_N"""
    accounts = []
    for i in range(1, MAX_COOKIE_COUNT + 1):
        env_name = f"WEIRDHOST_COOKIE_{i}"
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue

        config = parse_account_config(raw)
        if not config:
            print(f"[WARN] {env_name} 格式错误，跳过")
            print(f"       正确格式: 备注-----remember_web_xxx=yyy")
            continue

        remark = config["remark"] or f"账号{i}"
        print(f"[INFO] 检测到 {env_name}: {remark}")

        accounts.append({
            "index": i,
            "cookie_env": env_name,
            "remark": remark,
            "cookie_str": config["cookie_str"],
            "cookie_name": config["cookie_name"],
            "cookie_value": config["cookie_value"],
        })

    return accounts


# ============================================================
#  Telegram 通知
# ============================================================

async def tg_notify(message):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        print("[INFO] 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知")
        return
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
            )
        except Exception as e:
            print(f"[ERROR] TG 发送失败: {e}")


async def tg_notify_photo(photo_path, caption=""):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id or not os.path.exists(photo_path):
        return
    async with aiohttp.ClientSession() as session:
        try:
            with open(photo_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                data.add_field("photo", f, filename=os.path.basename(photo_path))
                data.add_field("caption", caption)
                data.add_field("parse_mode", "HTML")
                await session.post(f"https://api.telegram.org/bot{token}/sendPhoto", data=data)
        except Exception as e:
            print(f"[ERROR] TG 图片发送失败: {e}")


def sync_tg_notify(message):
    asyncio.run(tg_notify(message))


def sync_tg_notify_photo(photo_path, caption=""):
    asyncio.run(tg_notify_photo(photo_path, caption))


# ============================================================
#  GitHub Secret
# ============================================================

def encrypt_secret(public_key, secret_value):
    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


async def update_github_secret(secret_name, secret_value):
    repo_token = os.environ.get("REPO_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo_token or not repository or not NACL_AVAILABLE:
        return False
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {repo_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with aiohttp.ClientSession() as session:
        try:
            pk_url = f"https://api.github.com/repos/{repository}/actions/secrets/public-key"
            async with session.get(pk_url, headers=headers) as resp:
                if resp.status != 200:
                    return False
                pk_data = await resp.json()
            encrypted_value = encrypt_secret(pk_data["key"], secret_value)
            secret_url = f"https://api.github.com/repos/{repository}/actions/secrets/{secret_name}"
            async with session.put(secret_url, headers=headers, json={
                "encrypted_value": encrypted_value, "key_id": pk_data["key_id"]
            }) as resp:
                return resp.status in (201, 204)
        except:
            return False


# ============================================================
#  WeirdHost API
# ============================================================

class WeirdHostAPI:
    def __init__(self, cookie_str):
        self.cookie_name, self.cookie_value = parse_weirdhost_cookie(cookie_str)
        self.xsrf_token = None
        self.initialized = False

    async def init_session(self, session):
        if self.initialized:
            return True
        if not self.cookie_name or not self.cookie_value:
            return False
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        session.cookie_jar.update_cookies({self.cookie_name: self.cookie_value})
        try:
            async with session.get(f"https://{DOMAIN}/", headers=headers) as resp:
                if resp.status != 200:
                    return False
                for c in session.cookie_jar:
                    if c.key == "XSRF-TOKEN":
                        self.xsrf_token = unquote(c.value)
                self.initialized = True
                return True
        except Exception as e:
            print(f"[ERROR] API 初始化异常: {e}")
            return False

    async def api_request(self, session, endpoint):
        if not self.initialized and not await self.init_session(session):
            return None
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://{DOMAIN}/",
        }
        if self.xsrf_token:
            headers["X-XSRF-TOKEN"] = self.xsrf_token
        try:
            async with session.get(f"{API_BASE_URL}{endpoint}", headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 401:
                    return {"error": "unauthorized"}
                return None
        except Exception as e:
            print(f"[ERROR] API 请求异常: {e}")
            return None

    async def get_account_email(self, session):
        data = await self.api_request(session, "/account/activity?sort=-timestamp&page=1&include[]=actor")
        if not data or data.get("error"):
            return None
        for item in data.get("data", []):
            actor = item.get("attributes", {}).get("relationships", {}).get("actor", {})
            if actor.get("object") == "user":
                email = actor.get("attributes", {}).get("email")
                if email:
                    return email
        return None

    async def get_server_list(self, session):
        return await self.api_request(session, "?page=1")

    async def get_server_info(self, session, server_uuid, server_type="notfree"):
        ep = f"/freeservers/{server_uuid}/info" if server_type == "free" else f"/notfreeservers/{server_uuid}/info"
        return await self.api_request(session, ep)


async def check_cookie_valid_async(cookie_str):
    cn, cv = parse_weirdhost_cookie(cookie_str)
    if not cn or not cv:
        return False
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            session.cookie_jar.update_cookies({cn: cv})
            headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
            async with session.get(f"https://{DOMAIN}/", headers=headers) as resp:
                if resp.status != 200:
                    return False
            api_headers = {
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://{DOMAIN}/",
            }
            for c in session.cookie_jar:
                if c.key == "XSRF-TOKEN":
                    api_headers["X-XSRF-TOKEN"] = unquote(c.value)
            async with session.get(f"{API_BASE_URL}?page=1", headers=api_headers) as resp:
                if resp.status == 200:
                    return "data" in (await resp.json())
                return False
    except:
        return False


def check_cookie_valid(cookie_str):
    return asyncio.run(check_cookie_valid_async(cookie_str))


async def get_account_info_via_api_async(cookie_str):
    api = WeirdHostAPI(cookie_str)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        if not await api.init_session(session):
            return None

        email = await api.get_account_email(session)

        server_list = await api.get_server_list(session)
        if not server_list or server_list.get("error"):
            return {"email": email, "servers": []}

        servers = []
        for s in server_list.get("data", []):
            attrs = s.get("attributes", {})
            stype = attrs.get("server_type", "")
            info = {
                "identifier": attrs.get("identifier", ""),
                "uuid": attrs.get("uuid", ""),
                "name": attrs.get("name", ""),
                "server_type": stype,
                "expire": "Unknown",
                "add_hours": "Unknown",
            }
            if attrs.get("uuid") and stype in ("notfree", "free"):
                si = await api.get_server_info(session, attrs["uuid"], stype)
                if si and si.get("success"):
                    d = si.get("data", {})
                    info["expire"] = d.get("expire", "Unknown")
                    info["add_hours"] = d.get("addHours", "Unknown")
            servers.append(info)

        return {"email": email, "servers": servers}


def get_account_info_via_api(cookie_str):
    return asyncio.run(get_account_info_via_api_async(cookie_str))


async def get_server_info_via_api_async(cookie_str, server_uuid, server_type="notfree"):
    api = WeirdHostAPI(cookie_str)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        if not await api.init_session(session):
            return None
        info = await api.get_server_info(session, server_uuid, server_type)
        if info and info.get("success"):
            return info.get("data", {})
        return None


def get_server_info_via_api(cookie_str, server_uuid, server_type="notfree"):
    return asyncio.run(get_server_info_via_api_async(cookie_str, server_uuid, server_type))


# ============================================================
#  SeleniumBase 页面交互
# ============================================================

def get_expiry_from_page(sb):
    try:
        page_text = sb.get_page_source()
        match = re.search(r'유통기한\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', page_text)
        if match:
            return match.group(1).strip()
        match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', page_text)
        if match:
            return match.group(1).strip()
        return "Unknown"
    except:
        return "Unknown"


def find_renewal_button(sb):
    for selector in RENEWAL_BUTTON_SELECTORS:
        try:
            if sb.is_element_present(selector):
                return selector
        except:
            continue
    return None


def check_renewal_button_enabled(sb):
    xpath = find_renewal_button(sb)
    if not xpath:
        return (False, False, None, "页面上未找到续期按钮")

    try:
        is_disabled = sb.execute_script(f"""
            var btn = document.evaluate("{xpath}", document, null,
                XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            if (!btn) return null;
            return btn.disabled || btn.getAttribute('aria-disabled') === 'true'
                   || btn.classList.contains('disabled');
        """)
        if is_disabled is None:
            return (False, False, None, "按钮元素无法访问")
        if is_disabled:
            return (True, False, xpath, "续期按钮已禁用（可能在冷却期）")
    except:
        pass

    return (True, True, xpath, "")


def is_logged_in(sb):
    try:
        url = sb.get_current_url()
        if "/login" in url or "/auth" in url:
            return False
        if get_expiry_from_page(sb) != "Unknown":
            return True
        if find_renewal_button(sb):
            return True
        if sb.is_element_present("//div[contains(@class,'ServerControls')]") or \
           sb.is_element_present("//a[contains(@href,'/server/')]"):
            return True
        return False
    except:
        return False


EXPAND_POPUP_JS = """
(function() {
    var turnstileInput = document.querySelector('input[name="cf-turnstile-response"]');
    if (!turnstileInput) return 'no turnstile input';
    var el = turnstileInput;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var style = window.getComputedStyle(el);
        if (style.overflow === 'hidden' || style.overflowX === 'hidden' || style.overflowY === 'hidden') {
            el.style.overflow = 'visible';
        }
        el.style.minWidth = 'max-content';
    }
    var turnstileContainers = document.querySelectorAll('[class*="sc-fKFyDc"], [class*="nwOmR"]');
    turnstileContainers.forEach(function(container) {
        container.style.overflow = 'visible';
        container.style.width = '300px';
        container.style.minWidth = '300px';
        container.style.height = '65px';
    });
    var iframes = document.querySelectorAll('iframe');
    iframes.forEach(function(iframe) {
        if (iframe.src && iframe.src.includes('challenges.cloudflare.com')) {
            iframe.style.width = '300px';
            iframe.style.height = '65px';
            iframe.style.minWidth = '300px';
            iframe.style.visibility = 'visible';
            iframe.style.opacity = '1';
        }
    });
    return 'done';
})();
"""


def check_turnstile_exists(sb):
    try:
        return sb.execute_script(
            "return document.querySelector('input[name=\"cf-turnstile-response\"]') !== null;"
        )
    except:
        return False


def check_turnstile_solved(sb):
    try:
        return sb.execute_script("""
            var input = document.querySelector('input[name="cf-turnstile-response"]');
            return input && input.value && input.value.length > 20;
        """)
    except:
        return False


def get_turnstile_checkbox_coords(sb):
    try:
        return sb.execute_script("""
            var iframes = document.querySelectorAll('iframe');
            for (var i = 0; i < iframes.length; i++) {
                var src = iframes[i].src || '';
                if (src.includes('cloudflare') || src.includes('turnstile')) {
                    var rect = iframes[i].getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        return {x:rect.x, y:rect.y, width:rect.width, height:rect.height,
                                click_x:Math.round(rect.x+30), click_y:Math.round(rect.y+rect.height/2)};
                    }
                }
            }
            var input = document.querySelector('input[name="cf-turnstile-response"]');
            if (input) {
                var container = input.parentElement;
                for (var j = 0; j < 5; j++) {
                    if (!container) break;
                    var rect = container.getBoundingClientRect();
                    if (rect.width > 100 && rect.height > 30) {
                        return {x:rect.x, y:rect.y, width:rect.width, height:rect.height,
                                click_x:Math.round(rect.x+30), click_y:Math.round(rect.y+rect.height/2)};
                    }
                    container = container.parentElement;
                }
            }
            return null;
        """)
    except:
        return None


def activate_browser_window():
    try:
        result = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", "chrome"],
            capture_output=True, text=True, timeout=3
        )
        window_ids = result.stdout.strip().split('\n')
        if window_ids and window_ids[0]:
            subprocess.run(
                ["xdotool", "windowactivate", window_ids[0]],
                timeout=2, stderr=subprocess.DEVNULL
            )
            time.sleep(0.2)
            return True
    except:
        pass
    return False


def xdotool_click(x, y):
    x, y = int(x), int(y)
    activate_browser_window()
    try:
        subprocess.run(["xdotool", "mousemove", str(x), str(y)], timeout=2, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
        return True
    except:
        pass
    try:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")
        return True
    except:
        return False


def click_turnstile_checkbox(sb):
    coords = get_turnstile_checkbox_coords(sb)
    if not coords:
        print("[WARN] 无法获取 Turnstile 坐标")
        return False

    try:
        window_info = sb.execute_script("""
            return {screenX:window.screenX||0, screenY:window.screenY||0,
                    outerHeight:window.outerHeight, innerHeight:window.innerHeight};
        """)
        chrome_bar_height = window_info["outerHeight"] - window_info["innerHeight"]
        abs_x = coords["click_x"] + window_info["screenX"]
        abs_y = coords["click_y"] + window_info["screenY"] + chrome_bar_height
        return xdotool_click(abs_x, abs_y)
    except Exception as e:
        print(f"[ERROR] 坐标计算失败: {e}")
        return False


def check_result_popup(sb):
    try:
        return sb.execute_script("""
            var buttons = document.querySelectorAll('button');
            var hasNextBtn = false;
            for (var i = 0; i < buttons.length; i++) {
                if (buttons[i].innerText.includes('NEXT') || buttons[i].innerText.includes('Next')) {
                    hasNextBtn = true; break;
                }
            }
            var bodyText = document.body.innerText || '';
            var hasSuccessTitle = bodyText.includes('Success');
            var hasSuccessContent = bodyText.includes('성공') || bodyText.includes('갱신') || bodyText.includes('연장');
            var hasCooldown = bodyText.includes('아직') || bodyText.includes('Error');
            if (hasNextBtn || hasSuccessTitle) {
                if (hasCooldown && bodyText.includes('아직')) return 'cooldown';
                if (hasSuccessTitle && hasSuccessContent) return 'success';
                if (hasNextBtn) {
                    if (hasCooldown) return 'cooldown';
                    if (hasSuccessContent) return 'success';
                }
            }
            return null;
        """)
    except:
        return None


def check_popup_still_open(sb):
    try:
        return sb.execute_script("""
            var t = document.querySelector('input[name="cf-turnstile-response"]');
            if (!t) return false;
            var buttons = document.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                var text = buttons[i].innerText || '';
                if ((text.includes('시간추가') || text.includes('시간 추가') || text.includes('연장하기'))
                    && !text.includes('DELETE')) {
                    var rect = buttons[i].getBoundingClientRect();
                    if (rect.x > 200 && rect.width > 0) return true;
                }
            }
            return false;
        """)
    except:
        return False


def click_next_button(sb):
    try:
        for sel in [
            "//button[contains(text(), 'NEXT')]",
            "//button[contains(text(), 'Next')]",
            "//button//span[contains(text(), 'NEXT')]",
        ]:
            if sb.is_element_visible(sel):
                sb.click(sel)
                print("[INFO] 已点击 NEXT 按钮")
                return True
    except:
        pass
    return False


def handle_renewal_popup(sb, screenshot_prefix="", timeout=90):
    screenshot_name = f"{screenshot_prefix}_popup.png" if screenshot_prefix else "popup_fixed.png"

    print("[INFO]   [阶段1] 等待弹窗和 Turnstile...")

    turnstile_ready = False
    for _ in range(20):
        result = check_result_popup(sb)
        if result == "cooldown":
            print("[INFO]   检测到冷却期弹窗")
            sb.save_screenshot(screenshot_name)
            return {"status": "cooldown", "screenshot": screenshot_name}
        if result == "success":
            print("[INFO]   检测到成功弹窗")
            sb.save_screenshot(screenshot_name)
            return {"status": "success", "screenshot": screenshot_name}
        if check_turnstile_exists(sb):
            turnstile_ready = True
            print("[INFO]   检测到 Turnstile")
            break
        time.sleep(1)

    if not turnstile_ready:
        print("[WARN]   未检测到 Turnstile")
        sb.save_screenshot(screenshot_name)
        return {"status": "error", "message": "未检测到 Turnstile", "screenshot": screenshot_name}

    print("[INFO]   [阶段2] 修复弹窗样式...")
    for _ in range(3):
        sb.execute_script(EXPAND_POPUP_JS)
        time.sleep(0.5)
    sb.save_screenshot(screenshot_name)

    print("[INFO]   [阶段3] 点击 Turnstile...")
    for attempt in range(6):
        if check_turnstile_solved(sb):
            print("[INFO]   Turnstile 已通过!")
            break
        sb.execute_script(EXPAND_POPUP_JS)
        time.sleep(0.3)
        click_turnstile_checkbox(sb)
        for _ in range(8):
            time.sleep(0.5)
            if check_turnstile_solved(sb):
                print("[INFO]   Turnstile 已通过!")
                break
        if check_turnstile_solved(sb):
            break
        sb.save_screenshot(
            f"{screenshot_prefix}_turnstile_{attempt}.png" if screenshot_prefix
            else f"turnstile_attempt_{attempt}.png"
        )

    print("[INFO]   等待提交结果...")
    result_start = time.time()
    last_screenshot_time = 0

    while time.time() - result_start < 45:
        result = check_result_popup(sb)
        if result == "success":
            print("[INFO]   续期成功!")
            sb.save_screenshot(screenshot_name)
            time.sleep(1)
            click_next_button(sb)
            return {"status": "success", "screenshot": screenshot_name}
        if result == "cooldown":
            print("[INFO]   冷却期内")
            sb.save_screenshot(screenshot_name)
            time.sleep(1)
            click_next_button(sb)
            return {"status": "cooldown", "screenshot": screenshot_name}
        if not check_popup_still_open(sb):
            time.sleep(2)
            result = check_result_popup(sb)
            if result:
                sb.save_screenshot(screenshot_name)
                if result == "success":
                    click_next_button(sb)
                    return {"status": "success", "screenshot": screenshot_name}
                elif result == "cooldown":
                    click_next_button(sb)
                    return {"status": "cooldown", "screenshot": screenshot_name}
        if time.time() - last_screenshot_time > 5:
            sb.save_screenshot(screenshot_name)
            last_screenshot_time = time.time()
        time.sleep(1)

    print("[WARN]   等待结果超时")
    sb.save_screenshot(screenshot_name)
    return {"status": "timeout", "screenshot": screenshot_name}


def check_and_update_cookie(sb, cookie_env, original_cookie_value, remark=""):
    """检查浏览器 Cookie 变化，变化时更新 GitHub Secret（保持 备注-----cookie 格式）"""
    try:
        cookies = sb.get_cookies()
        for cookie in cookies:
            if cookie.get("name", "").startswith("remember_web"):
                new_val = cookie.get("value", "")
                c_name = cookie.get("name", "")
                if new_val and new_val != original_cookie_value:
                    new_cookie_str = f"{c_name}={new_val}"
                    if remark:
                        new_secret_value = f"{remark}-----{new_cookie_str}"
                    else:
                        new_secret_value = new_cookie_str
                    print(f"[INFO]   Cookie 已变化，更新 {cookie_env}...")
                    if asyncio.run(update_github_secret(cookie_env, new_secret_value)):
                        print(f"[INFO]   ✅ {cookie_env} 已更新")
                        return True
                    else:
                        print(f"[ERROR]  ❌ {cookie_env} 更新失败")
                        return False
                break
    except Exception as e:
        print(f"[ERROR]  Cookie 检查失败: {e}")
    return False


# ============================================================
#  单个服务器的续期处理
# ============================================================

def process_single_server(sb, server_info, cookie_name, cookie_value, cookie_str,
                          cookie_env, remark, screenshot_prefix):
    server_id = server_info.get("identifier", "Unknown")
    server_uuid = server_info.get("uuid", "")
    server_type = server_info.get("server_type", "notfree")
    server_name = server_info.get("name", "")
    api_expiry = server_info.get("expire", "Unknown")

    srv_result = {
        "server_id": server_id,
        "server_uuid": server_uuid,
        "server_type": server_type,
        "server_name": server_name,
        "status": "unknown",
        "original_expiry": api_expiry,
        "new_expiry": api_expiry,
        "message": "",
        "screenshot": None,
        "cookie_updated": False,
    }

    server_url = build_server_url(server_id)
    rd = get_remaining_days(api_expiry)
    dd = format_remaining_days(rd)

    # 日志中隐藏服务器ID
    print(f"\n  {'─' * 50}")
    print(f"  [INFO] 服务器: {mask_server_id(server_id)} [{server_type}] {server_name}")
    print(f"  [INFO] 到期: {api_expiry} | 剩余: {calculate_remaining_time(api_expiry)} ({dd}天)")
    print(f"  [INFO] 访问服务器页面...")

    try:
        sb.uc_open_with_reconnect(server_url, reconnect_time=5)
        time.sleep(3)

        if not is_logged_in(sb):
            sb.add_cookie({"name": cookie_name, "value": cookie_value, "domain": DOMAIN, "path": "/"})
            sb.uc_open_with_reconnect(server_url, reconnect_time=5)
            time.sleep(3)

        if not is_logged_in(sb):
            ss_path = f"{screenshot_prefix}_login_fail.png"
            sb.save_screenshot(ss_path)
            srv_result.update(status="error", message="浏览器登录失败", screenshot=ss_path)
            print(f"  [ERROR] 浏览器登录失败")
            return srv_result

        print(f"  [INFO] 登录成功")

        page_expiry = get_expiry_from_page(sb)
        if page_expiry != "Unknown":
            srv_result["original_expiry"] = page_expiry

        # 检查续期按钮
        print(f"  [INFO] 检查续期按钮...")
        btn_found, btn_enabled, btn_xpath, btn_reason = check_renewal_button_enabled(sb)

        if not btn_found:
            print(f"  [WARN] {btn_reason}")
            ss_path = f"{screenshot_prefix}_no_btn.png"
            sb.save_screenshot(ss_path)
            srv_result.update(status="skipped", message=btn_reason, screenshot=ss_path)
            return srv_result

        if not btn_enabled:
            print(f"  [WARN] {btn_reason}")
            ss_path = f"{screenshot_prefix}_btn_disabled.png"
            sb.save_screenshot(ss_path)
            srv_result.update(status="skipped", message=btn_reason, screenshot=ss_path)
            return srv_result

        print(f"  [INFO] 续期按钮可用，执行续期")

        random_delay(1.0, 2.0)
        sb.click(btn_xpath)
        print(f"  [INFO] 已点击续期按钮，等待弹窗...")
        time.sleep(3)

        popup_result = handle_renewal_popup(sb, screenshot_prefix=screenshot_prefix, timeout=90)
        srv_result["screenshot"] = popup_result.get("screenshot")

        # 验证结果
        time.sleep(3)
        if server_uuid:
            new_info = get_server_info_via_api(cookie_str, server_uuid, server_type)
            new_expiry = new_info.get("expire", "Unknown") if new_info else srv_result["original_expiry"]
        else:
            sb.uc_open_with_reconnect(server_url, reconnect_time=3)
            time.sleep(3)
            new_expiry = get_expiry_from_page(sb)

        srv_result["new_expiry"] = new_expiry

        original_dt = parse_expiry_to_datetime(srv_result["original_expiry"])
        new_dt = parse_expiry_to_datetime(new_expiry)

        if popup_result["status"] == "cooldown":
            srv_result.update(status="cooldown", message="冷却期内")
            print(f"  [INFO] 冷却期内")
        elif original_dt and new_dt and new_dt > original_dt:
            diff_h = (new_dt - original_dt).total_seconds() / 3600
            srv_result.update(status="success", message=f"延长了 {diff_h:.1f} 小时")
            print(f"  [INFO] ✅ 续期成功！延长 {diff_h:.1f} 小时")
        elif popup_result["status"] == "success":
            srv_result.update(status="success", message="操作完成")
            print(f"  [INFO] ✅ 续期成功")
        else:
            srv_result.update(status=popup_result["status"], message=popup_result.get("message", "未知"))
            print(f"  [WARN] 结果: {popup_result['status']}")

        if check_and_update_cookie(sb, cookie_env, cookie_value, remark):
            srv_result["cookie_updated"] = True

        if not srv_result["screenshot"] or not os.path.exists(srv_result["screenshot"]):
            final_ss = f"{screenshot_prefix}_final.png"
            sb.save_screenshot(final_ss)
            srv_result["screenshot"] = final_ss

    except Exception as e:
        import traceback
        print(f"  [ERROR] 异常: {repr(e)}")
        traceback.print_exc()
        srv_result.update(status="error", message=str(e)[:100])
        try:
            ss_path = f"{screenshot_prefix}_error.png"
            sb.save_screenshot(ss_path)
            srv_result["screenshot"] = ss_path
        except:
            pass

    return srv_result


# ============================================================
#  单个账号的处理
# ============================================================

def process_single_account(sb, account, account_index):
    remark = account.get("remark", f"账号{account_index + 1}")
    cookie_env = account.get("cookie_env", "")
    cookie_str = account.get("cookie_str", "")
    cookie_name = account.get("cookie_name", "")
    cookie_value = account.get("cookie_value", "")

    result = {
        "remark": remark,
        "cookie_env": cookie_env,
        "email": "Unknown",
        "status": "unknown",
        "message": "",
        "servers": [],
        "cookie_updated": False,
    }

    print(f"\n{'=' * 60}")
    print(f"[INFO] 处理账号 [{account_index + 1}]: {remark} ({cookie_env})")
    print(f"{'=' * 60}")

    # Step 1: Cookie 有效性
    print(f"[INFO] [步骤1] 检查 Cookie 有效性...")
    if not check_cookie_valid(cookie_str):
        print(f"[ERROR] Cookie 已失效")
        result["status"] = "cookie_invalid"
        result["message"] = "Cookie 失效，请重新获取"
        return result
    print(f"[INFO] ✅ Cookie 有效")

    # Step 2: API 获取信息
    print(f"[INFO] [步骤2] API 获取账号信息...")
    account_info = get_account_info_via_api(cookie_str)
    if not account_info:
        print(f"[ERROR] API 调用失败")
        result["status"] = "error"
        result["message"] = "API 调用失败"
        return result

    email = account_info.get("email", "Unknown")
    servers = account_info.get("servers", [])
    result["email"] = email

    # 日志中隐藏邮箱
    if email and email != "Unknown":
        print(f"[INFO] 邮箱: {mask_email(email)}")

    if not servers:
        print(f"[WARN] 该账号下没有服务器")
        result["status"] = "no_server"
        result["message"] = "该账号下没有服务器"
        return result

    # 日志中隐藏服务器ID
    print(f"[INFO] 找到 {len(servers)} 个服务器:")
    for s in servers:
        sid = s.get("identifier", "?")
        stype = s.get("server_type", "?")
        sname = s.get("name", "")
        sexpire = s.get("expire", "Unknown")
        print(f"  - {mask_server_id(sid)} [{stype}] {sname} | 到期: {sexpire}")

    # Step 3: 设置 Cookie
    print(f"[INFO] [步骤3] 设置浏览器 Cookie...")
    try:
        sb.uc_open_with_reconnect(f"https://{DOMAIN}", reconnect_time=3)
        time.sleep(1)
        sb.delete_all_cookies()
    except:
        pass
    sb.uc_open_with_reconnect(f"https://{DOMAIN}", reconnect_time=3)
    time.sleep(2)
    sb.add_cookie({"name": cookie_name, "value": cookie_value, "domain": DOMAIN, "path": "/"})
    print(f"[INFO] Cookie 已设置")

    # Step 4: 逐个服务器
    print(f"[INFO] [步骤4] 逐个处理服务器续期...")
    server_results = []

    for srv_idx, server in enumerate(servers):
        ss_prefix = f"acc{account_index + 1}_srv{srv_idx + 1}"
        srv_result = process_single_server(
            sb, server, cookie_name, cookie_value, cookie_str, cookie_env, remark, ss_prefix
        )
        server_results.append(srv_result)

        if srv_result.get("cookie_updated"):
            result["cookie_updated"] = True

        if srv_idx < len(servers) - 1:
            if srv_result.get("status") == "skipped":
                wait = random.randint(2, 4)
            else:
                wait = random.randint(5, 10)
            print(f"\n  [INFO] 等待 {wait} 秒后处理下一个服务器...")
            time.sleep(wait)

    result["servers"] = server_results

    # 汇总
    statuses = [s["status"] for s in server_results]
    if "success" in statuses:
        result["status"] = "success"
        result["message"] = f"{statuses.count('success')}/{len(statuses)} 个服务器续期成功"
    elif all(s == "skipped" for s in statuses):
        result["status"] = "skipped"
        result["message"] = "所有服务器均跳过"
    elif "cooldown" in statuses:
        result["status"] = "cooldown"
        result["message"] = "冷却期内"
    elif "error" in statuses or "timeout" in statuses:
        result["status"] = "error"
        err_count = statuses.count("error") + statuses.count("timeout")
        result["message"] = f"{err_count}/{len(statuses)} 个服务器失败"
    else:
        result["status"] = statuses[0] if statuses else "unknown"

    return result


# ============================================================
#  TG 汇总报告（私人通知，显示完整信息）
# ============================================================

def send_summary_report(results):
    total_accounts = len(results)
    total_servers = sum(len(r.get("servers", [])) for r in results)
    success_servers = sum(
        sum(1 for s in r.get("servers", []) if s["status"] == "success") for r in results
    )
    skipped_servers = sum(
        sum(1 for s in r.get("servers", []) if s["status"] == "skipped") for r in results
    )
    failed_servers = sum(
        sum(1 for s in r.get("servers", []) if s["status"] in ["error", "timeout", "cooldown"])
        for r in results
    )

    lines = [
        f"🔔 <b>Weirdhost 续期报告</b>",
        f"",
        f"👤 账号: {total_accounts}  |  🖥 服务器: {total_servers}",
        f"✅ {success_servers}  ⏭️ {skipped_servers}  ❌ {failed_servers}",
    ]

    for i, r in enumerate(results):
        remark = r.get("remark", f"账号{i+1}")
        email = r.get("email", "Unknown")
        cookie_env = r.get("cookie_env", "")
        cookie_updated = r.get("cookie_updated", False)
        server_list = r.get("servers", [])

        acct_icon = {
            "success": "✅", "cooldown": "⏳", "skipped": "⏭️",
            "error": "❌", "timeout": "⚠️", "cookie_invalid": "🔒",
            "no_server": "📭",
        }.get(r["status"], "❓")

        # TG 通知：显示完整邮箱（私人）
        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        acct_title = f"{acct_icon} <b>{remark}</b>"
        if email and email != "Unknown":
            acct_title += f"  ({email})"
        lines.append(acct_title)

        if not server_list:
            lines.append(f"  ⚠️ {r.get('message', '未知错误')}")
            if cookie_updated:
                lines.append(f"  🔑 Cookie 已自动更新")
            continue

        for s in server_list:
            # TG 通知：显示完整服务器ID（私人）
            sid = s.get("server_id", "?")
            stype = s.get("server_type", "?")
            sname = s.get("server_name", "")
            status = s["status"]

            srv_icon = {
                "success": "✅", "cooldown": "⏳", "skipped": "⏭️",
                "error": "❌", "timeout": "⚠️",
            }.get(status, "❓")

            type_label = "💰" if stype == "notfree" else "🆓" if stype == "free" else "❓"

            lines.append(f"")
            lines.append(f"  {srv_icon} {type_label} <code>{sid}</code>")
            if sname:
                lines.append(f"      {sname}")

            if status == "success":
                new_exp = s.get("new_expiry", "Unknown")
                lines.append(f"      📅 {new_exp}")
                lines.append(f"      ⏳ 剩余 {calculate_remaining_time(new_exp)}")
                msg = s.get("message", "")
                if msg:
                    lines.append(f"      📝 {msg}")

            elif status == "skipped":
                expiry = s.get("original_expiry", s.get("new_expiry", "Unknown"))
                lines.append(f"      📅 {expiry}")
                lines.append(f"      ⏳ 剩余 {calculate_remaining_time(expiry)}")
                msg = s.get("message", "")
                if msg:
                    lines.append(f"      💡 {msg}")

            elif status == "cooldown":
                expiry = s.get("original_expiry", "Unknown")
                lines.append(f"      📅 {expiry}")
                lines.append(f"      ⏳ 剩余 {calculate_remaining_time(expiry)}")
                lines.append(f"      💡 冷却中")

            else:
                lines.append(f"      ⚠️ {s.get('message', '未知错误')}")

        if cookie_updated:
            lines.append(f"  🔑 Cookie 已自动更新")

    message = "\n".join(lines)

    # 找截图
    screenshot = None
    for r in results:
        for s in r.get("servers", []):
            if s["status"] in ["success", "cooldown", "error", "timeout"]:
                if s.get("screenshot") and os.path.exists(s["screenshot"]):
                    screenshot = s["screenshot"]
                    break
        if screenshot:
            break

    if screenshot:
        sync_tg_notify_photo(screenshot, message)
    else:
        sync_tg_notify(message)


# ============================================================
#  主函数
# ============================================================

def add_server_time():
    accounts = detect_accounts()

    if not accounts:
        print("\n" + "=" * 60)
        print("[ERROR] 未检测到任何有效的账号配置")
        print("=" * 60)
        print("\n请在 GitHub Secrets 中设置 WEIRDHOST_COOKIE_1 ~ WEIRDHOST_COOKIE_5")
        print("\n格式: 备注-----remember_web_xxx=yyy")
        print("示例: 我的账号-----remember_web_59ba36addc2b2f940CCCC=XXXXXXXXXXX")
        print("\n也支持纯 Cookie 格式 (无备注):")
        print("  remember_web_59ba36addc2b2f940CCCC=XXXXXXXXXXX")
        print("=" * 60)

        sync_tg_notify(
            "🔔 <b>Weirdhost 续期</b>\n\n"
            "❌ 未检测到任何有效的 WEIRDHOST_COOKIE_N\n\n"
            "请在 GitHub Secrets 中设置:\n"
            "<code>WEIRDHOST_COOKIE_1</code>\n"
            "格式: <code>备注-----remember_web_xxx=yyy</code>"
        )
        return

    print("=" * 60)
    print(f"[INFO] Weirdhost 自动续期")
    print(f"[INFO] 共 {len(accounts)} 个账号")
    print(f"[INFO] 续期策略: 检查续期按钮是否可用")
    print(f"[INFO] 服务器发现: API 自动查找")
    print("=" * 60)

    results = []

    try:
        with SB(
            uc=True,
            test=True,
            locale="ko",
            headless=False,
            chromium_arg="--disable-dev-shm-usage,--no-sandbox,--disable-gpu,--disable-software-rasterizer,--disable-background-timer-throttling"
        ) as sb:
            print("\n[INFO] 浏览器已启动")

            for i, account in enumerate(accounts):
                result = process_single_account(sb, account, i)
                results.append(result)

                if i < len(accounts) - 1:
                    if result.get("status") == "skipped":
                        wait_time = random.randint(2, 4)
                    else:
                        wait_time = random.randint(5, 10)
                    print(f"\n[INFO] 等待 {wait_time} 秒后处理下一个账号...")
                    time.sleep(wait_time)

    except Exception as e:
        import traceback
        print(f"\n[ERROR] 浏览器异常: {repr(e)}")
        traceback.print_exc()

        if results:
            send_summary_report(results)
        else:
            sync_tg_notify(f"🔔 <b>Weirdhost</b>\n\n❌ 浏览器启动失败\n\n<code>{repr(e)}</code>")
        return

    # 控制台摘要（隐藏敏感信息）
    print(f"\n{'=' * 60}")
    print("[INFO] 全部处理完成")
    print(f"{'=' * 60}")
    icons = {
        "success": "🟢", "cooldown": "🟡", "skipped": "🔵",
        "cookie_invalid": "🔒", "no_server": "📭",
        "error": "❌", "timeout": "⚠️",
    }
    for r in results:
        icon = icons.get(r["status"], "❓")
        srv_count = len(r.get("servers", []))
        email_display = mask_email(r.get("email", ""))
        print(f"  {icon} {r.get('remark', '?')} ({email_display}) | "
              f"{srv_count} 个服务器 | {r['status']} | {r.get('message', '')}")

    send_summary_report(results)


if __name__ == "__main__":
    add_server_time()
