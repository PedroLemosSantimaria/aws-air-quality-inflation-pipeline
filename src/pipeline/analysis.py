"""Analytical output generated 100% by code from the CURATED dataset.

Produces (in analytics/): report.html, summary.json and SVG charts.
No heavy dependencies (standard library only), so it runs the same locally and in Lambda.

Questions answered:
  1. Do countries with more PM2.5 also have higher inflation? (Pearson and Spearman correlation)
  2. How do PM2.5 and inflation evolve over the years? (trend)
  3. How are the numbers distributed by income group and region? (ranking/summary)
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from .config import Settings, get_settings
from .curate import CURATED_KEY
from .metadata import track_run
from .storage import Storage, get_storage
from .tabular import from_csv_bytes

log = logging.getLogger(__name__)

SOURCE_ID = "analysis"
OUT_PREFIX = "analytics"
MIN_YEARS_PER_COUNTRY = 3
INFLATION_CAP = 20.0  # the scatter's Y axis is truncated here (hyperinflation distorts the scale)
INCOME_ORDER = ["High income", "Upper middle income", "Lower middle income", "Low income"]
CASTS = {"year": int, "pm25_ugm3": float, "inflation_pct": float}


# ---------------------------------------------------------------- statistics
def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def pearson(x: list[float], y: list[float]) -> Optional[float]:
    try:
        return statistics.correlation(x, y)
    except (statistics.StatisticsError, ZeroDivisionError):
        return None


def spearman(x: list[float], y: list[float]) -> Optional[float]:
    return pearson(_ranks(x), _ranks(y))


def describe_correlation(r: Optional[float]) -> str:
    if r is None:
        return "could not be computed"
    a = abs(r)
    strength = "very weak" if a < 0.2 else "weak" if a < 0.4 else "moderate" if a < 0.6 else "strong"
    return f"{strength} {'positive' if r > 0 else 'negative'}"


# ---------------------------------------------------------------- aggregations
def country_level(rows: list[dict], min_years: int = MIN_YEARS_PER_COUNTRY) -> list[dict]:
    """One row per country: mean PM2.5 and MEDIAN inflation (the median resists outliers)."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["country_iso3"]].append(r)
    out = []
    for iso3, rs in groups.items():
        if len(rs) < min_years:
            continue
        out.append({
            "country_iso3": iso3, "country_name": rs[0]["country_name"],
            "region": rs[0]["region"], "income_level": rs[0]["income_level"],
            "years": len(rs),
            "pm25_mean": statistics.fmean(r["pm25_ugm3"] for r in rs),
            "inflation_median": statistics.median(r["inflation_pct"] for r in rs),
        })
    return sorted(out, key=lambda c: c["country_iso3"])


def group_summary(countries: list[dict], field: str, order: Optional[list[str]] = None) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in countries:
        groups[c[field]].append(c)
    out = [{
        "group": g, "countries": len(cs),
        "pm25_mean": round(statistics.fmean(c["pm25_mean"] for c in cs), 2),
        "inflation_median": round(statistics.median(c["inflation_median"] for c in cs), 2),
    } for g, cs in groups.items()]
    if order:
        out.sort(key=lambda r: order.index(r["group"]) if r["group"] in order else len(order))
    else:
        out.sort(key=lambda r: -r["pm25_mean"])
    return out


def yearly_trend(rows: list[dict]) -> list[dict]:
    groups: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["year"]].append(r)
    return [{
        "year": y, "countries": len(rs),
        "pm25_mean": round(statistics.fmean(r["pm25_ugm3"] for r in rs), 2),
        "inflation_median": round(statistics.median(r["inflation_pct"] for r in rs), 2),
    } for y, rs in sorted(groups.items())]


# ---------------------------------------------------------------- SVG (no libraries)
_W, _H, _ML, _MR, _MT, _MB = 620, 380, 64, 20, 42, 52
_FONT = "font-family:Helvetica,Arial,sans-serif"


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}"


def _sx(v, lo, hi):
    return _ML + (v - lo) / (hi - lo) * (_W - _ML - _MR) if hi > lo else (_ML + _W - _MR) / 2


def _sy(v, lo, hi):
    return _H - _MB - (v - lo) / (hi - lo) * (_H - _MT - _MB) if hi > lo else (_H - _MB + _MT) / 2


