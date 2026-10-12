# Shots

A scene cut is a span on the same asset. Search returns that span.

`index_shots` reads a closed hi-res. Cut times are seconds from the start of the file, added to the essence timecode, written as `shot` spans. The first shot starts at the essence start. Replacing the set deletes the old shot spans in the same step, so a second pass does not duplicate.

Without cut times, ffmpeg's scene filter finds them. An open file is refused.

Wrap queues a `shots` job beside the final proxy and the transcript. The runner writes the spans.
