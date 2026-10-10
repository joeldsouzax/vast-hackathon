# Demo showcase work

## Work list

- Complete: show suitable graphics and transitions while commentary continues.
- Complete: two commentators in one Gemini commentary flow. The impatient cabbie
  leads. A calm, dry co-commentator adds a useful explanation or short response.
  Do not alternate mechanically, overlap speech or invent exchanges with attendees.

## Graphics scope

The live logs showed completed director requests, repeated decisions to wait,
some timeouts and gaps with no eligible observation. One headline had aired.
The worker was not completely starved. Lower-third validation also rejected
graphics whenever a commentary cue was active.

Lower thirds now share airtime with narration. The lower graphic temporarily
replaces the speech caption; it does not stop speech. Full-screen cards retain
their separate speech exclusion.

The explicit event setting `editorial_policy.graphics_mode=showcase` enables a
controller sequence for prepared event facts. It needs no model worker or fresh
camera claim. It selects the least-used suitable preset from actual cue history.
It spaces cues by eight seconds and leaves at least twenty seconds between
transitions. Existing graphics, operator takeover, holding and replay prevent
new showcase cues. Every cue still uses the controller's source, context and
program revision checks.

The sequence covers eleven overlay presets and four transition presets. It uses
the supplied event title, organizer, venue, branding and actual live status.
Scores, unknown identities, countdowns and event-phase screens remain excluded.
Gemini can still propose a fresh scene or speech graphic between showcase cues.
Its decisions to abstain now appear in the trace instead of only the shared status.

Manual check: join one camera, leave commentary on, and watch for a headline,
a transition and a lower third. Speech must continue. Hold or Take control must
stop new showcase cues. A replay must still wait for approval.

Focused verification: 34 direction checks passed, including all fifteen showcase
presets during an active speech cue, transition spacing, Hold and takeover.
The rebuilt runtime health check passed. Phone/viewer verification remains pending.

The [commentary pair record](35-commentary-pair.md) documents the roles, voice
checks and remaining manual acceptance.
