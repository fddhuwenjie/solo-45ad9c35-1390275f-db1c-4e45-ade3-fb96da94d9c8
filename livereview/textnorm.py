"""分词与文本规范化。

规则：
  - 先做 NFKC 规范化（全角→半角、兼容字符折叠）；
  - CJK 表意文字每字一个词元；
  - 连续拉丁字母/数字（含撇号）为一个词元，匹配时小写化；
  - 其余非空白字符为标点词元（参与展示，不参与对齐匹配）。
"""

import re
import unicodedata

_CJK = r"㐀-䶿一-鿿豈-﫿"
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?")
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?|[%s]|[^\sA-Za-z0-9%s]" % (_CJK, _CJK)
)
_CJK_RE = re.compile(r"[%s]" % _CJK)


def tokenize(text):
    """把一段文本切成词元列表：{text, norm, kind, pos}。"""
    text = unicodedata.normalize("NFKC", text or "")
    toks = []
    for m in _TOKEN_RE.finditer(text):
        s = m.group(0)
        if _WORD_RE.fullmatch(s):
            kind, norm = "word", s.lower()
        elif _CJK_RE.fullmatch(s):
            kind, norm = "cjk", s
        else:
            kind, norm = "punct", s
        toks.append({"text": s, "norm": norm, "kind": kind, "pos": m.start()})
    return toks


def content_tokens(toks):
    """去掉标点，只保留参与对齐的内容词元。"""
    return [t for t in toks if t["kind"] != "punct"]


def norms(toks):
    return [t["norm"] for t in toks]


def char_count(text):
    """屏幕占用字数（非空白字符数），用于阅读速度。"""
    return sum(1 for ch in (text or "") if not ch.isspace())


def join_tokens(toks):
    """把词元重新拼成可读文本：两个拉丁词之间补空格，其余直接相连。"""
    out = []
    prev = None
    for t in toks:
        if prev is not None and prev["kind"] == "word" and t["kind"] == "word":
            out.append(" ")
        out.append(t["text"])
        prev = t
    return "".join(out)
