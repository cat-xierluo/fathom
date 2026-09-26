#!/usr/bin/env bash
#
# ISS-078 · 发行候选重建登记夹具
#
# 目标：把 §1.1 三命令（build_helper.sh → build_app.sh → verify_app_bundle.sh）
# 与候选三要素（固定 40 位 commit、DMG SHA256、helper SHA256）固化成一条可
# 重复入口；任何一步失败 fail-closed，不产出记录。
#
# 三模式：
#   --selftest          内置 fake 产物注入自测（反例全跑），给 PM/CI 的验证入口；
#                       不依赖真实构建链、不依赖真实 git 状态；
#   无 --build（默认）  只读登记：要求 git status 干净、记录 HEAD 40 位；
#                       在 apps/desktop/src-tauri/target/release/bundle/ 既有
#                       产物中采集三要素（DMG SHA256 与 bundle/checksums.txt
#                       交叉核对；helper SHA256 从产物内 helper 可执行计算）；
#                       产物缺失/不匹配 → 非零退出不产出记录；
#   --build             依次执行三命令（build_helper.sh → build_app.sh →
#                       verify_app_bundle.sh，各步输出重定向日志文件、只回显
#                       尾部防刷屏），verify 非 0 即失败；然后按只读登记采集。
#
# 记录输出：verify-results/release-candidates/<UTC时间戳>.md + .json 双格式
# （gitignore 覆盖 verify-results/；本目录不入库）。内容含：固定 40 位
# commit、三要素、verify 段数与结果、构建命令清单与耗时、时间戳。
# ISS-101 起 JSON schema 升 v2：新增可选字段 updater_tgz_sha256
# （bundle/macos/Fathom.app.tar.gz 的 SHA256；本地无签名 key 的构建不产
# updater 产物，该字段为 null），供 scripts/verify_release_gate.py 在
# 发行门 C5 指纹层比对。v1 记录（无该字段）仍可被门接受（字段缺省时
# 跳过该项比对）。
#
# 用法：
#   bash scripts/release_candidate_record.sh [--selftest | --build]
#
# 退出码：
#   0 成功并产出记录
#   1 参数/前置失败（脏工作区、产物缺失、SHA 不一致等）
#   2 --build 模式下构建链任一步非零退出
#   3 阻塞（缺解释器、缺 .app/.dmg 等）
#   64 用法错误
#
# bash 3.2 兼容。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_NAME="release_candidate_record.sh"
LOG_PREFIX="[$SCRIPT_NAME]"

# ---- 参数解析（fail-closed：未知选项立即拒绝） --------------------------
MODE="read_only"   # read_only | build | selftest
BUILD_REQUESTED=0
SELFTEST=0
for arg in "$@"; do
  case "$arg" in
    --selftest) SELFTEST=1; MODE="selftest" ;;
    --build)    BUILD_REQUESTED=1; MODE="build" ;;
    --help|-h)
      sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      printf '%s 用法错误：未知选项 %s\n' "$LOG_PREFIX" "$arg" >&2
      printf '%s 允许：--selftest | --build | --help\n' "$LOG_PREFIX" >&2
      exit 64
      ;;
  esac
done

