#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本 (最终调试版)
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.notify import notify

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'

def load_balance_hash():
    try:
        if os.path.exists(BALANCE_HASH_FILE):
            with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception:
        pass
    return None

def save_balance_hash(balance_hash):
    try:
        with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
            f.write(balance_hash)
    except Exception as e:
        print(f'Warning: Failed to save balance hash: {e}')

def generate_balance_hash(balances):
    simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]

def parse_cookies(cookies_data):
    if isinstance(cookies_data, dict):
        return cookies_data
    if isinstance(cookies_data, str):
        cookies_dict = {}
        for cookie in cookies_data.split(';'):
            if '=' in cookie:
                key, value = cookie.strip().split('=', 1)
                cookies_dict[key] = value
        return cookies_dict
    return {}

def get_domain_from_url(url):
    parsed = urlparse(url)
    return parsed.netloc

async def run_playwright_checkin(account_name: str, provider_config, cookies: dict) -> bool:
    """
    Playwright 签到 - 增强版选择器 + 调试信息
    """
    print(f'[BROWSER] {account_name}: Starting browser automation...')

    target_url = f'{provider_config.domain}/console/personal'
    domain = get_domain_from_url(provider_config.domain)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )

            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080}
            )

            pw_cookies = []
            for name, value in cookies.items():
                pw_cookies.append({'name': name, 'value': value, 'domain': domain, 'path': '/'})
            await context.add_cookies(pw_cookies)
            
            page = await context.new_page()

            print(f'[BROWSER] {account_name}: Navigating to {target_url}')
            try:
                await page.goto(target_url, timeout=60000, wait_until='domcontentloaded')
            except Exception as e:
                print(f'[WARN] {account_name}: Page load warning: {str(e)[:50]}')

            # 等待页面加载，特别是 API 回调渲染
            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
            except:
                pass
            
            await page.wait_for_timeout(3000) # 额外等待 3 秒给 Vue/React 渲染

            # URL 校验
            current_url = page.url
            if "/login" in current_url:
                print(f'[FAILED] {account_name}: Redirected to login page. Cookie expired.')
                await browser.close()
                return False

            # 状态检查
            content = await page.content()
            if "已签到" in content or "今日已签" in content:
                print(f'[SUCCESS] {account_name}: Status is "Already Signed In".')
                await browser.close()
                return True

            # --- 寻找按钮 ---
            # 策略：增加更多可能的选择器，包括图标和模糊匹配
            
            target_button = None
            
            # 1. 尝试标准选择器
            selectors = [
                "#checkin", 
                "button:has-text('签到')",
                "button:has-text('打卡')",
                "text=立即签到",
                "a:has-text('签到')", # 有些是链接样式
                "div[onclick*='checkin']", # 绑定了点击事件的 div
                ".ui.button.positive", # 常见的 Semantic UI 绿色按钮
                ".ui.button.primary"
            ]

            print(f'[DEBUG] {account_name}: Scanning for check-in button...')
            
            for selector in selectors:
                locator = page.locator(selector)
                count = await locator.count()
                for i in range(count):
                    element = locator.nth(i)
                    if await element.is_visible():
                        # 排除导航栏 (通常在 header 或 nav 标签里，或者位置很靠上)
                        box = await element.bounding_box()
                        if box and box['y'] > 50: # 假设导航栏在顶部 50px
                            target_button = element
                            print(f'[ACTION] {account_name}: Found candidate using "{selector}" at y={box["y"]}')
                            break
                if target_button:
                    break

            # 2. 如果没找到，尝试查找“未签到”相关的卡片并点击
            if not target_button:
                try:
                    # 查找包含“签到”文字的任何可点击元素
                    candidates = page.get_by_text("签到")
                    count = await candidates.count()
                    if count > 0:
                         print(f'[DEBUG] {account_name}: Found {count} text elements containing "签到", trying to click likely one...')
                         for i in range(count):
                             if await candidates.nth(i).is_visible():
                                 target_button = candidates.nth(i)
                                 break
                except:
                    pass

            # --- 失败时的调试信息打印 ---
            if not target_button:
                print(f'[FAILED] {account_name}: Could not find button.')
                print(f'[DEBUG] {account_name}: Listing all visible button texts on page:')
                try:
                    buttons = page.locator("button")
                    count = await buttons.count()
                    for i in range(count):
                        txt = await buttons.nth(i).text_content()
                        if txt:
                            print(f' - Button {i}: "{txt.strip()}"')
                    
                    # 打印页面部分内容辅助判断
                    # print(f'[DEBUG] HTML Snippet: {content[:1000]}')
                except:
                    pass
                
                await browser.close()
                return False

            # --- 点击并验证 ---
            print(f'[ACTION] {account_name}: Clicking button...')
            
            # 监听网络请求
            request_triggered = False
            checkin_success = False
            
            async with page.expect_response(
                lambda response: "checkin" in response.url and response.request.method == "POST",
                timeout=6000
            ) as response_info:
                try:
                    await target_button.click()
                    request_triggered = True
                except Exception as e:
                    print(f'[ERROR] Click action failed: {e}')

            if request_triggered:
                try:
                    response = await response_info.value
                    status = response.status
                    print(f'[NETWORK] {account_name}: API Status: {status}')
                    if status == 200:
                        checkin_success = True
                except Exception as e:
                     print(f'[WARN] {account_name}: No API response captured (timeout), verifying UI...')
                     await page.wait_for_timeout(2000)
                     if "已签到" in await page.content():
                         checkin_success = True
            else:
                 # 没捕获到 API 请求，可能是直接刷新了页面
                 print(f'[WARN] {account_name}: No network request triggered. Checking UI text...')
                 await page.wait_for_timeout(2000)
                 if "已签到" in await page.content():
                     checkin_success = True

            await browser.close()
            
            if checkin_success:
                print(f'[SUCCESS] {account_name}: Check-in verified.')
                return True
            else:
                print(f'[FAILED] {account_name}: Clicked but verification failed.')
                return False

        except Exception as e:
            print(f'[ERROR] {account_name}: Playwright error: {e}')
            return False

