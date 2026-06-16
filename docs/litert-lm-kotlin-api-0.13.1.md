# LiteRT-LM Kotlin API — verified surface (v0.13.1)

> Extracted via `javap` from `com.google.ai.edge.litertlm:litertlm-android:0.13.1`
> (Google Maven). This is the **ground-truth** API the concrete `LiteRtEngine`
> (Task 2.6) is written against. Function calling is INCLUDED in this artifact —
> there is no separate "AI Edge FC SDK". Package: `com.google.ai.edge.litertlm`.

## Gradle
```kotlin
implementation("com.google.ai.edge.litertlm:litertlm-android:0.13.1")
```
GPU backend also needs, in `AndroidManifest.xml` `<application>`:
```xml
<uses-native-library android:name="libOpenCL.so" android:required="false"/>
<uses-native-library android:name="libvndksupport.so" android:required="false"/>
```

## Engine lifecycle
```
class EngineConfig(
    modelPath: String,
    backend: Backend = ...,        // primary (text) compute backend
    visionBackend: Backend = ...,
    audioBackend: Backend = ...,
    maxNumTokens: Int? = null,
    maxNumImages: Int? = null,
    cacheDir: String? = null,      // speeds up 2nd load
)
abstract class Backend                      // subclasses below
class Backend.CPU(...)                       // Backend.CPU()
class Backend.GPU(...)                       // Backend.GPU()
class Backend.NPU(...)                       // Backend.NPU(nativeLibraryDir = context.applicationInfo.nativeLibraryDir)

class Engine(config: EngineConfig) : AutoCloseable {
    val isInitialized: Boolean
    fun initialize()                         // ~10s; call off main thread
    fun close()
    fun createConversation(config: ConversationConfig = default): Conversation
    fun createSession(config: SessionConfig): Session
}
// throws: LiteRtLmJniException (native), IllegalStateException (lifecycle)
```

## Conversation
```
class Conversation : AutoCloseable {
    val isAlive: Boolean
    // SYNC:
    fun sendMessage(text: String, extra: Map<String,Any> = emptyMap()): Message
    fun sendMessage(contents: Contents, ...): Message
    fun sendMessage(message: Message, ...): Message
    // STREAMING (use this) — cold Flow, each emission is a Message CHUNK:
    fun sendMessageAsync(text: String, extra: Map<String,Any> = emptyMap()): Flow<Message>
    fun sendMessageAsync(contents: Contents, ...): Flow<Message>
    fun sendMessageAsync(message: Message, ...): Flow<Message>
    // callback variants also exist (MessageCallback)
    fun cancelProcess()
    val tokenCount: Int
    fun close()
}
```
NOTE: each `Message` emitted by the Flow is a chunk; concatenate the text of all
emissions to build the full reply (analogous to SSE token deltas, but each is a
whole-Message chunk not a raw token).

## Message / Content / ToolCall
```
enum class Role { SYSTEM, USER, MODEL, TOOL }   // .value: String

class Message(
    role: Role,
    contents: Contents,
    toolCalls: List<ToolCall> = emptyList(),
    channels: Map<String,String> = emptyMap(),
) {
    val role: Role
    val contents: Contents
    val toolCalls: List<ToolCall>     // <- model's function-call requests
    companion object {
        fun system(text: String): Message ; fun system(c: Contents): Message
        fun user(text: String): Message   ; fun user(c: Contents): Message
        fun model(text: String): Message  ; fun model(c, toolCalls, channels): Message
        fun tool(c: Contents): Message
        fun of(text: String): Message
    }
}

class Contents {
    val contents: List<Content>
    companion object {
        fun of(text: String): Contents
        fun of(vararg c: Content): Contents
        fun of(list: List<Content>): Contents
    }
}

sealed/abstract class Content
class Content.Text(text: String)               { val text: String }     // <- plain text
class Content.ImageBytes(byte[]) ; class Content.ImageFile(absolutePath: String)
class Content.AudioBytes(byte[]) ; class Content.AudioFile(absolutePath: String)
class Content.ToolResponse(name: String, response: Any)   // feed a tool result back

class ToolCall(name: String, arguments: Map<String,Any>) {
    val name: String
    val arguments: Map<String,Any>     // NOT a JSON string — a Map
}
```

