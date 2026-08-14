"""Tests for the UI's logic layer, with a fake encoder.

Everything the Streamlit app computes lives in `scan`, so it is testable here
without a browser, a model or a network. The fake follows the pattern in
test_tier2_encoder.py: it records what it was asked, so the tests can assert the
things that would silently corrupt a batch -- the threshold actually used, the
chunk sizes, and which row each finding lands on.
"""
from __future__ import annotations

import html
import re

import pytest

from pipelineguard import processor, scan
from pipelineguard.detectors.tier1_rules import RulesDetector
from pipelineguard.detectors.tier2_encoder import Tier2Detector
from pipelineguard.models import Finding, Tier

CNIC = "35202-1234567-1"


class FakeGLiNER:
    """Hits on a trigger word, and records the (size, labels, threshold) of
    every call so batching and the threshold can be asserted."""

    TRIGGERS = {"Ayesha": "person", "Islamabad": "location"}

    def __init__(self, raises: bool = False):
        self.calls: list[tuple[int, tuple[str, ...], float]] = []
        # Kept apart from `calls` so the shielding tests can read the exact
        # text the encoder was handed without disturbing the batching asserts.
        self.texts: list[str] = []
        self.raises = raises

    def eval(self):
        return self

    def batch_predict_entities(self, texts, labels, threshold=0.25):
        self.calls.append((len(texts), tuple(labels), threshold))
        self.texts.extend(texts)
        if self.raises:
            raise RuntimeError("boom")
        out = []
        for text in texts:
            hits = []
            for word, label in self.TRIGGERS.items():
                if label in labels and word in text:
                    start = text.index(word)
                    hits.append({"start": start, "end": start + len(word),
                                 "label": label, "score": 0.91})
            out.append(hits)
        return out


@pytest.fixture(scope="module")
def tier1() -> RulesDetector:
    return RulesDetector()


def make_tier2(batch_size: int = 4, raises: bool = False) -> Tier2Detector:
    d = Tier2Detector("fake/model", threshold=0.25, device="cpu",
                      batch_size=batch_size)
    d._model = FakeGLiNER(raises=raises)
    return d


# --------------------------------------------------------------------------- #
# scan_text
# --------------------------------------------------------------------------- #
def test_rules_only_redacts_a_cnic(tier1):
    result = scan.scan_text(f"Sent by {CNIC} today", tier1=tier1)
    assert result.redacted == "Sent by [CNIC] today"
    assert [f.entity_type for f in result.findings] == ["CNIC"]


def test_no_findings_is_passthrough(tier1):
    result = scan.scan_text("Zakat contribution", tier1=tier1)
    assert result.redacted == "Zakat contribution"
    assert result.findings == () and result.spans == ()
    assert result.coverage == 0.0


def test_coverage_is_zero_when_text_has_no_alphanumerics(tier1):
    """Guards a ZeroDivisionError: '!!!' has nothing identifying to cover."""
    result = scan.scan_text("!!! ---", tier1=tier1)
    assert result.identifying_chars == 0
    assert result.coverage == 0.0


def test_coverage_counts_only_alphanumerics(tier1):
    result = scan.scan_text(CNIC, tier1=tier1)
    # 13 digits inside the span; the two hyphens count for neither side.
    assert result.identifying_chars == 13
    assert result.masked_chars == 13
    assert result.coverage == 1.0


def test_entity_type_filter_drops_only_that_type(tier1):
    text = f"CNIC {CNIC} mail ayesha@example.com"
    result = scan.scan_text(text, tier1=tier1, entity_types={"CNIC"})
    assert [f.entity_type for f in result.findings] == ["CNIC"]
    assert "ayesha@example.com" in result.redacted


def test_entity_type_filter_of_none_keeps_everything(tier1):
    text = f"CNIC {CNIC} mail ayesha@example.com"
    result = scan.scan_text(text, tier1=tier1, entity_types=None)
    assert {f.entity_type for f in result.findings} == {"CNIC", "EMAIL"}


def test_redaction_matches_the_shipped_merge(tier1):
    """The anti-drift guard. If scan ever grows its own span arithmetic, the
    UI starts claiming redactions the pipeline would not perform."""
    text = f"Ayesha {CNIC} Islamabad"
    result = scan.scan_text(text, tier1=tier1, tier2=make_tier2())
    assert result.redacted == processor.redact(text, list(result.findings))
    assert result.spans == tuple(processor.merge_spans(list(result.findings)))


