# Screenshot-first visual fallback (optional OCR)

HandsFreePC normally uses Windows UI Automation. Some Chromium/Qt windows expose
no useful accessibility controls. For configured apps, the optional visual
fallback captures only the exact selected top-level window and uses the complete
PNG as the primary planner signal. It always provides one frame-bound visual
viewport; PaddleOCR text boxes are a separate, optional enhancement.

This feature is off by default and is used only when a listed app lacks a rich
actionable UIA surface. It supports read-only observation, one rebound left
click on an optional OCR text region, one screenshot-local viewport point click,
one-page vertical viewport scroll, and a narrowly bound rendered-search flow.
A point is bound to the exact HWND, window rectangle, observation and target
patch; it is not a reusable naked coordinate.

Rendered search is not arbitrary visual typing. A viewport click can expose one
`type_text` action only on the next fresh observation and only after local Win32
`GetGUIThreadInfo` evidence binds the exact target process/thread, active/focus/
caret HWNDs, a visible system caret, and the clicked screenshot point. The text
must be one exact contiguous destination/search span copied from the user's
instruction. It cannot be a message body, prompt, credential, payment value, or
text copied from the screen. If the fresh screenshot after typing shows no
result and the same focus/caret binding still holds, one Enter/Return may be
used with a `LAST_ACTION_VERIFIED` expectation. The fresh screenshot transition
and the next planning step establish what the search produced; this visual path
does not claim the UIA-only `SEARCH_SUBMITTED` semantic expectation. It never authorizes Send, Submit,
reply, arbitrary keys, authentication, credentials, or payment surfaces.
Listing `wechat` therefore enables visual search navigation, not arbitrary
WeChat text entry or message sending.

## Optional PaddleOCR regions

You do not need an OCR server for screenshot planning or viewport point clicks.
Only when you want numbered text regions, set `ocr_regions_enabled: true` and
run the loopback service. Use a WSL Python environment that already has a
working PaddleOCR-VL GPU/CPU installation. Add the small HTTP dependencies if
needed:

```bash
python -m pip install fastapi uvicorn pillow numpy
python /mnt/c/path/to/HandsFreePC/scripts/visual_ocr_server.py
```

The default bind is numeric loopback `127.0.0.1:8766`. The protocol is a raw
`POST image/png`, so `python-multipart` is not required. The server retains no
PNG and disables access logs. Verify it from Windows with
`http://127.0.0.1:8766/health`.

## Private configuration

Put the opt-in in ignored `config.local.yaml`, never in the public repository:

```yaml
privacy:
  allow_cloud_planner: true

computer_control:
  enabled: true
  driver: windows_uia
  planner_backend: codex_cli_best_effort
  safety_profile: local_unrestricted
  allow_screen_context_to_cloud: true
  allow_codex_cli_host_read: true

visual_ocr:
  enabled: true
  # Optional. Keep false for screenshot-only visual planning.
  ocr_regions_enabled: false
  endpoint: http://127.0.0.1:8766/layout-parsing
  allow_remote_screen_ocr: false
  apps: [codex, wechat]

execution:
  dry_run: false
```

`visual_ocr.enabled` activates screenshot-first fallback for the listed apps.
With `ocr_regions_enabled: false`, HandsFreePC does not call the endpoint and
the complete screenshot viewport remains available. Set it to `true` only to
add PaddleOCR text regions; an OCR error still leaves the screenshot viewport
available for a new plan. OCR regions are optional click hints, never the
planner's replacement for the window image and never proof that an input has
focus.

Codex is required for these visual steps because its CLI receives an exact-
window image (annotated only when OCR returned text regions). The complete
capture remains the source of truth locally. If its longest edge exceeds 2048
pixels, the adapter preserves aspect ratio and downsizes it to a bounded planner
canvas. Planner `x/y` points are checked against that canvas and mapped back by
the horizontal and vertical source/canvas ratios to original capture pixels.
The driver then rechecks the original-window rectangle and target patch before
input. The Claude CLI backend in this project is text-only and cannot use visual
regions.

With `allow_remote_screen_ocr: false`, only numeric loopback IP addresses are
accepted and HTTP redirects are never followed. A non-loopback endpoint requires
the separate `allow_remote_screen_ocr: true` consent because exact window
screenshots then leave this PC.

## One-action visual loop

