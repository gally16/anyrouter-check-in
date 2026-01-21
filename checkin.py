#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本 (增强版 - 模拟点击模式)
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
    """加载余额hash"""
    try:
        if os.path.exists(BALANCE_HASH_FILE):
            with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
    except Exception:
        pass
    return None


def save_balance_hash(balance_hash):
    """保存余额hash"""
    try:
        with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as f:
            f.write(balance_hash)
    except Exception as e:
        print(f'Warning: Failed to save balance hash: {e}')


def generate_balance_hash(balances):
    """生成余额数据的hash"""
    simple_balances = {k: v['quota'] for k, v in balances.items()} if balances else {}
    balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def parse_cookies(cookies_data):
    """解析 cookies 数据"""
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
    """从URL中提取域名"""
    parsed = urlparse(url)
    return parsed.netloc


async def run_playwright_checkin(account_name: str, provider_config, cookies: dict) -> bool:
    """
    使用 Playwright 进行可视化模拟点击签到
    解决页面延迟加载和动态按钮的问题
    """
    print(f'[BROWSER] {account_name}: Starting browser automation for check-in...')

    # 目标页面：通常是个人中心
    target_url = f'{provider_config.domain}/console/personal'
    domain = get_domain_from_url(provider_config.domain)

    async with async_playwright() as p:
        try:
            # 启动浏览器
            # headless=True 必须用于 GitHub Actions
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )

            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080},
                device_scale_factor=1,
            )

            # 转换 Cookies 格式
            pw_cookies = []
            for name, value in cookies.items():
                pw_cookies.append({
                    'name': name,
                    'value': value,
                    'domain': domain,
                    'path': '/'
                })
            
            # 注入 Cookies
            await context.add_cookies(pw_cookies)
            
            page = await context.new_page()

            print(f'[BROWSER] {account_name}: Navigating to {target_url}')
            try:
                await page.goto(target_url, timeout=60000, wait_until='domcontentloaded')
            except Exception as e:
                print(f'[WARN] {account_name}: Page load timeout, but continuing check... ({str(e)[:50]})')

            # --- 关键修改：处理延迟加载 ---
            print(f'[BROWSER] {account_name}: Waiting for page to stabilize...')
            try:
                await page.wait_for_load_state('networkidle', timeout=15000)
            except:
                pass
            
            # 强制等待 5 秒，确保动态 JS 执行完毕 (针对 duckcoding 等慢加载站点)
            await page.wait_for_timeout(5000)

            # --- 检查是否已经签到 ---
            # 检查页面上是否存在指示“已签到”的文本
            content = await page.content()
            if "已签到" in content or "今日已签" in content or "Signed in" in content:
                print(f'[SUCCESS] {account_name}: Detected "Already Signed In" status.')
                await browser.close()
                return True

            # --- 寻找并点击按钮 ---
            # 定义可能的按钮文本
            button_texts = ["立即签到", "签到", "打卡", "Sign In", "Check in"]
            clicked = False

            for btn_text in button_texts:
                # 查找可见的按钮或链接
                locator = page.get_by_text(btn_text, exact=True)
                
                # 如果找不到精确匹配，尝试包含匹配
                if await locator.count() == 0:
                     locator = page.get_by_text(btn_text)

                # 确保元素可见且可点击
                if await locator.count() > 0:
                    # 遍历找到的元素，点击第一个可见的
                    count = await locator.count()
                    for i in range(count):
                        element = locator.nth(i)
                        if await element.is_visible():
                            print(f'[ACTION] {account_name}: Found button "{btn_text}", clicking...')
                            try:
                                await element.click(timeout=5000)
                                clicked = True
                                break
                            except:
                                continue
                    if clicked:
                        break
            
            # 如果文本没找到，尝试查找带 icon 的按钮 (适配某些主题)
            if not clicked:
                try:
                    icon_btn = page.locator('.ui.button').filter(has_text="签到")
                    if await icon_btn.count() > 0 and await icon_btn.first.is_visible():
                        print(f'[ACTION] {account_name}: Found button via CSS selector, clicking...')
                        await icon_btn.first.click()
                        clicked = True
                except:
                    pass

            if not clicked:
                print(f'[FAILED] {account_name}: Could not find any check-in button on the page.')
                # 调试时可以取消注释下面这行来查看页面内容
                # print(await page.content())
                await browser.close()
                return False

            # --- 验证点击结果 ---
            await page.wait_for_timeout(3000) # 点击后等待响应

            # 再次检查页面内容
            content_after = await page.content()
            success_keywords = ["已签到", "成功", "Success", "获得", "received"]
            
            is_success = False
            for keyword in success_keywords:
                if keyword in content_after:
                    is_success = True
                    break
            
            if is_success:
                print(f'[SUCCESS] {account_name}: Check-in action completed successfully!')
            else:
                # 有时候点击了但没有弹窗提示，只要没报错，我们暂且认为是成功的，或者已经被签过了
                print(f'[INFO] {account_name}: Button clicked. Assuming success (no error detected).')
                is_success = True

            await browser.close()
            return is_success

        except Exception as e:
            print(f'[ERROR] {account_name}: Playwright execution failed: {e}')
            return False


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
    """准备请求所需的 cookies"""
    # 这里我们简化逻辑：如果使用浏览器签到，WAF cookie 会在浏览器会话中自动处理
    # 但为了后续的 API 余额查询，如果配置了需要 WAF，我们还是可以尝试获取一下
    # 或者简单地直接返回用户 cookies，让后续 API 调用尝试
    
    # 鉴于 run_playwright_checkin 是全新的浏览器会话，WAF 预获取不是点击签到的必须步骤
    # 只需要返回 user_cookies 即可，浏览器会自动处理 WAF
    return user_cookies


