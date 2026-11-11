"""Offline tests use synthetic statements built from the module's own title tables; live tests hit real sources."""
from __future__ import annotations

import json
import re

import pytest

from stock_analyst import core
from stock_analyst import fundamentals as F
from stock_analyst.fundamentals import BS, CF, IS, NOTE

PERIODS = [(y, q) for y in (2023, 2024, 2025) for q in (1, 2, 3, 4)] + [(2026, 1), (2026, 2), (2026, 3)]
LATEST = (2026, 3)
AGO = (2025, 3)

DEFAULTS = {
    "non_financial": dict(
        revenue=1000, cogs=-700, gross_profit=300, financial_income=20, financial_expense=-10, interest_expense=-10,
        selling_expense=-50, admin_expense=-50, operating_profit=210, pbt=200, net_profit=160, minority_profit=10,
        parent_profit=150, current_assets=900, cash=100, short_term_investments=200, receivables=200, inventory=200,
        total_assets=2000, liabilities=1000, current_liabilities=500, st_debt=200, lt_debt=200, convertible_bonds=0,
        equity=1000, minority=100, total_resources=2000, cfo=180, capex=-50, depreciation=40),
    "bank": dict(
        nii=100, fee_income=10, other_income=10, revenue=130, opex=-40, provision=-20, pbt=70, net_profit=56,
        minority_profit=-1, parent_profit=55, total_assets=10000, liabilities=9000, equity=1000, minority=10,
        total_resources=10000, sbv_deposits=200, placements=800, trading_securities=100, gross_loans=7000,
        loan_provision=-140, debt_bought=0, afs_securities=1000, htm_securities=400, deposits=7500, cfo=300, capex=-5,
        npl_substandard=30, npl_doubtful=20, npl_loss=50, demand_deposits=2000),
    "securities": dict(
        revenue=500, gross_profit=300, financial_income=5, interest_expense=-100, admin_expense=-20, operating_profit=200,
        pbt=200, unrealized_profit=10, net_profit=160, minority_profit=0, parent_profit=160, fund_appropriation=0,
        current_assets=9000, cash=100, margin_loans=4000, receivables=100, fvtpl_assets=4000, total_assets=9500,
        liabilities=5500, current_liabilities=5000, st_debt=4500, lt_debt=0, equity=4000, minority=0,
        total_resources=9500, cfo=-300, capex=-5, depreciation=3),
}


def _catalog(kind):
    catalog = {IS: [], BS: [], CF: [], NOTE: []}
    for canon, spec in F.LINES[kind].items():
        section, title, *level = spec[0] if isinstance(spec, list) else spec
        catalog[section].append({"field": f"x_{canon}", "titleVi": title, "level": level[0] if level else 1})
    for section, title in F.TYPE_SIGNATURES[kind]:
        if not any(i["titleVi"] == title for i in catalog[section]):
            catalog[section].append({"field": f"sig_{len(catalog[section])}", "titleVi": title, "level": 1})
    return catalog


def _value(overrides, canon, default, period):
    value = overrides.get(canon, default)
    return value(period) if callable(value) else value


def _build(kind="non_financial", cumulative=False, stats=None, periods=PERIODS, **overrides):
    """Raw payload as Vietcap shapes it: amounts in VND, standalone unless cumulative=True."""
    defaults = {**DEFAULTS[kind], **{k: v for k, v in overrides.items() if k not in DEFAULTS[kind]}}
    spec = F.LINES[kind]
    catalog = _catalog(kind)
    section_of = {c: (s[0] if isinstance(s, list) else s)[0] for c, s in spec.items()}
    statements = {}
    for section in (IS, BS, CF) + ((NOTE,) if kind == "bank" else ()):
        quarters, years = [], {}
        for year, quarter in periods:
            row = {"yearReport": year, "lengthReport": quarter, "publicDate": f"{year}-{quarter * 3:02d}-28T00:00:00"}
            for item in catalog[section]:
                row.setdefault(item["field"], 1e9)  # signature-only lines just need to be populated
            for canon, default in defaults.items():
                if section_of.get(canon) != section:
                    continue
                value = _value(overrides, canon, default, (year, quarter))
                flow = section in (IS, CF)
                if flow and cumulative:
                    value = sum(_value(overrides, canon, default, (year, q)) or 0 for q in range(1, quarter + 1))
                row[f"x_{canon}"] = None if value is None else value * F.BIL
                if flow:
                    standalone = _value(overrides, canon, default, (year, quarter)) or 0
                    years.setdefault(year, {"yearReport": year, "lengthReport": 5}).setdefault(f"x_{canon}", 0.0)
                    years[year][f"x_{canon}"] += standalone * F.BIL
            if section == CF:
                index = periods.index((year, quarter))
                row["x_opening_cash"] = (100 + index * 10) * F.BIL if not cumulative else (100 + periods.index((year, 1)) * 10) * F.BIL
                row["x_closing_cash"] = (110 + index * 10) * F.BIL
            quarters.append(row)
        complete = {y for y, _ in periods if all((y, q) in periods for q in (1, 2, 3, 4))}
        statements[section] = {"quarters": quarters, "years": [years.get(y, {"yearReport": y, "lengthReport": 5}) for y in sorted(complete)]}
    return {"catalog": catalog, "statements": statements, "stats": stats if stats is not None else []}