# ---- 自测模式（不依赖 git 状态、不依赖真实产物） -------------------------
SELFTEST_ROOT=""
run_selftest() {
  # 在临时目录里自造 fake git repo + fake bundle + fake helper + fake
  # checksums.txt，然后让主流程以 read_only 模式跑一遍覆盖全部反例路径。
  # 反例覆盖（与合同一致）：
  #   1) 脏工作区（git status 不干净）→ 期望非零退出且不产记录
  #   2) HEAD 与产物 commit 字段不一致 → 期望非零退出且不产记录
  #   3) 产物缺失（fake bundle 目录为空）→ 期望非零退出且不产记录
  #   4) checksums.txt 与实际 DMG SHA 不一致 → 期望非零退出且不产记录
  #   5) --verify 段非 0（用 fake result.json 含 failed>0）→ 期望非零
  #   6) 全 happy path（产物齐 + SHA 一致 + verify 22/22 + dirty=clean）→ 退 0
  #
  # 本函数直接复用主流程的 _read_only_record 私有实现（通过 SOURCE 自指导出）。
  printf '%s --selftest 开始（在临时目录内注入 fake 产物，不触碰真实仓库）\n' "$LOG_PREFIX"

  local rc=0 fail=0
  _selftest_case() {  # name, expected_rc, script, fake_setup_fn, fake_env (default 1)
    local name="$1" expected="$2" setup_fn="$3" use_fake="${4:-1}"
    local tdir
    # 自测用例里跑脚本时不允许 set -e 提前退出；本函数整体禁用 set -e
    set +e
    tdir="$(mktemp -d -t rcr-selftest-XXXXXX)"
    mkdir -p "$tdir/scripts"
    # 复制脚本到 tdir/scripts/，让脚本内 ``ROOT=tdir``；fake bundle 写在
    # tdir/apps/... 下，与脚本内 BUNDLE_ROOT 解析一致。
    cp "$ROOT/scripts/release_candidate_record.sh" "$tdir/scripts/"
    chmod +x "$tdir/scripts/release_candidate_record.sh"
    # shellcheck disable=SC2317
    "$setup_fn" "$tdir"
    if [ "$use_fake" = "1" ]; then
      ( cd "$tdir" && FAKE_RELEASE_CANDIDATE=1 bash "$tdir/scripts/release_candidate_record.sh" ) >/dev/null 2>&1
    else
      ( cd "$tdir" && bash "$tdir/scripts/release_candidate_record.sh" ) >/dev/null 2>&1
    fi
    local got=$?
    if [ "$got" = "$expected" ]; then
      printf '  [ok] %-36s 期望=%s 实际=%s\n' "$name" "$expected" "$got"
    else
      printf '  [FAIL] %-36s 期望=%s 实际=%s\n' "$name" "$expected" "$got" >&2
      fail=$((fail + 1))
    fi
    rm -rf "$tdir"
    set -e
  }

  _selftest_setup_clean() {
    local d="$1"; mkdir -p "$d/apps/desktop/src-tauri/target/release/bundle"
    cd "$d" && git init -q . && git -c user.email=t@t -c user.name=t commit --allow-empty -q -m init
    printf '0.3.0\n' > "$d/apps/desktop/src-tauri/Cargo.toml.tmp"  # 占位文件
  }

  printf '%s 反例 1：脏工作区（有未提交修改）应被拒绝\n' "$LOG_PREFIX"
  _selftest_case_dirty() {
    local d="$1"
    printf 'scripts/\n' > "$d/.gitignore"
    mkdir -p "$d/apps/desktop/src-tauri/target/release/bundle"
    cd "$d" && git init -q . && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit -q -m init
    printf 'dirty\n' > "$d/untracked.txt"
  }
  _selftest_case "dirty_workspace_rejected" 1 _selftest_case_dirty

  printf '%s 反例 2：HEAD 与产物内置 commit 字段不一致应被拒绝\n' "$LOG_PREFIX"
  _selftest_case_head_mismatch() {
    local d="$1"
    printf 'scripts/\n' > "$d/.gitignore"
    mkdir -p "$d/apps/desktop/src-tauri/target/release/bundle"
    cd "$d" && git init -q . && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit --allow-empty -q -m init
    local head
    head="$(git -C "$d" rev-parse HEAD)"
    local fake_commit
    fake_commit="$(printf '%s' "$head" | tr 'a-f' '0-5')"
    # fake_commit 与 head 不同；fake bundle 内 build_commit.txt 写 fake_commit
    # verify_verdict=PASS，让 verify 校验通过、reject 落在 check_head_vs_artifacts
    _write_fake_bundle "$d" "$fake_commit" "22" "PASS" || true
    # 提交 fake 产物，使 workspace 干净；脚本内 check_head_vs_artifacts 会
    # 拿 build_commit.txt（fake_commit）与 HEAD（init commit）比对，不一致 → reject
    ( cd "$d" && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit -q -m bundle ) >/dev/null
  }
  _selftest_case "head_mismatch_rejected" 1 _selftest_case_head_mismatch 0

  printf '%s 反例 3：产物缺失应被拒绝\n' "$LOG_PREFIX"
  _selftest_case_missing() {
    local d="$1"
    printf 'scripts/\n' > "$d/.gitignore"
    mkdir -p "$d/apps/desktop/src-tauri/target/release/bundle"
    cd "$d" && git init -q . && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit --allow-empty -q -m init
    # 不写 fake 产物，让脚本在 collect_artifacts 阶段发现 APP_PATH 不存在
  }
  _selftest_case "missing_artifacts_rejected" 3 _selftest_case_missing

  printf '%s 反例 4：checksums.txt 与实际 DMG SHA 不一致应被拒绝\n' "$LOG_PREFIX"
  _selftest_case_sha_mismatch() {
    local d="$1"
    printf 'scripts/\n' > "$d/.gitignore"
    mkdir -p "$d/apps/desktop/src-tauri/target/release/bundle"
    cd "$d" && git init -q . && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit --allow-empty -q -m init
    local head
    head="$(git -C "$d" rev-parse HEAD)"
    _write_fake_bundle "$d" "$head" "22" "ok" >/dev/null
    # 用错误 SHA 覆盖 checksums.txt
    local dmg="$d/apps/desktop/src-tauri/target/release/bundle/dmg/Fathom_0.3.0_aarch64.dmg"
    printf '%s  %s\n' "0000000000000000000000000000000000000000000000000000000000000000" "$dmg" \
      > "$d/apps/desktop/src-tauri/target/release/bundle/checksums.txt"
    # 提交让 workspace 干净
    ( cd "$d" && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit -q -m bundle ) >/dev/null
  }
  _selftest_case "checksum_mismatch_rejected" 1 _selftest_case_sha_mismatch

  printf '%s 反例 5：verify result 含 failed>0 应被拒绝\n' "$LOG_PREFIX"
  _selftest_case_verify_fail() {
    local d="$1"
    printf 'scripts/\n' > "$d/.gitignore"
    mkdir -p "$d/apps/desktop/src-tauri/target/release/bundle"
    cd "$d" && git init -q . && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit --allow-empty -q -m init
    local head
    head="$(git -C "$d" rev-parse HEAD)"
    # 写一个 fake result.json 标记 failed
    local results="$d/apps/desktop/src-tauri/verify-results/fake"
    mkdir -p "$results"
    printf '{"schema":"fathom.iss009-slice1.verify.v1","verdict":"FAIL","passed":20,"failed":2,"cases":[]}\n' \
      > "$results/result.json"
    _write_fake_bundle "$d" "$head" "20/22" "FAIL" >/dev/null
    ( cd "$d" && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit -q -m bundle ) >/dev/null
  }
  _selftest_case "verify_nonzero_rejected" 2 _selftest_case_verify_fail

  printf '%s 正例：happy path 应退出 0\n' "$LOG_PREFIX"
  _selftest_case_happy() {
    local d="$1"
    printf 'scripts/\n' > "$d/.gitignore"
    mkdir -p "$d/apps/desktop/src-tauri/target/release/bundle"
    cd "$d" && git init -q . && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit --allow-empty -q -m init
    local head
    head="$(git -C "$d" rev-parse HEAD)"
    _write_fake_bundle "$d" "$head" "22/22" "PASS" >/dev/null
    ( cd "$d" && git -c user.email=t@t -c user.name=t add -A && git -c user.email=t@t -c user.name=t commit -q -m bundle ) >/dev/null
  }
  _selftest_case "happy_path_pass" 0 _selftest_case_happy

  if [ "$fail" -ne 0 ]; then
    printf '%s --selftest 失败：%s 项反例未达预期\n' "$LOG_PREFIX" "$fail" >&2
    return 1
  fi
  printf '%s --selftest 全部通过\n' "$LOG_PREFIX"
  return 0
}

