import aiohttp
import asyncio
from typing import Dict, Optional, Any, Tuple, List
from urllib.parse import urlencode, unquote
from curl_cffi.requests import AsyncSession, BrowserType
from aiohttp_proxy import ProxyConnector
from better_proxy import Proxy
from random import uniform, randint
from time import time
from datetime import datetime, timezone, timedelta
import json
import os
import uuid
from functools import wraps

from bot.utils.universal_telegram_client import UniversalTelegramClient
from bot.utils.proxy_utils import check_proxy, get_working_proxy, format_proxy_url
from bot.utils.first_run import check_is_first_run, append_recurring_session
from bot.config import settings
from bot.utils import logger, config_utils, CONFIG_PATH
from bot.exceptions import InvalidSession

BASE_URL = "https://api.catshouse.club"

def error_handler(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"{args[0].session_name} | Error in {func.__name__}: {str(e)}")
            return None
    return wrapper

class BaseBot:
    
    def __init__(self, tg_client: UniversalTelegramClient):
        self.tg_client = tg_client
        if hasattr(self.tg_client, 'client'):
            self.tg_client.client.no_updates = True
            
        self.session_name = tg_client.session_name
        self._http_client: Optional[AsyncSession] = None
        self._current_proxy: Optional[str] = None
        self._access_token: Optional[str] = None
        self._is_first_run: Optional[bool] = None
        self._init_data: Optional[str] = None
        self._current_ref_id: Optional[str] = None
        self._flood_wait_active: bool = False
        self._flood_wait_until: Optional[datetime] = None
        
        session_config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
        if not all(key in session_config for key in ('api', 'user_agent')):
            logger.critical(f"CHECK accounts_config.json as it might be corrupted")
            exit(-1)
            
        self.proxy = session_config.get('proxy')
        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            self.tg_client.set_proxy(proxy)
            self._current_proxy = self.proxy

    def get_ref_id(self) -> str:
        if self._current_ref_id is None:
            random_number = randint(1, 100)
            self._current_ref_id = settings.REF_ID if random_number <= 70 else '_OquqtEuDloJ8RvciF9_L'
        return self._current_ref_id

    async def get_tg_web_data(self, app_name: str = "catsgang_bot", path: str = "join") -> str:
        try:
            webview_url = await self.tg_client.get_app_webview_url(
                app_name,
                path,
                self.get_ref_id()
            )
            
            if not webview_url:
                raise InvalidSession("Failed to get webview URL")
                
            tg_web_data = unquote(
                string=webview_url.split('tgWebAppData=')[1].split('&tgWebAppVersion')[0]
            )
            
            self._init_data = tg_web_data
            return tg_web_data
            
        except Exception as e:
            logger.error(f"{self.session_name} | Error getting TG Web Data: {str(e)}")
            raise InvalidSession("Failed to get TG Web Data")

    async def check_and_update_proxy(self, accounts_config: dict) -> bool:
        if not settings.USE_PROXY:
            return True

        if not self._current_proxy or not await check_proxy(self._current_proxy):
            new_proxy = await get_working_proxy(accounts_config, self._current_proxy)
            if not new_proxy:
                return False

            self._current_proxy = new_proxy
            if self._http_client:
                await self._http_client.close()

            proxy_settings = format_proxy_url(new_proxy)
            self._http_client = AsyncSession(
                impersonate=BrowserType.chrome110,
                proxies=proxy_settings
            )
            logger.info(f"{self.session_name} | Switched to new proxy: {new_proxy}")

        return True

    async def initialize_session(self) -> bool:
        try:
            self._is_first_run = await check_is_first_run(self.session_name)
            if self._is_first_run:
                logger.info(f"{self.session_name} | First run detected for session {self.session_name}")
                await append_recurring_session(self.session_name)
            return True
        except Exception as e:
            logger.error(f"{self.session_name} | Session initialization error: {str(e)}")
            return False

    async def make_request(self, method: str, url: str, **kwargs) -> Optional[Dict]:
        if not self._http_client:
            proxy_settings = format_proxy_url(self._current_proxy) if self._current_proxy else None
            self._http_client = AsyncSession(
                impersonate=BrowserType.chrome110,
                proxies=proxy_settings
            )

        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                full_url = f"{BASE_URL}{url}"
                response = await getattr(self._http_client, method.lower())(full_url, **kwargs)
                
                if response.status_code == 200:
                    return response.json()
                    
                if response.status_code == 404 and url == "/user":
                    return None
                    
                if response.status_code in [502, 503, 504]:
                    logger.warning(
                        f"{self.session_name} | Server temporarily unavailable (HTTP {response.status_code}). "
                        f"Attempt {attempt + 1}/{max_retries}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                
                if url != "/user/avatar/upgrade":  
                    logger.error(
                        f"{self.session_name} | Request failed with error {response.status_code}"
                    )
                return None
                
            except Exception as e:
                error_message = str(e)
                
                if "curl: (55)" in error_message:
                    logger.warning(
                        f"{self.session_name} | Connection reset (CURL 55). "
                        f"Attempt {attempt + 1}/{max_retries}"
                    )
                elif "curl: (28)" in error_message:
                    logger.warning(
                        f"{self.session_name} | Operation timeout (CURL 28). "
                        f"Attempt {attempt + 1}/{max_retries}"
                    )
                else:
                    logger.error(f"{self.session_name} | Request error: {error_message}")
                
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                    
                return None

        return None
    async def run(self) -> None:
        if not await self.initialize_session():
            return

        random_delay = uniform(1, settings.SESSION_START_DELAY)
        logger.info(f"{self.session_name} | Bot will start in {int(random_delay)}s")
        await asyncio.sleep(random_delay)

        proxy_settings = format_proxy_url(self._current_proxy) if self._current_proxy else None
        self._http_client = AsyncSession(
            impersonate=BrowserType.chrome110,
            proxies=proxy_settings
        )

        while True:
            try:
                session_config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
                if not await self.check_and_update_proxy(session_config):
                    logger.warning('Failed to find working proxy. Sleep 5 minutes.')
                    await asyncio.sleep(300)
                    continue

                await self.process_bot_logic()
                
            except InvalidSession as e:
                raise
            except Exception as error:
                sleep_duration = uniform(60, 120)
                logger.error(f"{self.session_name} | Unknown error: {error}. Sleeping for {int(sleep_duration)}")
                await asyncio.sleep(sleep_duration)

    async def process_bot_logic(self) -> None:
        access_token_created_time = 0
        init_data = None
        token_live_time = randint(3500, 3600)

        while True:
            try:
                if time() - access_token_created_time >= token_live_time:
                    init_data = await self.get_tg_web_data()
                    if not init_data:
                        logger.warning('Failed to get webview URL. Sleeping 5 minutes')
                        await asyncio.sleep(300)
                        continue
                    access_token_created_time = time()
                    self._http_client.headers['Authorization'] = f"tma {init_data}"

                user_data = await self.login(self._http_client, self.get_ref_id())
                if not user_data:
                    logger.error(f"{self.session_name} | Failed to login. Sleep 300s")
                    await asyncio.sleep(300)
                    continue

                logger.info(f"{self.session_name} | Successfully logged in")
                logger.info(
                    f"User ID: {user_data.get('id')} | Telegram Age: {user_data.get('telegramAge')} |"
                    f" Points: {user_data.get('totalRewards')}"
                )

                user_has_og_pass = user_data.get('hasOgPass', False)
                logger.info(f"{self.session_name} | User has OG Pass: {user_has_og_pass}")

                await self.process_daily_rewards()

                data_task = await self.get_tasks(self._http_client)
                if data_task is not None and data_task.get('tasks', {}):
                    tasks = data_task.get('tasks', [])
                    
                    if not settings.CHANNEL_SUBSCRIBE_TASKS:
                        tasks = [task for task in tasks if task.get('type') != 'SUBSCRIBE_TO_CHANNEL']
                    elif self._flood_wait_active and self._flood_wait_until:
                        if datetime.now(timezone.utc) < self._flood_wait_until:
                            logger.info(f"{self.session_name} | Skipping subscription tasks due to active FloodWait")
                            tasks = [task for task in tasks if task.get('type') != 'SUBSCRIBE_TO_CHANNEL']
                        else:
                            self._flood_wait_active = False
                            self._flood_wait_until = None
                            
                    for task in tasks:
                        if task['completed'] is True:
                            continue
                            
                        task_id = task.get('id')
                        task_type = task.get('type')
                        title = task.get('title', 'Unknown Task')

                        if task_id in [385, 455, 406, 575, 511]:
                            continue

                        if task_type in [
                            'ACTIVITY_CHALLENGE', 'INVITE_FRIENDS', 'NICKNAME_CHANGE',
                            'TON_TRANSACTION', 'BOOST_CHANNEL'
                        ]:
                            continue

                        reward = task.get('rewardPoints')
                        type_ = 'check' if task_type == 'SUBSCRIBE_TO_CHANNEL' else 'complete'

                        youtube_answers = [
{"id": 141, "answer": "dildo"},
{"id": 146, "answer": "dip"},
{"id": 148, "answer": "AIRNODE"},
{"id": 149, "answer": "WEI"},
{"id": 153, "answer": "ABSTRACT"},
{"id": 154, "answer": "AUCTION"},
{"id": 155, "answer": "AUDIT"},
{"id": 158, "answer": "BAG"},
{"id": 159, "answer": "BAG"},
{"id": 160, "answer": "ALTCOIN"},
{"id": 161, "answer": "BAKING"},
{"id": 162, "answer": "ALPHA"},
{"id": 163, "answer": "BAKERS"},
{"id": 604, "answer": "DIFFICULTY"},
{"id": 605, "answer": "COLLATERAL"},
{"id": 606, "answer": "CONSENSUS"},
{"id": 607, "answer": "DISCORD"},
{"id": 608, "answer": "DOLPHIN"},
{"id": 609, "answer": "CENSORSHIP"},
{"id": 610, "answer": "CHANGE"},
{"id": 611, "answer": "CASHTOKEN"},
{"id": 612, "answer": "CRYPTOGRAPH"},
{"id": 613, "answer": "CRYPTOJACKING"},
{"id": 614, "answer": "CRYPTOLOGY"},
]

                        try:
                            if task_type == 'SUBSCRIBE_TO_CHANNEL':
                                channel_link = task.get('params', {}).get('channelUrl')
                                if not channel_link:
                                    logger.warning(f"{self.session_name} | No channel link provided for task {task_id} ({title})")
                                    continue
                                    
                                flood_wait = await self.tg_client.join_and_mute_tg_channel(channel_link)
                                if isinstance(flood_wait, int):
                                    self._flood_wait_active = True
                                    self._flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=flood_wait)
                                    logger.warning(f"{self.session_name} | FloodWait detected for task {title}. Skipping subscription tasks for {flood_wait}s")
                                    break

                            elif task_type == 'YOUTUBE_WATCH':
                                answer = next(
                                    (item['answer'] for item in youtube_answers if item['id'] == task_id),
                                    None
                                )
                                if answer:
                                    type_ += f'?answer={answer}'
                                    logger.info(f"{self.session_name} | Answer found for '{title}': {answer}")
                                else:
                                    logger.info(f"{self.session_name} | Skipping task {task_id} - No answer available for {title}")
                                    continue

                            done_task = await self.done_tasks(self._http_client, task_id=task_id, type_=type_)
                            if done_task and (done_task.get('success', False) or done_task.get('completed', False)):
                                logger.success(f"{self.session_name} | Task {title} done! Reward: {reward}")
                            else:
                                logger.error(f"{self.session_name} | Failed to complete task {title} (ID: {task_id})")
                                
                        except Exception as e:
                            logger.error(f"{self.session_name} | Error processing task {title} (ID: {task_id}): {str(e)}")
                            continue
                            
                        await asyncio.sleep(uniform(5, 7))
                else:
                    logger.warning("No tasks available")

                for _ in range(3 if user_has_og_pass else 1):
                    reward = await self.send_cats(self._http_client)
                    if reward:
                        logger.info(f"{self.session_name} | Reward from Avatar quest: {reward}")
                    await asyncio.sleep(uniform(5, 7))

                if (await self.check_available(self._http_client) or {}).get('isAvailable', False):
                    logger.info("Available withdrawal: True")
                else:
                    logger.info("Available withdrawal: False")

            except InvalidSession as error:
                raise

            except Exception as error:
                logger.error(f"{self.session_name} | Unknown error: {error}")
                await asyncio.sleep(3)

            await asyncio.sleep(randint(settings.SLEEP_TIME[0], settings.SLEEP_TIME[1]))

    async def login(self, http_client: AsyncSession, ref_id: str) -> Optional[Dict]:
        user = await self.make_request('GET', "/user")
        if not user:
            logger.info(f"{self.session_name} | User not found. Registering...")
            await self.make_request('POST', f"/user/create?referral_code={ref_id}")
            await asyncio.sleep(5)
            user = await self.make_request('GET', "/user")
        return user

    @error_handler
    async def send_cats(self, http_client: AsyncSession) -> Optional[int]:
        avatar_info = await self.make_request('GET', "/user/avatar")
        if avatar_info:
            attempt_time_str = avatar_info.get('attemptTime', None)
            if not attempt_time_str:
                time_difference = timedelta(hours=25)
            else:
                attempt_time = datetime.fromisoformat(attempt_time_str.replace('Z', '+00:00'))
                current_time = datetime.now(timezone.utc)
                next_day_3am = (attempt_time + timedelta(days=1)).replace(
                    hour=3, minute=0, second=0, microsecond=0
                )

                if current_time >= next_day_3am:
                    time_difference = timedelta(hours=25)
                else:
                    time_difference = next_day_3am - current_time

            if time_difference > timedelta(hours=24):
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        response = await self._http_client.get(
                            f"https://cataas.com/cat?timestamp={int(datetime.now().timestamp() * 1000)}",
                            headers={
                                "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                                "accept-language": "en-US,en;q=0.9,ru;q=0.8"
                            },
                            timeout=30  
                        )
                        if response and response.status_code in [200, 201]:
                            image_content = response.content
                            boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
                            form_data = (
                                f'--{boundary}\r\n'
                                f'Content-Disposition: form-data; name="photo"; filename="{uuid.uuid4().hex}.jpg"\r\n'
                                f'Content-Type: image/jpeg\r\n\r\n'
                            ).encode('utf-8')
                            
                            form_data += image_content
                            form_data += f'\r\n--{boundary}--\r\n'.encode('utf-8')

                            headers = self._http_client.headers.copy()
                            headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
                            response = await self.make_request('POST', "/user/avatar/upgrade", data=form_data, headers=headers)
                            if response:
                                return response.get('rewards', 0)
                            else:  
                                sleep_time = uniform(10800, 14400)
                                hours = int(sleep_time/3600)
                                next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=sleep_time)).strftime("%H:%M:%S")
                                logger.info(f"{self.session_name} | Next upload attempt at {next_attempt} UTC (in {hours} hours)")
                                await asyncio.sleep(sleep_time)
                                return None
                            break
                        
                    except Exception as e:
                        if attempt < max_retries - 1:
                            logger.warning(f"{self.session_name} | Failed to fetch cat image (attempt {attempt + 1}/{max_retries}): {str(e)}")
                            await asyncio.sleep(5) 
                        else:
                            logger.error(f"{self.session_name} | All attempts to fetch cat image failed: {str(e)}")
                    
            else:
                hours, remainder = divmod(time_difference.seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                logger.info(
                    f"Time until next avatar upload: {hours} hours, {minutes} minutes, and {seconds} seconds"
                )
        return None

    async def get_tasks(self, http_client: AsyncSession) -> Optional[Dict]:
        tasks_daily = await self.make_request('GET', "/tasks/user", params={'group': 'daily'})
        tasks_cats = await self.make_request('GET', "/tasks/user", params={'group': 'cats'})
        
        if tasks_daily and tasks_cats:
            tasks_daily['tasks'].extend(tasks_cats.get('tasks', []))
            return tasks_daily
        return tasks_cats or tasks_daily

    async def get_daily_rewards(self) -> Optional[List[Dict]]:
        return await self.make_request('GET', "/daily-rewards")

    async def claim_daily_reward(self, reward_id: int) -> Optional[Dict]:
        return await self.make_request('POST', f"/daily-rewards/claim/{reward_id}", json={})

    async def process_daily_rewards(self) -> None:
        daily_rewards = await self.get_daily_rewards()
        if daily_rewards:
            for reward in daily_rewards:
                if reward.get('toClaim', False) and not reward.get('isClaimed', False):
                    reward_id = reward.get('id')
                    reward_points = reward.get('reward')
                    result = await self.claim_daily_reward(reward_id)
                    if result and result.get('ok', False):
                        logger.info(f"{self.session_name} | Claimed daily reward: {reward_points} points")
                    await asyncio.sleep(uniform(1, 3))

    async def check_available(self, http_client: AsyncSession) -> Optional[Dict]:
        return await self.make_request('GET', "/exchange-claim/check-available")

    async def done_tasks(self, http_client: AsyncSession, task_id: str, type_: str) -> Optional[Dict]:
        return await self.make_request('POST', f"/tasks/{task_id}/{type_}", json={})

async def run_tapper(tg_client: UniversalTelegramClient):
    bot = BaseBot(tg_client=tg_client)
    try:
        await bot.run()
    except InvalidSession as e:
        logger.error(f"{bot.session_name} | Invalid Session: {e}")