def _run(raw, quarters=8):
    result = F._analyze("TST", raw, quarters)
    json.dumps(result, allow_nan=False)  # contract: JSON-pure
    return result


def _rules(result):
    return {f["rule"] for f in result["red_flags"]}


# ---------------------------------------------------------------- basis and YTD conversion

def test_classify_year():
    assert F._classify_year([10, 20, 30, 40], 100) == "standalone"
    assert F._classify_year([10, 30, 60, 100], 100) == "cumulative"
    assert F._classify_year([10, 20, 30, None], 100) is None
    assert F._classify_year([0, 0, 0, 100], 100) is None  # both tests match, no evidence


def test_ytd_rows_are_detected_and_converted():
    growing = lambda p: 100 + p[1] * 10
    raw = _build(cumulative=True, revenue=growing, gross_profit=lambda p: 30 + p[1], net_profit=50, parent_profit=40,
                 minority_profit=10, cfo=lambda p: p[1] * 5 - 2, capex=-5)
    result = _run(raw)
    q = {r["period"]: r for r in result["statements"]}
    assert q["Q3/2025"]["revenue"] == 130
    assert q["Q4/2025"]["revenue"] == 140
    assert q["Q1/2026"]["revenue"] == 110
    assert q["Q3/2025"]["cfo"] == 13
    assert result["data_basis"][IS]["2025"] == {"basis": "cumulative", "method": "annual_reconciliation"}
    assert result["data_basis"][CF]["2026"]["basis"] == "cumulative"
    assert result["growth"]["revenue_ttm"] == 140 + 110 + 120 + 130
    assert any("lũy kế" in w for w in result["warnings"])


def test_standalone_rows_stay_untouched_and_latest_cf_year_uses_cash_roll():
    result = _run(_build())
    assert result["statements"][0]["revenue"] == 1000
    assert result["data_basis"][IS]["2025"]["basis"] == "standalone"
    assert result["data_basis"][IS]["2026"] == {"basis": "standalone", "method": "inherited_from_prior_year"}
    assert result["data_basis"][CF]["2026"] == {"basis": "standalone", "method": "cash_rollforward"}


def test_unknown_basis_gives_null_flows_not_a_guess():
    rows = {(2026, 2): {"f": 5.0}, (2026, 3): {"f": 7.0}}
    basis = F.detect_basis(rows, {}, ["f"])
    assert basis[2026]["basis"] is None
    assert F.to_standalone(rows, "f", basis) == {(2026, 2): None, (2026, 3): None}


def test_cumulative_quarter_without_predecessor_is_null():
    rows = {(2025, 3): {"f": 30.0}}
    assert F.to_standalone(rows, "f", {2025: {"basis": "cumulative"}})[(2025, 3)] is None


# ---------------------------------------------------------------- ratios

def test_non_financial_ratios():
    result = _run(_build())
    assert result["company_type"] == "non_financial"
    assert result["units"]["amounts"] == "tỷ đồng"
    p, l, c = result["profitability"], result["liquidity_leverage"], result["cash_quality"]
    assert (p["gross_margin_ttm"], p["operating_margin_ttm"], p["net_margin_ttm"]) == (0.3, 0.2, 0.16)
    assert p["roe"] == pytest.approx(600 / 900, abs=1e-4)  # parent profit over equity excluding minorities
    assert p["roa"] == 0.3
    assert (l["current_ratio"], l["quick_ratio"], l["debt_to_equity"]) == (1.8, 1.4, 0.4)
    assert l["net_debt"] == 100
    assert l["ebitda_ttm"] == 960
    assert l["net_debt_to_ebitda"] == pytest.approx(100 / 960, abs=1e-4)
    assert l["interest_cover"] == 20
    assert (c["cfo_ttm"], c["capex_ttm"], c["fcf_ttm"]) == (720, 200, 520)
    assert c["cfo_to_net_profit"] == 1.125
    assert result["latest_period"] == "Q3/2026"
    assert result["public_date"] == "2026-09-28"
    assert len(result["statements"]) == 8


