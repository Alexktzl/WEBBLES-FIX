def parse(s):
    try:
        return int(s)
    except:
        return None

print(parse("x"))
