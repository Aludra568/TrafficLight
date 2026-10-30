import uuid

import pytest

from svetofor.core.config import LLMMode, Settings
from svetofor.core.ids import uuid7, uuid7_timestamp_ms


class TestUuid7:
    def test_version_and_variant(self) -> None:
        value = uuid7()
        assert value.version == 7
        assert value.variant == uuid.RFC_4122

    def test_timestamp_roundtrip(self) -> None:
        assert uuid7_timestamp_ms(uuid7(1_790_000_000_000)) == 1_790_000_000_000

    def test_sorted_by_time(self) -> None:
        values = [uuid7(ts) for ts in (1_000, 2_000, 3_000)]
        assert values == sorted(values)

    def test_unique(self) -> None:
        assert len({uuid7(5_000) for _ in range(1000)}) == 1000

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            uuid7(1 << 48)


class TestSettings:
    def test_without_provider_works_in_replay(self) -> None:
        s = Settings(_env_file=None, llm_mode=LLMMode.LIVE, llm_base_url="", llm_model_main="")
        assert not s.llm_configured
        assert s.effective_llm_mode is LLMMode.REPLAY

    def test_local_provider_without_key_is_configured(self) -> None:
        s = Settings(_env_file=None, llm_base_url="http://ollama:11434/v1", llm_model_main="qwen")
        assert s.llm_configured
        assert s.effective_llm_mode is LLMMode.LIVE

    def test_fast_model_falls_back_to_main(self) -> None:
        s = Settings(_env_file=None, llm_model_main="main", llm_model_fast="")
        assert s.llm_model_fast_or_main == "main"

    def test_upload_limit_in_bytes(self) -> None:
        assert Settings(_env_file=None, max_upload_mb=100).max_upload_bytes == 100 * 1024 * 1024

    def test_quote_score_bounds(self) -> None:
        with pytest.raises(ValueError):
            Settings(_env_file=None, quote_min_score=10)