def test_growth_same_quarter_and_ttm():
    result = _run(_build(revenue=lambda p: 1200 if p[0] == 2026 else 1000, parent_profit=lambda p: 180 if p[0] == 2026 else 150,
                         net_profit=lambda p: 190 if p[0] == 2026 else 160))
    g = result["growth"]
    assert g["revenue_yoy"] == 0.2
    assert g["parent_profit_yoy"] == 0.2
    assert g["revenue_ttm_yoy"] == pytest.approx((1000 + 3600) / 4000 - 1, abs=1e-4)


def test_division_by_zero_and_negative_denominators_are_null():
    assert F._div(1.0, 0.0) is None
    assert F._div(1.0, -2.0) is None
    assert F._div(None, 2.0) is None
    assert F._growth(5.0, 0.0) is None
    assert F._growth(5.0, -1.0) is None
    result = _run(_build(revenue=0, gross_profit=0, interest_expense=0, current_liabilities=0,
                         pbt=-10, net_profit=-10, parent_profit=-10, minority_profit=0))
    p, l = result["profitability"], result["liquidity_leverage"]
    assert p["gross_margin_ttm"] is None and p["net_margin_ttm"] is None
    assert l["interest_cover"] is None and l["current_ratio"] is None and l["quick_ratio"] is None
    assert result["growth"]["parent_profit_yoy"] is None
    assert result["cash_quality"]["cfo_to_net_profit"] is None


def test_missing_quarter_makes_ttm_null():
    periods = [p for p in PERIODS if p != (2026, 1)]
    result = _run(_build(periods=periods))
    assert result["growth"]["revenue_ttm"] is None
    assert result["profitability"]["roe"] is None
    assert any("revenue_ttm" in w for w in result["warnings"])


def test_bank_ratios():
    result = _run(_build("bank"))
    assert result["company_type"] == "bank"
    p, l, c = result["profitability"], result["liquidity_leverage"], result["cash_quality"]
    assert p["nim"] == pytest.approx(400 / 9500, abs=1e-4)
    assert p["cir"] == pytest.approx(160 / 520, abs=1e-4)
    assert p["ldr"] == pytest.approx(7000 / 7500, abs=1e-4)
    assert p["npl_ratio"] == pytest.approx(100 / 7000, abs=1e-4)
    assert p["coverage"] == 1.4
    assert p["casa"] == pytest.approx(2000 / 7500, abs=1e-4)
    assert p["roe"] == pytest.approx(220 / 990, abs=1e-4)
    assert l == {"equity_to_assets": 0.1, "total_assets": 10000, "equity": 1000}
    assert c["fcf_ttm"] is None and c["cfo_to_net_profit"] is None and c["note"]


def test_securities_ratios_and_fund_appropriation_added_back():
    raw = _build("securities", parent_profit=lambda p: 110 if p == (2025, 2) else 160,
                 fund_appropriation=lambda p: 50 if p == (2025, 2) else 0)
    result = _run(raw, quarters=12)
    row = next(r for r in result["statements"] if r["period"] == "Q2/2025")
    assert row["parent_profit"] == 160
    assert result["integrity"]["passed"] is True
    assert result["liquidity_leverage"]["margin_loans_to_equity"] == 1.0
    assert result["liquidity_leverage"]["interest_cover"] == 3.0
    assert "PROFIT_UP_CFO_NEGATIVE" not in _rules(result)  # margin book moves CFO; rule is non-financial only


# ---------------------------------------------------------------- routing and resolution

@pytest.mark.parametrize("kind", ["non_financial", "bank", "securities"])
def test_detect_type(kind):
    assert F.detect_company_type(_catalog(kind)) == kind


def test_detect_type_needs_data_and_refuses_mixed_catalogue():
    catalog = _catalog("bank")
    assert F.detect_company_type(catalog, {}) is None
    mixed = _catalog("bank")
    mixed[IS] += _catalog("non_financial")[IS]
    mixed[BS] += _catalog("non_financial")[BS]
    assert F.detect_company_type(mixed) is None


def test_undeterminable_type_returns_nulls_with_warning():
    result = _run({"catalog": {IS: [{"field": "a", "titleVi": "Lạ", "level": 1}], BS: [], CF: [], NOTE: []},
                   "statements": {IS: {"quarters": [{"yearReport": 2026, "lengthReport": 3, "a": 1.0}]}}})
    assert result["company_type"] is None and result["profitability"] is None
    assert result["warnings"]


