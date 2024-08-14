# server.py
import json
import os
import subprocess
import sys
import time

import aiohttp
import requests

from lagent.schema import AgentMessage


class HTTPAgentClient:

    def __init__(self, host='127.0.0.1', port=8090):
        self.host = host
        self.port = port

    @property
    def is_alive(self):
        try:
            resp = requests.get(f'http://{self.host}:{self.port}/health_check')
            return resp.status_code == 200
        except:
            return False

    def __call__(self, message: AgentMessage, session_id: int = 0):
        if isinstance(message, str):
            message = AgentMessage(sender='user', content=message)
        response = requests.post(
            f'http://{self.host}:{self.port}/chat_completion',
            json={
                'message': message.model_dump(),
                'session_id': session_id
            },
            headers={'Content-Type': 'application/json'})
        resp = response.json()
        if response.status_code != 200:
            return resp
        return AgentMessage.model_validate(resp)

    def state_dict(self, session_id: int = 0):
        resp = requests.get(
            f'http://{self.host}:{self.port}/memory/{session_id}')
        return resp.json()


class HTTPAgentServer(HTTPAgentClient):

    def __init__(self, gpu_id, config, host='127.0.0.1', port=8090):
        super().__init__(host, port)
        self.gpu_id = gpu_id
        self.config = config
        self.start_server()

    def start_server(self):
        # set CUDA_VISIBLE_DEVICES in subprocess
        env = os.environ.copy()
        env['CUDA_VISIBLE_DEVICES'] = self.gpu_id
        cmds = [
            sys.executable, 'lagent/distributed/http_serve/app.py', '--host',
            self.host, '--port',
            str(self.port), '--config',
            json.dumps(self.config)
        ]
        self.process = subprocess.Popen(
            cmds,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True)

        while True:
            output = self.process.stdout.readline()
            if not output:  # 如果读到 EOF，跳出循环
                break
            sys.stdout.write(output)  # 打印到标准输出
            sys.stdout.flush()
            if 'Uvicorn running on' in output:  # 根据实际输出调整
                break
            time.sleep(0.1)

    def shutdown(self):
        self.process.terminate()
        self.process.wait()


class AsyncHTTPAgentMixin:

    async def __call__(self, message: AgentMessage, session_id: int = 0):
        if isinstance(message, str):
            message = AgentMessage(sender='user', content=message)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    f'http://{self.host}:{self.port}/chat_completion',
                    json={
                        'message': message.model_dump(),
                        'session_id': session_id
                    },
                    headers={'Content-Type': 'application/json'},
            ) as response:
                resp = await response.json()
                if response.status != 200:
                    return resp
                return AgentMessage.model_validate(resp)


class AsyncHTTPAgentClient(AsyncHTTPAgentMixin, HTTPAgentClient):
    pass


class AsyncHTTPAgentServer(AsyncHTTPAgentMixin, HTTPAgentServer):
    pass