def _frame(title, xlabel, ylabel, xlo, xhi, ylo, yhi, xticks) -> list[str]:
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_W} {_H}" width="{_W}" height="{_H}" '
             f'style="{_FONT};background:#fff">',
             f'<text x="{_W / 2}" y="24" text-anchor="middle" font-size="15" font-weight="bold">{html.escape(title)}</text>']
    for i in range(6):  # grid and Y-axis labels
        v = ylo + (yhi - ylo) * i / 5
        y = _sy(v, ylo, yhi)
        parts.append(f'<line x1="{_ML}" x2="{_W - _MR}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e5e5e5"/>')
        parts.append(f'<text x="{_ML - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="#555">{_fmt(v)}</text>')
    for v in xticks:
        x = _sx(v, xlo, xhi)
        parts.append(f'<text x="{x:.1f}" y="{_H - _MB + 18}" text-anchor="middle" font-size="11" fill="#555">{_fmt(v)}</text>')
    parts.append(f'<line x1="{_ML}" x2="{_W - _MR}" y1="{_H - _MB}" y2="{_H - _MB}" stroke="#333"/>')
    parts.append(f'<line x1="{_ML}" x2="{_ML}" y1="{_MT}" y2="{_H - _MB}" stroke="#333"/>')
    parts.append(f'<text x="{(_ML + _W - _MR) / 2}" y="{_H - 10}" text-anchor="middle" font-size="12">{html.escape(xlabel)}</text>')
    parts.append(f'<text transform="translate(16 {(_MT + _H - _MB) / 2}) rotate(-90)" text-anchor="middle" font-size="12">{html.escape(ylabel)}</text>')
    return parts


def scatter_svg(countries: list[dict]) -> str:
    xs = [c["pm25_mean"] for c in countries]
    ys = [c["inflation_median"] for c in countries]
    xlo, xhi = 0.0, max(xs) * 1.05
    ylo, yhi = min(0.0, min(ys)), INFLATION_CAP
    ticks = [xlo + (xhi - xlo) * i / 5 for i in range(6)]
    parts = _frame("Mean PM2.5 vs. median inflation (one dot per country)",
                   "Mean PM2.5 exposure (\u00b5g/m\u00b3)", "Median annual inflation (%)",
                   xlo, xhi, ylo, yhi, ticks)
    clipped = 0
    for c in countries:
        x = _sx(c["pm25_mean"], xlo, xhi)
        tip = html.escape(f'{c["country_name"]}: PM2.5 {c["pm25_mean"]:.1f}, inflation {c["inflation_median"]:.1f}%')
        if c["inflation_median"] > yhi:
            clipped += 1
            y = _sy(yhi, ylo, yhi)
            parts.append(f'<path d="M{x:.1f} {y - 5:.1f} l4 8 h-8 z" fill="#c0392b" opacity="0.7"><title>{tip}</title></path>')
        else:
            y = _sy(c["inflation_median"], ylo, yhi)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.6" fill="#2c7fb8" opacity="0.65"><title>{tip}</title></circle>')
    if clipped:
        parts.append(f'<text x="{_W - _MR}" y="{_MT - 6}" text-anchor="end" font-size="10" fill="#c0392b">'
                     f'\u25b2 {clipped} countries above {INFLATION_CAP:.0f}% truncated at the top</text>')
    parts.append("</svg>")
    return "".join(parts)


