"""对齐：终稿↔参考稿、快照↔终稿。

终稿↔参考稿的对齐结果决定每个终稿词元的参照词元（延迟计算的基准）。
当终稿与参考稿在一个替换块内长度不一致时，无法确定一一对应关系，
这些词元标记为 ambiguous（文本多解），其相对参考稿的指标保持未定。
"""

from difflib import SequenceMatcher


def align_final_to_ref(ref_norms, fin_norms):
    """返回 (mapping, status, ambiguous_blocks, missing_ref)。

    mapping[j] = i  表示终稿内容词元 j 对应参考稿内容词元 i（None 表示无对应）。
    status[j] ∈ match | sub | extra | ambiguous
    missing_ref: 参考稿中存在但终稿缺失的内容词元下标列表。
    """
    sm = SequenceMatcher(a=ref_norms, b=fin_norms, autojunk=False)
    mapping = [None] * len(fin_norms)
    status = ["extra"] * len(fin_norms)
    ambiguous_blocks = []
    missing_ref = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                mapping[j1 + k] = i1 + k
                status[j1 + k] = "match"
        elif tag == "replace":
            if (i2 - i1) == (j2 - j1):
                for k in range(j2 - j1):
                    mapping[j1 + k] = i1 + k
                    status[j1 + k] = "sub"
            else:
                for j in range(j1, j2):
                    status[j] = "ambiguous"
                ambiguous_blocks.append({"ref": [i1, i2], "final": [j1, j2]})
        elif tag == "delete":
            missing_ref.extend(range(i1, i2))
        # tag == "insert": 终稿多出的词元，保持 extra
    return mapping, status, ambiguous_blocks, missing_ref


def map_snapshot_to_final(final_norms, snap_tokens):
    """把一条快照的内容词元对齐到终稿内容词元。

    返回 values：values[j] 为该快照中与终稿词元 j 对齐的快照词元（dict），
    没有对应时为 None。替换块按下标顺序配对，超长部分舍弃/留空——
    快照对齐只用于追踪"同一位置显示了什么"，不影响多解判定。
    """
    snap_norms = [t["norm"] for t in snap_tokens]
    sm = SequenceMatcher(None, final_norms, snap_norms, autojunk=False)
    values = [None] * len(final_norms)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                values[i1 + k] = snap_tokens[j1 + k]
        elif tag == "replace":
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                values[i1 + k] = snap_tokens[j1 + k]
        # delete: 快照缺少该词 → None；insert: 快照多出的词忽略
    return values
