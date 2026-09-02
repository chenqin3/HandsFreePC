# Contributor instructions

- Target Windows 11 and Python 3.11 or 3.12.
- The design is deliberately small: a local, offline speech front-end (wake phrase, `over`
  delimiter, FIFO queue, feedback, stop) and one executor, Kimi Code CLI. Do not add a second
  execution path, a UI Automation driver, a planner, or a rule engine in front of Kimi.
- Everything Kimi needs to know goes into the preamble in `handsfree_pc/kimi_agent.py` or the
  user's `gui-control` skill, not into Python code.
- Control phrases (wake, end, stop, resume, feedback mode) must be recognised locally and must
  never be forwarded to Kimi. Every phrase must also appear in `speech.wake.grammar`.
- On-screen notices are short and self-hiding; only "in progress" notices may use `duration=0`,
  and they must be replaced by the outcome.
- Never commit audio, transcripts, local paths, model weights, tokens, logs, or `config.local.yaml`.
  Test fixtures and docs use neutral example names, never names from a contributor's machine.
- Add or update tests for every new phrase and every runtime state change; live tests are opt-in
  and marked `@pytest.mark.live`.
