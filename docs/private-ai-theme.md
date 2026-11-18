# Private AI command centre

Original Tommy Tints red/gold/chrome reactor theme, scoped to the existing owner-only assistant. No film assets, character voice or actor cloning. Customer mascot unchanged.

States follow actual existing chat request events, speech recognition start/end/error and synthesis start/end/error. Standby rings are paused. Speech bars respond to word-boundary events (speech activity, not measured audio amplitude). Device engines without boundary support still display the speaking state. Reduced-motion disables transitions/animation. Backgrounding, navigation away and Stop cancel voice and microphone.

Optional introduction requires a button press and uses UK local time (Europe/London). It says the interface is ready; it does not claim banking or external integrations are online. Optional spoken replies default off. Only local-service en-GB synthesis voices qualify; otherwise the UI explains the missing device voice. There is no new speech provider, secret or server audio store. No messages are sent by the voice module.

Optional browser dictation requires a separate session-only consent checkbox and explicit Start. UI explains that the browser speech provider may process audio. Transcripts are not submitted automatically. Unsupported devices use keyboard text/dictation. Real physical iPhone Safari/PWA voice availability requires testing on the owner's device; no emulator result substitutes for that.

Existing private OpenAI chat, CSRF, owner authentication, business data permissions and approval gates are unchanged. Existing HTML no-store and automatic update injection continue to apply.

## Refined British delivery

Prefer local British voices advertised by the device as Premium, Enhanced, Natural or Neural, with Daniel/Oliver/Arthur as secondary preferences. These labels are device metadata, not a guarantee of quality. The owner can choose from actual installed local en-GB voices. Pace 0.90, pitch 0.94; no actor/character cloning. Introduction uses a more personal, restrained phrasing. Quality ultimately depends on the installed voice engine; this does not add a studio/cloud TTS voice or require credentials.

## Human-like neural speech

The default voice option now uses an owner-only, CSRF-protected POST /api/hq/assistant/speech with the existing OPENAI_API_KEY. Built-in Cedar voice is prompted for refined, warm, natural British phrasing, without actor imitation. UI discloses AI generation, text transmission to OpenAI and API charges. Device voices remain explicit alternatives; failures do not silently revert to robotic device output.

No audio files or spoken text are saved by HQ. Response no-store; audio uses a short-lived browser object URL revoked on stop/end/background/navigation. Optional spoken replies default off. Safari receives a native audio Play control if automatic playback is blocked. State follows actual playing events; bars show playback activity, not measured acoustic amplitude.

Limits: 2000 input characters, 3 MB output, 5 requests/minute and 60 requests/day per owner, including failed generation attempts. Durable usage table stores only owner subject and timestamps. No financial transaction or booking tools are added.

Model defaults to gpt-4o-mini-tts, overridable via OPENAI_TTS_MODEL. Model access and funded API billing must be verified in the owner's runtime. No production credentials or owner session are available to local tests; provider calls are mocked.

## Layered 3D reactor presentation

Decorative chrome bezel, depth-separated segmented rings, two inclined orbital rings, luminous energy lens and raised hub use CSS perspective/translateZ. No WebGL, canvas render loop, video or new external assets. Standby remains static. Existing request, actual microphone and actual speech playback states activate ring rotation and lens pulses. Startup is a short owner-activated reveal. Reduced-motion flattens depth and disables animation; state labels remain available. Carbon/red/gold styling is scoped to private HQ and customer mascot is unchanged. No fabricated connection counts or telemetry.
