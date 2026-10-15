/* cmd_guess: which files a shell command writes or removes. */
#include <stdio.h>
#include <string.h>

#include "cmdguess.h"

static char got[2048];

static void on(void *ud, const char *dir, const char *path, GuessKind kind, const char *into) {
    (void)ud;
    char one[512];
    snprintf(one, sizeof one, "%s%s%s%s%s%s", *got ? " | " : "", kind == GUESS_DELETE ? "-" : "", dir ? dir : "",
             dir ? ":" : "", path, into ? "" : "");
    if (into) snprintf(one + strlen(one), sizeof one - strlen(one), "<%s", into);
    strncat(got, one, sizeof got - strlen(got) - 1);
}

int main(void) {
    static const char *cases[][2] = {
        {"sed -i 's/a/b/' f.c", "f.c"},
        {"sed -n 's/a/b/p' f.c", ""},
        {"sed -i.bak -e 's/a/b/' -e 's/c/d/' a.c b.c && false", "a.c | b.c"},
        {"sed -Ei '1,2d' src/x.c", "src/x.c"},
        {"sed -i '' -e 's/x/y/' mac.c", "mac.c"},
        {"cd /home/u/p/aigource && sed -i '1,2d' src/timeline.c && false", "/home/u/p/aigource:src/timeline.c"},
        {"cd sub; cd deeper; echo hi > out.txt", "sub/deeper:out.txt"},
        {"cd $X && touch a", ""},
        {"cd $X && touch /abs/a", "/abs/a"},
        {"echo x > a.txt 2>/dev/null; cat b >> c.txt 2>&1 >&2", "a.txt | c.txt"},
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
    if (!fail) puts("ok   cmdguess");
    return fail;
}
