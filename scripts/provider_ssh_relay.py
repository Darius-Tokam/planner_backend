"""QA-only HTTPS relay when the workstation's TLS trust chain is unavailable.

The authenticated Railway process calls only DeepSeek, verifies TLS normally,
and reads its own provider credential. It receives synthetic evaluation inputs,
never queries the production database, and writes no files. Transport is buffered:
timings through this relay are NOT provider/streaming latency measurements.
"""
import asyncio
import base64
import json
from pathlib import Path
import shutil
import zlib

import httpx


def model(model_name):
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.deepseek import DeepSeekProvider
    return OpenAIChatModel(model_name, provider=DeepSeekProvider(openai_client=AsyncOpenAI(
        api_key='qa-relay-no-local-credential', base_url='https://api.deepseek.com', max_retries=0,
        http_client=httpx.AsyncClient(transport=RailwayDeepSeekRelay()))))


class RailwayDeepSeekRelay(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        try:
            return await self._relay(request)
        except Exception as exc:
            print('QA relay error:', type(exc).__name__, 'winerror=', getattr(exc, 'winerror', None), flush=True)
            raise

    async def _relay(self, request):
        if request.method != 'POST' or str(request.url) != 'https://api.deepseek.com/chat/completions':
            raise RuntimeError('QA relay only permits the DeepSeek completion endpoint.')
        payload = base64.b64encode(zlib.compress(await request.aread())).decode()
        script = '''import base64,zlib,urllib.request,urllib.error,os,json
body=zlib.decompress(base64.b64decode(PAYLOAD))
req=urllib.request.Request('https://api.deepseek.com/chat/completions',data=body,
 headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY']})
try:
 with urllib.request.urlopen(req,timeout=60) as response:
  print('QA_STATUS:'+str(response.status),flush=True)
  for line in response:
   if line.startswith(b'data:'): print(line.decode('utf-8').strip(),flush=True)
except urllib.error.HTTPError as error:
 print('QA_STATUS:'+str(error.code),flush=True)
'''.replace('PAYLOAD', repr(payload))
        encoded = base64.b64encode(zlib.compress(script.encode())).decode()
        cli = shutil.which('railway.exe') or shutil.which('railway')
        if cli and Path(cli).suffix.lower() in ('.cmd', '.ps1', ''):
            native = Path(cli).parent / 'node_modules/@railway/cli/bin/railway.exe'
            if native.exists():
                cli = str(native)
        if not cli:
            raise RuntimeError('Railway CLI unavailable for authenticated QA relay.')
        process = await asyncio.create_subprocess_exec(cli, 'ssh', '--service',
            'planner-chat-worker', '--', 'python', '-c',
            f"import base64,zlib;exec(zlib.decompress(base64.b64decode('{encoded}')))",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=85)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        lines = stdout.decode('utf-8', errors='replace').splitlines()
        statuses = [int(line.split(':',1)[1]) for line in lines if line.startswith('QA_STATUS:')]
        if process.returncode or not statuses:
            print('QA relay exit:', process.returncode, 'stdout_bytes=', len(stdout), flush=True)
            raise RuntimeError('Authenticated QA relay failed; no provider result.')
        status = statuses[-1]
        body = '\n\n'.join(line for line in lines if line.startswith('data:')) + '\n\n'
        if status != 200:
            body = json.dumps({'error': {'type': 'provider_error', 'message': 'QA provider request failed'}})
        return httpx.Response(status, headers={'content-type':'text/event-stream'}, content=body.encode())
