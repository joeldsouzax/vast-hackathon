# Source audio and commentary

Gemini now receives original camera audio in the existing clip flow. The preview
previously removed all audio. The current model can understand and transcribe
audio in video, as described in Google's [audio documentation](https://ai.google.dev/gemini-api/docs/audio)
and [video documentation](https://ai.google.dev/gemini-api/docs/video-understanding).
The Supabase relay and provider models remain the same.

The selected microphone drives the voice decision, independently of the camera
on screen. Its words have their own source epoch and clock. The commentator
can leave a foreground speaker audible, show a useful exact quote, or add a
short line over background chatter. A quote appears as **Heard: …** and never
goes through text-to-speech. Spoken commentary uses the existing source-audio
ducking. Unknown words and speaker identities stay unknown.

```mermaid
flowchart LR
  Camera[Camera audio and video] --> Clip[Recorded clip]
  Clip --> Gemini[Gemini observations]
  Gemini --> Mic[Selected microphone evidence]
  Mic --> Crew[Commentator decision]
  Crew --> Listen[Leave speaker audible]
  Crew --> Quote[Show heard quote]
  Crew --> Talk[Speak over background]
  Quote --> Controller[Program controller]
  Talk --> Controller
```

The custom cabbie voice remains selected.
Its delivery is now more impatient: clipped phrases, exasperated pauses, dry
sarcasm and mock anger at delays. It avoids constant shouting and attendee insults.
The reusable [event template](../config/events/forever22.json) has the same style.

The controller checks exact quote text, microphone identity, epoch, mute state,
evidence availability and the original deadline. A quote lasts at most four
seconds. The demo now uses `audio_policy.foreground_priority=listen-first`.
A fresh selected-microphone observation with clear foreground words and an
understood meaning gets a listening turn before either commentator speaks.
Gemini can show an exact quote or abstain. A completed quote or response records
that evidence as heard; the next commentator can add a useful reaction.
Background chatter, unclear words and an active microphone alone do not block
narration. Muted microphones, other sources and replay audio do not create a live
listening turn. The original evidence deadline and microphone checks still apply.
This uses
short recorded clips; it cannot stop an already playing line the instant a
person begins speaking. Quotes are recent excerpts, not word-aligned subtitles.

`editorial_policy.event_talk_interval_s=25` limits event-only commentary to one
line per 25 seconds of program time. The controller checks the full delivery
ledger, including interrupted lines that viewers heard, rather than only the
short model history. Canceled, unheard text does not consume the interval.
Prepared event lines prevent duplicate preparation. Scene commentary must not
add sponsor lists as filler. When there is no useful new evidence, the room
sound stays audible. A reserved co-commentator turn does not override listening.

The focused direction suite passed 30 checks, including foreground versus
background decisions, forged quote rejection, microphone changes and captions
that never call TTS. A real retained phone clip included video and 16 kHz audio.
Two additional checks cover unclear transcripts and actual FFmpeg audio trims,
including multiple chunks and a missing audio track.
Gemini classified foreground speech and returned a 19-character transcript.
The proxy and provider work took 4.606 seconds. This diagnostic did not change
the program and does not prove transcription accuracy or viewer latency. See
[the measured result](evidence/source-audio.json).

After the runtime restart, reconnect a phone with the current QR and tap
**Join camera**. The first ready camera goes live automatically. Select its
microphone. Speak a clear short sentence, then pause.
Confirm that heard quotes match the words and cabbie commentary continues over
background chatter, with listening turns for clear speaker content. Change or mute the selected microphone and check
that an old quote cannot continue. These venue checks remain manual.

Release `breadcast-forever22-r10` passed 42 focused direction checks. Two real
Gemini requests on private scripted contexts selected a silent source quote for
clear foreground words and abstained when no evidence was available during the
event-talk cooldown. These are model checks, not live microphone acceptance.
See [listening-priority evidence](evidence/listening-priority.json).

## Default playback sound

Release `breadcast-forever22-r14` explicitly starts the Viewer and Studio players
unmuted at full player volume. The broadcast microphone already defaults to
unmuted, and joining already enables microphone sharing. The program still
selects one microphone independently from the camera view. The phone self-preview
stays silent to prevent feedback. Replay preview players no longer start muted;
the existing replay asset audio policy is unchanged.

When audible autoplay is blocked, video can continue muted. A trusted page click
or Enter/Space key retries sound within that interaction. The sound button still
works. Explicitly muting playback prevents later page interactions from turning
it back on. A newer play request supersedes an older rejected request, so a late
error cannot mute a newly unlocked player. Reloading starts with sound enabled
again. Device volume and browser permissions remain outside application control.

Browser limits are documented by [Chrome](https://developer.chrome.com/blog/autoplay/)
and [MDN](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay).
See [the focused check](evidence/default-sound.json). For the manual check, refresh
Viewer, tap the page if sound is blocked, then mute it and use another control.
The second action must not undo an explicit mute.

## Reconnect recovery

The controller now restores the selected unmuted microphone when the same
phone lease reconnects with a newer source epoch. It waits for fresh buffered
frames and preserves the current view, explicit mute and crew mode. A different
phone in the same slot cannot inherit the selection. See
[microphone recovery and measured audio](38-microphone-recovery.md).