def test_no_catalogue_for_unknown_symbol():
    result = _run({"catalog": None, "statements": {}})
    assert result["company_type"] is None and result["warnings"] and result["statements"] is None


def test_insurer_reports_not_supported_with_raw_lines():
    catalog = {IS: [{"field": "isi103", "titleVi": "Doanh thu từ phí bảo hiểm", "level": 1},
                    {"field": "isi51", "titleVi": "Thu phí bảo hiểm gốc", "level": 2}],
               BS: [{"field": "bsa53", "titleVi": "TỔNG CỘNG TÀI SẢN", "level": 1}], CF: [], NOTE: []}
    row = {"yearReport": 2026, "lengthReport": 3, "publicDate": "2026-10-30T00:00:00", "isi103": 5e9, "isi51": 4e9, "bsa53": 9e9}
    result = _run({"catalog": catalog, "statements": {IS: {"quarters": [row]}, BS: {"quarters": [row]}}})
    assert result["company_type"] == "insurance" and result["supported"] is False
    assert "chưa hỗ trợ" in result["warnings"][0]
    assert result["profitability"] is None and result["red_flags"] is None
    titles = {l["title"]: l["value"] for l in result["raw_lines"][0]["lines"]}
    assert titles == {"Doanh thu từ phí bảo hiểm": 5.0, "TỔNG CỘNG TÀI SẢN": 9.0}


def test_same_field_code_means_what_the_catalogue_says():
    """cfa34 is financing for FPT but net cash flow for VCB: the code alone must never decide."""
    raw = _build()
    for item in raw["catalog"][IS]:
        if item["field"] == "x_revenue":
            item["titleVi"] = "Chi phí gì đó"
    result = _run(raw)
    assert result["statements"][0]["revenue"] is None
    assert result["profitability"]["gross_margin_ttm"] is None
    assert any("revenue" in w for w in result["warnings"])


def test_field_with_two_titles_is_ambiguous_and_falls_back():
    raw = _build("securities")
    raw["catalog"][IS].append({"field": "x_pbt", "titleVi": "CHI PHÍ THUẾ THU NHẬP DOANH NGHIỆP", "level": 1})
    raw["catalog"][CF].append({"field": "cfa1", "titleVi": "LỢI NHUẬN TRƯỚC THUẾ", "level": 2})
    for row in raw["statements"][CF]["quarters"]:
        row["cfa1"] = 123 * F.BIL
    for row in raw["statements"][CF]["years"]:
        row["cfa1"] = 492 * F.BIL
    result = _run(raw)
    assert result["resolved_fields"]["pbt"]["field"] == "cfa1"
    assert result["statements"][0]["pbt"] == 123


def test_title_matching_two_fields_stays_unresolved():
    raw = _build()
    raw["catalog"][BS].append({"field": "other", "titleVi": "Vay ngắn hạn", "level": 1})
    result = _run(raw)
    assert "st_debt" not in result["resolved_fields"]
    assert result["liquidity_leverage"]["debt"] is None
    assert any("st_debt" in w for w in result["warnings"])


# ---------------------------------------------------------------- red flags

def _year_step(new, old):
    return lambda p: new if p >= (2025, 4) else old


@pytest.mark.parametrize("overrides, rule", [
    (dict(cfo=-50, net_profit=_year_step(200, 160), parent_profit=_year_step(190, 150)), "PROFIT_UP_CFO_NEGATIVE"),
    (dict(cfo=20), "LOW_CASH_CONVERSION"),
    (dict(receivables=lambda p: 400 if p >= (2026, 1) else 200), "RECEIVABLES_OUTPACE_REVENUE"),
    (dict(inventory=lambda p: 400 if p >= (2026, 1) else 200), "INVENTORY_OUTPACE_REVENUE"),
    (dict(st_debt=lambda p: 500 if p >= (2026, 1) else 200, equity=1000, liabilities=1000), "DEBT_JUMP"),
    (dict(gross_profit=_year_step(150, 300)), "MARGIN_COLLAPSE"),
    (dict(cash=10, short_term_investments=5), "CURRENT_LIABILITIES_VS_CASH"),
    (dict(financial_income=150), "FINANCIAL_INCOME_DOMINANT"),
    (dict(financial_income=lambda p: 150 if p == (2026, 1) else 20), "FINANCIAL_INCOME_SPIKE"),
    (dict(gross_profit=100, selling_expense=-50, admin_expense=-40, interest_expense=-10), "LOW_INTEREST_COVER"),
])
def test_non_financial_flags_fire(overrides, rule):
    flags = [f for f in _run(_build(**overrides))["red_flags"] if f["rule"] == rule]
    assert flags, rule
    assert flags[0]["severity"] in ("low", "medium", "high")
    assert flags[0]["description"] and flags[0]["values"]


