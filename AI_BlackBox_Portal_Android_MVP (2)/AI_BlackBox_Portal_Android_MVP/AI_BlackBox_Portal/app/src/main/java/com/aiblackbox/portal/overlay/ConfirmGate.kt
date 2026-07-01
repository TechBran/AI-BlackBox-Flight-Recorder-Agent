package com.aiblackbox.portal.overlay

/**
 * The AUTONOMY CONFIRM-GATE (Phase 4, Task 4.6) — the core SAFETY control for the
 * on-device phone-control agent.
 *
 * When the on-device Gemma agent drives the owner's OWN phone through the
 * consented [BlackBoxA11yService], a **high-consequence** action — tapping a
 * send / pay / delete / post / install button, or typing into a password/login
 * field — must, in [AutonomyMode.PERMISSION], get the user's explicit OK BEFORE
 * it fires. In [AutonomyMode.YOLO] the user has opted into full autonomy, so
 * those actions run immediately. **Benign actions never gate** in either mode.
 *
 * This file is the PURE, JVM-unit-testable decision core ([isHighConsequence],
 * [shouldConfirm], [describeAction]) plus the [ConfirmUi] seam through which the
 * actuator asks the user. The wiring into the actuator lives in [Actuators]; the
 * production [ConfirmUi] is a SYSTEM overlay ([OverlayConfirmUi]) because when
 * Gemma drives, the user is looking at ANOTHER app — an in-app dialog would not
 * be visible.
 *
 * ## Why "pure core + seam"
 * The whole safety question — *does this action need confirmation?* — is decided
 * by the three pure functions below, with zero Android dependency, so it can be
 * tested exhaustively (every keyword, every benign label, the null-label choice,
 * the no-secret-in-message guarantee). The actuation + overlay are framework and
 * device-verified in Task 4.8.
 *
 * ## Leak discipline (shared with 4.2/4.3)
 * A password field's text MUST NEVER reach a confirm message or a log line. The
 * gate is fed `label = null` for a password target (the actuator never reads a
 * password node's text), and [describeAction] emits a fixed generic string for a
 * password type — there is no path for the secret to enter the description.
 */

/** The device's autonomy posture for phone control. */
enum class AutonomyMode {
    /** Asks before each high-consequence phone action (the SAFE default). */
    PERMISSION,

    /** Full autonomy — high-consequence actions run without per-action prompts. */
    YOLO,
}

/**
 * The seam the gate uses to ask the user to confirm a high-consequence action.
 *
 * [confirm] shows [description] and suspends until the user answers, returning
 * `true` to ALLOW the action or `false` to DENY it. The production implementation
 * ([OverlayConfirmUi]) is a SYSTEM overlay (the user is in another app when Gemma
 * drives); unit tests substitute a fake.
 */
interface ConfirmUi {
    /** Show [description] + Allow/Deny; suspend until answered. true = allow. */
    suspend fun confirm(description: String): Boolean
}

/**
 * A FAIL-SAFE-DENY [ConfirmUi]: [confirm] always returns `false` (DENY), with NO UI.
 *
 * The counterpart to [AutoApproveConfirmUi]. It is the SAFE seam for a surface that must
 * NEVER auto-approve a high-consequence action but has no real user-facing confirm UI yet.
 *
 * ## Why it exists (M1, the boot-survivable remote `/action` path)
 * The remote-control dispatcher ([com.aiblackbox.portal.data.remote.PhoneActionDispatcher],
 * wired in [com.aiblackbox.portal.NotificationListenerFgs]) can fire high-consequence
 * actions (send_email / send_sms / send_intent; send/pay/delete/post/confirm taps) with the
 * app backgrounded or after a reboot — but M1 has NO overlay confirm UI on that path yet.
 * Pairing this with [AutonomyMode.PERMISSION] makes every high-consequence confirmation
 * resolve to DENY, so those actions are REFUSED until M4 wires the real [OverlayConfirmUi]
 * + per-device autonomy. Benign navigation/typing/open_app/scroll never reach [confirm]
 * ([shouldConfirm]/[shouldConfirmIntent] are already false for them), so they still work.
 *
 * TODO(M4): the remote path replaces this with the real [OverlayConfirmUi] + an
 * AutonomyStore-backed per-device mode reader.
 */
internal object FailSafeDenyConfirmUi : ConfirmUi {
    override suspend fun confirm(description: String): Boolean = false
}

