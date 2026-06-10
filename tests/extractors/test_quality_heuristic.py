"""W-2 Stage A — quality_heuristic unit tests.

Pure-function tests of the per-page text-extraction quality scorer.
"""

from platform_service.workers.extractors.quality_heuristic import score_page


class TestTextEmpty:
    def test_empty_string_fails(self) -> None:
        s = score_page("", primary_language="en")
        assert not s.passed
        assert "text_empty" in s.fail_reasons

    def test_whitespace_only_fails(self) -> None:
        s = score_page("    \n\n    \t", primary_language="en")
        assert not s.passed
        assert "text_empty" in s.fail_reasons

    def test_short_text_below_threshold_fails(self) -> None:
        s = score_page("hi", primary_language="en")
        assert not s.passed
        assert "text_empty" in s.fail_reasons

    def test_long_clean_english_passes(self) -> None:
        text = "This is a long enough English page with substantial content. " * 5
        s = score_page(text, primary_language="en")
        assert s.passed
        assert s.fail_reasons == ()


class TestBanglaHeuristic:
    def test_clean_unicode_bangla_passes(self) -> None:
        # Pure Bangla text, well above codepoint threshold.
        text = "এটি একটি বাংলা পরীক্ষা পৃষ্ঠা যা যথেষ্ট অক্ষর সংখ্যা রয়েছে।" * 3
        s = score_page(text, primary_language="bn")
        assert s.passed
        assert s.native_script_ratio > 0.4

    def test_mixed_bangla_english_passes(self) -> None:
        # Common case: Bangla narrative with English drug names + numbers.
        text = (
            "রোগীর BP মান 140/90 mmHg ছিল এবং Hb মান 8 g/dL এর নিচে। "
            "চিকিৎসক রোগীকে UHC তে পাঠিয়েছেন। অতিরিক্ত বিশ্রাম নেওয়ার পরামর্শ দেওয়া হয়েছে।"
        )
        s = score_page(text, primary_language="bn")
        assert s.passed
        assert s.native_script_ratio >= 0.40

    def test_legacy_bijoy_bytes_fail(self) -> None:
        # Simulates the BRAC SK Basic Training legacy-encoded extraction:
        # high non-ASCII byte rate but very few actual Bangla codepoints
        # (the bytes are mangled Latin-1 representations of Bangla glyphs).
        # Construct text where most characters are non-ASCII Latin-1 chars
        # in the 0xC0-0xFF range — explicitly NOT Bangla codepoints.
        garbled = "".join(chr(c) for c in range(0xC0, 0xFF)) * 5
        s = score_page(garbled, primary_language="bn")
        assert not s.passed
        assert "native_encoding_corrupt" in s.fail_reasons

    def test_english_text_in_bn_doc_does_not_trigger_bangla_fail(self) -> None:
        # A page that's entirely English in a primarily-Bangla document
        # should NOT fail on Bangla heuristic because non_ascii_byte_ratio
        # is low (English is ASCII).
        english = "This is a long enough English passage with no Bangla codepoints. " * 5
        s = score_page(english, primary_language="bn")
        # bangla codepoint ratio is 0, but non_ascii_byte_ratio is also 0,
        # so the AND in the heuristic doesn't fire.
        assert "native_encoding_corrupt" not in s.fail_reasons


class TestEnglishOnlyDoc:
    def test_english_doc_skips_bangla_check(self) -> None:
        # Even with non-ASCII bytes (e.g. accented chars), an en-document
        # never trips the encoding-integrity heuristic (no native script
        # registered for 'en').
        text = "Café résumé naïve façade " * 20
        s = score_page(text, primary_language="en")
        assert s.passed
        assert "native_encoding_corrupt" not in s.fail_reasons


class TestHindiHeuristic:
    """Generalised mojibake check (post-Induction-Hindi run). The Bijoy/ANSI
    encoding produces high non-ASCII byte rate with zero Devanagari
    codepoints — exactly the signature the bn check caught on the SK
    manual. Without language-agnostic detection, the Hindi doc routed
    through text path and Stage 2 silently dropped TB and FP modules
    because their canonical names appeared only as mojibake'd ASCII.
    """

    def test_clean_unicode_devanagari_passes(self) -> None:
        text = "यह एक हिंदी परीक्षण पृष्ठ है जिसमें पर्याप्त सामग्री है।" * 3
        s = score_page(text, primary_language="hi")
        assert s.passed
        assert s.native_script_ratio > 0.4

    def test_legacy_bijoy_devanagari_bytes_fail(self) -> None:
        # Same construction as the Bangla Bijoy test: high non-ASCII byte
        # rate, zero Devanagari codepoints. Latin-1 0xC0..0xFF bytes are
        # what the Hindi-Bijoy ASHA Induction PDF surfaced as.
        garbled = "".join(chr(c) for c in range(0xC0, 0xFF)) * 5
        s = score_page(garbled, primary_language="hi")
        assert not s.passed
        assert "native_encoding_corrupt" in s.fail_reasons

    def test_marathi_uses_devanagari_too(self) -> None:
        # Marathi shares Devanagari with Hindi — the codepoint range is
        # the same, so the heuristic should treat it identically.
        text = "हे एक मराठी चाचणी पृष्ठ आहे ज्यामध्ये पुरेसा मजकूर आहे।" * 3
        s = score_page(text, primary_language="mr")
        assert s.passed
        assert s.native_script_ratio > 0.4

    def test_unknown_language_skips_encoding_check(self) -> None:
        # A primary_language code we don't recognise (e.g. 'kn' for Kannada
        # before we add it) should NOT trip the encoding-corrupt check —
        # we'd rather pass-through than fail-closed when we can't score.
        garbled = "".join(chr(c) for c in range(0xC0, 0xFF)) * 5
        s = score_page(garbled, primary_language="kn")
        assert "native_encoding_corrupt" not in s.fail_reasons


class TestHeadingDetection:
    def test_heading_count_recorded(self) -> None:
        text = "# Title\n\nSome body content.\n\n## Sub heading\n\nMore body."
        s = score_page(text, primary_language="en")
        assert s.heading_count >= 2

    def test_no_headings_doesnt_fail_extraction(self) -> None:
        # Heading absence is a soft signal at the page level — Stage B
        # outline parser handles document-level heading absence.
        text = "Just a flowing paragraph with no markdown headings at all. " * 5
        s = score_page(text, primary_language="en")
        # Should still pass on text-content alone.
        assert s.passed


class TestCompositeScore:
    def test_composite_score_in_range(self) -> None:
        s = score_page("Some text. " * 10, primary_language="en")
        assert 0.0 <= s.composite_score <= 1.0

    def test_passing_page_has_higher_score_than_failing(self) -> None:
        passing = score_page("# Heading\n\n" + ("Body text. " * 10), primary_language="en")
        failing = score_page("", primary_language="en")
        assert passing.composite_score > failing.composite_score
