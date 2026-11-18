# Private AI command centre

Original Tommy Tints red/gold/chrome reactor theme, scoped to the existing owner-only assistant. No film assets, character voice or actor cloning. Customer mascot unchanged.

States follow actual existing chat request events, speech recognition start/end/error and synthesis start/end/error. Standby rings are paused. Speech bars respond to word-boundary events (speech activity, not measured audio amplitude). Device engines without boundary support still display the speaking state. Reduced-motion disables transitions/animation. Backgrounding, navigation away and Stop cancel voice and microphone.

Optional introduction requires a button press and uses UK local time (Europe/London). It says the interface is ready; it does not claim banking or external integrations are online. Optional spoken replies default off. Only local-service en-GB synthesis voices qualify; otherwise the UI explains the missing device voice. There is no new speech provider, secret or server audio store. No messages are sent by the voice module.

Optional browser dictation requires a separate session-only consent checkbox and explicit Start. UI explains that the browser speech provider may process audio. Transcripts are not submitted automatically. Unsupported devices use keyboard text/dictation. Real physical iPhone Safari/PWA voice availability requires testing on the owner's device; no emulator result substitutes for that.

Existing private OpenAI chat, CSRF, owner authentication, business data permissions and approval gates are unchanged. Existing HTML no-store and automatic update injection continue to apply.