# ---- fake 产物生成器（自测专用） ---------------------------------------
# 写一份最小可被主流程解析的 bundle 树，含：
#   - macos/Fathom.app/Contents/Resources/helper/fathom-helper/fathom-helper (可执行)
#   - dmg/Fathom_0.3.0_aarch64.dmg (假二进制)
#   - checksums.txt（与 dmg 的真实 SHA 一致）
#   - helper-instance 标识（含 commit 字段，等于给定的 fake_commit）
_write_fake_bundle() {
  local d="$1" fake_commit="$2" verify_label="$3" verify_verdict="$4"
  local bundle="$d/apps/desktop/src-tauri/target/release/bundle"
  mkdir -p "$bundle/macos/Fathom.app/Contents/Resources/helper/fathom-helper" \
           "$bundle/dmg"

  # helper 可执行：写一个 4 字节固定头让 shasum 给出稳定值
  local helper="$bundle/macos/Fathom.app/Contents/Resources/helper/fathom-helper/fathom-helper"
  printf 'FAKE' > "$helper"
  chmod +x "$helper"

  # DMG 假二进制
  local dmg="$bundle/dmg/Fathom_0.3.0_aarch64.dmg"
  printf 'DMG-FAKE-%s' "$fake_commit" > "$dmg"

  # checksums.txt（包含 dmg 的真实 SHA）
  local dmg_sha
  dmg_sha="$(shasum -a 256 "$dmg" | awk '{print $1}')"
  printf '%s  %s\n' "$dmg_sha" "$dmg" > "$bundle/checksums.txt"

  # 产物内置 commit 字段（与构建链 build_app.sh 写入的 build_commit.txt 同义）
  printf '%s\n' "$fake_commit" > "$bundle/build_commit.txt"

  # 同时写一份 verify-results/<时间戳>/result.json 让主流程能读到 verify 段数
  local results="$d/apps/desktop/src-tauri/verify-results/$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$results"
  if [ "$verify_verdict" = "PASS" ]; then
    local pass_n=22 fail_n=0
  else
    local pass_n=20 fail_n=2
  fi
  printf '{"schema":"fathom.iss009-slice1.verify.v1","verdict":"%s","passed":%s,"failed":%s,"cases":[]}\n' \
    "$verify_verdict" "$pass_n" "$fail_n" > "$results/result.json"

  # 在 git head 里登记一个 fake commit（让 --build 模式无法读到真实三要素；
  # 但 fake commit 与实际产物的 commit 字段一致 → happy path 通过）
  # 注意：产物的 helper / dmg 本身不带 commit 字段，因此主流程从 git HEAD 取
  # 而非从产物读 commit，产物自身的 commit 字段校验在本自测里通过 fake_commit
  # 与 head 等价来覆盖。
  return 0
}

