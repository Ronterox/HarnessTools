#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "analysis.h"
#include "event.h"
#include "ingest.h"
#include "metrics.h"
#include "render.h"
#include "transcript.h"
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
          "  -w, --watch        also show filesystem changes (yours, other tools), live\n"
          "                     only; agent commands' changes go to that agent\n"
          "  -W, --size WxH     window size (default 1600x900)\n"
          "  --dump             print the normalized event stream as NDJSON and exit\n"
          "  --follow SECS      with --dump: keep printing new events for SECS seconds\n"
          "  --detail           with --dump: after each step, what the detail window shows\n"
          "                     and the agent's comments on it\n"
          "  --structure-dump   print the structure analysis (tree, turns, changes) as JSON\n"
          "                     and exit; counts go to stderr\n"
          "  --metrics FILE     with --structure-dump: also write what the structure view\n"
          "                     derives from it (per node and turn) to FILE as JSON;\n"
          "                     build times go to stderr\n"
          "  --rebuild N        with --structure-dump: build N more times on the same\n"
          "                     analysis (its caches warm), adding any new events first,\n"
          "                     as live rebuilds do; prints the last, each time to stderr\n"
          "  --shot FILE        render, save a PNG after --frames frames, exit\n"
          "  --frames N         frames before --shot (default 600)\n"
          "  --seek F           start paused at fraction F (0..1) of the timeline\n"
          "  --mouse X,Y        fake mouse position for --shot (hover testing)\n"
          "  --turn ID          open the turn window of the actor whose id starts with ID\n"
          "  --step N           with --turn: open its step N in full (negative: from the end)\n"
          "  --file PATH        open the last change of this file, as a click on it does\n"
          "  --structure VIEW[:MODE[:PATH]]\n"
          "                     open the structure screen: icicle, treemap or floating;\n"
          "                     a heat mode (turns, lines, risk, rework, fixes, context,\n"
          "                     cost, time, age, owner); a folder to focus, or a file or\n"
          "                     function to inspect (path below the root)\n"
          "  --structure-turn N select its Nth turn in time order (negative: from the end)\n"
          "  --structure-search QUERY\n"
          "                     open the structure screen's search box on QUERY\n"
          "  --structure-pick N with --structure-search: go to its Nth result, as Enter\n"
          "Keys: space pause, arrows seek, +/- speed, L live, H legend, C camera,\n"
          "      F fullscreen, Q quit, Esc close window, S structure screen. Click or\n"
          "      drag the timeline to jump; click an actor to see its current turn;\n"
          "      click a line of code to open it in the nvim working in the root.\n"
          "      Structure screen: 1 2 3 views, [ ] turns, Esc/Backspace up, R rebuild,\n"
          "      / or Ctrl+F search (Enter go, n/N next/previous).\n",
          stderr);
}

/* A step's detail window content as one NDJSON line. */
static void print_detail(const Event *e) {
    Detail *d = tr_detail(e->src, e->src_off, e->tool_id, e->path);
    static const char *kinds[] = {"text", "head", "dim", "hunk", "add", "del", "err"};
    fputs("{\"detail\":", stdout);
    json_str(stdout, d->tool);
    printf(",\"state\":%d,\"lines\":[", d->state);
    for (int i = 0; i < d->n; i++) {
        printf("%s[\"%s\",%d,", i ? "," : "", kinds[d->lines[i].kind], d->lines[i].no);
        json_str(stdout, d->lines[i].s);
        putchar(']');
    }
    fputs("],\"notes\":[", stdout);
    Notes ns = tr_notes(e->src, e->note_off, e->notes, e->tool_id);
    for (int i = 0; i < ns.n; i++) {
        if (i) putchar(',');
        json_str(stdout, ns.s[i]);
    }
    puts("]}");
    notes_free(&ns);
    detail_free(d);
}

