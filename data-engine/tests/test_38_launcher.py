"""层 38：launcher 运维面单测（821 行运维关键模块的 CI 覆盖）

判据：单实例锁/身份校验/端口探测/前端可选语义——全部离线可测，不进 CI 黑。
"""
import os
import socket
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_single_instance_lock():
    """单实例锁：拿到→再拿拒绝→释放后可拿；死 PID 残留锁自动回收
    （用临时锁路径——真实锁可能正被运行中的应用持有，测试不得碰它）"""
    from agent.management import launcher as L
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        orig = L._LAUNCHER_LOCK
        L._LAUNCHER_LOCK = Path(tmp) / "launcher.lock"
        try:
            assert L._acquire_single_instance_lock(), "首次应拿到"
            assert not L._acquire_single_instance_lock(), "持锁中第二次应被拒"
            L._release_single_instance_lock()
            assert L._acquire_single_instance_lock(), "释放后应可再拿"
            L._release_single_instance_lock()
            # 残留锁：写入死 PID（999999 不存在）→ 应被识别回收并可拿
            L._LAUNCHER_LOCK.write_text("999999", encoding="utf-8")
            assert L._acquire_single_instance_lock(), "死 PID 残留锁应被回收"
            L._release_single_instance_lock()
        finally:
            L._LAUNCHER_LOCK = orig
    print("OK - 单实例锁：互斥/释放/死 PID 残留回收（临时锁路径隔离）")


def test_process_identity():
    """进程身份校验：当前 python 进程认得出；不存在的 PID 认不出"""
    from agent.management import launcher as L
    assert L._is_project_process(os.getpid()), "当前进程（命令行含本项目）应认出"
    assert not L._is_project_process(999999), "不存在的 PID 应认不出"
    print("OK - 进程身份校验（宁不杀不错杀）")


def _http_ok_server(marker: bytes):
    """拉起一个返回固定身份标识的微型 HTTP 服务（端口就绪判据测试用）"""
    import http.server, threading
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = marker
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_wait_for_port_tcp():
    """就绪判据=HTTP 身份校验（20260824 纯 TCP 探测退役）：
    本服务标识命中 True；纯 TCP 监听（外部占用者）/空闲端口 False"""
    from agent.management import launcher as L
    srv = _http_ok_server(b'{"status":"ok","service":"management-api"}')
    port = srv.server_address[1]
    try:
        assert L._wait_for_port(port, timeout=3), "本服务标识应就绪"
    finally:
        srv.shutdown(); srv.server_close()

    # 纯 TCP 监听（外部占用者，给不出本服务身份）→ 不就绪（防假绿/假活）
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.bind(("127.0.0.1", 0))
    raw.listen(1)
    raw_port = raw.getsockname()[1]
    try:
        assert not L._wait_for_port(raw_port, timeout=2), "外部占用者不得判定就绪"
    finally:
        raw.close()
    assert not L._wait_for_port(port, timeout=2), "关闭后应探测失败"
    print("OK - 端口就绪=HTTP 身份校验（外部占用不假绿，代理陷阱不沾）")


def test_frontend_optional_watch():
    """前端可选语义：frontend_expected=False 时前端端口不拖累判定
    （端口替换为测试临时端口——真实端口可能被运行中的应用占用）"""
    from agent.management import launcher as L
    srv = _http_ok_server(b'{"status":"ok","service":"management-api"}')
    free_mgmt = srv.server_address[1]
    dead_front = 59999  # 空闲端口
    orig = (L.PORT_MGMT, L.PORT_FRONTEND)
    L.PORT_MGMT, L.PORT_FRONTEND = free_mgmt, dead_front
    try:
        # mgmt 端口活着 + 前端缺席：frontend_expected=False → True（不绞杀）
        assert L._check_processes_alive([], frontend_expected=False)
        # frontend_expected=True + 前端缺席 → False（语义保持）
        assert not L._check_processes_alive([], frontend_expected=True)
    finally:
        srv.shutdown(); srv.server_close()
        L.PORT_MGMT, L.PORT_FRONTEND = orig
    print("OK - watchdog 前端可选语义（可选缺失不绞杀健康后端）")


def test_maintenance_flag_lifecycle():
    """维护窗旗标生命周期：stop 置位 → 启动完成须清除（曾只置不清，
    重启后系统被卡在维护态 30 分钟，用户一切写操作报"系统维护中"）"""
    import core.daemon.runtime as rt
    from pathlib import Path
    # 1. runtime 开关行为（隔离临时旗标路径，不碰真实运行文件）
    with tempfile.TemporaryDirectory() as tmp:
        orig = rt.MAINTENANCE_FILE
        rt.MAINTENANCE_FILE = Path(tmp) / "maintenance.flag"
        try:
            rt.set_maintenance(True)
            assert rt.MAINTENANCE_FILE.exists() and rt.in_maintenance()
            rt.set_maintenance(False)
            assert not rt.MAINTENANCE_FILE.exists() and not rt.in_maintenance()
        finally:
            rt.MAINTENANCE_FILE = orig
    # 2. 启动路径契约：启动完成段必须调用 set_maintenance(False)（源码锚定——
    #    stop 注释承诺"文件随下次启动清理"，该承诺必须有代码实体）
    src = Path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "agent", "management", "launcher.py")).read_text(encoding="utf-8")
    seg = src[src.index("SuperAIDB 启动完成"):]
    assert "set_maintenance(False)" in seg, "启动完成路径必须清除维护窗旗标"
    print("OK - 维护窗旗标：置位/清除行为 + 启动完成清除契约（源码锚定）")


if __name__ == "__main__":
    test_single_instance_lock()
    test_process_identity()
    test_wait_for_port_tcp()
    test_frontend_optional_watch()
    test_maintenance_flag_lifecycle()
    print("\n=== LAUNCHER TESTS PASSED ===")