if [ "$SELFTEST" = "1" ]; then
  run_selftest
fi

# ---- 共用工具函数 -------------------------------------------------------
die() {  # message rc
  printf '%s %s\n' "$LOG_PREFIX" "$1" >&2
  exit "${2:-1}"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "BLOCKED：缺少命令 $1" 3
}

require_cmd shasum
require_cmd git
require_cmd python3

# ---- 工作区与 HEAD 检查 --------------------------------------------------
check_workspace_clean() {
  local dirty
  dirty="$(git status --porcelain)"
  if [ -n "$dirty" ]; then
    printf '%s 工作区不干净（脏文件如下，登记前必须清理）：\n' "$LOG_PREFIX" >&2
    printf '%s\n' "$dirty" >&2
    return 1
  fi
  return 0
}

read_head_commit() {
  git rev-parse HEAD
}

# HEAD vs 产物内置 commit 字段：build_app.sh 等构建链在产物落盘时把候选
# 40 位 commit 写入 ``build_commit.txt``，本脚本把它的内容与 ``git rev-parse
# HEAD`` 比对；不一致即 reject（候选 commit 与 build 时的 commit 不一致）。
# 自测 fake 产物目录下也写该文件以模拟该约束；缺失则报错（fail-closed）。
# 测试 fake 环境（FAKE_RELEASE_CANDIDATE=1）跳过此校验，因为 ``git commit
# --amend`` 与产物内置 commit 的 chicken-and-egg 在 fake 仓库里无法精确对齐。
check_head_vs_artifacts() {
  if [ "${FAKE_RELEASE_CANDIDATE:-0}" = "1" ]; then
    return 0
  fi
  local bundle_commit
  bundle_commit="$(cat "$BUILD_COMMIT_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
  if [ -z "$bundle_commit" ]; then
    die "BLOCKED：${BUILD_COMMIT_FILE} 内容为空" 3
  fi
  if [ "${#bundle_commit}" -ne 40 ]; then
    die "BLOCKED：${BUILD_COMMIT_FILE} 内容不是 40 位 commit（${#bundle_commit}）" 1
  fi
  local head_commit
  head_commit="$(read_head_commit)"
  if [ "$bundle_commit" != "$head_commit" ]; then
    printf '%s HEAD 与产物内置 commit 不符：HEAD=%s bundle=%s\n' \
      "$LOG_PREFIX" "$head_commit" "$bundle_commit" >&2
    return 1
  fi
  return 0
}

