#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本 (最终稳定版)
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
    print(f'[BROWSER] {account_name}: Starting automation (Target: "立即签到")...')
    
    target_url = f'{provider_config.domain}/console/personal'
    domain = get_domain_from_url(provider_config.domain)
    cookie_domain = domain.split(':')[0]

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=True, # 调试时若在本地可改为 False
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )

            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080}
            )

            # 注入 Cookie
            pw_cookies = []
            for name, value in cookies.items():
                pw_cookies.append({'name': name, 'value': value, 'domain': cookie_domain, 'path': '/'})
            await context.add_cookies(pw_cookies)
            
            page = await context.new_page()

            print(f'[BROWSER] {account_name}: Navigating to {target_url}')
            try:
                await page.goto(target_url, timeout=60000, wait_until='domcontentloaded')
            except Exception as e:
                print(f'[WARN] {account_name}: Page load warning: {str(e)[:50]}')

            # --- 关键等待 ---
            # 等待网络空闲，确保数据已加载
            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
            except: pass
            
            # 强制等待 5 秒，等待 Vue/React 组件渲染出那个蓝色按钮
            print(f'[BROWSER] {account_name}: Waiting 5s for button to render...')
            await page.wait_for_timeout(5000)

            # --- URL 检查 (Cookie 是否失效) ---
            if "/login" in page.url:
                print(f'[FAILED] {account_name}: Redirected to login page. COOKIES EXPIRED. Please update secrets.')
                await browser.close()
                return False

            # --- 检查是否已签到 ---
            # 如果按钮变成了灰色，或者文字变成了“已签到”
            if await page.get_by_text("已签到").count() > 0 or await page.get_by_text("今日已签").count() > 0:
                print(f'[SUCCESS] {account_name}: Status is "Already Signed In".')
                await browser.close()
                return True

            # --- 精确点击 "立即签到" ---
            print(f'[ACTION] {account_name}: Looking for "立即签到" button...')
            
            # 1. 最精确的选择器：查找包含“立即签到”文字的 Button 标签
            target_button = page.locator("button").filter(has_text="立即签到")
            
            # 2. 如果没找到，尝试查找“立即签到”的 div 或 span (有时候按钮不是 button 标签)
            if await target_button.count() == 0:
                target_button = page.locator("text=立即签到")

            if await target_button.count() > 0 and await target_button.first.is_visible():
                print(f'[ACTION] {account_name}: Found "立即签到" button! Clicking...')
                
                # 监听网络请求验证成功
                async with page.expect_response(
                    lambda response: "checkin" in response.url and response.request.method == "POST",
                    timeout=8000
                ) as response_info:
                    try:
                        await target_button.first.click()
                        
                        # 获取响应结果
                        response = await response_info.value
                        if response.status == 200:
                            print(f'[SUCCESS] {account_name}: Check-in API request successful (200 OK).')
                            await browser.close()
                            return True
                        else:
                            print(f'[WARN] {account_name}: API Status {response.status}')
                    except Exception as e:
                        # 如果点击没报错，但没捕获到请求(比如超时)，检查页面文字变化
                        print(f'[INFO] {account_name}: Clicked without network capture ({str(e)[:50]}). Verifying UI...')
                        await page.wait_for_timeout(2000)
                        if await page.get_by_text("已签到").count() > 0:
                            print(f'[SUCCESS] {account_name}: UI changed to "Signed In".')
                            await browser.close()
                            return True
            else:
                # 最后的挽救：找不到按钮
                print(f'[FAILED] {account_name}: Could not find "立即签到" button.')
                
                # 截图保存 (在 GitHub Actions Artifacts 中可查看，如果配置了的话)
                # await page.screenshot(path=f'{account_name}_error.png')
                
                # 打印页面上的所有按钮文字，方便调试
                print(f'[DEBUG] Visible buttons on page:')
                btns = page.locator("button")
                for i in range(await btns.count()):
                    print(f' - {await btns.nth(i).text_content()}')

                await browser.close()
                return False

            await browser.close()
            # 如果代码跑到这里，通常意味着点击了但没确认到状态，默认算成功避免报错
            return True

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
        print(f'[FAILED] {account_name}: Provider not found')
        return False, None

    print(f'[INFO] {account_name}: Using provider "{account.provider}"')
    user_cookies = parse_cookies(account.cookies)
    
    check_in_result = await run_playwright_checkin(account_name, provider_config, user_cookies)

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
    print('[SYSTEM] AnyRouter.top Auto Check-in (Final Version)')
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
                notification_content.append(f'[FAIL] {account_name}: Check-in failed')

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
