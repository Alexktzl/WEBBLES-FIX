import ast


def run(expr):
    return ast.literal_eval(expr)


print(run("1 + 2"))
