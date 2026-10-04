"""設定と同じコマンドの実stdioサーバーとダミーCLIを結合検証する。"""

from contextlib import asynccontextmanager
import importlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from unittest import mock

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / '.orchestration/mcp-launcher-bridge'
DUMMY = ROOT / 'tests/fixtures/dummy_cli.py'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory(dir=ROOT / '.orchestration') as directory:
        yield Path(directory)


@asynccontextmanager
async def connect(workspace):
    configuration = json.loads((ROOT / '.mcp.json').read_text())['mcpServers']
    assert 'mcp-launcher-bridge' in configuration, '実働MCP設定は未実装'
    entry = configuration['mcp-launcher-bridge']
    # .mcp.json はClaude接続用に各開発機の絶対パスを持つため、CIなど別の
    # チェックアウト位置では一致しない。形状(python実行ファイル+server.py)
    # だけを検証し、実起動はこのテストを動かしているインタプリタと
    # このチェックアウトのserver.pyを使う。
    assert Path(entry['command']).name in ('python', 'python3')
    assert len(entry['args']) == 1 and Path(entry['args'][0]).name == 'server.py'
    entry = dict(command=sys.executable, args=[str(BRIDGE / 'server.py')])
    # GitHub Contents API経由の反映では実行ビットが失われるため、実行時に保証する。
    DUMMY.chmod(DUMMY.stat().st_mode | 0o111)
    assert DUMMY.is_file() and os.access(DUMMY, os.X_OK), '実行可能なダミーCLIが必要'
    stdio_module = importlib.import_module('mcp.client.stdio')
    original_spawn = stdio_module._create_platform_compatible_process
    processes = []

    async def capture_process(*args, **kwargs):
        # 実起動はそのままに、SDKの強制終了とサーバー自身の正常終了を区別する。
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    with (workspace / 'server-stderr.log').open('w') as errlog, \
         mock.patch.object(stdio_module, '_create_platform_compatible_process', capture_process), \
         anyio.fail_after(12):
        # ダミーのenv shebangにもvenvを使い、OSの開発ツールラッパーを起動しない。
        environment = {'PATH': str(BRIDGE / 'venv/bin') + os.pathsep + os.environ['PATH']}
        async with stdio_client(StdioServerParameters(**entry, cwd=ROOT, env=environment), errlog=errlog) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                yield session, processes[0]
        assert processes[0].returncode == 0, (workspace / 'server-stderr.log').read_text()


def arguments(workspace, harness, prompt='DUMMY_WAIT', **overrides):
    args = dict(task_id='integration', harness=harness, role='reviewer',
                workspace=str(workspace), prompt=prompt, cancel_grace=1,
                executable_override=str(DUMMY), timeout=10)
    args.update(overrides)
    return args


def identity(workspace, launched):
    return dict(task_id='integration', workspace=str(workspace), job_id=launched['job_id'])


async def call(session, name, args):
    result = await session.call_tool(name, args)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


async def terminal(session, workspace, launched):
    with anyio.fail_after(5):
        while True:
            state = await call(session, 'status', identity(workspace, launched))
            if state['status'] in {'completed', 'failed', 'cancelled', 'timed_out'}:
                return state
            await anyio.sleep(0.02)


async def child_info(workspace):
    path = workspace / 'dummy-cli.json'
    with anyio.fail_after(3):
        while not path.exists():
            await anyio.sleep(0.01)
    return json.loads(path.read_text())


def assert_group_gone(info):
    with pytest.raises(ProcessLookupError):
        os.killpg(info['pgid'], 0)


@pytest.mark.anyio
@pytest.mark.parametrize('harness', ['claude', 'codex'])
async def test_stdio_tools_launch_status_cancel(workspace, harness):
    async with connect(workspace) as (session, process):
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == {'launch', 'status', 'cancel'}
        launched = await call(session, 'launch', arguments(workspace, harness))
        assert launched['status'] == 'running'
        assert await call(session, 'status', identity(workspace, launched)) == launched
        assert await call(session, 'launch', arguments(workspace, harness)) == launched
        info = await child_info(workspace)
        assert not info['stdin_isatty'] and not info['stdout_isatty']
        await call(session, 'cancel', identity(workspace, launched))
        state = await terminal(session, workspace, launched)
        assert state['status'] == 'cancelled'
        assert state['reason'] == 'user_requested'
        assert state['stop_confirmed'] is True
        assert process.returncode is None
        assert_group_gone(info)


@pytest.mark.anyio
@pytest.mark.parametrize('harness', ['claude', 'codex'])
@pytest.mark.parametrize('prompt, expected', [('DUMMY_SUCCESS', 'completed'), ('DUMMY_FAIL', 'failed')])
async def test_stdio_result_collection(workspace, harness, prompt, expected):
    async with connect(workspace) as (session, _):
        launched = await call(session, 'launch', arguments(workspace, harness, prompt))
        state = await terminal(session, workspace, launched)
        assert state['status'] == expected
        if expected == 'completed':
            assert Path(state['response_path']).read_text() == 'DUMMY_OK'
            assert state['exit_code'] == 0
        else:
            assert state['exit_code'] == 17
            assert state['error']


@pytest.mark.anyio
async def test_stdio_timeout(workspace):
    async with connect(workspace) as (session, _):
        launched = await call(session, 'launch', arguments(workspace, 'codex', timeout=0.3))
        state = await terminal(session, workspace, launched)
        assert state['status'] == 'timed_out'
        assert state['reason'] == 'timeout'
        assert state['stop_confirmed'] is True


@pytest.mark.anyio
async def test_invalid_arguments_and_unknown_job_are_mcp_tool_errors(workspace):
    async with connect(workspace) as (session, _):
        args = arguments(workspace, 'codex')
        missing = dict(args)
        del missing['prompt']
        for name, invalid in [('launch', missing), ('launch', dict(args, timeout='bad')),
                              ('launch', dict(args, workspace=str(ROOT.parent))),
                              ('status', dict(task_id='integration', workspace=str(workspace), job_id='missing'))]:
            result = await session.call_tool(name, invalid)
            assert result.is_error


@pytest.mark.anyio
@pytest.mark.parametrize('harness', ['claude', 'codex'])
@pytest.mark.parametrize('termination', ['eof', 'sigterm'])
async def test_server_shutdown_reaps_child_and_exits_in_time(workspace, harness, termination):
    async with connect(workspace) as (session, process):
        launched = await call(session, 'launch', arguments(workspace, harness))
        info = await child_info(workspace)
        if termination == 'sigterm':
            process.send_signal(signal.SIGTERM)
            with anyio.fail_after(4):
                assert await process.wait() == 0
    assert_group_gone(info)
    state_path = workspace / '.orchestration/integration/jobs' / launched['job_id'] / 'state.json'
    state = json.loads(state_path.read_text())
    assert state['status'] == 'cancelled'
    assert state['reason'] == 'shutdown'
    assert state['exit_code'] is not None
    assert state['stop_confirmed'] is True
