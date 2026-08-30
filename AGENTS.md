# Contributor instructions

- Target Windows 11 and Python 3.11 or 3.12.
- Keep deterministic parsers and allow-listed actions ahead of LLM fallback.
- Never add arbitrary shell execution to the voice action schema.
- Never commit audio, transcripts, local paths, model weights, tokens, logs, or `config.local.yaml`.
- Every UI action must verify its target window before input and verify an observable result after input.
- Add or update tests for every new command phrase and safety rule.
- Live tests must be opt-in and clearly marked with `@pytest.mark.live`.
