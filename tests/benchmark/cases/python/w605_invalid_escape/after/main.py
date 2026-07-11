import re

def find_digits(s):
    return re.findall(r"\d+", s)

print(find_digits("a1b2"))
