"""本机 MaiBot 的隔离 Chrome 联调工具（手动执行，不参与 unittest）。

读取本机已有 WebUI 凭据，只通过 CDP 写入测试 profile，不记录凭据。
运行前启动 Chrome --headless --remote-debugging-port=9224，并设置
MAIBOT_INSTANCE_DIR。只输出页面文本、控件和可选截图。
"""
import asyncio
import base64
import json
import sys
import urllib.request
from pathlib import Path

import websockets


async def main():
    from src.webui.core.security import get_token_manager
    manager = get_token_manager()
    cookie = manager.encode_cookie_token(manager.get_token())
    with urllib.request.urlopen("http://127.0.0.1:9224/json/version") as response:
        endpoint = json.load(response)["webSocketDebuggerUrl"]
    async with websockets.connect(endpoint, max_size=10_000_000) as ws:
        seq = 0
        session = None
        events = []

        async def call(method, params=None):
            nonlocal seq
            seq += 1
            payload = {"id": seq, "method": method, "params": params or {}}
            if session:
                payload["sessionId"] = session
            await ws.send(json.dumps(payload))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == seq:
                    if "error" in message:
                        raise RuntimeError(message["error"])
                    return message.get("result", {})
                if message.get("method") == "Network.responseReceived":
                    response = message["params"]["response"]
                    if "/api/webui/plugins" in response["url"]:
                        print("NETWORK", response["status"], response["url"])
                if message.get("method") in {"Runtime.exceptionThrown", "Log.entryAdded"}:
                    events.append(message)

        target = await call("Target.createTarget", {"url": "about:blank"})
        attached = await call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        session = attached["sessionId"]
        await call("Network.enable")
        await call("Runtime.enable")
        await call("Log.enable")
        await call("Network.setCookie", {"name": "maibot_session", "value": cookie, "url": "http://127.0.0.1:8001/", "httpOnly": True})
        await call("Page.enable")
        route = sys.argv[1] if len(sys.argv) > 1 else "/plugins"
        await call("Page.navigate", {"url": "http://127.0.0.1:8001" + route})
        await asyncio.sleep(8)
        for _ in range(10):
            result = await call("Runtime.evaluate", {"expression": "document.body.innerText", "returnByValue": True})
            text = result.get("result", {}).get("value", "")
            if len(text) > 100:
                break
            await asyncio.sleep(1)
        if route == "/plugin-config":
            # 使用 CDP 的 DOM 滚动和原生鼠标点击打开本插件卡片。
            await call("DOM.enable")
            await call("DOM.getDocument")
            found = await call("DOM.performSearch", {"query": "//*[text()='GitHub 仓库订阅通知']"})
            if found["resultCount"]:
                nodes = await call("DOM.getSearchResults", {"searchId": found["searchId"], "fromIndex": 0, "toIndex": 1})
                node = nodes["nodeIds"][0]
                await call("DOM.scrollIntoViewIfNeeded", {"nodeId": node})
                box = (await call("DOM.getBoxModel", {"nodeId": node}))["model"]["content"]
                x, y = (box[0]+box[2])/2, (box[1]+box[5])/2
                await call("Input.dispatchMouseEvent", {"type":"mousePressed", "x":x, "y":y, "button":"left", "clickCount":1})
                await call("Input.dispatchMouseEvent", {"type":"mouseReleased", "x":x, "y":y, "button":"left", "clickCount":1})
                await asyncio.sleep(6)
                result = await call("Runtime.evaluate", {"expression":"document.body.innerText", "returnByValue":True})
                text = result["result"]["value"]
        print(text[:16000])
        controls = await call("Runtime.evaluate", {"expression": "JSON.stringify(Array.from(document.querySelectorAll('a,button,input,[role=combobox],[role=checkbox]')).map(e=>({tag:e.tagName,role:e.getAttribute('role'),text:e.innerText||e.getAttribute('aria-label')||e.getAttribute('placeholder'),href:e.getAttribute('href'),type:e.type})))", "returnByValue": True})
        print("CONTROLS", controls.get("result", {}).get("value", ""))
        print("BROWSER_ERRORS", json.dumps(events, ensure_ascii=False)[:3000])
        if len(sys.argv) > 2:
            shot = await call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            Path(sys.argv[2]).write_bytes(base64.b64decode(shot["data"]))
        await call("Target.closeTarget", {"targetId": target["targetId"]})


if __name__ == "__main__":
    asyncio.run(main())
