# Event foundation fixtures

These fixtures prove application contracts. They do not prove live provider access
or recognition quality. `labels.json` declares two adjacent simulated actions in
the first 12 seconds of the existing `tests/fixtures/demo.mp4`. Labels also drive
small generated video segments in unit tests. Detection boxes and ranking are
simulated. Track IDs, participant names, scores, and sensor clocks remain unknown.

`speech.wav` is a prerecorded local fixture. It was produced on macOS with
`/usr/bin/say -v Samantha`, then converted with the pinned Docker FFmpeg to mono
24 kHz PCM WAV. Its phrase is declared in `labels.json`. The speech adapter returns
this phrase for every fixture request. It fully decodes the file and measures its
duration. This is not W&B output or a verified provider voice.

SHA-256 identities:

| File | SHA-256 |
|---|---|
| `../demo.mp4` | `5e70b96ad27dc8581424be7069ee9de8da9388b716e6fe213d88385f19baf80a` |
| `speech.wav` | `42ce770414fc2b85f519b65dcc5306fb5a241cefb45cf73c8245871935234603` |
| `labels.json` | `ed230adf4168bca65a9564e1e924a84734f93a3a4e26b06dad677dd58088d3eb` |

The marker check generates frame-coded video before evaluation. Its native/proxy
precision and one-program-frame tolerance are declared in the check function.
Generated media and decoded results stay in the ignored runtime evidence folder.

The declared Camera 5 fault delays fixture analysis by nine seconds for windows
that overlap source seconds 24–30. The original live deadline cancels that call.
The media check requires an expired job and continuous five-camera output.
