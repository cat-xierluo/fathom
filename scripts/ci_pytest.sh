#!/usr/bin/env bash
# ISS-031 可复现 pytest 入口（CI 与本地同一断言口径）。
#
# 本地等价命令（ISS-101 后 766 passed = 753 + 13(tests/test_release_gate.py)；753 = 739 + 14(tests/test_upgrade_restore.py)；739 = 727 + 12(ISS-097)，727 = 720 + 7(ISS-096)）：
#   .runtime/bin/python -m pytest tests -q
# CI：FATHOM_PYTHON 指向 setup-python 锁定版本创建的 venv 解释器。
#
# 断言口径：
#   - fathom 源码必须来自当前工作区（TESTING 的防误测要求）；
#   - pytest 非零退出（failed/error/收集失败）经 pipefail 直接判失败；
#   - 通过数必须等于 EXPECTED_PYTEST_PASSED（默认 766）；计数变化必须
#     显式同步本默认值与任务证据，不允许静默漂移。
set -euo pipefail
cd "$(dirname "$0")/.."

# ISS-091 +2：tests/test_api_status_coverage.py 锁定 /api/status
# latest_snapshot 的 dir/denied/vanished 字段契约（697 → 699）；
# ISS-090 +21：tests/test_scan_progress.py 扫描进度流式计数/状态文件/
# live 判活（699 → 720）；
# ISS-096 +7：tests/test_upgrade_helper_exit.py helper 正常退出后实例文件
# 清理的兼容（起始无实例/退出自清两条成功路径 + 损坏/未退出/端口未释放/
# 身份不符四条保守拒绝路径）（720 → 727）；
# ISS-097 +12：tests/test_upgrade_txn_journal.py 升级事务持续停写
# （prepared/installing/installed 各阶段 start_scan 拒绝 + API 409 +
# CLI exit 3 + 恢复后可写）、journal 所有权（重入拒绝字节不变/txn_id
# 唯一/损坏与旧格式 fail-closed + 显式恢复入口）、中断恢复链与 lib.rs
# 独占门/有界等待 Python 钉子（727 → 739）；
# ISS-098 +14：tests/test_upgrade_restore.py 安装后失败的真实旧 bundle
# 与数据恢复——恢复材料合同（③b 落盘/磁盘预算替换前拒绝/--helper-dir
# 缺省跳过）、修前反例转正（N+1 替换后 rollback 恢复旧版文件）、恢复
# 失败明确报错保留材料（形态/身份两层拦截 + 修复后接续）、恢复途中再次
# 中断接续、正常升级材料保留至 finalize 才清、迁移失败闭环（db_verify_
# failed → 停 N+1 → 从备份恢复库，CLI 链与 run_full 建模双钉）、三类
# 故障注入（启动失败/身份不符/迁移失败真实进程链）与 lib.rs 恢复合同
# 钉子（739 → 753）；
# ISS-101 +13：tests/test_release_gate.py 发行门聚焦单测——必需 job 清单
# 与真实 ci.yml 一致（6 实例逐字）+ 矩阵展开与缺 name 回退、解析
# fail-closed 三反例、候选登记/制品指纹四反例（更换/缺制品/commit 不符/
# manifest 版本不符）+ manifest 非法 JSON、非 git 目录阻塞、--selftest
# 全绿（753 → 766）。
expected="${EXPECTED_PYTEST_PASSED:-766}"
py="${FATHOM_PYTHON:-.runtime/bin/python}"

if [ ! -x "$py" ]; then
  echo "缺少可执行解释器: ${py}（本地用 .runtime/bin/python，CI 设 FATHOM_PYTHON）" >&2
  exit 1
fi

"$py" -c 'import os, fathom; src = os.path.realpath(fathom.__file__); here = os.path.realpath(os.getcwd()); assert src.startswith(here + os.sep), f"误测别处源码: {src}"'

out="$(mktemp)"
trap 'rm -f "$out"' EXIT
# pipefail：pytest 自身非零退出（失败/错误/收集失败）在此处直接判失败。
"$py" -m pytest tests -q | tee "$out"

# 只认最终摘要行的通过数；空库误跑（"no tests ran"）会因拿不到计数而失败。
passed="$(grep -oE '[0-9]+ passed' "$out" | tail -1 | grep -oE '[0-9]+')"
if [ "$passed" != "$expected" ]; then
  printf '%s\n' "pytest 通过数 ${passed} != 期望 ${expected}；计数变化需同步 EXPECTED_PYTEST_PASSED 并在任务卡留证据" >&2
  exit 1
fi
printf 'pytest: %s passed (expected %s)\n' "$passed" "$expected"