def test_clean_company_has_no_flags():
    assert _run(_build())["red_flags"] == []


def test_negative_equity_flag():
    result = _run(_build(equity=-100, liabilities=2100, minority=0))
    flag = next(f for f in result["red_flags"] if f["rule"] == "NEGATIVE_EQUITY")
    assert flag["severity"] == "high" and flag["values"]["equity"] == -100


def test_perimeter_change_flag_warns_about_comparability():
    change = lambda new, old: (lambda p: new if p >= (2026, 1) else old)
    result = _run(_build(total_assets=change(1500, 2000), total_resources=change(1500, 2000), liabilities=change(500, 1000), minority=change(20, 100)))
    assert "PERIMETER_CHANGE" in _rules(result)
    assert any("phạm vi hợp nhất" in w for w in result["warnings"])


@pytest.mark.parametrize("overrides, rule", [
    (dict(npl_loss=lambda p: 150 if p >= (2026, 1) else 50, loan_provision=lambda p: -300 if p >= (2026, 1) else -140), "NPL_UP"),
    (dict(loan_provision=lambda p: -110 if p == LATEST else -180), "COVERAGE_DOWN"),
    (dict(loan_provision=-80), "COVERAGE_LOW"),
    (dict(demand_deposits=lambda p: 1500 if p >= (2026, 1) else 2000), "CASA_DOWN"),
    (dict(nii=lambda p: 50 if p >= (2025, 4) else 100), "NIM_DOWN"),
    (dict(other_income=30), "OTHER_INCOME_DOMINANT"),
])
def test_bank_flags_fire(overrides, rule):
    assert rule in _rules(_run(_build("bank", **overrides)))


def test_clean_bank_has_no_flags_and_no_cash_rules():
    assert _run(_build("bank", cfo=-500))["red_flags"] == []


def test_unrealized_profit_flag_for_securities():
    assert "UNREALIZED_PROFIT_DOMINANT" in _rules(_run(_build("securities", unrealized_profit=120)))


def test_output_never_recommends():
    text = json.dumps(_run(_build()), ensure_ascii=False).lower()
    for word in ("recommendation", "target_price", "composite", "khuyến nghị", "giá mục tiêu"):
        assert word not in text


# ---------------------------------------------------------------- integrity

def test_balance_identity_failure_becomes_warning():
    result = _run(_build(total_assets=lambda p: 2500 if p == LATEST else 2000))
    assert result["integrity"]["passed"] is False
    assert next(b for b in result["integrity"]["balance_identity"] if b["period"] == "Q3/2026")["ok"] is False
    assert any("Q3/2026" in w and "không khớp" in w for w in result["warnings"])
    assert result["profitability"]["roe"] is None  # parent equity cannot be derived from a broken balance sheet


def test_balance_identity_accepts_minority_outside_equity():
    result = _run(_build(equity=900, liabilities=1000, minority=100))
    assert result["integrity"]["passed"] is True
    assert result["profitability"]["roe"] == pytest.approx(600 / 900, abs=1e-4)


def test_quarter_sum_vs_annual_failure_warns():
    raw = _build()
    for row in raw["statements"][IS]["years"]:
        if row["yearReport"] == 2025:
            row["x_parent_profit"] *= 1.05
    result = _run(raw)
    assert result["integrity"]["passed"] is False
    bad = [r for r in result["integrity"]["annual_reconciliation"] if not r["ok"]]
    assert bad[0]["year"] == 2025 and bad[0]["line"] == "parent_profit"
    assert any("Năm 2025" in w for w in result["warnings"])


def test_profit_attribution_failure_warns():
    result = _run(_build(parent_profit=lambda p: 100 if p == LATEST else 150))
    assert result["integrity"]["profit_attribution_ok"] is False
    assert any("cổ đông mẹ" in w for w in result["warnings"])


# ---------------------------------------------------------------- valuation

def _stats(pe=10.0, pb=2.0, ps=2.5, **extra):
    return [{"yearReport": 2026, "quarter": 3, "marketCap": 6000 * F.BIL, "numberOfSharesMktCap": 1e8, "pe": pe, "pb": pb, "ps": ps,
             "roe": 0.6667, "roa": 0.3, "grossMargin": 0.3, "currentRatio": 1.8, "ebitda": 960 * F.BIL, **extra}]


