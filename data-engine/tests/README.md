# tests/ — 测试说明

## 唯一入口：`run_all.py`

```bash
python tests/run_all.py            # 运行全部测试层
python tests/run_all.py --quick    # 只跑离线测试层（CI 用，29 层全绿）
python tests/run_all.py --layer 8  # 只跑指定层
python tests/run_all.py --list     # 列出所有测试层
```

测试层定义在 `run_all.py` 顶部的 `TEST_LAYERS`，每层标注：是否需要
Management API (:2025) / DeepSeek Key、是否纳入 quick、预计耗时。
层级与依赖详见 `run_all.py` 头部 docstring。

## pytest 的地位

各测试文件兼容 pytest collect（`test_*` 函数可被收集），但 pytest
**不是主入口**——层级编排、API 可用性检查、超时控制、退出码口径都由
`run_all.py` 负责。CI 与日常回归一律用 `run_all.py`。

## 新增测试的约定

1. **放哪一层**：按主题归入现有层（见 `run_all.py` docstring 的层级表）；
   新主题才新建 `test_NN_<主题>.py`，层号取未用的两位数。
2. **脚本式 check 模式**：文件内用模块级 `pass_count/fail_count` +
   `check(name, condition, detail)` 累计结果，`__main__` 末尾打印
   `PASS=/FAIL=` 并在有失败时 `sys.exit(1)`（参考 `test_08`）。
   纯函数测试可直接写 `test_*` 函数（参考 `test_12`）。
3. **隔离**：用独立测试行业目录（如 `_test_*`），不碰工程行业数据；
   测试后完整清理（删行业目录、删测试表、切回原行业）。
4. **驱动是 ContractDriver 包装层，无 `conn` 属性**：查询走 `drv.query()`，
   DDL/PRAGMA 走 `drv.execute()`，事务走 `drv.commit()`，
   不要用 `drv.conn.execute(...)`。
5. **登记**：新层要加进 `run_all.py` 的 `TEST_LAYERS`（标注
   needs_api / quick / 预计耗时），并在其头部 docstring 的层级表里补一行。
