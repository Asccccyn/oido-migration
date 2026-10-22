"""词汇声调词典层（05 韵律主链 3/8：汉字、拼音、词汇声调分别处理）。

对应施工包条目：词典给出的**本调**不冒充实际测得声调——本模块只产出
词典本调（lexical tone）与拼音，表层变调（三声连读、一/不变调、轻声实现）
属于声学层，与这里严格分离。

口径与来源：
- 输出单元与 align.strip_to_syllable_chars 完全同口径（复用它），保证
  对齐 span 与词典信息逐单元可拼；
- pypinyin.lazy_pinyin(style=Style.TONE3)：声调数字从音节尾部解析；
  为保证逐字对位，先对文本做"汉字投影"（只保留汉字序列）再查词典，
  汉字间的拉丁/标点不进词典查询；
- tone_source 语义：
  * "pypinyin_first_guess"：词典本调，多音字取词典首个候选（已知局限，
    如"还/重/行"需上层语境或人工校订纠正）；
  * "pypinyin_tone3_default"：TONE3 输出无尾数字（轻声/未标调）→ 按轻声
    记 lexical_tone=5，这是 pypinyin 输出口径的缺省解释，不是测量结果；
- 非汉字单元（拉丁词/数字串）：pinyin=None、lexical_tone=None——不伪造；
- pypinyin 未安装（ImportError）→ 返回 None，上游按 provider_not_configured
  降级，不留半截结果。
"""

from __future__ import annotations

import re

from oido.prosody.align import strip_to_syllable_chars

_TONE3_TRAILING_DIGIT = re.compile(r"^([a-zü:]+)([1-5])$", re.IGNORECASE)


def _is_han(ch: str) -> bool:
    return "\u3400" <= ch <= "\u4dbf" or "\u4e00" <= ch <= "\u9fff"


def char_lexical_info(text: str) -> list[dict] | None:
    """逐单元词典信息：{"unit", "pinyin"(TONE3 如 "ni3"), "lexical_tone", "tone_source"}。

    词典本调口径（"你好"→ 3/3，不是表层三声连读的 2/3）；
    pypinyin 缺失或输出与单元数不齐 → None（上游降级，不硬凑）。
    """
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return None

    units = strip_to_syllable_chars(text)
    # 汉字投影：只把汉字序列喂给词典（保持词组上下文供多音字消歧），
    # 拉丁/数字单元不查词典、不伪造拼音
    han_units = [u for u in units if len(u) == 1 and _is_han(u)]
    if not han_units:
        return [
            {"unit": u, "pinyin": None, "lexical_tone": None, "tone_source": None}
            for u in units
        ]

    items = lazy_pinyin("".join(han_units), style=Style.TONE3, errors="default")
    if len(items) != len(han_units):
        return None   # 词典输出与单元不齐（理论不可达，防御性拒答）

    # 按位置消费：han_units 的顺序即 units 中汉字单元的顺序
    syllables = iter(items)
    result: list[dict] = []
    for u in units:
        if len(u) == 1 and _is_han(u):
            syllable = next(syllables)
            m = _TONE3_TRAILING_DIGIT.match(syllable)
            if m:
                # 词典本调，多音字取词典首个候选（已知局限，语境消歧归上层）
                result.append({
                    "unit": u,
                    "pinyin": syllable,
                    "lexical_tone": int(m.group(2)),
                    "tone_source": "pypinyin_first_guess",
                })
            else:
                # TONE3 无尾数字：轻声/未标调 → 按轻声 5 记（缺省解释，非测量）
                result.append({
                    "unit": u,
                    "pinyin": syllable,
                    "lexical_tone": 5,
                    "tone_source": "pypinyin_tone3_default",
                })
        else:
            # 拉丁词/数字串：不查词典、不伪造拼音
            result.append({
                "unit": u,
                "pinyin": None,
                "lexical_tone": None,
                "tone_source": None,
            })
    return result
