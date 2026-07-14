# Android Live-Stream Focal Follow Design

## Approved Bottom-Residence Revision

This revision supersedes references below to a rail near vertical center. The one-line Signal/status stream instead occupies a permanent app-owned residence at the very bottom of the usable screen, directly above Android system navigation. Its reserved height remains present when the label is absent, so the composer never jumps; black space and particles may remain visible there, but message words and controls may not render behind it.

The prompt window, provider/model selector, and Auto-TTS controls move above that residence. Their bounds and the system navigation/keyboard insets are measured dynamically. While following, the newest thinking or answer edge is held above the composer stack by slightly more than the prompt-window height, keeping live prose, controls, and Signal simultaneously readable. Main provider chat and Claude Code/Gemini CLI-agent chat use the same geometry contract. Existing immediate manual-scroll suspension, down-arrow behavior, five-second idle resume, status lifecycle, and reduced-motion behavior remain unchanged.

## Goal

Keep the live edge of model thinking or answer text near the center of the Android screen, with the one-line Signal/status stream fixed directly beneath it. Apply the behavior consistently to the main provider chat and the Claude Code/Gemini CLI-agent chat while preserving normal message history and user-controlled scrolling.

## Scope

This design covers:

- Main Android chat providers, including Gemini, OpenAI, Anthropic, Grok, local models, and other providers routed through `ChatScreen`.
- Claude Code and Gemini CLI-agent conversations routed through `AgentChatScreen`.
- Thinking/reasoning deltas, answer deltas, tool activity, and the existing one-line Signal/status lifecycle.
- Automatic focal scrolling, manual-scroll suspension, the existing down-arrow control, and reduced-motion behavior.

It does not change provider transports, message persistence, reasoning storage, response rendering, or model output.

## Current Behavior and Gap

`ChatScreen` follows changes to the newest message's answer content but does not observe changes to its reasoning text. Thinking can therefore grow without moving the viewport. `AgentChatScreen` likewise observes message count and answer length but not reasoning length.

The current main-chat behavior bottom-pins the last message. That keeps the newest answer visible, but it does not create the requested focal composition: current thinking or answer text above a continuously visible, one-line Signal/status stream near the center of the screen.

## Chosen Approach

Use a shared live focal-follow controller in both chat screens. The controller tracks a measured live-edge anchor inside the active message and positions it just above a fixed Signal/status rail near the viewport's vertical center.

The full reasoning and answer remain in the ordinary message bubble. Live text is not duplicated in an overlay. Older text flows upward naturally as the live edge grows.

## Architecture

### Live stream snapshot

Each chat screen supplies the shared controller with:

- active message ID;
- reasoning length;
- answer length;
- active phase: thinking, answering, tool activity, or idle;
- current Signal/status label; and
- terminal state when completed, cancelled, disconnected, or failed.

Provider view models remain responsible for consuming events and updating messages and status. The controller owns viewport behavior only.

### Live-edge anchor

The active message exposes a measurable anchor at the end of the section currently growing:

- the end of reasoning during thinking;
- the end of answer content during response streaming; and
- the latest applicable message edge during tool/status activity when no prose delta is arriving.

When thinking transitions to answering, ownership moves from the reasoning anchor to the answer anchor without changing the focal target.

### Fixed focal rail

The one-line Signal/status stream is a viewport-fixed rail slightly below vertical center. The active text edge is targeted immediately above it. The rail stays stable while the message list moves beneath it.

Main chat retains its existing Signal labels and post-answer mint flourish. CLI-agent chat maps its existing thinking, tool, and session status into the same single-line presentation instead of showing a second competing live-status location.

## Follow Behavior

Reasoning length and answer length are both follow triggers. On a trigger, the controller measures the live edge, calculates its displacement from the focal target, and issues a short corrective scroll.

Token updates may arrive faster than the display should animate. The controller coalesces small, rapid changes and maintains at most one corrective motion. A newer target replaces an obsolete target instead of queuing another animation. This avoids token-by-token jitter.

Programmatic focal scrolling never counts as user input and cannot suspend itself.

## User Control

Any user-originated drag, fling, wheel, or accessibility scroll immediately:

- cancels active corrective motion;
- suspends automatic follow;
- shows the existing down-arrow control; and
- starts a five-second idle timer.

Further user interaction resets the timer. New model and status deltas continue accumulating while follow is suspended, but the app does not move the list.

After five uninterrupted seconds, the controller smoothly returns to the current live edge and resumes follow. Tapping the down arrow resumes immediately and cancels the remaining timer.

The fixed Signal/status rail remains visible and continues updating while list follow is suspended.

## Stream Lifecycle

- **Start:** Select the newest active assistant/agent turn and begin focal follow on the first thinking, answer, or applicable status event.
- **Thinking:** Track the reasoning edge above the status rail.
- **Thinking to answer:** Switch to answer content while retaining the same target position.
- **Tool activity:** Keep the single-line status rail live and follow the latest applicable message edge without creating another status banner.
- **Completion:** Stop automatic tracking and keep the viewport at its last followed position. Allow the existing completion/mint flourish to finish, then remove the rail according to its current lifecycle.
- **Cancellation, error, or disconnect:** Stop automatic tracking cleanly and leave ordinary manual scrolling available.

## Accessibility and Motion

The down arrow remains an accessible explicit action for returning to live output. User-originated accessibility scrolling receives the same five-second suspension as touch scrolling.

When Android reduced motion is enabled, focal corrections use an immediate or very short adjustment instead of a normal smooth animation. Status text remains readable without relying on motion.

## Testing

### Unit tests

Test the shared follow policy independently from Compose rendering:

- reasoning deltas request follow;
- answer deltas request follow;
- manual input suspends immediately;
- repeated input resets the idle timer;
- automatic follow resumes after five seconds;
- the down arrow resumes immediately;
- programmatic scrolling does not suspend follow; and
- completion, cancellation, errors, and disconnects stop tracking.

### Compose UI tests

Cover both `ChatScreen` and `AgentChatScreen`:

- the live reasoning edge remains above the center status rail;
- answer-only streaming follows correctly;
- thinking-to-answer transition does not jump to another focal region;
- responses taller than the viewport keep their newest edge visible;
- manual scrolling is not overridden during suspension;
- the down arrow appears during suspension;
- the status rail remains visible and updates during suspension;
- five seconds of inactivity returns to the current live edge; and
- tapping the down arrow returns immediately.

### Regression and device acceptance

Preserve the existing one-line Signal lifecycle and provider event behavior. Validate on an Android device with:

- a provider that emits explicit thinking deltas;
- an answer-only provider;
- Claude Code or Gemini CLI agent;
- a response longer than one screen;
- manual scroll followed by five-second automatic return;
- immediate return through the down arrow; and
- Android reduced motion enabled.

## Acceptance Criteria

1. The newest thinking or answer edge stays immediately above a fixed, one-line Signal/status rail near vertical center while follow is active.
2. Reasoning growth triggers the same follow behavior as answer growth for every supported main-chat provider and both CLI-agent providers.
3. Manual scrolling stops automatic movement immediately and reveals the down arrow.
4. Continued interaction keeps follow suspended; five uninterrupted seconds resumes it.
5. The down arrow resumes follow immediately.
6. The Signal/status rail remains visible and live during suspension.
7. The app never duplicates or removes reasoning or answer content to implement focal follow.
8. Stream completion, cancellation, errors, and disconnects leave the list stable and manually scrollable.
