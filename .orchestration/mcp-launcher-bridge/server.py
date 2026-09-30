"""CLIセッション中常駐する、claude/codex起動用stdio MCPサーバー。"""

import os
import signal
import threading

from mcp.server.mcpserver import MCPServer

from launcher_core import JobManager

server = MCPServer('mcp-launcher-bridge')
shutdown_event = threading.Event()
manager = JobManager(shutdown_event)


@server.tool()
def launch(task_id: str, harness: str, role: str, workspace: str, prompt: str,
           access: str = 'read-only', model: str | None = None,
           reasoning_effort: str | None = None, timeout: float = 600,
           cancel_grace: float = 5, packet: str | None = None,
           executable_override: str | None = None) -> dict:
    """claude/codexを起動し、終了を待たずジョブIDと現在の状態を返す。"""
    return manager.launch(task_id, harness, role, workspace, prompt, access,
                          model, reasoning_effort, timeout, cancel_grace,
                          packet, executable_override)


@server.tool()
def status(task_id: str, workspace: str, job_id: str) -> dict:
    """ジョブの状態を取得する。非オーナーの未完了ジョブはunknownで返す。"""
    return manager.status(task_id, workspace, job_id)


@server.tool()
def cancel(task_id: str, workspace: str, job_id: str) -> dict:
    """所有するジョブの停止を要求する。完了はstatusで確認する。"""
    return manager.cancel(task_id, workspace, job_id)


def main():
    def request_shutdown(signum, frame):
        # シグナルハンドラではロック取得やプロセス待機をしない。
        shutdown_event.set()

    def shutdown_on_signal():
        shutdown_event.wait()
        manager.shutdown_and_wait_all()
        os._exit(0)

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    threading.Thread(target=shutdown_on_signal, daemon=True).start()
    try:
        server.run(transport='stdio')
    finally:
        manager.shutdown_and_wait_all()


if __name__ == '__main__':
    main()