def get_user_info(client, headers, user_info_url: str):
    try:
        response = client.get(user_info_url, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                user_data = data.get('data', {})
                quota = round(user_data.get('quota', 0) / 500000, 2)
                used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
                return {
                    'success': True,
                    'quota': quota,
                    'used_quota': used_quota,
                    'display': f':money: Current balance: ${quota}, Used: ${used_quota}',
                }
        return {'success': False, 'error': f'HTTP {response.status_code}'}
    except Exception as e:
        return {'success': False, 'error': f'{str(e)[:50]}'}

async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    account_name = account.get_display_name(account_index)
    print(f'\n[PROCESSING] Starting to process {account_name}')

    provider_config = app_config.get_provider(account.provider)
    if not provider_config:
        print(f'[FAILED] {account_name}: Provider "{account.provider}" not found')
        return False, None

    print(f'[INFO] {account_name}: Using provider "{account.provider}"')
    user_cookies = parse_cookies(account.cookies)
    
    # 执行浏览器签到
    check_in_result = await run_playwright_checkin(account_name, provider_config, user_cookies)

    # 查询余额
    user_info = None
    try:
        client = httpx.Client(http2=True, timeout=30.0)
        client.cookies.update(user_cookies) 
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': provider_config.domain,
            provider_config.api_user_key: account.api_user,
        }
        user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
        user_info = get_user_info(client, headers, user_info_url)
        if user_info and user_info.get('success'):
            print(user_info['display'])
        client.close()
    except Exception as e:
        print(f'[WARN] {account_name}: Failed to fetch balance: {e}')

    return check_in_result, user_info

async def main():
    print('[SYSTEM] AnyRouter.top Auto Check-in (Debug Mode)')
    print(f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    app_config = AppConfig.load_from_env()
    accounts = load_accounts_config()
    
    if not accounts:
        print('[FAILED] No accounts found.')
        sys.exit(1)

    print(f'[INFO] Found {len(accounts)} accounts.')

    last_balance_hash = load_balance_hash()
    success_count = 0
    total_count = len(accounts)
    notification_content = []
    current_balances = {}
    need_notify = False
    balance_changed = False

    for i, account in enumerate(accounts):
        account_key = f'account_{i + 1}'
        try:
            success, user_info = await check_in_account(account, i, app_config)
            
            if success:
                success_count += 1
            else:
                need_notify = True
                account_name = account.get_display_name(i)
                # 如果是因为 cookie 过期，明确提示
                notification_content.append(f'[FAIL] {account_name}: Check-in failed (Please check cookies)')

            if user_info and user_info.get('success'):
                current_balances[account_key] = {'quota': user_info['quota'], 'used': user_info['used_quota']}

        except Exception as e:
            need_notify = True
            print(f'[ERROR] Account {i+1} exception: {e}')
            notification_content.append(f'[ERROR] Account {i+1}: {str(e)[:50]}')

    current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
    if current_balance_hash:
        if last_balance_hash is None or current_balance_hash != last_balance_hash:
            balance_changed = True
            need_notify = True
            print('[NOTIFY] Balance change detected')
        save_balance_hash(current_balance_hash)

    if balance_changed:
        for i, account in enumerate(accounts):
            account_key = f'account_{i + 1}'
            if account_key in current_balances:
                account_name = account.get_display_name(i)
                info = f'[BALANCE] {account_name}\n💰 Balance: ${current_balances[account_key]["quota"]}, Used: ${current_balances[account_key]["used"]}'
                notification_content.append(info)

    if need_notify and notification_content:
        summary = f'[STATS] Success: {success_count}/{total_count}\n[TIME] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
        full_msg = '\n\n'.join([summary] + notification_content)
        print("--- Sending Notification ---")
        notify.push_message('AnyRouter Check-in Report', full_msg, msg_type='text')
    
    sys.exit(0 if success_count > 0 else 1)

def run_main():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as e:
        print(f'[FATAL] {e}')
        sys.exit(1)

if __name__ == '__main__':
    run_main()