### Extract assistant text from a streamed Message
```kotlin
fun Message.plainText(): String =
    contents.contents.filterIsInstance<Content.Text>().joinToString("") { it.text }
```

## Tools / function calling
```
interface ToolProvider
fun tool(t: ToolSet): ToolProvider          // top-level (ToolKt.tool)
fun tool(t: OpenApiTool): ToolProvider       // top-level (ToolKt.tool)

interface OpenApiTool {
    fun getToolDescriptionJsonString(): String   // the function spec the model sees
    fun execute(paramsJsonString: String): String
}

class ConversationConfig(
    systemInstruction: Contents = ...,
    initialMessages: List<Message> = emptyList(),
    tools: List<ToolProvider> = emptyList(),
    samplerConfig: SamplerConfig = ...,
    automaticToolCalling: Boolean = true,        // <- set FALSE for our tiered loop
    channels: List<Channel> = ...,
    extraContext: Map<String,Any> = ...,
    loraConfig: LoraConfig = ...,
)
```

### How OUR design uses this (tiered two-hop — `automaticToolCalling = false`)
We do NOT use LiteRT-LM's built-in auto tool loop, because it registers a FIXED
tool set at conversation creation and we need DYNAMIC tiering (model sees only
`search_tools`, then we inject discovered schemas next turn). So `LiteRtEngine`:
- `LocalLlm.generate(prompt): Flow<String>` → `createConversation()` (no tools) →
  `sendMessageAsync(prompt)` → map each `Message` to its `Content.Text` and emit
  the delta string.
- `ToolCallingLlm.generateWithTools(prompt, tools): Flow<LlmEvent>` →
  `createConversation(ConversationConfig(tools = tools.map { tool(openApiToolFor(it)) }, automaticToolCalling = false))`
  → `sendMessageAsync(prompt)` → for each `Message`: emit `LlmEvent.TextDelta(plainText)`
  if non-empty, then for each `tc in message.toolCalls` emit
  `LlmEvent.ToolCall(tc.name, tc.arguments.toJsonObject())`.
  `FcLoop.runAgent` owns the loop + dispatch + feed-back, so the engine stays
  STATELESS per call (fresh conversation each turn; matches FcLoop's full-prompt
  rebuild) and never needs `Content.ToolResponse`/`Message.tool`.

`openApiToolFor(schema: ToolSchema)`: an `OpenApiTool` whose
`getToolDescriptionJsonString()` returns the function spec built from the
schema's name/description/parameters, and whose `execute()` is a stub (never
invoked when `automaticToolCalling = false` — we dispatch via the BlackBox bridge).
TODO(verify): confirm the exact JSON shape `getToolDescriptionJsonString()`
expects (OpenAI/OpenAPI function-declaration format `{"name","description","parameters":<json-schema>}`)
against a LiteRT-LM example or `ReflectionTool` output during the device smoke.

## Prompt templating note (resolves the Phase-2 gotcha)
`Conversation.sendMessage(text)` applies Gemma's chat template ITSELF (one level).
`FcLoop.buildAgentPrompt` emits PLAIN-TEXT `User:`/`Assistant:` markers (not Gemma
turn tokens), so passing the full FcLoop prompt as one user message is templated
exactly ONCE — no double-templating. (Do not also emit `<start_of_turn>` tokens
in FcLoop.)

## Models (Hugging Face, ungated, Apache-2.0)
| slug | repo | file | size |
|---|---|---|---|
| gemma-4-e2b | `litert-community/gemma-4-E2B-it-litert-lm` | `gemma-4-E2B-it.litertlm` | ~2.59 GB |
| gemma-4-e4b | `litert-community/gemma-4-E4B-it-litert-lm` | `gemma-4-E4B-it.litertlm` | ~3.66 GB |

(Repo casing is uppercase `E2B`/`E4B`. Vendor NPU variants exist —
`*_qualcomm_sm8750.litertlm`, `*_Google_Tensor_G5.litertlm` — but the plain
`.litertlm` is the portable CPU/GPU build and the safe default.)
