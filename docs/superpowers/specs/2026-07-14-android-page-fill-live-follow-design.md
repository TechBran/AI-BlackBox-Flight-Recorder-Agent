# Android Page-Fill Live Follow Design

## Goal

Make thinking and answer streaming readable at production quality: text fills the available page without movement, then follows the live edge line-for-line without lag. Place the return-to-live arrow directly above the prompt window and make both manual and timed return reliably reach the current live edge.

## Scope

This design applies to main provider chat plus Claude Code and Gemini CLI-agent chat. It changes viewport-follow behavior and return-arrow placement only. It preserves provider transports, message content, persistence, the permanent bottom Signal residence, the five-second idle period, and reduced-motion support.

## Root Cause

The current coordinator starts a 180 ms `animateScrollBy` correction for each changing live edge. Every new reasoning or answer delta cancels the previous job, joins it, and starts another animation. Token updates commonly arrive faster than 180 ms, so the animation repeatedly restarts and the viewport trails the actual stream.

The return arrow calls the same correction path. It can disappear as soon as follow state resumes even when the viewport has not reached the live edge. Its placement is derived from broad bottom-stack clearance rather than an explicit anchor at the prompt window's top edge, allowing it to appear too high.

## Chosen Behavior

### Page fill

Thinking and answer text initially grow downward without moving the viewport. No corrective scroll occurs while the measured live edge remains above the reading boundary. The reading boundary is the protected horizontal line above the prompt/composer stack.

When the live edge reaches or crosses that boundary, continuous follow begins. From then on, the live edge remains pinned to the boundary and prior text moves upward at the same rate that new rendered lines add height.

### Continuous follow

Rapid deltas are conflated into one newest displacement. A correction already in progress is updated toward the latest target rather than cancelled and restarted for every token. Frame-aligned corrections consume the current rendered displacement, preventing a queue of obsolete animations and eliminating cumulative lag.

The controller distinguishes two modes:

- `FILLING`: live text grows naturally; corrections that would pull text downward are suppressed.
- `FOLLOWING`: after the edge first crosses the boundary, positive overflow is consumed continuously to keep the edge pinned.

Thinking-to-answer handoff retains the current mode and boundary. It must not reset to page fill or jump to a different focal region.

## Return to Live

Any user-originated scroll immediately enters suspended mode, cancels continuous correction, shows the existing down arrow, and starts the five-second idle timer. Further user interaction resets the timer.

The arrow is right-aligned in a dedicated row north of the prompt window, not in the center of the message viewport. Its complete 48 dp touch target sits above the measured prompt top with an 8 dp clear gap, so it never overlaps the prompt or its controls. Its placement updates with prompt growth, attachments, keyboard visibility, system insets, rotation, and window-size changes.

The arrow is hosted by the activity as the highest visual layer above navigation content, task panels, and the composer. The active main or agent chat publishes visibility, click behavior, and return state to this host; it does not render a competing screen-local arrow. The host remains mounted independently of Signal or stream activity, allowing older completed conversations to show the shortcut immediately when their list can scroll forward.

Tapping the arrow starts one quick smooth glide to the newest live edge. Five seconds of uninterrupted idle starts the same glide automatically. The arrow remains visible and follow remains in a returning state until the measured live edge has reached the boundary within a small pixel tolerance. Only then does the arrow disappear and continuous line-follow resume.

If the stream completes while suspended, return targets the actual bottom/live edge of the completed response. Completion does not strand the user at an older viewport position.

Reduced-motion mode replaces the glide with an immediate correction while preserving the same arrival verification and state transition.

### Completed-history bottom shortcut

After streaming completes, the arrow becomes a general scroll-to-bottom shortcut. It is visible whenever the message list can still scroll forward toward newer content, independent of active-stream state. The completed response keeps its nonvisual measurable bottom anchor while this shortcut or its return glide is active.

Completed-history reading never triggers the five-second automatic return. The viewport remains where the user placed it until the arrow is tapped. Tapping uses the same quick smooth, measurement-verified glide; the arrow disappears only after the completed-response bottom reaches its destination. If the list is already at the true bottom, the arrow is absent.

The shortcut retains the same right-aligned, dedicated 48 dp row with an 8 dp prompt gap in main provider chat, Claude Code, and Gemini CLI.