Before every visual action, HandsFreePC re-captures the currently bound exact
window and checks its rectangle. An OCR text click re-runs OCR, uniquely rebinds
the same text/label/nearby box, and checks its crop. A viewport point click maps
the planner-canvas point to original pixels and checks the original target patch,
so unrelated animated pixels may change but the target may not.

Unrelated full-window animation has one narrower non-visual exception. A UIA
action may proceed only when the planned and fresh observations retain the same
app and local window, the element index is still unique, and its non-visual
`local_identity`, control type, enabled state, and addressability are identical.
The driver revalidates that element again at dispatch. A visual point never uses
this semantic bridge; its local target patch must still be stable.

The driver executes exactly one atomic visual action: one click, one-page
vertical scroll, one caret-proven search-text insertion, or one separately
proven search Enter/Return. It then captures a fresh full-window frame. The next
step is planned and verified from that new frame; an old image, region, point,
caret witness, or consumed capability is never reused. A changed frame is
transition evidence only--the task-specific verifier and a later fresh visual
review still decide whether the requested state actually exists.

## Locally bound visual completion

The cloud-facing planner observation does not contain the raw HWND or
`local_window_id`. When the planner returns visual `DONE`, the controller first
binds that proposal locally to the private full observation with a token derived
from the app, exact local window, generation, and complete screenshot bytes. The
model cannot author or guess this token.

That first decision is only a candidate. HandsFreePC captures a separately fresh
screenshot of the same exact window, requires a newer generation and capture
time, and asks the planner to review the new frame. Completion can proceed only
after the second visual `DONE` is locally bound to that newer frame for the same
app/window. A window transition, reused generation, or one-frame judgment fails
closed.

## Rendered-search focus evidence

A point click records the exact HWND, local window identity, window rectangle,
and original-capture point. On the following observation, `GetGUIThreadInfo`
must provide one coherent witness with all of these properties:

- the target PID/TID still belongs to the exact bound window;
- active, focus, and caret ownership remain in that window/process identity;
- the system caret is visible and has a non-empty rectangle;
- the caret rectangle maps into the exact window and is close enough to the
  clicked point.

Missing API support, a foreground race, a foreign focus/caret HWND, an invisible
or empty caret, coordinate-conversion failure, or a caret far from the point all
fail closed. OCR output and visual appearance cannot substitute for this focus
witness. The same witness and point patch are checked again immediately before
Unicode text input. The `type_text` capability is single-use and is removed
after the call.

After typing, the fresh screenshot is sent back to the planner. If a result is
already visible, the planner must click it normally. Only when no result is
visible, the click was in the bounded search zone, and the same focus/caret
identity remains valid may the next fresh viewport expose a single Enter/Return.
That key is consumed as a search transition and followed by another fresh
screenshot. It is never treated as a generic submit or message-send key.

This is also deterministic at the parser boundary. If exactly one fresh armed
`VisualViewport` supports `PRESS_KEY`, the parser replaces a click only when it
targets that viewport with one left-click point still inside the same bounded
top search zone. It then uses the viewport's single-use Enter and a
`LAST_ACTION_VERIFIED` expectation. A semantic result `Button`, a visual result
outside the search zone, or any other target remains a click. The rewrite does
not authorize a second key, another key value, Send, Submit, or another click
replay.

## Related rendered windows

Some apps open a separate foreground renderer for search. After an accepted
click or search Enter/Return, HandsFreePC may continue only when the new HWND is
the foreground window and its PID is the same as the old window's PID or the two
processes have a verifiable parent/child relationship. The dynamic app binding
is then replaced with the new exact HWND/PID/process/title before observation.
The WeChat main window to a `WeChatAppEx` search window is the intended example.
An unrelated process, a same-name impostor, ambiguous identity, or an
unexplained foreground change stops the task.

A separate helper executable may inherit an app profile only from its immediate
parent process, and only when that parent name has one unique profile match. Once
a verified transition moves the task-scoped app alias to the related child, an
inventory refresh keeps the alias on that child even if the original parent
window remains visible; the parent receives a different candidate ID.

Some rendered-search helpers expose one semantic UIA result `Button`. Its exact
full label may be used for a `TEXT_ABSENT` bridge only when it is the sole eligible
button, contains the user's exact destination, and ends in `前往` or `Go to`.
Exact full-label disappearance verifies only that navigation left this bridge.
The related-process window transition and requested destination still require a
fresh screenshot; partial labels, multiple matches, and generic disappearance
cannot prove completion.
