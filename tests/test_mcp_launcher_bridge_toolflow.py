"""単一サーバー内の別ツール呼び出しがジョブを引き継ぐことを検証する。"""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / '.orchestration/mcp-launcher-bridge'
sys.path.insert(0, str(BRIDGE))
import launcher_core as core


@pytest.fixture
def bridge():
    path = BRIDGE / 'server.py'
    assert path.is_file(), 'MCPツール層 server.py は未実装'
    spec = importlib.util.spec_from_file_location('launcher_server_toolflow', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(dir=ROOT / '.orchestration') as directory:
        module.manager = core.JobManager(threading.Event(), workspace_root=directory)
        process = mock.Mock(pid=999999)
        process.poll.return_value = None
        with mock.patch.object(core.subprocess, 'Popen', return_value=process) as popen, \
             mock.patch.object(core.JobManager, '_start_monitor'), \
             mock.patch.object(core, 'stop_process_group', return_value=0):
            yield module, directory, process, popen
            module.manager.shutdown_and_wait_all()


def arguments(workspace, harness='codex'):
    return dict(task_id='toolflow', harness=harness, role='reviewer',
                workspace=workspace, prompt='Review the supplied task')


@pytest.mark.parametrize('harness', ['claude', 'codex'])
def test_launch_status_then_completion_across_calls(bridge, harness):
    server, workspace, process, popen = bridge
    launched = server.launch(**arguments(workspace, harness))
    identity = dict(task_id='toolflow', workspace=workspace, job_id=launched['job_id'])
    assert launched['status'] == 'running'
    assert server.status(**identity) == launched
    job = server.manager.registry[launched['job_id']]
    if harness == 'codex':
        (job.directory / 'response.md').write_text('TOOLFLOW_OK')
    else:
        (job.directory / 'stdout.log').write_text(json.dumps({'result': 'TOOLFLOW_OK'}))
    process.poll.return_value = 0
    server.manager._monitor(job)
    finished = server.status(**identity)
    assert finished['status'] == 'completed'
    assert Path(finished['response_path']).read_text() == 'TOOLFLOW_OK'
    assert server.cancel(**identity) == finished
    popen.assert_called_once()


@pytest.mark.parametrize('harness', ['claude', 'codex'])
def test_duplicate_rejected_and_cancel_acknowledged(bridge, harness):
    server, workspace, _, popen = bridge
    args = arguments(workspace, harness)
    launched = server.launch(**args)
    identity = dict(task_id='toolflow', workspace=workspace, job_id=launched['job_id'])
    assert server.launch(**args) == launched
    assert server.cancel(**identity)['job_id'] == launched['job_id']
    job = server.manager.registry[launched['job_id']]
    assert job.cancel_event.is_set()
    server.manager._monitor(job)
    finished = server.status(**identity)
    assert finished['status'] == 'cancelled'
    assert finished['reason'] == 'user_requested'
    assert finished['stop_confirmed'] is True
    assert server.cancel(**identity) == finished
    popen.assert_called_once()


def test_missing_required_argument_raises_tool_error(bridge):
    server, workspace, _, popen = bridge
    args = arguments(workspace)
    del args['prompt']
    with pytest.raises(TypeError):
        server.launch(**args)
    popen.assert_not_called()


def test_unknown_job_id_propagates_as_tool_error(bridge):
    server, workspace, _, _ = bridge
    for tool in (server.status, server.cancel):
        with pytest.raises(ValueError, match='unknown job_id'):
            tool('toolflow', workspace, 'missing')