## Architecture

### Follow policy

Extend the pure follow policy with explicit `FILLING`, `FOLLOWING`, `SUSPENDED`, and `RETURNING` states. The policy decides whether a measured displacement should be ignored, followed continuously, or used as a return destination. It also owns arrow visibility and the five-second transition from suspended to returning.

### Frame-conflated coordinator

The Compose coordinator receives live-edge measurements and the fixed boundary. It stores only the latest displacement and runs at most one correction loop. While following, each frame consumes the currently measured positive overflow with a direct `scrollBy`, then waits for the next layout measurement. It does not create a fresh 180 ms animation per delta.

Returning is separate from ordinary following. It performs one bounded smooth glide toward the latest destination, re-evaluates after layout, and continues only if measurable distance remains. Arrival is measurement-based rather than assumed from animation completion.

### Prompt-edge arrow anchor

The bottom focal geometry exposes the measured prompt/composer top as the arrow anchor. Main chat and CLI-agent chat use the same placement contract. The Signal residence and prompt stack remain unchanged.

## Edge Cases

- Content shorter than the available page never scrolls automatically.
- Bursty deltas update the latest displacement without queuing work.
- A live edge that temporarily becomes unmeasurable waits for the next valid layout instead of scrolling to a fallback coordinate.
- User input during a return glide cancels it immediately and restarts the five-second idle period.
- Stream completion during return preserves the newest measurable destination.
- Cancellation, disconnect, and failure stop live following but retain a working arrow when the viewport is away from the response bottom.
- Reduced motion never depends on animation completion callbacks.

## Testing

### Unit tests

- `FILLING` suppresses corrections before boundary crossing.
- The first positive overflow transitions to `FOLLOWING`.
- Rapid updates conflate to the newest displacement.
- Thinking-to-answer handoff does not reset follow mode.
- Manual input transitions immediately to `SUSPENDED`.
- Repeated input resets the five-second deadline.
- Arrow tap and idle expiry enter `RETURNING` without hiding the arrow.
- Only measured arrival transitions from `RETURNING` to `FOLLOWING` and hides the arrow.
- Completion while suspended or returning retains a valid return destination.
- Completed-history scrolling shows a tap-only shortcut whenever the list is away from its true bottom.
- Completed-history reading does not auto-return after five seconds.

### Compose tests

- Short thinking and answers fill without viewport movement.
- Long thinking and answers begin moving only after crossing the boundary.
- High-frequency delta bursts do not accumulate live-edge lag.
- The live edge remains pinned after page fill.
- Thinking-to-answer transition has no focal jump.
- The arrow is directly above the real prompt in normal, expanded, attachment, and keyboard-open layouts.
- Arrow tap and five-second idle return reach the live edge before the arrow disappears.
- User input interrupts a return glide.
- Main chat, Claude Code, and Gemini CLI use equivalent behavior.

### Verification and delivery

Run focused unit tests, the full JVM suite, Android test compilation, debug APK assembly, and independent code review. Run connected tests when the authorized device is available and stable. Deliver through exactly one in-place `adb install -r`; never uninstall or automatically retry.

## Acceptance Criteria

1. Thinking and answer text do not move the viewport until they fill the available reading area.
2. After page fill, the newest rendered line stays at the reading boundary without progressively trailing the stream.
3. Rapid token bursts do not queue or restart per-token animations.
4. Thinking-to-answer handoff remains in the same focal region.
5. Manual scrolling pauses follow immediately and shows the arrow.
6. The arrow's complete 48 dp touch target sits in a dedicated highest-layer row north of the prompt, separated from its measured top by 8 dp, and never intersects the prompt or middle reading area.
7. Arrow tap and five-second idle return use a quick smooth glide and keep the arrow visible until measured arrival.
8. User input can interrupt return at any time.
9. Main provider chat, Claude Code, and Gemini CLI satisfy the same behavior.
10. The permanent bottom Signal residence and its keyboard/inset behavior remain unchanged.
11. Scrolling upward after completion shows the arrow; it remains until tapped or the user manually reaches the true bottom.
12. Completed-history mode never moves automatically after the five-second live-stream idle interval.
