"""ISS-101：发行门 verify_release_gate.py 的聚焦单测（全部离线）。

被测对象是把「合法发行候选」判定固化的 fail-closed 门（scripts/verify_release_gate.py）：
同 commit SHA 的 CI run 存在且 success、必需 job 恰为 ci.yml 当前清单且逐个
success、制品指纹与候选登记一致、tag 与单一版本源一致。本测试钉住：

1. 必需 job 清单推导与真实 .github/workflows/ci.yml 一致（验收 3：
   必需检查的列表与配置一致），矩阵展开（pytest/cargo 双架构）与
   无 name 的 job 回退到 job 键名；
2. 解析 fail-closed：ci.yml 缺失 / 无 jobs 段 / 空清单 → GateBlocked，
   而不是静默放行；
3. 候选登记与制品指纹（C5）：指纹一致通过、候选更换拒绝
   （record_fingerprint_mismatch）、记录声明指纹但缺制品路径拒绝
   （record_artifact_missing）、manifest 版本与 tag 不符拒绝；
4. 入口行为：非 git 目录 + 合法 tag → 阻塞（exit 3）；--selftest 全绿
   （覆盖全部 reason 路径与一个正例，见脚本头注释的合同表）。

测试不访问网络、不写仓库文件：夹具全部落在 tmp_path。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_release_gate as gate  # noqa: E402

GATE_SCRIPT = REPO_ROOT / "scripts" / "verify_release_gate.py"

# 真实 ci.yml（ISS-100 后）的必需 job 展开清单：4 个 job、pytest/cargo
# 矩阵各 2 架构 = 6 个实例。此清单与 GitHub run 页显示的 job 名逐字一致
# （v0.3.4 CI run 36039169999 实测比对，2026-09-27）。
REAL_CI_YML_REQUIRED = {
    "pytest (arm64)",
    "pytest (x86_64)",
    "API/浏览器检查（arm64）",
    "品牌几何一致性（DEC-023 五处同步）",
    "cargo locked offline（arm64）",
    "cargo locked offline（x86_64）",
}


# ---- 必需 job 清单推导（与配置一致） --------------------------------------


def test_required_jobs_matches_real_ci_yml() -> None:
    """推导结果必须逐字等于真实 ci.yml 的 6 个 job 实例名。"""
    required = gate.derive_required_jobs(REPO_ROOT / ".github" / "workflows" / "ci.yml")
    assert set(required) == REAL_CI_YML_REQUIRED
    assert len(required) == 6


def test_required_jobs_matrix_expansion_and_name_fallback(tmp_path: Path) -> None:
    """矩阵占位按 include 的 arch 值展开；缺 name 的 job 回退 job 键名。"""
    ci = tmp_path / "ci.yml"
    ci.write_text(
        "name: CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  mjob:\n"
        "    name: pytest (${{ matrix.arch }})\n"
        "    strategy:\n"
        "      matrix:\n"
        "        include:\n"
        "          - { os: macos-15, arch: arm64 }\n"
        "          - { os: macos-15-intel, arch: x86_64 }\n"
        "  bare:\n"
        "    runs-on: macos-15\n",
        encoding="utf-8",
    )
    required = gate.derive_required_jobs(ci)
    assert set(required) == {"pytest (arm64)", "pytest (x86_64)", "bare"}


def test_required_jobs_missing_file_blocked(tmp_path: Path) -> None:
    with pytest.raises(gate.GateBlocked):
        gate.derive_required_jobs(tmp_path / "nope.yml")


def test_required_jobs_no_jobs_section_blocked(tmp_path: Path) -> None:
    ci = tmp_path / "ci.yml"
    ci.write_text("name: CI\non: [push]\n", encoding="utf-8")
    with pytest.raises(gate.GateBlocked):
        gate.derive_required_jobs(ci)


def test_required_jobs_unparsed_empty_blocked(tmp_path: Path) -> None:
    """jobs: 段存在但没有任何 job 键 → 解析器失效必须阻塞而非放行。"""
    ci = tmp_path / "ci.yml"
    ci.write_text("name: CI\non: [push]\njobs:\n", encoding="utf-8")
    with pytest.raises(gate.GateBlocked):
        gate.derive_required_jobs(ci)


# ---- 候选登记与制品指纹（C5） ----------------------------------------------


def _make_candidate(tmp_path: Path) -> tuple[Path, Path, dict]:
    dmg = tmp_path / "Fathom_0.3.4_aarch64.dmg"
    dmg.write_bytes(b"dmg-payload")
    helper = tmp_path / "fathom-helper"
    helper.write_bytes(b"helper-payload")
    record = {
        "schema": "fathom.iss078.release-candidate.v1",
        "head_commit": "a" * 40,
        "dmg_sha256": gate.sha256_file(dmg),
        "helper_sha256": gate.sha256_file(helper),
    }
    return dmg, helper, record


def test_record_and_artifacts_pass(tmp_path: Path) -> None:
    dmg, helper, record = _make_candidate(tmp_path)
    gate.check_record_and_artifacts(
        record=record, head="a" * 40, tag_version="0.3.4",
        dmg=dmg, helper=helper, updater_tgz=None, manifest=None,
    )


def test_record_fingerprint_mismatch_rejected(tmp_path: Path) -> None:
    """候选更换（DMG 内容变）不得复用原记录结论。"""
    dmg, helper, record = _make_candidate(tmp_path)
    dmg.write_bytes(b"swapped-payload")
    with pytest.raises(gate.GateError) as ei:
        gate.check_record_and_artifacts(
            record=record, head="a" * 40, tag_version="0.3.4",
            dmg=dmg, helper=helper, updater_tgz=None, manifest=None,
        )
    assert ei.value.reason == "record_fingerprint_mismatch"


def test_record_declared_but_artifact_missing_rejected(tmp_path: Path) -> None:
    dmg, _helper, record = _make_candidate(tmp_path)
    with pytest.raises(gate.GateError) as ei:
        gate.check_record_and_artifacts(
            record=record, head="a" * 40, tag_version="0.3.4",
            dmg=None, helper=None, updater_tgz=None, manifest=None,
        )
    assert ei.value.reason == "record_artifact_missing"


def test_record_commit_mismatch_rejected(tmp_path: Path) -> None:
    dmg, helper, record = _make_candidate(tmp_path)
    with pytest.raises(gate.GateError) as ei:
        gate.check_record_and_artifacts(
            record=record, head="b" * 40, tag_version="0.3.4",
            dmg=dmg, helper=helper, updater_tgz=None, manifest=None,
        )
    assert ei.value.reason == "record_commit_mismatch"


def test_manifest_version_mismatch_rejected(tmp_path: Path) -> None:
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps({"version": "0.3.5", "notes": "t",
                    "pub_date": "2026-09-27T00:00:00Z", "platforms": {}}),
        encoding="utf-8",
    )
    with pytest.raises(gate.GateError) as ei:
        gate.check_record_and_artifacts(
            record=None, head="a" * 40, tag_version="0.3.4",
            dmg=None, helper=None, updater_tgz=None, manifest=manifest,
        )
    assert ei.value.reason == "manifest_version_mismatch"


def test_manifest_invalid_json_rejected(tmp_path: Path) -> None:
    manifest = tmp_path / "latest.json"
    manifest.write_text("{not json", encoding="utf-8")
    with pytest.raises(gate.GateError) as ei:
        gate.check_record_and_artifacts(
            record=None, head="a" * 40, tag_version="0.3.4",
            dmg=None, helper=None, updater_tgz=None, manifest=manifest,
        )
    assert ei.value.reason == "manifest_invalid"


# ---- 入口行为 ---------------------------------------------------------------


def test_entry_blocked_outside_git_repo(tmp_path: Path) -> None:
    """合法 tag 但不在 git 检出内 → 阻塞（exit 3），不得判通过。"""
    proc = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), "--repo", "o/r", "--tag", "v0.3.4"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == gate.EXIT_BLOCKED


def test_entry_selftest_green() -> None:
    """--selftest 覆盖全部 reason 路径 + 正例 + 探针 unbound 正例。"""
    proc = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), "--selftest"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "全部通过" in (proc.stdout + proc.stderr)
