import math

def grow(n):
    try:
        return math.exp(n)
    except OverflowError:
        return float("inf")

print(grow(1000))