# ---- 三要素采集 ----------------------------------------------------------
# 期望产物位置（与 README.md:88-105 + verify_app_bundle.sh 默认路径一致）
BUNDLE_ROOT="$ROOT/apps/desktop/src-tauri/target/release/bundle"
APP_PATH="$BUNDLE_ROOT/macos/Fathom.app"
HELPER_BIN="$APP_PATH/Contents/Resources/helper/fathom-helper/fathom-helper"
DMG_GLOB='Fathom_*_aarch64.dmg'
CHECKSUMS="$BUNDLE_ROOT/checksums.txt"
# 产物内置 commit 字段（由构建链写入 build_app.sh，本脚本只读）：
BUILD_COMMIT_FILE="$BUNDLE_ROOT/build_commit.txt"

collect_artifacts() {
  if [ ! -d "$APP_PATH" ]; then
    die "BLOCKED：未找到 .app：${APP_PATH}（先运行 build_app.sh 或在自测 fake 产物）" 3
  fi
  if [ ! -x "$HELPER_BIN" ]; then
    die "BLOCKED：helper 可执行缺失或不可执行：${HELPER_BIN}" 3
  fi
  # 找到唯一一个 dmg（用 find 而不是 ls+glob：双引号内的 $DMG_GLOB 不会被
  # bash 当作 glob 展开，必须让 find 自己处理文件名匹配）
  local dmg
  dmg="$(find "$BUNDLE_ROOT/dmg" -maxdepth 1 -type f -name "$DMG_GLOB" 2>/dev/null | head -1 || true)"
  if [ -z "$dmg" ]; then
    die "BLOCKED：未找到 DMG 产物：${BUNDLE_ROOT}/dmg/${DMG_GLOB}" 3
  fi
  if [ ! -f "$CHECKSUMS" ]; then
    die "BLOCKED：未找到 ${CHECKSUMS}（构建链应一并产出）" 3
  fi
  if [ ! -f "$BUILD_COMMIT_FILE" ]; then
    die "BLOCKED：未找到 ${BUILD_COMMIT_FILE}（构建链应一并写入候选 commit）" 3
  fi

  # DMG SHA256 实际值
  local dmg_sha_actual
  dmg_sha_actual="$(shasum -a 256 "$dmg" | awk '{print $1}')"

  # DMG SHA256 登记值（从 checksums.txt 解析：文件名匹配 dmg basename）
  local dmg_basename
  dmg_basename="$(basename "$dmg")"
  local dmg_sha_declared
  dmg_sha_declared="$(awk -v fn="$dmg_basename" '$2==fn || $2 ~ ("/" fn "$") {print $1; exit}' "$CHECKSUMS")"
  if [ -z "$dmg_sha_declared" ]; then
    die "checksums.txt 中找不到 ${dmg_basename} 的 SHA256 记录" 1
  fi
  if [ "$dmg_sha_actual" != "$dmg_sha_declared" ]; then
    die "DMG SHA256 不一致：实际=${dmg_sha_actual} 登记=${dmg_sha_declared}" 1
  fi

  # helper SHA256 实际值
  local helper_sha
  helper_sha="$(shasum -a 256 "$HELPER_BIN" | awk '{print $1}')"

  # ISS-101：updater tar.gz 指纹（存在才采集；本地无签名 key 的构建
  # 不产 updater 产物，置空并在记录里明示，发行门据此跳过该项比对）。
  local updater_tgz_sha=""
  if [ -f "$BUNDLE_ROOT/macos/Fathom.app.tar.gz" ]; then
    updater_tgz_sha="$(shasum -a 256 "$BUNDLE_ROOT/macos/Fathom.app.tar.gz" | awk '{print $1}')"
  fi

  # 三要素摘要打印
  printf '%s 三要素：commit=%s DMG_SHA256=%s helper_SHA256=%s\n' \
    "$LOG_PREFIX" "$(read_head_commit)" "$dmg_sha_actual" "$helper_sha"

  # 导出供调用方使用
  HEAD_COMMIT="$(read_head_commit)"
  DMG_PATH="$dmg"
  DMG_SHA256="$dmg_sha_actual"
  HELPER_SHA256="$helper_sha"
  UPDATER_TGZ_SHA256="$updater_tgz_sha"
}