def test_valuation_and_crosscheck_agree():
    result = _run(_build(stats=_stats(pb=6000 / 900, ps=1.5)))
    v = result["valuation"]
    assert v["market_cap"] == 6000 and v["pe"] == 10
    assert v["own_pe"] == 10 and v["implied_price_vnd"] == 60000
    assert all(c["ok"] for c in v["crosscheck"])
    assert not [w for w in result["warnings"] if "statistics-financial" in w]


def test_crosscheck_divergence_warns():
    result = _run(_build(stats=_stats(pe=5.0, pb=6000 / 900, ps=1.5)))
    bad = [c for c in result["valuation"]["crosscheck"] if not c["ok"]]
    assert bad[0]["metric"] == "parent_profit_ttm"
    assert any("parent_profit_ttm" in w for w in result["warnings"])


def test_no_stats_gives_null_valuation_with_warning():
    result = _run(_build())
    assert result["valuation"] is None and any("statistics-financial" in w for w in result["warnings"])


def test_stale_stats_period_warns():
    stats = _stats(pb=6000 / 900, ps=1.5)
    stats[0]["quarter"] = 2
    assert any("Q2/2026" in w for w in _run(_build(stats=stats))["warnings"])


# ---------------------------------------------------------------- CafeF list

def test_list_reports_parses_and_normalises(monkeypatch):
    calls = []

    def fake(url, **kwargs):
        calls.append(kwargs["params"])
        return {"Success": True, "Data": [
            {"Quarter": 3, "Year": 2026, "Name": "Báo cáo tài chính công ty mẹ quý 3 năm 2026 ",
             "Link": " https://cafefnew.mediacdn.vn/Images/a b.pdf?v=123 "},
            {"Quarter": 3, "Year": 2026, "Name": "Báo cáo tài chính hợp nhất quý 3 năm 2026", "Link": "//cafefnew.mediacdn.vn/x.pdf"},
            {"Quarter": 5, "Year": 2025, "Name": "Báo cáo tài chính hợp nhất năm 2025 (đã kiểm toán)", "Link": "http://cafefnew.mediacdn.vn/y.pdf"},
            {"Quarter": 2, "Year": 2025, "Name": "Báo cáo tài chính quý 2 năm 2025", "Link": "https://evil.example/z.pdf"},
        ]}
    monkeypatch.setattr(core, "fetch_json", fake)
    reports = F.list_reports("fpt", 2026)
    assert calls[0] == {"Symbol": "FPT", "Type": 1, "Year": 2026}
    assert [r["consolidated"] for r in reports] == [False, True, True, None]
    assert reports[0]["url"] == "https://cafefnew.mediacdn.vn/Images/a%20b.pdf"
    assert reports[1]["url"] == "https://cafefnew.mediacdn.vn/x.pdf"
    assert reports[2] == {"title": "Báo cáo tài chính hợp nhất năm 2025 (đã kiểm toán)", "year": 2025, "quarter": None,
                          "consolidated": True, "url": "https://cafefnew.mediacdn.vn/y.pdf"}
    assert reports[3]["url"] is None


def test_list_reports_source_failure_and_bad_year(monkeypatch):
    monkeypatch.setattr(core, "fetch_json", lambda url, **k: {"Success": False, "Message": "Object reference", "Data": []})
    with pytest.raises(core.SourceError):
        F.list_reports("FPT")
    with pytest.raises(ValueError):
        F.list_reports("FPT", "2026; drop")
    with pytest.raises(ValueError):
        F.list_reports("../etc")


# ---------------------------------------------------------------- PDF

