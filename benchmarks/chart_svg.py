"""Static SVG scatter charts for the README, standard library only.

One renderer shared by plot_stt.py and plot_tts_tradeoff.py so every
speed-vs-memory chart looks the same: light and dark variants, shape plus
colour per hardware (identity never rests on colour alone), a direct label on
every point, recessive grid, optional dashed lines at a card's memory limit.
"""
import math

THEMES = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e", "grid": "#e4e3df",
              "axis": "#b9b8b2", "cpu": "#2a78d6", "gtx1650": "#eb6834", "rtx4060": "#1baf7a"},
    "dark": {"surface": "#1a1a19", "text": "#ffffff", "muted": "#c3c2b7", "grid": "#2e2e2c",
             "axis": "#55544f", "cpu": "#3987e5", "gtx1650": "#d95926", "rtx4060": "#199e70"},
}
FONT = 'font-family="system-ui, -apple-system, Segoe UI, sans-serif"'


def marker(kind, x, y, t):
    # 2px surface ring keeps overlapping marks separable.
    c, s = t[kind], t["surface"]
    if kind == "cpu":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{c}" stroke="{s}" stroke-width="2"/>'
    if kind == "gtx1650":
        return (f'<rect x="{x - 6:.1f}" y="{y - 6:.1f}" width="12" height="12" rx="2" fill="{c}" '
                f'stroke="{s}" stroke-width="2"/>')
    return (f'<path d="M{x:.1f},{y - 7.5:.1f} L{x + 7.5:.1f},{y + 5.5:.1f} L{x - 7.5:.1f},{y + 5.5:.1f} Z" '
            f'fill="{c}" stroke="{s}" stroke-width="2" stroke-linejoin="round"/>')


class Axis:
    def __init__(self, lo, hi, a, b, log=False):
        self.lo, self.hi, self.a, self.b, self.log = lo, hi, a, b, log

    def __call__(self, v):
        f = (lambda x: math.log10(x)) if self.log else (lambda x: x)
        return self.a + (self.b - self.a) * (f(v) - f(self.lo)) / (f(self.hi) - f(self.lo))


def scatter(theme, *, title, legend, points, x, y, xticks, yticks, xlabel, note,
            limits=(), connectors=(), width=820, height=500):
    """points: dicts with x, y, kind, label, value, place=(dx, dy, anchor) or None
    (a point without place gets no label, e.g. the second of a shared label).
    x/y: (lo, hi, log). limits: (x value, text) dashed verticals."""
    t = THEMES[theme]
    L, R, T, B = 84, width - 30, 104, height - 76
    sx, sy = Axis(x[0], x[1], L, R, x[2]), Axis(y[0], y[1], B, T, y[2])
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
           f'height="{height}" role="img" aria-labelledby="t d" {FONT}>',
           f'<title id="t">{title}</title>',
           '<desc id="d">' + "; ".join(f"{p['label']}: {p['value']}" for p in points if p.get("value"))
           + "</desc>",
           f'<rect width="{width}" height="{height}" fill="{t["surface"]}"/>',
           f'<text x="{L}" y="30" font-size="16" font-weight="600" fill="{t["text"]}">{title}</text>']
    lx = L
    for kind, name in legend:
        out.append(marker(kind, lx + 6, 54, t))
        out.append(f'<text x="{lx + 18}" y="58" font-size="12.5" fill="{t["muted"]}">{name}</text>')
        lx += 18 + 7.1 * len(name) + 28
    for v, label in yticks:
        yy = sy(v)
        out.append(f'<line x1="{L}" x2="{R}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{t["grid"]}" stroke-width="1"/>')
        out.append(f'<text x="{L - 10}" y="{yy + 4:.1f}" font-size="12" text-anchor="end" '
                   f'fill="{t["muted"]}">{label}</text>')
    out.append(f'<line x1="{L}" x2="{R}" y1="{B}" y2="{B}" stroke="{t["axis"]}"/>')
    for v, label in xticks:
        xx = sx(v)
        out.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{B}" y2="{B + 5}" stroke="{t["axis"]}"/>')
        out.append(f'<text x="{xx:.1f}" y="{B + 20}" font-size="12" text-anchor="middle" '
                   f'fill="{t["muted"]}">{label}</text>')
    out.append(f'<text x="{(L + R) / 2}" y="{B + 44}" font-size="12.5" text-anchor="middle" '
               f'fill="{t["muted"]}">{xlabel}</text>')
    out.append(f'<text x="{R}" y="{T - 10}" font-size="11.5" text-anchor="end" fill="{t["muted"]}">{note}</text>')
    for v, text in limits:
        xx = sx(v)
        out.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{T}" y2="{B}" stroke="{t["muted"]}" '
                   f'stroke-width="1.5" stroke-dasharray="5 4"/>')
        out.append(f'<text x="{xx - 6:.1f}" y="{T + 14}" font-size="11.5" text-anchor="end" '
                   f'fill="{t["muted"]}">{text}</text>')
    for i, j in connectors:
        a, b = points[i], points[j]
        out.append(f'<line x1="{sx(a["x"]):.1f}" y1="{sy(a["y"]):.1f}" x2="{sx(b["x"]):.1f}" '
                   f'y2="{sy(b["y"]):.1f}" stroke="{t["axis"]}" stroke-width="1.5"/>')
    for p in points:
        out.append(marker(p["kind"], sx(p["x"]), sy(p["y"]), t))
    for p in points:
        if not p.get("place"):
            continue
        dx, dy, anchor = p["place"]
        ax, ay = sx(p["x"]) + dx, sy(p["y"]) + dy
        out.append(f'<text x="{ax:.1f}" y="{ay:.1f}" font-size="12.5" font-weight="600" '
                   f'text-anchor="{anchor}" fill="{t["text"]}">{p["label"]}</text>')
        out.append(f'<text x="{ax:.1f}" y="{ay + 15:.1f}" font-size="11.5" text-anchor="{anchor}" '
                   f'fill="{t["muted"]}">{p["value"]}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def mib(v):
    return f"{v / 1024:.1f} GB" if v >= 1000 else f"{v:.0f} MiB"
