#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "event.h"
#include "ingest.h"
#include "render.h"
#include "util.h"

static void usage(void) {
    fputs("usage: aigource [options] [transcript.jsonl ...]\n"
          "  Animates what Claude Code sessions do to a repo: replay, then follow live.\n"
          "  -r, --root DIR     repo root (default: current directory)\n"
          "  -s, --since HOURS  sessions written to in the last HOURS (default 2; the\n"
          "                     newest session when none qualify)\n"
          "  -a, --all          every session of the repo\n"
          "  -x, --speed N      replay speed multiplier (default 60)\n"
          "  -l, --live         skip the replay animation, start at now\n"
          "  -W, --size WxH     window size (default 1600x900)\n"
          "  --dump             print the normalized event stream as NDJSON and exit\n"
          "  --shot FILE        render, save a PNG after --frames frames, exit\n"
          "  --frames N         frames before --shot (default 600)\n"
          "  --seek F           start paused at fraction F (0..1) of the timeline\n"
          "  --mouse X,Y        fake mouse position for --shot (hover testing)\n"
          "Keys: space pause, arrows seek, +/- speed, L live, H legend, C camera,\n"
          "      F fullscreen, Q quit. Click or drag the timeline to jump.\n",
          stderr);
}

static int dump(Ingest *in, EvQueue *q) {
    ingest_poll(in);
    Event *evs;
    size_t n = evq_drain(q, &evs);
    qsort(evs, n, sizeof *evs, ev_cmp);
    for (size_t i = 0; i < n; i++) {
        ev_print_json(stdout, &evs[i]);
        ev_free(&evs[i]);
    }
    free(evs);
    return 0;
}

int main(int argc, char **argv) {
    char root[PATH_MAX];
    const char *root_arg = ".";
    double since_h = 2;
    int all = 0, want_dump = 0;
    VizOpts vo = {.speed = 60, .width = 1600, .height = 900, .frames = 600, .seek = -1,
                  .mouse_x = -1, .mouse_y = -1};
    const char **files = xcalloc((size_t)argc, sizeof *files);
    int nfiles = 0;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *v = i + 1 < argc ? argv[i + 1] : NULL;
#define TAKES(s, l) ((!strcmp(a, s) || !strcmp(a, l)) && v && ++i)
        if (TAKES("-r", "--root")) root_arg = v;
        else if (TAKES("-s", "--since")) since_h = atof(v);
        else if (TAKES("-x", "--speed")) vo.speed = atof(v);
        else if (TAKES("-W", "--size")) sscanf(v, "%dx%d", &vo.width, &vo.height);
        else if (TAKES("--shot", "--shot")) vo.shot = v;
        else if (TAKES("--frames", "--frames")) vo.frames = atoi(v);
        else if (TAKES("--seek", "--seek")) vo.seek = atof(v);
        else if (TAKES("--mouse", "--mouse")) sscanf(v, "%f,%f", &vo.mouse_x, &vo.mouse_y);
        else if (!strcmp(a, "-a") || !strcmp(a, "--all")) all = 1;
        else if (!strcmp(a, "-l") || !strcmp(a, "--live")) vo.live = 1;
        else if (!strcmp(a, "--dump")) want_dump = 1;
        else if (!strcmp(a, "-h") || !strcmp(a, "--help")) { usage(); return 0; }
        else if (*a == '-') { fprintf(stderr, "aigource: unknown option %s\n", a); usage(); return 2; }
        else files[nfiles++] = a;
#undef TAKES
    }
    if (!realpath(root_arg, root)) {
        /* Fixtures name a root that need not exist; keep it verbatim when absolute. */
        if (*root_arg != '/') { perror(root_arg); return 1; }
        snprintf(root, sizeof root, "%s", root_arg);
    }

    EvQueue q;
    evq_init(&q);
    IngestOpts io = {.root = root, .since = all ? 0 : now_wall() - since_h * 3600,
                     .files = files, .nfiles = nfiles};
    Ingest *in = ingest_new(&io, &q);
    if (want_dump) return dump(in, &q);

    vo.root = root;
    ingest_poll(in); /* load history before the first frame */
    if (!ingest_nfiles(in)) {
        fprintf(stderr, "aigource: no Claude sessions found for %s\n", root);
        return 1;
    }
    if (ingest_start(in)) {
        perror("aigource: ingest thread");
        return 1;
    }
    int rc = viz_run(&vo, &q);
    ingest_stop(in);
    return rc;
}
