#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本 (Playwright 网络拦截版)
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
    使用 Playwright 进行可视化模拟点击签到
    并拦截网络请求以验证是否真正成功
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

            # 注入 Cookies
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

            # 等待网络空闲
            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
            except:
                pass
            
            # --- 关键调试：检查当前 URL ---
            current_url = page.url
            print(f'[DEBUG] {account_name}: Current URL is {current_url}')
            
            if "/login" in current_url:
                print(f'[FAILED] {account_name}: Redirected to login page. Cookies likely invalid or expired.')
                await browser.close()
                return False

            # --- 检查是否已签到 ---
            # 有些站点显示“已签到”，有些是按钮变成灰色
            content = await page.content()
            if "已签到" in content or "今日已签" in content:
                print(f'[SUCCESS] {account_name}: Status is "Already Signed In".')
                await browser.close()
                return True

            # --- 定位按钮 ---
            # 策略：优先找 ID，其次找特定文本的 Button
            # 很多 OneAPI 主题的签到按钮 ID 是 'checkin' 或者 class 包含 checkin
            
            possible_selectors = [
                "text=立即签到",
                "text=每日签到",
                "button:has-text('签到')", # 避免匹配到导航栏的纯文本
                "a.button:has-text('签到')",
                "#checkin", # 常见 ID
                "text=Check in", # 英文
                "text=打卡"
            ]

            target_button = None
            for selector in possible_selectors:
                locator = page.locator(selector)
                count = await locator.count()
                if count > 0:
                    # 过滤掉不可见的元素
                    for i in range(count):
                        if await locator.nth(i).is_visible():
                            target_button = locator.nth(i)
                            print(f'[ACTION] {account_name}: Found button using selector "{selector}"')
                            break
                    if target_button:
                        break
            
            if not target_button:
                print(f'[FAILED] {account_name}: Could not find any valid check-in button.')
                # 打印一下页面标题，确认是不是跑到别的页面了
                print(f'[DEBUG] Page title: {await page.title()}')
                await browser.close()
                return False

            # --- 核心逻辑：点击并监听 API 请求 ---
            # 当点击按钮时，监听是否有发往 /api/user/checkin 的请求
            print(f'[ACTION] {account_name}: Clicking button and waiting for API response...')
            
            # 设置一个标志位
            request_triggered = False
            checkin_success = False
            
            async with page.expect_response(
                lambda response: "checkin" in response.url and response.request.method == "POST",
                timeout=8000
            ) as response_info:
                try:
                    await target_button.click()
                    request_triggered = True
                except Exception as e:
                    print(f'[ERROR] Click failed: {e}')

            if request_triggered:
                try:
                    response = await response_info.value
                    status = response.status
                    print(f'[NETWORK] {account_name}: API Response Status: {status}')
                    
                    if status == 200:
                        try:
                            res_json = await response.json()
                            print(f'[NETWORK] {account_name}: Response Body: {json.dumps(res_json, ensure_ascii=False)}')
                            
                            # 判定逻辑：API 返回成功，或者提示已经签到
                            if res_json.get('success') or res_json.get('data') is True:
                                checkin_success = True
                            elif "已签到" in str(res_json):
                                checkin_success = True
                            else:
                                msg = res_json.get('message', res_json.get('msg', 'Unknown'))
                                print(f'[FAILED] {account_name}: API returned error: {msg}')
                        except:
                            # 某些站点可能不返回 JSON，只返回 200 OK
                            checkin_success = True
                    else:
                        print(f'[FAILED] {account_name}: API returned non-200 status.')
                except Exception as e:
                     print(f'[WARN] {account_name}: Timeout waiting for API response, but button was clicked. ({e})')
                     # 如果超时，回退到检查页面文本
                     await page.wait_for_timeout(2000)
                     content_after = await page.content()
                     if "已签到" in content_after or "成功" in content_after:
                         checkin_success = True
            
            await browser.close()
            
            if checkin_success:
                print(f'[SUCCESS] {account_name}: Check-in verified via Network/UI.')
                return True
            else:
                print(f'[FAILED] {account_name}: Check-in failed (No success signal).')
                return False

        except Exception as e:
            print(f'[ERROR] {account_name}: Playwright unexpected error: {e}')
            return False


def get_user_info(client, headers, user_info_url: str):
    """获取用户信息 (用于显示余额)"""
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
        return {'success': False, 'error': f'Failed to get user info: HTTP {response.status_code}'}
    except Exception as e:
        return {'success': False, 'error': f'Failed to get user info: {str(e)[:50]}...'}


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
    """为单个账号执行签到操作"""
    account_name = account.get_display_name(account_index)
    print(f'\n[PROCESSING] Starting to process {account_name}')

    provider_config = app_config.get_provider(account.provider)
    if not provider_config:
        print(f'[FAILED] {account_name}: Provider "{account.provider}" not found')
        return False, None

    print(f'[INFO] {account_name}: Using provider "{account.provider}"')

    user_cookies = parse_cookies(account.cookies)
    
    # 1. 执行浏览器模拟签到
    check_in_result = await run_playwright_checkin(account_name, provider_config, user_cookies)

    # 2. 查询余额 (仅用于展示)
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
        print(f'[WARN] {account_name}: Failed to fetch balance info: {e}')

    return check_in_result, user_info


async def main():
    print('[SYSTEM] AnyRouter.top Auto Check-in (Network Intercept Mode)')
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
                notification_content.append(f'[FAIL] {account_name}: Check-in verification failed')

            if user_info and user_info.get('success'):
                current_balances[account_key] = {'quota': user_info['quota'], 'used': user_info['used_quota']}

        except Exception as e:
            need_notify = True
            print(f'[ERROR] Account {i+1} exception: {e}')
            notification_content.append(f'[ERROR] Account {i+1}: {str(e)[:50]}')

    # 余额变动检测
    current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
    if current_balance_hash:
        if last_balance_hash is None or current_balance_hash != last_balance_hash:
            balance_changed = True
            need_notify = True
            print('[NOTIFY] Balance change detected')
        
    if current_balance_hash:
        save_balance_hash(current_balance_hash)

    # 汇总通知
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