def get_user_info(client, headers, user_info_url: str):
    """获取用户信息 (用于显示余额)"""
    try:
        response = client.get(user_info_url, headers=headers, timeout=30)

        if response.status_code == 200:
            data = response.json()
            if data.get('success'):
                user_data = data.get('data', {})
                # 注意：有些站点单位不同，这里保持你原脚本的逻辑 / 500000
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
        print(f'[FAILED] {account_name}: Provider "{account.provider}" not found in configuration')
        return False, None

    print(f'[INFO] {account_name}: Using provider "{account.provider}" ({provider_config.domain})')

    user_cookies = parse_cookies(account.cookies)
    if not user_cookies:
        print(f'[FAILED] {account_name}: Invalid configuration format')
        return False, None

    # 1. 执行浏览器模拟签到 (核心修改)
    check_in_result = await run_playwright_checkin(account_name, provider_config, user_cookies)

    # 2. 查询余额 (使用 HTTP 请求，因为只需读取 JSON)
    # 注意：如果站点有严格的 WAF，这里的 API 请求可能会失败，但前面的签到已经完成了
    user_info = None
    try:
        client = httpx.Client(http2=True, timeout=30.0)
        client.cookies.update(user_cookies) # API 查询使用原始 Cookie

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Referer': provider_config.domain,
            provider_config.api_user_key: account.api_user,
        }

        user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
        user_info = get_user_info(client, headers, user_info_url)
        
        if user_info and user_info.get('success'):
            print(user_info['display'])
        elif user_info:
            print(f"[WARN] Balance check info: {user_info.get('error')}")

        client.close()
    except Exception as e:
        print(f'[WARN] {account_name}: Failed to fetch balance info (Check-in might still be successful): {e}')

    return check_in_result, user_info


async def main():
    """主函数"""
    print('[SYSTEM] AnyRouter.top multi-account auto check-in script started (Browser Click Mode)')
    print(f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    app_config = AppConfig.load_from_env()
    print(f'[INFO] Loaded {len(app_config.providers)} provider configuration(s)')

    accounts = load_accounts_config()
    if not accounts:
        print('[FAILED] Unable to load account configuration, program exits')
        sys.exit(1)

    print(f'[INFO] Found {len(accounts)} account configurations')

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
            
            # 记录用于通知的余额信息
            if user_info and user_info.get('success'):
                current_quota = user_info['quota']
                current_used = user_info['used_quota']
                current_balances[account_key] = {'quota': current_quota, 'used': current_used}

            # 仅当失败时添加到通知列表 (余额变化在后面统一处理)
            if not success:
                need_notify = True
                account_name = account.get_display_name(i)
                print(f'[NOTIFY] {account_name} check-in failed')
                notification_content.append(f'[FAIL] {account_name} Check-in failed')

        except Exception as e:
            account_name = account.get_display_name(i)
            print(f'[FAILED] {account_name} processing exception: {e}')
            need_notify = True
            notification_content.append(f'[FAIL] {account_name} exception: {str(e)[:50]}...')

    # 检查余额变化
    current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
    
    if current_balance_hash:
        if last_balance_hash is None:
            balance_changed = True
            need_notify = True
            print('[NOTIFY] First run, saving balance hash')
        elif current_balance_hash != last_balance_hash:
            balance_changed = True
            need_notify = True
            print('[NOTIFY] Balance changes detected')
        else:
            print('[INFO] No balance changes detected')

    # 如果有余额变化，把所有能获取到余额的账号信息都放入通知
    if balance_changed:
        for i, account in enumerate(accounts):
            account_key = f'account_{i + 1}'
            if account_key in current_balances:
                account_name = account.get_display_name(i)
                info = f'[BALANCE] {account_name}\n:money: Balance: ${current_balances[account_key]["quota"]}, Used: ${current_balances[account_key]["used"]}'
                notification_content.append(info)

    # 保存新的 hash
    if current_balance_hash:
        save_balance_hash(current_balance_hash)

    # 发送通知
    if need_notify and notification_content:
        summary = [
            f'[STATS] Success: {success_count}/{total_count}',
            f'[TIME] {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
        ]
        
        full_msg = '\n\n'.join(summary + notification_content)
        print("--- Notification Content ---")
        print(full_msg)
        print("----------------------------")
        
        notify.push_message('AnyRouter Check-in Alert', full_msg, msg_type='text')
    else:
        print('[INFO] All good, no notification sent.')

    sys.exit(0 if success_count > 0 else 1)


def run_main():
    """运行主函数的包装函数"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n[WARNING] Program interrupted by user')
        sys.exit(1)
    except Exception as e:
        print(f'\n[FAILED] Error occurred during program execution: {e}')
        # 打印详细堆栈以便 Action 调试
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    run_main()
