"""daemon 通信协议——JSON Lines（每帧一行 JSON，UTF-8）

请求：{"token": str, "session": str, "method": str, "args": dict}
响应：{"ok": true, "result": <json>} / {"ok": false, "error": str, "error_kind": str}

设计取舍：文本帧 + 每请求一个短连接——实现极简、可 curl 调试（白盒哲学），
数据面是管理级流量（非高吞吐流式），短连接开销可忽略。
值编码：bytes → {"__daemon_bytes__": base64}；其余走 JSON 默认（失败如实报错）。
"""
import base64
import json
import socket


def _default(o):
    if isinstance(o, (bytes, bytearray)):
        return {"__daemon_bytes__": base64.b64encode(bytes(o)).decode("ascii")}
    raise TypeError(f"不可 JSON 序列化的类型: {type(o).__name__}")


def _hook(o):
    if isinstance(o, dict) and set(o.keys()) == {"__daemon_bytes__"}:
        return base64.b64decode(o["__daemon_bytes__"])
    return o


def encode(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, default=_default) + "\n").encode("utf-8")


def decode(data: bytes):
    return json.loads(data.decode("utf-8"), object_hook=_hook)


class DaemonError(RuntimeError):
    """daemon 调用失败——带 error_kind 供调用方程序化区分（auth/method/args/异常类）"""
    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


def rpc_call(port: int, token: str, method: str, args: dict,
             session: str = "", timeout: float = 120.0):
    """单次 RPC：短连接一问一答。失败抛 ConnectionError/RuntimeError。"""
    req = {"token": token, "session": session, "method": method, "args": args}
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(encode(req))
        buf = b""
        while True:
            chunk = sock.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
    if not buf:
        raise ConnectionError("daemon 无响应（连接被关闭）")
    resp = decode(buf.split(b"\n", 1)[0])
    if not resp.get("ok"):
        raise DaemonError(resp.get("error", "daemon 执行失败"), resp.get("error_kind", ""))
    return resp
