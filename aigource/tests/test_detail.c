/* detail_file_line: which line of the file a detail row opens in nvim. */
#include <stdio.h>

#include "transcript.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

int main(void) {
    /* An edit's diff: a removed run before an added line, one at a hunk's
     * end, a hunk of removals only; a read numbered from 10; rows that are
     * no file's lines. */
    DetailLine l[] = {
        {DL_DIM, 0, "/r/a.py"},
        {DL_HUNK, 0, "@@ -20,6 +20,5 @@"},
        {DL_TEXT, 20, "x20"},
        {DL_DEL, 22, "old22"}, /* 3 */
        {DL_DEL, 23, "old23"},
        {DL_ADD, 22, "x22"},
        {DL_TEXT, 23, "x23"},
        {DL_HUNK, 0, "@@ -40,3 +39,1 @@"}, /* 7 */
        {DL_TEXT, 39, "x39"},
        {DL_DEL, 41, "old41"}, /* 9: at the hunk's end */
        {DL_DEL, 42, "old42"},
        {DL_HUNK, 0, "@@ -5,2 +4,0 @@"}, /* 11 */
        {DL_DEL, 5, "old5"},
        {DL_DEL, 6, "old6"},
        {DL_TEXT, 0, ""}, /* 14: a gap */
        {DL_HEAD, 0, "$ make"},
        {DL_TEXT, 10, "read 10"}, /* 16 */
        {DL_TEXT, 11, "read 11"},
        {DL_ADD, 1, "created 1"}, /* 18 */
    };
    Detail d = {.tool = "Edit", .lines = l, .n = (int)(sizeof l / sizeof *l)};
    CHECK(detail_file_line(&d, 0) == 0);
    CHECK(detail_file_line(&d, 1) == 0);
    CHECK(detail_file_line(&d, 2) == 20);
    CHECK(detail_file_line(&d, 3) == 22); /* where it was: the added line after it */
    CHECK(detail_file_line(&d, 4) == 22);
    CHECK(detail_file_line(&d, 5) == 22);
    CHECK(detail_file_line(&d, 6) == 23);
    CHECK(detail_file_line(&d, 7) == 0);
    CHECK(detail_file_line(&d, 9) == 39); /* nothing after: the kept line before */
    CHECK(detail_file_line(&d, 10) == 39);
    CHECK(detail_file_line(&d, 12) == 4); /* removals only: the hunk's new start */
    CHECK(detail_file_line(&d, 13) == 4);
    CHECK(detail_file_line(&d, 14) == 0);
    CHECK(detail_file_line(&d, 15) == 0);
    CHECK(detail_file_line(&d, 16) == 10);
    CHECK(detail_file_line(&d, 17) == 11);
    CHECK(detail_file_line(&d, 18) == 1);
    if (!fails) puts("ok   detail lines");
    return fails != 0;
}
