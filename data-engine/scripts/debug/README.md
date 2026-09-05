# scripts/debug —— 开发期探针（非产品面，不进 CI）

手工排障用的一次性脚本。**依赖注意**：`_nuke_sdk_*`、`_hist_probe`、
`_mutate_gate_check`、`_thread_state` 等 import `langgraph_sdk`——该包是
图编排时代的调试依赖，已不在 requirements.txt（产品代码零引用）。
要跑这些探针请自行 `pip install langgraph_sdk`；产品回归一律走
`tests/run_all.py`，不依赖本目录任何脚本。