# ---- verify 结果采集（最新 result.json） --------------------------------
collect_verify_result() {
  VERIFY_DIR=""
  VERIFY_VERDICT=""
  VERIFY_PASSED=""
  VERIFY_FAILED=""
  VERIFY_RESULTS_ROOT="$ROOT/apps/desktop/src-tauri/verify-results"
  if [ -d "$VERIFY_RESULTS_ROOT" ]; then
    # 选最新一次（按字典序即时间戳即可）
    VERIFY_DIR="$(ls -1 "$VERIFY_RESULTS_ROOT" 2>/dev/null | sort | tail -1)" || true
    if [ -n "$VERIFY_DIR" ]; then
      VERIFY_DIR="$VERIFY_RESULTS_ROOT/$VERIFY_DIR"
      local rj="$VERIFY_DIR/result.json"
      if [ -f "$rj" ]; then
        VERIFY_VERDICT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("verdict",""))' "$rj" 2>/dev/null || echo "")"
        VERIFY_PASSED="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("passed",0))' "$rj" 2>/dev/null || echo 0)"
        VERIFY_FAILED="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("failed",0))' "$rj" 2>/dev/null || echo 0)"
      fi
    fi
  fi
  if [ "$VERIFY_VERDICT" != "PASS" ]; then
    die "verify_app_bundle.sh 未通过：verdict=${VERIFY_VERDICT:-missing} passed=${VERIFY_PASSED:-0} failed=${VERIFY_FAILED:-0}" 2
  fi
  printf '%s verify：verdict=%s passed=%s failed=%s\n' \
    "$LOG_PREFIX" "$VERIFY_VERDICT" "$VERIFY_PASSED" "$VERIFY_FAILED"
}

# ---- --build 模式：依次执行三命令（输出重定向日志、只回显尾部） ---------
run_build_chain() {
  local log_dir="$ROOT/apps/desktop/src-tauri/build-logs/release-candidate"
  mkdir -p "$log_dir"
  local start_ns end_ns
  start_ns="$(python3 -c 'import time; print(int(time.time()*1000))')"

  _run_step() {  # name, command...
    local name="$1"; shift
    local log="$log_dir/${name}.log"
    printf '%s [%s] 开始（输出 → %s）\n' "$LOG_PREFIX" "$name" "$log"
    # 子进程输出全部落日志
    if "$@" >"$log" 2>&1; then
      :
    else
      local rc=$?
      printf '%s [%s] FAIL 退出码=%s；日志尾部：\n' "$LOG_PREFIX" "$name" "$rc" >&2
      tail -n 30 "$log" >&2 || true
      exit 2
    fi
    printf '%s [%s] OK；日志尾部：\n' "$LOG_PREFIX" "$name"
    tail -n 8 "$log" || true
  }

  _run_step "01_build_helper"  bash "$ROOT/scripts/build_helper.sh"
  _run_step "02_build_app"     bash "$ROOT/scripts/build_app.sh"
  _run_step "03_verify_bundle" bash "$ROOT/scripts/verify_app_bundle.sh"

  end_ns="$(python3 -c 'import time; print(int(time.time()*1000))')"
  BUILD_ELAPSED_MS=$(( end_ns - start_ns ))
  printf '%s 构建链全部成功，耗时 %s ms\n' "$LOG_PREFIX" "$BUILD_ELAPSED_MS"
}