def test_tier2_findings_are_included(tier1):
    result = scan.scan_text("Ayesha went to Islamabad", tier1=tier1,
                            tier2=make_tier2())
    assert {f.entity_type for f in result.findings} == {"PERSON_NAME", "ADDRESS"}
    assert all(f.tier == Tier.ENCODER for f in result.findings)


def test_findings_are_sorted_by_position(tier1):
    text = f"Islamabad {CNIC} Ayesha"
    result = scan.scan_text(text, tier1=tier1, tier2=make_tier2())
    starts = [f.span_start for f in result.findings]
    assert starts == sorted(starts)


# --------------------------------------------------------------------------- #
# shielding — the encoder must not read characters a rule already owns
# --------------------------------------------------------------------------- #
def test_encoder_never_sees_a_rule_claimed_span(tier1):
    """Row 3 of the reported file in miniature. Unshielded, the encoder reads
    an email domain as a place and bridges from it into the next address,
    swallowing the text between. See findings §26."""
    tier2 = make_tier2()
    text = f"Ayesha {CNIC} lives in Islamabad"

    scan.scan_text(text, tier1=tier1, tier2=tier2)

    seen = tier2._model.texts[0]
    assert CNIC not in seen
    assert len(seen) == len(text)
    # Everything the rules did not claim must survive untouched.
    assert "Ayesha" in seen and "Islamabad" in seen


def test_batch_also_shields_every_row(tier1):
    tier2 = make_tier2(batch_size=2)
    texts = [f"Ayesha {CNIC}", f"Islamabad {CNIC}"]

    scan.scan_batch(texts, tier1=tier1, tier2=tier2)

    # One forward pass per label group, so each row is handed over more than
    # once. Every copy must be shielded, which is what the set comparison says.
    assert set(tier2._model.texts) == {
        "Ayesha" + " " * (len(texts[0]) - len("Ayesha")),
        "Islamabad" + " " * (len(texts[1]) - len("Islamabad")),
    }


def test_shielding_leaves_the_result_text_and_spans_on_the_original(tier1):
    """Blanking is length-preserving, so encoder offsets still index the text
    the user typed. A shorter placeholder would shift every later span."""
    tier2 = make_tier2()
    text = f"{CNIC} Ayesha"

    result = scan.scan_text(text, tier1=tier1, tier2=tier2)

    assert result.text == text
    person = next(f for f in result.findings if f.entity_type == "PERSON_NAME")
    assert text[person.span_start:person.span_end] == "Ayesha"


def test_encoder_finding_inside_a_rule_span_is_dropped(tier1):
    """The fake hits on 'Ayesha' wherever it appears, including inside a span
    the rules already own. That guess must not reach the findings table."""
    tier2 = make_tier2()
    # The rules claim the whole email; 'Ayesha' sits inside it.
    result = scan.scan_text("mail Ayesha.k@example.com", tier1=tier1,
                            tier2=tier2)

    assert [f.entity_type for f in result.findings] == ["EMAIL"]
    assert result.redacted == "mail [EMAIL]"


# --------------------------------------------------------------------------- #
# threshold
# --------------------------------------------------------------------------- #
def test_threshold_is_applied_and_restored(tier1):
    tier2 = make_tier2()
    scan.scan_text("Ayesha", tier1=tier1, tier2=tier2, threshold=0.8)
    assert {c[2] for c in tier2._model.calls} == {0.8}
    assert tier2.threshold == 0.25


def test_threshold_restored_when_detect_raises(tier1):
    tier2 = make_tier2(raises=True)
    with pytest.raises(RuntimeError, match="boom"):
        scan.scan_text("Ayesha", tier1=tier1, tier2=tier2, threshold=0.8)
    assert tier2.threshold == 0.25


def test_threshold_of_none_leaves_the_detector_alone(tier1):
    tier2 = make_tier2()
    scan.scan_text("Ayesha", tier1=tier1, tier2=tier2, threshold=None)
    assert {c[2] for c in tier2._model.calls} == {0.25}


def test_span_widener_is_applied_and_restored(tier1):
    """The widener changes detection, not display, so it is set on the detector
    and put back -- the same shared-instance hazard as the threshold."""
    tier2 = make_tier2()
    assert tier2.extend_addresses is True
    seen = []
    original = tier2._to_findings
    tier2._to_findings = lambda *a, **k: (
        seen.append(tier2.extend_addresses) or original(*a, **k)
    )
    scan.scan_text("Islamabad", tier1=tier1, tier2=tier2, extend_addresses=False)
    assert seen == [False]
    assert tier2.extend_addresses is True