/* The structure analysis of the loaded history, as JSON. */
static int structure_dump(Ingest *in, EvQueue *q, const char *root, const char *metrics, int rebuild) {
    FILE *mf = metrics ? fopen(metrics, "w") : NULL; /* before the long build: fail fast */
    if (metrics && !mf) {
        perror(metrics);
        ingest_free(in);
        return 1;
    }
    ingest_poll(in);
    Event *evs;
    size_t n = evq_drain(q, &evs);
    Analysis *a = an_new(root);
    an_add(a, evs, n);
    for (size_t i = 0; i < n; i++) ev_free(&evs[i]);
    free(evs);
    double t0 = 0, t1 = 0;
    AnSnapshot *s = NULL;
    for (int i = 0; i <= rebuild; i++) { /* --rebuild: again on the same analysis, new events first */
        if (i) {
            ingest_poll(in);
            size_t k = evq_drain(q, &evs);
            an_add(a, evs, k);
            for (size_t j = 0; j < k; j++) ev_free(&evs[j]);
            free(evs);
            n += k;
            an_snapshot_free(s);
        }
        t0 = now_wall();
        s = an_build(a);
        t1 = now_wall();
        if (rebuild) fprintf(stderr, "build %d: %.1f ms, %zu events\n", i + 1, (t1 - t0) * 1e3, n);
    }
    an_snapshot_json(stdout, s);
    an_snapshot_stats(stderr, s);
    int rc = 0;
    if (metrics) {
        StMetrics *m = st_build(s);
        double t2 = now_wall();
        st_json(mf, m);
        if (fclose(mf)) {
            perror(metrics);
            rc = 1;
        }
        fprintf(stderr, "an_build %.1f ms, st_build %.1f ms\n", (t1 - t0) * 1e3, (t2 - t1) * 1e3);
        st_free(m);
    }
    an_snapshot_free(s);
    an_free(a);
    ingest_free(in);
    return rc;
}

/* History as sorted NDJSON; with follow > 0, then new events as they come. */
static int dump(Ingest *in, EvQueue *q, double follow, int details) {
    double until = now_wall() + follow;
    do {
        ingest_poll(in);
        Event *evs;
        size_t n = evq_drain(q, &evs);
        if (n) qsort(evs, n, sizeof *evs, ev_cmp);
        for (size_t i = 0; i < n; i++) {
            ev_print_json(stdout, &evs[i]);
            if (details && evs[i].src && ev_staged(evs[i].kind)) print_detail(&evs[i]);
            ev_free(&evs[i]);
        }
        free(evs);
        fflush(stdout);
        if (follow > 0) usleep(100000);
    } while (now_wall() < until);
    return 0;
}

int main(int argc, char **argv) {
    char root[PATH_MAX];
    const char *root_arg = ".", *metrics = NULL;
    double since_h = 2;
    int all = 0, want_dump = 0, watch = 0, details = 0, want_structure = 0, rebuild = 0;
    double follow = 0;
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
        else if (TAKES("--turn", "--turn")) vo.turn = v;
        else if (TAKES("--step", "--step")) vo.step = v;
        else if (TAKES("--file", "--file")) vo.file = v;
        else if (TAKES("--structure", "--structure")) vo.structure = v;
        else if (TAKES("--structure-turn", "--structure-turn")) vo.structure_turn = atoi(v);
        else if (TAKES("--structure-search", "--structure-search")) vo.structure_search = v;
        else if (TAKES("--structure-pick", "--structure-pick")) vo.structure_pick = atoi(v);
        else if (TAKES("--follow", "--follow")) follow = atof(v);
        else if (TAKES("--metrics", "--metrics")) metrics = v;
        else if (TAKES("--rebuild", "--rebuild")) rebuild = atoi(v);
        else if (!strcmp(a, "-a") || !strcmp(a, "--all")) all = 1;
        else if (!strcmp(a, "-l") || !strcmp(a, "--live")) vo.live = 1;
        else if (!strcmp(a, "-w") || !strcmp(a, "--watch")) watch = 1;
        else if (!strcmp(a, "--dump")) want_dump = 1;
        else if (!strcmp(a, "--detail")) details = 1;
        else if (!strcmp(a, "--structure-dump")) want_structure = 1;
        else if (!strcmp(a, "-h") || !strcmp(a, "--help")) { usage(); return 0; }
        else if (*a == '-') { fprintf(stderr, "aigource: unknown option %s\n", a); usage(); return 2; }
        else files[nfiles++] = a;
#undef TAKES
    }
    if ((metrics || rebuild) && !want_structure) {
        fputs("aigource: --metrics and --rebuild need --structure-dump\n", stderr);
        return 2;
    }
    if (rebuild < 0) {
        fputs("aigource: --rebuild needs a count of 0 or more\n", stderr);
        return 2;
    }
    if (!realpath(root_arg, root)) {
        /* Fixtures name a root that need not exist; keep it verbatim when absolute. */
        if (*root_arg != '/') { perror(root_arg); return 1; }
        snprintf(root, sizeof root, "%s", root_arg);
    }

    EvQueue q;
    evq_init(&q);
    IngestOpts io = {.root = root, .since = all ? 0 : now_wall() - since_h * 3600,
                     .files = files, .nfiles = nfiles, .watch = watch};
    Ingest *in = ingest_new(&io, &q);
    if (want_dump) return dump(in, &q, follow, details);
    if (want_structure) {
        int rc = structure_dump(in, &q, root, metrics, rebuild);
        free(files);
        free(q.items);
        return rc;
    }

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
