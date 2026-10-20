/* cmd_guess: which files a shell command writes, removes (-) or reads (@, with
 * [first,last] for a read of some lines). */
#include <stdio.h>
#include <string.h>

#include "cmdguess.h"

static char got[2048];

static void on(void *ud, const char *dir, const char *path, GuessKind kind, const char *into, int first, int last) {
    (void)ud;
    char one[512];
    snprintf(one, sizeof one, "%s%s%s%s%s%s", *got ? " | " : "", kind == GUESS_DELETE ? "-" : kind == GUESS_READ ? "@" : "", dir ? dir : "",
             dir ? ":" : "", path, into ? "" : "");
    if (into) snprintf(one + strlen(one), sizeof one - strlen(one), "<%s", into);
    if (first || last) snprintf(one + strlen(one), sizeof one - strlen(one), "[%d,%d]", first, last);
    strncat(got, one, sizeof got - strlen(got) - 1);
}

int main(void) {
    static const char *cases[][2] = {
        {"sed -i 's/a/b/' f.c", "f.c"},
        {"sed -n 's/a/b/p' f.c", "@f.c"},
        {"sed -i.bak -e 's/a/b/' -e 's/c/d/' a.c b.c && false", "a.c | b.c"},
        {"sed -Ei '1,2d' src/x.c", "src/x.c"},
        {"sed -i '' -e 's/x/y/' mac.c", "mac.c"},
        {"cd /home/u/p/aigource && sed -i '1,2d' src/timeline.c && false", "/home/u/p/aigource:src/timeline.c"},
        {"cd sub; cd deeper; echo hi > out.txt", "sub/deeper:out.txt"},
        {"cd $X && touch a", ""},
        {"cd $X && touch /abs/a", "/abs/a"},
        {"echo x > a.txt 2>/dev/null; cat b >> c.txt 2>&1 >&2", "a.txt | c.txt | @b"},
        /* Reads. */
        {"cat -n src/a.c | head -40; sed -n '120,200p' src/b.c", "@src/a.c | @src/b.c[120,200]"},
        {"cd aigource && head -n 20 README.md && tail -5 Makefile && nl -ba x.h", "@aigource:README.md[1,20] | @aigource:Makefile[0,-5] | @aigource:x.h"},
        {"sed -e 's/a/b/' -n in.c > out.c", "out.c | @in.c"},
        {"while read l; do echo $l; done < list.txt; grep x <<< \"$v\"; wc -l <&3", "@list.txt"},
        {"cat <<EOF\nnot read\nEOF\ncat $F *.c", ""},
        {"perl -ne 'print' q.pl; less -R log.txt", "@log.txt"},
        {"head --lines 5 f1; tail --pid 123 -f f2; bat -l rust -r 1:2 f3; less -p pat +G f4; more +10 f5", "@f1[1,5] | @f2[0,-10] | @f3 | @f4 | @f5"},
        /* Line ranges: sed -n prints, head and tail counts. */
        {"sed -n 5p a; sed -n '10,40p' b; sed -n '7,$p' c; sed -n '1,$p' d; sed -n 9,3p e", "@a[5,5] | @b[10,40] | @c[7,0] | @d | @e[9,9]"},
        {"sed -n '1,5p;20,25p' f g; sed -ne 3p -e '8,9p' h; sed --quiet --expression=2p i", "@f[1,5] | @f[20,25] | @g[1,5] | @g[20,25] | @h[3,3] | @h[8,9] | @i[2,2]"},
        {"sed '5p' a; sed -n '/re/p' b; sed -n '1,5p;/x/p' c; sed -n -f s.sed d; sed -n \"$R\" e; sed -En 4p f", "@a | @b | @c | @d | @e | @f[4,4]"},
        {"head -30 a; head -n30 b; head --lines=3 c; head d; head -c 100 e; head -n -5 f; head -n 0 g; head -n $N h", "@a[1,30] | @b[1,30] | @c[1,3] | @d[1,10] | @e | @f | @h"},
        {"tail -n 20 a; tail -n +5 b; tail +1 c; tail -n +1 d; tail e; tail -c 9 f; tail -n 0 -f g; tail -n3 h", "@a[0,-20] | @b[5,0] | @c[0,-10] | @d | @e[0,-10] | @f | @h[0,-3]"},
        {"cat - f6; sed p -; tac -s x f7; less -o log.txt f8", "@f6 | @f7 | @f8"},
        {"(( i < n )) && for ((i=0;i<10;i++)); do cat a; done; (( $# < 2 )) || true", "@a"},
        {"(( x > 5 )) && false", ""},
        {"pushd sub && cat Makefile; popd; cat top.txt; popd; cat lost.txt", "@sub:Makefile | @top.txt"},
        {"make 2> err.log &> all.log", "err.log | all.log"},
        {"cat <<'EOF' > gen.h\necho no > nope\nsed -i x y\nEOF\nrm old.h", "gen.h | -old.h"},
        {"python3 - <<EOF\nopen('a','w')\nEOF", ""},
        {"echo \"$HOME\" > \"$OUT\"; echo x > \"my file.txt\"", "my file.txt"},
        {"ls *.c > list.txt; rm *.o", "list.txt"},
        {"grep x f | tee -a log1 log2 >/dev/null", "log1 | log2"},
        {"rm -rf build; rm -f a.o b.o", "-a.o | -b.o"},
        {"mv a.c b.c && mv x y dir/ && cp -r d e && cp f g", "-a.c | b.c<a.c | -x | dir/<x | -y | dir/<y | g<f"},
        {"mv -t dest one", "-one | dest<one"},
        {"perl -pi -e 's/a/b/' p.pl && perl -ne 'print' q.pl", "p.pl"},
        {"FOO=1 sudo -E touch -d tomorrow t1 && truncate -s 0 t2", "t1 | t2"},
        {"echo $(rm -f inner) > o.txt", "o.txt"},
        {"if true; then touch y; fi # touch comment", "y"},
        {"echo 'a > b' \"c > d\" \\> e", ""},
        {"diff <(sort a) <(sort b) > d.txt", "d.txt"},
        {"git commit -m 'x > y'", ""},
        {"git commit -m \"$(cat <<'EOF'\nUse 12\" screens > all\nEOF\n)\" && rm x", "-x"},
        {"git commit -m \"$(cat <<'EOF'\nFix \"it's\" bug\nEOF\n)\" && touch real", "real"},
        {"x=$(cat <<'EOF'\ndon't\nEOF\n)\nrm a.o", "-a.o"},
        {"(cd build && cmake ..) && make > log.txt", "log.txt"},
        {"[[ $a > b ]] && echo yes > ok.txt", "ok.txt"},
        {"perl -lpi -e 's/a/b/' l.pl", "l.pl"},
        {"touch ~bob/x; cd ~bob && touch y", ""},
        {"touch x && rm x", "x | -x"},
        {"if [[ $x > 3 ]]; then touch t; fi; ! [[ a > b ]]", "t"},
        {"x=$(\n y=$((1<<2))\n echo z\n)\ntouch tail", "tail"},
    };
    int fail = 0;
    for (size_t i = 0; i < sizeof cases / sizeof *cases; i++) {
        got[0] = 0;
        cmd_guess(cases[i][0], on, NULL);
        if (strcmp(got, cases[i][1])) {
            printf("FAIL cmdguess: %s\n  want: %s\n  got:  %s\n", cases[i][0], cases[i][1], got);
            fail = 1;
        }
    }
    /* cmd_runs with VERIFY, run in /r: 1 a run in the tree, 2 piped with pipefail off, 4 a run elsewhere. */
    static const struct { const char *cmd; int want; } runs[] = {
        {"make test 2>&1 | tail -20", 3},
        {"(cd aigource && make) | tail", 3},
        {"(cd aigource && make) 2>&1 | tail", 3},
        {"set -o pipefail; make | tail", 1},
        {"set -euo pipefail\nuv run pytest -q | tail -3", 1},
        {"set -o pipefail; set +o pipefail; make | tail", 3},
        {"echo pipefail; make | tail", 3},
        {"cat <<EOF\nset -o pipefail\nEOF\nmake | tail", 3},
        {"make && grep FAIL log | head", 1},
        {"make || true", 1},
        {"grep FAIL log | head", 0},
        {"echo make | sh", 0},
        {"cat x | make -f -", 1},
        {"make; python3 tests/a.py | head", 3},
        {"python3 -c \"print('(cd aigource && make) | tail')\"", 0},
        {"python3 - <<'EOF'\nmake | tail\nEOF", 0},
        {"cd /tmp/x && make test | tail", 4},
        {"cd \"$(mktemp -d)\" && make", 4},
        {"W=/tmp/wt; cd $W/aigource && make | tail; cd /r/sub && make", 5},
        {"cd ~ && make", 4},
        {"cd sub/.. && make | tail", 3},
        {"cd ../.. && make", 4},
        {"pushd /tmp; popd; make", 1},
        {"x=$(make 2>&1 | tail)", 0},
        {"timeout 60 nvim --headless -l t.lua 2>&1 | grep FAIL", 3},
    };
    regex_t re;
    regcomp(&re, CMD_VERIFY_ERE, REG_EXTENDED | REG_NOSUB);
    for (size_t i = 0; i < sizeof runs / sizeof *runs; i++) {
        int got_runs = cmd_runs(&re, runs[i].cmd, "/r", "/r");
        if (got_runs != runs[i].want) {
            printf("FAIL cmd_runs: %s\n  want %d, got %d\n", runs[i].cmd, runs[i].want, got_runs);
            fail = 1;
        }
    }
    regfree(&re);
    if (!fail) puts("ok   cmdguess");
    return fail;
}
