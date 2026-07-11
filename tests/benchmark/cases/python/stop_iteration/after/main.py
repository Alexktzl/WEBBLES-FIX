def first(gen):
    it = iter(gen)
    return next(it, None)

print(first([]))