/**
 * Keywords that, when they appear in a TAP target's label, mark the tap as
 * high-consequence: it commits, sends, spends, destroys, installs, or grants.
 *
 * Matched case-insensitively as substrings of the (lowercased, trimmed) label so
 * a fuller button caption ("Pay $42.00", "Confirm purchase", "Delete account")
 * still trips the gate. Kept deliberately conservative toward SAFETY: a few extra
 * confirmations beat one silent destructive tap — but NOT so broad that benign
 * navigation ("Back", "Settings", a contact name) gates (over-gating trains the
 * user to rubber-stamp). "OK" is intentionally absent: a bare acknowledgement
 * does not commit; genuinely committing buttons are labeled Confirm/Submit/Pay.
 */
private val HIGH_CONSEQUENCE_TAP_KEYWORDS: List<String> = listOf(
    "send", "post", "pay", "buy", "purchase", "order", "delete", "remove",
    "uninstall", "install", "confirm", "submit", "transfer", "allow", "grant",
    "accept", "agree", "sign in", "signin", "log in", "login", "checkout",
    "place order",
)

/**
 * PURE: is this action high-consequence (needs confirmation in Permission mode)?
 *
 * Rules:
 *  - `type` into a password/login target ([isPasswordTarget]) → **true**. (Note:
 *    as of 4.7 the actuator diverts a password `type` to the CREDENTIAL HANDOFF
 *    (the user enters the secret; the model's text is discarded) BEFORE this gate
 *    is consulted, so in practice this branch guards any FUTURE non-password
 *    sensitive type; it is included so the decision is complete on its own.)
 *  - `tap` whose [targetLabel] (lowercased) CONTAINS any
 *    [HIGH_CONSEQUENCE_TAP_KEYWORDS] entry → **true**.
 *  - everything else (`read_screen`/`back`/`home`/`swipe`/`scroll`/`open_app`,
 *    a `tap` on a benign label, a `type` into a non-password field) → **false**.
 *  - a null/blank tap label → **false**. DELIBERATE "don't over-gate" choice: we
 *    cannot judge a plain unlabeled tap, so we treat it as benign rather than
 *    gate every unlabeled control. Flagged for the 4.8 security review.
 */
fun isHighConsequence(action: String, targetLabel: String?, isPasswordTarget: Boolean): Boolean {
    val act = action.trim().lowercase()
    if (act == "type") {
        // Typing a secret/credential is high-consequence; a plain text field is not.
        return isPasswordTarget
    }
    if (act == "tap") {
        val label = targetLabel?.trim()?.lowercase()
        if (label.isNullOrBlank()) return false // can't judge → don't over-gate
        return HIGH_CONSEQUENCE_TAP_KEYWORDS.any { keyword -> label.contains(keyword) }
    }
    // read_screen / back / home / swipe / scroll / open_app — never gate.
    return false
}

/**
 * PURE: should the actuator ask the user before firing this action?
 *
 * Only when the device is in [AutonomyMode.PERMISSION] AND the action is
 * high-consequence. In [AutonomyMode.YOLO] nothing gates; a benign action never
 * gates regardless of mode.
 */
fun shouldConfirm(mode: AutonomyMode, isHighConsequence: Boolean): Boolean =
    mode == AutonomyMode.PERMISSION && isHighConsequence

/**
 * PURE: the human-readable confirm message shown to the user.
 *
 * - `tap` + a label → `Tap "<label>"`; a null/blank label → `Tap this control`.
 * - `type` + a (field) label → `Type into "<label>"`.
 * - `type` with a null/blank label → `Type into password field` (the gate passes
 *   `label = null` for a password target, so this is the password case). The
 *   typed TEXT is NEVER an input here and can never appear in the message.
 * - any other action → a plain `<Action> this control` fallback.
 *
 * SECURITY: this function only ever receives an *action* and a *target label*
 * (never the typed text). For a password target the label is null by
 * construction, so the secret cannot leak into the description.
 */
fun describeAction(action: String, targetLabel: String?): String {
    val label = targetLabel?.trim()?.takeIf { it.isNotBlank() }
    return when (action.trim().lowercase()) {
        "tap" -> if (label != null) "Tap \"$label\"" else "Tap this control"
        // No label on a type == the password case (we never read a password
        // node's text) → describe generically, NEVER echo any text.
        "type" -> if (label != null) "Type into \"$label\"" else "Type into password field"
        else -> {
            val verb = action.trim().replaceFirstChar { it.uppercase() }.ifBlank { "Act on" }
            if (label != null) "$verb \"$label\"" else "$verb this control"
        }
    }
}

