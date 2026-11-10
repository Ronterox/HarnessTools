import argparse

import core


def main():
    p = argparse.ArgumentParser(description='Count notes from the database, a fresh copy from scratch.')
    p.add_argument('db', help='read the table from this file')
    args = p.parse_args()
    print(core.count(core.connect(args.db), 'me'))


if __name__ == '__main__':
    main()
