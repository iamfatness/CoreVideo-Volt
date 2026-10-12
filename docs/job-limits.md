# Job limits

Four holes left by the race work. None need a core or a session.

## Publish heartbeat

`publish()` claims a version for 30 minutes and does not renew it. A long render loses the claim and a second writer takes the same version.

Done when a heartbeat extends `lease_expires_at` while the renderer runs, and a second writer still sees `busy` past the original lease.

## One proxy encode

`proxy:<source>` and `proxy-final:<asset>` are different jobs. Both encode. The later rename wins.

Done when a second build of the same asset does not start ffmpeg. It waits. One encode, one essence.

## Late open-file proxy

A build that started while the ISO was open can finish after wrap and replace the final proxy with the shorter file.

Done when that build discards its file and does not replace a proxy built after wrap.

## One transcribe

A runner and a hand-run `transcribe` both pay for the engine. The last write wins.

Done when the second call does not run the engine. It leaves the first transcript in place.
