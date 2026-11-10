import sys


def summarize(path):
    return open(path).read().count('\n')


def main():
    print(summarize(sys.argv[-1]))


if __name__ == '__main__':
    main()
