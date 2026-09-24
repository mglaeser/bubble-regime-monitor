"""The context material a message's explanation is written from: selected
per trigger from the repository's own registries, bounded, and nothing but
repo-authored text (AGENTS.md, ground rule 1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.message_engine import context
from app.references import REGISTRY, SOURCE_REGISTRY

LIBRARY = Path(__file__).resolve().parents[1] / "config" / "message_prompts.v1.json"


def _library_triggers() -> set[str]:
    return set(json.loads(LIBRARY.read_text(encoding="utf-8"))["prompts"])


class TestTheMap:
    def test_every_library_trigger_is_decided(self):
        """A new trigger must be given material or explicitly none."""
        assert set(context.TRIGGER_INDICATORS) == _library_triggers()

    def test_every_indicator_named_is_a_methodology_record(self):
        for trigger, indicators in context.TRIGGER_INDICATORS.items():
            assert set(indicators) <= set(REGISTRY), trigger

    def test_every_methodology_record_has_its_sources(self):
        keys = {spec.key for spec in SOURCE_REGISTRY}
        assert set(context.INDICATOR_SOURCES) == set(REGISTRY)
        for indicator, sources in context.INDICATOR_SOURCES.items():
            assert sources and set(sources) <= keys, indicator

    def test_the_digest_is_explained_by_every_indicator(self):
        assert set(context.TRIGGER_INDICATORS["daily_digest"]) == set(REGISTRY)

    @pytest.mark.parametrize("trigger, indicator", [
        ("MARGIN_ROLLOVER", "d2"), ("RF4_FIRST", "d1"), ("RF3_CREDIT_STRESS", "s5"),
        ("S3_TIER", "s3"), ("VOL_BACKWARDATION", "v"),
    ])
    def test_an_indicator_alert_is_explained_by_its_indicator(self, trigger, indicator):
        assert context.TRIGGER_INDICATORS[trigger] == (indicator,)


class TestTheMaterial:
    def test_a_reference_carries_the_records_own_words(self):
        (ref,) = context.references_for("MARGIN_ROLLOVER")
        record = REGISTRY["d2"]
        assert ref.name == record.name
        assert ref.what in " ".join(record.what.split())
        assert ref.why in " ".join(record.why.split())
        assert ref.sources == ("FINRA margin-statistics XLSX",)

    def test_no_trigger_without_indicators_has_material(self):
        assert context.references_for("failure_alert_failing") == ()
        assert context.references_for("not_a_trigger") == ()
        assert context.render(()) == ""

    def test_every_text_is_bounded_to_whole_sentences(self):
        for trigger in context.TRIGGER_INDICATORS:
            for ref in context.references_for(trigger):
                for text in (ref.what, ref.why):
                    assert 0 < len(text) <= context.MAX_TEXT, (trigger, ref.indicator)
        # the longest rationale in the registry is cut at a sentence
        (ref,) = [r for r in context.references_for("daily_digest") if r.indicator == "s4"]
        assert len(" ".join(REGISTRY["s4"].why.split())) > context.MAX_TEXT
        assert ref.why.endswith((".", "!", "?"))

    def test_a_single_overlong_sentence_is_cut_at_a_word(self):
        text = "word " * 200
        cut = context._bounded(text, 50)
        assert len(cut) <= 50 and not cut.endswith(" ")

    def test_the_rendered_block_is_only_registry_text(self):
        """Ground rule 1: every line is a label the renderer writes around
        text the registries hold - no other string reaches the prompt."""
        registry_text = " ".join(
            " ".join(part.split())
            for record in REGISTRY.values()
            for part in (record.name, record.what, record.why)
        ) + " ".join(spec.name for spec in SOURCE_REGISTRY)
        for trigger in context.TRIGGER_INDICATORS:
            for ref in context.references_for(trigger):
                for text in (ref.name, ref.what, ref.why, *ref.sources):
                    assert text in registry_text, (trigger, text[:60])

    def test_the_digest_block_names_each_indicator_once(self):
        block = context.render(context.references_for("daily_digest"))
        for record in REGISTRY.values():
            assert block.count(f"- {record.name} (") == 1
        assert block.count("Sources: ") == len(REGISTRY)
