import os
import json
import asyncio
from datetime import datetime

import requests
from requests import RequestException
from nonebot.internal.matcher.matcher import Matcher
from nonebot.adapters.onebot.v11 import MessageSegment

from . import headers_302, headers_json_302

sora_url = "https://api.302.ai/sora/v2/video"
RETRY_DELAY = 20      # 重试间隔


async def request_sora(sora: type[Matcher], prompt: str):
    """
    调用 302.ai Sora 视频生成接口
    """
    # === 1. 创建视频任务 ===
    payload = json.dumps({
        "model": "sora-2",
        "orientation": "portrait",  # 可选: portrait / landscape
        "prompt": prompt
    })

    try:
        response = requests.post(sora_url, headers=headers_json_302, data=payload, timeout=10)
        result = response.json()
    except RequestException as e:
        await sora.send(f"❌ 请求错误：{e}")
        return

    if result.get("code") != 200:
        await sora.send(f"⚠️ 创建任务失败：{result}")
        return

    task_id = result["data"]["id"]
    await sora.send(f"🎬 Sora 视频生成任务已创建，ID: {task_id}\n请稍等2~5分钟，正在生成视频...")

    # === 2. 轮询任务状态 ===
    url = f"{sora_url}/{task_id}"
    outputs = []

    while not outputs:
        try:
            response = requests.get(url, headers=headers_302, timeout=10)
            result = response.json()

            if result.get("code") == 200:
                data = result["data"]
                outputs = data.get("outputs", [])
                if outputs:
                    await sora.send("✅ 视频生成完成！开始下载...")
                    break
            else:
                await sora.send(f"⚠️ 查询任务失败：{result}")
                return

        except RequestException as e:
            await sora.send(f"⚠️ 查询出错：{e}")

        # await sora.send(f"⏳ 等待 {RETRY_DELAY} 秒后重试...")
        await asyncio.sleep(RETRY_DELAY)

    # === 3. 下载视频 ===
    if outputs:
        video_url = outputs[0]
        await sora.send(f"[Sora] {video_url}")
        await sora.send(MessageSegment.video(video_url))
        
        output_dir = r"./output"
        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        video_file = os.path.join(output_dir, f"{timestamp}.mp4")

        try:
            with requests.get(video_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(video_file, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
        except RequestException as e:
            await sora.send(f"❌ 视频下载失败：{e}")
    else:
        await sora.send("⚠️ 未获取到视频输出 URL。")
