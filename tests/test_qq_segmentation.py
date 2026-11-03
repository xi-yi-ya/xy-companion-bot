import asyncio
import random
import re

import pytest

from plugin.rhythm import DEFAULT_RHYTHM, Rhythm, bubbles, delay, display_bubbles
from tests.test_rhythm_followup import Clock

INTRO = "我想先把这件事情的来龙去脉说清楚，这样你看到后面的材料时，就知道哪些细节值得留意。\n\n"
OUTRO = "\n\n至于接下来怎么安排，我们可以等手头的事情告一段落，再一起挑一个不那么赶的时间慢慢看。"


@pytest.mark.parametrize("text", ["", "嗯。", "好，知道了！", "晚安。明天见。", "  好的\r\n"])
def test_short_replies_remain_exactly_one_bubble(text):
    assert bubbles(text) == [text]


def test_long_paragraph_and_mixed_sentence_clause_boundaries():
    paragraph = "这件事情的细节还不少，我想先把前面的经过说完整，再慢慢看后面的变化。" * 25
    parts = bubbles(paragraph)
    assert len(parts) > 3
    assert "".join(parts) == paragraph
    assert all(p.endswith("。") for p in parts)
    # A semantic turn can split a long sentence before the closing full stop.
    clause = "前面那些事情可以慢慢整理，现在最重要的是先弄清我们真正想要什么，不必一次把所有细节都定下来，不过后面还有一种可能，我们明天可以再聊。"
    parts = bubbles(clause)
    assert len(parts) == 2 and parts[0].endswith("，")
    assert parts[1].startswith("不过") and "".join(parts) == clause
    assert bubbles("没有任何标点可以安全切开" * 100) == ["没有任何标点可以安全切开" * 100]


@pytest.mark.parametrize(
    "block",
    [
        "```python\nprint('hello?!')\n\nfor x in range(3):\n    print(x)\n```",
        "~~~~markdown\n```python\nprint(1)\n```\n\n~~~~",
        "> 别急。我们可以慢慢来！\n> 第二行保留，不能丢掉引用标记。\n>\n> 仍是引用。",
        "1. 先确定范围。\n   这一行属于第一项，包含补充说明。\n\n2. 再看结果！\n   - 嵌套条目保留。",
        "- 第一项，保持完整。\n- 第二项；也保持完整。\n- 第三项：最后讨论。",
        "| 项目 | 说明 |\n| --- | --- |\n| A | 不拆这个表。 |",
        "    print('indented code')\n    # keep this too\n\n    print('last')",
    ],
)
def test_structured_blocks_intact_with_surrounding_prose_still_split(block):
    text = INTRO + block + OUTRO
    parts = bubbles(text)
    assert len(parts) > 1
    assert "".join(parts) == text
    assert any(block in part for part in parts)


@pytest.mark.parametrize(
    "fragment",
    [
        "https://example.com/very/long/path?q=a.b&x=(a,b)",
        "[阅读这里](https://example.com/path_(a,b)?q=1)",
        "![图片](https://example.com/photo_(1).png)",
        "`result = run(a, b); print(result)`",
        "``use ` inside this code; still intact``",
        "“别着急。先把话说完，然后我们再一起想办法！”",
        '"First, take your time. Then decide!"',
        "**这一段加粗内容不能从逗号中间，随便切开。**",
    ],
)
def test_inline_units_do_not_disable_splitting_elsewhere(fragment):
    text = INTRO * 3 + fragment + OUTRO * 4
    parts = bubbles(text)
    assert len(parts) > 3 and "".join(parts) == text
    assert any(fragment in p for p in parts)


def test_unclosed_fence_preserves_rest_and_does_not_block_intro():
    tail = "```python\n" + "x = '完整保留，没有补写结尾。'\n" * 15
    text = INTRO + tail
    parts = bubbles(text)
    assert len(parts) == 2 and parts[-1] == tail
    assert "".join(parts) == text


@pytest.mark.parametrize("strategy", ["natural", "paragraph", "single"])
def test_lossless_whitespace_punctuation_and_unicode(strategy):
    text = (
        "  想到这里，我又觉得不必着急……真的？！\r\n \t\r\n"
        "Wait... really?! Price: 3.14, size: 1,000. 👍🏽👨‍👩‍👧‍👦\n"
        "还有：这部分；以及后面的事——等你有空再聊。\n\n\n"
    ) * 9 + "  \r\n"
    parts = bubbles(text, strategy)
    assert "".join(parts) == text
    assert all(part.strip() for part in parts)
    if strategy == "single":
        assert parts == [text]


