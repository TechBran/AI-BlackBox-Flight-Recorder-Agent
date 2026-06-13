# Gemini Pro TTS Voice Reference Guide

Complete guide for using Gemini Pro TTS with emotional cues for cinematic voice acting.

> **ElevenLabs voices are DYNAMIC — never hardcode them.** Unlike the static Gemini/OpenAI lists below, ElevenLabs voices (premade + your cloned/designed voices + added library voices) come live from `GET /tts/catalog` (the `elevenlabs` group; ids are `elevenlabs:<voice_id>`, ⭐ = My Voices). To synthesize, pass an `elevenlabs:<id>` voice to `text_to_speech`/`/tts`. To discover/manage them, use the Portal Voice Lab panel, the voice-library browse modal, or the `elevenlabs_list_voices`/`elevenlabs_clone_voice`/`elevenlabs_design_voice` tools. Defaults are quality-first (`eleven_v3`). Requires `ELEVENLABS_API_KEY`; check `GET /elevenlabs/status` for tier/capabilities. See `docs/plans/2026-06-12-elevenlabs-integration-design.md`.

## Male Voices (16 voices)

- **Puck** (Upbeat) - Young, energetic - Perfect for anxious teens (Morty!)
- **Charon** (Informative) - Authoritative narrator
- **Fenrir** (Excitable) - Manic, high-energy - Great for Rick!
- **Orus** (Firm) - Strong, commanding
- **Enceladus** (Breathy) - Rich, dramatic - Perfect for kings/nobles
- **Iapetus** (Clear) - Articulate, precise
- **Umbriel** (Easy-going) - Relaxed, casual
- **Algieba** (Smooth) - Warm narrator - Excellent for storytelling
- **Algenib** (Gravelly) - Rough, cynical - PERFECT for Rick Sanchez!
- **Rasalgethi** (Informative) - Educational tone
- **Alnilam** (Firm) - Authoritative
- **Schedar** (Even) - Balanced, neutral
- **Achird** (Friendly) - Approachable
- **Zubenelgenubi** (Casual) - Conversational
- **Sadachbia** (Lively) - Energetic, dynamic
- **Sadaltager** (Knowledgeable) - Wise, experienced

## Female Voices (14 voices)

- **Zephyr** (Bright) - Cheerful, light
- **Kore** (Firm) - Strong, commanding - Queens, leaders
- **Leda** (Youthful) - Young, fresh
- **Aoede** (Breezy) - Light, conversational
- **Callirrhoe** (Easy-going) - Relaxed
- **Autonoe** (Bright) - Clear, energetic
- **Despina** (Smooth) - Elegant, flowing - NOT for male characters!
- **Erinome** (Clear) - Articulate
- **Laomedeia** (Upbeat) - Cheerful, energetic
- **Achernar** (Soft) - Gentle, warm
- **Pulcherrima** (Forward) - Confident, direct
- **Vindemiatrix** (Gentle) - Soft, kind
- **Sulafat** (Warm) - Comforting, friendly
- **Gacrux** (Mature) - Experienced, wise

## Emotional Cue Format

**CRITICAL:** Gemini Pro TTS requires emotional cues in **(parentheses) BEFORE the words** they modify.

### Correct Format:
```
"(frantically, with burp) Morty! Get in here! (impatiently) I need your help!"
```

### WRONG Format:
```
"Morty! Get in here, he said frantically. I need your help, he said impatiently."
```

## Emotional Cue Types

**Actions:** (burps), (laughs), (sighs), (whispers), (shouts), (screams)

**Emotions:** (nervously), (angrily), (sadly), (joyfully), (with dread), (with fear)

**Delivery Styles:** (sarcastically), (matter-of-factly), (triumphantly), (smugly)

**Combinations:** (with manic enthusiasm, burps), (softly, with rare emotion), (frantically, stammering)

## Character Voice Examples

### Rick Sanchez (Rick and Morty)
**Best Voice:** Algenib (Gravelly) or Fenrir (Excitable)

```
"(frantically, burps) Morty! (impatiently) Get the quantum stabilizer!"
"(sarcastically) Oh great, another dimension where Jerry's successful. (burps dismissively)"
"(with manic enthusiasm) We're gonna save the multiverse, Morty! (triumphantly)"
```

### Morty Smith (Rick and Morty)
**Best Voice:** Puck (Upbeat)

```
"(nervously) Aw geez, Rick. (anxiously) I don't know about this..."
"(panicking) Oh my god, oh my god! (screaming in terror)"
"(with fierce determination) We HAVE to stop it!"
```

### Fantasy King
**Best Voice:** Enceladus (Breathy) or Orus (Firm)

```
"(with commanding authority) I am Aldric, descendant of Dragon Lords!"
"(wearily) Forty winters I have held this crown. (with heavy resignation)"
"(declared with fierce determination) Your oath is to my bloodline!"
```

### Fantasy Queen
**Best Voice:** Kore (Firm) or Gacrux (Mature)

```
"(with measured grace) My lord, the prophecy speaks of fire and ash."
"(with unwavering strength) Then we ride faster, Commander."
"(fierce conviction) The dragon chooses courage, not blood!"
```

### Narrator (Fantasy/Epic)
**Best Voice:** Algieba (Smooth) or Charon (Informative)

```
"(with grave authority) In the kingdom of Westmoor, where dragons once ruled..."
"(ominously) But age has weakened him, and vultures circle."
"(with epic grandeur) The age of dragons had returned."
```

## Production Voice Combinations

**Fantasy Epic:**
- Narrator: Algieba (Smooth)
- King: Enceladus (Breathy)
- Queen: Kore (Firm)
- Villain: Umbriel (Easy-going) + dark emotional cues
- Warrior: Fenrir (Excitable)

**Sci-Fi Adventure:**
- Narrator: Charon (Informative)
- Commander: Orus (Firm)
- Scientist: Sadaltager (Knowledgeable)
- AI: Iapetus (Clear)
- Engineer: Achird (Friendly)

**Comedy (Rick and Morty style):**
- Rick: Algenib (Gravelly) or Fenrir (Excitable)
- Morty: Puck (Upbeat)
- Villain: Rasalgethi (Informative) + menacing cues
- Comic Relief: Puck (Upbeat) + silly cues

## Why Emotional Cues Matter

**Without Cues:**
- Flat, monotone delivery
- No emotional range
- Sounds robotic
- Loses audience engagement

**With Cues:**
- Dynamic prosody and emphasis
- Full emotional range (joy, fear, anger, sarcasm)
- Natural-sounding performance
- Cinematic voice acting quality

**Example Comparison:**

**No cues:** "Morty, we need to go. The multiverse is collapsing."
**With cues:** "(frantically, burps) Morty, we need to go! (with urgent dread) The multiverse is collapsing!"

**The second version is 10x more engaging.**

## Quick Reference

**For narration:** Algieba (Smooth), Charon (Informative)
**For heroes:** Orus (Firm), Fenrir (Excitable), Kore (Firm)
**For villains:** Algenib (Gravelly), Umbriel (Easy-going) + dark cues
**For comedy:** Puck (Upbeat), Fenrir (Excitable)
**For youth:** Puck (Upbeat), Leda (Youthful), Zephyr (Bright)
**For authority:** Charon, Orus, Alnilam, Kore
**For warmth:** Algieba (Smooth), Sulafat (Warm), Achird (Friendly)

## Remember

**Always use emotional cues in Gemini Pro TTS.** It's the difference between robotic text-to-speech and cinematic voice acting.