def _mini_pdf(pages):
    """Smallest valid PDF with one text line per page; an empty string makes an image-like blank page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 10 Tf 40 700 Td ({text}) Tj ET" if text else ""
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objects)} 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = "%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


class _Response:
    is_redirect = False

    def __init__(self, body, url="https://cafefnew.mediacdn.vn/a.pdf", headers=None, history=()):
        self.body, self.url, self.headers, self.history, self.closed = body, url, headers or {}, list(history), False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


@pytest.fixture
def filings(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)

    def serve(body, **kwargs):
        response = _Response(body, **kwargs)
        monkeypatch.setattr(core, "fetch", lambda url, **k: response)
        return response
    return serve


URL = "https://cafefnew.mediacdn.vn/Images/Uploaded/a.pdf"
LONG_TEXT = "Doanh thu thuan 13754159003395 Loi nhuan sau thue 2729313537249 dong"


@pytest.mark.parametrize("url", [
    "http://cafefnew.mediacdn.vn/a.pdf",
    "ftp://cafefnew.mediacdn.vn/a.pdf",
    "https://evil.example/a.pdf",
    "https://cafefnew.mediacdn.vn.evil.example/a.pdf",
    "https://evilcafef.vn/a.pdf",
    "https://cafef.vn.evil.example/a.pdf",
    "https://cafef.vn@evil.example/a.pdf",
    "https://user:pw@cafef.vn/a.pdf",
    "https://cafef.vn:8443/a.pdf",
    "https://cafef.vn\\@evil.example/a.pdf",
    "https://caféf.vn/a.pdf",
    "https://cafef.vn/a b.pdf",
    "file:///c:/windows/win.ini",
    "",
    None,
])
def test_host_allowlist_rejects(url, filings):
    filings(_mini_pdf([LONG_TEXT]))
    with pytest.raises(ValueError):
        F.read_report(url, "FPT")


@pytest.mark.parametrize("url", ["https://cafefnew.mediacdn.vn/a.pdf?v=1", "https://CAFEF1.mediacdn.vn/a.pdf", "https://cafef.vn:443/a.pdf"])
def test_host_allowlist_accepts(url):
    assert F._check_report_url(url) == url


def test_redirect_to_other_host_is_never_requested_and_nothing_saved(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    requested = []

    def fake_fetch(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        requested.append(url)
        response = _Response(b"", headers={"Location": "https://evil.example/a.pdf"})
        response.is_redirect = True
        return response

    monkeypatch.setattr(core, "fetch", fake_fetch)
    with pytest.raises(ValueError):
        F.read_report(URL, "FPT")
    assert requested == [URL]
    assert not list((tmp_path / "filings" / "FPT").iterdir())


def test_render_report_pages_writes_png_and_validates_pages(filings, tmp_path):
    filings(_mini_pdf([LONG_TEXT, LONG_TEXT, LONG_TEXT]))
    result = F.render_report_pages(URL, "FPT", pages="1,3")
    assert result["pages"] == 3 and [i["page"] for i in result["images"]] == [1, 3]
    for image in result["images"]:
        with open(image["path"], "rb") as handle:
            assert handle.read(4) == b"\x89PNG"
    for bad in ("0", "4", "2-1", "abc", "1;2", "../x"):
        with pytest.raises(ValueError):
            F.render_report_pages(URL, "FPT", pages=bad)


@pytest.mark.live
def test_live_fpt_associates_line_matches_filing():
    result = F.analyze("FPT")
    q3_2026 = next(row for row in result["statements"] if row["period"] == "Q3/2026")
    assert q3_2026["associates_profit"] == pytest.approx(796.9, abs=0.05)  # filing PDF p.8
    assert result["profitability"]["associates_share_of_pbt_ttm"] is not None


def test_page_limit_per_render_call():
    assert F._parse_pages("3-5,9", 54) == [3, 4, 5, 9]
    with pytest.raises(ValueError):
        F._parse_pages("1-21", 54)
    with pytest.raises(ValueError):
        F._parse_pages("1-15,20-30", 54)


@pytest.mark.parametrize("body", [b"<html>not a pdf</html>", b"PK\x03\x04zipfile", b"%PD", b""])
def test_non_pdf_is_rejected_and_not_kept(body, filings, tmp_path):
    filings(body)
    with pytest.raises(core.SourceError):
        F.read_report(URL, "FPT")
    assert not list((tmp_path / "filings" / "FPT").iterdir())


def test_size_limit_declared_and_actual(filings, monkeypatch, tmp_path):
    response = filings(b"%PDF-1.4" + b"x" * 100, headers={"Content-Length": "999999999"})
    with pytest.raises(core.SourceError):
        F.read_report(URL, "FPT")
    assert response.closed
    monkeypatch.setattr(F, "MAX_PDF_BYTES", 50)
    filings(b"%PDF-1.4" + b"x" * 100)
    with pytest.raises(core.SourceError):
        F.read_report(URL, "FPT")
    assert not list((tmp_path / "filings" / "FPT").iterdir())


def test_read_report_extracts_text_and_sanitises_filename(filings, tmp_path, monkeypatch):
    filings(_mini_pdf([LONG_TEXT, LONG_TEXT + " page two"]))
    result = F.read_report("https://cafefnew.mediacdn.vn/x/..%2F..%2Fevil name.pdf".replace(" ", "%20"), "fpt")
    json.dumps(result, allow_nan=False)
    assert re.fullmatch(r"[0-9a-f]{24}\.pdf", result["path"].replace("\\", "/").rsplit("/", 1)[-1])
    assert result["path"].startswith(str(tmp_path / "filings" / "FPT"))
    assert result["pages"] == 2 and result["scanned"] is False
    assert "13754159003395" in result["text_by_page"][0]["text"]
    assert result["text_by_page"][1]["page"] == 2
    assert set(result) == {"path", "pages", "text_by_page", "tables", "scanned", "warnings"}


def test_read_report_max_pages_and_cache(filings, monkeypatch):
    filings(_mini_pdf([LONG_TEXT, LONG_TEXT, LONG_TEXT]))
    result = F.read_report(URL, "FPT", max_pages=1)
    assert result["pages"] == 3 and len(result["text_by_page"]) == 1
    assert any("1/3" in w for w in result["warnings"])

    def boom(url, **k):
        raise AssertionError("should use the cached file")
    monkeypatch.setattr(core, "fetch", boom)
    assert F.read_report(URL + "?v=2", "FPT")["pages"] == 3
    with pytest.raises(ValueError):
        F.read_report(URL, "FPT", max_pages=0)


def test_scanned_pdf_is_flagged_not_returned_as_fine(filings):
    filings(_mini_pdf(["", "", "", "", "", "", ""]))
    result = F.read_report(URL, "FPT")
    assert result["scanned"] is True
    assert any("scan" in w for w in result["warnings"])
    assert result["tables"] == []


def test_mostly_blank_pdf_with_a_text_cover_is_still_scanned(filings):
    filings(_mini_pdf([LONG_TEXT] + [""] * 9))
    assert F.read_report(URL, "FPT")["scanned"] is True


def test_partly_scanned_pdf_warns(filings):
    filings(_mini_pdf([LONG_TEXT, LONG_TEXT, ""]))
    result = F.read_report(URL, "FPT")
    assert result["scanned"] is False
    assert any("2/3" in w for w in result["warnings"])


def test_corrupt_pdf_with_valid_magic_raises_source_error(filings):
    filings(b"%PDF-1.4\nthis is not really a pdf")
    with pytest.raises(core.SourceError):
        F.read_report(URL, "FPT")


# ---------------------------------------------------------------- live

@pytest.mark.live
def test_live_fpt():
    result = F.analyze("FPT", quarters=12)
    assert result["company_type"] == "non_financial"
    assert result["red_flags"] is not None and result["valuation"]["market_cap"] > 0
    row = next(r for r in result["statements"] if r["period"] == "Q3/2026")
    # Figures read from the consolidated Q3/2026 filing (B02a, B01a).
    assert row["revenue"] == pytest.approx(13754.16, abs=0.01)
    assert row["parent_profit"] == pytest.approx(2723.12, abs=0.01)
    assert row["equity"] == pytest.approx(44039.38, abs=0.01)
    assert result["data_basis"][CF]["2025"]["basis"] == "standalone"


@pytest.mark.live
def test_live_vcb():
    result = F.analyze("VCB", quarters=12)
    assert result["company_type"] == "bank"
    row = next(r for r in result["statements"] if r["period"] == "Q3/2026")
    assert row["nii"] == pytest.approx(20385.96, abs=0.01)
    assert row["parent_profit"] == pytest.approx(9033.30, abs=0.01)
    assert row["equity"] == pytest.approx(253761.15, abs=0.01)
    assert result["profitability"]["nim"] > 0 and result["profitability"]["npl_ratio"] is not None


@pytest.mark.live
def test_live_ssi():
    result = F.analyze("SSI", quarters=12)
    assert result["company_type"] == "securities"
    assert result["resolved_fields"]["pbt"]["field"] == "cfa1"  # isa16 is listed twice in the SSI catalogue
    assert result["statements"][0]["revenue"] > 0
    assert result["liquidity_leverage"]["margin_loans_to_equity"] > 0


@pytest.mark.live
def test_live_insurer_is_not_supported():
    result = F.analyze("BVH")
    assert result["company_type"] == "insurance" and result["supported"] is False and result["raw_lines"]


@pytest.mark.live
def test_live_fpt_report_listing_and_scanned_pdf():
    reports = F.list_reports("FPT", 2026)
    report = next(r for r in reports if r["quarter"] == 3 and r["consolidated"])
    result = F.read_report(report["url"], "FPT", max_pages=8)
    assert result["pages"] > 8 and result["scanned"] is True  # the Q3/2026 filing is image-only
