import json
import shutil
import subprocess
from datetime import datetime

import pytest

from stock_analyst import core, gemini_sync as G

GITIGNORE = core.ROOT / ".gitignore"


# --- parsing test output ------------------------------------------------------------------

def test_pytest_summary_is_counted():
    out = "....F..\n=== short test summary ===\nFAILED tests/x.py::t\n2 failed, 353 passed, 14 deselected, 1 warning in 8.47s\n"
    assert G.parse_pytest(out) == {"passed": 353, "failed": 2, "errors": 0, "warnings": 1, "skipped": 0, "deselected": 14}
    assert G.parse_pytest("no tests ran in 0.01s")["passed"] == 0


def test_vitest_summary_is_counted_through_colour_codes():
    out = "\x1b[2m Test Files \x1b[22m 1 passed (1)\n\x1b[2m      Tests \x1b[22m \x1b[31m2 failed\x1b[39m | \x1b[32m26 passed\x1b[39m (28)\n"
    assert G.parse_vitest(out) == {"passed": 26, "failed": 2, "total": 28}
    assert G.parse_vitest("      Tests  28 passed (28)") == {"passed": 28, "failed": 0, "total": 28}


# --- the status file ------------------------------------------------------------------------

def fake_runner(calls, status=" M stock_analyst/cli.py\n?? STATUS_FOR_GEMINI.md\n", staged=""):
    def runner(command):
        calls.append(command)
        joined = " ".join(command)
        if "pytest" in joined:
            return 0, "355 passed, 14 deselected, 1 warning in 9s"
        if "--prefix" in joined:
            return 0, "      Tests  28 passed (28)"
        if "check-ignore" in joined:
            return 0, ""
        if "status" in joined:
            return 0, status
        if "rev-parse" in joined:
            return 0, "abc123"
        if "diff --stat" in joined:
            return 0, " stock_analyst/cli.py | 12 ++++\n 1 file changed"
        if "diff --cached" in joined:
            return 0, staged
        return 0, ""
    return runner


