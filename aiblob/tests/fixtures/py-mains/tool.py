import sys

import core


def run(path):
    with open(path) as fh:
        return core.words(fh.read())


if __name__ == '__main__':
    print(run(sys.argv[1]))
