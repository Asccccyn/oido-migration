"""词汇声调词典层测试（05 韵律主链 3/8：本调不冒充表层声调）。

pypinyin==0.55.0 词典实测口径（本测试按实测断言）：
- "你好" → ['ni3','hao3']（三声连读本调 3/3，表层 2/3 属声学层，不在此测）；
- "妈妈" → ['ma1','ma1']——该词典版本把"妈"统一标 ma1（轻声未降格），
  施工包预判的 'ma1 ma'（第二音节无数字）在 0.55.0 不成立；
- 无数字的轻声路径用 "好吗" → ['hao3','ma'] 覆盖（'吗' 轻声无尾数字）。
"""

import sys

import pytest

from oido.prosody.lexicon import char_lexical_info

pypinyin = pytest.importorskip("pypinyin", reason="pypinyin 未安装则本层降级，另有降级路径测试")


class TestCharLexicalInfo:
    def test_third_tone_sandhi_word_keeps_lexical_tones(self):
        # "你好"：词典本调 3/3。表层连读变调（2/3）是声学测量，词典不冒充
        info = char_lexical_info("你好")
        assert info is not None
        assert [d["unit"] for d in info] == ["你", "好"]
        assert [d["lexical_tone"] for d in info] == [3, 3]
        assert [d["pinyin"] for d in info] == ["ni3", "hao3"]
        assert all(d["tone_source"] == "pypinyin_first_guess" for d in info)

    def test_neutral_tone_without_digit_defaults_to_5(self):
        # "好吗"：'ma' 无尾数字（轻声/未标调）→ lexical_tone=5，
        # tone_source 标明是 TONE3 缺省解释，不是测量
        info = char_lexical_info("好吗")
        assert info is not None
        assert info[1]["pinyin"] == "ma"
        assert info[1]["lexical_tone"] == 5
        assert info[1]["tone_source"] == "pypinyin_tone3_default"

    def test_mama_dictionary_gives_both_tone1_in_055(self):
        # pypinyin 0.55.0 实测：妈妈 → ma1/ma1（词典未把第二音节降为轻声）。
        # 如实记录词典口径；预判的 'ma1 ma' 路径由上例"好吗"覆盖
        info = char_lexical_info("妈妈")
        assert info is not None
        assert [d["lexical_tone"] for d in info] == [1, 1]
        assert all(d["tone_source"] == "pypinyin_first_guess" for d in info)

    def test_latin_units_get_no_fake_pinyin(self):
        info = char_lexical_info("我today好")
        assert info is not None
        assert [d["unit"] for d in info] == ["我", "today", "好"]
        assert info[1]["pinyin"] is None
        assert info[1]["lexical_tone"] is None
        assert info[1]["tone_source"] is None
        assert info[0]["pinyin"] == "wo3" and info[2]["pinyin"] == "hao3"

    def test_unit_split_matches_align_convention(self):
        # 与 align.strip_to_syllable_chars 同口径：逐单元可拼
        from oido.prosody.align import strip_to_syllable_chars
        for text in ("我today好", "行啊？", "mp3 files 好", "没事……"):
            info = char_lexical_info(text)
            assert info is not None
            assert [d["unit"] for d in info] == strip_to_syllable_chars(text)

    def test_import_error_returns_none(self, monkeypatch):
        # provider_not_configured 降级路径：pypinyin 导入失败 → None，不留半截结果
        monkeypatch.setitem(sys.modules, "pypinyin", None)
        assert char_lexical_info("你好") is None

    def test_no_han_at_all(self):
        info = char_lexical_info("ok 123")
        assert info is not None
        assert all(d["pinyin"] is None and d["lexical_tone"] is None for d in info)
        assert [d["unit"] for d in info] == ["ok", "123"]