def test_seeded_varied_inputs_never_drop_or_rewrite_characters():
    rng = random.Random(77)
    units = [
        "短句。",
        "真的？！",
        "仍然没结束，",
        "然后再想一想；",
        "\n",
        "\r\n \t\r\n",
        "“先别急。慢慢来！”",
        "`call(a, b)`",
        "https://example.com/a?x=1",
        "👍🏽",
    ]
    for _ in range(150):
        text = "".join(rng.choices(units, k=rng.randrange(1, 80)))
        assert "".join(bubbles(text)) == text


def test_following_gaps_vary_but_stay_short():
    rng = random.Random(5)
    gaps = [delay(INTRO, DEFAULT_RHYTHM, first=False, rng=rng) for _ in range(10)]
    assert len(set(gaps)) > 1 and all(0 < g <= 1.2 for g in gaps)


async def test_many_bubbles_have_bounded_total_wait_and_exact_receipts(store):
    clock = Clock()
    rhythm = Rhythm(store.pool, clock, clock.sleep)
    text = INTRO * 25 + OUTRO
    sent, recorded = [], []

    async def current():
        return True

    async def send(part, index):
        sent.append((index, part))
        return {"status": "sent"}

    async def record(part, index):
        recorded.append((index, part))

    result = await rhythm.deliver("room", text, -30, current, send, record, source_id=800)
    assert result["status"] == "sent" and len(sent) > 3
    assert sent == recorded == list(enumerate(display_bubbles(text)))
    assert "".join(p for _, p in sent) == "".join(display_bubbles(text))
    assert all(not p.startswith(("\n", "\r")) and not p.endswith(("\n", "\r")) for _, p in sent)
    assert clock.value <= 4.00001  # Existing generation time absorbs first delay.


async def test_new_input_stops_many_bubbles_at_exact_delivered_prefix(store):
    clock = Clock()
    rhythm = Rhythm(store.pool, clock, clock.sleep)
    text, sent, recorded = INTRO * 12, [], []

    async def current():
        return len(sent) < 2

    async def send(part, index):
        sent.append(part)
        await asyncio.sleep(0)
        return {"status": "sent"}

    async def record(part, index):
        recorded.append(part)

    result = await rhythm.deliver("room", text, -30, current, send, record, source_id=801)
    assert result["status"] == "cancelled"
    assert sent == recorded == display_bubbles(text)[:2]
    assert re.sub(r"\s+", "", text).startswith(re.sub(r"\s+", "", "".join(sent)))


@pytest.mark.parametrize("strategy", ["natural", "paragraph", "single"])
def test_display_removes_outer_blank_lines_not_internal_layout(strategy):
    text = "\r\n \t\r\n    print('one')\r\n\r\n    print('two')\r\n \t\r\n"
    assert display_bubbles(text, strategy) == ["    print('one')\r\n\r\n    print('two')"]
    assert display_bubbles("\n\n好，明天再说。\n\n", strategy) == ["好，明天再说。"]
    assert display_bubbles("\r\n  \n", strategy) == []


def test_display_keeps_internal_fence_quote_and_list_whitespace():
    for block in [
        "```python\n    x = 1\n\n    print(x)\n```",
        "> 第一行。\n>\n> 第二行。",
        "1. 第一项\n   补充内容\n\n2. 第二项",
    ]:
        assert display_bubbles("\n\n" + block + "\n\n") == [block]


def test_medium_paragraph_prefers_clause_over_a_stranded_sentence_tail():
    # Synthetic fixture with a long explanation and a short concluding sentence.
    text = "说到这个安排，前面的材料其实都已经收齐了，我倒不担心整理得慢，而是担心漏掉细节，比如把某个关键步骤忘记了，后面检查时还得重新翻一遍。那就太费时间了。"
    parts = display_bubbles(text)
    assert len(parts) >= 2
    assert "".join(parts) == text
    assert all(len(part) >= 18 for part in parts)
    assert not any(part.startswith("而是") for part in parts)
    assert not any(part == "那就太费时间了。" for part in parts)


def test_short_multisentence_paragraph_is_not_split_sentence_by_sentence():
    text = "材料整理好了。标注也都在原来的地方。明天我们再一起看看。"
    assert display_bubbles(text) == [text]
