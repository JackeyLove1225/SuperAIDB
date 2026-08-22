"""数据守护进程（core/daemon）——运行时隔离层，上层零感知

架构：数据层收进独立 OS 进程（daemon），它是唯一持有数据库句柄与主密钥的
进程；MCP server / 管理 API / 测试等一切调用方经 DaemonDriver（同一套 29 个
驱动接口的 RPC 代理实现）访问——上层换驱动实现即完成切换，业务代码零改动。

安全语义：
- 主密钥只在 daemon 进程内存（从 keyring 取出后不落任何盘/不进任何其他进程）
- 监听只绑 127.0.0.1；每次启动生成随机令牌写入仅当前用户可读的运行文件，
  无令牌不调（本机回环也要有凭据）
- daemon 不在/崩溃：客户端按需自动拉起（免 launcher 也能活）

模块结构：
- protocol：帧格式与编解码（JSON Lines 换行分帧）
- server：daemon 进程入口（python -m core.daemon.server）
- client：DaemonDriver（29 接口 RPC 代理）+ 连接/拉起逻辑
- runtime：运行文件（端口+令牌+pid）的读写与拉起
"""