// =============================================================================
// CREDENTIAL HANDOFF (Phase 4, Task 4.7)
// =============================================================================

/**
 * What the actuator should do when the model tries to fill a `type` target.
 *
 * The whole point of Task 4.7: a password field gets a *graceful handoff* rather
 * than the bare 4.3 refusal. The model's attempted text is DISCARDED and the USER
 * is asked to type the secret themselves (or, once wired, the system autofills it
 * from a saved credential). The password therefore reaches the model in NEITHER
 * direction — read_screen redacts it (4.2) and the model's attempted text is never
 * typed here (4.7).
 */
enum class CredentialAction {
    /** Not a password target → the model's text is typed normally (the 4.3 path). */
    TYPE_NORMAL,

    /**
     * Password target WITH a saved credential → the system fills it (Credential
     * Manager / Autofill). DEFERRED for v1 — see [credentialDecision]; the
     * actuator currently treats this like [USER_HANDOFF] so logins still work.
     */
    SYSTEM_AUTOFILL,

    /**
     * Password target with NO saved credential → hand off to the USER: discard the
     * model's text, prompt the user to type the secret themselves, then resume.
     */
    USER_HANDOFF,
}

/**
 * PURE: decide how to fill a `type` target.
 *
 * - not a password target → [CredentialAction.TYPE_NORMAL] (unchanged 4.3 path).
 * - password target + a saved credential → [CredentialAction.SYSTEM_AUTOFILL].
 * - password target + no saved credential → [CredentialAction.USER_HANDOFF].
 *
 * NOTE (v1): the call-site passes `hasSavedCredential = false` always — Credential
 * Manager autofill is DEFERRED, so every password resolves to [USER_HANDOFF]. The
 * [SYSTEM_AUTOFILL] branch + the parameter are kept for that follow-up and are
 * unit-tested so the decision is correct the day autofill lands.
 *
 * This function NEVER sees the model's attempted text — it cannot, by signature.
 */
fun credentialDecision(isPasswordTarget: Boolean, hasSavedCredential: Boolean): CredentialAction =
    when {
        !isPasswordTarget -> CredentialAction.TYPE_NORMAL
        hasSavedCredential -> CredentialAction.SYSTEM_AUTOFILL
        else -> CredentialAction.USER_HANDOFF
    }

/**
 * The seam through which the actuator hands a password entry back to the USER.
 *
 * When the model tries to fill a password field, the actuator DISCARDS the model's
 * attempted text and calls [requestUserEntry], which shows the user a SYSTEM
 * overlay ("Please enter your password in the field, then tap Done") and SUSPENDS
 * until the user finishes (`true`) or cancels (`false`). The user types the secret
 * directly into the target app's own field — it never passes through the model in
 * either direction.
 *
 * SECURITY — [fieldDescription] MUST be GENERIC ("the password field"): it must
 * NEVER carry the model's attempted text or any field content. The production
 * implementation ([OverlayCredentialHandoff]) is a SYSTEM overlay because the user
 * is in another app when the agent drives; tests substitute a fake. It fails SAFE
 * (returns `false`) on any error or when un-wired.
 */
interface CredentialHandoff {
    /**
     * Prompt the user to enter their credential directly into the focused field.
     * [fieldDescription] is a GENERIC label (e.g. "the password field") — never the
     * model's text. Suspends until the user taps Done (`true`) or Cancel (`false`).
     */
    suspend fun requestUserEntry(fieldDescription: String): Boolean
}

/**
 * The default [CredentialHandoff] for un-wired [Actuators] (existing call-sites /
 * tests): auto-DECLINES. This is the SAFE inert default — an un-wired actuator can
 * never silently let a password entry proceed; the production wiring (ChatViewModel)
 * supplies the real [OverlayCredentialHandoff]. It also never receives, and so can
 * never leak, the model's attempted text (the actuator discards it before calling).
 */
internal object AutoDeclineCredentialHandoff : CredentialHandoff {
    override suspend fun requestUserEntry(fieldDescription: String): Boolean = false
}

