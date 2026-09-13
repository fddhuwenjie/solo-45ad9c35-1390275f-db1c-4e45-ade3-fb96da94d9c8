"""锚点：日志时钟 → 音频时钟的拟合。

校审员给出若干 (log_ts, audio_ts) 锚点对。≥2 个锚点时做最小二乘线性
拟合 audio = a·log + b（允许时钟漂移）；1 个锚点时只估偏移（a=1）；
0 个锚点时时钟映射不可用，所有依赖音频时刻的指标未定。

拟合残差过大说明锚点本身矛盾（或日志时钟不是匀速的），
此时延迟类指标保持未定。
"""


def fit_clock(anchors):
    """anchors: [{'log_ts':..,'audio_ts':..}, ...]（按 log_ts 排序）。

    返回 None（无锚点）或 {a, b, residuals, max_residual, map}。
    """
    if not anchors:
        return None
    pts = sorted(anchors, key=lambda a: a["log_ts"])
    xs = [p["log_ts"] for p in pts]
    ys = [p["audio_ts"] for p in pts]
    if len(pts) == 1:
        a, b = 1.0, ys[0] - xs[0]
        residuals = [0.0]
    else:
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx < 1e-9:
            a = 1.0
        else:
            a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        b = my - a * mx
        residuals = [abs(a * x + b - y) for x, y in zip(xs, ys)]
    return {
        "a": a,
        "b": b,
        "residuals": residuals,
        "max_residual": max(residuals),
        "n": len(pts),
    }


def map_time(clock, log_ts):
    """日志时刻 → 音频时刻；无时钟时返回 None。"""
    if clock is None or log_ts is None:
        return None
    return clock["a"] * log_ts + clock["b"]
