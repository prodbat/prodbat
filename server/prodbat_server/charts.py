"""Time buckets and small server-drawn charts for the engineer views. Plain
SVG, so the pages need no JavaScript library and work without scripts."""

import datetime as dt
from dataclasses import dataclass

from markupsafe import Markup, escape
from sqlalchemy import text

RANGES = {
    "1h": (dt.timedelta(hours=1), 60),
    "24h": (dt.timedelta(hours=24), 48),
    "7d": (dt.timedelta(days=7), 42),
    "30d": (dt.timedelta(days=30), 30),
}
COLORS = {"green": "#1B7245", "red": "#B93A0B", "amber": "#B7791F", "ink": "#15181B", "grey": "#9AA0A6",
          "mint": "#5FBF86", "blue": "#2F6690"}


@dataclass
class Window:
    key: str
    start: dt.datetime
    end: dt.datetime
    step: dt.timedelta
    n: int

    def label(self, i: int) -> str:
        at = self.start + self.step * i
        return f"{at:%H:%M}" if self.span <= dt.timedelta(days=1) else f"{at:%-d %b}"

    @property
    def span(self) -> dt.timedelta:
        return self.end - self.start


def window(key: str, now: dt.datetime | None = None) -> Window:
    key = key if key in RANGES else "24h"
    span, n = RANGES[key]
    end = now or dt.datetime.now(dt.UTC)
    return Window(key, end - span, end, span / n, n)


async def counts(db, w: Window, table: str, project_id, where: str = "", params: dict | None = None,
                 value: str = "count(*)") -> list[float]:
    """One number per bucket: by default how many rows, or any aggregate."""
    rows = await db.execute(text(
        f"select floor(extract(epoch from ts - :start) / :step)::int as b, {value} as v from {table} "
        f"where project_id = :pid and ts >= :start and ts < :end {where} group by b"),
        {"start": w.start, "end": w.end, "step": w.step.total_seconds(), "pid": project_id, **(params or {})})
    out = [0.0] * w.n
    for b, v in rows:
        if 0 <= b < w.n and v is not None:
            out[b] = float(v)
    return out


def bars(series: list[tuple[str, list[float], str]], w: Window, height: int = 120, unit: str = "",
         max_value: float | None = None) -> Markup:
    """Stacked bars, one per bucket, with the top value and the time labels."""
    totals = [sum(values[i] for _, values, _ in series) for i in range(w.n)]
    top = max_value or max(totals + [0]) or 1
    width, gap = 1000, 2
    bw = width / w.n
    rects = []
    for i in range(w.n):
        y = height
        for name, values, color in series:
            v = values[i]
            if v <= 0:
                continue
            h = max(1.5, v / top * (height - 4))
            y -= h
            rects.append(f'<rect x="{i * bw + gap / 2:.1f}" y="{y:.1f}" width="{bw - gap:.1f}" height="{h:.1f}" '
                         f'fill="{COLORS.get(color, color)}"><title>{escape(w.label(i))}: {escape(name)} '
                         f'{v:,.0f}{escape(unit)}</title></rect>')
    legend = "".join(f'<span><i style="background:{COLORS.get(c, c)}"></i>{escape(n)}</span>' for n, _, c in series)
    return Markup(
        f'<figure class="chart"><div class="chart-top"><span class="legend">{legend}</span>'
        f'<span class="muted">{top:,.0f}{escape(unit)}</span></div>'
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
        f'aria-label="{escape(", ".join(n for n, _, _ in series))} over time">'
        f'<line x1="0" y1="{height - 0.5}" x2="{width}" y2="{height - 0.5}" stroke="#DADDD5"/>{"".join(rects)}</svg>'
        f'<figcaption class="muted"><span>{escape(w.label(0))}</span><span>{escape(w.label(w.n // 2))}</span>'
        f'<span>now</span></figcaption></figure>')


def spark(values: list[float], color: str = "red") -> Markup:
    top = max(values + [0]) or 1
    width, height = 120, 24
    bw = width / max(1, len(values))
    rects = "".join(f'<rect x="{i * bw:.1f}" y="{height - max(1, v / top * height):.1f}" width="{max(bw - 1, 1):.1f}" '
                    f'height="{max(1, v / top * height):.1f}" fill="{COLORS[color] if v else "#ECEEE8"}"/>'
                    for i, v in enumerate(values))
    return Markup(f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
                  f'aria-hidden="true">{rects}</svg>')
