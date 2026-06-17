import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mail_server.queue import dot_stuff_message


def simulate_receiver(stuffed: str) -> str:
    """
    Simulate exactly what an SMTP receiver does line-by-line.
    Returns the raw DATA content as received.
    """
    lines = stuffed.split("\r\n")
    collected = []
    for line in lines:
        if line == ".":
            break
        if line.startswith(".."):
            collected.append(line[1:])
        else:
            collected.append(line)
    return "\r\n".join(collected) + "\r\n"


test_cases = [
    # Case 1: Message already ends with \r\n (normal case)
    (
        "From: a@b.com\r\nTo: c@d.com\r\nSubject: test\r\n\r\nHello world\r\n",
        "normal ending \\r\\n"
    ),
    # Case 2: Message with dot content AND normal ending
    (
        "From: a@b.com\r\n\r\n"
        "Line one.\r\n"
        ".This line starts with a period.\r\n"
        "..Two periods at start.\r\n"
        "Normal line in middle.\r\n"
        ".\r\n"
        "Line after single dot.\r\n"
        "...Three dots at start.\r\n",
        "dot content with normal ending"
    ),
    # Case 3: Message with body ending in empty lines
    (
        "From: a@b.com\r\n\r\nBody text\r\n\r\n",
        "body ending in multiple newlines"
    ),
    # Case 4: No header/body separator weirdness - simple
    (
        "From: sender@example.com\r\n"
        "To: recipient@mock.test\r\n"
        "Subject: dot stuffing test\r\n"
        "\r\n"
        "Line one.\r\n"
        ".This line starts with a period.\r\n"
        "..Two periods at start.\r\n"
        "Normal line in middle.\r\n"
        ".\r\n"
        "Line after single dot.\r\n"
        "...Three dots at start.\r\n",
        "exact original test message"
    ),
]

all_pass = True
for raw, desc in test_cases:
    stuffed = dot_stuff_message(raw)
    restored = simulate_receiver(stuffed)
    
    ok = (raw == restored)
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    
    print(f"[{status}] {desc}")
    if not ok:
        print(f"       len(original)={len(raw)}, len(restored)={len(restored)}")
        # show repr comparison
        if len(raw) < 500 and len(restored) < 500:
            print(f"       original: {repr(raw)}")
            print(f"       restored: {repr(restored)}")
        # find diff
        min_len = min(len(raw), len(restored))
        for i in range(min_len):
            if raw[i] != restored[i]:
                print(f"       first diff at pos {i}: {repr(raw[i])} vs {repr(restored[i])}")
                print(f"       context before: original={repr(raw[max(0,i-10):i])} restored={repr(restored[max(0,i-10):i])}")
                print(f"       context after : original={repr(raw[i:i+10])} restored={repr(restored[i:i+10])}")
                break
        else:
            if len(raw) != len(restored):
                longer = raw if len(raw) > len(restored) else restored
                extra_start = min_len
                print(f"       length differs. extra chars from pos {extra_start}: {repr(longer[extra_start:extra_start+50])}")
    print()

print(f"\nOverall: {'ALL PASS' if all_pass else 'SOME FAIL'}")
