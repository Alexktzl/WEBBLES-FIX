def parse(s):
    try:
        return int(s)
    except ValueError:
        return None

print(parse("x"))