def test_span_widener_off_leaves_the_address_span_unwidened(tier1):
    """With the widener on, 'House 12, Islamabad' keeps the leading house
    number; with it off, only the model's own span survives."""
    text = "House 12, Islamabad"
    wide = scan.scan_text(text, tier1=tier1, tier2=make_tier2(),
                          extend_addresses=True)
    narrow = scan.scan_text(text, tier1=tier1, tier2=make_tier2(),
                            extend_addresses=False)
    assert wide.findings[0].span_start < narrow.findings[0].span_start
    assert narrow.findings[0].span_start == text.index("Islamabad")


def test_batch_applies_the_span_widener(tier1):
    tier2 = make_tier2()
    results = scan.scan_batch(["House 12, Islamabad"], tier1=tier1, tier2=tier2,
                              extend_addresses=False)
    assert results[0].findings[0].span_start == 10
    assert tier2.extend_addresses is True


# --------------------------------------------------------------------------- #
# scan_batch
# --------------------------------------------------------------------------- #
def test_batch_preserves_row_order_and_length(tier1):
    """detect_batch omits keys with no findings. Reading results back by
    position instead of by key shifts every later row onto the wrong text."""
    texts = ["nothing here", "Ayesha", "still nothing", "Islamabad", "quiet"]
    results = scan.scan_batch(texts, tier1=tier1, tier2=make_tier2(batch_size=2))

    assert len(results) == len(texts)
    assert [r.text for r in results] == texts
    assert [[f.entity_type for f in r.findings] for r in results] == [
        [], ["PERSON_NAME"], [], ["ADDRESS"], [],
    ]


def test_batch_chunks_at_batch_size_and_reports_progress(tier1):
    seen: list[tuple[int, int]] = []
    tier2 = make_tier2(batch_size=2)
    scan.scan_batch(["a", "b", "c", "d", "e"], tier1=tier1, tier2=tier2,
                    progress=lambda done, total: seen.append((done, total)))

    # Two label groups per chunk, so sizes repeat: 2,2 | 2,2 | 1,1.
    assert [c[0] for c in tier2._model.calls] == [2, 2, 2, 2, 1, 1]
    assert seen == [(2, 5), (4, 5), (5, 5)]


def test_batch_without_tier2_still_reports_progress(tier1):
    seen: list[tuple[int, int]] = []
    results = scan.scan_batch([f"CNIC {CNIC}", "clean"], tier1=tier1,
                              progress=lambda d, t: seen.append((d, t)))
    assert [r.redacted for r in results] == ["CNIC [CNIC]", "clean"]
    assert seen == [(1, 2), (2, 2)]


def test_batch_applies_the_entity_filter(tier1):
    results = scan.scan_batch(["Ayesha in Islamabad"], tier1=tier1,
                              tier2=make_tier2(), entity_types={"ADDRESS"})
    assert [f.entity_type for f in results[0].findings] == ["ADDRESS"]


def test_batch_of_nothing_is_empty(tier1):
    assert scan.scan_batch([], tier1=tier1, tier2=make_tier2()) == []


# --------------------------------------------------------------------------- #
# truncation
# --------------------------------------------------------------------------- #
def test_long_text_is_cut_and_flagged(tier1):
    result = scan.scan_text("x" * 50, tier1=tier1, max_chars=10)
    assert result.truncated is True
    assert result.text == "x" * 10


def test_short_text_is_not_flagged(tier1):
    assert scan.scan_text("short", tier1=tier1).truncated is False


def test_batch_flags_only_the_long_rows(tier1):
    results = scan.scan_batch(["short", "x" * 50], tier1=tier1, max_chars=10)
    assert [r.truncated for r in results] == [False, True]


# --------------------------------------------------------------------------- #
# highlight_html
# --------------------------------------------------------------------------- #
def test_highlight_escapes_markup(tier1):
    out = scan.highlight_html("<script>a & b</script>", [])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out and "&amp;" in out


