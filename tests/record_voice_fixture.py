#!/usr/bin/env python3
"""Record a spoken test fixture for i_ai_voice_test.py.

Usage: python3 tests/record_voice_fixture.py <name> "<sentence to read>" [seconds] [language]

A countdown runs, then the microphone is recorded (pw-record or arecord)
to tests/data/<name>.wav, and the sentence is saved as tests/data/<name>.txt,
the text the transcription is checked against.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from iai_voice import Recorder  # noqa: E402


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    name, sentence = sys.argv[1], sys.argv[2]
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0
    language = sys.argv[4] if len(sys.argv) > 4 else "en"
    data = os.path.join(HERE, "data")
    os.makedirs(data, exist_ok=True)
    print("Read this when told to speak:\n\n    {}\n".format(sentence), flush=True)
    for n in (3, 2, 1):
        print("{}...".format(n), flush=True)
        time.sleep(1)
    recorder = Recorder()
    recorder.start(data)
    print("SPEAK NOW ({:.0f} s)".format(seconds), flush=True)
    try:
        time.sleep(seconds)
    finally:
        path, duration = recorder.stop()
        if sys.exc_info()[0] is not None and path and os.path.exists(path):
            os.remove(path)  # Interrupted: no stray recording.
    target = os.path.join(data, name + ".wav")
    os.replace(path, target)
    with open(os.path.join(data, name + ".txt"), "w", encoding="utf-8") as handle:
        handle.write(sentence + "\n")
    with open(os.path.join(data, name + ".lang"), "w", encoding="utf-8") as handle:
        handle.write(language + "\n")
    print("Saved {} ({:.1f} s) and its expected text.".format(target, duration))


if __name__ == "__main__":
    main()