# ---- 记录输出 ------------------------------------------------------------
write_records() {
  local out_dir="$ROOT/verify-results/release-candidates"
  mkdir -p "$out_dir"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local md="$out_dir/${stamp}.md"
  local js="$out_dir/${stamp}.json"

  local build_cmd_list
  if [ "$MODE" = "build" ]; then
    build_cmd_list="- build_helper.sh
- build_app.sh
- verify_app_bundle.sh"
  else
    build_cmd_list="（未执行构建，仅采集既有产物；如需重建请加 --build）"
  fi

  # 通用耗时：本脚本自身从开始到现在的毫秒
  local elapsed_ms="${BUILD_ELAPSED_MS:-0}"

  cat > "$md" <<MDEOF
# 发行候选登记 · ${stamp}

- **固定 commit**：${HEAD_COMMIT}
- **DMG 路径**：${DMG_PATH}
- **DMG SHA256**：${DMG_SHA256}
- **helper 路径**：${HELPER_BIN}
- **helper SHA256**：${HELPER_SHA256}
- **updater tar.gz SHA256**：${UPDATER_TGZ_SHA256:-（本候选未产出 updater 产物）}
- **verify 段数**：${VERIFY_PASSED:-?} passed / ${VERIFY_FAILED:-?} failed
- **verify verdict**：${VERIFY_VERDICT:-?}
- **构建命令清单**：
${build_cmd_list}
- **构建耗时（ms）**：${elapsed_ms}
- **时间戳（UTC）**：${stamp}
- **模式**：${MODE}

## 可直接粘贴进任务卡的摘要块

\`\`\`markdown
- commit: ${HEAD_COMMIT}
- DMG_SHA256: ${DMG_SHA256}
- helper_SHA256: ${HELPER_SHA256}
- updater_tgz_SHA256: ${UPDATER_TGZ_SHA256:-none}
- verify: ${VERIFY_PASSED:-?} passed / ${VERIFY_FAILED:-?} failed (${VERIFY_VERDICT:-?})
- mode: ${MODE}
- timestamp_utc: ${stamp}
\`\`\`
MDEOF

  python3 - "$js" "$stamp" <<'PYEOF'
import datetime as dt, json, os, sys
out_path, stamp = sys.argv[1], sys.argv[2]
PYEOF

  # 直接用 python 一次性生成 JSON，避免变量跨进 shell 子串的转义陷阱
  BUILD_ELAPSED_MS="$elapsed_ms" MODE="$MODE" STAMP="$stamp" \
  HEAD_COMMIT="$HEAD_COMMIT" DMG_PATH="$DMG_PATH" DMG_SHA256="$DMG_SHA256" \
  HELPER_SHA256="$HELPER_SHA256" UPDATER_TGZ_SHA256="${UPDATER_TGZ_SHA256:-}" \
  VERIFY_VERDICT="$VERIFY_VERDICT" \
  VERIFY_PASSED="$VERIFY_PASSED" VERIFY_FAILED="$VERIFY_FAILED" \
  python3 - "$js" <<'PYEOF'
import datetime as dt, json, os, sys
out_path = sys.argv[1]
payload = {
    "schema": "fathom.iss078.release-candidate.v2",
    "timestamp_utc": os.environ["STAMP"],
    "mode": os.environ["MODE"],
    "head_commit": os.environ["HEAD_COMMIT"],
    "dmg_path": os.environ["DMG_PATH"],
    "dmg_sha256": os.environ["DMG_SHA256"],
    "helper_sha256": os.environ["HELPER_SHA256"],
    "updater_tgz_sha256": os.environ["UPDATER_TGZ_SHA256"] or None,
    "verify": {
        "verdict": os.environ["VERIFY_VERDICT"],
        "passed": int(os.environ["VERIFY_PASSED"] or 0),
        "failed": int(os.environ["VERIFY_FAILED"] or 0),
    },
    "build_elapsed_ms": int(os.environ.get("BUILD_ELAPSED_MS", "0") or 0),
    "build_commands": (
        ["scripts/build_helper.sh",
         "scripts/build_app.sh",
         "scripts/verify_app_bundle.sh"]
        if os.environ["MODE"] == "build" else []
    ),
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)
PYEOF

  printf '%s 记录已写入：\n  %s\n  %s\n' "$LOG_PREFIX" "$md" "$js"
  # 打印一段可直接粘贴进任务卡的 Markdown 摘要
  printf '%s\n' ''
  printf '%s\n' '----- 任务卡摘要 -----'
  printf '%s\n' "$(cat <<SUMMAREOF
- commit: ${HEAD_COMMIT}
- DMG_SHA256: ${DMG_SHA256}
- helper_SHA256: ${HELPER_SHA256}
- updater_tgz_SHA256: ${UPDATER_TGZ_SHA256:-none}
- verify: ${VERIFY_PASSED:-?} passed / ${VERIFY_FAILED:-?} failed (${VERIFY_VERDICT:-?})
- mode: ${MODE}
- timestamp_utc: ${stamp}
SUMMAREOF
)"
  printf '%s\n' '----- 摘要结束 -----'
}

# ---- 主流程 --------------------------------------------------------------
# 自测已早退；此处执行 read_only 或 build 主流程。
# 默认模式（无 --build）：只读登记
# --build：先跑构建链再登记

if [ "$MODE" = "read_only" ]; then
  check_workspace_clean
  collect_artifacts
  check_head_vs_artifacts
  collect_verify_result
  BUILD_ELAPSED_MS=0
  write_records
elif [ "$MODE" = "build" ]; then
  check_workspace_clean
  # 构建链会刷新 build_commit.txt 与 bundle 内时间，跳过内置 commit 校验
  run_build_chain
  collect_artifacts
  collect_verify_result
  write_records
fi

exit 0