SAMPLE = {"as_of": "2026-11-11", "source": "vietcap", "warnings": [],
          **{key: {"value": 1.0} for key in G.EIGHT_GROUPS},
          "trade_levels": {"entry_zone": None, "risk_reward": None, "notes": ["Không có vùng mua theo quy tắc."]}}


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    reports, agents, installed = tmp_path / "reports", tmp_path / "agent", tmp_path / "installed"
    for folder in (reports, agents, installed):
        folder.mkdir()
    for name in G.AGENT_NAMES:
        text = f"---\nname: {name}\n---\nbody\n"
        (agents / f"{name}.md").write_text(text, encoding="utf-8")
        (installed / f"{name}.md").write_text(text, encoding="utf-8")
    (reports / "2026-11-10").mkdir()
    (reports / "2026-11-10" / "FINAL_TRADING_REPORT.md").write_text("cũ", encoding="utf-8")
    (reports / "2026-11-11").mkdir()
    (reports / "2026-11-11" / "02_classified_news.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(core, "REPORTS_DIR", reports)
    monkeypatch.setattr(G, "LATEST_DIR", reports / "latest")
    monkeypatch.setattr(G, "STATUS_PATH", tmp_path / "STATUS_FOR_GEMINI.md")
    monkeypatch.setattr(G, "AGENT_DIR", agents)
    monkeypatch.setattr(G, "INSTALLED_AGENT_DIR", installed)
    return tmp_path


def test_status_file_has_every_required_section(workspace):
    calls = []
    result = G.sync("Step A: test", runner=fake_runner(calls), analyze=lambda *a, **k: SAMPLE,
                    now=datetime(2026, 11, 11, 23, 50, tzinfo=core.VN_TZ))
    text = (workspace / "STATUS_FOR_GEMINI.md").read_text(encoding="utf-8")
    for heading in ("**Thời gian đồng bộ:** 2026-11-11T23:50:00+07:00", "**Bước vừa hoàn thành:** Step A: test",
                    "## Trạng thái Test", "## Danh sách File vừa thay đổi", "## Kiểm tra Hợp đồng Dữ liệu (Contract & 8 Indicators Check)",
                    "## Câu hỏi / Vấn đề cần Gemini review"):
        assert heading in text, heading
    assert "| Pytest (`pytest -q`) | 0 | 355 | 0 |" in text and "| Vitest" in text and "| 28 | 0 |" in text
    assert "M stock_analyst/cli.py" in text and "1 file changed" in text
    for key in (*G.EIGHT_GROUPS, "trade_levels"):
        assert f"| `{key}` | có |" in text
    assert all(f"| `{name}` | có | đúng | đúng |" in text for name in G.AGENT_NAMES)
    assert result["tests_ok"] is True and result["pushed"] is None
    assert not any(c[1:2] == ["push"] or "push" in c for c in calls)  # no push without --push


def test_pipeline_files_are_copied_newest_first_and_gaps_are_reported(workspace):
    G.sync("s", runner=fake_runner([]), analyze=lambda *a, **k: SAMPLE)
    latest = workspace / "reports" / "latest"
    assert (latest / "02_classified_news.json").exists() and (latest / "FINAL_TRADING_REPORT.md").read_text(encoding="utf-8") == "cũ"
    text = (workspace / "STATUS_FOR_GEMINI.md").read_text(encoding="utf-8")
    assert "| `02_classified_news.json` | 2026-11-11 |" in text and "| `03_fundamental_picks.json` | chưa có |" in text
    assert "nhiều ngày khác nhau" in text and "Chưa có đầu ra của chuỗi 5 agent" in text
    assert "trade_levels FPT: Không có vùng mua theo quy tắc." in text
    sample = json.loads((latest / "sample_fpt_technical.json").read_text(encoding="utf-8"))
    assert sample["status"] == "live" and set(G.EIGHT_GROUPS) <= set(sample)


def test_offline_keeps_the_previous_sample_marked_stale(workspace):
    G.sync("s", runner=fake_runner([]), analyze=lambda *a, **k: SAMPLE)

    def offline(*a, **k):
        raise core.SourceError("mất mạng")

    G.sync("s", runner=fake_runner([]), analyze=offline)
    sample = json.loads((workspace / "reports" / "latest" / "sample_fpt_technical.json").read_text(encoding="utf-8"))
    assert sample["status"] == "stale" and "mất mạng" in sample["stale_reason"] and sample["ma20"] == {"value": 1.0}


def test_failures_and_agent_drift_become_review_items(workspace):
    (workspace / "installed" / "vn-news-scout.md").write_text("đã sửa", encoding="utf-8")

    def failing(command):
        if "pytest" in " ".join(command):
            return 1, "1 failed, 10 passed in 1s"
        return fake_runner([])(command)

    result = G.sync("s", runner=failing, analyze=lambda *a, **k: SAMPLE)
    assert result["tests_ok"] is False
    assert any(item.startswith("Pytest không đạt") for item in result["review_items"])
    assert any("vn-news-scout" in item for item in result["review_items"])


def test_push_refuses_sensitive_files_and_unstages_them(workspace):
    calls = []
    runner = fake_runner(calls, staged="stock_analyst/cli.py\ndata/live/invites.json\nreports/latest/x.db\n")
    with pytest.raises(ValueError) as error:
        G.push("s", runner)
    assert "invites.json" in str(error.value) and "x.db" in str(error.value)
    assert ["reset", "-q"] == calls[-1][1:] and not any("commit" in c for c in calls)


def test_push_commits_with_the_step_and_trailer_then_pushes(workspace):
    calls = []
    result = G.push("Step A", fake_runner(calls, staged="stock_analyst/cli.py\nSTATUS_FOR_GEMINI.md\n"))
    commit = next(c for c in calls if "commit" in c)
    assert "chore(sync): Step A" in commit and G.COMMIT_TRAILER in commit
    assert calls[-1][1:] == ["push", "-u", "origin", "main"] and result["committed"] is True


# --- the real .gitignore, checked by real git ----------------------------------------------

@pytest.mark.skipif(shutil.which("git") is None and not __import__("os").path.exists(G.GIT_FALLBACK), reason="git không có")
@pytest.mark.parametrize("path,ignored", [
    ("data/live/invites.json", True), ("data/live/quotes.db", True), ("data/news.db", True), ("data/filings/a.pdf", True),
    ("data/replay/2026-11-06.json", True), ("anywhere/cache.db", True), ("x.sqlite", True), (".env", True), (".venv/x", True),
    ("web/node_modules/a.js", True), ("web/dist/index.html", True), ("reports/2026-11-11/03_fundamental_picks.json", True),
    ("reports/charts/FPT.png", True), ("reports/latest/notes.db", True),
    ("stock_analyst/cli.py", False), ("web/src/App.jsx", False), ("web/public/sw.js", False), ("tests/test_cli.py", False),
    ("agent/vn-news-scout.md", False), ("config/live.json", False), ("config/watchlist.json", False), ("config/holidays.json", False),
    ("reports/latest/FINAL_TRADING_REPORT.md", False), ("reports/latest/sample_fpt_technical.json", False), ("STATUS_FOR_GEMINI.md", False),
])
def test_gitignore_blocks_secrets_and_data_but_keeps_source(tmp_path, path, ignored):
    shutil.copyfile(GITIGNORE, tmp_path / ".gitignore")
    subprocess.run([G.git(), "init", "-q"], cwd=tmp_path, check=True)
    code = subprocess.run([G.git(), "check-ignore", "-q", "--no-index", path], cwd=tmp_path).returncode
    assert (code == 0) is ignored


def test_published_snapshot_never_carries_article_text(workspace):
    day = workspace / "reports" / "2026-11-11"
    (day / "01_raw_news.json").write_text(json.dumps({"articles": [{"title": "t", "text": "x" * 5000}]}), encoding="utf-8")
    (day / "02_classified_news.json").write_text(json.dumps({"events": [{"title": "HPG lãi", "url": "https://cafef.vn/a",
        "summary": "đoạn tóm tắt của báo", "sources": [{"name": "cafef", "content": "toàn văn"}]}]}), encoding="utf-8")
    (day / "03_fundamental_picks.json").write_text("{half-written", encoding="utf-8")
    latest = workspace / "reports" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "01_raw_news.json").write_text("{}", encoding="utf-8")  # left by an older version
    found = G.copy_pipeline_outputs()
    copied = json.loads((latest / "02_classified_news.json").read_text(encoding="utf-8"))
    assert copied == {"events": [{"title": "HPG lãi", "url": "https://cafef.vn/a", "sources": [{"name": "cafef"}]}]}
    assert not (latest / "01_raw_news.json").exists() and "01_raw_news.json" not in found
    assert found["03_fundamental_picks.json"] is None and not (latest / "03_fundamental_picks.json").exists()