def line_svg(trend: list[dict], field: str, title: str, ylabel: str, color: str) -> str:
    xs = [t["year"] for t in trend]
    ys = [t[field] for t in trend]
    pad = (max(ys) - min(ys)) * 0.15 or 1.0
    ylo, yhi = min(ys) - pad, max(ys) + pad
    xlo, xhi = min(xs), max(xs)
    parts = _frame(title, "Year", ylabel, xlo, xhi, ylo, yhi, xs)
    pts = " ".join(f"{_sx(x, xlo, xhi):.1f},{_sy(y, ylo, yhi):.1f}" for x, y in zip(xs, ys))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5"/>')
    for x, y in zip(xs, ys):
        parts.append(f'<circle cx="{_sx(x, xlo, xhi):.1f}" cy="{_sy(y, ylo, yhi):.1f}" r="3.5" fill="{color}"><title>{x}: {y}</title></circle>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- report
def _table(headers: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


_CSS = ("body{font-family:Helvetica,Arial,sans-serif;max-width:1000px;margin:32px auto;padding:0 16px;color:#222}"
        "h1{margin-bottom:4px}.sub{color:#666;margin-top:0}.box{background:#f4f8fb;border-left:4px solid #2c7fb8;"
        "padding:12px 16px;margin:16px 0}table{border-collapse:collapse;margin:8px 0 20px}"
        "th,td{border:1px solid #ddd;padding:6px 12px;text-align:right}th{background:#f0f0f0}"
        "td:first-child,th:first-child{text-align:left}.charts svg{max-width:100%;height:auto;margin:8px 0;border:1px solid #eee}"
        "small{color:#666}")


def build_report(summary: dict, scatter: str, pm_line: str, infl_line: str) -> str:
    c = summary["correlation"]
    fmt = lambda v: "n/a" if v is None else f"{v:+.2f}"  # noqa: E731
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>PM2.5 vs. inflation - analytical report</title><style>{_CSS}</style></head><body>
<h1>Air pollution vs. inflation</h1>
<p class="sub">Generated {html.escape(summary["generated_at"])} from the curated dataset
({summary["curated_rows"]} country-year rows, {summary["countries_total"]} countries, {summary["year_min"]}\u2013{summary["year_max"]}).</p>
<div class="box"><b>Key insight.</b> {html.escape(summary["insight"])}</div>
<h2>Correlation across countries</h2>
{_table(["Metric", "Value"], [["Spearman (rank) correlation", fmt(c["spearman"])],
                              ["Pearson correlation", fmt(c["pearson"])],
                              ["Countries in the calculation", c["n"]]])}
<small>Each country is summarised by its mean PM2.5 and median inflation over the years available (min. {summary["min_years_per_country"]}).
Spearman is the primary metric because inflation has extreme outliers.</small>
<div class="charts">{scatter}</div>
<h2>Trend over time</h2>
<div class="charts">{pm_line}{infl_line}</div>
{_table(["Year", "Countries", "Mean PM2.5 (\u00b5g/m\u00b3)", "Median inflation (%)"],
        [[t["year"], t["countries"], t["pm25_mean"], t["inflation_median"]] for t in summary["yearly"]])}
<h2>By income group</h2>
{_table(["Income group", "Countries", "Mean PM2.5 (\u00b5g/m\u00b3)", "Median inflation (%)"],
        [[g["group"], g["countries"], g["pm25_mean"], g["inflation_median"]] for g in summary["by_income"]])}
<h2>By region (ranked by PM2.5)</h2>
{_table(["Region", "Countries", "Mean PM2.5 (\u00b5g/m\u00b3)", "Median inflation (%)"],
        [[g["group"], g["countries"], g["pm25_mean"], g["inflation_median"]] for g in summary["by_region"]])}
<h2>Caveats</h2>
<small>Correlation is not causation: both indicators track country development and size. Country-level figures are
unweighted by population. Only countries present in both sources (matched by ISO3 code) and with enough overlapping
years are included.</small></body></html>"""


def build_summary(rows: list[dict]) -> dict:
    countries = country_level(rows)
    if len(countries) < 3:
        raise ValueError("Too few countries with enough data for the analysis")
    x = [c["pm25_mean"] for c in countries]
    y = [c["inflation_median"] for c in countries]
    sp, pe = spearman(x, y), pearson(x, y)
    years = sorted({r["year"] for r in rows})
    insight = (f"Across {len(countries)} countries, mean PM2.5 exposure and median inflation show a "
               f"{describe_correlation(sp)} rank correlation (Spearman \u03c1 = {sp:+.2f}). "
               "The relationship is a statistical association, not evidence that one drives the other.") \
        if sp is not None else "Correlation could not be computed."
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "curated_rows": len(rows), "countries_total": len({r["country_iso3"] for r in rows}),
        "year_min": years[0], "year_max": years[-1], "min_years_per_country": MIN_YEARS_PER_COUNTRY,
        "correlation": {"spearman": None if sp is None else round(sp, 4),
                        "pearson": None if pe is None else round(pe, 4), "n": len(countries)},
        "insight": insight,
        "yearly": yearly_trend(rows),
        "by_income": group_summary(countries, "income_level", INCOME_ORDER),
        "by_region": group_summary(countries, "region"),
        "_countries": countries,
    }


def run(settings: Optional[Settings] = None, storage: Optional[Storage] = None) -> dict:
    settings = settings or get_settings()
    storage = storage or get_storage(settings)
    with track_run(storage, SOURCE_ID) as run_info:
        data = storage.get_bytes(CURATED_KEY)
        if data is None:
            raise FileNotFoundError("Curated dataset not found (run the curation stage first)")
        rows = from_csv_bytes(data, CASTS)
        summary = build_summary(rows)
        countries = summary.pop("_countries")

        scatter = scatter_svg(countries)
        pm_line = line_svg(summary["yearly"], "pm25_mean", "Mean PM2.5 by year", "\u00b5g/m\u00b3", "#2c7fb8")
        infl_line = line_svg(summary["yearly"], "inflation_median", "Median inflation by year", "%", "#c0392b")

        storage.put_bytes(f"{OUT_PREFIX}/scatter_pm25_vs_inflation.svg", scatter.encode("utf-8"), "image/svg+xml")
        storage.put_bytes(f"{OUT_PREFIX}/trend_pm25.svg", pm_line.encode("utf-8"), "image/svg+xml")
        storage.put_bytes(f"{OUT_PREFIX}/trend_inflation.svg", infl_line.encode("utf-8"), "image/svg+xml")
        storage.put_json(f"{OUT_PREFIX}/summary.json", summary)
        storage.put_bytes(f"{OUT_PREFIX}/report.html",
                          build_report(summary, scatter, pm_line, infl_line).encode("utf-8"),
                          "text/html; charset=utf-8")

        run_info["metrics"].update(
            curated_rows=len(rows), countries_in_correlation=len(countries),
            spearman=summary["correlation"]["spearman"], pearson=summary["correlation"]["pearson"],
            outputs=5,
        )
        log.info("Analysis OK: %s", run_info["metrics"])
        return run_info


def main() -> None:
    argparse.ArgumentParser(description="Generate the analytical report from the curated dataset").parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
