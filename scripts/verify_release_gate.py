#!/usr/bin/env python3
"""ISS-101 · 发行门：发行候选完整 SHA 回归约束（fail-closed）。

## 发行门合同（合法候选判定的完整条件清单）

一个发行候选（tag + commit SHA + 候选登记记录）被判为合法，当且仅当以下
条件全部成立；任一不成立即拒绝（exit 1）并给出可辨认原因：

  C1 SHA 一致：门的判定对象是当前检出树（cwd 的 git HEAD，40 位）。显式
     传入 --sha 时必须与 HEAD 一致；tag 已存在于远端时，tag 剥离
     （refs/tags/<tag>^{}）指向的 commit 必须与 HEAD 一致。二者任一不符
     = 试图用「另一个 SHA 的成功 run」替代本候选的证据，拒绝。
  C2 tag 形态与版本绑定：tag 必须严格 vX.Y.Z，且去前缀版本 == 检出树的
     单一版本源 fathom/__init__.py __version__。
  C3 同 SHA CI run 存在且成功：该 HEAD SHA 在 CI workflow
     （.github/workflows/ci.yml）下存在 status=completed 且
     conclusion=success 的 run。门只按候选 SHA 查询，绝不回退
     main/latest/其他 SHA 的成功 run。
  C4 必需 job 恰为 ci.yml 当前清单且逐个 success：上述 run 的 job 名称
     集合必须恰好等于从检出树 .github/workflows/ci.yml 推导的必需 job
     清单（含矩阵展开后的每个实例），且每个 job conclusion=success。
  C5 候选登记绑定（提供 --record-json 时）：记录 head_commit == HEAD；
     提供 --dmg/--helper 时实测 sha256 必须与记录一致（候选更换/过期记录
     不能复用原通过结论）；--updater-tgz/--manifest 在记录携带对应指纹
     字段时同样实测比对；--manifest 的 version 必须等于 tag 版本。记录
     声明了指纹但未提供对应制品路径 = 无法完成绑定，拒绝。

## 每类失败输入的判定结果（全部拒绝，reason 可辨认）

  输入类别                          reason                   说明
  --------------------------------  -----------------------  -------------------------
  显式 SHA != HEAD / tag 目标 != HEAD  gate_sha_mismatch       其他 SHA 成功不替代
  tag 不存在（且未 --probe-unbound）  tag_missing             dispatch 生产路径要求
                                                            tag 已存在
  tag 与版本源不符                   tag_version_mismatch    承接 ISS-041A 错 tag 负向
  SHA 无 CI run                     ci_run_missing          不回退其他 SHA 的 run
  CI run 进行中/排队                ci_run_in_progress      进行中不得判通过
  CI run 取消                       ci_run_cancelled        取消不得判通过
  CI run 失败（含 timed_out 等）      ci_conclusion_failure   失败不得判通过
  run 的 job 集合 != ci.yml 清单     required_jobs_mismatch  必需检查列表与配置一致
  run 中有 job 非 success            job_not_success         逐 job 判定
  记录 commit != HEAD                record_commit_mismatch  记录与候选不同源
  制品指纹 != 记录指纹               record_fingerprint_mismatch  候选更换不可复用
  记录声明指纹但未提供制品            record_artifact_missing 绑定不完整即拒绝
  manifest 版本 != tag 版本          manifest_version_mismatch
  manifest 非法 JSON                 manifest_invalid

## 判定输出口径

  机器可判读：stdout 逐行 `GATE <key>=<value>`；末行恒有
  `GATE verdict=pass` 或 `GATE verdict=reject`（reject 时另有
  `GATE reason=<code>`）。人读行以 `[gate]` 前缀。
  退出码：0 合法候选；1 拒绝（上表任一）；2 用法错误；3 阻塞
  （git/gh/文件系统不可用等——阻塞同样不得判通过）。

## 与既有门的分工

  - release.yml「版本门（构建前/上传前）」仍逐字保留：本门不替代
    check_version_consistency.sh（tauri/Cargo 多文件一致性），只在
    其之上叠加 SHA 绑定与 CI 绑定。
  - ISS-041A 的错 tag 负向验收（错 tag 在构建前被拦）由 C2 承接，
    并在本卡探针中首次于真实 runner 执行。
  - 候选登记记录来自 scripts/release_candidate_record.sh（ISS-078）。

只依赖标准库。bash 侧调用示例见 .github/workflows/release.yml 的
「发行门」步骤。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LOG = "[gate]"

# ---- 常量 ----------------------------------------------------------------

TAG_RE = re.compile(r"^v([0-9]+)\.([0-9]+)\.([0-9]+)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_LINE_RE = re.compile(
    r'^__version__[ \t]*=[ \t]*"([0-9]+\.[0-9]+\.[0-9]+)"[ \t]*$'
)
# ci.yml 内 job 展开名里的矩阵占位（与 actions 渲染同形）
MATRIX_ARCH_TOKEN = "${{ matrix.arch }}"
CI_WORKFLOW_REL = Path(".github/workflows/ci.yml")
GH_RUN_LIST_FIELDS = "databaseId,headSha,status,conclusion,event,url"

EXIT_PASS, EXIT_REJECT, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 3


class GateError(Exception):
    """可辨认的拒绝（reason code + 人读说明）。"""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class GateBlocked(Exception):
    """阻塞（不能完成判定 = 不得判通过）。"""


# ---- 机器可判读输出 -------------------------------------------------------


def emit(key: str, value: object) -> None:
    print(f"GATE {key}={value}")


def info(msg: str) -> None:
    print(f"{LOG} {msg}")


def fail(msg: str) -> None:
    print(f"{LOG} FAIL：{msg}", file=sys.stderr)


# ---- 基础原语 -------------------------------------------------------------


def run_cmd(
    args: list[str], *, cwd: Path | None = None, env: dict | None = None
) -> str:
    """运行子进程，失败抛 GateBlocked（输出留在异常文本里供诊断）。"""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=merged,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateBlocked(f"命令无法执行 {args[0]}：{exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise GateBlocked(
            f"命令退出码 {proc.returncode}：{' '.join(args[:4])}…\n{tail}"
        )
    return proc.stdout


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def head_sha_of(cwd: Path) -> str:
    out = run_cmd(["git", "rev-parse", "HEAD"], cwd=cwd)
    sha = out.strip()
    if not SHA_RE.match(sha):
        raise GateBlocked(f"git rev-parse HEAD 不是 40 位 SHA：{sha!r}")
    return sha


def remote_url(cwd: Path) -> str:
    override = os.environ.get("GATE_GIT_REMOTE_URL")
    if override:
        return override
    out = run_cmd(["git", "remote", "get-url", "origin"], cwd=cwd)
    url = out.strip()
    if not url:
        raise GateBlocked("git remote get-url origin 为空（无 origin 远端）")
    return url


def resolve_tag_target(cwd: Path, tag: str) -> str | None:
    """剥出 tag 指向的 commit；tag 不存在返回 None；网络/权限失败抛阻塞。

    优先 refs/tags/<tag>^{}（附注 tag 剥离到 commit），空则回退
    refs/tags/<tag>（轻量 tag 本身即 commit）。
    """
    url = remote_url(cwd)
    for ref in (f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"):
        out = run_cmd(["git", "ls-remote", url, ref], cwd=cwd)
        line = out.strip().splitlines()[0].strip() if out.strip() else ""
        if line:
            sha = line.split()[0]
            if SHA_RE.match(sha):
                return sha
            # ^{} 查询无结果时 ls-remote 退出码仍为 0 但输出空，继续回退。
    return None


def read_single_version(cwd: Path) -> str:
    init_py = cwd / "fathom" / "__init__.py"
    if not init_py.is_file():
        raise GateBlocked(f"未找到单一版本源 {init_py}（须在候选检出树内运行）")
    for line in init_py.read_text(encoding="utf-8").splitlines():
        m = VERSION_LINE_RE.match(line.strip())
        if m:
            return m.group(1)
    raise GateBlocked(f"{init_py} 未找到 __version__ 行")


# ---- 必需 job 清单推导（与 ci.yml 配置一致，C4） ---------------------------


def derive_required_jobs(ci_yml: Path) -> list[str]:
    """从 ci.yml 的 jobs: 段推导必需 job 展开名清单。

    解析口径（针对本仓库 ci.yml 的受控格式，解析失败 fail-closed）：
      - 顶层 job 键：`jobs:` 下两空格缩进的 `<key>:`；
      - job 显示名：job 块内四空格缩进的 `name:` 值（缺失时用 job 键名）；
      - 矩阵展开：name 含 ${{ matrix.arch }} 时，按同 job 块
        strategy.matrix.include 里每个 `arch: <值>` 展开为多个实例名。
    """
    if not ci_yml.is_file():
        raise GateBlocked(f"未找到 {ci_yml}")
    lines = ci_yml.read_text(encoding="utf-8").splitlines()

    # 定位 jobs: 段
    try:
        jobs_idx = next(i for i, ln in enumerate(lines) if ln.rstrip() == "jobs:")
    except StopIteration as exc:
        raise GateBlocked(f"{ci_yml} 无顶层 jobs: 段") from exc

    job_key_re = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
    name_re = re.compile(r"^    name:\s*(.+?)\s*$")
    arch_re = re.compile(r"arch:\s*([A-Za-z0-9_-]+)")

    required: list[str] = []
    current: dict[str, object] | None = None
    for ln in lines[jobs_idx + 1 :]:
        m = job_key_re.match(ln)
        if m:
            if current is not None:
                required.extend(_expand_job(current))
            current = {"key": m.group(1), "name": None, "archs": []}
            continue
        if current is None:
            continue  # jobs: 段之前的杂项（不会出现，防御）
        if ln.startswith("  ") and not ln.startswith("    "):
            continue  # job 键行之外的两空格行（如列表项结束）
        nm = name_re.match(ln)
        if nm and current["name"] is None:
            current["name"] = nm.group(1)
            continue
        if "arch" in ln:
            am = arch_re.search(ln)
            if am:
                current["archs"].append(am.group(1))  # type: ignore[union-attr]
    if current is not None:
        required.extend(_expand_job(current))

    if not required:
        raise GateBlocked(f"{ci_yml} jobs: 段未解析出任何必需 job（解析器失效）")
    return required


def _expand_job(job: dict[str, object]) -> list[str]:
    name = job["name"] if job["name"] else str(job["key"])
    archs = job["archs"] or []
    if MATRIX_ARCH_TOKEN in name and archs:
        return [name.replace(MATRIX_ARCH_TOKEN, a) for a in archs]
    return [name]


# ---- CI run 查询（C3/C4） --------------------------------------------------

def _gh_available() -> bool:
    return shutil.which("gh") is not None


def query_ci_runs(repo: str, sha: str) -> list[dict]:
    """该 SHA 在 CI workflow 下的 run 列表（fixtures 可注入，见 --selftest）。"""
    fixture_dir = os.environ.get("GATE_GH_FIXTURE_DIR")
    if fixture_dir:
        path = Path(fixture_dir) / "runs.json"
        if not path.is_file():
            raise GateBlocked(f"夹具目录缺 runs.json：{path}")
        runs = json.loads(path.read_text(encoding="utf-8"))
        return [r for r in runs if r.get("headSha") == sha]
    if not _gh_available():
        raise GateBlocked("缺 gh CLI（且未设 GATE_GH_FIXTURE_DIR）")
    # 认证口径：runner 上由步骤 env 提供 GH_TOKEN（github.token）；本地由
    # gh 自身登录态提供。此处不强制 env——gh 认证失败会在 run_cmd 中转为
    # GateBlocked（阻塞同样不得判通过）。
    out = run_cmd(
        [
            "gh", "run", "list", "--repo", repo, "--workflow", "ci.yml",
            "--limit", "200", "--json", GH_RUN_LIST_FIELDS,
        ]
    )
    try:
        runs = json.loads(out)
    except json.JSONDecodeError as exc:
        raise GateBlocked(f"gh run list 输出不是 JSON：{exc}") from exc
    return [r for r in runs if r.get("headSha") == sha]


def query_run_jobs(repo: str, run_id: int) -> list[dict]:
    fixture_dir = os.environ.get("GATE_GH_FIXTURE_DIR")
    if fixture_dir:
        path = Path(fixture_dir) / f"jobs-{run_id}.json"
        if not path.is_file():
            raise GateBlocked(f"夹具目录缺 jobs-{run_id}.json：{path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("jobs", [])
    out = run_cmd(
        ["gh", "run", "view", str(run_id), "--repo", repo, "--json", "jobs"]
    )
    try:
        return json.loads(out).get("jobs", [])
    except json.JSONDecodeError as exc:
        raise GateBlocked(f"gh run view 输出不是 JSON：{exc}") from exc


def check_ci_binding(repo: str, sha: str, required: list[str]) -> dict:
    """C3+C4：同 SHA 的 CI run 存在、成功、必需 job 恰为清单且逐个 success。

    返回通过证据 dict（run id/url/event）；不满足抛 GateError。
    """
    runs = query_ci_runs(repo, sha)
    if not runs:
        raise GateError(
            "ci_run_missing",
            f"SHA {sha[:10]} 在 CI workflow 下没有任何 run（近 200 条范围内）。"
            "门只按候选 SHA 查询，不回退 main/latest/其他 SHA 的成功 run（合同 C3）",
        )
    emit("ci_runs_seen", len(runs))

    ok_runs = [
        r for r in runs
        if r.get("status") == "completed" and r.get("conclusion") == "success"
    ]
    if not ok_runs:
        latest = max(runs, key=lambda r: int(r.get("databaseId", 0)))
        status = latest.get("status")
        conclusion = latest.get("conclusion")
        emit("ci_latest_status", status)
        emit("ci_latest_conclusion", conclusion)
        if status != "completed":
            raise GateError(
                "ci_run_in_progress",
                f"SHA {sha[:10]} 最新 CI run {latest.get('databaseId')} "
                f"status={status}（进行中/排队不得判通过）",
            )
        if conclusion == "cancelled":
            raise GateError(
                "ci_run_cancelled",
                f"SHA {sha[:10]} CI run {latest.get('databaseId')} 被取消",
            )
        raise GateError(
            "ci_conclusion_failure",
            f"SHA {sha[:10]} CI run {latest.get('databaseId')} "
            f"conclusion={conclusion}（失败不得判通过）",
        )

    # 新到旧逐个找「job 集合恰好一致且逐个 success」的 run
    ok_runs.sort(key=lambda r: int(r.get("databaseId", 0)), reverse=True)
    newest = ok_runs[0]
    for run in ok_runs:
        run_id = int(run["databaseId"])
        jobs = query_run_jobs(repo, run_id)
        names = [j.get("name", "") for j in jobs]
        if set(names) != set(required):
            # 记录最新成功 run 的差异供诊断；继续找更旧的成功 run
            missing = sorted(set(required) - set(names))
            extra = sorted(set(names) - set(required))
            info(
                f"CI run {run_id} 的 job 集合与 ci.yml 清单不一致"
                f"（缺 {missing} 多 {extra}），尝试更旧的成功 run"
            )
            continue
        bad = [j for j in jobs if j.get("conclusion") != "success"]
        if bad:
            names_bad = ", ".join(
                f"{j.get('name')}({j.get('conclusion')})" for j in bad
            )
            raise GateError(
                "job_not_success",
                f"CI run {run_id} 存在非 success job：{names_bad}",
            )
        emit("ci_run_id", run_id)
        emit("ci_run_url", run.get("url", ""))
        emit("ci_run_event", run.get("event", ""))
        return {
            "run_id": run_id,
            "url": run.get("url", ""),
            "event": run.get("event", ""),
        }
    names = [j.get("name", "") for j in query_run_jobs(repo, int(newest["databaseId"]))]
    raise GateError(
        "required_jobs_mismatch",
        "该 SHA 的成功 CI run 的 job 集合与 ci.yml 当前清单不一致"
        f"（必需 {sorted(required)}；run {newest.get('databaseId')} 实际 "
        f"{sorted(set(names))}）。必需检查的列表必须与配置一致（合同 C4）",
    )


# ---- 候选登记与制品指纹（C5） ----------------------------------------------

def load_record(arg: str, cwd: Path) -> dict:
    """--record-json 接受内联 JSON 字符串或 @文件路径。"""
    raw: str
    if arg.startswith("@"):
        path = Path(arg[1:])
        if not path.is_file():
            raise GateBlocked(f"候选登记文件不存在：{path}")
        raw = path.read_text(encoding="utf-8")
    else:
        raw = arg
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateBlocked(f"候选登记不是合法 JSON：{exc}") from exc
    if not isinstance(rec, dict) or not isinstance(rec.get("head_commit"), str):
        raise GateBlocked(
            "候选登记缺少 head_commit 字段（应为 release_candidate_record.sh 产物）"
        )
    return rec


def _fingerprint_compare(
    label: str,
    path: Path | None,
    declared: str | None,
) -> None:
    """实测制品指纹；记录声明了指纹就必须比对（候选更换不可复用结论）。"""
    if path is None:
        if declared:
            raise GateError(
                "record_artifact_missing",
                f"候选登记声明了 {label} 指纹，但未提供 --{label} 制品路径，"
                "绑定不完整即拒绝（合同 C5）",
            )
        return
    if not path.is_file():
        raise GateBlocked(f"{label} 制品不存在：{path}")
    actual = sha256_file(path)
    emit(f"{label}_sha256", actual)
    if declared and actual != declared:
        raise GateError(
            "record_fingerprint_mismatch",
            f"{label} 实测 sha256 {actual} != 登记 {declared}"
            "（候选已更换，原通过结论不可复用；合同 C5）",
        )


def check_record_and_artifacts(
    *,
    record: dict | None,
    head: str,
    tag_version: str,
    dmg: Path | None,
    helper: Path | None,
    updater_tgz: Path | None,
    manifest: Path | None,
) -> None:
    if record is not None:
        emit("record_bound", "true")
        if record.get("head_commit") != head:
            raise GateError(
                "record_commit_mismatch",
                f"候选登记 head_commit {record.get('head_commit')} != 门判定"
                f" HEAD {head}（登记与候选不同源；合同 C5）",
            )
    else:
        emit("record_bound", "false")

    rec = record or {}
    _fingerprint_compare("dmg", dmg, rec.get("dmg_sha256"))
    _fingerprint_compare("helper", helper, rec.get("helper_sha256"))
    _fingerprint_compare(
        "updater_tgz", updater_tgz, rec.get("updater_tgz_sha256")
    )

    if manifest is not None:
        if not manifest.is_file():
            raise GateBlocked(f"manifest 制品不存在：{manifest}")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GateError("manifest_invalid", f"manifest 不是合法 JSON：{exc}")
        emit("manifest_sha256", sha256_file(manifest))
        mver = data.get("version") if isinstance(data, dict) else None
        if mver != tag_version:
            raise GateError(
                "manifest_version_mismatch",
                f"manifest version {mver!r} != tag 版本 {tag_version}",
            )
        declared_manifest = rec.get("manifest_sha256")
        if declared_manifest:
            actual = sha256_file(manifest)
            if actual != declared_manifest:
                raise GateError(
                    "record_fingerprint_mismatch",
                    f"manifest 实测 sha256 {actual} != 登记 {declared_manifest}",
                )
    elif rec.get("manifest_sha256"):
        raise GateError(
            "record_artifact_missing",
            "候选登记声明了 manifest 指纹，但未提供 --manifest 制品路径（合同 C5）",
        )


# ---- 主流程 ---------------------------------------------------------------


def gate_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="ISS-101 发行门：同 SHA 完整回归约束（fail-closed）"
    )
    parser.add_argument("--repo", default=None, help="owner/name（C3 查询用）")
    parser.add_argument("--tag", required=True, help="发行 tag（vX.Y.Z）")
    parser.add_argument(
        "--sha", default=None,
        help="声明的候选 SHA；提供时必须等于检出树 HEAD（默认取 HEAD）",
    )
    parser.add_argument(
        "--probe-unbound", action="store_true",
        help="探针模式：跳过 tag→commit 绑定（评估 (commit,tag) 组合；"
        "仅用于负向/正例探针，生产路径不得使用）",
    )
    parser.add_argument(
        "--record-json", default=None,
        help="候选登记（release_candidate_record.sh 产物 JSON：内联字符串或 @路径）",
    )
    parser.add_argument("--dmg", default=None, help="实测 DMG 制品路径")
    parser.add_argument("--helper", default=None, help="实测 helper 制品路径")
    parser.add_argument(
        "--updater-tgz", default=None, help="实测 updater .app.tar.gz 路径"
    )
    parser.add_argument(
        "--manifest", default=None, help="实测 latest.json（updater 清单）路径"
    )
    parser.add_argument(
        "--selftest", action="store_true",
        help="离线自测（临时夹具仓库 + 注入 gh 响应，覆盖全部 reason 路径）",
    )
    # --selftest 独立短路（不要求 --tag 等生产参数）
    if "--selftest" in argv:
        return run_selftest()

    args = parser.parse_args(argv)

    cwd = Path.cwd()
    try:
        return _decide(args, cwd)
    except GateError as exc:
        fail(f"[{exc.reason}] {exc.detail}")
        emit("verdict", "reject")
        emit("reason", exc.reason)
        return EXIT_REJECT
    except GateBlocked as exc:
        fail(f"BLOCKED {exc}")
        emit("verdict", "reject")
        emit("reason", "blocked")
        emit("blocked_detail", str(exc).replace("\n", " ")[:400])
        return EXIT_BLOCKED


def _decide(args: argparse.Namespace, cwd: Path) -> int:
    # ---- 输入形态 ----
    m = TAG_RE.match(args.tag)
    if not m:
        raise GateError(
            "tag_version_mismatch",
            f"tag {args.tag!r} 不匹配 vX.Y.Z 严格形态",
        )
    tag_version = args.tag[1:]
    emit("tag", args.tag)
    emit("tag_version", tag_version)

    # ---- C1：SHA 一致 ----
    head = head_sha_of(cwd)
    emit("candidate_sha", head)
    if args.sha and args.sha != head:
        raise GateError(
            "gate_sha_mismatch",
            f"声明候选 SHA {args.sha} != 检出树 HEAD {head}"
            "（其他 SHA 的成功 run 不可替代本候选；合同 C1）",
        )
    tag_target = resolve_tag_target(cwd, args.tag)
    if args.probe_unbound:
        # 探针模式：完全跳过 tag→commit 绑定（含 tag 目标 != HEAD 的比较），
        # 只把解析结果作为信息输出——用于在真实 runner 上评估 (commit, tag)
        # 组合的其余门条件（CI 绑定 / 版本绑定）。
        info(
            f"探针模式：跳过 tag 绑定（tag 目标="
            f"{tag_target or '不存在'}，HEAD={head[:10]}）"
        )
        emit("tag_target", tag_target or "unbound(probe)")
    elif tag_target is None:
        raise GateError(
            "tag_missing",
            f"tag {args.tag} 不存在于远端（生产路径要求先打 tag；"
            "探针评估组合请显式 --probe-unbound）",
        )
    elif tag_target != head:
        raise GateError(
            "gate_sha_mismatch",
            f"tag {args.tag} 目标 commit {tag_target} != 检出树 HEAD {head}"
            "（不能用另一个 SHA 的成功 run 替代 tag 目标的回归结果；合同 C1）",
        )
    else:
        emit("tag_target", tag_target)

    # ---- C2：tag 与单一版本源 ----
    version = read_single_version(cwd)
    emit("version_source", version)
    if version != tag_version:
        raise GateError(
            "tag_version_mismatch",
            f"tag {args.tag} 版本 {tag_version} != 单一版本源 __version__ "
            f"{version}（承接 ISS-041A 错 tag 负向门；合同 C2）",
        )

    # ---- C3/C4：同 SHA CI run 与必需 job ----
    required = derive_required_jobs(cwd / CI_WORKFLOW_REL)
    emit("required_jobs_count", len(required))
    emit("required_jobs", json.dumps(sorted(required), ensure_ascii=False))
    if not args.repo:
        raise GateBlocked("缺 --repo（C3 CI 查询需要）")
    evidence = check_ci_binding(args.repo, head, required)
    info(
        f"CI 绑定通过：run {evidence['run_id']}（{evidence['event']}），"
        f"必需 job {len(required)} 项逐个 success"
    )

    # ---- C5：候选登记与制品指纹（有输入才激活） ----
    record = load_record(args.record_json, cwd) if args.record_json else None
    has_artifacts = any([args.dmg, args.helper, args.updater_tgz, args.manifest])
    if record is not None or has_artifacts:
        check_record_and_artifacts(
            record=record,
            head=head,
            tag_version=tag_version,
            dmg=Path(args.dmg) if args.dmg else None,
            helper=Path(args.helper) if args.helper else None,
            updater_tgz=Path(args.updater_tgz) if args.updater_tgz else None,
            manifest=Path(args.manifest) if args.manifest else None,
        )

    emit("verdict", "pass")
    info(
        "发行门通过：合法候选"
        f"（tag={args.tag} sha={head[:10]} CI=run {evidence['run_id']}）"
    )
    return EXIT_PASS


# ---- 离线自测 -------------------------------------------------------------


def run_selftest() -> int:
    """临时夹具仓库 + 注入 gh 响应，覆盖全部 reason 路径与一个正例。

    不访问网络（gh 查询经 GATE_GH_FIXTURE_DIR 注入，tag 解析经
    GATE_GIT_REMOTE_URL 指向本地裸仓库）。
    """
    repo_root = Path(__file__).resolve().parent.parent
    info("--selftest 开始（离线夹具，不访问网络）")

    cases: list[tuple[str, int, dict]] = []  # (name, expected_rc, env-extra)

    def build_fixture(base: Path, marker: str = "a") -> tuple[Path, str]:
        """临时 git 仓库（__version__ 0.3.4 + 本仓库真实 ci.yml）+ 裸远端含 v0.3.4。

        marker 写入仓库根的区分文件：保证多份夹具的 commit SHA 必然不同
        （tag 绑定反例需要「tag 目标 != 检出 HEAD」）。
        """
        work = base / "repo"
        (work / "fathom").mkdir(parents=True)
        (work / ".github" / "workflows").mkdir(parents=True)
        (work / "fathom" / "__init__.py").write_text(
            '__version__ = "0.3.4"\n', encoding="utf-8"
        )
        (work / "marker.txt").write_text(marker, encoding="utf-8")
        shutil.copy(repo_root / CI_WORKFLOW_REL, work / CI_WORKFLOW_REL)
        run_cmd(["git", "init", "-q", "."], cwd=work)
        run_cmd(
            ["git", "-c", "user.email=g@t", "-c", "user.name=t",
             "add", "-A"], cwd=work,
        )
        run_cmd(
            ["git", "-c", "user.email=g@t", "-c", "user.name=t",
             "commit", "-q", "-m", "init"], cwd=work,
        )
        head = head_sha_of(work)
        bare = base / "remote.git"
        run_cmd(["git", "init", "-q", "--bare", str(bare)], cwd=work)
        run_cmd(
            ["git", "-c", "user.email=g@t", "-c", "user.name=t",
             "tag", "-a", "v0.3.4", "-m", "t"], cwd=work,
        )
        run_cmd(
            ["git", "push", "-q", str(bare), "HEAD", "v0.3.4"],
            cwd=work,
        )
        return work, head

    def write_gh_fixture(
        d: Path,
        head: str,
        *,
        runs: list[dict] | None = None,
        jobs_by_run: dict[int, list[dict]] | None = None,
    ) -> None:
        d.mkdir(parents=True, exist_ok=True)
        all_runs = runs if runs is not None else [
            {
                "databaseId": 101, "headSha": head, "status": "completed",
                "conclusion": "success", "event": "push", "url": "https://x/101",
            }
        ]
        (d / "runs.json").write_text(
            json.dumps(all_runs + [
                {"databaseId": 999, "headSha": "0" * 40, "status": "completed",
                 "conclusion": "success", "event": "push", "url": "https://x/9"},
            ]), encoding="utf-8",
        )
        by_run = jobs_by_run if jobs_by_run is not None else {
            101: [{"name": n, "conclusion": "success", "status": "completed"}
                  for n in derive_required_jobs(repo_root / CI_WORKFLOW_REL)]
        }
        for rid, jobs in by_run.items():
            (d / f"jobs-{rid}.json").write_text(
                json.dumps({"jobs": jobs}), encoding="utf-8"
            )

    with tempfile.TemporaryDirectory(prefix="gate-selftest-") as td:
        base = Path(td)

        # 正例主夹具：HEAD==tag 目标、版本一致、CI 成功、记录+制品全对
        work, head = build_fixture(base)
        dmg = base / "Fathom_0.3.4_aarch64.dmg"
        dmg.write_bytes(b"dmg-payload")
        helper = base / "fathom-helper"
        helper.write_bytes(b"helper-payload")
        tgz = base / "Fathom_0.3.4_aarch64.app.tar.gz"
        tgz.write_bytes(b"tgz-payload")
        manifest = base / "latest.json"
        manifest.write_text(
            json.dumps({"version": "0.3.4", "notes": "t",
                        "pub_date": "2026-09-27T00:00:00Z",
                        "platforms": {}}), encoding="utf-8",
        )
        record = json.dumps({
            "schema": "fathom.iss078.release-candidate.v1",
            "head_commit": head,
            "dmg_sha256": sha256_file(dmg),
            "helper_sha256": sha256_file(helper),
            "updater_tgz_sha256": sha256_file(tgz),
            "manifest_sha256": sha256_file(manifest),
        })
        gh_dir = base / "gh-ok"
        write_gh_fixture(gh_dir, head)
        common = {
            "GATE_GH_FIXTURE_DIR": str(gh_dir),
            "GATE_GIT_REMOTE_URL": str(base / "remote.git"),
        }

        def invoke(extra_env: dict, args: list[str]) -> int:
            env = dict(os.environ)
            env.update(common)
            env.update(extra_env)
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), *args],
                cwd=str(work), env=env, capture_output=True, text=True,
            )
            return proc.returncode

        full_artifacts = [
            "--record-json", record,
            "--dmg", str(dmg), "--helper", str(helper),
            "--updater-tgz", str(tgz), "--manifest", str(manifest),
        ]

        # 1. 正例：全条件成立 → 0
        cases.append(("happy_pass", 0, ({}, ["--repo", "o/r", "--tag", "v0.3.4",
                                            *full_artifacts], invoke)))
        # 2. 显式 SHA != HEAD → gate_sha_mismatch
        cases.append(("declared_sha_mismatch", 1, ({}, ["--repo", "o/r", "--tag", "v0.3.4",
                                                        "--sha", "1" * 40], invoke)))
        # 3. tag 目标 != HEAD → gate_sha_mismatch（其他 SHA 成功不替代）
        other_work, other_head = build_fixture(base / "other", marker="b")
        # other 仓库的远端同 base/remote.git（v0.3.4 → 第一个仓库的 head）
        # other_head 必然不同 → tag 绑定失败
        def invoke_other(extra_env: dict, args: list[str]) -> int:
            env = dict(os.environ)
            env.update({
                "GATE_GH_FIXTURE_DIR": str(base / "gh-other"),
                "GATE_GIT_REMOTE_URL": str(base / "remote.git"),
            })
            env.update(extra_env)
            write_gh_fixture(base / "gh-other", other_head)
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), *args],
                cwd=str(other_work), env=env, capture_output=True, text=True,
            )
            return proc.returncode
        cases.append(("tag_target_mismatch", 1, ({}, ["--repo", "o/r", "--tag", "v0.3.4"],
                                                  invoke_other)))
        # 4. tag 不存在且未 unbound → tag_missing
        cases.append(("tag_missing", 1, ({}, ["--repo", "o/r", "--tag", "v0.9.9"], invoke)))
        # 5. tag 版本与源不符 → tag_version_mismatch（ISS-041A 承接）
        (work / "fathom" / "__init__.py").write_text(
            '__version__ = "0.3.5"\n', encoding="utf-8"
        )
        cases.append(("tag_version_mismatch", 1, ({}, ["--repo", "o/r", "--tag", "v0.9.9",
                                                       "--probe-unbound"], invoke)))
        (work / "fathom" / "__init__.py").write_text(
            '__version__ = "0.3.4"\n', encoding="utf-8"
        )
        # 6-8. CI run 缺失/进行中/取消/失败
        for name, run_payload, reason in (
            ("ci_run_in_progress",
             {"databaseId": 1, "headSha": head, "status": "in_progress",
              "conclusion": None, "event": "push", "url": "https://x/1"}, None),
            ("ci_run_cancelled",
             {"databaseId": 2, "headSha": head, "status": "completed",
              "conclusion": "cancelled", "event": "push", "url": "https://x/2"}, None),
            ("ci_conclusion_failure",
             {"databaseId": 3, "headSha": head, "status": "completed",
              "conclusion": "failure", "event": "push", "url": "https://x/3"}, None),
        ):
            d = base / f"gh-{name}"
            write_gh_fixture(d, head, runs=[run_payload], jobs_by_run={})
            cases.append((name, 1, ({"GATE_GH_FIXTURE_DIR": str(d)},
                                    ["--repo", "o/r", "--tag", "v0.3.4"], invoke)))
        d = base / "gh-missing"
        write_gh_fixture(d, head, runs=[], jobs_by_run={})
        cases.append(("ci_run_missing", 1, ({"GATE_GH_FIXTURE_DIR": str(d)},
                                            ["--repo", "o/r", "--tag", "v0.3.4"], invoke)))
        # 9. job 集合不一致 → required_jobs_mismatch
        d = base / "gh-jobsset"
        partial = [{"name": "pytest (arm64)", "conclusion": "success",
                    "status": "completed"}]
        write_gh_fixture(d, head, jobs_by_run={101: partial})
        cases.append(("required_jobs_mismatch", 1, ({"GATE_GH_FIXTURE_DIR": str(d)},
                                                    ["--repo", "o/r", "--tag", "v0.3.4"], invoke)))
        # 10. 单 job 失败 → job_not_success
        d = base / "gh-jobfail"
        jobs = [{"name": n, "conclusion": "success", "status": "completed"}
                for n in derive_required_jobs(repo_root / CI_WORKFLOW_REL)]
        jobs[-1] = {"name": jobs[-1]["name"], "conclusion": "failure",
                    "status": "completed"}
        write_gh_fixture(d, head, jobs_by_run={101: jobs})
        cases.append(("job_not_success", 1, ({"GATE_GH_FIXTURE_DIR": str(d)},
                                             ["--repo", "o/r", "--tag", "v0.3.4"], invoke)))
        # 11. 记录 commit 不符 → record_commit_mismatch
        bad_rec = json.loads(record)
        bad_rec["head_commit"] = "2" * 40
        cases.append(("record_commit_mismatch", 1, ({}, ["--repo", "o/r", "--tag", "v0.3.4",
                                                         "--record-json", json.dumps(bad_rec),
                                                         *full_artifacts[2:]], invoke)))
        # 12. 候选更换（DMG 指纹不符）→ record_fingerprint_mismatch
        swapped = base / "swapped.dmg"
        swapped.write_bytes(b"swapped-payload")
        cases.append(("record_fingerprint_mismatch", 1, ({}, ["--repo", "o/r", "--tag", "v0.3.4",
                                                              "--record-json", record,
                                                              "--dmg", str(swapped),
                                                              "--helper", str(helper)], invoke)))
        # 13. 记录声明指纹但缺制品路径 → record_artifact_missing
        cases.append(("record_artifact_missing", 1, ({}, ["--repo", "o/r", "--tag", "v0.3.4",
                                                          "--record-json", record], invoke)))
        # 14. manifest 版本不符 → manifest_version_mismatch
        bad_manifest = base / "bad.latest.json"
        bad_manifest.write_text(
            json.dumps({"version": "0.3.5", "notes": "t",
                        "pub_date": "2026-09-27T00:00:00Z",
                        "platforms": {}}), encoding="utf-8",
        )
        cases.append(("manifest_version_mismatch", 1, ({}, ["--repo", "o/r", "--tag", "v0.3.4",
                                                            "--manifest", str(bad_manifest)], invoke)))
        # 15. 探针 unbound 正例：无 tag 绑定、组合合法 → 0
        cases.append(("probe_unbound_pass", 0, ({}, ["--repo", "o/r", "--tag", "v0.3.4",
                                                     "--probe-unbound"], invoke)))

        failures = 0
        for name, expected, (extra_env, arglist, inv) in cases:
            got = inv(extra_env, arglist)
            mark = "ok" if got == expected else "FAIL"
            if got != expected:
                failures += 1
            print(f"  [{mark}] {name:<28} 期望={expected} 实际={got}")

        if failures:
            fail(f"--selftest 失败：{failures} 项未达预期")
            return 1
        info("--selftest 全部通过")
        return 0


def main() -> int:
    return gate_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
