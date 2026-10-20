def greet(name):
    if not name:
        return "hi there"
    return "hi " + name


def main():
    print(greet("world"))