def test_highlight_covers_every_character_exactly_once():
    """Stripping the tags must reproduce the input. Catches off-by-one span
    arithmetic, which would silently duplicate or drop characters."""
    text = "Ayesha <b>*x*</b> & Co"
    spans = [(0, 6, "PERSON_NAME"), (9, 12, "ADDRESS")]
    stripped = re.sub(r"<[^>]+>", "", scan.highlight_html(text, spans))
    assert stripped == html.escape(text)


def test_highlight_marks_each_span(tier1):
    out = scan.highlight_html("Ayesha here", [(0, 6, "PERSON_NAME")])
    assert out.count("<mark") == 1
    assert 'title="PERSON_NAME"' in out


def test_highlight_with_no_spans_has_no_marks():
    assert "<mark" not in scan.highlight_html("nothing", [])


# --------------------------------------------------------------------------- #
# read_rows
# --------------------------------------------------------------------------- #
def test_read_rows_txt_skips_blanks():
    data = b"first line\n\n   \nsecond line\n"
    assert scan.read_rows(data, "memos.txt") == ["first line", "second line"]


def test_read_rows_csv_picks_the_named_column():
    data = b"id,memo,amount\n1,Transfer to Ayesha,500\n2,Zakat,100\n"
    assert scan.read_rows(data, "x.csv", column="memo") == [
        "Transfer to Ayesha", "Zakat",
    ]


def test_read_rows_csv_defaults_to_the_first_column():
    data = b"memo,amount\nRent payment,500\n"
    assert scan.read_rows(data, "x.csv") == ["Rent payment"]


def test_read_rows_csv_unknown_column_names_the_real_ones():
    data = b"id,memo\n1,hello\n"
    with pytest.raises(ValueError, match="id, memo"):
        scan.read_rows(data, "x.csv", column="note")


def test_read_rows_csv_tolerates_a_utf8_bom():
    data = "﻿memo\nRent payment\n".encode("utf-8")
    assert scan.read_rows(data, "x.csv", column="memo") == ["Rent payment"]


def test_csv_columns_are_stripped():
    assert scan.csv_columns(b"id, memo ,amount\n1,x,2\n") == [
        "id", "memo", "amount",
    ]


def test_csv_columns_of_empty_file_is_empty():
    assert scan.csv_columns(b"") == []


def test_widest_column_prefers_text_over_an_id():
    """The first column is usually an id. Defaulting to it makes the tool find
    nothing on its first run, which reads as broken rather than misconfigured."""
    data = b"id,memo,amount\n1,Transfer to Ayesha Malik in Karachi,500\n2,Zakat,100\n"
    assert scan.widest_column(data) == "memo"


def test_widest_column_of_header_only_file_is_the_first():
    assert scan.widest_column(b"id,memo\n") == "id"


def test_widest_column_of_empty_file_is_none():
    assert scan.widest_column(b"") is None


def test_widest_column_breaks_ties_leftmost():
    assert scan.widest_column(b"a,b\nxx,yy\n") == "a"


# --------------------------------------------------------------------------- #
# without_text -- what the batch tab holds after a scan
# --------------------------------------------------------------------------- #
def test_without_text_drops_the_original_and_its_spans(tier1):
    result = scan.scan_text(f"Sent by {CNIC} today", tier1=tier1)

    stripped = result.without_text()

    assert stripped.text == ""
    assert stripped.spans == ()
    assert CNIC not in stripped.text
    assert CNIC not in stripped.redacted


def test_without_text_keeps_every_figure_the_batch_view_needs(tier1):
    result = scan.scan_text(f"Sent by {CNIC} today", tier1=tier1)

    stripped = result.without_text()

    assert stripped.redacted == result.redacted
    assert stripped.findings == result.findings
    assert stripped.masked_chars == result.masked_chars
    assert stripped.identifying_chars == result.identifying_chars
    assert stripped.truncated == result.truncated
    assert stripped.coverage == result.coverage


def test_without_text_leaves_the_original_result_alone(tier1):
    """Frozen dataclass, so this returns a copy rather than mutating in place.
    The playground still needs the text it was given."""
    text = f"Sent by {CNIC} today"
    result = scan.scan_text(text, tier1=tier1)

    result.without_text()

    assert result.text == text
    assert result.spans


def test_findings_carry_no_value_so_stripping_text_leaves_nothing(tier1):
    """The reason dropping the text is sufficient: a Finding stores a span and
    a type, never the matched characters."""
    result = scan.scan_text(f"Sent by {CNIC} today", tier1=tier1).without_text()

    assert CNIC not in repr(result)
