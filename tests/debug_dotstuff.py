import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mail_server.queue import dot_stuff_message


def dot_unstuff_and_extract(stuffed: str) -> str:
    """Simulate what a receiving SMTP server does."""
    lines = stuffed.split("\r\n")
    data_lines = []
    for line in lines:
        if line == ".":
            break
        if line.startswith(".."):
            data_lines.append(line[1:])
        else:
            data_lines.append(line)
    return "\r\n".join(data_lines)


test_cases = [
    # 正常结尾
    (
        "From: a@b.com\r\nTo: c@d.com\r\nSubject: test\r\n\r\nHello world\r\n",
        "normal ending with \\r\\n"
    ),
    # 结尾没有换行
    (
        "From: a@b.com\r\nTo: c@d.com\r\nSubject: test\r\n\r\nHello world",
        "no ending newline"
    ),
    # 有单独的点行
    (
        "From: a@b.com\r\n\r\nLine one.\r\n.\r\nLine after single dot.\r\n",
        "contains single dot line"
    ),
    # 点开头的行
    (
        "From: a@b.com\r\n\r\n.normal line with dot prefix\r\n..two dots\r\n",
        "dot-prefixed lines"
    ),
    # 末尾有空行
    (
        "From: a@b.com\r\n\r\nBody text\r\n\r\n",
        "trailing empty lines"
    ),
]

all_pass = True
for raw, desc in test_cases:
    stuffed = dot_stuff_message(raw)
    restored = dot_unstuff_and_extract(stuffed)
    
    ok = (raw == restored)
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    
    print(f"[{status}] {desc}")
    if not ok:
        print(f"       original: {repr(raw)}")
        print(f"       restored: {repr(restored)}")
        # Find first diff
        for i, (a, b) in enumerate(zip(raw, restored)):
            if a != b:
                print(f"       first diff at pos {i}: {repr(a)} vs {repr(b)}")
                break
        if len(raw) != len(restored):
            print(f"       length: {len(raw)} vs {len(restored)}")
    print()

print(f"\nOverall: {'ALL PASS' if all_pass else 'SOME FAIL'}")