/** A GENERIC, content-free description for the credential-handoff prompt (never the model's text). */
const val CREDENTIAL_FIELD_DESCRIPTION: String = "the password field"

// =============================================================================
// INTENT GATE (intent-based phone actions — Task IA-1)
// =============================================================================

/**
 * The named intent actions that are HIGH-CONSEQUENCE: in [AutonomyMode.PERMISSION]
 * the actuator must get the user's explicit OK BEFORE firing them.
 *
 * Three intents gate, for two distinct reasons:
 *  - `send_email` / `send_sms` FIRE A PREFILLED OUTBOUND MESSAGE to a RECIPIENT.
 *  - `send_intent` is the GUARDED GENERIC escape-hatch (decision 9): it can fire an
 *    arbitrary (pre-validated, non-dangerous) OS intent, so it is treated as
 *    high-consequence BY DEFAULT — in Permission mode the user OKs it before it
 *    fires. (Its argument safety-envelope — the dangerous-action denylist + unsafe-
 *    URI-scheme reject — lives in [sendIntentRejectionReason]; this gate is the
 *    second layer.)
 *
 * Every OTHER intent action is either:
 *  - **benign** (flashlight, open a settings panel, set a timer, show a map, take a
 *    photo, pick a file/contact) — nothing leaves the device on the user's behalf; or
 *  - **finalized by the user inside the launched UI** (`dial` pre-fills the dialer
 *    but the user still taps Call; `create_calendar_event` opens the editor; an
 *    `open_url` just opens the browser) — so a separate confirm here would be
 *    redundant over-gating, which trains the user to rubber-stamp.
 *
 * Kept deliberately conservative and extensible: any future fire-and-forget outbound
 * intent (e.g. a one-shot "post" intent) should be added HERE so it inherits the
 * Permission-mode confirm. Compared case-insensitively against the trimmed name.
 */
private val HIGH_CONSEQUENCE_INTENTS: Set<String> = setOf("send_email", "send_sms", "send_intent")

/**
 * PURE: is the intent [name] high-consequence (needs confirmation in Permission
 * mode)? True only for [HIGH_CONSEQUENCE_INTENTS], matched on the trimmed,
 * lowercased name.
 */
fun isHighConsequenceIntent(name: String): Boolean =
    name.trim().lowercase() in HIGH_CONSEQUENCE_INTENTS

/**
 * PURE: should the actuator ask the user before firing intent [name]?
 *
 * Only when the device is in [AutonomyMode.PERMISSION] AND the intent is
 * high-consequence. In [AutonomyMode.YOLO] nothing gates; a benign intent never
 * gates regardless of mode. Mirrors [shouldConfirm] for the intent surface.
 */
fun shouldConfirmIntent(mode: AutonomyMode, name: String): Boolean =
    mode == AutonomyMode.PERMISSION && isHighConsequenceIntent(name)

/**
 * PURE: the human-readable confirm message for an intent action.
 *
 * - `send_email` → [primaryArg] is the RECIPIENT address:
 *   `Send an email to "<to>"`, or `Send an email` if [primaryArg] is null/blank.
 * - `send_sms` → [primaryArg] is the phone NUMBER:
 *   `Send a text to "<number>"`, or `Send a text message` if null/blank.
 * - `send_intent` → [primaryArg] is the (non-sensitive) intent ACTION string:
 *   `Run the app action "<action>"`, or `Run a custom app action` if null/blank.
 * - any other [name] → a generic `Run <name>`.
 *
 * SECURITY: [primaryArg] is ONLY ever the recipient / number / action constant —
 * never a message BODY or field content. Bodies/extras are supplied separately to
 * the actuator (IA-2) and never reach this function, so they can never appear in a
 * confirm prompt. The entire output is a fixed function of (`name`, `primaryArg`);
 * there is no path for any other text to leak in.
 */
fun describeIntent(name: String, primaryArg: String?): String {
    val arg = primaryArg?.trim()?.takeIf { it.isNotBlank() }
    return when (name.trim().lowercase()) {
        "send_email" -> if (arg != null) "Send an email to \"$arg\"" else "Send an email"
        "send_sms" -> if (arg != null) "Send a text to \"$arg\"" else "Send a text message"
        "send_intent" -> if (arg != null) "Run the app action \"$arg\"" else "Run a custom app action"
        else -> "Run $name"
    }
}
