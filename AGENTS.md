# Contributor instructions

- Target Windows 11 and Python 3.11 or 3.12.
- Keep deterministic parsers and allow-listed actions ahead of LLM fallback.
- Never add arbitrary shell execution to the voice action schema.
- Never commit audio, transcripts, local paths, model weights, tokens, logs, or `config.local.yaml`.
- Before keyboard or mouse input, bind the action to one exact visible, non-sensitive window and
  target.
- After input, poll for task progress or the final task goal. If one micro-step cannot yet be
  proved, use bounded waiting, retry, an alternate supported action, or one replan instead of
  automatically failing the entire task.
- Final success in `assistive_v1` is decided only by the task-level `GoalVerifier`.
- Navigation and unsent draft entry may run automatically. Sending, submitting, deleting,
  overwriting, installing, uploading, sharing, or discarding unsaved work requires an
  action-bound spoken confirmation.
- Passwords, authentication, payments, UAC, terminals, and Windows security/privacy surfaces
  remain blocked.
- Add or update tests for every new command phrase and safety rule.
- Live tests must be opt-in and clearly marked with `@pytest.mark.live`.
