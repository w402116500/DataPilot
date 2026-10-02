"""scripts/score_benchmark.py 的行为测试：subprocess 调用 + 最小题库夹具。

脚本以子进程方式运行（保持其独立入口），夹具位于
``tests/fixtures/score-benchmark/``；覆盖 verdict 分桶、数值容差命中/不命中、
月份同义匹配、缺省 score.json 落盘与 --lint 通过/失败（含题面 gold_sql 与
expected 不同源）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "score_benchmark.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "score-benchmark"
BENCH = FIXTURES / "bench"
BENCH_BAD = FIXTURES / "bench-bad"


def run_scorer(*args: str) -> subprocess.CompletedProcess[str]:
    # 显式固定子进程 stdout/stderr 为 UTF-8：Windows 下子进程管道编码默认跟随
    # 本地代码页（cp936），父进程按 utf-8 解码会失配。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def score_to(tmp_path: Path, *rounds: str) -> dict:
    output = tmp_path / "scores.json"
    result = run_scorer("--bench-dir", str(BENCH), "--output", str(output), *rounds)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_verdict_buckets_cover_all_families(tmp_path: Path) -> None:
    scores = score_to(tmp_path, "ok", "partial", "miss", "failed", "incomplete")

    assert scores["ok"]["counts"] == {"correct": 2}
    assert scores["partial"]["results"]["q02"]["verdict"] == "partial"
    assert "1/3" in scores["partial"]["results"]["q02"]["detail"]
    assert scores["miss"]["results"]["q03"] == {
        "verdict": "no-answer",
        "detail": "0 行命中",
        "completed": "completed",
        "answer_chars": len("一共 600 名顾客浏览未下单。"),
    }
    assert scores["failed"]["results"]["q03"]["verdict"] == "run-failed"
    assert scores["failed"]["results"]["q03"]["detail"] == "SANDBOX_REJECTED"
    assert scores["incomplete"]["results"]["q01"]["verdict"] == "no-answer"
    assert (
        scores["incomplete"]["results"]["q01"]["detail"]
        == "partial/ANALYSIS_CLAIM_COMMIT_RETRY_EXHAUSTED"
    )


def test_numeric_rounding_tolerance_hit_and_miss(tmp_path: Path) -> None:
    """gold 4.2：4.1962 超出 0.05% 容差但按 gold 小数位舍入命中；4.1 双路都不命中。"""

    scores = score_to(tmp_path, "tolerance-hit", "tolerance-miss")

    assert scores["tolerance-hit"]["results"]["q01"]["verdict"] == "correct"
    assert scores["tolerance-miss"]["results"]["q01"]["verdict"] == "no-answer"


def test_month_label_synonym_match(tmp_path: Path) -> None:
    scores = score_to(tmp_path, "month")

    assert scores["month"]["counts"] == {"correct": 1}


def test_round_dir_accepted_as_direct_path(tmp_path: Path) -> None:
    """重放场景：轮次参数可直接给路径（--bench-dir 下不存在同名目录时）。"""

    output = tmp_path / "by-path.json"
    result = run_scorer("--bench-dir", str(BENCH), "--output", str(output), str(BENCH / "ok"))

    assert result.returncode == 0, result.stdout + result.stderr
    scores = json.loads(output.read_text(encoding="utf-8"))
    assert list(scores) == [str(BENCH / "ok")]
    assert scores[str(BENCH / "ok")]["counts"] == {"correct": 2}


def test_default_mode_writes_score_json_into_round_dir(tmp_path: Path) -> None:
    bench_copy = tmp_path / "bench"
    shutil.copytree(BENCH, bench_copy)

    result = run_scorer("--bench-dir", str(bench_copy), "ok")

    assert result.returncode == 0, result.stdout + result.stderr
    score_file = bench_copy / "ok" / "score.json"
    assert score_file.exists()
    scores = json.loads(score_file.read_text(encoding="utf-8"))
    assert scores["counts"] == {"correct": 2}
    assert "全对 2/2" in result.stdout


def test_lint_passes_on_consistent_bench() -> None:
    result = run_scorer("--lint", "--bench-dir", str(BENCH))

    assert result.returncode == 0, result.stdout
    assert "结构一致性检查通过" in result.stdout


def test_lint_reports_structural_findings() -> None:
    result = run_scorer("--lint", "--bench-dir", str(BENCH_BAD))

    assert result.returncode == 1
    assert "题 id 集合不一致" in result.stdout
    assert "q01 的 row_count=2 与 len(rows)=1 不一致" in result.stdout
    assert "q02 的 gold_sql 为空" in result.stdout


def test_lint_reports_gold_sql_drift_from_expected() -> None:
    """题面文件留有旧 gold 而 expected 已修正时 lint 必须失败（q13/q15 防复发）。

    夹具中 questions.jsonl 的 q02 gold_sql 是修正前的旧值，questions-datalink.jsonl
    与 expected 同源——只有前者应被点名。
    """

    result = run_scorer("--lint", "--bench-dir", str(FIXTURES / "bench-gold-drift"))

    assert result.returncode == 1
    assert "questions.jsonl: q02 的 gold_sql 与 expected-answers.json 不一致" in result.stdout
    assert "questions-datalink.jsonl: q02" not in result.stdout
